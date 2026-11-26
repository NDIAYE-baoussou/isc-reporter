"""``iscr-clean``: the only part of isc-reporter that changes a tenant.

``iscr`` only reads. ``iscr-clean`` deletes, and only what a reviewed plan lists:

1. ``iscr externals`` writes the plan: the accounts of non-employee and delimited file sources whose
   end date, and their identity's, is past, on the source the identity profile is built from.
2. ``iscr-clean externals PLAN --env NAME`` checks every account of the plan again against the
   tenant, with the plan's settings, and says what it would delete. Nothing is changed.
3. With ``--apply``, after a typed confirmation (or ``--confirm COUNT``), it saves a backup (the
   accounts, records and identities, and the export of each non-employee source), then deletes:
   the non-employee record (ISC deletes the account and the identity), or the account of a
   delimited file source. Every call is written to an audit file as it happens.

Safeguards: the environment must be named (``--env``), the plan must be for that tenant and recent
(``--max-age``), at most ``--max`` accounts are deleted in one run, an account that changed since the
plan is skipped, and the run stops on a permission error or after repeated failures.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import warnings
from datetime import datetime, timezone

from isc_reporter import __version__
from isc_reporter.csvio import output_path
from isc_reporter.errors import UsageError
from isc_reporter.logs import setup_logging

log = logging.getLogger("isc_reporter")

AUDIT_COLUMNS = ["Time", "Source", "Type", "Account", "Identity", "End date", "Result", "Detail"]
WOULD_DELETE, SKIPPED, DELETED, NOT_FOUND, FAILED = ("Would be deleted", "Skipped", "Deleted", "Not found",
                                                      "Failed")
STOP_STATUS = {401, 403}
MAX_FAILURES = 5


# -- plan --------------------------------------------------------------------------------


def read_plan(path: str, max_age: int, now: datetime | None = None) -> dict:
    """The plan written by ``iscr externals``, refused when it is malformed or older than *max_age*
    days."""
    from isc_reporter.externals import PLAN_FORMAT

    try:
        with open(path, encoding="utf-8") as fh:
            plan = json.load(fh)
    except OSError as exc:
        raise UsageError(f"cannot read the plan {path} ({exc.strerror})") from exc
    except ValueError as exc:
        raise UsageError(f"{path} is not valid JSON ({exc})") from exc
    if not isinstance(plan, dict) or plan.get("format") != PLAN_FORMAT:
        raise UsageError(f"{path} is not a plan written by 'iscr externals'")
    try:
        created = datetime.fromisoformat(str(plan.get("created")))
    except ValueError as exc:
        raise UsageError(f"{path}: the date of the plan cannot be read") from exc
    if created.tzinfo is None:
        created = created.replace(tzinfo=timezone.utc)
    age = ((now or datetime.now(timezone.utc)) - created).days
    if age > max_age:
        raise UsageError(f"the plan is {age} day(s) old, more than --max-age {max_age}: run 'iscr externals' "
                         "again for a fresh one")
    deletions = plan.get("deletions")
    if not isinstance(deletions, list) or not isinstance(plan.get("settings"), dict):
        raise UsageError(f"{path} has no list of deletions")
    for number, entry in enumerate(deletions, 1):
        missing = [key for key in ("source", "kind", "accountId", "endDate")
                   if not isinstance(entry, dict) or not entry.get(key)]
        if missing:
            raise UsageError(f"{path}: entry {number} has no {', '.join(missing)}")
    return plan


def same_tenant(first: str, second: str) -> bool:
    return str(first or "").rstrip("/").lower() == str(second or "").rstrip("/").lower()


# -- checking again ------------------------------------------------------------------------


def recheck(isc, plan: dict):
    """``(to delete, skipped)``: the entries of *plan* that are still to delete in the tenant, each
    with its current :class:`~isc_reporter.externals.Row`, and the others with why they are skipped."""
    from isc_reporter import externals

    settings = externals.Settings.from_json(plan["settings"])
    entries = plan["deletions"]
    data = externals.fetch(isc, list(dict.fromkeys(e["source"] for e in entries)), settings)
    for note in data.notes:
        log.warning("%s", note)
    result = externals.analyse(data)
    rows = {row.account.get("id"): row for row in result.rows}
    existing = {a.get("id") for accounts in data.accounts.values() for a in accounts}
    keep, skipped = [], []
    for entry in entries:
        row = rows.get(entry["accountId"])
        if entry["accountId"] not in existing:
            skipped.append((entry, "the account is no longer in ISC"))
        elif row is None:
            skipped.append((entry, "its end date is no longer past"))
        elif row.status != externals.TO_DELETE:
            why = "; ".join(f"{label}: {detail}" for label, detail in row.reasons)
            skipped.append((entry, f"now {row.status.lower()}" + (f" ({why})" if why else "")))
        elif row.end_text != entry["endDate"]:
            skipped.append((entry, f"its end date changed from {entry['endDate']} to {row.end_text}"))
        elif (row.account.get("identityId") or "") != (entry.get("identityId") or ""):
            skipped.append((entry, "it is now correlated to another identity"))
        elif row.source.kind != entry["kind"]:
            skipped.append((entry, f"its source is now of type {row.source.kind}"))
        elif (row.source.kind == externals.NON_EMPLOYEE
              and (row.record or {}).get("id") != entry.get("recordId")):
            skipped.append((entry, "its non-employee record changed"))
        else:
            keep.append((entry, row))
    return keep, skipped


# -- confirmation, backup, deletion --------------------------------------------------------


def confirm(count: int, tenant: str, given: int | None, ask=input, interactive: bool | None = None) -> None:
    """Raise unless the deletion of *count* accounts is confirmed: by ``--confirm`` with the same count,
    or by typing ``delete <count>``."""
    if given is not None:
        if given != count:
            raise UsageError(f"--confirm {given} does not match the {count} account(s) to delete: nothing "
                             "deleted")
        return
    if not (sys.stdin.isatty() if interactive is None else interactive):
        raise UsageError(f"not run from a terminal: give --confirm {count} to delete without a prompt")
    phrase = f"delete {count}"
    answer = ask(f'Type "{phrase}" to delete {count} account(s) in {tenant}, anything else to stop: ')
    if answer.strip().lower() != phrase:
        raise UsageError("not confirmed: nothing deleted")


def _safe(name: str) -> str:
    return re.sub(r"[^\w.-]+", "-", name).strip("-") or "source"


def _download(func, **kwargs) -> bytes:
    from isc_reporter.api import HttpError, call

    def once():
        response = func(**kwargs)
        if response.status >= 400:
            raise HttpError(response.status, response.headers, response.data)
        return response.data or b""

    once.__name__ = getattr(func, "__name__", "API")
    return call(once)


def backup(isc, folder: str, keep: list) -> list[str]:
    """Save what is about to be deleted: the accounts, records and identities, and the export of each
    non-employee source involved. Raises when an export fails, before anything is deleted."""
    from isc_reporter.externals import NON_EMPLOYEE, source_ids

    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "before-deletion.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump([{"plan": entry, "account": row.account, "record": row.record, "identity": row.identity}
                   for entry, row in keep], fh, indent=2, ensure_ascii=False, default=str)
    written = [path]
    sources = {row.source.id: row.source for _, row in keep if row.source.kind == NON_EMPLOYEE}
    for info in sources.values():
        content, problem = None, ""
        for source_id in source_ids(info):
            try:
                content = _download(isc.non_employee.export_non_employee_records_v1_without_preload_content,
                                    id=source_id)
                break
            except Exception as exc:
                from isc_reporter.api import error_message

                problem = error_message(exc)
        if content is None:
            raise UsageError(f"the non-employees of '{info.name}' could not be exported ({problem}): nothing "
                             "deleted")
        path = os.path.join(folder, f"non-employees_{_safe(info.name)}.csv")
        with open(path, "wb") as fh:
            fh.write(content)
        written.append(path)
    return written


def delete(isc, entry: dict, row) -> tuple[str, str]:
    """Delete one account; returns (result, detail). Raises on 401 and 403: the run stops."""
    from isc_reporter.api import call, error_message, model_json
    from isc_reporter.externals import NON_EMPLOYEE

    try:
        if row.source.kind == NON_EMPLOYEE:
            call(isc.non_employee.delete_non_employee_record_v1, id=row.record["id"])
            return DELETED, "non-employee record deleted: ISC deletes the account and the identity"
        task = model_json(call(isc.accounts.delete_account_v1, id=entry["accountId"])).get("id", "")
        return DELETED, ("account delete task " + task).strip() + "; remove the row from the file"
    except Exception as exc:
        status = getattr(exc, "status", None)
        if status == 404:
            return NOT_FOUND, "not found: already deleted?"
        if status in STOP_STATUS:
            raise
        return FAILED, error_message(exc)


class Audit:
    """The audit file, one line per account, written as each line happens."""

    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._file = open(path, "w", newline="", encoding="utf-8-sig")  # noqa: SIM115 - closed in close()
        self._writer = csv.writer(self._file, delimiter=";")
        self._writer.writerow(AUDIT_COLUMNS)

    def write(self, entry: dict, result: str, detail: str) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self._writer.writerow([now, entry.get("source", ""), entry.get("kind", ""), entry.get("account", ""),
                               entry.get("identity", ""), entry.get("endDate", ""), result, detail])
        self._file.flush()

    def close(self) -> None:
        self._file.close()


# -- command -----------------------------------------------------------------------------


def _externals(args) -> int:
    from isc_reporter.api import error_message
    from isc_reporter.client import connect
    from isc_reporter.externals import DELIMITED, other_accounts

    plan = read_plan(args.plan, args.max_age)
    entries = plan["deletions"]
    if not entries:
        log.info("The plan lists no account to delete")
        return 0
    isc = connect(None, args.env)
    if not same_tenant(plan.get("tenant", ""), isc.base_url):
        raise UsageError(f"the plan is for {plan.get('tenant')}, but the environment '{args.env}' is "
                         f"{isc.base_url}: nothing deleted")
    log.info("Connected to %s (environment '%s'); checking the %d account(s) of the plan again", isc.base_url,
             args.env, len(entries))
    keep, skipped = recheck(isc, plan)
    for entry, why in skipped:
        log.warning("Skipped %s / %s: %s", entry["source"], entry.get("account", entry["accountId"]), why)
    log.info("%d of %d account(s) of the plan are still to delete", len(keep), len(entries))
    folder = args.output or output_path(args.output_dir, "clean-externals", "")
    if not args.apply:
        audit = Audit(os.path.join(folder, "checked.csv"))
        for entry, _ in keep:
            audit.write(entry, WOULD_DELETE, "")
        for entry, why in skipped:
            audit.write(entry, SKIPPED, why)
        audit.close()
        log.info("Checked: %s", audit.path)
        log.info("Nothing was deleted. To delete these %d account(s): add --apply", len(keep))
        return 0
    if not keep:
        log.info("Nothing to delete")
        return 0
    if len(keep) > args.max:
        raise UsageError(f"{len(keep)} account(s) to delete, more than --max {args.max}: raise --max, or "
                         "remove accounts from the plan")
    confirm(len(keep), isc.base_url, args.confirm)
    for path in backup(isc, os.path.join(folder, "backup"), keep):
        log.info("Backup: %s", path)
    audit = Audit(os.path.join(folder, "deleted.csv"))
    for entry, why in skipped:
        audit.write(entry, SKIPPED, why)
    results: dict[str, int] = {}
    failures, files, left = 0, False, 0
    try:
        for number, (entry, row) in enumerate(keep, 1):
            try:
                result, detail = delete(isc, entry, row)
            except Exception as exc:
                audit.write(entry, FAILED, error_message(exc))
                results[FAILED] = results.get(FAILED, 0) + 1
                log.error("Stopped: %s. The token cannot delete (it needs idn:nelm:manage for non-employee "
                          "records, idn:accounts:manage for delimited file accounts)", error_message(exc))
                break
            audit.write(entry, result, detail)
            results[result] = results.get(result, 0) + 1
            files |= result == DELETED and row.source.kind == DELIMITED
            if result == DELETED and row.identity:
                left += sum(not a.get("disabled") for a in other_accounts(row.identity, row.source))
            failures = failures + 1 if result == FAILED else 0
            if result == FAILED:
                log.warning("Not deleted: %s / %s: %s", entry["source"], entry.get("account"), detail)
            if failures >= MAX_FAILURES:
                log.error("Stopped after %d failures in a row", failures)
                break
            if number % 25 == 0:
                log.info("%d / %d", number, len(keep))
    finally:
        audit.close()
    log.info("%d deleted, %d not found, %d failed, %d skipped; audit: %s", results.get(DELETED, 0),
             results.get(NOT_FOUND, 0), results.get(FAILED, 0), len(skipped), audit.path)
    if left:
        log.warning("%d enabled account(s) on other sources are now uncorrelated, with their access: the "
                    "'Left uncorrelated' sheet of the report lists them, to disable or delete.", left)
    if files:
        log.warning("Delimited file sources: remove the deleted rows from the file before the next "
                    "aggregation, or the accounts and identities come back.")
    log.info("ISC removes the identities after their accounts: run 'iscr externals' again later to check.")
    return 1 if results.get(FAILED) else 0


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    group = common.add_argument_group("common options")
    group.add_argument("--output-dir", default="outputs", help="where audit and backup files go (default: "
                                                               "outputs)")
    group.add_argument("--log-dir", default="logs", help="daily log files (default: logs; '' to disable)")
    group.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    parser = argparse.ArgumentParser(
        prog="iscr-clean",
        description="Changes the tenant: deletes what a plan of isc-reporter lists, after checking it again. "
                    "Without --apply, nothing is changed.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")
    help_text = ("Delete the accounts of externals who left, from the plan of 'iscr externals': the "
                 "non-employee record (ISC deletes the account and the identity) or the account of a "
                 "delimited file source. Each account is checked again first; without --apply, nothing is "
                 "deleted.")
    p = commands.add_parser("externals", help=help_text, description=help_text, parents=[common])
    p.set_defaults(func=_externals)
    p.add_argument("plan", metavar="PLAN", help="the .plan.json written by 'iscr externals'")
    p.add_argument("--env", required=True, metavar="NAME",
                   help="the environment to change, saved with 'iscr env add' (required: never the current "
                        "environment by default); it must be the tenant of the plan")
    p.add_argument("--apply", action="store_true", help="delete; without it, only check and list")
    p.add_argument("--confirm", type=int, metavar="COUNT",
                   help="the number of accounts to delete, instead of typing it (scripts)")
    p.add_argument("--max", type=int, default=100, metavar="COUNT",
                   help="refuse to delete more than COUNT accounts in one run (default 100)")
    p.add_argument("--max-age", type=int, default=7, metavar="DAYS",
                   help="refuse a plan older than DAYS days (default 7)")
    p.add_argument("--output", "-o", help="folder of the audit and backup files (default: in --output-dir, "
                                          "timestamped)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_dir or None)
    warnings.filterwarnings("ignore", message=".*must be one of", module=r"sailpoint\.")
    started = time.monotonic()
    try:
        return args.func(args)
    except UsageError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:
        log.error("Interrupted")
        return 130
    finally:
        log.debug("Finished in %.1fs", time.monotonic() - started)


if __name__ == "__main__":
    sys.exit(main())
