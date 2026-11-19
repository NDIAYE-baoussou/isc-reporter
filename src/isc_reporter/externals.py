"""Externals who left: accounts of a non-employee or delimited file source whose end date is at least
some days past, checked against their identity before anyone deletes them.

Deleting the account of an authoritative source deletes its identity too, and the identity's
accounts on other sources then become uncorrelated, with their access. So an account is only *to
delete* when nothing would be left behind: its identity left too (end date and lifecycle state), has
no enabled account elsewhere, owns nothing and manages no one. Otherwise it is *to review*, with the
reasons.

This module only reads. It writes a report and a plan, the accounts to delete; the separate
``iscr-clean`` command deletes them from a reviewed plan (see :mod:`isc_reporter.clean`).
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from isc_reporter import dashboard
from isc_reporter.csvio import write_table
from isc_reporter.query import quote

log = logging.getLogger("isc_reporter")

NON_EMPLOYEE, DELIMITED, OTHER = "Non-employee", "Delimited file", "Other"
TO_DELETE, TO_REVIEW, RECENT = "To delete", "To review", "Left recently"
STATUSES = (TO_REVIEW, TO_DELETE, RECENT)
STATUS_FILLS = {TO_DELETE: "F8CBAD", TO_REVIEW: "FFE0B2", RECENT: "DDEBF7"}
STATUS_COLOURS = {TO_DELETE: dashboard.RED, TO_REVIEW: dashboard.AMBER, RECENT: dashboard.BLUE}

# Why an account that ended is to review rather than to delete.
OTHER_ENABLED = "Enabled accounts elsewhere"
OWNS = "Owns objects"
MANAGER = "Manages identities"
NOT_LEAVER = "Not in a leaver state"
IDENTITY_DATE = "Identity end date"
NOT_AUTHORITATIVE = "Not authoritative"
NO_IDENTITY = "Identity not found"
UNREADABLE = "End date not readable"
NO_RECORD = "No non-employee record"
UNSUPPORTED = "Source type"
REASONS = (OTHER_ENABLED, OWNS, MANAGER, NOT_LEAVER, IDENTITY_DATE, NOT_AUTHORITATIVE, NO_IDENTITY,
           UNREADABLE, NO_RECORD, UNSUPPORTED)

DEFAULT_ATTRIBUTE = "endDate"
INACTIVE = {"INACTIVE_SHORT_TERM", "INACTIVE_LONG_TERM"}
PLAN_FORMAT = "iscr-externals-plan/1"
ID_CHUNK = 100
OWNED = {"sources": "source(s)", "entitlements": "entitlement(s)", "accessProfiles": "access profile(s)",
         "roles": "role(s)", "apps": "application(s)", "governanceGroups": "governance group(s)"}

COLUMNS = ["Status", "Source", "Account", "Identity", "Display name", "Lifecycle state", "Account end date",
           "Identity end date", "Days since", "Other accounts", "Enabled elsewhere", "Owns", "Manages",
           "Reasons", "Notes", "Action"]
DELETE_COLUMNS = ["Source", "Account", "Identity", "Display name", "Lifecycle state", "Account end date",
                  "Identity end date", "Days since", "Other accounts", "Notes", "Action"]
REVIEW_COLUMNS = ["Source", "Account", "Identity", "Display name", "Lifecycle state", "Account end date",
                  "Identity end date", "Days since", "Enabled elsewhere", "Owns", "Manages", "Reasons",
                  "Notes"]
RECENT_COLUMNS = ["Source", "Account", "Identity", "Display name", "Account end date", "Days since",
                  "Action"]


# -- dates -------------------------------------------------------------------------------

ISO = re.compile(r"^\d{4}-\d{2}-\d{2}")
COMPACT = re.compile(r"^\d{8}$")
NUMERIC = re.compile(r"^(\d{1,2})[/.-](\d{1,2})[/.-](\d{4})$")


def day_order(values) -> str:
    """How the numeric dates among *values* are written: ``dmy`` (dd/MM/yyyy), ``mdy`` (MM/dd/yyyy),
    ``mixed`` (both are seen) or ``""`` (no value tells: every day and month is 12 or less)."""
    day_first = month_first = False
    for value in values:
        match = NUMERIC.match(str(value or "").strip())
        if match:
            day_first |= int(match.group(1)) > 12
            month_first |= int(match.group(2)) > 12
    if day_first and month_first:
        return "mixed"
    return "dmy" if day_first else "mdy" if month_first else ""


def parse_date(value, fmt: str = "", order: str = "") -> tuple[date | None, str]:
    """The date of *value*, and why it cannot be read when it cannot.

    With *fmt* (``strptime`` codes), only that format. Otherwise ISO 8601, yyyyMMdd, and dd/MM/yyyy or
    MM/dd/yyyy as *order* (from :func:`day_order` on all the values of the attribute) says: a date
    that reads both ways, such as 03/04/2026, is never guessed.
    """
    if value is None or value == "":
        return None, ""
    if isinstance(value, datetime):
        return value.date(), ""
    if isinstance(value, date):
        return value, ""
    text = str(value).strip()
    if fmt:
        try:
            return datetime.strptime(text, fmt).date(), ""
        except ValueError:
            return None, f"'{text}' does not match the date format {fmt}"
    if ISO.match(text):
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).date(), ""
        except ValueError:
            try:
                return date.fromisoformat(text[:10]), ""
            except ValueError:
                return None, f"'{text}' is not a date"
    if COMPACT.match(text):
        try:
            return datetime.strptime(text, "%Y%m%d").date(), ""
        except ValueError:
            return None, f"'{text}' is not a date"
    match = NUMERIC.match(text)
    if not match:
        return None, f"'{text}' is not a date in a known format: give --date-format"
    first, second, year = (int(part) for part in match.groups())
    if order == "mixed" and first != second:
        return None, f"'{text}': this attribute mixes dd/MM/yyyy and MM/dd/yyyy dates: give --date-format"
    if first == second or order == "dmy" or (not order and first > 12):
        day, month = first, second
    elif order == "mdy" or (not order and second > 12):
        day, month = second, first
    else:
        return None, f"'{text}' reads as dd/MM/yyyy or as MM/dd/yyyy: give --date-format"
    try:
        return date(year, month, day), ""
    except ValueError:
        return None, f"'{text}' is not a date"


# -- data --------------------------------------------------------------------------------


@dataclass
class SourceInfo:
    id: str
    name: str
    kind: str                                           # NON_EMPLOYEE, DELIMITED or OTHER
    nelm_id: str = ""                                   # its non-employee source id
    profiles: dict[str, str] = field(default_factory=dict)   # profiles it is authoritative for: id -> name


@dataclass
class Settings:
    days: int
    account_attribute: str = ""          # "": the record's end date (non-employee), else endDate
    identity_attribute: str = DEFAULT_ATTRIBUTE   # "": not compared
    date_format: str = ""
    states: list[str] = field(default_factory=list)      # leaver states; default: the inactive ones

    def to_json(self) -> dict:
        return {"days": self.days, "accountAttribute": self.account_attribute,
                "identityAttribute": self.identity_attribute, "dateFormat": self.date_format,
                "states": list(self.states)}

    @classmethod
    def from_json(cls, data: dict) -> Settings:
        return cls(int(data["days"]), data.get("accountAttribute") or "",
                   data.get("identityAttribute") or "", data.get("dateFormat") or "",
                   list(data.get("states") or []))

    def account_label(self, kind: str) -> str:
        if self.account_attribute:
            return self.account_attribute
        return "the end date of the non-employee record" if kind == NON_EMPLOYEE else DEFAULT_ATTRIBUTE


@dataclass
class ExternalData:
    sources: list[SourceInfo]
    accounts: dict[str, list[dict]]                     # source id -> accounts
    records: dict[str, list[dict]]                      # source id -> non-employee records
    identities: dict[str, dict]                         # identity id -> search document
    managed: dict[str, int]                             # identity id -> identities it manages
    lifecycle: dict[str, dict[str, dict]]               # identity profile id -> technical name -> state
    settings: Settings
    today: date = field(default_factory=date.today)
    notes: list[str] = field(default_factory=list)
    origin: str = ""


def source_kind(source: dict, nelm_sources: dict[str, str]) -> str:
    if source.get("id") in nelm_sources:
        return NON_EMPLOYEE
    text = " ".join(str(source.get(key) or "") for key in ("type", "connector", "connectorName"))
    text = re.sub(r"[\s_-]", "", text.lower())
    if "nonemployee" in text:
        return NON_EMPLOYEE
    return DELIMITED if "delimitedfile" in text else OTHER


def account_ends(info: SourceInfo, accounts: list[dict], records: list[dict],
                 settings: Settings) -> list[tuple[dict, dict | None, object, date | None, str]]:
    """``(account, record, raw end date, end date, why it cannot be read)`` for each account."""
    by_name: dict[str, dict] = {}
    for record in records:
        by_name.setdefault(str(record.get("accountName") or "").lower(), record)
    items = []
    for account in accounts:
        record = None
        if info.kind == NON_EMPLOYEE:
            record = (by_name.get(str(account.get("nativeIdentity") or "").lower())
                      or by_name.get(str(account.get("name") or "").lower()))
        attributes = account.get("attributes") or {}
        if settings.account_attribute or info.kind != NON_EMPLOYEE:
            raw = attributes.get(settings.account_attribute or DEFAULT_ATTRIBUTE)
        else:
            raw = (record or {}).get("endDate") or attributes.get(DEFAULT_ATTRIBUTE)
        items.append((account, record, raw))
    order = day_order(raw for _, _, raw in items)
    return [(account, record, raw, *parse_date(raw, settings.date_format, order))
            for account, record, raw in items]


# -- reading -----------------------------------------------------------------------------


def fetch(isc, names: list[str], settings: Settings, today: date | None = None) -> ExternalData:
    """The sources named, their accounts (and non-employee records), and the identities of the
    accounts whose end date is at least ``settings.days`` days past."""
    from isc_reporter.api import error_message, list_all, model_json
    from isc_reporter.leavers import read_lifecycle
    from isc_reporter.search import search_all, search_by_ids
    from isc_reporter.sync_impact import find_source

    today = today or date.today()
    notes: list[str] = []
    nelm: dict[str, str] = {}
    try:
        for found in list_all(isc.non_employee.list_non_employee_sources_v1):
            item = model_json(found)
            nelm[item.get("sourceId", "")] = item.get("id", "")
    except Exception as exc:
        notes.append(f"Non-employee sources not read ({error_message(exc)}): their records cannot be "
                     "matched to the accounts.")
    profiles = [model_json(p) for p in list_all(isc.identity_profiles.list_identity_profiles_v1)]
    sources, accounts, records = [], {}, {}
    for name in dict.fromkeys(names):
        source = model_json(find_source(isc, name))
        info = SourceInfo(source.get("id", ""), source.get("name", name), source_kind(source, nelm),
                          nelm.get(source.get("id", ""), ""),
                          {p.get("id", ""): p.get("name", "") for p in profiles
                           if (p.get("authoritativeSource") or {}).get("id") == source.get("id")})
        sources.append(info)
        accounts[info.id] = [model_json(a) for a in list_all(isc.accounts.list_accounts_v1,
                                                             filters=f"sourceId eq {quote(info.id)}")]
        if info.kind == NON_EMPLOYEE:
            records[info.id] = _records(isc, info, notes)
        log.info("%s (%s): %d account(s)%s", info.name, info.kind, len(accounts[info.id]),
                 f", {len(records[info.id])} non-employee record(s)" if info.id in records else "")
        notes += missing_attribute(info, accounts[info.id], settings)
    data = ExternalData(sources, accounts, records, {}, {}, {}, settings, today, notes, isc.base_url)
    ids = [account.get("identityId") for info in sources for account, _, _, end, _ in
           account_ends(info, accounts[info.id], records.get(info.id, []), settings)
           if account.get("identityId") and end and (today - end).days >= max(settings.days, 1)]
    if ids:
        log.info("Reading the %d identit(ies) of the accounts that ended", len(set(ids)))
        data.identities = {doc.get("id", ""): doc for doc in search_by_ids(isc, "identities", ids)}
    managers = [i for i, doc in data.identities.items() if doc.get("isManager")]
    for start in range(0, len(managers), ID_CHUNK):
        query = "manager.id:(" + " OR ".join(quote(i) for i in managers[start:start + ID_CHUNK]) + ")"
        for doc in search_all(isc, "identities", query, sort_field=None, include_nested=False):
            boss = (doc.get("manager") or {}).get("id", "")
            data.managed[boss] = data.managed.get(boss, 0) + 1
    wanted = {pid: name for info in sources for pid, name in info.profiles.items()}
    data.lifecycle, lifecycle_notes = read_lifecycle(isc, wanted)
    data.notes += lifecycle_notes
    return data


def _records(isc, info: SourceInfo, notes: list[str]) -> list[dict]:
    from isc_reporter.api import error_message, list_all, model_json

    try:
        for source_id in dict.fromkeys(i for i in (info.nelm_id, info.id) if i):
            found = list_all(isc.non_employee.list_non_employee_records_v1,
                             filters=f"sourceId eq {quote(source_id)}")
            if found:
                return [model_json(r) for r in found]
    except Exception as exc:
        notes.append(f"Non-employee records of '{info.name}' not read ({error_message(exc)}): its accounts "
                     "cannot be deleted.")
    return []


def missing_attribute(info: SourceInfo, accounts: list[dict], settings: Settings) -> list[str]:
    if not accounts or (info.kind == NON_EMPLOYEE and not settings.account_attribute):
        return []
    name = settings.account_attribute or DEFAULT_ATTRIBUTE
    if any(name in (account.get("attributes") or {}) for account in accounts):
        return []
    seen = sorted({key for account in accounts[:200] for key in (account.get("attributes") or {})})
    return [f"No account of '{info.name}' has the attribute '{name}': give its end date attribute with "
            f"--account-attribute (attributes: {', '.join(seen[:30]) or 'none'})."]


# -- analysis ----------------------------------------------------------------------------


@dataclass
class Row:
    status: str
    source: SourceInfo
    account: dict
    record: dict | None
    identity: dict | None
    end: date | None
    end_text: str
    days: int | None
    identity_end: str = ""
    reasons: list[tuple[str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    other: int = 0
    enabled_elsewhere: int = 0
    owns: int = 0
    manages: int = 0
    eligible: date | None = None                  # when a recent leaver can be deleted


@dataclass
class Result:
    data: ExternalData
    rows: list[Row]                                # accounts whose end date has passed
    counts: dict[str, Counter]                     # source id -> counts


def analyse(data: ExternalData) -> Result:
    settings, today = data.settings, data.today
    identity_order = day_order((doc.get("attributes") or {}).get(settings.identity_attribute)
                               for doc in data.identities.values()) if settings.identity_attribute else ""
    rows, counts = [], {}
    for info in data.sources:
        count = counts.setdefault(info.id, Counter())
        accounts = data.accounts.get(info.id, [])
        count["Accounts"] = len(accounts)
        for account, record, raw, end, problem in account_ends(info, accounts, data.records.get(info.id, []),
                                                               settings):
            if problem:
                row = Row(TO_REVIEW, info, account, record, None, None, str(raw), None,
                          reasons=[(UNREADABLE, problem)])
            elif end is None:
                count["No end date"] += 1
                continue
            elif (today - end).days < 1:
                count["End date to come"] += 1
                continue
            else:
                days = (today - end).days
                row = Row(TO_DELETE, info, account, record, None, end, end.isoformat(), days)
                if days < settings.days:
                    row.status, row.eligible = RECENT, end + timedelta(days=settings.days)
                    row.identity = data.identities.get(account.get("identityId") or "")
                else:
                    _identity_checks(row, data, identity_order)
                    row.status = TO_REVIEW if row.reasons else TO_DELETE
            count[row.status] += 1
            rows.append(row)
    rows.sort(key=lambda r: (STATUSES.index(r.status), -(r.days or 0), r.source.name.lower(),
                             _account_name(r.account).lower()))
    return Result(data, rows, counts)


def _identity_checks(row: Row, data: ExternalData, identity_order: str) -> None:
    """Fill the reasons (and notes) that make an ended account *to review*."""
    settings, info, account = data.settings, row.source, row.account
    if info.kind == OTHER:
        row.reasons.append((UNSUPPORTED, "only accounts of delimited file and non-employee sources can be "
                                         "deleted by API"))
    elif info.kind == NON_EMPLOYEE and row.record is None:
        row.reasons.append((NO_RECORD, "no non-employee record has this account name"))
    identity_id = account.get("identityId") or ""
    if not identity_id or account.get("uncorrelated"):
        row.notes.append("Uncorrelated account: no identity to delete")
        return
    identity = row.identity = data.identities.get(identity_id)
    if identity is None:
        row.reasons.append((NO_IDENTITY, "its identity is not in the search index: check it in ISC"))
        return
    profile = identity.get("identityProfile") or {}
    if profile.get("id") not in info.profiles:
        row.reasons.append((NOT_AUTHORITATIVE, f"the identity's profile '{profile.get('name', '')}' is not "
                                               "built from this source: deleting the account leaves the "
                                               "identity"))
    attributes = identity.get("attributes") or {}
    if settings.identity_attribute:
        raw = attributes.get(settings.identity_attribute)
        ended, problem = parse_date(raw, settings.date_format, identity_order)
        row.identity_end = ended.isoformat() if ended else str(raw or "")
        if problem:
            row.reasons.append((IDENTITY_DATE, problem))
        elif ended is None:
            row.notes.append(f"No {settings.identity_attribute} on the identity")
        elif (data.today - ended).days < settings.days:
            row.reasons.append((IDENTITY_DATE, f"{ended.isoformat()} on the identity: less than "
                                               f"{settings.days} day(s) ago"))
    state = str(attributes.get("cloudLifecycleState") or "")
    if not state:
        row.notes.append("No lifecycle state: not checked")
    elif settings.states:
        if state not in settings.states:
            row.reasons.append((NOT_LEAVER, f"'{state}' is not one of the leaver states given"))
    elif profile.get("id") in data.lifecycle:
        found = data.lifecycle[profile["id"]].get(state) or {}
        if str(found.get("identityState") or "").upper() not in INACTIVE:
            row.reasons.append((NOT_LEAVER, f"'{state}' is not an inactive lifecycle state"))
    else:
        row.notes.append("Lifecycle states of the profile not read: state not checked")
    others = [a for a in identity.get("accounts") or [] if (a.get("source") or {}).get("id") != info.id]
    enabled = [a for a in others if not a.get("disabled")]
    row.other, row.enabled_elsewhere = len(others), len(enabled)
    if enabled:
        names = ", ".join(dict.fromkeys((a.get("source") or {}).get("name", "") for a in enabled))
        row.reasons.append((OTHER_ENABLED, f"{len(enabled)} on {names}: they would stay, uncorrelated, "
                                           "with their access"))
    elif others:
        names = ", ".join(dict.fromkeys((a.get("source") or {}).get("name", "") for a in others))
        row.notes.append(f"{len(others)} disabled account(s) on {names} will become uncorrelated")
    owned = owned_objects(identity)
    row.owns = sum(owned.values())
    if row.owns:
        row.reasons.append((OWNS, ", ".join(f"{n} {OWNED.get(k, k)}" for k, n in owned.items())
                           + ": reassign them first"))
    row.manages = data.managed.get(identity_id, 0)
    if row.manages:
        row.reasons.append((MANAGER, f"manager of {row.manages} identit(ies): give them another manager "
                                     "first"))


def owned_objects(identity: dict) -> dict[str, int]:
    owns = identity.get("owns") or {}
    found = {key: len(value) for key, value in owns.items() if isinstance(value, list) and value}
    if owns.get("fallbackApp"):
        found["fallback application owner"] = 1
    return found


def _account_name(account: dict) -> str:
    return str(account.get("nativeIdentity") or account.get("name") or "")


def action(row: Row) -> str:
    if row.status == RECENT:
        return f"Kept until {row.eligible.isoformat()}" if row.eligible else "Kept"
    if row.status == TO_REVIEW:
        return "Fix the reasons, then run again"
    if row.source.kind == NON_EMPLOYEE:
        return "Delete the non-employee record: ISC deletes the account" + (
            " and the identity" if row.identity else "")
    return ("Remove the row from the file, and delete the account: otherwise the next aggregation brings it "
            "back")


def display(row: Row) -> dict:
    identity, ref = row.identity or {}, row.account.get("identity") or {}
    attributes = identity.get("attributes") or {}
    return {"Status": row.status, "Source": row.source.name, "Account": _account_name(row.account),
            "Identity": identity.get("name") or ref.get("name") or "",
            "Display name": identity.get("displayName") or "",
            "Lifecycle state": attributes.get("cloudLifecycleState") or row.account.get("cloudLifecycleState")
            or "",
            "Account end date": row.end_text, "Identity end date": row.identity_end, "Days since": row.days,
            "Other accounts": row.other, "Enabled elsewhere": row.enabled_elsewhere, "Owns": row.owns,
            "Manages": row.manages,
            "Reasons": "\n".join(f"{label}: {detail}" for label, detail in row.reasons),
            "Notes": "\n".join(row.notes), "Action": action(row)}


# -- plan ---------------------------------------------------------------------------------


def plan(result: Result, created: datetime | None = None) -> dict:
    """The accounts to delete, with what ``iscr-clean`` checks again before deleting them."""
    data = result.data
    created = created or datetime.now(timezone.utc)
    deletions = []
    for row in result.rows:
        if row.status != TO_DELETE:
            continue
        shown = display(row)
        deletions.append({"source": row.source.name, "sourceId": row.source.id, "kind": row.source.kind,
                          "accountId": row.account.get("id", ""), "account": shown["Account"],
                          "recordId": (row.record or {}).get("id", ""),
                          "identityId": row.account.get("identityId") or "", "identity": shown["Identity"],
                          "endDate": row.end.isoformat() if row.end else ""})
    return {"format": PLAN_FORMAT, "tenant": data.origin, "created": created.isoformat(timespec="seconds"),
            "settings": {**data.settings.to_json(), "sources": [s.name for s in data.sources]},
            "note": "Accounts to delete with 'iscr-clean externals'. Remove the entries to keep; do not "
                    "add any: iscr-clean checks every entry again before deleting it.",
            "deletions": deletions}


def write_plan(path: str, result: Result) -> str:
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(plan(result), fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    return path


# -- report -------------------------------------------------------------------------------


def read_me(settings: Settings) -> list[tuple[str, str]]:
    days = settings.days
    return [
        ("What is this?", f"Accounts of non-employee and delimited file sources whose end date is at least "
                          f"{days} day(s) past, checked against their identity before deleting them. Nothing "
                          "was changed in the tenant."),
        (TO_DELETE, "Nothing prevents deleting the account. On the source the identity profile is built "
                    "from, deleting the account deletes the identity too. These accounts are in the plan "
                    "(.plan.json)."),
        (TO_REVIEW, "Deleting the account now would leave something behind, or cannot be done: see Reasons."),
        (RECENT, f"Ended less than {days} day(s) ago: kept for now. Action gives the first day it can go."),
        (OTHER_ENABLED, "The identity still has enabled accounts on other sources. Once the identity is "
                        "deleted they stay, uncorrelated, with their access: disable or delete them first "
                        "(see the leavers report)."),
        (OWNS, "The identity owns sources, roles, access profiles, entitlements, applications or "
               "governance groups: reassign them first."),
        (MANAGER, "Other identities have this identity as manager."),
        (NOT_LEAVER, "The identity's lifecycle state is not a leaver state (by default, the states "
                     "configured as inactive; --states gives others): the leaver process has not run."),
        (IDENTITY_DATE, "The identity's end date is less than the days given ago, or cannot be read: the "
                        "identity and the source disagree."),
        (NOT_AUTHORITATIVE, "The identity's profile is built from another source: deleting this account "
                            "would not delete the identity."),
        (NO_RECORD, "The account of a non-employee source has no non-employee record with its name: it "
                    "cannot be deleted through the non-employee API."),
        (UNREADABLE, "The end date is not a date, or reads both as dd/MM/yyyy and MM/dd/yyyy: give "
                     "--date-format."),
        (UNSUPPORTED, "ISC deletes accounts by API only on delimited file and non-employee sources."),
        ("Deleting", "Review the To delete sheet and remove from the plan the accounts to keep. Then run "
                     "'iscr-clean externals <plan> --env <name>' to check them again, and add --apply to "
                     "delete them. iscr-clean is a separate command: iscr itself never changes the tenant."),
        ("Delimited file sources", "ISC only deletes the account it holds: remove the row from the file too, "
                                   "or the next aggregation brings the account and its identity back."),
        ("Privacy", "The sheets and the plan list real people and accounts: protect them."),
    ]


def source_rows(result: Result) -> list[dict]:
    rows = []
    for info in result.data.sources:
        count = result.counts.get(info.id, Counter())
        rows.append({"Source": info.name, "Type": info.kind, "Accounts": count["Accounts"],
                     "No end date": count["No end date"], "End date to come": count["End date to come"],
                     RECENT: count[RECENT], TO_REVIEW: count[TO_REVIEW], TO_DELETE: count[TO_DELETE],
                     "Identity profiles": ", ".join(info.profiles.values()) or "none"})
    return rows


def settings_rows(result: Result) -> list[dict]:
    data, settings = result.data, result.data.settings
    rows = [{"Setting": "Sources", "Value": ", ".join(f"{s.name} ({s.kind})" for s in data.sources)},
            {"Setting": "Ended at least (days)", "Value": str(settings.days)},
            {"Setting": "Account end date", "Value": "; ".join(dict.fromkeys(
                settings.account_label(s.kind) for s in data.sources)) or settings.account_label("")},
            {"Setting": "Identity end date", "Value": settings.identity_attribute or "not compared"},
            {"Setting": "Date format", "Value": settings.date_format or "ISO 8601, yyyyMMdd, dd/MM/yyyy or "
                                                                       "MM/dd/yyyy when the values tell"},
            {"Setting": "Leaver states",
             "Value": ", ".join(settings.states) or "the inactive lifecycle states"},
            {"Setting": "Date of the analysis", "Value": data.today.isoformat()}]
    return rows + [{"Setting": "Note", "Value": note} for note in data.notes]


def build_dashboard(result: Result, title: str = "") -> dashboard.Dashboard:
    data = result.data
    shown = [display(row) for row in result.rows]
    by_status = {status: [{c: row[c] for c in columns} for row in shown if row["Status"] == status]
                 for status, columns in ((TO_DELETE, DELETE_COLUMNS), (TO_REVIEW, REVIEW_COLUMNS),
                                         (RECENT, RECENT_COLUMNS))}
    total = Counter()
    for count in result.counts.values():
        total.update(count)
    reasons = Counter(label for row in result.rows if row.status == TO_REVIEW for label, _ in row.reasons)
    kpis = [
        ("Accounts read", str(total["Accounts"]), dashboard.BLUE),
        (f"Ended at least {data.settings.days} day(s) ago", str(total[TO_DELETE] + total[TO_REVIEW]),
         dashboard.BLUE),
        (TO_DELETE, str(total[TO_DELETE]), dashboard.RED if total[TO_DELETE] else dashboard.GREEN),
        (TO_REVIEW, str(total[TO_REVIEW]), dashboard.AMBER if total[TO_REVIEW] else dashboard.GREEN),
        (RECENT, str(total[RECENT]), dashboard.BLUE),
        ("Without end date", str(total["No end date"]), dashboard.AMBER if total["No end date"] else
         dashboard.GREEN),
    ]
    charts = []
    labels = [s for s in STATUSES if total[s]]
    if labels:
        charts.append(dashboard.Chart("Accounts that ended", "doughnut", labels,
                                      [("Accounts", [total[s] for s in labels], "")]))
    labels = [r for r in REASONS if reasons[r]]
    if labels:
        charts.append(dashboard.Chart("Why accounts are to review", "bar", labels,
                                      [("Accounts", [reasons[r] for r in labels], dashboard.AMBER)]))
    wide = {"Reasons", "Notes", "Action", "Value", "Display name", "Identity profiles"}
    tables = [dashboard.Table(TO_DELETE, by_status[TO_DELETE], wide=wide),
              dashboard.Table(TO_REVIEW, by_status[TO_REVIEW], wide=wide,
                              red_positive={"Enabled elsewhere", "Owns", "Manages"}),
              dashboard.Table(RECENT, by_status[RECENT], wide=wide),
              dashboard.Table("By source", source_rows(result), wide=wide),
              dashboard.Table("Settings", settings_rows(result), wide=wide)]
    sections = [dashboard.Section("By source", source_rows(result),
                                  ["Source", "Type", "Accounts", TO_DELETE, TO_REVIEW, RECENT]),
                dashboard.Section(TO_REVIEW, by_status[TO_REVIEW],
                                  ["Source", "Account", "Identity", "Days since", "Reasons"]),
                dashboard.Section(TO_DELETE, by_status[TO_DELETE],
                                  ["Source", "Account", "Identity", "Account end date", "Days since"])]
    return dashboard.Dashboard(title or "Externals who left", dashboard.subtitle(data.origin), kpis, charts,
                               tables, sections, read_me(data.settings), colours=STATUS_COLOURS,
                               wide_pdf={"Reasons", "Account", "Identity"})


def write_xlsx(path: str, result: Result, title: str = "") -> str:
    return dashboard.write_xlsx(build_dashboard(result, title), path)


def write_pdf(path: str, result: Result, title: str = "", max_rows: int = 500) -> str:
    return dashboard.write_pdf(build_dashboard(result, title), path, max_rows)


def write_csv(path: str, result: Result) -> str:
    rows = [[display(row)[c] for c in COLUMNS] for row in result.rows]
    return write_table(path, COLUMNS, rows, ";")
