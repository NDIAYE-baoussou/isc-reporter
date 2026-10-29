"""Leavers who still have access: identities in a leaver lifecycle state that still have enabled
accounts, and for each account, whether the lifecycle state is configured to disable it.

The cause tells what to fix. "Should be disabled" means the lifecycle state disables (or deletes)
the account but it is still enabled: provisioning failed, is pending, or was undone. "Not covered
by the lifecycle state" means nothing in the configuration disables it: a gap to close.
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime

from isc_reporter import dashboard
from isc_reporter.csvio import write_table
from isc_reporter.errors import UsageError
from isc_reporter.query import quote

log = logging.getLogger("isc_reporter")

INACTIVE = {"INACTIVE_SHORT_TERM", "INACTIVE_LONG_TERM"}
DISABLE, DELETE, KEPT = "Should be disabled", "Should be deleted", "Kept enabled by the lifecycle state"
NOT_COVERED, UNKNOWN = "Not covered by the lifecycle state", "Lifecycle state not configured"
CAUSES = (DISABLE, DELETE, NOT_COVERED, UNKNOWN, KEPT)
CAUSE_FILLS = {DISABLE: "F8CBAD", DELETE: "F8CBAD", NOT_COVERED: "FFE0B2", UNKNOWN: "EDEDED", KEPT: "DDEBF7"}
CAUSE_COLOURS = {DISABLE: dashboard.RED, DELETE: "8E24AA", NOT_COVERED: dashboard.AMBER,
                 UNKNOWN: dashboard.GREY, KEPT: dashboard.BLUE}
ACCOUNT_COLUMNS = ["Identity", "Display name", "Lifecycle state", "Left on", "Days since", "Source",
                   "Account", "Privileged", "Cause", "Beyond grace"]
DATE_FORMATS = ("%d/%m/%Y", "%Y%m%d")      # after ISO 8601


@dataclass
class LeaverData:
    states: list[str]                                    # lifecycle states (technical names) of leavers
    identities: list[dict]                               # identity search documents in those states
    lifecycle: dict[str, dict[str, dict]]                # identity profile id -> technical name -> state
    profiles: dict[str, str] = field(default_factory=dict)          # identity profile id -> name
    skipped_sources: dict[str, str] = field(default_factory=dict)   # source id -> why it is left out
    date_attribute: str = ""
    grace: int = 0
    today: date = field(default_factory=date.today)
    notes: list[str] = field(default_factory=list)
    origin: str = ""


# -- reading ------------------------------------------------------------------------------


def fetch(isc, states: list[str] | None = None, exclude_sources: list[str] | None = None,
          include_authoritative: bool = False, date_attribute: str = "", grace: int = 0) -> LeaverData:
    """Lifecycle states of every identity profile, the sources, and the identities in the leaver
    states (by default, the states configured as inactive)."""
    from isc_reporter.api import error_message, list_all, model_json
    from isc_reporter.search import search_all

    profiles = {p.id: p.name for p in list_all(isc.identity_profiles.list_identity_profiles_v1)}
    lifecycle, notes = {}, []
    for profile_id, name in profiles.items():
        try:
            found = list_all(isc.lifecycle_states.get_lifecycle_states_v1, identity_profile_id=profile_id)
        except Exception as exc:
            notes.append(f"Lifecycle states of the profile '{name}' not read ({error_message(exc)}).")
            continue
        lifecycle[profile_id] = {s.get("technicalName", ""): s for s in (model_json(x) for x in found)}
    states = states or default_states(lifecycle)
    if not states:
        raise UsageError("no lifecycle state is configured as inactive: give the leaver states with "
                         "--states, e.g. --states inactive terminated")
    skipped = {}
    wanted = {name.lower() for name in exclude_sources or []}
    for source in list_all(isc.sources.list_sources_v1):
        if source.name.lower() in wanted:
            skipped[source.id] = "excluded with --exclude-source"
        elif source.authoritative and not include_authoritative:
            skipped[source.id] = "authoritative source"
    query = "attributes.cloudLifecycleState:(" + " OR ".join(quote(s) for s in states) + ")"
    log.info("Searching identities: %s", query)
    identities = search_all(isc, "identities", query, sort_field="name", descending=False)
    log.info("%d identit(ies) in %s", len(identities), ", ".join(states))
    return LeaverData(states, identities, lifecycle, profiles, skipped, date_attribute, grace,
                      notes=notes, origin=isc.base_url)


def default_states(lifecycle: dict[str, dict[str, dict]]) -> list[str]:
    """Technical names of the lifecycle states whose identity state is inactive, in any profile."""
    return sorted({name for states in lifecycle.values() for name, state in states.items()
                   if name and str(state.get("identityState") or "").upper() in INACTIVE})


# -- analysis -----------------------------------------------------------------------------


def expected_action(state: dict, source_id: str) -> str | None:
    """The account action (ENABLE, DISABLE, DELETE) the lifecycle state applies to a source."""
    for action in state.get("accountActions") or []:
        covered = source_id in (action.get("sourceIds") or [])
        if action.get("allSources") and source_id not in (action.get("excludeSourceIds") or []):
            covered = True
        if covered:
            return str(action.get("action") or "").upper()
    return None


def cause(state: dict | None, source_id: str) -> str:
    if state is None:
        return UNKNOWN
    return {"DISABLE": DISABLE, "DELETE": DELETE, "ENABLE": KEPT}.get(expected_action(state, source_id) or "",
                                                                      NOT_COVERED)


def left_on(value) -> date | None:
    """The departure date from an identity attribute: ISO 8601, dd/MM/yyyy or yyyyMMdd."""
    if not value:
        return None
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


@dataclass
class Result:
    data: LeaverData
    accounts: list[dict]                   # one row per enabled account (ACCOUNT_COLUMNS)
    leavers: list[dict]                    # one row per identity with at least one enabled account
    without_access: int = 0
    unparsed_dates: int = 0

    @property
    def total(self) -> int:
        return len(self.data.identities)


def analyse(data: LeaverData) -> Result:
    accounts, leavers = [], []
    without, unparsed = 0, 0
    for identity in data.identities:
        attributes = identity.get("attributes") or {}
        state_name = str(attributes.get("cloudLifecycleState") or "")
        profile_id = (identity.get("identityProfile") or {}).get("id", "")
        state = data.lifecycle.get(profile_id, {}).get(state_name)
        left = left_on(attributes.get(data.date_attribute)) if data.date_attribute else None
        if data.date_attribute and attributes.get(data.date_attribute) and left is None:
            unparsed += 1
        days = (data.today - left).days if left else None
        beyond = "" if days is None else ("yes" if days > data.grace else "no")
        enabled = [a for a in identity.get("accounts") or []
                   if not a.get("disabled") and (a.get("source") or {}).get("id") not in data.skipped_sources]
        if not enabled:
            without += 1
            continue
        rows = []
        for account in enabled:
            source = account.get("source") or {}
            rows.append({
                "Identity": identity.get("name", ""), "Display name": identity.get("displayName", ""),
                "Lifecycle state": state_name, "Left on": left.isoformat() if left else "",
                "Days since": days, "Source": source.get("name", ""),
                "Account": account.get("accountId") or account.get("name", ""),
                "Privileged": "yes" if account.get("privileged") else "no",
                "Cause": cause(state, source.get("id", "")), "Beyond grace": beyond})
        accounts += rows
        leavers.append({
            "Identity": identity.get("name", ""), "Display name": identity.get("displayName", ""),
            "Lifecycle state": state_name,
            "Identity profile": (identity.get("identityProfile") or {}).get("name", ""),
            "Left on": left.isoformat() if left else "", "Days since": days,
            "Enabled accounts": len(rows), "Privileged": sum(r["Privileged"] == "yes" for r in rows),
            "Sources": "\n".join(dict.fromkeys(r["Source"] for r in rows)),
            "Causes": "\n".join(dict.fromkeys(r["Cause"] for r in rows))})
    accounts.sort(key=lambda r: (r["Privileged"] != "yes", CAUSES.index(r["Cause"]), -(r["Days since"] or 0),
                                 r["Identity"].lower()))
    leavers.sort(key=lambda r: (-r["Privileged"], -(r["Days since"] or 0), r["Identity"].lower()))
    return Result(data, accounts, leavers, without, unparsed)


# -- report -------------------------------------------------------------------------------

READ_ME = [
    ("What is this?", "Identities in a leaver lifecycle state that still have enabled accounts. Nothing "
                      "was changed in the tenant."),
    ("Leaver states", "The lifecycle states listed under Settings: by default those configured as "
                      "inactive in the identity profiles, or the ones given with --states."),
    ("Left out", "Disabled accounts, accounts of authoritative sources (unless --include-authoritative) "
                 "and of the sources given with --exclude-source."),
    (DISABLE, "The lifecycle state disables the account on this source, but it is still enabled: "
              "provisioning failed, is still pending, or the account was enabled again."),
    (DELETE, "The lifecycle state deletes the account, but it is still there and enabled."),
    (NOT_COVERED, "Nothing in the lifecycle state disables accounts of this source: add the source to the "
                  "state's account actions, or handle it another way."),
    (UNKNOWN, "The identity's lifecycle state does not exist in its identity profile, or the profile's "
              "states could not be read."),
    (KEPT, "The lifecycle state explicitly keeps accounts of this source enabled."),
    ("Days since / Beyond grace", "With --date-attribute, days since the departure date, and whether it "
                                  "is more than the --grace days allowed to revoke access."),
    ("Privacy", "The sheets list real people and accounts: protect the file."),
]


def by_source(result: Result) -> list[dict]:
    counts: dict[str, Counter] = defaultdict(Counter)
    for row in result.accounts:
        counts[row["Source"]]["Enabled accounts"] += 1
        counts[row["Source"]][row["Cause"]] += 1
        counts[row["Source"]]["Privileged"] += row["Privileged"] == "yes"
    rows = [{"Source": source, "Enabled accounts": c["Enabled accounts"], "Privileged": c["Privileged"],
             **{cause_: c[cause_] for cause_ in CAUSES}} for source, c in counts.items()]
    return sorted(rows, key=lambda r: (-r["Enabled accounts"], r["Source"].lower()))


def state_rows(result: Result) -> list[dict]:
    data = result.data
    found = Counter(((i.get("identityProfile") or {}).get("id", ""),
                     str((i.get("attributes") or {}).get("cloudLifecycleState") or ""))
                    for i in data.identities)
    rows = []
    for profile_id, states in data.lifecycle.items():
        for name, state in states.items():
            if name not in data.states:
                continue
            actions = []
            for action in state.get("accountActions") or []:
                target = ("all sources" if action.get("allSources") else
                          f"{len(action.get('sourceIds') or [])} source(s)")
                if action.get("excludeSourceIds"):
                    target += f" except {len(action['excludeSourceIds'])}"
                actions.append(f"{action.get('action', '')} on {target}")
            rows.append({"Identity profile": data.profiles.get(profile_id, profile_id),
                         "Lifecycle state": name, "Identity state": state.get("identityState") or "",
                         "Account actions": "\n".join(actions) or "none",
                         "Identities found": found[(profile_id, name)]})
    return rows


def build_dashboard(result: Result, title: str = "") -> dashboard.Dashboard:
    data = result.data
    with_access = len(result.leavers)
    share = 100 * with_access / result.total if result.total else 0
    causes = Counter(r["Cause"] for r in result.accounts)
    privileged = sum(r["Privileged"] == "yes" for r in result.accounts)
    alarm = dashboard.RED if with_access else dashboard.GREEN
    kpis = [
        ("Leavers", f"{result.total}  ({', '.join(data.states)})", dashboard.BLUE),
        ("Leavers with enabled access", f"{with_access}  ({share:.1f}%)", alarm),
        ("Enabled accounts", str(len(result.accounts)), alarm),
        ("Privileged accounts", str(privileged), dashboard.RED if privileged else dashboard.GREEN),
        ("Should have been disabled", str(causes[DISABLE] + causes[DELETE]),
         dashboard.RED if causes[DISABLE] + causes[DELETE] else dashboard.GREEN),
        ("Not covered by the lifecycle state", str(causes[NOT_COVERED]),
         dashboard.AMBER if causes[NOT_COVERED] else dashboard.GREEN),
    ]
    if data.date_attribute:
        beyond = sum(r["Beyond grace"] == "yes" for r in result.accounts)
        kpis.append((f"Beyond {data.grace} day(s) of grace", str(beyond),
                     dashboard.RED if beyond else dashboard.GREEN))
    charts = []
    labels = [c for c in CAUSES if causes[c]]
    if labels:
        charts.append(dashboard.Chart("Enabled accounts by cause", "doughnut", labels,
                                      [("Accounts", [causes[c] for c in labels], "")]))
    sources = by_source(result)[:12]
    if sources:
        series = [(c, [r[c] for r in sources], CAUSE_COLOURS[c])
                  for c in CAUSES if any(r[c] for r in sources)]
        charts.append(dashboard.Chart("Enabled accounts by source", "stacked", [r["Source"] for r in sources],
                                      series))
    settings = [{"Setting": "Leaver lifecycle states", "Value": ", ".join(data.states)},
                {"Setting": "Sources left out", "Value": ", ".join(sorted(set(data.skipped_sources.values())))
                 or "none"},
                {"Setting": "Departure date attribute", "Value": data.date_attribute or "not used"},
                {"Setting": "Grace (days)", "Value": str(data.grace) if data.date_attribute else "not used"}]
    settings += [{"Setting": "Note", "Value": note} for note in data.notes]
    if result.unparsed_dates:
        settings.append({"Setting": "Note",
                         "Value": f"{result.unparsed_dates} departure date(s) could not be read (expected "
                                  "ISO 8601, dd/MM/yyyy or yyyyMMdd)."})
    wide = {"Sources", "Causes", "Account actions", "Value", "Display name"}
    fills = {"Cause": CAUSE_FILLS}
    tables = [dashboard.Table("Leavers with access", result.leavers, wide=wide, red_positive={"Privileged"}),
              dashboard.Table("Enabled accounts", result.accounts, wide=wide, fills=fills,
                              red_yes={"Privileged", "Beyond grace"}),
              dashboard.Table("By source", by_source(result), wide=wide, red_positive={"Privileged"}),
              dashboard.Table("Lifecycle states", state_rows(result), wide=wide),
              dashboard.Table("Settings", settings, wide=wide)]
    sections = [
        dashboard.Section("Enabled accounts by source", by_source(result),
                          ["Source", "Enabled accounts", "Privileged", DISABLE, NOT_COVERED]),
        dashboard.Section("Leavers with enabled access", result.leavers,
                          ["Identity", "Lifecycle state", "Days since", "Enabled accounts", "Sources"]),
    ]
    return dashboard.Dashboard(
        title or "Leavers with access", dashboard.subtitle(data.origin), kpis, charts, tables, sections,
        READ_ME, colours=CAUSE_COLOURS,
        wide_pdf={"Identity", "Sources", "Source"},
        alert=lambda row: bool(row.get("Privileged")) and row.get("Privileged") not in ("no", 0))


def write_xlsx(path: str, result: Result, title: str = "") -> str:
    return dashboard.write_xlsx(build_dashboard(result, title), path)


def write_pdf(path: str, result: Result, title: str = "", max_rows: int = 500) -> str:
    return dashboard.write_pdf(build_dashboard(result, title), path, max_rows)


def write_csv(path: str, result: Result) -> str:
    rows = [[row[c] for c in ACCOUNT_COLUMNS] for row in result.accounts]
    return write_table(path, ACCOUNT_COLUMNS, rows, ";")
