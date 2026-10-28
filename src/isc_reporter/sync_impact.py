"""Impact of enabling attribute synchronization on a source, before enabling it.

Attribute synchronization keeps an account attribute equal to the identity attribute it is mapped
to. When it is enabled, every account whose current value differs from the value of its identity is
a candidate for an update on the target system. This module reads the provisioning policy of the
source (the fields mapped directly to an identity attribute), the accounts of the source and the
identities they belong to, and counts the updates ISC could send: values added, changed or cleared.
Nothing is changed in the tenant.
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from isc_reporter import dashboard
from isc_reporter.api import call, list_all, model_json
from isc_reporter.csvio import Column, read_rows, write_table
from isc_reporter.errors import UsageError
from isc_reporter.query import quote
from isc_reporter.search import search_all

log = logging.getLogger("isc_reporter")

WARNING, INFO = "Warning", "Info"
ADD, CHANGE, CLEAR, CASE = "Add value", "Change value", "Clear value", "Case only"
SEVERITY = {CLEAR: 4, CHANGE: 3, ADD: 2, CASE: 1}      # the worst change of an account wins
OUTCOME_COLOURS = {"No change": dashboard.GREEN, CASE: dashboard.GREY, ADD: dashboard.BLUE,
                   CHANGE: dashboard.AMBER, CLEAR: dashboard.RED}
ID_CHUNK = 100
DEFAULT_THRESHOLD = 10.0
DEFAULT_MAX_DETAILS = 100_000

MAP_SCHEMA = {
    "identity_attribute": Column(("identity", "identityattribute", "identity_attr")),
    "account_attribute": Column(("account", "accountattribute", "account_attr", "target_attribute")),
}
CHANGE_COLUMNS = ["Account", "Identity", "Identity attribute", "Account attribute", "Current value",
                  "New value", "Change"]


@dataclass(frozen=True)
class Mapping:
    """An account attribute kept equal to an identity attribute."""

    identity_attribute: str
    account_attribute: str
    origin: str


@dataclass
class Change:
    account_id: str
    account: str
    identity_id: str
    identity: str
    mapping: Mapping
    current: str
    new: str
    kind: str
    disabled: bool = False


@dataclass
class Stats:
    compared: int = 0
    kinds: Counter = field(default_factory=Counter)
    account_attribute_found: int = 0       # accounts that have the account attribute at all
    identity_attribute_found: int = 0      # identities that have the identity attribute at all

    @property
    def updates(self) -> int:
        return sum(self.kinds.values())


@dataclass
class Result:
    source: str
    mappings: list[Mapping]
    ignored: list[dict]                    # policy fields that are not a direct identity attribute copy
    accounts: int = 0
    uncorrelated: int = 0
    identity_missing: int = 0
    analysed: int = 0
    disabled: int = 0
    changes: list[Change] = field(default_factory=list)
    stats: dict[Mapping, Stats] = field(default_factory=dict)
    outcomes: dict[str, str] = field(default_factory=dict)   # account id -> worst change
    threshold: float = DEFAULT_THRESHOLD
    notes: list[str] = field(default_factory=list)

    @property
    def impacted(self) -> int:
        return len(self.outcomes)

    @property
    def share(self) -> float:
        return 100 * self.impacted / self.analysed if self.analysed else 0.0

    @property
    def verdict(self) -> str:
        if not self.impacted:
            return "No update expected"
        cleared = any(s.kinds[CLEAR] for s in self.stats.values())
        return "Bulk update expected" if cleared or self.share >= self.threshold else "Limited updates"


# -- mappings -----------------------------------------------------------------------------


def policy_mappings(policies: list[dict], usage_types=("CREATE",)) -> tuple[list[Mapping], list[dict]]:
    """``(direct mappings, other fields)`` of the provisioning policies of one source.

    A field is a direct mapping when its transform is ``identityAttribute``: the account attribute
    takes the value of the identity attribute as is. Any other field cannot be compared here.
    """
    wanted = {u.upper() for u in usage_types}
    mappings: dict[tuple[str, str], Mapping] = {}
    ignored = []
    for policy in policies:
        usage = str(policy.get("usageType", "")).upper()
        if usage not in wanted:
            continue
        origin = f"{usage} policy" + (f" '{policy['name']}'" if policy.get("name") else "")
        for item in policy.get("fields") or []:
            transform, name = item.get("transform") or {}, item.get("name", "")
            identity = (transform.get("attributes") or {}).get("name")
            if transform.get("type") == "identityAttribute" and identity:
                mappings.setdefault((identity.lower(), name.lower()), Mapping(identity, name, origin))
            else:
                kind = transform.get("type", "")
                ignored.append({"Usage": usage, "Field": name, "Transform": kind or "none",
                                "Why": ("Not a direct copy of an identity attribute: the new value "
                                        "depends on " + describe(transform)) if kind else
                                       "No transform: the value is entered at provisioning time, "
                                       "not taken from an identity attribute."})
    return list(mappings.values()), ignored


def describe(transform: dict) -> str:
    kind = transform.get("type", "")
    name = (transform.get("attributes") or {}).get("name")
    return f"the {kind} '{name}'" if name else f"a {kind} transform"


def parse_maps(values: list[str] | None) -> list[Mapping]:
    """``identity=account`` pairs given on the command line."""
    mappings = []
    for value in values or []:
        identity, separator, account = value.partition("=")
        if not separator or not identity.strip() or not account.strip():
            raise UsageError(f"--map '{value}' must look like identityAttribute=accountAttribute")
        mappings.append(Mapping(identity.strip(), account.strip(), "--map"))
    return mappings


def read_map_file(path: str | None) -> list[Mapping]:
    """CSV with the columns ``identity_attribute`` and ``account_attribute``."""
    if not path:
        return []
    rows = read_rows(path, MAP_SCHEMA).rows
    mappings = [Mapping(row["identity_attribute"], row["account_attribute"], f"--map-file {path}")
                for row in rows if row.get("identity_attribute") and row.get("account_attribute")]
    if not mappings:
        raise UsageError(f"'{path}' has no identity_attribute;account_attribute line")
    return mappings


def merge(*groups: list[Mapping]) -> list[Mapping]:
    """Mappings without duplicates; the first group to mention a pair keeps it."""
    seen: dict[tuple[str, str], Mapping] = {}
    for group in groups:
        for mapping in group:
            seen.setdefault((mapping.identity_attribute.lower(), mapping.account_attribute.lower()), mapping)
    return list(seen.values())


# -- reading the tenant -------------------------------------------------------------------


def find_source(isc, name: str):
    found = call(isc.sources.list_sources_v1, filters=f"name eq {quote(name)}", limit=2) or []
    if not found:
        raise UsageError(f"source '{name}' not found")
    if len(found) > 1:
        raise UsageError(f"source name '{name}' is ambiguous")
    return found[0]


def fetch_policies(isc, source_id: str) -> list[dict]:
    return [model_json(p) for p in list_all(isc.sources.list_provisioning_policies_v1, source_id=source_id)]


def fetch_accounts(isc, source_id: str) -> list[dict]:
    return [model_json(a) for a in list_all(isc.accounts.list_accounts_v1,
                                            filters=f"sourceId eq {quote(source_id)}")]


def fetch_identities(isc, ids: list[str]) -> dict[str, dict]:
    """Identity search documents (they carry the identity attributes) by id."""
    found: dict[str, dict] = {}
    unique = list(dict.fromkeys(ids))
    for start in range(0, len(unique), ID_CHUNK):
        query = "id:(" + " OR ".join(quote(i) for i in unique[start:start + ID_CHUNK]) + ")"
        for doc in search_all(isc, "identities", query, sort_field=None, include_nested=False):
            found[doc.get("id", "")] = doc
        if start and (start // ID_CHUNK) % 20 == 0:
            log.info("%d / %d identities read", min(start + ID_CHUNK, len(unique)), len(unique))
    return found


# -- analysis ----------------------------------------------------------------------------


def values_of(value) -> tuple[str, ...]:
    """Comparable form of an attribute value: sorted non-empty strings (empty tuple = no value)."""
    if value is None:
        return ()
    items = value if isinstance(value, (list, tuple, set)) else [value]
    return tuple(sorted(_text(v) for v in items if v is not None and _text(v) != ""))


def _text(value) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, dict):
        return str(value.get("name") or value.get("id") or json.dumps(value, sort_keys=True))
    return str(value)


def lookup(container: dict | None, name: str) -> tuple[bool, object]:
    """``(present, value)``; the name is matched exactly, then ignoring case."""
    container = container or {}
    if name in container:
        return True, container[name]
    lowered = name.lower()
    for key, value in container.items():
        if str(key).lower() == lowered:
            return True, value
    return False, None


def classify(current: tuple[str, ...], new: tuple[str, ...]) -> str | None:
    """The update ISC would send, or None when the account already has the identity's value."""
    if current == new:
        return None
    if not new:
        return CLEAR
    if not current:
        return ADD
    if [v.lower() for v in current] == [v.lower() for v in new]:
        return CASE
    return CHANGE


def analyse(source: str, accounts: list[dict], identities: dict[str, dict], mappings: list[Mapping],
            ignored: list[dict] | None = None, threshold: float = DEFAULT_THRESHOLD) -> Result:
    result = Result(source, mappings, ignored or [], accounts=len(accounts), threshold=threshold)
    result.stats = {m: Stats() for m in mappings}
    for account in accounts:
        identity = identities.get(account.get("identityId") or "")
        if account.get("uncorrelated") or not account.get("identityId"):
            result.uncorrelated += 1
            continue
        if identity is None:
            result.identity_missing += 1
            continue
        result.analysed += 1
        result.disabled += bool(account.get("disabled"))
        worst = None
        for mapping in mappings:
            stats = result.stats[mapping]
            stats.compared += 1
            has_account, current = lookup(account.get("attributes"), mapping.account_attribute)
            has_identity, new = lookup(identity.get("attributes"), mapping.identity_attribute)
            if not has_identity:                       # a few attributes are top-level fields
                has_identity, new = lookup(identity, mapping.identity_attribute)
            stats.account_attribute_found += has_account
            stats.identity_attribute_found += has_identity
            kind = classify(values_of(current), values_of(new))
            if kind is None:
                continue
            stats.kinds[kind] += 1
            if worst is None or SEVERITY[kind] > SEVERITY[worst]:
                worst = kind
            result.changes.append(Change(
                account.get("id", ""), account.get("name") or account.get("nativeIdentity", ""),
                account["identityId"], identity.get("displayName") or identity.get("name", ""),
                mapping, "; ".join(values_of(current)), "; ".join(values_of(new)), kind,
                bool(account.get("disabled"))))
        if worst:
            result.outcomes[account.get("id", "")] = worst
    result.notes = _notes(result)
    return result


def _notes(result: Result) -> list[str]:
    notes = []
    if not result.mappings:
        notes.append("No attribute is mapped directly to an identity attribute: "
                     "nothing would be synchronised.")
    return notes


def checks(result: Result) -> list[tuple[str, str, str, str]]:
    """``(severity, attribute, check, details)``."""
    out = []
    for note in result.notes:
        out.append((WARNING, "", "Nothing to synchronise", note))
    if result.uncorrelated:
        out.append((INFO, "", "Uncorrelated accounts", f"{result.uncorrelated} account(s) belong to no "
                    "identity: they have no identity value to synchronise and are left out."))
    if result.identity_missing:
        out.append((WARNING, "", "Identity not found", f"{result.identity_missing} account(s) point to an "
                    "identity that could not be read: they are left out."))
    if result.disabled and result.impacted:
        disabled = sum(c.disabled for c in result.changes)
        if disabled:
            out.append((INFO, "", "Disabled accounts", f"{result.disabled} analysed account(s) are disabled; "
                    f"{len({c.account_id for c in result.changes if c.disabled})} would be updated."))
    for mapping, stats in result.stats.items():
        label = f"{mapping.identity_attribute} -> {mapping.account_attribute}"
        if stats.compared and not stats.account_attribute_found:
            out.append((WARNING, label, "Account attribute not found",
                        f"No account of {result.source} has the attribute '{mapping.account_attribute}': "
                        "check its name (it is case sensitive) in the account schema."))
        if stats.compared and not stats.identity_attribute_found:
            out.append((WARNING, label, "Identity attribute not found",
                        f"No identity has the attribute '{mapping.identity_attribute}': every account "
                        "that has a value would be cleared."))
        if stats.kinds[CLEAR]:
            out.append((WARNING, label, "Values would be cleared", f"{stats.kinds[CLEAR]} account(s) have a "
                        f"value but the identity has none: ISC would empty '{mapping.account_attribute}'."))
        share = 100 * (stats.updates - stats.kinds[CASE]) / stats.compared if stats.compared else 0
        if share >= result.threshold:
            out.append((WARNING, label, "Bulk update", f"{share:.0f}% of the accounts "
                        f"({stats.updates - stats.kinds[CASE]} of {stats.compared}) would be updated "
                        f"(threshold {result.threshold:g}%)."))
        if stats.kinds[CASE]:
            out.append((INFO, label, "Case only", f"{stats.kinds[CASE]} account(s) differ only by "
                        "upper/lower case; the target system may or may not ignore it."))
    if result.ignored:
        out.append((INFO, "", "Fields not analysed", f"{len(result.ignored)} field(s) of the policy are "
                    "not a direct copy of an identity attribute (see the Not analysed sheet)."))
    return out


# -- report -------------------------------------------------------------------------------

READ_ME = [
    ("What is this?", "A forecast of the updates ISC could send to the target system if attribute "
                      "synchronization is enabled on the source. Nothing was changed in the tenant."),
    ("How is it computed?", "For each account attribute mapped directly to an identity attribute in the "
                            "provisioning policy (or given with --map), the current value of the account is "
                            "compared with the current value of its identity. Each difference is an update."),
    ("Worst case", "Every difference found is counted, as if ISC evaluated all accounts. In practice ISC "
                   "may send only the changes it detects at the next refresh, so read the figures as an "
                   "upper bound."),
    ("Add value", "The account has no value, the identity has one: ISC would write it."),
    ("Change value", "Both have a value and they differ: ISC would overwrite the account value."),
    ("Clear value", "The account has a value, the identity has none: ISC would empty the account "
                    "attribute. The riskiest change."),
    ("Case only", "The values differ only by upper/lower case."),
    ("Verdict", "No update expected / Limited updates / Bulk update expected (a value would be cleared, or "
                "the share of accounts to update reaches the threshold)."),
    ("Not analysed", "Policy fields that are not a direct copy of an identity attribute, accounts without "
                     "identity, and attributes handled by rules cannot be forecast here."),
    ("Privacy", "The Changes sheet shows real attribute values (names, emails...). Protect the file."),
]


def attribute_label(mapping: Mapping) -> str:
    return f"{mapping.identity_attribute} -> {mapping.account_attribute}"


def attribute_rows(result: Result) -> list[dict]:
    rows = []
    for mapping, stats in result.stats.items():
        changed = stats.updates
        rows.append({
            "Identity attribute": mapping.identity_attribute, "Account attribute": mapping.account_attribute,
            "From": mapping.origin, "Accounts compared": stats.compared,
            "No change": stats.compared - changed, ADD: stats.kinds[ADD], CHANGE: stats.kinds[CHANGE],
            CLEAR: stats.kinds[CLEAR], CASE: stats.kinds[CASE], "Updates": changed,
            "Share of accounts": changed / stats.compared if stats.compared else 0,
            "Risk": _risk(stats, result.threshold)})
    return rows


def _risk(stats: Stats, threshold: float) -> str:
    real = stats.updates - stats.kinds[CASE]
    if stats.kinds[CLEAR] or (stats.compared and 100 * real / stats.compared >= threshold):
        return "High"
    return "Low" if stats.updates else "None"


def account_rows(result: Result, limit: int) -> list[dict]:
    by_account: dict[str, list[Change]] = defaultdict(list)
    for change in result.changes:
        by_account[change.account_id].append(change)
    rows = [{"Account": items[0].account, "Identity": items[0].identity,
             "Disabled": "yes" if items[0].disabled else "no", "Updates": len(items),
             "Attributes": "\n".join(c.mapping.account_attribute for c in items),
             "Worst change": result.outcomes[account_id]}
            for account_id, items in by_account.items()]
    rows.sort(key=lambda r: (-SEVERITY[r["Worst change"]], -r["Updates"], r["Account"].lower()))
    return rows[:limit]


def change_rows(result: Result, limit: int | None = None) -> list[dict]:
    ordered = sorted(result.changes, key=lambda c: (-SEVERITY[c.kind], c.mapping.account_attribute.lower(),
                                                    c.account.lower()))
    return [{"Account": c.account, "Identity": c.identity, "Identity attribute": c.mapping.identity_attribute,
             "Account attribute": c.mapping.account_attribute, "Current value": c.current,
             "New value": c.new, "Change": c.kind} for c in ordered[:limit]]


def build_dashboard(result: Result, origin: str = "", title: str = "",
                    max_details: int = DEFAULT_MAX_DETAILS) -> dashboard.Dashboard:
    rows = attribute_rows(result)
    found = checks(result)
    check_rows = sorted(({"Severity": s, "Attribute": a, "Check": c, "Details": d} for s, a, c, d in found),
                        key=lambda r: (r["Severity"] != WARNING, r["Attribute"].lower()))
    accounts = account_rows(result, max_details)
    changes = change_rows(result, max_details)
    kinds = Counter(result.outcomes.values())
    outcome = [("No change", result.analysed - result.impacted), (CASE, kinds[CASE]), (ADD, kinds[ADD]),
               (CHANGE, kinds[CHANGE]), (CLEAR, kinds[CLEAR])]
    verdict_colour = {"No update expected": dashboard.GREEN, "Limited updates": dashboard.AMBER,
                      "Bulk update expected": dashboard.RED}[result.verdict]
    total = sum(s.updates for s in result.stats.values())
    kpis = [("Source", result.source, dashboard.BLUE), ("Verdict", result.verdict, verdict_colour),
            ("Accounts analysed", str(result.analysed), dashboard.BLUE),
            ("Accounts to update", f"{result.impacted}  ({result.share:.1f}%)", verdict_colour),
            ("Attribute updates", str(total), verdict_colour),
            ("Attributes synchronised", str(len(result.mappings)), dashboard.BLUE),
            ("Left out", f"{result.uncorrelated + result.identity_missing}  (no identity)", dashboard.GREY)]
    charts = []
    if result.analysed:
        charts.append(dashboard.Chart("Accounts by outcome", "doughnut", [n for n, v in outcome if v],
                                      [("Accounts", [v for _, v in outcome if v], "")]))
    if rows:
        labels = [f"{r['Identity attribute']} -> {r['Account attribute']}" for r in rows]
        charts.append(dashboard.Chart("Updates per attribute", "stacked", labels, [
            (kind, [r[kind] for r in rows], OUTCOME_COLOURS[kind]) for kind in (ADD, CHANGE, CLEAR, CASE)]))
    wide = {"Details", "Attributes", "Why", "Current value", "New value", "From"}
    tables = [dashboard.Table("Attributes", rows, percent={"Share of accounts"}, wide=wide,
                              fills={"Risk": {"High": "F8CBAD", "Low": "FFF2CC", "None": "E2F0D9"}}),
              dashboard.Table("Accounts", accounts, wide=wide, fills={"Worst change": {
                  CLEAR: "F8CBAD", CHANGE: "FFE0B2", ADD: "DDEBF7", CASE: "EDEDED"}}),
              dashboard.Table("Changes", changes, wide=wide, fills={"Change": {
                  CLEAR: "F8CBAD", CHANGE: "FFE0B2", ADD: "DDEBF7", CASE: "EDEDED"}}),
              dashboard.Table("Not analysed", result.ignored, wide=wide),
              dashboard.Table("Checks", check_rows, wide=wide, fills={"Severity": {
                  WARNING: "F8CBAD", INFO: "DDEBF7"}})]
    sections = [
        dashboard.Section("Attributes", [{k: v for k, v in r.items() if k != "From"} for r in rows],
                          ["Identity attribute", "Account attribute", "Accounts compared", ADD, CHANGE, CLEAR,
                           CASE, "Risk"]),
        dashboard.Section("Points to check", check_rows, ["Severity", "Attribute", "Check", "Details"]),
        dashboard.Section("Accounts to update (worst first)", accounts,
                          ["Account", "Identity", "Updates", "Attributes", "Worst change"]),
    ]
    note = f"{len(result.changes)} change(s), the first {max_details} are listed" \
        if len(result.changes) > max_details else ""
    return dashboard.Dashboard(
        title or f"Attribute synchronization impact: {result.source}", dashboard.subtitle(origin, note),
        kpis, charts, tables, sections, READ_ME, colours={**OUTCOME_COLOURS, WARNING: dashboard.RED,
                                                         INFO: dashboard.BLUE},
        wide_pdf={"Details", "Attributes", "Account", "Identity"},
        alert=lambda row: row.get("Severity") == WARNING or row.get("Worst change") == CLEAR)


def write_xlsx(path: str, result: Result, origin: str = "", title: str = "",
               max_details: int = DEFAULT_MAX_DETAILS) -> str:
    return dashboard.write_xlsx(build_dashboard(result, origin, title, max_details), path)


def write_pdf(path: str, result: Result, origin: str = "", title: str = "",
              max_details: int = DEFAULT_MAX_DETAILS, max_rows: int = 500) -> str:
    return dashboard.write_pdf(build_dashboard(result, origin, title, max_details), path, max_rows)


def write_csv(path: str, result: Result) -> str:
    """Every change, without limit (one line per account attribute to update)."""
    rows = change_rows(result)
    return write_table(path, CHANGE_COLUMNS, [[row[c] for c in CHANGE_COLUMNS] for row in rows], ";")
