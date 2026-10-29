"""Role enablement impact: who a role would be assigned to, and what enabling it would provision.

Every identity is checked against the role's membership criteria (or identity list), then compared
with what it holds today: who would get the role and which of its entitlements would be provisioned
(with the accounts to create on sources where they have none), who already has it, who has it
outside the criteria, and who holds all of its access without matching. Nothing is changed in the
tenant.

The criteria are evaluated here the way ISC is expected to evaluate them: values are compared
ignoring case, a multi-valued attribute matches when one of its values does, and NOT_EQUALS and
DOES_NOT_CONTAIN match when no value does. For a role already assigned by its criteria, the
identities that match are compared with those the criteria assigned, which shows whether this
holds on the tenant.
"""

from __future__ import annotations

import logging
import operator
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from isc_reporter import dashboard
from isc_reporter.criteria import criteria_text, key_attribute, leaves
from isc_reporter.csvio import write_table
from isc_reporter.errors import UsageError
from isc_reporter.query import quote
from isc_reporter.sync_impact import lookup

log = logging.getLogger("isc_reporter")

NEW_ACCESS, ROLE_ONLY = "Gets the role and new access", "Gets the role, access already held"
KEEPS, LOSES = "Already has the role", "Loses the role"
OUTSIDE, GAP = "Has the role outside the criteria", "Has the access without the role"
OUTCOMES = (NEW_ACCESS, ROLE_ONLY, LOSES, OUTSIDE, GAP, KEEPS)
OUTCOME_FILLS = {NEW_ACCESS: "DDEBF7", ROLE_ONLY: "E2EFDA", KEEPS: "EDEDED", LOSES: "F8CBAD",
                 OUTSIDE: "FFE0B2", GAP: "E1D5E7"}
OUTCOME_COLOURS = {NEW_ACCESS: dashboard.BLUE, ROLE_ONLY: dashboard.GREEN, KEEPS: dashboard.GREY,
                   LOSES: dashboard.RED, OUTSIDE: dashboard.AMBER, GAP: "8E24AA"}
BY_CRITERIA, BY_REQUEST = "ROLE_MEMBERSHIP", "ACCESS_REQUEST"
ASSIGNED_BY = {BY_CRITERIA: "criteria", BY_REQUEST: "access request", "": "not known"}
NEW_ACCOUNT = "new account"
NEGATIONS = {"NOT_EQUALS": "EQUALS", "DOES_NOT_CONTAIN": "CONTAINS"}
ORDERINGS = {"GREATER_THAN": operator.gt, "LESS_THAN": operator.lt,
             "GREATER_THAN_EQUALS": operator.ge, "LESS_THAN_EQUALS": operator.le}
SUPPORTED = {"EQUALS", "CONTAINS", "STARTS_WITH", "ENDS_WITH", *NEGATIONS, *ORDERINGS}
IDENTITY_COLUMNS = ["Identity", "Display name", "Lifecycle state", "Outcome", "Assigned by",
                    "Entitlements to add", "Accounts to create", "Entitlements held", "To add"]


@dataclass
class RoleData:
    role: dict                             # the role, as the roles API returns it
    access: list[dict]                     # entitlements it grants, with "via" (see fetch_access)
    identities: list[dict]                 # identity search documents analysed
    # source id -> identity id -> attributes of its accounts, for the sources read by account criteria
    accounts: dict[str, dict[str, list[dict]]] = field(default_factory=dict)
    # identity id -> ROLE_MEMBERSHIP or ACCESS_REQUEST; None when the assignments were not read
    assigned: dict[str, str] | None = None
    query: str = "*"
    notes: list[str] = field(default_factory=list)
    origin: str = ""


# -- reading ------------------------------------------------------------------------------


def find_role(isc, name: str) -> dict:
    from isc_reporter.api import call, model_json

    found = call(isc.roles.list_roles_v1, filters=f"name eq {quote(name)}", limit=2) or []
    if not found:
        raise UsageError(f"role '{name}' not found")
    if len(found) > 1:
        raise UsageError(f"role name '{name}' is ambiguous")
    return model_json(found[0])


def load_role(paths: list[str], name: str | None = None) -> dict:
    """A role from JSON files: as the roles API returns it, a draft with other criteria, or a
    configuration export (give the *name* when the files hold several roles)."""
    from isc_reporter.identity_profiles import iter_objects

    roles = [obj for obj in iter_objects(paths)
             if ("membership" in obj or "accessProfiles" in obj) and not obj.get("parentId")]
    if name:
        roles = [role for role in roles if str(role.get("name", "")).lower() == name.lower()]
    if len(roles) == 1:
        return roles[0]
    if not roles:
        raise UsageError(f"no role{f' named {name!r}' if name else ''} in the files")
    names = sorted(str(role.get("name", "")) for role in roles)
    raise UsageError(f"{len(roles)} roles in the files, give the role name: {', '.join(names[:10])}"
                     f"{' ...' if len(names) > 10 else ''}")


def fetch(isc, role: dict, query: str = "*") -> RoleData:
    """What the analysis needs besides the role: the entitlements it grants, the identities
    matching *query* with their access, the accounts of the sources its criteria read, and the
    identities it is assigned to today."""
    from isc_reporter.api import error_message, list_all, model_json
    from isc_reporter.search import search_all

    data = RoleData(role, [], [], query=query, origin=isc.base_url)
    data.access = fetch_access(isc, role, data.notes)
    log.info("Role '%s': %d entitlement(s) to grant", role.get("name", ""), len(data.access))
    if role.get("dimensional"):
        data.notes.append("The role is dimensional: the criteria of its dimensions are not analysed.")
    for source_id in account_sources(role):
        by_identity: dict[str, list[dict]] = defaultdict(list)
        for account in list_all(isc.accounts.list_accounts_v1, filters=f"sourceId eq {quote(source_id)}"):
            account = model_json(account)
            if account.get("identityId"):
                by_identity[account["identityId"]].append(account.get("attributes") or {})
        data.accounts[source_id] = dict(by_identity)
        log.info("%d identit(ies) with an account on the source %s", len(by_identity), source_id)
    if role.get("id"):
        try:
            found = list_all(isc.roles.get_role_assigned_identities_v1, id=role["id"])
        except Exception as exc:
            data.notes.append(f"Identities assigned the role not read ({error_message(exc)}): taken from "
                              "the identities' access, without how they were assigned.")
        else:
            data.assigned = {item.get("id", ""): str(item.get("roleAssignmentSource") or "")
                             for item in (model_json(i) for i in found)}
    log.info("Searching identities: %s", query)
    data.identities = search_all(isc, "identities", query, sort_field="name", descending=False,
                                 progress=lambda n: n % 2500 == 0 and log.info("%d identities so far", n))
    log.info("%d identit(ies) analysed", len(data.identities))
    return data


def fetch_access(isc, role: dict, notes: list[str]) -> list[dict]:
    """The entitlements the role grants (search documents), each with "via": the names of the role's
    access profiles that contain it, or "role" when the role grants it directly."""
    from isc_reporter.search import search_by_ids

    profile_ids = [ref["id"] for ref in role.get("accessProfiles") or [] if ref.get("id")]
    direct_ids = [ref["id"] for ref in role.get("entitlements") or [] if ref.get("id")]
    access: dict[str, dict] = {}
    profiles = search_by_ids(isc, "accessprofiles", profile_ids)
    for profile in profiles:
        for entitlement in profile.get("entitlements") or []:
            item = access.setdefault(entitlement.get("id", ""),
                                     {**entitlement, "source": profile.get("source") or {}, "via": []})
            item["via"].append(profile.get("name", ""))
    entitlements = search_by_ids(isc, "entitlements", direct_ids)
    for entitlement in entitlements:
        access.setdefault(entitlement.get("id", ""), {**entitlement, "via": []})["via"].append("role")
    for kind, wanted, found in (("access profile", profile_ids, profiles),
                                ("entitlement", direct_ids, entitlements)):
        missing = set(wanted) - {doc.get("id") for doc in found}
        if missing:
            notes.append(f"{len(missing)} {kind}(s) of the role not found by search, left out: "
                         f"{', '.join(sorted(missing))}.")
    return sorted(access.values(), key=lambda e: (_source(e).lower(), str(e.get("name", "")).lower()))


def account_sources(role: dict) -> list[str]:
    """Ids of the sources whose account attributes the role's criteria read."""
    found = [str((leaf.get("key") or {}).get("sourceId") or "") for leaf in leaves(_criteria(role))
             if str((leaf.get("key") or {}).get("type", "")).upper() == "ACCOUNT"]
    return sorted({source for source in found if source})


# -- criteria -----------------------------------------------------------------------------


def _criteria(role: dict) -> dict | None:
    return (role.get("membership") or {}).get("criteria")


def compare(operation: str, value: str, expected: str) -> bool:
    """One value against the criterion's value, ignoring case; ordering is numeric when both are
    numbers."""
    value, expected = value.lower(), expected.lower()
    if operation == "EQUALS":
        return value == expected
    if operation == "CONTAINS":
        return expected in value
    if operation == "STARTS_WITH":
        return value.startswith(expected)
    if operation == "ENDS_WITH":
        return value.endswith(expected)
    if operation in ORDERINGS:
        try:
            return ORDERINGS[operation](float(value), float(expected))
        except ValueError:
            return ORDERINGS[operation](value, expected)
    return False


def values(key: dict, identity: dict, accounts: dict[str, dict[str, list[dict]]]) -> list[str]:
    """The identity's values for a criterion key: an identity attribute, the attribute of its
    accounts on a source, or the values of its entitlements of an attribute on a source."""
    kind, name = str(key.get("type") or "").upper(), key_attribute(key)
    if kind == "IDENTITY":
        found, value = lookup(identity.get("attributes"), name)
        return _texts(value if found else lookup(identity, name)[1])
    source = key.get("sourceId") or ""
    if kind == "ACCOUNT":
        return [text for attributes in accounts.get(source, {}).get(identity.get("id", ""), [])
                for text in _texts(lookup(attributes, name)[1])]
    if kind == "ENTITLEMENT":
        return [str(item.get("value") or item.get("name") or "") for item in identity.get("access") or []
                if item.get("type") == "ENTITLEMENT" and (item.get("source") or {}).get("id") == source
                and str(item.get("attribute") or "").lower() == name.lower()]
    return []


def _texts(value) -> list[str]:
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("id")
        if item is not None:
            out.append(str(item).lower() if isinstance(item, bool) else str(item))
    return out


def matches(node, identity: dict, accounts: dict[str, dict[str, list[dict]]]) -> bool:
    """Whether the identity meets the criteria (a role's ``membership.criteria``)."""
    if not isinstance(node, dict):
        return False
    operation = str(node.get("operation") or "").upper()
    children = node.get("children")
    if children:
        results = (matches(child, identity, accounts) for child in children)
        return any(results) if operation == "OR" else all(results)
    if not node.get("key"):
        return False
    positive = NEGATIONS.get(operation, operation)
    expected = str(node.get("stringValue") or "")
    found = any(compare(positive, value, expected) for value in values(node["key"], identity, accounts))
    return not found if operation in NEGATIONS else found


def unsupported(role: dict) -> list[str]:
    """Operations of the criteria this evaluation does not know (they never match)."""
    return sorted({str(leaf.get("operation") or "") for leaf in leaves(_criteria(role))
                   if str(leaf.get("operation") or "").upper() not in SUPPORTED})


def members(role: dict, identities: list[dict], accounts: dict[str, dict[str, list[dict]]]) -> set[str]:
    """Ids of the identities the role's membership selects."""
    membership = role.get("membership") or {}
    if str(membership.get("type") or "").upper() == "IDENTITY_LIST":
        return {str(ref.get("id")) for ref in membership.get("identities") or []}
    criteria = membership.get("criteria")
    if not criteria:
        return set()
    return {doc.get("id", "") for doc in identities if matches(criteria, doc, accounts)}


# -- analysis -----------------------------------------------------------------------------


@dataclass
class Result:
    data: RoleData
    identities: list[dict]                 # one row per identity concerned (IDENTITY_COLUMNS)
    provisioning: list[dict]               # one row per entitlement to provision to an identity
    entitlements: list[dict]               # one row per entitlement of the role
    matching: int = 0
    unsupported: list[str] = field(default_factory=list)

    @property
    def outcomes(self) -> Counter:
        return Counter(row["Outcome"] for row in self.identities)

    @property
    def new_accounts(self) -> int:
        return sum(row["Accounts to create"] for row in self.identities)

    @property
    def new_members(self) -> int:
        return self.outcomes[NEW_ACCESS] + self.outcomes[ROLE_ONLY]


def assignment(data: RoleData, identity: dict) -> str | None:
    """How the identity has the role today (ROLE_MEMBERSHIP, ACCESS_REQUEST, "" when unknown), or None."""
    role_id = data.role.get("id")
    if data.assigned is not None and identity.get("id") in data.assigned:
        return data.assigned[identity["id"]]
    held = role_id and any(item.get("type") == "ROLE" and item.get("id") == role_id
                           for item in identity.get("access") or [])
    return "" if held else None


def analyse(data: RoleData) -> Result:
    selected = members(data.role, data.identities, data.accounts)
    rows, provisioning = [], []
    held_by_members, held_by_others, to_add = Counter(), Counter(), Counter()
    for identity in data.identities:
        held = {item.get("id") for item in identity.get("access") or [] if item.get("type") == "ENTITLEMENT"}
        member = identity.get("id") in selected
        for entitlement in data.access:
            if entitlement.get("id") in held:
                (held_by_members if member else held_by_others)[entitlement.get("id")] += 1
        missing = [e for e in data.access if e.get("id") not in held]
        assigned = assignment(data, identity)
        if member:
            outcome = KEEPS if assigned is not None else NEW_ACCESS if missing else ROLE_ONLY
        elif assigned is not None:
            outcome = LOSES if assigned == BY_CRITERIA else OUTSIDE
        elif data.access and not missing:
            outcome = GAP
        else:
            continue
        accounts: dict[str, list[str]] = defaultdict(list)
        for account in identity.get("accounts") or []:
            name = account.get("accountId") or account.get("name") or ""
            accounts[(account.get("source") or {}).get("id", "")].append(
                f"{name} (disabled)" if account.get("disabled") else name)
        new_accounts = []
        if outcome == NEW_ACCESS:
            for entitlement in missing:
                source = entitlement.get("source") or {}
                existing = accounts.get(source.get("id", ""))
                if not existing and source.get("name", "") not in new_accounts:
                    new_accounts.append(source.get("name", ""))
                to_add[entitlement.get("id")] += 1
                provisioning.append({
                    "Identity": identity.get("name", ""), "Display name": identity.get("displayName", ""),
                    "Source": source.get("name", ""), "Entitlement": entitlement.get("name", ""),
                    "Attribute": entitlement.get("attribute", ""), "Value": entitlement.get("value", ""),
                    "Via": "\n".join(entitlement.get("via") or []),
                    "Account": "\n".join(existing) if existing else NEW_ACCOUNT,
                    "Privileged": "yes" if entitlement.get("privileged") else "no"})
        rows.append({
            "Identity": identity.get("name", ""), "Display name": identity.get("displayName", ""),
            "Lifecycle state": str((identity.get("attributes") or {}).get("cloudLifecycleState") or ""),
            "Outcome": outcome,
            "Assigned by": "" if assigned is None else ASSIGNED_BY.get(assigned, assigned),
            "Entitlements to add": len(missing) if outcome == NEW_ACCESS else 0,
            "Accounts to create": len(new_accounts),
            "Entitlements held": len(data.access) - len(missing),
            "To add": "\n".join(_label(e) for e in missing) if outcome == NEW_ACCESS else ""})
    rows.sort(key=lambda r: (OUTCOMES.index(r["Outcome"]), -r["Entitlements to add"], r["Identity"].lower()))
    provisioning.sort(key=lambda r: (r["Privileged"] != "yes", r["Source"].lower(), r["Entitlement"].lower(),
                                     r["Identity"].lower()))
    entitlements = [{
        "Source": _source(e), "Entitlement": e.get("name", ""), "Attribute": e.get("attribute", ""),
        "Value": e.get("value", ""), "Via": "\n".join(e.get("via") or []),
        "Privileged": "yes" if e.get("privileged") else "no",
        "To provision": to_add[e.get("id")], "Held by matching identities": held_by_members[e.get("id")],
        "Held by other identities": held_by_others[e.get("id")]} for e in data.access]
    return Result(data, rows, provisioning, entitlements, len(selected), unsupported(data.role))


def _source(entitlement: dict) -> str:
    return str((entitlement.get("source") or {}).get("name") or "")


def _label(entitlement: dict) -> str:
    return f"{_source(entitlement)}: {entitlement.get('name', '')}"


# -- report -------------------------------------------------------------------------------

READ_ME = [
    ("What is this?", "What enabling the role would do: the identities its membership criteria select, "
                      "the entitlements ISC would provision to them and the accounts it would create. "
                      "Nothing was changed in the tenant."),
    (NEW_ACCESS, "Matches the criteria, does not have the role, and lacks some of its entitlements: they are "
                 "provisioned (see Provisioning), with a new account where the identity has none on the "
                 "source."),
    (ROLE_ONLY, "Matches the criteria and already holds every entitlement of the role: the role is assigned, "
                "nothing is provisioned. Revoking the role later would remove that access."),
    (KEEPS, "Matches the criteria and already has the role: no change."),
    (LOSES, "Has the role through its criteria but no longer matches them: at the next identity refresh "
            "the role is removed, and its access unless something else grants it."),
    (OUTSIDE, "Has the role through an access request (or an assignment whose origin was not read), "
              "without matching the criteria: the role stays."),
    (GAP, "Holds every entitlement of the role without matching the criteria: the criteria may be too "
          "narrow, or this access should be reviewed."),
    ("How the criteria are evaluated", "Values are compared ignoring case; a multi-valued attribute matches "
                                       "when one of its values does; NOT_EQUALS and DOES_NOT_CONTAIN match "
                                       "when no value does. For a role already assigned by its criteria, few "
                                       f"'{LOSES}' and '{NEW_ACCESS}' rows confirm that this matches the "
                                       "tenant."),
    ("Limits", "Only the identities matching the search query under Role are analysed. The criteria of "
               "dimensions, and access granted by other roles or access profiles, are not taken into "
               "account."),
    ("Privacy", "The sheets list real people and their access: protect the file."),
]


def by_source(result: Result) -> list[dict]:
    counts: dict[str, Counter] = defaultdict(Counter)
    identities: dict[str, set] = defaultdict(set)
    for row in result.provisioning:
        first = row["Identity"] not in identities[row["Source"]]       # one new account per identity
        identities[row["Source"]].add(row["Identity"])
        counts[row["Source"]]["Entitlements to add"] += 1
        counts[row["Source"]]["Privileged"] += row["Privileged"] == "yes"
        counts[row["Source"]]["Accounts to create"] += first and row["Account"] == NEW_ACCOUNT
    rows = [{"Source": source, "Identities": len(identities[source]),
             "Entitlements to add": c["Entitlements to add"], "Accounts to create": c["Accounts to create"],
             "Privileged": c["Privileged"]} for source, c in counts.items()]
    return sorted(rows, key=lambda r: (-r["Entitlements to add"], r["Source"].lower()))


def role_rows(result: Result) -> list[dict]:
    data, role = result.data, result.data.role
    names = {str(s.get("id")): str(s.get("name") or "") for doc in data.identities
             for s in [*((a.get("source") or {}) for a in doc.get("accounts") or []),
                       *((i.get("source") or {}) for i in doc.get("access") or [])] if s.get("id")}
    membership = role.get("membership") or {}
    kind = str(membership.get("type") or "").upper()
    if kind == "IDENTITY_LIST":
        selection = f"identity list ({len(membership.get('identities') or [])} identities)"
    else:
        selection = (criteria_text(membership.get("criteria"), names)
                     or "none: nobody is assigned automatically")
    rows = [("Role", role.get("name", "")), ("Enabled", "yes" if role.get("enabled") else "no"),
            ("Membership", selection),
            ("Access profiles", _names(role.get("accessProfiles"))),
            ("Entitlements granted directly", _names(role.get("entitlements"))),
            ("Identities analysed", f"{len(data.identities)} (search query: {data.query})"),
            ("Current assignments", "read from the role" if data.assigned is not None else
             "from the identities' access")]
    if result.unsupported:
        rows.append(("Note", "Operations not evaluated (they never match): "
                             f"{', '.join(result.unsupported)}."))
    rows += [("Note", note) for note in data.notes]
    return [{"Setting": setting, "Value": value} for setting, value in rows]


def _names(refs) -> str:
    return "\n".join(str(ref.get("name") or ref.get("id") or "") for ref in refs or []) or "none"


def build_dashboard(result: Result, title: str = "") -> dashboard.Dashboard:
    data, outcomes = result.data, result.outcomes
    privileged = sum(r["Privileged"] == "yes" for r in result.provisioning)
    kpis = [
        ("Identities analysed", str(len(data.identities)), dashboard.BLUE),
        ("Match the criteria", str(result.matching), dashboard.BLUE),
        _tile("Would get the role", result.new_members, dashboard.AMBER),
        _tile("Entitlements to provision", len(result.provisioning), dashboard.AMBER),
        _tile("Accounts to create", result.new_accounts, dashboard.RED),
        _tile("Privileged entitlements to provision", privileged, dashboard.RED),
        _tile("Would lose the role", outcomes[LOSES], dashboard.RED),
        _tile("Have the access without the role", outcomes[GAP], dashboard.AMBER),
    ]
    charts = []
    labels = [o for o in OUTCOMES if outcomes[o]]
    if labels:
        charts.append(dashboard.Chart("Identities by outcome", "doughnut", labels,
                                      [("Identities", [outcomes[o] for o in labels], "")]))
    sources = by_source(result)[:12]
    if sources:
        charts.append(dashboard.Chart("Provisioning by source", "bar", [r["Source"] for r in sources],
                                      [("Entitlements to add", [r["Entitlements to add"] for r in sources],
                                        dashboard.BLUE),
                                       ("Accounts to create", [r["Accounts to create"] for r in sources],
                                        dashboard.RED)]))
    wide = {"Display name", "To add", "Via", "Account", "Value", "Entitlement"}
    fills = {"Outcome": OUTCOME_FILLS}
    tables = [dashboard.Table("Identities", result.identities, wide=wide, fills=fills,
                              red_positive={"Accounts to create"}),
              dashboard.Table("Provisioning", result.provisioning, wide=wide, red_yes={"Privileged"}),
              dashboard.Table("Entitlements", result.entitlements, wide=wide, red_yes={"Privileged"}),
              dashboard.Table("By source", by_source(result), wide=wide,
                              red_positive={"Accounts to create", "Privileged"}),
              dashboard.Table("Role", role_rows(result), wide={"Value"})]
    sections = [
        dashboard.Section("Provisioning by source", by_source(result),
                          ["Source", "Identities", "Entitlements to add", "Accounts to create",
                           "Privileged"]),
        dashboard.Section("Entitlements of the role", result.entitlements,
                          ["Source", "Entitlement", "Via", "To provision", "Held by matching identities",
                           "Held by other identities"]),
        dashboard.Section("Identities", result.identities,
                          ["Identity", "Lifecycle state", "Outcome", "Entitlements to add",
                           "Accounts to create"]),
    ]
    name = data.role.get("name", "")
    return dashboard.Dashboard(
        title or f"Role enablement impact: {name}", dashboard.subtitle(data.origin), kpis, charts, tables,
        sections, READ_ME, colours=OUTCOME_COLOURS, wide_pdf={"Identity", "Entitlement", "Via", "Source"},
        alert=lambda row: row.get("Outcome") == LOSES or bool(row.get("Accounts to create")))


def _tile(label: str, count: int, colour: str) -> tuple[str, str, str]:
    """A KPI tile, green when there is nothing to report."""
    return label, str(count), colour if count else dashboard.GREEN


def write_xlsx(path: str, result: Result, title: str = "") -> str:
    return dashboard.write_xlsx(build_dashboard(result, title), path)


def write_pdf(path: str, result: Result, title: str = "", max_rows: int = 500) -> str:
    return dashboard.write_pdf(build_dashboard(result, title), path, max_rows)


def write_csv(path: str, result: Result) -> str:
    rows = [[row[c] for c in IDENTITY_COLUMNS] for row in result.identities]
    return write_table(path, IDENTITY_COLUMNS, rows, ";")
