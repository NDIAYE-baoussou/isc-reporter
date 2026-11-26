"""Detailed report on identity attributes: how each identity profile computes them, everything that
uses them, and the quality of their values over the identities. Read-only.

For each attribute asked:

* **Definition**: its settings (type, multi-valued, searchable...) and, profile by profile, its
  mapping: source, account attribute, transforms, a plain explanation and the transform tree.
* **Used by**: what reads it: other mappings, named transforms, provisioning policies (what it
  writes to target systems), role and dimension criteria, segments, account and manager
  correlation, workflows.
* **Values**: over the identities read, how many have a value, the distinct values and how often,
  by identity profile, and data quality checks: empty although mapped, spaces, variants of the
  same value, placeholders, rare values, duplicates of identifiers, dates that cannot be read,
  e-mail addresses, lifecycle states that do not exist.
"""

from __future__ import annotations

import difflib
import json
import logging
import re
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from isc_reporter import attribute_usage, criteria, dashboard, transform_tree
from isc_reporter.csvio import write_table
from isc_reporter.errors import UsageError
from isc_reporter.identity_profiles import INFO, WARNING, mapping_rows

log = logging.getLogger("isc_reporter")

WORKFLOW = "Workflow"
USAGE_ORDER = (attribute_usage.PROFILE, attribute_usage.TRANSFORM, attribute_usage.POLICY, criteria.ROLE,
               criteria.DIMENSION, criteria.SEGMENT, criteria.CORRELATION, criteria.MANAGER, WORKFLOW)
#: Identity attributes that the search documents hold outside "attributes": the path to their value.
TOP_LEVEL = {"manager": ("manager", "name"), "email": ("email",), "firstname": ("firstName",),
             "lastname": ("lastName",), "displayName": ("displayName",), "uid": ("name",),
             "phone": ("phone",)}
INCLUDES = ["id", "name", "displayName", "identityProfile", "attributes", "manager", "email", "firstName",
            "lastName", "phone"]
PLACEHOLDERS = {"null", "none", "n/a", "na", "-", "--", "unknown", "tbd", "?", "undefined", "nil", "empty",
                "#n/a", "not set"}
IDENTIFIERS = {"uid", "email", "employeenumber", "identificationnumber"}
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
EXAMPLES = 8

SUMMARY_COLUMNS = ["Attribute", "Display name", "Type", "Multi-valued", "Searchable", "Standard", "Mapped in",
                   "Used by", "Identities", "Filled", "Filled %", "Distinct values", "Top value", "Warnings"]
USAGE_COLUMNS = ["Attribute", "Type", "Object", "Enabled", "Where", "How", "Definition"]
QUALITY_COLUMNS = ["Severity", "Attribute", "Check", "Identities", "Details"]


# -- reading ------------------------------------------------------------------------------


@dataclass
class AttributeData:
    names: list[str]                                  # the attributes analysed (technical names)
    definitions: dict[str, dict]                      # every identity attribute definition, by name
    profiles: list[dict]
    transforms: dict[str, dict]
    policies: list[dict]                              # provisioning policies, with their "source"
    objects: criteria.Objects                         # roles, segments, sources, workflows
    identities: list[dict]                            # search documents
    lifecycle: dict[str, dict[str, dict]] = field(default_factory=dict)   # profile id -> states
    query: str = "*"
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    origin: str = ""


def mapped_attributes(profiles: list[dict]) -> set[str]:
    return {m.get("identityAttributeName", "") for p in profiles
            for m in (p.get("identityAttributeConfig") or {}).get("attributeTransforms") or []}


def resolve(asked: list[str], definitions: dict[str, dict], mapped: set[str]) -> list[str]:
    """The technical names of the attributes *asked* (technical or display names, any case)."""
    known = {name.lower(): name for name in [*mapped, *definitions] if name}
    display = {str(d.get("displayName") or "").lower(): n
               for n, d in definitions.items() if d.get("displayName")}
    found, missing = [], []
    for name in asked:
        hit = known.get(name.lower()) or display.get(name.lower())
        if hit:
            found.append(hit)
        else:
            missing.append(name)
    if missing:
        close = {m: difflib.get_close_matches(m, list(known.values()), n=3) for m in missing}
        hints = "; ".join(f"{m}: did you mean {', '.join(c)}?" for m, c in close.items() if c)
        raise UsageError(f"unknown identity attribute(s): {', '.join(missing)}"
                         + (f" ({hints})" if hints else ""))
    return list(dict.fromkeys(found))


def fetch(isc, asked: list[str], query: str = "*", max_identities: int | None = None) -> AttributeData:
    from isc_reporter import snapshot
    from isc_reporter.api import call, error_message, list_all, model_json
    from isc_reporter.identity_profiles import fetch_policies
    from isc_reporter.leavers import read_lifecycle
    from isc_reporter.search import search_all

    definitions = {a.get("name", ""): a for a in (model_json(x) for x in call(
        isc.identity_attributes.list_identity_attributes_v1, include_system=True) or [])}
    profiles = [model_json(p) for p in list_all(isc.identity_profiles.list_identity_profiles_v1)]
    names = resolve(asked, definitions, mapped_attributes(profiles))
    transforms = {t["name"]: t for t in (model_json(t) for t in list_all(isc.transforms.list_transforms_v1))}
    policies = fetch_policies(isc)
    objects = criteria.fetch(isc)
    try:
        objects.workflows = snapshot._list(isc, snapshot.KINDS["workflows"])
    except Exception as exc:
        objects.notes.append(f"Workflows not read ({error_message(exc)}).")
    notes: list[str] = []
    lifecycle: dict[str, dict[str, dict]] = {}
    if "cloudLifecycleState" in names:
        lifecycle, notes = read_lifecycle(isc, {p.get("id", ""): p.get("name", "") for p in profiles})
    log.info("Reading the identities matching %s", query)
    identities = search_all(isc, "identities", query, sort_field=None, include_nested=False,
                            includes=INCLUDES, max_results=max_identities + 1 if max_identities else None,
                            progress=lambda n: n % 5000 == 0 and log.info("%d identities so far", n))
    truncated = bool(max_identities) and len(identities) > max_identities
    identities = identities[:max_identities] if max_identities else identities
    log.info("%d identit(ies) read%s", len(identities), " (limit reached)" if truncated else "")
    return AttributeData(names, definitions, profiles, transforms, policies, objects, identities, lifecycle,
                         query, truncated, notes + objects.notes, isc.base_url)


# -- values -------------------------------------------------------------------------------


def value_of(doc: dict, name: str):
    attributes = doc.get("attributes") or {}
    if name in attributes:
        return attributes[name]
    value = doc
    for key in TOP_LEVEL.get(name, ("-",)):
        value = value.get(key) if isinstance(value, dict) else None
    return value


def values_of(value) -> list[str]:
    """The values held (one, or several for a multi-valued attribute), as text; empty ones left out."""
    items = value if isinstance(value, list) else [value]
    found = []
    for item in items:
        if isinstance(item, dict):
            item = item.get("name") or item.get("displayName") or item.get("id") or json.dumps(item)
        if item is not None and str(item).strip():
            found.append(str(item))
    return found


def normal(value: str) -> str:
    """The value without case, accents and extra spaces, to find variants of the same value."""
    text = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return " ".join(text.split()).casefold()


@dataclass
class Stats:
    name: str
    identities: int = 0
    filled: int = 0
    empty_mapped: int = 0                    # empty although the identity's profile maps the attribute
    empty_unmapped: int = 0
    values: Counter = field(default_factory=Counter)
    holders: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    profiles: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    distinct: dict[str, set] = field(default_factory=lambda: defaultdict(set))
    value_profiles: dict[str, Counter] = field(default_factory=lambda: defaultdict(Counter))
    unknown_states: Counter = field(default_factory=Counter)

    @property
    def share(self) -> float:
        return self.filled / self.identities if self.identities else 0.0


def statistics(data: AttributeData, name: str) -> Stats:
    stats = Stats(name)
    maps = {p.get("name", ""): name in mapped_attributes([p]) for p in data.profiles}
    states_by_name = {p.get("name", ""): data.lifecycle.get(p.get("id", ""), {}) for p in data.profiles}
    for doc in data.identities:
        profile = (doc.get("identityProfile") or {}).get("name", "") or "(no profile)"
        held = values_of(value_of(doc, name))
        stats.identities += 1
        stats.profiles[profile]["Identities"] += 1
        if not held:
            if maps.get(profile):
                stats.empty_mapped += 1
            else:
                stats.empty_unmapped += 1
            stats.profiles[profile]["Empty"] += 1
            continue
        stats.filled += 1
        stats.profiles[profile]["Filled"] += 1
        for value in held:
            stats.values[value] += 1
            stats.distinct[profile].add(value)
            stats.value_profiles[value][profile] += 1
            if len(stats.holders[value]) < 10:
                stats.holders[value].append(doc.get("name") or doc.get("id", ""))
            states = states_by_name.get(profile)
            if name == "cloudLifecycleState" and states and value not in states:
                stats.unknown_states[f"{value} ({profile})"] += 1
    return stats


def _examples(values, limit: int = EXAMPLES) -> str:
    items = [f"'{v}'" for v in list(values)[:limit]]
    more = len(list(values)) - limit
    return ", ".join(items) + (f" and {more} more" if more > 0 else "")


def quality(stats: Stats, definition: dict, mapped_in: int) -> list[dict]:
    """Data quality findings on the values of one attribute."""
    from isc_reporter.externals import COMPACT, ISO, NUMERIC, day_order, parse_date

    name, rows = stats.name, []

    def add(severity, check, count, details):
        rows.append({"Severity": severity, "Attribute": name, "Check": check, "Identities": count,
                     "Details": details})

    if not stats.identities:
        return rows
    if stats.empty_mapped:
        share = stats.empty_mapped / stats.identities
        add(WARNING if share >= 0.05 else INFO, "Empty although mapped", stats.empty_mapped,
            f"{share:.1%} of the identities read have no value, although their identity profile maps it: "
            "the source has no value for them, or the mapping fails.")
    if stats.empty_unmapped:
        add(INFO, "Not mapped in their profile", stats.empty_unmapped,
            "Identities whose identity profile does not compute this attribute (expected to be empty).")
    if not stats.filled:
        add(INFO, "No value", 0, "No identity read has a value.")
        return rows
    values = stats.values
    spaced = [v for v in values if v != v.strip() or "  " in v]
    if spaced:
        add(WARNING, "Extra spaces", sum(values[v] for v in spaced),
            f"{len(spaced)} value(s) with leading, trailing or double spaces: {_examples(spaced)}")
    groups: dict[str, list[str]] = defaultdict(list)
    for value in values:
        groups[normal(value)].append(value)
    variants = [g for g in groups.values() if len(g) > 1]
    if variants:
        text = "; ".join(" / ".join(f"'{v}' ({values[v]})" for v in sorted(g, key=lambda v: -values[v]))
                         for g in variants[:EXAMPLES])
        add(WARNING, "Variants of the same value", sum(values[v] for g in variants for v in g),
            f"{len(variants)} value(s) written in several ways (case, accents, spaces): {text}")
    placeholders = [v for v in values if v.strip().casefold() in PLACEHOLDERS]
    if placeholders:
        add(WARNING, "Placeholder values", sum(values[v] for v in placeholders),
            f"Values that mean 'no value': {_examples(placeholders)}")
    distinct, filled = len(values), stats.filled
    identifier = name.lower() in IDENTIFIERS or (filled >= 10 and distinct >= 0.9 * filled)
    if identifier:
        duplicates = [v for v, n in values.items() if n > 1]
        if duplicates:
            add(WARNING, "Duplicates", sum(values[v] for v in duplicates),
                f"{len(duplicates)} value(s) held by several identities, for an attribute that identifies "
                f"them: {_examples(duplicates)} (see Duplicates)")
    order = day_order(values)
    parsed = {v: parse_date(v, order=order) for v in values}
    readable = [v for v, (d, _) in parsed.items() if d]
    dates_only = bool(readable) and sum(values[v] for v in readable) >= 0.8 * sum(values.values())
    if not identifier and not dates_only and filled >= 20 and distinct <= 0.2 * filled:
        rare = [v for v, n in values.items() if n == 1]
        if rare:
            add(INFO, "Rare values", len(rare),
                f"{len(rare)} value(s) held by one identity only, among {distinct}: possible typos. "
                f"{_examples(rare)}")
    if dates_only:
        unreadable = [v for v, (d, _) in parsed.items() if d is None]
        if unreadable:
            add(WARNING, "Dates that cannot be read", sum(values[v] for v in unreadable),
                f"{_examples(unreadable)}: {parsed[unreadable[0]][1]}")
        formats = Counter("ISO 8601" if ISO.match(v.strip()) else "yyyyMMdd" if COMPACT.match(v.strip())
                          else {"dmy": "dd/MM/yyyy", "mdy": "MM/dd/yyyy"}.get(order, "dd/MM or MM/dd")
                          if NUMERIC.match(v.strip()) else "other" for v in readable)
        dates = sorted(parsed[v][0] for v in readable)
        if len(formats) > 1:
            add(WARNING, "Several date formats", sum(values[v] for v in readable),
                ", ".join(f"{f} ({n} value(s))" for f, n in formats.most_common()))
        odd = [v for v in readable if not 1900 <= parsed[v][0].year <= 2100]
        if odd:
            add(WARNING, "Unlikely dates", sum(values[v] for v in odd), _examples(odd))
        add(INFO, "Date range", sum(values[v] for v in readable),
            f"From {dates[0].isoformat()} to {dates[-1].isoformat()} ({', '.join(formats)})")
    if "mail" in name.lower():
        invalid = [v for v in values if not EMAIL.match(v.strip())]
        if invalid:
            add(WARNING, "Invalid e-mail addresses", sum(values[v] for v in invalid), _examples(invalid))
    if stats.unknown_states:
        add(WARNING, "Not a lifecycle state", sum(stats.unknown_states.values()),
            "Values that are not a lifecycle state of the identity's profile: "
            + _examples(stats.unknown_states))
    top, count = values.most_common(1)[0]
    if distinct > 1 and count >= 0.95 * filled and not identifier:
        add(INFO, "Almost always the same", count,
            f"'{top}' for {count / filled:.0%} of the identities with a value.")
    if definition.get("multi"):
        add(INFO, "Multi-valued", filled, f"{sum(values.values())} value(s) on {filled} identit(ies).")
    if not mapped_in and stats.filled:
        add(INFO, "Filled without a mapping", stats.filled,
            "No identity profile maps it: its values come from elsewhere (system attribute, workflow, API).")
    return rows


# -- usages -------------------------------------------------------------------------------


def _workflow_where(workflow: dict, name: str) -> list[str]:
    patterns = [rf"attributes\.{re.escape(name)}\b",
                rf'"(?:attribute|attributeName|identityAttribute|identityAttributeName)"\s*:\s*"{re.escape(name)}"',
                rf"@\.attribute\s*==\s*['\"]{re.escape(name)}['\"]"]
    found = []
    for part in ("trigger", "definition"):
        text = json.dumps(workflow.get(part) or {}, ensure_ascii=False)
        if any(re.search(p, text) for p in patterns):
            found.append(part)
    return found


def usage_rows(data: AttributeData, rows_by_profile: list[list[dict]]) -> list[dict]:
    """Everything that reads the attributes analysed (one row per use, see :data:`USAGE_COLUMNS`)."""
    wanted = {name.lower(): name for name in data.names}
    known = set(data.definitions) | mapped_attributes(data.profiles)
    rows = []

    def add(attribute, kind, obj, enabled, where, how, definition=""):
        rows.append(dict(zip(USAGE_COLUMNS, (attribute, kind, obj, enabled, where, how, definition),
                             strict=True)))

    for u in attribute_usage.collect(data.profiles, rows_by_profile, data.transforms, data.policies, known):
        if u["role"] == attribute_usage.READ and u["attribute"].lower() in wanted:
            add(wanted[u["attribute"].lower()], u["where"], u["object"], "", u["location"], u["detail"])
    names = {s.get("id", ""): s.get("name", "") for s in data.objects.sources or []}
    for role in data.objects.roles or []:
        enabled = "no" if role.get("enabled") is False else "yes"
        targets = [(criteria.ROLE, role.get("name", ""), (role.get("membership") or {}).get("criteria"))]
        targets += [(criteria.DIMENSION, f"{role.get('name', '')} / {d.get('name', '')}",
                     (d.get("membership") or {}).get("criteria")) for d in role.get("dimensions") or []]
        for kind, obj, node in targets:
            if not node:
                continue
            for leaf in criteria.leaves(node):
                key = leaf.get("key") or {}
                attribute = criteria.key_attribute(key).lower()
                if str(key.get("type", "")).upper() == "IDENTITY" and attribute in wanted:
                    add(wanted[attribute], kind, obj, enabled, criteria.criteria_text(leaf, names),
                        "membership criterion", criteria.criteria_text(node, names))
    for segment in data.objects.segments or []:
        expression = (segment.get("visibilityCriteria") or {}).get("expression")
        for leaf in criteria._segment_leaves(expression):
            attribute = str(leaf.get("attribute", "")).lower()
            if attribute in wanted:
                add(wanted[attribute], criteria.SEGMENT, segment.get("name", ""),
                    "no" if segment.get("active") is False else "yes", criteria.segment_text(leaf),
                    "visibility criterion", criteria.segment_text(expression))
    for source in data.objects.sources or []:
        assignments = (source.get("correlationConfig") or {}).get("attributeAssignments") or []
        for assignment in assignments:
            sides = [str(assignment.get(k) or "") for k in ("property", "value")]
            hit = next((s for s in sides if s.lower() in wanted), None)
            if hit:
                add(wanted[hit.lower()], criteria.CORRELATION, source.get("name", ""), "",
                    f"{sides[0]} = {sides[1]}", "joins the accounts of the source to identities")
        manager = source.get("managerCorrelationMapping") or {}
        identity = str(manager.get("identityAttributeName") or "")
        if identity.lower() in wanted:
            add(wanted[identity.lower()], criteria.MANAGER, source.get("name", ""), "",
                f"{source.get('name', '')}.{manager.get('accountAttributeName', '')} = identity.{identity}",
                "finds the manager of each identity")
    for workflow in data.objects.workflows or []:
        for name in data.names:
            for part in _workflow_where(workflow, name):
                enabled = "yes" if workflow.get("enabled") else "no"
                add(name, WORKFLOW, workflow.get("name", ""), enabled, part, "named in the workflow")
    rows.sort(key=lambda r: (data.names.index(r["Attribute"]), USAGE_ORDER.index(r["Type"]),
                             r["Object"].lower()))
    return rows


# -- analysis -----------------------------------------------------------------------------


@dataclass
class Result:
    data: AttributeData
    stats: dict[str, Stats]
    summary: list[dict]
    mappings: list[dict]
    usages: list[dict]
    quality: list[dict]
    values: list[dict]                       # every value, with its count
    profiles: list[dict]
    duplicates: list[dict]
    top: int = 25


def analyse(data: AttributeData, top: int = 25) -> Result:
    display = {n: d.get("displayName") or "" for n, d in data.definitions.items()}
    rows_by_profile = [mapping_rows(p, data.transforms, display) for p in data.profiles]
    mappings = []
    for name in data.names:
        for profile, rows in zip(data.profiles, rows_by_profile, strict=True):
            row = next((r for r in rows if r["identity_attribute"] == name), None)
            if row is None:
                mappings.append({"Attribute": name, "Identity profile": profile.get("name", ""),
                                 "Mapping": "Not mapped", "Explanation": "This profile does not compute it.",
                                 "Sources": "", "Source attributes": "", "Transforms": "", "Logic": "",
                                 "Tree": "", "Checks": ""})
                continue
            mappings.append({"Attribute": name, "Identity profile": profile.get("name", ""),
                             "Mapping": row["mapping"], "Explanation": row["explanation"],
                             "Sources": row["sources"], "Source attributes": row["source_attributes"],
                             "Transforms": row["transforms"], "Logic": row["logic"],
                             "Tree": transform_tree.draw(row["tree"]),
                             "Checks": "\n".join(f"{check}: {details}"
                                                 for _, check, details in row["checks"])})
    usages = usage_rows(data, rows_by_profile)
    stats = {name: statistics(data, name) for name in data.names}
    summary, findings, values, profiles, duplicates = [], [], [], [], []
    for name in data.names:
        definition, s = data.definitions.get(name, {}), stats[name]
        mapped_in = sum(1 for m in mappings if m["Attribute"] == name and m["Mapping"] != "Not mapped")
        found = quality(s, definition, mapped_in)
        findings += found
        top_value = s.values.most_common(1)
        summary.append({
            "Attribute": name, "Display name": display.get(name, ""), "Type": definition.get("type", ""),
            "Multi-valued": "yes" if definition.get("multi") else "no",
            "Searchable": "yes" if definition.get("searchable") else "no",
            "Standard": ("system" if definition.get("system") else
                         "yes" if definition.get("standard") else "no"),
            "Mapped in": f"{mapped_in} / {len(data.profiles)}",
            "Used by": sum(1 for u in usages if u["Attribute"] == name), "Identities": s.identities,
            "Filled": s.filled, "Filled %": s.share, "Distinct values": len(s.values),
            "Top value": f"{top_value[0][0]} ({top_value[0][1]})" if top_value else "",
            "Warnings": sum(1 for f in found if f["Severity"] == WARNING)})
        for value, count in s.values.most_common():
            where = ", ".join(f"{p} ({n})" for p, n in s.value_profiles[value].most_common(3))
            values.append({"Attribute": name, "Value": value, "Identities": count,
                           "Share": count / s.filled if s.filled else 0, "Identity profiles": where})
        mapped = {m["Identity profile"] for m in mappings
                  if m["Attribute"] == name and m["Mapping"] != "Not mapped"}
        for profile, count in sorted(s.profiles.items(), key=lambda kv: -kv[1]["Identities"]):
            profiles.append({"Attribute": name, "Identity profile": profile,
                             "Mapped": "yes" if profile in mapped else "no",
                             "Identities": count["Identities"],
                             "Filled": count["Filled"], "Empty": count["Empty"],
                             "Filled %": count["Filled"] / count["Identities"] if count["Identities"] else 0,
                             "Distinct values": len(s.distinct[profile])})
        if any(f["Check"] == "Duplicates" for f in found):
            for value, count in s.values.most_common():
                if count < 2:
                    break
                duplicates.append({"Attribute": name, "Value": value, "Identities": count,
                                   "Held by": ", ".join(s.holders[value]) + (" ..." if count > 10 else "")})
    findings.sort(key=lambda f: (data.names.index(f["Attribute"]), f["Severity"] != WARNING))
    return Result(data, stats, summary, mappings, usages, findings, values, profiles, duplicates, top)


# -- report -------------------------------------------------------------------------------

READ_ME = [
    ("What is this?", "A detailed report on the identity attributes given: how each identity profile "
                      "computes them, everything that uses them, and the quality of their values. Nothing "
                      "was changed in the tenant."),
    ("Attributes", "One line per attribute: its settings, the profiles that map it, how many objects use "
                   "it, and a summary of its values."),
    ("Mappings", "Profile by profile: the source and account attribute it comes from, the transforms, a "
                 "plain explanation, the transform tree and the points to check."),
    ("Used by", "What reads the attribute: other attributes' mappings, named transforms, provisioning "
                "policies (the attribute is written to the target system), role and dimension criteria, "
                "segments, account and manager correlation, workflows that name it. Rule code cannot be "
                "read."),
    ("Values", "The most frequent values of each attribute (the CSV has them all), with the profiles of "
               "the identities that hold them."),
    ("By profile", "For each identity profile, how many identities have a value, and whether the profile "
                   "maps the attribute."),
    ("Quality", "Checks on the values: empty although mapped, extra spaces, variants of the same value "
                "(case, accents, spaces), placeholders (N/A, null...), rare values (possible typos), "
                "duplicates of an attribute that identifies people, dates that cannot be read or use "
                "several formats, invalid e-mail addresses, values that are not a lifecycle state."),
    ("Duplicates", "The values of identifying attributes (uid, email, employee number, or any attribute "
                   "almost always unique) held by several identities, with who holds them."),
    ("Values read", "The values come from the search index, over the identities matching the query (all "
                    "by default)."),
    ("Privacy", "The values and duplicates list real people's data: protect the file."),
]


def settings_rows(result: Result) -> list[dict]:
    data = result.data
    rows = [{"Setting": "Attributes", "Value": ", ".join(data.names)},
            {"Setting": "Identities", "Value": f"{len(data.identities)} matching {data.query}"
                                               + (" (limit reached)" if data.truncated else "")},
            {"Setting": "Values listed per attribute", "Value": str(result.top)}]
    return rows + [{"Setting": "Note", "Value": note} for note in data.notes]


def build_dashboard(result: Result, title: str = "") -> dashboard.Dashboard:
    data = result.data
    warnings = sum(1 for f in result.quality if f["Severity"] == WARNING)
    lowest = min(result.summary, key=lambda r: r["Filled %"]) if result.summary else None
    kpis = [("Attributes", str(len(data.names)), dashboard.BLUE),
            ("Identities read", f"{len(data.identities)}" + (" (limit)" if data.truncated else ""),
             dashboard.BLUE),
            ("Lowest fill rate", f"{lowest['Attribute']}: {lowest['Filled %']:.0%}" if lowest else "-",
             dashboard.AMBER if lowest and lowest["Filled %"] < 0.9 else dashboard.GREEN),
            ("Used by", str(len(result.usages)), dashboard.BLUE),
            ("Data quality warnings", str(warnings), dashboard.RED if warnings else dashboard.GREEN)]
    charts = []
    if result.summary:
        names = [r["Attribute"] for r in result.summary]
        charts.append(dashboard.Chart("Identities with and without a value", "stacked", names, [
            ("Filled", [result.stats[n].filled for n in names], dashboard.GREEN),
            ("Empty although mapped", [result.stats[n].empty_mapped for n in names], dashboard.RED),
            ("Not mapped in their profile", [result.stats[n].empty_unmapped for n in names],
             dashboard.GREY)]))
    for name in data.names[:3]:
        common = result.stats[name].values.most_common(10)
        if common:
            charts.append(dashboard.Chart(f"Most frequent values: {name}", "bar", [v for v, _ in common],
                                          [("Identities", [n for _, n in common], dashboard.BLUE)]))
    shown = [row for name in data.names
             for row in [r for r in result.values if r["Attribute"] == name][:result.top]]
    wide = {"Explanation", "Logic", "Checks", "Definition", "Where", "How", "Details", "Held by", "Value",
            "Identity profiles", "Top value", "Sources", "Source attributes", "Transforms"}
    tables = [dashboard.Table("Attributes", result.summary, wide=wide, percent={"Filled %"},
                              bars={"Filled %"}, red_positive={"Warnings"}),
              dashboard.Table("Mappings", result.mappings, wide=wide, mono={"Tree"}),
              dashboard.Table("Used by", result.usages, wide=wide),
              dashboard.Table("Quality", result.quality, wide=wide,
                              fills={"Severity": {WARNING: "FFE0B2", INFO: "DDEBF7"}}),
              dashboard.Table("Values", shown, wide=wide, percent={"Share"}, bars={"Share"}),
              dashboard.Table("By profile", result.profiles, wide=wide, percent={"Filled %"},
                              bars={"Filled %"}),
              dashboard.Table("Duplicates", result.duplicates, wide=wide),
              dashboard.Table("Settings", settings_rows(result), wide=wide)]
    sections = [dashboard.Section("Attributes", result.summary,
                                  ["Attribute", "Mapped in", "Used by", "Identities", "Filled %",
                                   "Distinct values", "Warnings"]),
                dashboard.Section("Data quality", result.quality,
                                  ["Severity", "Attribute", "Check", "Identities", "Details"]),
                dashboard.Section("Used by", result.usages, ["Attribute", "Type", "Object", "Where"]),
                dashboard.Section("Mappings", result.mappings,
                                  ["Attribute", "Identity profile", "Mapping", "Explanation"])]
    default = "Identity attributes: " + ", ".join(data.names[:4]) + (" ..." if len(data.names) > 4 else "")
    return dashboard.Dashboard(title or default, dashboard.subtitle(data.origin), kpis, charts, tables,
                               sections, READ_ME, wide_pdf={"Details", "Explanation", "Where", "Object"},
                               alert=lambda row: row.get("Severity") == WARNING)


def write_xlsx(path: str, result: Result, title: str = "") -> str:
    return dashboard.write_xlsx(build_dashboard(result, title), path)


def write_pdf(path: str, result: Result, title: str = "", max_rows: int = 500) -> str:
    return dashboard.write_pdf(build_dashboard(result, title), path, max_rows)


def write_csv(path: str, result: Result) -> str:
    columns = ["Attribute", "Value", "Identities", "Share", "Identity profiles"]
    rows = [[r["Attribute"], r["Value"], r["Identities"], f"{r['Share']:.4f}", r["Identity profiles"]]
            for r in result.values]
    return write_table(path, columns, rows, ";")
