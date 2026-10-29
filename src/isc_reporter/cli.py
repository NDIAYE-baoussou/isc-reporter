"""``isc-reporter`` command line."""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
import time

from isc_reporter import __version__
from isc_reporter.csvio import output_path
from isc_reporter.errors import UsageError
from isc_reporter.logs import setup_logging

log = logging.getLogger("isc_reporter")
USAGE_CHOICES = ("CREATE", "UPDATE", "ENABLE", "DISABLE")


def _connect(args):
    from isc_reporter.client import connect

    isc = connect(args.config)
    log.info("Connected to %s", isc.base_url)
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


# -- identity profiles ---------------------------------------------------------------------


def _mappings(args) -> int:
    from isc_reporter import identity_profiles, mappings_html

    if args.command != "mappings":
        log.warning("The '%s' command is now called 'mappings'; the old name will be removed in a future "
                    "version.", args.command)
    if args.source:
        profiles, transforms = identity_profiles.load_files(args.source)
        policies = identity_profiles.load_policies(args.source)
        names: dict[str, str] = {}
        origin = ", ".join(os.path.basename(os.path.normpath(path)) for path in args.source)
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
    base = _base(args, "mappings")
    writers = {"xlsx": ("Excel", identity_profiles.write_xlsx), "pdf": ("PDF", identity_profiles.write_pdf),
               "html": ("Diagrams", mappings_html.write_html)}
    for fmt in dict.fromkeys(args.format):
        label, writer = writers[fmt]
        writer(base + "." + fmt, profiles, transforms, names, origin, policies)
        log.info("%s: %s (%d profile(s))", label, base + "." + fmt,
                 len(profiles))
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


def _migration(args) -> int:
    from isc_reporter import criteria, identity_profiles, source_migration
    from isc_reporter.source_migration import Source

    if args.source:
        profiles, transforms = identity_profiles.load_files(args.source)
        old, new = Source(args.old_source), Source(args.new_source)
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
    if not mappings:
        if args.only_map:
            raise UsageError("--only-map needs at least one --map or a --map-file")
        raise UsageError(sync_impact.nothing_to_compare(source.name, policies, args.usage, ignored,
                                                        bool(getattr(source, "authoritative", False))))
    accounts = sync_impact.fetch_accounts(isc, source.id)
    log.info("%d account(s) read from %s", len(accounts), source.name)
    identities = sync_impact.fetch_identities(isc, [a["identityId"] for a in accounts if a.get("identityId")])
    result = sync_impact.analyse(source.name, accounts, identities, mappings, ignored, args.threshold)
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
    group.add_argument("--config", help="credentials JSON (default: SAIL_* environment variables, "
                                        "then ./config.json)")
    group.add_argument("--output-dir", default="outputs", help="where reports go (default: outputs)")
    group.add_argument("--log-dir", default="logs", help="daily log files (default: logs; '' to disable)")
    group.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    parser = argparse.ArgumentParser(
        prog="isc-reporter",
        description="Reports and change-impact analysis for SailPoint Identity Security Cloud: search, "
                    "certification campaigns, identity attribute history, identity attribute mappings and "
                    "transform dependencies, leavers who still have access, source migrations, attribute "
                    "sync forecasts and "
                    "role enablement impact. "
                    "Read-only.",
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

    p = command("mappings", _mappings, "Identity attribute mappings of the identity profiles: how every "
                                       "identity attribute is built, as a tree of its transforms, where it "
                                       "is used, with checks.", aliases=["identity-profiles", "profiles"])
    p.add_argument("--from", dest="source", nargs="+", metavar="PATH",
                   help="exported JSON files or folders instead of the tenant")
    p.add_argument("--profile", action="append", metavar="NAME", help="only this profile (repeatable)")
    formats(p, ("xlsx", "pdf", "html"), default=("xlsx", "pdf", "html"))
    output(p)

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

    p = command("demo", _demo, "Write every report from fictional data, to see the output without a tenant.")
    output(p, "output folder")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_dir or None)
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
