"""``isc-reporter`` command line."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time
import warnings

from isc_reporter import __version__
from isc_reporter.csvio import output_path
from isc_reporter.errors import UsageError
from isc_reporter.logs import setup_logging

log = logging.getLogger("isc_reporter")
USAGE_CHOICES = ("CREATE", "UPDATE", "ENABLE", "DISABLE")


def _connect(args):
    from isc_reporter.client import connect

    isc = connect(args.config, args.env)
    log.info("Connected to %s%s", isc.base_url, f" ({isc.origin})" if getattr(isc, "origin", "") else "")
    return isc


def _base(args, stem: str) -> str:
    """Output path without extension: --output, or <output-dir>/<stem>_<timestamp>."""
    base = args.output or output_path(args.output_dir, stem, "")
    return os.path.splitext(base)[0] if base.lower().endswith((".xlsx", ".pdf", ".csv")) else base


def _list(value: str | None) -> list[str] | None:
    return [part.strip() for part in value.split(",") if part.strip()] if value else None


# -- search ---------------------------------------------------------------------------


def _search(args) -> int:
    from isc_reporter import report_writers, reporting
    from isc_reporter.search import search_all

    definition = reporting.load_definition(args.definition) if args.definition else {}
    type_name = args.type or definition.get("type")
    if not type_name:
        raise UsageError("give a report type (see 'isc-reporter types') or --definition")
    report_type = reporting.report_type(type_name)
    query = args.query or definition.get("query") or report_type.default_query
    limit = args.limit if args.limit is not None else definition.get("limit")

    isc = _connect(args)
    log.info("Searching %s: %s", report_type.index, query)
    docs = search_all(isc, report_type.index, query, sort_field=report_type.sort_field,
                      max_results=limit + 1 if limit else None,
                      progress=lambda n: n % 2500 == 0 and log.info("%d results so far", n))
    truncated = bool(limit) and len(docs) > limit
    docs = docs[:limit] if limit else docs
    log.info("%d result(s)%s", len(docs), " (limit reached)" if truncated else "")
    report = reporting.build(report_type, docs, query=query, title=args.title or definition.get("title", ""),
                             columns=_list(args.columns) or definition.get("columns"),
                             group_by=_list(args.group_by) or definition.get("group_by"),
                             expand=args.expand or definition.get("expand"), origin=isc.base_url,
                             truncated=truncated)
    base = _base(args, f"search-{report_type.name}")
    for fmt in dict.fromkeys(args.format):
        if fmt == "xlsx":
            log.info("Excel: %s", report_writers.write_xlsx(report, base + ".xlsx"))
        elif fmt == "pdf":
            log.info("PDF: %s", report_writers.write_pdf(report, base + ".pdf", args.pdf_max_rows))
        else:
            for path in report_writers.write_csv(report, base + ".csv"):
                log.info("CSV: %s", path)
    return 0


def _types(args) -> int:
    from isc_reporter.reporting import REPORT_TYPES

    for report_type in REPORT_TYPES.values():
        expands = ", ".join(report_type.expands) or "-"
        print(f"{report_type.name:<20} index {report_type.index:<18} {report_type.description}")
        print(f"{'':<20} --expand: {expands}")
    print("\nAny other search index can be given as the type, with --columns to choose the fields.")
    return 0


# -- certification campaigns ------------------------------------------------------------


def _campaigns(args) -> int:
    from isc_reporter import certification_reports as reports

    if args.since:
        from datetime import date

        try:
            date.fromisoformat(args.since)
        except ValueError as exc:
            raise UsageError(f"--since {args.since!r} must look like YYYY-MM-DD") from exc
    isc = _connect(args)
    data = reports.fetch(isc, campaigns=args.campaign, statuses=args.status, since=args.since,
                         summaries=not args.no_summaries, items=args.items)
    if not data.campaigns:
        raise UsageError("no campaign matches the selection")
    scope = [f"status: {', '.join(args.status)}" if args.status else "all statuses"]
    if args.campaign:
        scope.append(f"campaigns: {', '.join(args.campaign)}")
    if args.since:
        scope.append(f"created since {args.since}")
    data.scope, data.origin = "; ".join(scope), isc.base_url
    title = args.title or "Certification campaigns report"
    base = _base(args, "campaigns")
    for fmt in dict.fromkeys(args.format):
        if fmt == "xlsx":
            log.info("Excel: %s", reports.write_xlsx(data, base + ".xlsx", title))
        else:
            log.info("PDF: %s", reports.write_pdf(data, base + ".pdf", title, args.pdf_max_rows))
    return 0


# -- identity attribute changes ----------------------------------------------------------


def _changes(args) -> int:
    from isc_reporter import attribute_history as history

    start, end = history.period(args.days, args.since)
    isc = _connect(args)
    data = history.fetch(isc, start, end, _list(args.attributes), args.query or "", args.max_identities,
                         _list(args.exclude))
    data.origin = isc.base_url
    title = args.title or "Identity attribute changes"
    base = _base(args, "identity-changes")
    for fmt in dict.fromkeys(args.format):
        if fmt == "xlsx":
            log.info("Excel: %s", history.write_xlsx(data, base + ".xlsx", title))
        elif fmt == "pdf":
            log.info("PDF: %s", history.write_pdf(data, base + ".pdf", title, args.pdf_max_rows))
        else:
            for path in history.write_csv(data, base):
                log.info("CSV: %s", path)
    return 0


# -- sources --------------------------------------------------------------------------------


def _sources(args) -> int:
    from isc_reporter import source_status

    isc = _connect(args)
    data = source_status.fetch(isc, args.source, args.quick, args.stale_days)
    for pattern in data.unmatched:
        log.warning("--source %s matches no source", pattern)
    if not data.sources:
        raise UsageError("no source to report on" + (" matches --source" if args.source else ""))
    for note in data.notes:
        log.warning("%s", note)
    result = source_status.analyse(data)
    base = _base(args, "sources")
    for fmt in dict.fromkeys(args.format):
        path = base + "." + fmt
        if fmt == "xlsx":
            source_status.write_xlsx(path, result, args.title or "")
        elif fmt == "pdf":
            source_status.write_pdf(path, result, args.title or "", args.pdf_max_rows)
        else:
            source_status.write_csv(path, result)
        log.info("%s: %s", fmt.upper(), path)
    health = {state: sum(row["Health"] == state for row in result.sources)
              for state in (source_status.HEALTHY, source_status.ERROR)}
    log.info("%d source(s): %d healthy, %d in error, %d provisioned by their connector; %d warning(s)",
             len(result.sources), health[source_status.HEALTHY], health[source_status.ERROR],
             sum(row["Provisioning"] == source_status.CONNECTOR for row in result.sources), result.warnings)
    return 0


# -- identity profiles ---------------------------------------------------------------------


def _mappings(args) -> int:
    from isc_reporter import identity_profiles, lifecycle, mappings_html

    if args.command != "mappings":
        log.warning("The '%s' command is now called 'mappings'; the old name will be removed in a future "
                    "version.", args.command)
    if args.source:
        profiles, transforms = identity_profiles.load_files(args.source)
        policies = identity_profiles.load_policies(args.source)
        names = identity_profiles.load_names(args.source)
        origin =", ".join(os.path.basename(os.path.normpath(path)) for path in args.source)
        log.info("Read %d profile(s) and %d transform(s) from files", len(profiles), len(transforms))
    else:
        isc = _connect(args)
        profiles, transforms, names = identity_profiles.fetch(isc)
        policies = identity_profiles.fetch_policies(isc)
        origin = isc.base_url
        log.info("Read %d profile(s), %d transform(s) and %d provisioning policy(ies) from the tenant",
                 len(profiles), len(transforms), len(policies))
    if args.profile:
        wanted = {name.lower() for name in args.profile}
        profiles = [p for p in profiles if p.get("name", "").lower() in wanted]
        missing = wanted - {p.get("name", "").lower() for p in profiles}
        if missing:
            raise UsageError(f"identity profile(s) not found: {', '.join(sorted(missing))}")
    if not profiles:
        raise UsageError("no identity profile to document")
    states = lifecycle.from_files(profiles, args.source) if args.source else lifecycle.fetch(isc, profiles)
    for note in states.notes:
        log.warning("%s", note)
    log.info("%d lifecycle state(s) read", sum(len(found) for found in states.states.values()))
    base = _base(args, "mappings")
    writers = {"xlsx": ("Excel", identity_profiles.write_xlsx), "pdf": ("PDF", identity_profiles.write_pdf),
               "html": ("Diagrams", mappings_html.write_html)}
    for fmt in dict.fromkeys(args.format):
        label, writer = writers[fmt]
        writer(base + "." + fmt, profiles, transforms, names, origin, policies, states=states)
        log.info("%s: %s (%d profile(s))", label, base + "." + fmt,
                 len(profiles))
    return 0


def _transforms(args) -> int:
    from isc_reporter import transform_analysis as analysis

    if args.source:
        profiles, transforms, policies = analysis.load_files(args.source)
        origin = ", ".join(os.path.basename(os.path.normpath(path)) for path in args.source)
    else:
        isc = _connect(args)
        profiles, transforms, policies = analysis.fetch(isc)
        origin = isc.base_url
    log.info("Read %d named transform(s), %d identity profile(s) and %d provisioning polic(ies)",
             len(transforms), len(profiles), len(policies))
    result = analysis.analyse(profiles, transforms, policies, origin)
    folder = args.output or output_path(args.output_dir, "transform-analysis", "")
    analysis.write_outputs(result, folder, args.format, drafts=not args.no_drafts, title=args.title or "")
    log.info("%d finding(s), %d warning(s); %d change(s) in the optimized drafts", len(result.findings),
             result.warnings, len(result.changes))
    log.info("Custom named transforms: %d now, %d after the optimization plan", *result.custom)
    log.info("Analysis%s: %s", "" if args.no_drafts else " and optimized drafts", folder)
    return 0


def _source_with_schema(isc, name: str):
    from isc_reporter.api import call, model_json
    from isc_reporter.query import quote
    from isc_reporter.source_migration import Source

    found = call(isc.sources.list_sources_v1, filters=f"name eq {quote(name)}", limit=2) or []
    if len(found) != 1:
        log.warning("Source %r %s in the tenant: its attributes cannot be checked", name,
                    "not found" if not found else "is ambiguous")
        return Source(name)
    source = found[0]
    schemas = call(isc.sources.get_source_schemas_v1, source_id=source.id, include_types="account") or []
    attributes = [a["name"] for schema in schemas for a in model_json(schema).get("attributes", [])]
    return Source(source.name, source.id, attributes)


def _source_from_files(paths: list[str], name: str):
    """The source *name* of exported files, with its account attributes when the export has its schemas
    (a snapshot does)."""
    from isc_reporter.identity_profiles import iter_objects
    from isc_reporter.source_migration import Source

    for obj in iter_objects(paths):
        if obj.get("name") == name and ("connector" in obj or "connectorAttributes" in obj):
            schemas = [schema for schema in obj.get("schemas") or []
                       if isinstance(schema, dict) and schema.get("name") == "account"]
            if not schemas:
                break
            return Source(name, obj.get("id", ""), [a["name"] for schema in schemas
                                                    for a in schema.get("attributes") or [] if a.get("name")])
    return Source(name)


def _migration(args) -> int:
    from isc_reporter import criteria, identity_profiles, source_migration

    if args.source:
        profiles, transforms = identity_profiles.load_files(args.source)
        old, new = (_source_from_files(args.source, name) for name in (args.old_source, args.new_source))
        objects = criteria.load_files(args.source)
    else:
        isc = _connect(args)
        profiles, transforms, _ = identity_profiles.fetch(isc)
        old = _source_with_schema(isc, args.old_source)
        new = _source_with_schema(isc, args.new_source)
        objects = criteria.fetch(isc)
    for note in objects.notes:
        log.warning("%s", note)
    plan = source_migration.analyse(profiles, transforms, old, new,
                                    source_migration.read_attribute_map(args.attribute_map),
                                    in_place=args.in_place, suffix=args.suffix, objects=objects)
    folder = args.output or output_path(args.output_dir, "source-migration", "")
    workbook = source_migration.write_outputs(plan, folder, pdf=not args.no_pdf)
    to_define = sum(info["status"].startswith("TO DEFINE") for info in plan.attributes.values())
    log.info("%d identity profile(s), %d transform(s) and %d source attribute(s) impacted; "
             "%d attribute(s) still to define", len(plan.profiles), len(plan.transforms),
             len(plan.attributes), to_define)
    log.info("%d criteria to review (roles, dimensions, segments, correlation)", len(plan.criteria))
    log.info("Analysis: %s", workbook)
    log.info("Drafts and attribute-mapping.csv: %s", folder)
    return 0


def _sync(args) -> int:
    from isc_reporter import sync_impact

    explicit = sync_impact.merge(sync_impact.parse_maps(args.map), sync_impact.read_map_file(args.map_file))
    patterns = [p for value in args.exclude or [] for p in _list(value) or []]
    patterns += sync_impact.read_exclude_file(args.exclude_file)
    isc = _connect(args)
    source = sync_impact.find_source(isc, args.source)
    ignored: list[dict] = []
    mappings = explicit
    if not args.only_map:
        policies = sync_impact.fetch_policies(isc, source.id)
        found, ignored = sync_impact.policy_mappings(policies, args.usage)
        log.info("%s: %d provisioning polic(ies), %d attribute(s) mapped directly to an identity attribute",
                 source.name, len(policies), len(found))
        mappings = sync_impact.merge(found, explicit)
    compared = len(mappings)
    mappings, ignored, excluded, unmatched = sync_impact.exclude(patterns, mappings, ignored)
    if excluded:
        log.info("Excluded, as never synchronised: %s",
                 ", ".join(dict.fromkeys(row["Account attribute"] for row in excluded)))
    for pattern in unmatched:
        log.warning("--exclude %s matches no attribute of the policy or of --map", pattern)
    if compared and not mappings:
        raise UsageError("every attribute to compare is excluded: nothing left to forecast")
    if not mappings:
        if args.only_map:
            raise UsageError("--only-map needs at least one --map or a --map-file")
        raise UsageError(sync_impact.nothing_to_compare(source.name, policies, args.usage, ignored,
                                                        bool(getattr(source, "authoritative", False))))
    accounts = sync_impact.fetch_accounts(isc, source.id)
    log.info("%d account(s) read from %s", len(accounts), source.name)
    identities = sync_impact.fetch_identities(isc, [a["identityId"] for a in accounts if a.get("identityId")])
    result = sync_impact.analyse(source.name, accounts, identities, mappings, ignored, args.threshold,
                                 excluded)
    base = _base(args, f"sync-impact_{source.name}".replace(" ", "-").replace("/", "-"))
    for fmt in dict.fromkeys(args.format):
        path = base + "." + fmt
        if fmt == "xlsx":
            sync_impact.write_xlsx(path, result, isc.base_url, args.title or "", args.max_details)
        elif fmt == "pdf":
            sync_impact.write_pdf(path, result, isc.base_url, args.title or "", args.max_details,
                                  args.pdf_max_rows)
        else:
            sync_impact.write_csv(path, result)
        log.info("%s: %s", fmt.upper(), path)
    log.info("%s: %d of %d account(s) would be updated (%.1f%%), %d attribute update(s)", result.verdict,
             result.impacted, result.analysed, result.share, sum(s.updates for s in result.stats.values()))
    return 0


def _leavers(args) -> int:
    from isc_reporter import leavers

    if args.grace and not args.date_attribute:
        raise UsageError("--grace needs --date-attribute (the identity attribute holding the departure date)")
    isc = _connect(args)
    data = leavers.fetch(isc, args.states, args.exclude_source, args.include_authoritative,
                         args.date_attribute or "", args.grace)
    for note in data.notes:
        log.warning("%s", note)
    result = leavers.analyse(data)
    base = _base(args, "leavers")
    for fmt in dict.fromkeys(args.format):
        path = base + "." + fmt
        if fmt == "xlsx":
            leavers.write_xlsx(path, result, args.title or "")
        elif fmt == "pdf":
            leavers.write_pdf(path, result, args.title or "", args.pdf_max_rows)
        else:
            leavers.write_csv(path, result)
        log.info("%s: %s", fmt.upper(), path)
    log.info("%d of %d leaver(s) still have %d enabled account(s)", len(result.leavers), result.total,
             len(result.accounts))
    return 0


def _role_impact(args) -> int:
    from isc_reporter import role_impact

    if not args.role and not args.source:
        raise UsageError("give the role name, or a role definition with --from")
    role = role_impact.load_role(args.source, args.role) if args.source else None
    isc = _connect(args)
    role = role or role_impact.find_role(isc, args.role)
    data = role_impact.fetch(isc, role, args.query)
    for note in data.notes:
        log.warning("%s", note)
    result = role_impact.analyse(data)
    for operation in result.unsupported:
        log.warning("Criteria operation %s is not evaluated: it never matches in this report", operation)
    base = _base(args, "role-impact_" + re.sub(r"[^\w.-]+", "-", role.get("name", "")).strip("-"))
    for fmt in dict.fromkeys(args.format):
        path = base + "." + fmt
        if fmt == "xlsx":
            role_impact.write_xlsx(path, result, args.title or "")
        elif fmt == "pdf":
            role_impact.write_pdf(path, result, args.title or "", args.pdf_max_rows)
        else:
            role_impact.write_csv(path, result)
        log.info("%s: %s", fmt.upper(), path)
    log.info("%d identit(ies) match the criteria; enabling the role would assign it to %d, provision %d "
             "entitlement(s) and create %d account(s); %d would lose it", result.matching, result.new_members,
             len(result.provisioning), result.new_accounts, result.outcomes[role_impact.LOSES])
    return 0


def _snapshot(args) -> int:
    from isc_reporter import snapshot

    isc = _connect(args)
    found = snapshot.fetch(isc, list(dict.fromkeys(args.only or snapshot.KINDS)))
    for note in found.notes:
        log.warning("%s", note)
    if not found.objects:
        raise UsageError("nothing could be read: no snapshot written")
    folder = args.output or os.path.join(args.output_dir, "snapshot-" + snapshot.tenant_name(isc.base_url))
    result = snapshot.write(found, folder)
    for kind, count in result.counts.items():
        changed = result.changed(kind)
        detail = ", ".join(f"{changed[c]} {c}" for c in (snapshot.ADDED, snapshot.CHANGED, snapshot.REMOVED)
                           if changed[c]) if result.since else ""
        log.info("  %-20s %5d%s", snapshot.KINDS[kind].label, count, f"  ({detail})" if detail else "")
    if result.since:
        log.info("%d change(s) since the snapshot of %s", len(result.changes), result.since)
    if result.redacted:
        log.info("%d secret value(s) replaced by %s", result.redacted, snapshot.REDACTED)
    log.info("Snapshot: %s", folder)
    return 0


def _env(args) -> int:
    from isc_reporter import client, environments

    if args.action == "list":
        found = environments.listing()
        if not found:
            print("No environment yet. Add one: "
                  "iscr env add <name> --url https://<tenant>.api.identitynow.com --client-id <client id>")
        for env in found:
            missing = "" if environments.has_secret(env.name) else "   (secret missing on this computer)"
            print(f"{'*' if env.current else ' '} {env.name:<20} {env.base_url:<50} {env.client_id}{missing}")
        return 0
    if args.action == "use":
        env = environments.use(args.name)
        log.info("Current environment: %s (%s)", env.name, env.base_url)
        return 0
    if args.action == "remove":
        environments.remove(args.name)
        log.info("Environment '%s' removed, with its secret in %s", args.name, environments.store_name())
        return 0
    if args.action == "test":
        name = args.name or environments.current()
        if not name:
            raise UsageError("no current environment: give its name, or add one with 'iscr env add'")
        isc = client.open_session(*environments.credentials(name), origin=f"environment '{name}'")
        log.info("Environment '%s': connected to %s", name, isc.base_url)
        return 0
    if args.action == "import":
        base_url, client_id, secret = client.read_config_file(args.file)
    else:
        environments.check_name(args.name)
        base_url, client_id = args.url, args.client_id
        if args.secret_stdin:
            secret = sys.stdin.readline().strip()
        else:
            import getpass

            secret = getpass.getpass(f"Client secret for '{args.name}' (not shown): ").strip()
    if not getattr(args, "no_check", False):
        client.open_session(base_url, client_id, secret)
        log.info("Credentials checked: an access token was obtained from %s", base_url.rstrip("/"))
    is_current = environments.add(args.name, base_url, client_id, secret, make_current=args.use)
    log.info("Environment '%s' saved: %s, client ID %s; the secret is in %s, not in a file", args.name,
             base_url.rstrip("/"), client_id, environments.store_name())
    if is_current:
        log.info("'%s' is the current environment", args.name)
    if args.action == "import":
        if args.delete:
            os.remove(args.file)
            log.info("Deleted %s", args.file)
        else:
            log.warning("Delete %s now: it still holds the client secret in clear text.", args.file)
    return 0


def _demo(args) -> int:
    from isc_reporter import demo

    folder = args.output or os.path.join(args.output_dir, "demo")
    for path in demo.run(folder):
        log.info("Written: %s", path)
    log.info("Sample reports from fictional data are in %s", folder)
    return 0


# -- parser ------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    group = common.add_argument_group("common options")
    group.add_argument("--env", metavar="NAME",
                       help="tenant environment saved with 'iscr env add' (default: ISCR_ENV, the SAIL_* "
                            "environment variables, then the current environment)")
    group.add_argument("--config", metavar="JSON", help="credentials in a JSON file: not recommended, the "
                                                        "secret is in clear text (see 'iscr env import')")
    group.add_argument("--output-dir", default="outputs", help="where reports go (default: outputs)")
    group.add_argument("--log-dir", default="logs", help="daily log files (default: logs; '' to disable)")
    group.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    parser = argparse.ArgumentParser(
        prog="isc-reporter",
        description="Reports and change-impact analysis for SailPoint Identity Security Cloud: search, "
                    "certification campaigns, identity attribute history, source status and capabilities, "
                    "identity attribute mappings and "
                    "transform dependencies, transform analysis with optimized drafts, leavers who still "
                    "have access, source migrations, attribute sync forecasts, role enablement impact and "
                    "configuration snapshots. Read-only.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    def command(name, func, help_text, aliases=()):
        p = commands.add_parser(name, help=help_text, description=help_text, parents=[common],
                                aliases=list(aliases))
        p.set_defaults(func=func)
        return p

    def formats(p, choices=("xlsx", "pdf", "csv"), default=("xlsx", "pdf")):
        p.add_argument("--format", nargs="+", choices=list(choices), default=list(default),
                       help=f"output formats (default: {' '.join(default)})")

    def output(p, what="output path without extension"):
        p.add_argument("--output", "-o", help=f"{what} (default: in --output-dir, timestamped)")

    p = command("search", _search, "Report on a search index: one line per object, nested access "
                                   "consolidated, summary charts.")
    p.add_argument("type", nargs="?", help="identities, access-profiles, roles, entitlements, events, "
                                           "account-activities, workflows, or a search index")
    p.add_argument("--query", "-q", help="search query (default: '*')")
    p.add_argument("--columns", help="comma-separated fields instead of the default columns")
    p.add_argument("--group-by", help="comma-separated fields to count and chart")
    p.add_argument("--expand", help="also one line per nested item (see 'types')")
    p.add_argument("--definition", metavar="JSON", help="saved report definition (options override it)")
    p.add_argument("--limit", type=int, help="stop after this many results")
    p.add_argument("--title", help="report title")
    p.add_argument("--pdf-max-rows", type=int, default=2000, help="rows in the PDF table (default 2000)")
    formats(p)
    output(p)

    command("types", _types, "List the search report types.")

    p = command("campaigns", _campaigns, "Certification campaign dashboard: progress, decisions, "
                                         "reviewers, overdue certifications.")
    p.add_argument("--campaign", action="append", metavar="NAME_OR_ID",
                   help="only this campaign (repeatable)")
    p.add_argument("--status", nargs="+", type=str.upper, metavar="STATUS",
                   help="only campaigns in these statuses, e.g. ACTIVE COMPLETED")
    p.add_argument("--since", metavar="YYYY-MM-DD", help="only campaigns created since this date")
    p.add_argument("--items", action="store_true", help="also every reviewed access item (slower)")
    p.add_argument("--no-summaries", action="store_true", help="skip approved/revoked counts (faster)")
    p.add_argument("--title", help="report title")
    p.add_argument("--pdf-max-rows", type=int, default=500, help="rows per PDF table (default 500)")
    formats(p, ("xlsx", "pdf"))
    output(p)

    p = command("changes", _changes, "Identity attribute changes with old and new values over a period.")
    p.add_argument("--attributes", help="comma-separated attributes, e.g. department,manager (default: all)")
    p.add_argument("--exclude", help="comma-separated attributes to leave out")
    when = p.add_mutually_exclusive_group()
    when.add_argument("--days", type=int, default=30, help="the last N days (default 30)")
    when.add_argument("--since", metavar="YYYY-MM-DD", help="from this date to now")
    p.add_argument("--query", "-q", help="only identities matching this search query")
    p.add_argument("--max-identities", type=int, help="stop after reading this many identities")
    p.add_argument("--title", help="report title")
    p.add_argument("--pdf-max-rows", type=int, default=500, help="rows per PDF table (default 500)")
    formats(p)
    output(p)

    p = command("sources", _sources, "Status and capabilities of the sources: health, cluster, how ISC "
                                     "provisions them (connector, service desk or manual), features, "
                                     "provisioning policies, correlation, attribute sync, aggregation "
                                     "schedule and last aggregation, accounts, with checks.")
    p.add_argument("--source", action="append", metavar="NAME",
                   help="only the sources with this name, * and ? as wildcards (repeatable)")
    p.add_argument("--quick", action="store_true",
                   help="read the source list only (one call): no policies, schedules, correlation, "
                        "attribute sync, last aggregation or account counts")
    p.add_argument("--stale-days", type=int, default=7, metavar="DAYS",
                   help="flag the sources not aggregated for more than DAYS days (default 7)")
    p.add_argument("--title", help="report title")
    p.add_argument("--pdf-max-rows", type=int, default=500, help="rows per PDF table (default 500)")
    formats(p)
    output(p)

    p = command("mappings", _mappings, "Identity attribute mappings of the identity profiles: how every "
                                       "identity attribute is built, as a tree of its transforms, where it "
                                       "is used, with checks.", aliases=["identity-profiles", "profiles"])
    p.add_argument("--from", dest="source", nargs="+", metavar="PATH",
                   help="exported JSON files or folders instead of the tenant")
    p.add_argument("--profile", action="append", metavar="NAME", help="only this profile (repeatable)")
    formats(p, ("xlsx", "pdf", "html"), default=("xlsx", "pdf", "html"))
    output(p)

    p = command("transforms", _transforms, "Transform analysis: what is broken, risky, repeated or more "
                                           "complex than needed, with optimized drafts that give the same "
                                           "result and an optimization plan for the whole tenant. Changes "
                                           "nothing.")
    p.add_argument("--from", dest="source", nargs="+", metavar="PATH",
                   help="exported JSON files or folders instead of the tenant")
    p.add_argument("--no-drafts", action="store_true", help="the report only, without the optimized drafts")
    p.add_argument("--title", help="report title")
    formats(p, ("xlsx", "pdf"))
    output(p, "output folder")

    p = command("migration", _migration, "Impact of replacing a source by another on identity profiles, "
                                         "mappings and transforms, with draft JSON. Changes nothing.")
    p.add_argument("--old-source", required=True, metavar="NAME")
    p.add_argument("--new-source", required=True, metavar="NAME")
    p.add_argument("--attribute-map", metavar="CSV", help="old_attribute;new_attribute (the "
                                                          "attribute-mapping.csv of a previous run)")
    p.add_argument("--from", dest="source", nargs="+", metavar="PATH",
                   help="exported JSON files or folders instead of the tenant")
    p.add_argument("--in-place", action="store_true", help="update impacted transforms instead of copying")
    p.add_argument("--suffix", help="name suffix of copied transforms and new profiles")
    p.add_argument("--no-pdf", action="store_true", help="Excel only")
    output(p, "output folder")

    p = command("analyze-before-sync", _sync, "Forecast the bulk updates ISC could send to the target if "
                                              "attribute synchronization is enabled on a source. "
                                              "Changes nothing.", aliases=["analyzebeforesync"])
    p.add_argument("source", metavar="SOURCE", help="source name")
    p.add_argument("--map", action="append", metavar="IDENTITY=ACCOUNT",
                   help="also compare this identity attribute with this account attribute (repeatable)")
    p.add_argument("--map-file", metavar="CSV", help="identity_attribute;account_attribute lines to compare "
                                                      "in addition")
    p.add_argument("--only-map", action="store_true", help="compare only --map/--map-file, ignore the "
                                                           "provisioning policy")
    p.add_argument("--exclude", action="append", metavar="ATTRIBUTE",
                   help="leave out attributes that will never be synchronised: account or identity "
                        "attribute names, comma-separated, * and ? as wildcards, or IDENTITY=ACCOUNT pairs "
                        "(repeatable)")
    p.add_argument("--exclude-file", metavar="FILE", help="attributes to leave out, one per line, as for "
                                                          "--exclude (# starts a comment)")
    p.add_argument("--usage", nargs="+", type=str.upper, choices=list(USAGE_CHOICES), default=["CREATE"],
                   help="provisioning policy usage type(s) to read (default: CREATE)")
    p.add_argument("--threshold", type=float, default=10.0, metavar="PERCENT",
                   help="share of accounts to update that is flagged as a bulk update (default 10)")
    p.add_argument("--max-details", type=int, default=100_000, help="rows in the Accounts and Changes "
                                                                    "sheets (the CSV has them all)")
    p.add_argument("--title", help="report title")
    p.add_argument("--pdf-max-rows", type=int, default=500, help="rows per PDF table (default 500)")
    formats(p)
    output(p)

    p = command("leavers", _leavers, "Leavers who still have access: identities in a leaver lifecycle "
                                     "state with enabled accounts, and whether the state should have "
                                     "disabled them.")
    p.add_argument("--states", nargs="+", metavar="STATE",
                   help="leaver lifecycle states (technical names); default: the states configured as "
                        "inactive")
    p.add_argument("--exclude-source", action="append", metavar="NAME",
                   help="ignore this source (repeatable)")
    p.add_argument("--include-authoritative", action="store_true",
                   help="also list accounts of authoritative sources (left out by default)")
    p.add_argument("--date-attribute", metavar="ATTRIBUTE",
                   help="identity attribute holding the departure date (e.g. endDate), for the days "
                        "since leaving")
    p.add_argument("--grace", type=int, default=0, metavar="DAYS",
                   help="days allowed to revoke access after the departure date (with --date-attribute)")
    p.add_argument("--title", help="report title")
    p.add_argument("--pdf-max-rows", type=int, default=500, help="rows per PDF table (default 500)")
    formats(p)
    output(p)

    p = command("role-impact", _role_impact, "Impact of enabling a role: the identities its membership "
                                             "criteria select, the entitlements to provision and the "
                                             "accounts to create, who would lose it. Changes nothing.")
    p.add_argument("role", nargs="?", metavar="ROLE", help="role name")
    p.add_argument("--from", dest="source", action="append", metavar="PATH",
                   help="role JSON file or folder (an export, or a draft with new criteria) instead of the "
                        "tenant's definition, repeatable; identities are still read from the tenant")
    p.add_argument("--query", "-q", default="*", help="only identities matching this search query "
                                                      "(default: all)")
    p.add_argument("--title", help="report title")
    p.add_argument("--pdf-max-rows", type=int, default=500, help="rows per PDF table (default 500)")
    formats(p)
    output(p)

    from isc_reporter.snapshot import KINDS

    p = command("snapshot", _snapshot, "Configuration snapshot: every configuration object of the tenant as "
                                       "a JSON file, in a folder to keep under version control (git). "
                                       "Secrets are left out. Changes nothing.")
    p.add_argument("--only", nargs="+", choices=list(KINDS), metavar="TYPE",
                   help=f"only these types (default: all): {', '.join(KINDS)}")
    p.add_argument("--output", "-o", help="snapshot folder: a new or empty folder, or an earlier snapshot of "
                                          "the same tenant to bring up to date (default: in --output-dir, "
                                          "snapshot-<tenant>)")

    p = command("demo", _demo, "Write every report from fictional data, to see the output without a tenant.")
    output(p, "output folder")

    env_help = ("Tenant environments: create one per tenant and switch between them. The client secret is "
                "kept in the credential store of the computer (Windows Credential Manager, macOS "
                "Keychain...), never in a file.")
    p = commands.add_parser("env", help=env_help, description=env_help)
    p.set_defaults(func=_env)
    actions = p.add_subparsers(dest="action", required=True, metavar="ACTION")

    def action(name, help_text):
        return actions.add_parser(name, help=help_text, description=help_text, parents=[common])

    a = action("add", "Create or update an environment. The client secret is asked for and not shown.")
    a.add_argument("name", help="a short name, e.g. dev, sandbox, prod")
    a.add_argument("--url", required=True, help="tenant API base URL, e.g. https://acme.api.identitynow.com")
    a.add_argument("--client-id", required=True, help="client ID of the personal access token")
    a.add_argument("--secret-stdin", action="store_true",
                   help="read the client secret from standard input (scripts), instead of asking for it")
    a.add_argument("--use", action="store_true", help="also make it the current environment")
    a.add_argument("--no-check", action="store_true", help="save without checking that the tenant answers")
    action("list", "List the environments; * marks the current one.")
    a = action("use", "Make an environment the current one: commands run without --env use it.")
    a.add_argument("name")
    a = action("test", "Check that an environment connects (default: the current one).")
    a.add_argument("name", nargs="?")
    a = action("remove", "Remove an environment and its secret.")
    a.add_argument("name")
    a = action("import", "Move the credentials of a JSON file (config.json) into an environment.")
    a.add_argument("file", help="the JSON file, e.g. config.json")
    a.add_argument("--name", required=True, help="the environment to create")
    a.add_argument("--use", action="store_true", help="also make it the current environment")
    a.add_argument("--delete", action="store_true", help="delete the file once its credentials are saved")
    a.add_argument("--no-check", action="store_true", help="save without checking that the tenant answers")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_dir or None)
    # The SDK warns about every value of the tenant it does not know yet (new source features...).
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
