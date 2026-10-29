"""Document the identity attribute mappings of identity profiles: for each identity attribute, where
its value comes from (source and account attribute), which transforms it goes through, in plain
words, how the named transforms depend on each other, plus a summary with statistics and a list of
points to check.

Profiles come from the tenant or from exported JSON files (a profile, a list of profiles, or
a SailPoint configuration export); transform exports are read the same way and used to
follow ``reference`` transforms.
"""

from __future__ import annotations

import glob
import json
import logging
import os
from collections import Counter
from dataclasses import dataclass, field

from isc_reporter import attribute_usage, dashboard, transform_graph, transform_tree
from isc_reporter.api import error_message
from isc_reporter.errors import UsageError
from isc_reporter.transform_tree import describe_type

log = logging.getLogger("isc_reporter")

MAX_DEPTH = 8
WARNING, INFO = "Warning", "Info"

MAPPING_COLUMNS = ["identity_attribute", "display_name", "mapping", "explanation", "sources",
                   "source_attributes", "transforms", "logic", "used_in", "notes", "definition"]
HEADERS = {"identity_attribute": "Identity attribute", "display_name": "Display name",
           "mapping": "Mapping", "explanation": "Explanation", "sources": "Source(s)",
           "source_attributes": "Source attribute(s)", "transforms": "Transforms used",
           "logic": "Logic (short form)", "used_in": "Also used in",
           "notes": "Points to check", "definition": "Definition (JSON)"}
COLUMN_HELP = {
    "identity_attribute": "Technical name of the identity attribute.",
    "display_name": "Name shown in the ISC user interface.",
    "mapping": "How the value is obtained (see the mapping kinds below).",
    "explanation": "The mapping in plain words.",
    "sources": "Source(s) whose account data is read.",
    "source_attributes": "Account attribute(s) read, prefixed with the source when there are several.",
    "transforms": "Named transforms used, including transforms called by other transforms.",
    "logic": "Compact formula: Source.attribute, \"static value\", transform(input)...",
    "used_in": "Where else the value of this attribute is read: other mappings, named transforms and "
               "provisioning policies of the sources (see Attribute usage).",
    "notes": "Points to check (details in the Checks sheet).",
    "definition": "The mapping exactly as configured in ISC (JSON).",
}

# Mapping kind of the top-level transform type: (label, colour, meaning).
KINDS = {
    "accountAttribute": ("Direct copy", "E2F0D9", "The value of an account attribute is copied as is."),
    "reference": ("Named transform", "DDEBF7", "A named transform (shared, reusable) computes the value."),
    "inline": ("Inline transform", "E4DFEC", "Logic written directly in the profile (not reusable)."),
    "static": ("Static value", "FFF2CC", "A fixed value, or a Velocity expression."),
    "identityAttribute": ("Identity attribute", "EDEDED", "Copied from another attribute of the identity."),
    "rule": ("Rule", "FCE4D6", "A cloud rule (Java/BeanShell code deployed by SailPoint) computes it."),
    "": ("Not mapped", "F8CBAD", "No mapping is configured."),
}

# Display names of the standard attributes, used when reading exported files.
STANDARD_NAMES = {"uid": "Username", "email": "Email", "firstname": "First Name",
                  "lastname": "Last Name", "displayName": "Display Name", "manager": "Manager",
                  "cloudLifecycleState": "Lifecycle State", "phone": "Work Phone",
                  "employeeNumber": "Employee Number", "department": "Department",
                  "location": "Location", "startDate": "Start Date", "endDate": "End Date"}
REQUIRED_ATTRIBUTES = ("uid",)
COMMON_ATTRIBUTES = ("email", "firstname", "lastname", "displayName", "manager",
                     "cloudLifecycleState")


# -- loading ------------------------------------------------------------------


def load_files(paths: list[str]) -> tuple[list[dict], dict[str, dict]]:
    """Profiles and transforms (by name) from JSON files or folders of JSON files."""
    files = []
    for path in paths:
        if os.path.isdir(path):
            files += sorted(glob.glob(os.path.join(path, "**", "*.json"), recursive=True))
        elif os.path.isfile(path):
            files.append(path)
        else:
            raise UsageError(f"'{path}' does not exist")
    profiles, transforms = [], {}
    for path in files:
        try:
            with open(path, encoding="utf-8-sig") as fh:
                data = json.load(fh)
        except ValueError as exc:
            raise UsageError(f"'{path}' is not valid JSON ({exc})") from exc
        for obj in _objects(data):
            if "identityAttributeConfig" in obj:
                profiles.append(obj)
            elif obj.get("type") and "attributes" in obj and obj.get("name"):
                transforms[obj["name"]] = obj
    if not profiles:
        raise UsageError("no identity profile found in the given files")
    return profiles, transforms


def _objects(data):
    """Flatten a profile, a list, or a configuration export (``{"objects": [{"object": ...}]}``)."""
    if isinstance(data, list):
        for item in data:
            yield from _objects(item)
    elif isinstance(data, dict):
        if isinstance(data.get("objects"), list):
            for item in data["objects"]:
                yield from _objects(item.get("object", item) if isinstance(item, dict) else item)
        else:
            yield data


def iter_objects(paths: list[str]):
    """Every object of the JSON files at *paths* (files or folders); unreadable files are skipped."""
    for path in paths:
        files = (sorted(glob.glob(os.path.join(path, "**", "*.json"), recursive=True))
                 if os.path.isdir(path) else [path])
        for file in files:
            try:
                with open(file, encoding="utf-8-sig") as fh:
                    data = json.load(fh)
            except (OSError, ValueError):
                continue
            yield from (obj for obj in _objects(data) if isinstance(obj, dict))


def load_policies(paths: list[str]) -> list[dict]:
    """Provisioning policies of the sources found in exported files (a source export lists its
    ``provisioningPolicies``)."""
    return [{**policy, "source": obj.get("name", "")}
            for obj in iter_objects(paths) for policy in obj.get("provisioningPolicies") or []]


def fetch_policies(isc) -> list[dict]:
    """Provisioning policies of every source of the tenant (``source`` is the source name)."""
    from isc_reporter.api import list_all, model_json

    policies = []
    for source in list_all(isc.sources.list_sources_v1):
        try:
            found = list_all(isc.sources.list_provisioning_policies_v1, source_id=source.id)
        except Exception as exc:   # one unreadable source must not stop the report
            log.warning("Provisioning policies of %s not read: %s", source.name, error_message(exc))
            continue
        policies += [{**model_json(policy), "source": source.name} for policy in found]
    return policies


def fetch(isc) -> tuple[list[dict], dict[str, dict], dict[str, str]]:
    """Profiles, transforms by name and identity attribute display names from the tenant."""
    from isc_reporter.api import call, list_all, model_json

    profiles = [model_json(p) for p in list_all(isc.identity_profiles.list_identity_profiles_v1)]
    transforms = {t["name"]: t for t in
                  (model_json(t) for t in list_all(isc.transforms.list_transforms_v1))}
    attributes = call(isc.identity_attributes.list_identity_attributes_v1, include_system=True) or []
    names = {a.name: a.display_name or "" for a in attributes}
    return profiles, transforms, names


# -- analysis -----------------------------------------------------------------


@dataclass
class _Walk:
    transforms: dict[str, dict]
    leaves: list[tuple[str, str]] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)

    def text(self, node, depth: int = 0) -> str:
        """Short readable form of a transform definition, recording what it reads."""
        if isinstance(node, list):
            return ", ".join(self.text(item, depth) for item in node)
        if not isinstance(node, dict) or "type" not in node:
            return json.dumps(node, ensure_ascii=False) if not isinstance(node, str) else f'"{node}"'
        kind, attrs = node["type"], node.get("attributes") or {}
        if depth > MAX_DEPTH:
            return f"{kind}(…)"
        if kind == "accountAttribute":
            source = attrs.get("sourceName") or attrs.get("applicationName") or attrs.get("sourceId", "?")
            attribute = attrs.get("attributeName", "?")
            if (source, attribute) not in self.leaves:
                self.leaves.append((source, attribute))
            return f"{source}.{attribute}"
        if kind == "identityAttribute":
            return f"identity.{attrs.get('name', '?')}"
        if kind == "static":
            return f'"{attrs.get("value", "")}"'
        if kind == "rule":
            return f"rule '{attrs.get('name', '?')}'"
        if kind == "reference":
            name = attrs.get("id", "?")
            if name not in self.used:
                self.used.append(name)
            inner = self.text(attrs["input"], depth + 1) if "input" in attrs else ""
            definition = self.transforms.get(name)
            if definition is not None:
                # Follow the referenced transform to find the account attributes it reads itself.
                self.text(definition, depth + 1)
            elif self.transforms and name not in self.missing:
                self.missing.append(name)
            return f"{name}({inner})"
        args = []
        for key, value in attrs.items():
            if (isinstance(value, dict) and "type" in value) or \
                    (isinstance(value, list) and any(isinstance(v, dict) for v in value)):
                args.append(self.text(value, depth + 1) if key in ("input", "values")
                            else f"{key}={self.text(value, depth + 1)}")
            elif key != "requiresPeriodicRefresh":
                args.append(f"{key}={self.text(value, depth + 1)}")
        return f"{kind}({', '.join(args)})"


def kind_of(definition: dict) -> str:
    kind = (definition or {}).get("type", "")
    return kind if kind in KINDS else "inline"


def explain(definition: dict, walk: _Walk) -> str:
    """The mapping in one plain sentence."""
    if not definition:
        return "Not mapped."
    kind, attrs = definition.get("type", ""), definition.get("attributes") or {}
    reads = ", ".join(f"'{attribute}' from {source}" for source, attribute in walk.leaves)
    if kind == "accountAttribute" and walk.leaves:
        source, attribute = walk.leaves[0]
        return f"Copied from the account attribute '{attribute}' of the source {source}."
    if kind == "identityAttribute":
        return f"Copied from the identity attribute '{attrs.get('name', '?')}'."
    if kind == "static":
        value = str(attrs.get("value", ""))
        if "$" in value or "#" in value:
            return f"Computed with a Velocity expression: {value}"
        return f'Always "{value}".'
    if kind == "rule":
        return f"Computed by the rule '{attrs.get('name', '?')}'."
    if kind == "reference":
        name = attrs.get("id", "?")
        known = walk.transforms.get(name)
        what = f" ({describe_type(known.get('type', ''))})" if known else ""
        if "input" in attrs:
            return f"Value of {reads or 'its input'}, converted by the transform '{name}'{what}."
        return f"Computed by the transform '{name}'{what}" + (f", which reads {reads}" if reads else "") + "."
    text = describe_type(kind)
    return text[0].upper() + text[1:] + (f", using {reads}" if reads else "") + "."


def mapping_rows(profile: dict, transforms: dict[str, dict] | None = None,
                 display_names: dict[str, str] | None = None) -> list[dict]:
    """One row per identity attribute mapped in *profile*, with its points to check."""
    transforms, display_names = transforms or {}, display_names or {}
    authoritative = (profile.get("authoritativeSource") or {}).get("name", "")
    rows = []
    config = profile.get("identityAttributeConfig") or {}
    for mapping in config.get("attributeTransforms") or []:
        name = mapping.get("identityAttributeName", "")
        definition = mapping.get("transformDefinition") or {}
        walk = _Walk(transforms)
        logic = walk.text(definition) if definition else ""
        sources = list(dict.fromkeys(source for source, _ in walk.leaves))
        kind = kind_of(definition) if definition else ""
        checks = []
        if not definition:
            checks.append((WARNING, "Not mapped", "The attribute has no mapping."))
        for missing in walk.missing:
            checks.append((WARNING, "Missing transform",
                           f"The transform '{missing}' does not exist in the tenant/export."))
        other = [s for s in sources if authoritative and s != authoritative]
        if other:
            checks.append((INFO, "Other source", f"Reads {', '.join(other)}, not the authoritative "
                                                 f"source {authoritative}."))
        if name == "cloudLifecycleState" and kind == "accountAttribute":
            source, attribute = walk.leaves[0] if walk.leaves else ("?", "?")
            checks.append((WARNING, "Lifecycle state copied as is",
                           f"Copied from {source}.{attribute}: every value must exactly match a "
                           "lifecycle state technical name of this profile. It is usually "
                           "computed by a transform."))
        if kind == "static":
            checks.append((INFO, "Static value", "Every identity of the profile gets the same value."))
        explanation = explain(definition, walk)
        tree = transform_tree.Node(name, explanation, kind=transform_tree.ATTRIBUTE)
        if definition:
            tree.children.append(transform_tree.build(definition, transforms))
        rows.append({
            "identity_attribute": name,
            "display_name": display_names.get(name) or STANDARD_NAMES.get(name, ""),
            "kind": kind,
            "mapping": KINDS[kind][0],
            "explanation": explanation,
            "sources": "\n".join(sources),
            "source_attributes": "\n".join(attribute if len(sources) == 1 else f"{source}.{attribute}"
                                           for source, attribute in walk.leaves),
            "transforms": "\n".join(walk.used),
            "logic": logic,
            "leaves": list(walk.leaves),
            "checks": checks,
            "tree": tree,
            "definition": json.dumps(definition, ensure_ascii=False, indent=1),
        })
    # Attributes filled exactly the same way are often a copy-paste mistake.
    same: dict[str, list[str]] = {}
    for row in rows:
        if row["logic"]:
            same.setdefault(row["logic"], []).append(row["identity_attribute"])
    for row in rows:
        twins = [a for a in same.get(row["logic"], []) if a != row["identity_attribute"]]
        if twins:
            row["checks"].append((INFO, "Same mapping", f"Filled exactly like: {', '.join(twins)}."))
        row["notes"] = "\n".join(f"{check}" for _, check, _ in row["checks"])
    return rows


def profile_checks(profile: dict, rows: list[dict]) -> list[tuple[str, str, str, str]]:
    """``(severity, attribute, check, details)`` for the profile and its attributes."""
    mapped = {row["identity_attribute"] for row in rows if row["kind"]}
    checks = [(WARNING, a, "Required attribute not mapped",
               f"'{a}' ({STANDARD_NAMES.get(a, a)}) must be mapped.")
              for a in REQUIRED_ATTRIBUTES if a not in mapped]
    checks += [(INFO, a, "Common attribute not mapped",
                f"'{a}' ({STANDARD_NAMES.get(a, a)}) is usually mapped.")
               for a in COMMON_ATTRIBUTES if a not in mapped]
    if profile.get("identityRefreshRequired"):
        checks.append((INFO, "", "Identity refresh pending",
                       "The profile changed since the last identity refresh; identities do not "
                       "reflect the current mappings yet."))
    if profile.get("identityCount") == 0:
        checks.append((INFO, "", "No identities", "No identity uses this profile."))
    for row in rows:
        checks += [(severity, row["identity_attribute"], check, details)
                   for severity, check, details in row["checks"]]
    return checks


# -- rendering ------------------------------------------------------------------

RESERVED_SHEETS = {"Dashboard", "Profiles", "Matrix", "Coverage", "Attribute usage", "Usage detail",
                   "Transforms", "Transform dependencies", "Checks", "Chart data", "Read me"}
SEVERITY_FILLS = {WARNING: "F8CBAD", INFO: "DDEBF7"}
# KINDS colours are pale cell backgrounds; charts use strong ones.
CHART_COLOURS = {"Direct copy": dashboard.GREEN, "Named transform": dashboard.BLUE,
                 "Inline transform": "8E24AA", "Static value": dashboard.AMBER,
                 "Identity attribute": "546E7A",
                 "Rule": "6D4C41", "Not mapped": dashboard.RED}
READ_ME = [
    ("What is this?", "How every identity attribute is computed, for each identity profile: which "
                      "source and account attribute it comes from, and which transforms change it."),
    ("Dashboard", "Key figures and charts: mapping kinds, attributes per profile, most used "
                  "transforms and source attributes."),
    ("Profiles", "One line per identity profile with its statistics."),
    ("One sheet per profile", "One line per identity attribute. Start with the Explanation column."),
    ("Matrix", "Identity attributes (lines) x profiles (columns): how each profile fills each attribute."),
    ("Coverage", "Attributes that are not mapped in every profile."),
    ("Attribute usage", "One line per identity attribute: the profiles that map it and every mapping, "
                        "named transform and source provisioning policy that reads it."),
    ("Usage detail", "The same, one line per use: where, which object, which field, how."),
    ("Transforms", "Every named transform: what it does, where it is used, the named transforms it calls "
                   "and that call it, what it depends on (as a tree) and its definition."),
    ("Transform dependencies", "How the named transforms depend on each other, as one tree per identity "
                               "attribute that uses one, then the named transforms that nothing uses. Below "
                               "each line, indented, what it depends on: 'uses' (the attribute's mapping "
                               "calls the transform), 'calls' (a transform calls another one) and 'reads' (a "
                               "transform reads an identity attribute, itself computed by other "
                               "transforms). Use the + and - buttons on the left to fold a tree."),
    ("Checks", "Points worth a look: Warning = probably wrong, Info = good to know."),
    *[(f"Column: {HEADERS[c]}", COLUMN_HELP[c]) for c in MAPPING_COLUMNS],
    *[(f"Mapping kind: {label}", meaning) for label, _, meaning in KINDS.values()],
    ("Identity profile", "Defines how identities coming from one source are built. Identities belong "
                         "to the profile of their authoritative source."),
    ("Authoritative source", "The source (usually HR) whose accounts create the identities of the profile."),
    ("Priority", "When several profiles could apply, the lowest number wins."),
    ("Transform", "A small rule that turns input values into the attribute value (change a date format, "
                  "pick the first non-empty value, look up a table...)."),
    ("Named transform", "A transform saved under a name and reused by reference; changing it changes "
                        "every profile that uses it."),
    ("Lifecycle state", "cloudLifecycleState drives joiner/mover/leaver automation; its values must match "
                        "the lifecycle states defined on the profile."),
]
WIDE = {"Explanation", "Logic (short form)", "Definition (JSON)", "Points to check", "Description",
        "Details", "Used by (profile: attribute)", "Not mapped in", "Also used in",
        "Read by profile mappings", "Read by transforms", "Used in provisioning policies", "Detail",
        "What it does", "Calls", "Called by"}


def analyse(profiles: list[dict], transforms: dict[str, dict] | None = None,
            display_names: dict[str, str] | None = None, policies: list[dict] | None = None):
    """``(profiles by priority, their mapping rows, every usage of an identity attribute)``; each
    row also says where else its attribute is used (``used_in``)."""
    transforms = transforms or {}
    profiles = sorted(profiles, key=lambda p: (p.get("priority") or 0, p.get("name", "")))
    all_rows = [mapping_rows(p, transforms, display_names) for p in profiles]
    known = {r["identity_attribute"] for rows in all_rows for r in rows} | set(display_names or {})
    usages = attribute_usage.collect(profiles, all_rows, transforms, policies or [], known)
    elsewhere = attribute_usage.used_in(usages)
    for rows in all_rows:
        for row in rows:
            row["used_in"] = elsewhere.get(row["identity_attribute"], "")
    return profiles, all_rows, usages


def build_dashboard(profiles: list[dict], transforms: dict[str, dict] | None = None,
                    display_names: dict[str, str] | None = None, origin: str = "",
                    title: str = "Identity attribute mappings",
                    policies: list[dict] | None = None) -> dashboard.Dashboard:
    transforms = transforms or {}
    profiles, all_rows, usages = analyse(profiles, transforms, display_names, policies)
    all_checks = [profile_checks(p, rows) for p, rows in zip(profiles, all_rows, strict=True)]
    names = [p.get("name", "") for p in profiles]
    kind_colours = {label: colour for label, colour, _ in KINDS.values()}
    profile_rows = _profile_rows(profiles, all_rows, all_checks)
    check_rows = _check_rows(profiles, all_checks)

    tables = [dashboard.Table("Profiles", profile_rows, wide=WIDE, red_positive={"Warnings"},
                              red_yes={"Refresh pending"})]
    used_sheets = set(RESERVED_SHEETS)
    for profile, rows in zip(profiles, all_rows, strict=True):
        tables.append(dashboard.Table(_sheet_name(profile.get("name", "profile"), used_sheets),
                                      [{HEADERS[c]: r.get(c, "") for c in MAPPING_COLUMNS} for r in rows],
                                      wide=WIDE, fills={"Mapping": kind_colours}))
    tables.append(dashboard.Table("Matrix", _matrix_rows(names, all_rows), wide=WIDE | set(names)))
    if len(profiles) > 1:
        tables.append(dashboard.Table("Coverage", _coverage_rows(names, all_rows), wide=WIDE))
    shown = display_names or {}
    tables.append(dashboard.Table("Attribute usage", attribute_usage.summary_rows(
        usages, names, lambda a: shown.get(a) or STANDARD_NAMES.get(a, "")), wide=WIDE))
    tables.append(dashboard.Table("Usage detail", attribute_usage.detail_rows(usages), wide=WIDE))
    graph = transform_graph.build(profiles, all_rows, transforms, set(display_names or {}) or None)
    tables.append(dashboard.Table("Transforms", _transform_rows(profiles, all_rows, transforms, graph),
                                  wide=WIDE, mono={"Depends on"}))
    tables.append(dashboard.Table("Transform dependencies", transform_graph.rows(graph, profiles, all_rows),
                                  wide=WIDE | {"Used by", "What it is"}, mono={"Tree"}, outline="Level"))
    tables.append(dashboard.Table("Checks", check_rows, wide=WIDE, fills={"Severity": SEVERITY_FILLS}))

    sections = [
        dashboard.Section("Profiles", profile_rows, [
            "Profile", "Priority", "Authoritative source", "Identities", "Attributes", "Direct copy",
            "Named transform", "Other", "Warnings", "Info"]),
        dashboard.Section("Points to check", check_rows,
                          ["Severity", "Profile", "Identity attribute", "Check", "Details"]),
    ]
    for profile, rows in zip(profiles, all_rows, strict=True):
        sections.append(dashboard.Section(f"Profile {profile.get('name', '')}", [
            {"Identity attribute": r["identity_attribute"], "Mapping": r["mapping"],
             "Explanation": r["explanation"], "Points to check": r["notes"]} for r in rows],
            ["Identity attribute", "Mapping", "Explanation", "Points to check"]))
    dependencies = transform_graph.rows(graph, profiles, all_rows, transform_tree.PLAIN)
    if dependencies:
        sections.append(dashboard.Section("Transform dependencies", [
            {**row, "Used by": "" if row["Level"] else row["Used by"]} for row in dependencies],
            ["Used by", "Tree", "What it is"]))
    return dashboard.Dashboard(
        title, dashboard.subtitle(origin), _kpis(profiles, all_rows, all_checks, transforms),
        _charts(names, all_rows), tables, sections, READ_ME,
        colours={**CHART_COLOURS, WARNING: dashboard.RED, INFO: dashboard.BLUE},
        wide_pdf={"Profile", "Explanation", "Details", "Points to check", "Authoritative source"},
        alert=lambda row: row.get("Severity") == WARNING)


def _kpis(profiles, all_rows, all_checks, transforms) -> list[tuple[str, str, str]]:
    every = [row for rows in all_rows for row in rows]
    severities = Counter(severity for checks in all_checks for severity, *_ in checks)
    used = {name for row in every for name in filter(None, row["transforms"].split("\n"))}
    missing = [name for name in used if transforms and name not in transforms]
    attributes = len({r["identity_attribute"] for r in every})
    return [
        ("Identity profiles", str(len(profiles)), dashboard.BLUE),
        ("Identities", str(sum(p.get("identityCount") or 0 for p in profiles)), dashboard.BLUE),
        ("Attribute mappings", f"{len(every)}  ({attributes} attributes)", dashboard.BLUE),
        ("Named transforms used", f"{len(used)}" + (f"  ({len(missing)} missing)" if missing else ""),
         dashboard.RED if missing else dashboard.BLUE),
        ("Warnings", str(severities[WARNING]), dashboard.RED if severities[WARNING] else dashboard.GREEN),
        ("Information", str(severities[INFO]), dashboard.GREY),
    ]


def _charts(names, all_rows) -> list[dashboard.Chart]:
    every = [row for rows in all_rows for row in rows]
    charts = []
    kinds = Counter(r["mapping"] for r in every)
    labels = [label for label, _, _ in KINDS.values() if kinds[label]]
    if labels:
        charts.append(dashboard.Chart("Mapping kinds", "doughnut", labels,
                                      [("Attributes", [kinds[x] for x in labels], "")]))

    def count(rows, kind):
        return sum(1 for r in rows if r["kind"] == kind)

    if names:
        charts.append(dashboard.Chart("Attributes per profile", "stacked", names, [
            ("Direct copy", [count(rows, "accountAttribute") for rows in all_rows], dashboard.GREEN),
            ("Named transform", [count(rows, "reference") for rows in all_rows], dashboard.BLUE),
            ("Other", [len(rows) - count(rows, "accountAttribute") - count(rows, "reference")
                       for rows in all_rows], "8E24AA")]))
    used = Counter(name for row in every for name in filter(None, row["transforms"].split("\n")))
    if used:
        top = used.most_common(10)
        charts.append(dashboard.Chart("Most used named transforms", "bar", [n for n, _ in top],
                                      [("Uses", [c for _, c in top], dashboard.BLUE)]))
    leaves = Counter(f"{s}.{a}" for r in every for s, a in r["leaves"]).most_common(15)
    if leaves:
        charts.append(dashboard.Chart("Most read source attributes", "bar", [n for n, _ in leaves],
                                      [("Identity attributes fed", [c for _, c in leaves], "00897B")]))
    return charts


def _profile_rows(profiles, all_rows, all_checks) -> list[dict]:
    rows_out = []
    for profile, rows, checks in zip(profiles, all_rows, all_checks, strict=True):
        count = Counter(r["kind"] for r in rows)
        found = Counter(severity for severity, *_ in checks)
        rows_out.append({
            "Profile": profile.get("name", ""), "Priority": profile.get("priority", ""),
            "Authoritative source": (profile.get("authoritativeSource") or {}).get("name", ""),
            "Owner": (profile.get("owner") or {}).get("name", ""),
            "Identities": profile.get("identityCount", ""), "Attributes": len(rows),
            "Direct copy": count["accountAttribute"], "Named transform": count["reference"],
            "Other": len(rows) - count["accountAttribute"] - count["reference"],
            "Warnings": found[WARNING], "Info": found[INFO],
            "Refresh pending": "yes" if profile.get("identityRefreshRequired") else "no",
            "Description": profile.get("description") or ""})
    return rows_out


def _matrix_rows(names, all_rows) -> list[dict]:
    by_attribute = [{r["identity_attribute"]: r for r in rows} for rows in all_rows]
    attributes = sorted({a for rows in by_attribute for a in rows}, key=str.lower)
    columns = list(zip(names, by_attribute, strict=True))
    return [{"Identity attribute": a, **{n: rows[a]["logic"] if a in rows else "" for n, rows in columns}}
            for a in attributes]


def _coverage_rows(names, all_rows) -> list[dict]:
    mapped = [{r["identity_attribute"] for r in rows if r["kind"]} for rows in all_rows]
    attributes = sorted(set().union(*mapped), key=str.lower)
    return [{"Identity attribute": a, "Profiles": f"{sum(a in m for m in mapped)} / {len(names)}",
             "Not mapped in": ", ".join(n for n, m in zip(names, mapped, strict=True) if a not in m)}
            for a in attributes if not all(a in m for m in mapped)]


def _transform_rows(profiles, all_rows, transforms, graph) -> list[dict]:
    called_by: dict[str, list[str]] = {}
    for caller, definition in transforms.items():
        for name in transform_tree.calls(definition):
            called_by.setdefault(name, []).append(caller)
    usage: dict[str, list[str]] = {}
    for profile, rows in zip(profiles, all_rows, strict=True):
        for row in rows:
            for name in filter(None, row["transforms"].split("\n")):
                usage.setdefault(name, []).append(f"{profile.get('name', '')}: {row['identity_attribute']}")
    rows_out = []
    for name in transforms:
        usage.setdefault(name, [])
    for name, users in sorted(usage.items(), key=lambda kv: kv[0].lower()):
        definition = transforms.get(name)
        kind = (definition or {}).get("type", "")
        rows_out.append({
            "Transform": name, "Type": kind or ("not found" if transforms else "unknown"),
            "What it does": describe_type(kind) if kind else "", "Uses": len(users),
            "Used by (profile: attribute)": "\n".join(users),
            "Calls": "\n".join(transform_tree.calls(definition)) if definition else "",
            "Called by": "\n".join(sorted(called_by.get(name, []), key=str.lower)),
            "Depends on": transform_tree.draw(
                transform_graph.tree(graph, transform_graph.transform_id(name))),
            "Definition (JSON)": json.dumps(definition.get("attributes", ""), ensure_ascii=False, indent=1)
            if definition else ""})
    return rows_out


def _check_rows(profiles, all_checks) -> list[dict]:
    rows = [{"Severity": severity, "Profile": profile.get("name", ""), "Identity attribute": attribute,
             "Check": check, "Details": details}
            for profile, checks in zip(profiles, all_checks, strict=True)
            for severity, attribute, check, details in checks]
    return sorted(rows, key=lambda r: (r["Severity"] != WARNING, r["Profile"].lower(),
                                       r["Identity attribute"].lower()))


def write_xlsx(path: str, profiles: list[dict], transforms: dict[str, dict] | None = None,
               display_names: dict[str, str] | None = None, origin: str = "",
               policies: list[dict] | None = None) -> int:
    """Write the mapping workbook; returns the number of profiles documented."""
    dashboard.write_xlsx(build_dashboard(profiles, transforms, display_names, origin,
                                         policies=policies), path)
    return len(profiles)


def write_pdf(path: str, profiles: list[dict], transforms: dict[str, dict] | None = None,
              display_names: dict[str, str] | None = None, origin: str = "",
              policies: list[dict] | None = None) -> int:
    dashboard.write_pdf(build_dashboard(profiles, transforms, display_names, origin,
                                        policies=policies), path)
    return len(profiles)


def _sheet_name(name: str, used: set[str]) -> str:
    """Excel sheet names: 31 characters at most, no ``[]:*?/\\``, unique (case-insensitive)."""
    clean = "".join("_" if c in '[]:*?/\\' else c for c in name).strip("'") or "Profile"
    candidate, counter = clean[:31], 2
    while candidate.lower() in {u.lower() for u in used}:
        tag = f" ({counter})"
        candidate = clean[:31 - len(tag)] + tag
        counter += 1
    used.add(candidate)
    return candidate
