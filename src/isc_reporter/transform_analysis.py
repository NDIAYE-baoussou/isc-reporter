"""Transform analysis: what is broken, risky, repeated or more complex than needed in the transforms,
with optimized drafts of the changes that keep the same result.

Every place a transform is defined is analysed: the named transforms, the identity profile mappings
and the fields of the source provisioning policies. The findings follow SailPoint's guidance on
transforms: give every lookup a default, do not read identity attributes within an identity profile
mapping, plan for bad data, encapsulate repetition in named transforms.

The optimized drafts only contain rewrites that give the same result: duplicates of a named
transform and transforms that only call another one are replaced by the transform they repeat,
nested firstValid and concat are flattened, a firstValid or concat of a single value is replaced by
the value, firstValid choices after a fixed value are dropped, a lower, upper or trim below another
case change is dropped, a dateFormat to the same format is dropped, and a block repeated in several
places is moved to a named transform. SailPoint's own transforms (``internal``) are never changed.
Nothing is changed in the tenant.

The platform view looks at the transforms of the whole tenant: an optimization plan with the number
of custom named transforms before and after the clean-up, the transform steps each identity profile
runs for one identity and for all of them, now and with the drafts, the values recomputed every
night without need, and the named transforms that apply the same logic to different inputs, which
one transform without input can replace.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field

from isc_reporter import dashboard, identity_profiles
from isc_reporter.csvio import write_table
from isc_reporter.errors import UsageError
from isc_reporter.identity_profiles import INFO, WARNING
from isc_reporter.transform_tree import NO_ID, TRANSFORM_TYPES, attributes_of, calls, reference_name

log = logging.getLogger("isc_reporter")

NAMED, PROFILE, POLICY = "Named transform", "Identity profile", "Provisioning policy"
BROKEN, RISKY, SIMPLIFY, BAD_DATA, MAINTAIN = "Broken", "Risky", "Simplify", "Bad data", "Maintenance"
PERFORM = "Performance"
CATEGORIES = (BROKEN, RISKY, BAD_DATA, PERFORM, SIMPLIFY, MAINTAIN)
CATEGORY_COLOURS = {BROKEN: dashboard.RED, RISKY: dashboard.AMBER, BAD_DATA: "8E24AA", PERFORM: "00897B",
                    SIMPLIFY: dashboard.BLUE, MAINTAIN: dashboard.GREY}
NIGHTLY, SAME_LOGIC = "Recomputed every night without need", "Same logic, other input"
TIMED = {"getReferenceIdentityAttribute", "rule"}    # can change while the data they are given does not
FINDING_COLUMNS = ["Severity", "Category", "Where", "Object", "Location", "Check", "Details",
                   "Recommendation", "In the drafts"]
SIZE_LIMIT = 400 * 1024                  # SailPoint: a transform cannot exceed 400 KB
DEEP, MANY_STEPS = 6, 40                 # beyond these, a named transform is reported as complex
CASE_CHANGES = {"lower": {"lower", "upper"}, "upper": {"lower", "upper"}, "trim": {"trim"}}
VELOCITY_LOGIC = re.compile(r"#(if|set|foreach|elseif)\b")
TRUE = (True, "true", "True")


@dataclass
class Site:
    """A place where a transform is defined."""

    kind: str                            # NAMED, PROFILE or POLICY
    name: str                            # transform name, "profile: attribute", "source / usage: field"
    definition: dict
    owner: dict                          # the named transform, identity profile or provisioning policy
    index: int = -1                      # position of the mapping or field in the owner

    @property
    def internal(self) -> bool:
        return self.kind == NAMED and self.owner.get("internal") in TRUE


@dataclass
class Analysis:
    sites: list[Site]
    transforms: dict[str, dict]
    findings: list[dict] = field(default_factory=list)
    duplicates: list[dict] = field(default_factory=list)
    shared: list[dict] = field(default_factory=list)       # blocks moved to a new named transform
    unused: list[dict] = field(default_factory=list)
    complexity: list[dict] = field(default_factory=list)
    changes: list[dict] = field(default_factory=list)      # one row per rewrite in the drafts
    drafts: dict[str, dict] = field(default_factory=dict)  # relative path -> JSON
    to_delete: list[dict] = field(default_factory=list)
    similar: list[dict] = field(default_factory=list)      # same logic, other input
    profile_costs: list[dict] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    optimized: dict[int, dict] = field(default_factory=dict)  # site index -> definition in the drafts
    custom: tuple[int, int] = (0, 0)     # custom named transforms now, and after the clean-up
    origin: str = ""

    @property
    def warnings(self) -> int:
        return sum(f["Severity"] == WARNING for f in self.findings)

    @property
    def merged(self) -> list[str]:
        """The transforms a transform with the same logic can replace."""
        return [row["Transform"] for row in self.similar if row["Transform"] != row["Transform kept"]]


@dataclass
class Cost:
    """What a definition runs for one identity, the named transforms it calls included."""

    steps: int = 0                       # transforms run, the calls of named transforms left out
    rules: int = 0
    others: int = 0                      # getReferenceIdentityAttribute: reads another identity
    refreshed: bool = False              # requiresPeriodicRefresh is set
    timed: bool = False                  # can change on its own: today's date, another identity, a rule

    def add(self, other: Cost) -> None:
        self.steps += other.steps
        self.rules += other.rules
        self.others += other.others
        self.refreshed |= other.refreshed
        self.timed |= other.timed


class Costs:
    """The costs of definitions, through the named transforms of *transforms* (name -> transform)."""

    def __init__(self, transforms: dict[str, dict]):
        self.transforms = transforms
        self.named: dict[str, Cost] = {}
        self.open: set[str] = set()

    def of(self, definition) -> Cost:
        cost = Cost()
        for node, _ in walk(definition):
            kind, attrs = node.get("type"), attributes_of(node)
            cost.refreshed |= _refreshed(node)
            if kind == "reference":
                if reference_name(attrs) in self.transforms:
                    cost.add(self.call(reference_name(attrs)))
                continue
            cost.steps += 1
            cost.rules += kind == "rule"
            cost.others += kind == "getReferenceIdentityAttribute"
            cost.timed |= kind in TIMED or (kind in ("dateCompare", "dateMath") and _uses_now(attrs))
        return cost

    def call(self, name: str) -> Cost:
        if name in self.open:
            return Cost()                # a loop, reported as a circular reference
        if name not in self.named:
            self.open.add(name)
            cost = self.of(_definition(self.transforms[name]))
            cost.refreshed |= _refreshed(self.transforms[name])
            self.open.discard(name)
            self.named[name] = cost
        return self.named[name]


# -- reading ------------------------------------------------------------------------------------


def fetch(isc) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """Identity profiles, named transforms by name and the provisioning policies of every source."""
    from isc_reporter.api import list_all, model_json

    profiles = [model_json(p) for p in list_all(isc.identity_profiles.list_identity_profiles_v1)]
    transforms = {t["name"]: t for t in (model_json(t) for t in list_all(isc.transforms.list_transforms_v1))}
    return profiles, transforms, identity_profiles.fetch_policies(isc)


def load_files(paths: list[str]) -> tuple[list[dict], dict[str, dict], list[dict]]:
    """The same from exported JSON files: profiles, transforms and sources with their policies."""
    for path in paths:
        if not os.path.exists(path):
            raise UsageError(f"'{path}' does not exist")
    profiles, transforms = [], {}
    for obj in identity_profiles.iter_objects(paths):
        if "identityAttributeConfig" in obj:
            profiles.append(obj)
        elif identity_profiles.is_transform(obj):
            transforms[obj["name"]] = obj
    if not profiles and not transforms:
        raise UsageError("no transform or identity profile found in the given files")
    return profiles, transforms, identity_profiles.load_policies(paths)


def sites(profiles: list[dict], transforms: dict[str, dict], policies: list[dict]) -> list[Site]:
    found = [Site(NAMED, name, _definition(transforms[name]), transforms[name])
             for name in sorted(transforms, key=str.lower)]
    for profile in profiles:
        mappings = (profile.get("identityAttributeConfig") or {}).get("attributeTransforms") or []
        for index, mapping in enumerate(mappings):
            if isinstance(mapping.get("transformDefinition"), dict) and mapping["transformDefinition"]:
                name = f"{profile.get('name', '')}: {mapping.get('identityAttributeName', '')}"
                found.append(Site(PROFILE, name, mapping["transformDefinition"], profile, index))
    for policy in policies:
        for index, item in enumerate(policy.get("fields") or []):
            if isinstance(item.get("transform"), dict) and item["transform"]:
                found.append(Site(POLICY, f"{policy.get('source', '')} / {policy.get('usageType', '')}: "
                                          f"{item.get('name', '')}", item["transform"], policy, index))
    return found


def _definition(transform: dict) -> dict:
    return {"type": transform.get("type", ""), "attributes": attributes_of(transform)}


# -- walking a definition -----------------------------------------------------------------------


def walk(node, path: tuple = ()):
    """``(transform node, path)`` for every transform in a definition, outermost first."""
    if isinstance(node, dict) and "type" in node:
        yield node, path
        for key, value in attributes_of(node).items():
            yield from walk(value, (*path, key))
    elif isinstance(node, list):
        for index, item in enumerate(node):
            yield from walk(item, (*path, index))
    elif isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, (*path, key))


def location(path: tuple) -> str:
    if not path:
        return "the transform itself"
    text = ""
    for part in path:
        text += f"[{part + 1}]" if isinstance(part, int) else (" > " if text else "") + str(part)
    return text


def measure(node, depth: int = 0) -> tuple[int, int]:
    """``(steps, depth)``: the number of transforms in a definition, and how deeply they are nested."""
    if isinstance(node, list):
        found = [measure(item, depth) for item in node]
        return sum(s for s, _ in found), max([d for _, d in found], default=depth)
    if not isinstance(node, dict):
        return 0, depth
    typed = "type" in node
    inner = depth + 1 if typed else depth
    steps, deepest = int(typed), inner
    for value in (attributes_of(node) if typed else node).values():
        s, d = measure(value, inner)
        steps, deepest = steps + s, max(deepest, d)
    return steps, deepest


def canonical(node) -> str:
    return json.dumps(node, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _constant(value) -> bool:
    """A fixed, non-empty value: a plain string or a static transform without a template."""
    if isinstance(value, str):
        return bool(value) and "$" not in value and "#" not in value
    if isinstance(value, dict) and value.get("type") == "static" and set(attributes_of(value)) == {"value"}:
        return _constant(attributes_of(value)["value"])
    return False


def _reads_accounts(node) -> bool:
    return any(n.get("type") == "accountAttribute" for n, _ in walk(node))


def _refreshed(node: dict) -> bool:
    """requiresPeriodicRefresh is set, in the attributes or next to them."""
    return (node.get("requiresPeriodicRefresh") in TRUE
            or attributes_of(node).get("requiresPeriodicRefresh") in TRUE)


def _identities(profile: dict) -> int | None:
    count = profile.get("identityCount")
    return count if isinstance(count, int) and not isinstance(count, bool) else None


# -- analysis -----------------------------------------------------------------------------------


def analyse(profiles: list[dict], transforms: dict[str, dict], policies: list[dict],
            origin: str = "") -> Analysis:
    result = Analysis(sites(profiles, transforms, policies), transforms, origin=origin)
    graph = _calls(result.sites)
    costs = Costs(transforms)
    renames = _final(_duplicates(result, graph) | _aliases(result))
    _check(result, profiles, graph, costs)
    _unused(result, graph)
    _similar(result, graph, renames)
    _complexity(result, graph)
    _optimize(result, renames)
    _profile_costs(result, profiles, graph, renames, costs)
    _plan(result, renames)
    result.findings.sort(key=lambda f: (f["Severity"] != WARNING, CATEGORIES.index(f["Category"]),
                                        f["Object"].lower(), f["Location"]))
    return result


def _calls(all_sites: list[Site]) -> dict[str, list[str]]:
    """Site name -> the named transforms it calls directly."""
    return {site.name if site.kind == NAMED else f"{site.kind}\u0000{site.name}": calls(site.definition)
            for site in all_sites}


def _closure(start: list[str], graph: dict[str, list[str]]) -> set[str]:
    """The named transforms reachable from *start* through the calls of named transforms."""
    found, stack = set(), list(start)
    while stack:
        name = stack.pop()
        if name not in found:
            found.add(name)
            stack += graph.get(name, [])
    return found


def _finding(result: Analysis, severity: str, category: str, site: Site, path: tuple, check: str,
             details: str, recommendation: str, drafted: bool = False) -> None:
    result.findings.append({
        "Severity": severity, "Category": category, "Where": site.kind, "Object": site.name,
        "Location": location(path), "Check": check, "Details": details, "Recommendation": recommendation,
        "In the drafts": "yes" if drafted else "no"})


def _check(result: Analysis, profiles: list[dict], graph: dict[str, list[str]], costs: Costs) -> None:
    transforms = result.transforms
    in_profiles = _closure([name for key, names in graph.items() if key.startswith(PROFILE)
                            for name in names], graph)
    for site in result.sites:
        if site.internal:
            continue
        refreshed = _refreshed(site.definition) or (site.kind == NAMED and _refreshed(site.owner))
        computed = site.kind == PROFILE or (site.kind == NAMED and site.name in in_profiles)
        flagged: list[tuple] = []
        # a firstValid that is a choice of another one: the fallback belongs to the outer one
        nested = {id(value) for node, _ in walk(site.definition) if node.get("type") == "firstValid"
                  for value in attributes_of(node).get("values") or [] if isinstance(value, dict)
                  and value.get("type") == "firstValid"}
        for node, path in walk(site.definition):
            kind, attrs = node.get("type"), attributes_of(node)
            if (computed and (_refreshed(node) or (not path and refreshed))
                    and not any(path[:len(above)] == above for above in flagged)):
                flagged.append(path)
                if not costs.of(node).timed:
                    _finding(result, INFO, PERFORM, site, path, NIGHTLY,
                             "requiresPeriodicRefresh is set, but nothing in it changes on its own (no "
                             "comparison with today's date, no other identity, no rule): the value only "
                             "changes with the data it reads, and a change of that data already "
                             "recomputes it.",
                             "Remove requiresPeriodicRefresh: it makes the nightly identity refresh compute "
                             "the value again for every identity of the profile. After a change to the "
                             "transform itself, refresh the identities of the profile once.")
            if kind == "reference":
                name = reference_name(attrs)
                if name == NO_ID:
                    _finding(result, WARNING, BROKEN, site, path, "Reference without id",
                             "This reference names no transform, so it calls none.",
                             "Give it the name of the transform to call, or remove it.")
                elif transforms and name not in transforms:
                    _finding(result, WARNING, BROKEN, site, path, "Missing transform",
                             f"Calls '{name}', which does not exist.",
                             "Create it, or call an existing transform.")
            elif kind == "lookup" and _without_default(attrs):
                _finding(result, WARNING, RISKY, site, path, "Lookup without default",
                         f"The table has {len(attrs['table'])} value(s) and no 'default': SailPoint "
                         "returns an error for any other value.", "Add a 'default' entry to the table.")
            elif kind == "identityAttribute" and (site.kind == PROFILE or (site.kind == NAMED
                                                                            and site.name in in_profiles)):
                _finding(result, WARNING, RISKY, site, path, "Identity attribute read in a profile mapping",
                         f"Reads the identity attribute '{attrs.get('name', '')}'. SailPoint: this "
                         "transform is not intended within another identity profile attribute's "
                         "calculation; attributes are computed in parallel, so the value may not be ready, "
                         "which can lead to identity exceptions.",
                         "Read the account attribute the value comes from instead; a named transform avoids "
                         "repeating its logic.")
            elif (kind in ("dateCompare", "dateMath") and _uses_now(attrs)
                  and not (refreshed or _refreshed(node))):
                _finding(result, INFO, RISKY, site, path, "Compared with today, not refreshed",
                         "Uses 'now' without requiresPeriodicRefresh: the result may only change when the "
                         "identity is recomputed for another reason. To verify on the tenant.",
                         "Set requiresPeriodicRefresh to true if the value must follow the date.")
            elif kind == "firstValid" and site.kind != POLICY and id(node) not in nested:
                values = attrs.get("values") if isinstance(attrs.get("values"), list) else []
                if values and _reads_accounts(node) and not any(_constant(v) for v in values):
                    _finding(result, INFO, BAD_DATA, site, path, "No fallback value",
                             "Every choice reads data: when they are all empty, so is the result.",
                             "End the list with a fixed value (a default, or an empty string) if an empty "
                             "result is not wanted.")
            elif kind == "rule":
                _finding(result, INFO, MAINTAIN, site, path, "Calls a rule",
                         f"Runs the rule '{attrs.get('name', '')}': code that SailPoint reviews and deploys.",
                         "Keep it if no transform can do the same; SailPoint recommends transforms "
                         "otherwise.")
            elif kind == "static" and VELOCITY_LOGIC.search(str(attrs.get("value", ""))):
                _finding(result, INFO, MAINTAIN, site, path, "Logic in a Velocity template",
                         "The value holds #if/#set/#foreach logic, which is hard to read and to test.",
                         "Prefer conditional, firstValid or lookup transforms where they can do the same.")
    _cycles(result, graph)
    _fixed_sources(result, profiles, graph)


def _without_default(attrs: dict) -> bool:
    return isinstance(attrs.get("table"), dict) and "default" not in attrs["table"]


def _uses_now(attrs: dict) -> bool:
    dates = [attrs.get("firstDate"), attrs.get("secondDate"), attrs.get("expression")]
    return any(isinstance(value, str) and "now" in value.lower() for value in dates)


def _cycles(result: Analysis, graph: dict[str, list[str]]) -> None:
    named = {site.name: site for site in result.sites if site.kind == NAMED}
    state: dict[str, int] = {}
    reported: set[str] = set()

    def visit(name: str, chain: list[str]) -> None:
        state[name] = 1
        for called in graph.get(name, []):
            if called not in named:
                continue
            if state.get(called) == 1:
                loop = chain[chain.index(called):] + [called]
                if called not in reported:
                    reported.update(loop)
                    _finding(result, WARNING, BROKEN, named[called], (), "Circular reference",
                             " > ".join(loop) + ": the transforms call each other in a loop.",
                             "Break the loop: one of them must not call the others.")
            elif not state.get(called):
                visit(called, [*chain, called])
        state[name] = 2

    for name in named:
        if not state.get(name):
            visit(name, [name])


def _fixed_sources(result: Analysis, profiles: list[dict], graph: dict[str, list[str]]) -> None:
    """A named transform used by profiles of different authoritative sources that reads one of them."""
    authoritative = {profile.get("name", ""): (profile.get("authoritativeSource") or {}).get("name", "")
                     for profile in profiles}
    users = {name: {p.get("name", "") for p in found} for name, found in _users(result, graph).items()}
    for site in result.sites:
        if site.kind != NAMED or site.internal or len(users.get(site.name, ())) < 2:
            continue
        sources = {authoritative[p] for p in users[site.name] if authoritative.get(p)}
        read = {attributes_of(n).get("sourceName") for n, _ in walk(site.definition)
                if n.get("type") == "accountAttribute"} - {None}
        if len(sources) > 1 and read and read < sources:
            others = sorted(p for p in users[site.name] if authoritative.get(p) not in read)
            _finding(result, INFO, RISKY, site, (), "Reads a fixed source in several profiles",
                     f"Used by profiles of {', '.join(sorted(sources))} but reads {', '.join(sorted(read))} "
                     f"only: for the identities of {', '.join(others)} the value is usually empty.",
                     "Leave its input implicit (no 'input') so each profile gives its own attribute, or "
                     "use one transform per source.")


def _users(result: Analysis, graph: dict[str, list[str]]) -> dict[str, list[dict]]:
    """Named transform -> the identity profiles whose mappings run it."""
    users: dict[str, dict[int, dict]] = defaultdict(dict)
    for site in result.sites:
        if site.kind == PROFILE:
            for name in _closure(calls(site.definition), graph):
                users[name][id(site.owner)] = site.owner
    return {name: list(found.values()) for name, found in users.items()}


def _duplicates(result: Analysis, graph: dict[str, list[str]]) -> dict[str, str]:
    """Named transforms with the same definition: the copies are replaced by the one kept."""
    references = Counter(name for names in graph.values() for name in names)
    groups: dict[str, list[Site]] = defaultdict(list)
    for site in result.sites:
        if site.kind == NAMED:
            groups[canonical(site.definition)].append(site)
    renames = {}
    for same in groups.values():
        if len(same) < 2:
            continue
        keep = sorted(same, key=lambda s: (not s.internal, -references[s.name], s.name.lower()))[0]
        for site in same:
            if site is keep or site.internal:
                continue
            renames[site.name] = keep.name
            result.duplicates.append({"Transform kept": keep.name, "Duplicate": site.name,
                                      "Type": site.definition.get("type", ""),
                                      "References to change": references[site.name]})
            _finding(result, INFO, SIMPLIFY, site, (), "Duplicate transform",
                     f"Same definition as '{keep.name}'.",
                     f"Call '{keep.name}' instead, then delete this transform.", drafted=True)
    return renames


def _aliases(result: Analysis) -> dict[str, str]:
    """Named transforms that only call another one: they are replaced by the one they call."""
    renames = {}
    for site in result.sites:
        if site.kind != NAMED or site.internal or site.definition.get("type") != "reference":
            continue
        attrs = site.definition["attributes"]
        target = reference_name(attrs)
        if set(attrs) == {"id"} and target != NO_ID and target != site.name and target in result.transforms:
            renames[site.name] = target
            _finding(result, INFO, SIMPLIFY, site, (), "Transform that only calls another one",
                     f"Its whole definition is a call to '{target}'.",
                     f"Call '{target}' directly, then delete this transform.", drafted=True)
    return renames


def _final(renames: dict[str, str]) -> dict[str, str]:
    """Each replaced transform -> the transform that stays (a -> b -> c becomes a -> c)."""
    final = {}
    for name in renames:
        seen, target = {name}, renames[name]
        while target in renames and target not in seen:
            seen.add(target)
            target = renames[target]
        final[name] = target
    return final


def _unused(result: Analysis, graph: dict[str, list[str]]) -> None:
    used = _closure([n for key, names in graph.items() if "\u0000" in key for n in names], graph)
    callers: dict[str, list[str]] = defaultdict(list)
    for key, names in graph.items():
        for name in names:
            callers[name].append(key.split("\u0000")[-1])
    for site in result.sites:
        if site.kind != NAMED or site.internal or site.name in used:
            continue
        if callers.get(site.name):
            reason = ("only called by transforms that are not used either: "
                      + ", ".join(sorted(callers[site.name])))
        else:
            reason = "called by no identity profile, transform or provisioning policy"
        result.unused.append({"Transform": site.name, "Type": site.definition.get("type", ""), "Why": reason})
        _finding(result, INFO, MAINTAIN, site, (), "Not used", _sentence(reason),
                 "Delete it after checking that no form, workflow or other configuration uses it (they are "
                 "not read here).")


def _similar(result: Analysis, graph: dict[str, list[str]], renames: dict[str, str]) -> None:
    """Named transforms whose definitions differ only by their input, an attribute read: one transform
    without input can replace them, each caller giving the input (not in the drafts). A transform whose
    input is itself logic keeps it: its callers would have to repeat that logic."""
    gone = set(renames) | {row["Transform"] for row in result.unused}
    callers = Counter(name for names in graph.values() for name in names)
    groups: dict[str, list[Site]] = defaultdict(list)
    for site in result.sites:
        attrs = site.definition.get("attributes", {}) if site.kind == NAMED else {}
        if (site.kind != NAMED or site.name in gone or (site.internal and "input" in attrs)
                or ("input" in attrs and not _read(attrs["input"]))):
            continue                     # SailPoint's own transforms cannot lose their input
        logic = {"type": site.definition.get("type", ""),
                 "attributes": {key: value for key, value in attrs.items() if key != "input"}}
        groups[canonical(logic)].append(site)
    for same in groups.values():
        given = [site for site in same if "input" in site.definition["attributes"]]
        if len(given) < 2 and not (given and len(given) < len(same)):
            continue
        generic = [site for site in same if "input" not in site.definition["attributes"]]
        keep = sorted(generic or given, key=lambda s: (not s.internal, -callers[s.name], s.name.lower()))[0]
        kept_input = "input" in keep.definition["attributes"]
        for site in sorted(same, key=lambda s: (s is not keep, s.name.lower())):
            value = site.definition["attributes"].get("input")
            if site is keep:
                change = (f"kept without its input, which its {callers[keep.name]} caller(s) then give"
                          if kept_input else "kept as it is")
            else:
                change = (f"replaced by a call of '{keep.name}' with this input in its {callers[site.name]} "
                          "caller(s), then deleted")
            result.similar.append({"Transform kept": keep.name, "Transform": site.name,
                                   "Type": site.definition.get("type", ""),
                                   "Input": "(given by the caller)" if value is None else _short(value),
                                   "Change": change})
            if site is keep:
                continue
            first = (f" First remove the input of '{keep.name}' and give it in its {callers[keep.name]} "
                     "caller(s)." if kept_input else "")
            _finding(result, INFO, SIMPLIFY, site, (), SAME_LOGIC,
                     f"Same logic as '{keep.name}', applied to another input: {_short(value)}.",
                     f"Call '{keep.name}' with this input (the 'input' of the reference, or the source "
                     f"attribute of the identity profile mapping), then delete this transform.{first}")


def _read(value) -> bool:
    """An account or identity attribute read, with no transform inside."""
    return (isinstance(value, dict) and value.get("type") in ("accountAttribute", "identityAttribute")
            and measure(value)[0] == 1)


def _complexity(result: Analysis, graph: dict[str, list[str]]) -> None:
    callers = Counter(name for names in graph.values() for name in names)
    users = _users(result, graph)
    counted = any(_identities(site.owner) is not None for site in result.sites if site.kind == PROFILE)
    for site in result.sites:
        if site.kind != NAMED or site.internal:
            continue
        steps, depth = measure(site.definition)
        size = len(json.dumps(site.owner, ensure_ascii=False).encode("utf-8"))
        called = _closure(graph.get(site.name, []), graph)
        identities = sum(_identities(p) or 0 for p in users.get(site.name, [])) if counted else ""
        result.complexity.append({"Transform": site.name, "Type": site.definition.get("type", ""),
                                  "Steps": steps, "Depth": depth, "Named transforms called": len(called),
                                  "Places used": callers[site.name], "Identities": identities,
                                  "Size (KB)": round(size / 1024, 1)})
        if size > 0.9 * SIZE_LIMIT:
            _finding(result, WARNING, RISKY, site, (), "Close to the size limit",
                     f"{size / 1024:.0f} KB of the 400 KB SailPoint allows.",
                     "Move parts of it to named transforms.")
        elif depth > DEEP or steps > MANY_STEPS:
            _finding(result, INFO, MAINTAIN, site, (), "Complex transform",
                     f"{steps} steps nested {depth} deep: hard to read and to test.",
                     "Split it into small named transforms (SailPoint: start with small building blocks).")
    result.complexity.sort(key=lambda r: (-r["Steps"], -r["Depth"], r["Transform"].lower()))


# -- optimized drafts ---------------------------------------------------------------------------


def simplify(node, renames: dict[str, str], notes: list[str]):
    """The definition with the rewrites that keep its result (see the module documentation); what was
    changed is added to *notes*."""
    if isinstance(node, list):
        return [simplify(item, renames, notes) for item in node]
    if not isinstance(node, dict):
        return node
    if "type" not in node:
        return {key: simplify(value, renames, notes) for key, value in node.items()}
    node = dict(node)
    if isinstance(node.get("attributes"), dict):
        node["attributes"] = {key: simplify(value, renames, notes)
                              for key, value in node["attributes"].items()}
    kind, attrs = node["type"], attributes_of(node)
    if kind == "reference" and attrs.get("id") in renames:
        notes.append(f"calls '{renames[attrs['id']]}' instead of '{attrs['id']}', which repeats it")
        attrs["id"] = renames[attrs["id"]]
    elif kind in ("firstValid", "concat") and isinstance(attrs.get("values"), list):
        values = []
        for value in attrs["values"]:
            if _plain(value, kind) and attributes_of(value).get("ignoreErrors") == attrs.get("ignoreErrors"):
                notes.append(f"{kind} inside {kind} flattened")
                values += attributes_of(value)["values"]
            else:
                values.append(value)
        if kind == "firstValid":
            for index, value in enumerate(values[:-1]):
                if _constant(value):
                    notes.append(f"{len(values) - index - 1} choice(s) after the fixed value {_short(value)} "
                                 "removed: they are never used")
                    values = values[:index + 1]
                    break
        attrs["values"] = values
        if len(values) == 1 and set(attrs) == {"values"}:
            notes.append(f"{kind} of a single value replaced by the value")
            value = values[0]
            if isinstance(value, dict):
                return value
            return {"type": "static", "attributes": {"value": str(value)}}
    elif kind in CASE_CHANGES and isinstance(attrs.get("input"), dict):
        inner = attrs["input"]
        if (inner.get("type") in CASE_CHANGES[kind] and set(attributes_of(inner)) == {"input"}
                and set(inner) <= {"type", "attributes"}):
            notes.append(f"{inner['type']} removed: the {kind} above it gives the same result")
            attrs["input"] = attributes_of(inner)["input"]
    elif (kind == "dateFormat" and attrs.get("inputFormat")
          and attrs.get("inputFormat") == attrs.get("outputFormat") and isinstance(attrs.get("input"), dict)
          and set(attrs) <= {"input", "inputFormat", "outputFormat"}):
        notes.append(f"dateFormat from {attrs['inputFormat']} to the same format removed")
        return attrs["input"]
    return node


def _plain(value, kind: str) -> bool:
    """A *kind* transform with nothing but its values (it can be merged into its parent)."""
    return (isinstance(value, dict) and value.get("type") == kind and set(value) <= {"type", "attributes"}
            and set(attributes_of(value)) <= {"values", "ignoreErrors"}
            and isinstance(attributes_of(value).get("values"), list))


def _sentence(text: str) -> str:
    """*text* as a sentence; a transform type at the start keeps its case (firstValid, dateFormat)."""
    first = text.split(" ", 1)[0]
    return (text if first in TRANSFORM_TYPES else text[0].upper() + text[1:]) + "."


def _short(value) -> str:
    return identity_profiles.logic(value) if isinstance(value, dict) else f'"{value}"'


def _optimize(result: Analysis, renames: dict[str, str]) -> None:
    current: dict[int, dict] = {}
    notes: dict[int, list[str]] = {}
    for index, site in enumerate(result.sites):
        if site.internal:
            continue
        found: list[str] = []
        current[index] = simplify(copy.deepcopy(site.definition), renames, found)
        notes[index] = found
    _share(result, current, notes, renames)
    for index, site in enumerate(result.sites):
        if index not in current or canonical(current[index]) == canonical(site.definition):
            continue
        if site.kind == NAMED and site.name in renames:
            continue                     # replaced by the transform it repeats: to delete, not to update
        for note in dict.fromkeys(notes[index]):
            result.changes.append({"Where": site.kind, "Object": site.name, "Change": note,
                                   "Before": identity_profiles.logic(site.definition),
                                   "After": identity_profiles.logic(current[index])})
            if note.startswith("calls '"):
                continue                 # reported with the duplicate or the transform it calls
            check = ("Copy of a named transform" if note.startswith("moved to a call") else
                     "Repeated block" if note.startswith("moved to the new") else "Can be simplified")
            _finding(result, INFO, SIMPLIFY, site, (), check, _sentence(note), "Apply the optimized draft.",
                     drafted=True)
        _draft(result, site, current[index])
    result.optimized = current
    for name, kept in sorted(renames.items()):
        result.to_delete.append({"Transform": name, "Why": f"repeats '{kept}', which is called instead"})


def _share(result: Analysis, current: dict[int, dict], notes: dict[int, list[str]],
           renames: dict[str, str]) -> None:
    """Blocks repeated in several places, or copies of a named transform, become calls to a named
    transform. The biggest block is handled first, until no block repeats."""
    existing = {canonical(current.get(i, site.definition)): site.name for i, site in enumerate(result.sites)
                if site.kind == NAMED and site.name not in renames}
    used_names = set(result.transforms)
    while True:
        places: dict[str, list[int]] = defaultdict(list)
        sizes: dict[str, int] = {}
        examples: dict[str, dict] = {}
        for index, definition in list(current.items()) + [(-1 - n, s["definition"]) for n, s in
                                                           enumerate(result.shared)]:
            root = index >= 0 and result.sites[index].kind == NAMED or index < 0
            for node, path in walk(definition):
                if (root and not path) or node.get("type") == "reference":
                    continue
                key = canonical(node)
                steps = sizes.setdefault(key, measure(node)[0])
                if steps >= 2:
                    places[key].append(index)
                    examples.setdefault(key, node)
        candidates = [key for key, where in places.items()
                      if key in existing or len(where) >= 3 or (len(where) >= 2 and sizes[key] >= 3)]
        if not candidates:
            return
        key = max(candidates, key=lambda k: (sizes[k], len(places[k])))
        node = copy.deepcopy(examples[key])
        if key in existing:
            name = existing[key]
            note = f"moved to a call of the named transform '{name}', which is the same"
        else:
            name = _new_name(node, used_names)
            used_names.add(name)
            existing[key] = name
            note = f"moved to the new named transform '{name}', shared with other places"
            result.shared.append({"name": name, "type": node["type"], "definition": node,
                                  "occurrences": len(places[key]), "steps": sizes[key]})
        reference = {"type": "reference", "attributes": {"id": name}}
        for index in set(places[key]):
            if index >= 0:
                named = result.sites[index].kind == NAMED
                current[index] = _replace(current[index], key, reference, keep_root=named)
                notes[index].append(note)
            else:
                shared = result.shared[-1 - index]
                shared["definition"] = _replace(shared["definition"], key, reference, keep_root=True)


def _replace(node, key: str, reference: dict, keep_root: bool):
    if isinstance(node, list):
        return [_replace(item, key, reference, False) for item in node]
    if not isinstance(node, dict):
        return node
    if "type" in node and not keep_root and canonical(node) == key:
        return copy.deepcopy(reference)
    return {k: _replace(v, key, reference, False) for k, v in node.items()}


def _new_name(node: dict, used: set[str]) -> str:
    base = f"Shared {node['type']}"
    number = 1
    while f"{base} {number}" in used:
        number += 1
    return f"{base} {number}"


def _draft(result: Analysis, site: Site, definition: dict) -> None:
    if site.kind == NAMED:
        draft = {**{k: v for k, v in site.owner.items() if k not in ("type", "attributes")},
                 "type": definition.get("type", ""), "attributes": attributes_of(definition)}
        result.drafts[f"transforms/{_file(site.name)}.json"] = draft
    elif site.kind == PROFILE:
        path = f"identity-profiles/{_file(site.owner.get('name', ''))}.json"
        draft = result.drafts.setdefault(path, copy.deepcopy(site.owner))
        mappings = draft["identityAttributeConfig"]["attributeTransforms"]
        mappings[site.index]["transformDefinition"] = definition
    else:
        name = f"{site.owner.get('source', '')} - {site.owner.get('usageType', '')}"
        path = f"provisioning-policies/{_file(name)}.json"
        draft = result.drafts.setdefault(path, {key: copy.deepcopy(value) for key, value in site.owner.items()
                                                if key != "source"})
        draft["fields"][site.index]["transform"] = definition


def _file(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*]', "_", name).strip() or "unnamed"


# -- platform view ------------------------------------------------------------------------------


def _profile_costs(result: Analysis, profiles: list[dict], graph: dict[str, list[str]],
                   renames: dict[str, str], costs: Costs) -> None:
    """The transform steps each identity profile runs for one identity and for all of them, now and with
    the optimized drafts."""
    after = Costs(_transforms_after(result, renames))
    for profile in profiles:
        mappings = [(index, site) for index, site in enumerate(result.sites)
                    if site.kind == PROFILE and site.owner is profile]
        now = [costs.of(site.definition) for _, site in mappings]
        steps = sum(cost.steps for cost in now)
        identities = _identities(profile)
        refreshed = sum(cost.refreshed for cost in now)
        named = _closure([name for _, site in mappings for name in calls(site.definition)], graph)
        result.profile_costs.append({
            "Profile": profile.get("name", ""),
            "Authoritative source": (profile.get("authoritativeSource") or {}).get("name", ""),
            "Identities": "" if identities is None else identities,
            "Attributes mapped": len(mappings),
            "Named transforms run": len(named & set(result.transforms)),
            "Steps per identity": steps,
            "After the drafts": sum(after.of(result.optimized.get(index, site.definition)).steps
                                    for index, site in mappings),
            "Steps per refresh": "" if identities is None else steps * identities,
            "Refreshed every night": (f"yes, {refreshed} attribute(s)" if refreshed else
                                      "yes" if profile.get("hasTimeBasedAttr") in TRUE else "no"),
            "Rules": sum(cost.rules for cost in now),
            "Other identities read": sum(cost.others for cost in now)})
    result.profile_costs.sort(key=lambda r: (-(r["Steps per refresh"] or 0), -r["Steps per identity"],
                                             r["Profile"].lower()))


def _transforms_after(result: Analysis, renames: dict[str, str]) -> dict[str, dict]:
    """The named transforms as the optimized drafts leave them."""
    after = {site.name: {**site.owner, **result.optimized.get(index, site.definition)}
             for index, site in enumerate(result.sites) if site.kind == NAMED and site.name not in renames}
    for block in result.shared:
        after[block["name"]] = {"name": block["name"], **block["definition"]}
    return after


def _plan(result: Analysis, renames: dict[str, str]) -> None:
    """The suggestions for the whole tenant, in the order to apply them, with their effect."""
    custom = {site.name for site in result.sites if site.kind == NAMED and not site.internal}
    unused = {row["Transform"] for row in result.unused}
    duplicates = {row["Duplicate"] for row in result.duplicates} - unused
    aliases = set(renames) - duplicates - unused
    merged = set(result.merged)
    after = len(custom - unused - duplicates - aliases - merged) + len(result.shared)
    result.custom = (len(custom), after)

    def add(suggestion: str, effect: str, see: str) -> None:
        result.plan.append({"Step": len(result.plan) + 1, "Suggestion": suggestion, "Effect": effect,
                            "See": see})

    if result.warnings:
        add("Fix the warnings first", f"{result.warnings} warning(s): transforms that fail, or give a wrong "
            "value, on some data", "Findings")
    if unused:
        add("Delete the named transforms not used", f"-{len(unused)} named transform(s), after checking that "
            "no form, workflow or other configuration calls them", "Not used")
    if duplicates:
        add("Replace the duplicates by the transform they repeat", f"-{len(duplicates)} named transform(s); "
            "done in the optimized drafts", "Duplicates")
    if aliases:
        add("Call directly the transforms only called through another one", f"-{len(aliases)} named "
            "transform(s); done in the optimized drafts", "Findings")
    if merged:
        groups = len({row["Transform kept"] for row in result.similar})
        add("Keep one transform per logic, without input, each caller giving the input",
            f"-{len(merged)} named transform(s): {len(merged) + groups} transforms become {groups}",
            "Same logic")
    if result.shared:
        places = sum(block["occurrences"] for block in result.shared)
        add("Move the repeated blocks to shared named transforms", f"+{len(result.shared)} named "
            f"transform(s): logic written in {places} places is defined once; done in the optimized drafts",
            "Repeated blocks")
    now = sum(row["Steps per identity"] for row in result.profile_costs)
    then = sum(row["After the drafts"] for row in result.profile_costs)
    if then < now:
        counted = [row for row in result.profile_costs if row["Identities"] != ""]
        total = sum(row["Steps per refresh"] for row in counted)
        saved = total - sum(row["After the drafts"] * row["Identities"] for row in counted)
        add("Apply the simplifications of the optimized drafts", f"{now - then} transform step(s) fewer per "
            "identity, over all the profiles" + (f"; {saved:,} fewer for a refresh of every identity "
                                                 f"(-{_percent(saved, total)})" if total else ""),
            "Cost by profile, Changes")
    nightly = sum(f["Check"] == NIGHTLY for f in result.findings)
    if nightly:
        add("Remove requiresPeriodicRefresh where nothing changes on its own", f"{nightly} place(s) the "
            "nightly identity refresh computes again for nothing", "Findings")
    rules = sum(row["Rules"] for row in result.profile_costs)
    if rules:
        add("Replace the rules of the identity profiles by transforms where possible",
            f"{rules} rule call(s) run for every identity of their profile: code SailPoint must review and "
            "deploy", "Findings")
    change = ""
    if custom and after != len(custom):
        change = f" ({'+' if after > len(custom) else '-'}{_percent(abs(after - len(custom)), len(custom))})"
    result.plan.append({"Step": "", "Suggestion": "Result", "See": "",
                        "Effect": f"{len(custom)} custom named transform(s) now, {after} after these steps"
                                  f"{change}"})


def _percent(part: int, whole: int) -> str:
    share = 100 * part / whole
    return f"{share:.0f}%" if share >= 1 else "less than 1%"


# -- report -------------------------------------------------------------------------------------

READ_ME = [
    ("What is this?", "What is broken, risky, repeated or more complex than needed in the transforms: the "
                      "named transforms, the identity profile mappings and the fields of the provisioning "
                      "policies, and a plan to optimize them across the tenant. Nothing was changed in the "
                      "tenant."),
    ("Optimization plan", "The suggestions for the whole tenant, in the order to apply them, with their "
                          "effect on the number of custom named transforms and on the work of the identity "
                          "refresh. SailPoint's own transforms (internal) are not counted."),
    ("Severity", "Warning: probably wrong, or fails on some data. Info: works, but can be simpler or safer."),
    ("Categories", "Broken (fails or calls nothing), Risky (may give a wrong or stale value), Bad data (no "
                   "plan when the data is missing), Performance (more work at each identity refresh than "
                   "needed), Simplify (same result with less), Maintenance (unused, complex, rules)."),
    ("Cost by profile", "Steps per identity: the transforms the mappings of the profile run for one "
                        "identity, those of the named transforms they call included (the calls themselves "
                        "are not counted). Steps per refresh: the same for every identity of the profile. An "
                        "estimate to compare profiles and measure the drafts, not a measured time: a rule or "
                        "a lookup does not cost the same as a lower. A configuration snapshot holds no "
                        "identity counts."),
    ("Nightly refresh", "requiresPeriodicRefresh makes the nightly identity refresh compute a value again "
                        "even when nothing it reads has changed. It is needed only for values that change on "
                        "their own: comparisons with today's date, attributes of another identity, rules. "
                        "The profile shows it as hasTimeBasedAttr; SailPoint does not document whether one "
                        "such attribute makes the whole profile computed again, so remove it wherever it is "
                        "not needed."),
    ("Same logic", "Named transforms whose definitions differ only by their input. A transform without "
                   "'input' takes the input of its caller: the 'input' of the reference that calls it, or "
                   "the source attribute chosen in the identity profile mapping. One of them can replace "
                   "the others. Not done in the drafts: each caller must be given the input."),
    ("In the drafts", "'yes' when the optimized drafts already contain the change."),
    ("Optimized drafts", "The 'optimized' folder holds the transforms, identity profiles and provisioning "
                         "policies rewritten with changes that give the same result. Review them, then: "
                         "create the new 'Shared' transforms first, update the transforms, then the identity "
                         "profiles and provisioning policies, check a few identities with the identity "
                         "profile preview, and delete the transforms listed in delete.csv. changes.csv lists "
                         "every change."),
    ("Changes made in the drafts", "Duplicates of a named transform and transforms that only call another "
                                   "one are replaced by the transform they repeat; nested firstValid and "
                                   "concat are flattened; a firstValid or concat of one value is replaced by "
                                   "the value; firstValid choices after a fixed value are dropped; a lower, "
                                   "upper or trim below another case change is dropped; a dateFormat to the "
                                   "same format is dropped; blocks repeated in several places (3 places, or "
                                   "2 for blocks of 3 steps or more) become calls to a new named transform."),
    ("Not changed", "SailPoint's own transforms (internal), rules, and anything whose result could differ: "
                    "missing defaults and fallbacks are only recommended."),
    ("Limits", "Forms, workflows and other configurations that may call a transform are not read: check them "
               "before deleting a transform reported as not used."),
]
WIDE = {"Details", "Recommendation", "Object", "Location", "Change", "Before", "After", "Why", "Logic",
        "Suggestion", "Effect", "Input"}


def _tile(label: str, count: int, colour: str) -> tuple[str, str, str]:
    return label, str(count), colour if count else dashboard.GREEN


def build_dashboard(result: Analysis, title: str = "") -> dashboard.Dashboard:
    named = [site for site in result.sites if site.kind == NAMED]
    internal = sum(site.internal for site in named)
    by_category = Counter(f["Category"] for f in result.findings)
    by_check = Counter(f["Check"] for f in result.findings)
    custom, after = result.custom
    kpis = [
        ("Named transforms", f"{len(named)}" + (f"  ({internal} internal)" if internal else ""),
         dashboard.BLUE),
        ("Custom transforms after the clean-up",
         f"{after}" + (f"  (now {custom})" if after != custom else ""),
         dashboard.GREEN if after < custom else dashboard.GREY),
        _tile("Warnings", result.warnings, dashboard.RED),
        ("Changes in the optimized drafts", str(len(result.changes)), dashboard.BLUE),
        _tile("Duplicates or same logic", len(result.duplicates) + len(result.merged), dashboard.AMBER),
        _tile("Repeated blocks to share", len(result.shared), dashboard.AMBER),
        _tile("Transforms not used", len(result.unused), dashboard.AMBER),
        _tile("Recomputed every night without need", by_check[NIGHTLY], dashboard.AMBER),
    ]
    charts = []
    labels = [category for category in CATEGORIES if by_category[category]]
    if labels:
        charts.append(dashboard.Chart("Findings by category", "doughnut", labels,
                                      [("Findings", [by_category[c] for c in labels], "")]))
    heaviest = [row for row in result.profile_costs if row["Steps per identity"]][:10]
    if heaviest:
        charts.append(dashboard.Chart(
            "Transform steps per identity, by profile", "bar", [row["Profile"] for row in heaviest],
            [("Now", [row["Steps per identity"] for row in heaviest], dashboard.BLUE),
             ("With the drafts", [row["After the drafts"] for row in heaviest], dashboard.GREEN)]))
    top = by_check.most_common(10)
    if top:
        charts.append(dashboard.Chart("Most frequent findings", "bar", [check for check, _ in top],
                                      [("Findings", [count for _, count in top], dashboard.BLUE)]))
    biggest = result.complexity[:10]
    if biggest:
        charts.append(dashboard.Chart("Biggest named transforms (steps)", "bar",
                                      [row["Transform"] for row in biggest],
                                      [("Steps", [row["Steps"] for row in biggest], "8E24AA")]))
    shared = [{"New named transform": block["name"], "Type": block["type"], "Places": block["occurrences"],
               "Steps": block["steps"], "Logic": identity_profiles.logic(block["definition"])}
              for block in result.shared]
    tables = [
        dashboard.Table("Optimization plan", result.plan, wide=WIDE),
        dashboard.Table("Findings", result.findings, wide=WIDE,
                        fills={"Severity": identity_profiles.SEVERITY_FILLS}),
        dashboard.Table("Cost by profile", result.profile_costs, wide=WIDE),
        dashboard.Table("Duplicates", result.duplicates, wide=WIDE),
        dashboard.Table("Same logic", result.similar, wide=WIDE),
        dashboard.Table("Repeated blocks", shared, wide=WIDE),
        dashboard.Table("Not used", result.unused, wide=WIDE),
        dashboard.Table("Complexity", result.complexity, wide=WIDE),
        dashboard.Table("Changes", result.changes, wide=WIDE),
    ]
    sections = [
        dashboard.Section("Optimization plan", result.plan, ["Step", "Suggestion", "Effect"]),
        dashboard.Section("Findings", result.findings,
                          ["Severity", "Category", "Object", "Check", "Details"]),
        dashboard.Section("Cost by profile", result.profile_costs,
                          ["Profile", "Identities", "Steps per identity", "After the drafts",
                           "Steps per refresh", "Refreshed every night"]),
        dashboard.Section("Changes in the optimized drafts", result.changes, ["Object", "Change", "After"]),
        dashboard.Section("Same logic, other input", result.similar,
                          ["Transform kept", "Transform", "Input"]),
        dashboard.Section("Not used", result.unused, ["Transform", "Type", "Why"]),
        dashboard.Section("Biggest named transforms", result.complexity[:30],
                          ["Transform", "Type", "Steps", "Depth", "Places used", "Identities", "Size (KB)"]),
    ]
    return dashboard.Dashboard(
        title or "Transform analysis", dashboard.subtitle(result.origin), kpis, charts, tables, sections,
        READ_ME, colours={**CATEGORY_COLOURS, WARNING: dashboard.RED, INFO: dashboard.BLUE},
        wide_pdf={"Object", "Details", "Change", "After", "Why", "Transform", "Suggestion", "Effect",
                  "Input"},
        alert=lambda row: row.get("Severity") == WARNING)


def write_outputs(result: Analysis, folder: str, formats=("xlsx", "pdf"), drafts: bool = True,
                  title: str = "") -> list[str]:
    """The workbook and PDF in *folder*, and the optimized drafts in *folder*/optimized; returns the
    files written."""
    os.makedirs(folder, exist_ok=True)
    written = []
    report = build_dashboard(result, title)
    if "xlsx" in formats:
        written.append(dashboard.write_xlsx(report, os.path.join(folder, "transform-analysis.xlsx")))
    if "pdf" in formats:
        written.append(dashboard.write_pdf(report, os.path.join(folder, "transform-analysis.pdf")))
    if not drafts:
        return written
    optimized = os.path.join(folder, "optimized")
    files = dict(result.drafts)
    for block in result.shared:
        files[f"transforms/{_file(block['name'])}.json"] = {
            "name": block["name"], "type": block["type"], "attributes": attributes_of(block["definition"])}
    for relative, data in files.items():
        path = os.path.join(optimized, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=4)
        written.append(path)
    os.makedirs(optimized, exist_ok=True)
    columns = ["Where", "Object", "Change", "Before", "After"]
    written.append(write_table(os.path.join(optimized, "changes.csv"), columns,
                               [[change[c] for c in columns] for change in result.changes], ";"))
    written.append(write_table(os.path.join(optimized, "delete.csv"), ["Transform", "Why"],
                               [[row["Transform"], row["Why"]] for row in result.to_delete], ";"))
    return written
