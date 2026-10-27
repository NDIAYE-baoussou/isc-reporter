"""Impact analysis for replacing a source (typically the HR source) by a new one, limited to the
identity layer: identity profiles, their attribute mappings, and transforms.

It finds what depends on the old source, matches each old account attribute to one of the new
source, shows every mapping before and after, and writes draft JSON for the new identity
profiles and transforms. Nothing is changed in the tenant.
"""

from __future__ import annotations

import copy
import json
import os
import re
from collections import Counter
from dataclasses import dataclass, field

from isc_reporter import dashboard
from isc_reporter.csvio import Column, read_rows, write_table
from isc_reporter.identity_profiles import INFO, WARNING, _Walk

TODO = "TODO_"
NEW_SOURCE_ID = "<new source id>"
# Profile fields that belong to the existing object and must not be copied into a new one.
PROFILE_RUNTIME_FIELDS = ("id", "created", "modified", "identityCount", "identityRefreshRequired",
                          "hasTimeBasedAttr", "identityExceptionReportReference")
TRANSFORM_RUNTIME_FIELDS = ("id", "internal", "created", "modified")

MAPPING_SCHEMA = {
    "old_attribute": Column(("old", "ancien_attribut", "source_attribute")),
    "new_attribute": Column(("new", "nouvel_attribut", "target_attribute")),
}


@dataclass
class Source:
    name: str
    id: str = ""
    schema: list[str] | None = None   # account attribute names, when known

    def matches(self, attrs: dict) -> bool:
        names = {str(attrs.get(k, "")).lower() for k in ("sourceName", "applicationName")}
        return self.name.lower() in names or bool(self.id and attrs.get("sourceId") == self.id)

    def has(self, attribute: str) -> bool | None:
        if self.schema is None:
            return None
        return attribute.lower() in {a.lower() for a in self.schema}


@dataclass
class Plan:
    old: Source
    new: Source
    in_place: bool
    suffix: str
    profiles: list[dict] = field(default_factory=list)        # impacted, with "action"
    transforms: dict[str, dict] = field(default_factory=dict)  # impacted name -> info
    attributes: dict[str, dict] = field(default_factory=dict)  # old attribute -> info
    mappings: list[dict] = field(default_factory=list)          # before/after per profile attribute
    checks: list[tuple] = field(default_factory=list)          # (severity, object, check, details)
    drafts: dict[str, dict] = field(default_factory=dict)       # relative path -> JSON


# -- walking definitions --------------------------------------------------------


def _nodes(node):
    """Every transform node in a definition (not following references)."""
    if isinstance(node, list):
        for item in node:
            yield from _nodes(item)
    elif isinstance(node, dict):
        if "type" in node:
            yield node
        for value in node.values():
            if isinstance(value, (dict, list)):
                yield from _nodes(value)


def old_attributes(node, source: Source) -> list[str]:
    found = []
    for n in _nodes(node):
        attrs = n.get("attributes") or {}
        if n.get("type") == "accountAttribute" and source.matches(attrs):
            name = attrs.get("attributeName", "")
            if name and name not in found:
                found.append(name)
    return found


def references(node) -> list[str]:
    return list(dict.fromkeys((n.get("attributes") or {}).get("id", "") for n in _nodes(node)
                              if n.get("type") == "reference"))


def rules(node) -> list[str]:
    return [(n.get("attributes") or {}).get("name", "?") for n in _nodes(node) if n.get("type") == "rule"]


def rewrite(node, plan: Plan, renames: dict[str, str]):
    """Copy of *node* reading the new source, with mapped attribute names and renamed references."""
    node = copy.deepcopy(node)
    for n in _nodes(node):
        attrs = n.get("attributes")
        if not isinstance(attrs, dict):
            continue
        if n.get("type") == "accountAttribute" and plan.old.matches(attrs):
            old = attrs.get("attributeName", "")
            new = plan.attributes.get(old, {}).get("new_attribute") or f"{TODO}{old}"
            attrs["attributeName"] = new
            if "sourceName" in attrs or "applicationName" not in attrs:
                attrs["sourceName"] = plan.new.name
            if "applicationName" in attrs:
                attrs["applicationName"] = plan.new.name
            attrs["sourceId"] = plan.new.id or NEW_SOURCE_ID
        elif n.get("type") == "reference" and attrs.get("id") in renames:
            attrs["id"] = renames[attrs["id"]]
    return node


# -- analysis -------------------------------------------------------------------


def read_attribute_map(path: str | None) -> dict[str, str]:
    """``old_attribute;new_attribute`` CSV as a case-insensitive dict."""
    if not path:
        return {}
    rows = read_rows(path, MAPPING_SCHEMA).rows
    return {row["old_attribute"].lower(): row.get("new_attribute", "")
            for row in rows if row.get("old_attribute")}


def analyse(profiles: list[dict], transforms: dict[str, dict], old: Source, new: Source,
            attribute_map: dict[str, str] | None = None, in_place: bool = False,
            suffix: str | None = None) -> Plan:
    attribute_map = attribute_map or {}
    plan = Plan(old, new, in_place, suffix if suffix is not None else f" - {new.name}")
    if old.name.lower() == new.name.lower():
        plan.checks.append((WARNING, "", "Same source", "The old and new source are the same."))

    # Transforms: direct readers of the old source, then whatever calls them, level by level.
    for name, definition in transforms.items():
        attributes = old_attributes(definition, old)
        if attributes:
            plan.transforms[name] = {"impact": "reads the old source", "attributes": attributes,
                                     "calls": [], "used_by": []}
    changed = True
    while changed:
        changed = False
        for name, definition in transforms.items():
            calls = [r for r in references(definition) if r in plan.transforms and r != name]
            if calls and name not in plan.transforms:
                plan.transforms[name] = {"impact": "calls an impacted transform", "attributes": [],
                                         "calls": calls, "used_by": []}
                changed = True
    renames = {} if in_place else {name: f"{name}{plan.suffix}" for name in plan.transforms}

    # Profiles: authoritative on the old source (new profile needed) or reading it (edit mappings).
    for profile in profiles:
        authoritative = profile.get("authoritativeSource") or {}
        is_authoritative = old.matches({"sourceName": authoritative.get("name"),
                                        "sourceId": authoritative.get("id")})
        if is_authoritative and not old.id:
            old.id = authoritative.get("id", "")
        mappings = (profile.get("identityAttributeConfig") or {}).get("attributeTransforms") or []
        touched = []
        for mapping in mappings:
            definition = mapping.get("transformDefinition") or {}
            direct = old_attributes(definition, old)
            via = [r for r in references(definition) if r in plan.transforms]
            for r in via:
                plan.transforms[r]["used_by"].append(f"{profile.get('name', '')}: "
                                                     f"{mapping.get('identityAttributeName', '')}")
            for attribute in direct:
                _use(plan, attribute,
                     f"{profile.get('name', '')}: {mapping.get('identityAttributeName', '')}")
            if direct or via:
                touched.append(mapping.get("identityAttributeName", ""))
            for rule in rules(definition):
                plan.checks.append((WARNING, profile.get("name", ""), "Rule to review",
                                    f"'{mapping.get('identityAttributeName', '')}' uses the rule "
                                    f"'{rule}', which may read the old source in its code."))
        if not is_authoritative and not touched:
            continue
        action = ("Create a new identity profile for the new source" if is_authoritative
                  else "Edit the mappings that read the old source")
        plan.profiles.append({"profile": profile, "authoritative": is_authoritative,
                              "action": action, "touched": touched})
    for name, info in plan.transforms.items():
        for attribute in info["attributes"]:
            _use(plan, attribute, f"transform: {name}")

    # Old -> new attribute names.
    for attribute, info in plan.attributes.items():
        given = attribute_map.get(attribute.lower())
        if given is not None and given:
            info["new_attribute"], info["status"] = given, "from the mapping file"
            if new.has(given) is False:
                info["status"] = "from the mapping file, NOT in the new schema"
        elif new.has(attribute):
            info["new_attribute"] = _schema_name(new, attribute)
            info["status"] = "same name in the new schema"
        elif new.schema is None and given is None:
            info["new_attribute"], info["status"] = attribute, "same name assumed (new schema unknown)"
        else:
            info["new_attribute"], info["status"] = "", "TO DEFINE"
        if old.has(attribute) is False:
            info["status"] += "; not in the old schema"

    # Before / after for every mapping of the impacted profiles, and the drafts.
    transform_defs = {**transforms, **{renames.get(n, n): rewrite(transforms[n], plan, renames)
                                       for n in plan.transforms}}
    for item in plan.profiles:
        profile = item["profile"]
        draft = copy.deepcopy(profile)
        for mapping in (draft.get("identityAttributeConfig") or {}).get("attributeTransforms") or []:
            definition = mapping.get("transformDefinition") or {}
            before = _Walk(transforms).text(definition) if definition else ""
            if mapping.get("identityAttributeName", "") in item["touched"]:
                mapping["transformDefinition"] = rewrite(definition, plan, renames)
            after = _Walk(transform_defs).text(mapping.get("transformDefinition") or {}) if definition else ""
            plan.mappings.append({
                "profile": profile.get("name", ""),
                "identity_attribute": mapping.get("identityAttributeName", ""),
                "before": before, "after": after,
                "change": "to complete" if TODO in after else "changed" if after != before else "unchanged"})
        if item["authoritative"]:
            for key in PROFILE_RUNTIME_FIELDS:
                draft.pop(key, None)
            draft["name"] = f"{profile.get('name', '')}{plan.suffix or ' - new'}"
            draft["authoritativeSource"] = {"type": "SOURCE", "id": new.id or NEW_SOURCE_ID, "name": new.name}
            plan.drafts[f"identity-profiles/{_file(draft['name'])}.json"] = draft
        else:
            plan.drafts[f"identity-profiles/{_file(profile.get('name', ''))} (updated).json"] = draft
    for name in plan.transforms:
        draft = rewrite(transforms[name], plan, renames)
        draft["name"] = renames.get(name, name)
        if not in_place:
            for key in TRANSFORM_RUNTIME_FIELDS:
                draft.pop(key, None)
        plan.drafts[f"transforms/{_file(draft['name'])}.json"] = draft

    _checks(plan, profiles)
    unknown = {}
    for item in plan.profiles:
        config = item["profile"].get("identityAttributeConfig") or {}
        for mapping in config.get("attributeTransforms") or []:
            for name in references(mapping.get("transformDefinition") or {}):
                if name not in transforms:
                    unknown.setdefault(name, []).append(mapping.get("identityAttributeName", ""))
    for name, attributes in unknown.items():
        plan.checks.append((WARNING, name, "Transform not analysed",
                            f"Used by {', '.join(attributes)} but its definition is not available, so it "
                            "may read the old source. Add the transform exports to --from, or run "
                            "against the tenant."))
    return plan


def _use(plan: Plan, attribute: str, where: str) -> None:
    info = plan.attributes.setdefault(attribute, {"used_by": [], "new_attribute": "", "status": ""})
    if where not in info["used_by"]:
        info["used_by"].append(where)


def _schema_name(source: Source, attribute: str) -> str:
    return next(a for a in source.schema or [] if a.lower() == attribute.lower())


def _file(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "unnamed"


def _checks(plan: Plan, profiles: list[dict]) -> None:
    if not plan.profiles and not plan.transforms:
        plan.checks.append((WARNING, "", "Nothing found",
                            f"No identity profile or transform reads '{plan.old.name}'. Check the name."))
    for attribute, info in plan.attributes.items():
        if info["status"].startswith("TO DEFINE") or "NOT in the new schema" in info["status"]:
            plan.checks.append((WARNING, attribute, "New attribute to define",
                                f"'{attribute}' ({info['status']}) is used by {len(info['used_by'])} "
                                "mapping(s)/transform(s). Add it to the attribute mapping file."))
    if plan.new.schema is None:
        plan.checks.append((INFO, plan.new.name, "New schema unknown",
                            "Attribute names of the new source could not be checked "
                            "(source not found in the tenant, or offline run)."))
    if not plan.new.id:
        plan.checks.append((INFO, plan.new.name, "New source id unknown",
                            f"Drafts contain '{NEW_SOURCE_ID}': replace it once the source exists."))
    priorities = ", ".join(str(p) for p in sorted({p.get("priority") for p in profiles} - {None}))
    for item in plan.profiles:
        if item["authoritative"]:
            name = item["profile"].get("name", "")
            plan.checks.append((INFO, name, "Priority to choose",
                                f"The new profile copies priority {item['profile'].get('priority')}, "
                                f"already used. Priorities in use: "
                                f"{priorities}."))
    unused = [name for name, info in plan.transforms.items() if not info["used_by"]
              and not any(name in other["calls"] for other in plan.transforms.values())]
    for name in unused:
        plan.checks.append((INFO, name, "Transform not used by a profile",
                            "Reads the old source but no identity profile uses it here; it may be used "
                            "elsewhere (provisioning policies, other transforms not exported)."))
    plan.checks.append((INFO, "", "Outside this analysis",
                        "Also plan: lifecycle states of the new profile, correlation of the new HR "
                        "accounts to existing identities, provisioning policies, roles/access rules "
                        "and workflows using source attributes, and identity refresh after the switch."))


# -- output ---------------------------------------------------------------------

STEPS = [
    "Review the 'Attribute mapping' sheet and complete attribute-mapping.csv (old_attribute;new_attribute).",
    "Run the analysis again with --attribute-map attribute-mapping.csv until no attribute is TO DEFINE.",
    "Review the drafts in transforms/ and identity-profiles/ (search for TODO_ and <new source id>).",
    "Create the transforms first (copies), then the new identity profile(s), in a sandbox tenant.",
    "Recreate the lifecycle states of the new profile and set its priority.",
    "Correlate the new source accounts to the existing identities, then run an identity refresh.",
    "Compare identity attribute values before and after on a sample of identities.",
]
READ_ME = [
    ("Purpose", "What has to change in identity profiles, attribute mappings and transforms when the "
                "old source is replaced by the new one."),
    ("Dashboard", "Key figures and charts."),
    ("Next steps", "The migration checklist."),
    ("Profiles", "Impacted identity profiles and what to do with each."),
    ("Attribute mapping", "Each old account attribute used, its counterpart in the new source and how it was "
                          "found. Result: OK (confirmed), Assumed (same name, new schema unknown) or To "
                          "define. Fill attribute-mapping.csv and run again with --attribute-map."),
    ("Mappings", "Every identity attribute of the impacted profiles, today and after the migration. "
                 "'to complete' means an attribute is still TODO."),
    ("Transforms", "Transforms reading the old source, and transforms calling them (they must follow, "
                   "since they would still call the old version)."),
    ("Checks", "Points to review, Warning first."),
    ("Drafts", "identity-profiles/ and transforms/ next to this workbook: JSON ready to review and create. "
               "Nothing has been changed in the tenant."),
]
RESULT_FILLS = {"OK": "E2F0D9", "Assumed": "FFF2CC", "To define": "F8CBAD"}
CHANGE_FILLS = {"unchanged": "EDEDED", "changed": "E2F0D9", "to complete": "F8CBAD"}
WIDE = {"Used by", "Today", "After the migration", "Details", "Step", "Action", "Attributes to change",
        "Used by (profile: attribute)", "Old attributes read", "Calls"}


def _result(status: str) -> str:
    if status.startswith("TO DEFINE") or "NOT in the new schema" in status:
        return "To define"
    return "Assumed" if "assumed" in status else "OK"


def build_dashboard(plan: Plan, title: str = "") -> dashboard.Dashboard:
    title = title or f"Source migration: {plan.old.name} → {plan.new.name}"
    attributes = [{"Old attribute": a, "New attribute": i["new_attribute"], "Result": _result(i["status"]),
                   "Status": i["status"], "Uses": len(i["used_by"]), "Used by": "\n".join(i["used_by"])}
                  for a, i in sorted(plan.attributes.items(), key=lambda kv: kv[0].lower())]
    mappings = [{"Profile": m["profile"], "Identity attribute": m["identity_attribute"],
                 "Change": m["change"], "Today": m["before"], "After the migration": m["after"]}
                for m in plan.mappings]
    profiles = [{"Profile": p["profile"].get("name", ""),
                 "Authoritative source": (p["profile"].get("authoritativeSource") or {}).get("name", ""),
                 "Action": p["action"], "Attributes to change": ", ".join(p["touched"])}
                for p in plan.profiles]
    transforms = [{"Transform": n, "Impact": i["impact"], "Old attributes read": "\n".join(i["attributes"]),
                   "Calls": "\n".join(i["calls"]), "Used by (profile: attribute)": "\n".join(i["used_by"]),
                   "Draft name": n if plan.in_place else f"{n}{plan.suffix}"}
                  for n, i in sorted(plan.transforms.items())]
    checks = [{"Severity": c[0], "Object": c[1], "Check": c[2], "Details": c[3]}
              for c in sorted(plan.checks, key=lambda c: (c[0] != WARNING, c[1].lower()))]

    results = Counter(row["Result"] for row in attributes)
    changes = Counter(row["Change"] for row in mappings)
    reading = sum(i["impact"] == "reads the old source" for i in plan.transforms.values())
    new_profiles = sum(p["authoritative"] for p in plan.profiles)
    warnings = sum(c[0] == WARNING for c in plan.checks)
    kpis = [
        ("Identity profiles impacted", f"{len(plan.profiles)}  ({new_profiles} new to create)",
         dashboard.BLUE),
        ("Attribute mappings changing", str(changes["changed"] + changes["to complete"]), dashboard.BLUE),
        ("Transforms impacted", f"{len(plan.transforms)}  ({reading} read the old source)", dashboard.BLUE),
        ("Old source attributes used", str(len(attributes)), dashboard.BLUE),
        ("New attribute to define", str(results["To define"]),
         dashboard.RED if results["To define"] else dashboard.GREEN),
        ("Warnings", str(warnings), dashboard.RED if warnings else dashboard.GREEN),
    ]
    charts = []
    labels = [r for r in RESULT_FILLS if results[r]]
    if labels:
        charts.append(dashboard.Chart("Old attributes: new counterpart", "doughnut", labels,
                                      [("Attributes", [results[r] for r in labels], "")]))
    labels = [c for c in CHANGE_FILLS if changes[c]]
    if labels:
        charts.append(dashboard.Chart("Mappings of the impacted profiles", "doughnut", labels,
                                      [("Mappings", [changes[c] for c in labels], "")]))
    tables = [
        dashboard.Table("Next steps", [{"#": i, "Step": s} for i, s in enumerate(STEPS, start=1)], wide=WIDE),
        dashboard.Table("Profiles", profiles, wide=WIDE),
        dashboard.Table("Attribute mapping", attributes, wide=WIDE, fills={"Result": RESULT_FILLS}),
        dashboard.Table("Mappings", mappings, wide=WIDE, fills={"Change": CHANGE_FILLS}),
        dashboard.Table("Transforms", transforms, wide=WIDE),
        dashboard.Table("Checks", checks, wide=WIDE,
                        fills={"Severity": {WARNING: "F8CBAD", INFO: "DDEBF7"}}),
    ]
    sections = [
        dashboard.Section("Next steps", [{"#": i, "Step": s} for i, s in enumerate(STEPS, start=1)],
                          ["#", "Step"]),
        dashboard.Section("Impacted identity profiles", profiles,
                          ["Profile", "Authoritative source", "Action", "Attributes to change"]),
        dashboard.Section("Attribute mapping", attributes,
                          ["Old attribute", "New attribute", "Result", "Uses"]),
        dashboard.Section("Mappings that change", [m for m in mappings if m["Change"] != "unchanged"],
                          ["Profile", "Identity attribute", "Change", "Today", "After the migration"]),
        dashboard.Section("Points to check", checks, ["Severity", "Object", "Check", "Details"]),
    ]
    how = "updated in place" if plan.in_place else f"copied with the suffix '{plan.suffix}'"
    return dashboard.Dashboard(
        title, dashboard.subtitle("identity profiles, attribute mappings and transforms",
                                  f"transforms {how}"),
        kpis, charts, tables, sections, READ_ME,
        colours={"OK": dashboard.GREEN, "Assumed": dashboard.AMBER, "To define": dashboard.RED,
                 "unchanged": dashboard.GREY, "changed": dashboard.GREEN, "to complete": dashboard.RED},
        wide_pdf={"Step", "Action", "Attributes to change", "Today", "After the migration", "Details"},
        alert=lambda row: row.get("Severity") == WARNING or row.get("Result") == "To define")


def write_outputs(plan: Plan, folder: str, pdf: bool = True) -> str:
    """Workbook (and PDF), attribute mapping template and draft JSON files in *folder*;
    returns the workbook path."""
    os.makedirs(folder, exist_ok=True)
    for relative, data in plan.drafts.items():
        path = os.path.join(folder, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=4)
    write_table(os.path.join(folder, "attribute-mapping.csv"), ["old_attribute", "new_attribute", "status"],
                [[a, i["new_attribute"], i["status"]] for a, i in sorted(plan.attributes.items())],
                delimiter=";")
    report = build_dashboard(plan)
    path = dashboard.write_xlsx(report, os.path.join(folder, "source-migration.xlsx"))
    if pdf:
        dashboard.write_pdf(report, os.path.join(folder, "source-migration.pdf"))
    return path
