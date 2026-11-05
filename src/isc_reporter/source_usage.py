"""Where a source is used besides identity profiles, transforms and criteria: its access profiles and the
roles, dimensions and lifecycle states that grant them or its entitlements, the SoD policies on its
entitlements, the lifecycle states that act on its accounts, the provisioning policies of the other
sources that read it or an identity attribute computed from it, the workflows and connector rules that
name it, and the password policies that cover it.

The source migration analysis lists them in its Impacts sheet, with what to do with each one when the
source is replaced, and the configuration of the source itself to recreate on the new one. Nothing is
changed in the tenant.
"""

from __future__ import annotations

import logging
import re

from isc_reporter import attribute_usage, criteria, lifecycle
from isc_reporter.identity_profiles import INFO, WARNING, logic

log = logging.getLogger("isc_reporter")

ACCESS_PROFILE, SOD, LIFECYCLE = "Access profile", "SoD policy", "Lifecycle state"
POLICY, WORKFLOW, RULE, PASSWORD = "Provisioning policy", "Workflow", "Connector rule", "Password policy"
TYPE_ORDER = (criteria.ROLE, criteria.DIMENSION, ACCESS_PROFILE, SOD, LIFECYCLE, criteria.CORRELATION,
              criteria.MANAGER, POLICY, criteria.SEGMENT, WORKFLOW, RULE, PASSWORD)
#: What to do with the objects of each type, for the Overview sheet.
WHAT_TO_DO = {
    criteria.ROLE: "Grant the access of the new source instead of the old one; rewrite the criteria that "
                   "read the old source; check those on impacted identity attributes.",
    criteria.DIMENSION: "As for roles.",
    ACCESS_PROFILE: "Recreate them on the new source with its entitlements, then grant the new ones instead.",
    SOD: "Replace the entitlements of the old source by those of the new one.",
    LIFECYCLE: "Recreate the states of the new identity profile; act on the accounts and access of the new "
               "source instead of the old one.",
    criteria.CORRELATION: "Recreate the correlation of the old source on the new one; check the others that "
                          "use impacted identity attributes.",
    criteria.MANAGER: "Recreate the manager correlation of the old source on the new one; check the others.",
    POLICY: "Rewrite the fields that read the old source; compare the values of those reading impacted "
            "identity attributes.",
    criteria.SEGMENT: "Check that the new values of the identity attributes still match the segment.",
    WORKFLOW: "Point the workflows that name the old source to the new one.",
    RULE: "Attach the rules of the old source to the new one if still needed; review the code that names it.",
    PASSWORD: "Add the new source to the policy, if ISC manages its passwords.",
}
#: Attribute of :class:`criteria.Objects` -> type of :data:`isc_reporter.snapshot.KINDS` it is read as.
KINDS = {"access_profiles": "access-profiles", "sod_policies": "sod-policies", "workflows": "workflows",
         "rules": "connector-rules", "password_policies": "password-policies"}
#: Rules a source refers to by reference; the connector attributes name others (beforeRule, buildMapRule...).
SOURCE_RULES = ("beforeProvisioningRule", "accountCorrelationRule", "managerCorrelationRule")
RULE_KEY = re.compile(r"rule", re.I)
FLAGS = frozenset({"true", "false", "yes", "no", "none", "null"})
GONE = "once the source is gone"


# -- reading ------------------------------------------------------------------------------------


def fetch(isc, objects: criteria.Objects, profiles: list[dict], old) -> None:
    """Add to *objects* what the tenant holds besides criteria: access profiles, SoD policies, workflows,
    connector rules, password policies and the entitlements of the *old* source, the lifecycle states of
    *profiles* (``lifecycleStates``) and the provisioning policies of the sources
    (``provisioningPolicies``). What cannot be read is noted, and the analysis goes on."""
    from isc_reporter.api import error_message, list_json
    from isc_reporter.query import quote
    from isc_reporter.search import search_all
    from isc_reporter.snapshot import KINDS as SNAPSHOT_KINDS
    from isc_reporter.snapshot import _attach, _list

    for attribute, name in KINDS.items():
        kind = SNAPSHOT_KINDS[name]
        try:
            setattr(objects, attribute, [obj for obj in _list(isc, kind) if isinstance(obj, dict)])
        except Exception as exc:
            objects.notes.append(f"{kind.label} not read ({error_message(exc)}).")
    states = isc.lifecycle_states.get_lifecycle_states_v1_without_preload_content
    _attach([p for p in profiles if p.get("id")], "lifecycleStates",
            lambda p: list_json(states, identity_profile_id=p["id"]), objects.notes, "Lifecycle states",
            "identity profile")
    if objects.sources is not None:
        policies = isc.sources.list_provisioning_policies_v1_without_preload_content
        _attach(objects.sources, "provisioningPolicies",
                lambda s: list_json(policies, source_id=s.get("id", "")), objects.notes,
                "Provisioning policies", "source")
    if old.id:
        try:
            objects.entitlements = search_all(isc, "entitlements", f"source.id:{quote(old.id)}",
                                              sort_field=None, include_nested=False)
        except Exception as exc:
            objects.notes.append(f"Entitlements of {old.name} not read ({error_message(exc)}): SoD "
                                 "policies and roles are checked against the entitlements of its access "
                                 "profiles only.")
    counts = ["not" if found is None else str(len(found)) for found in (
        objects.access_profiles, objects.sod_policies, objects.workflows, objects.rules,
        objects.password_policies, objects.entitlements)]
    log.info("Also read: %s access profile(s), %s SoD polic(ies), %s workflow(s), %s connector rule(s), "
             "%s password polic(ies), %s entitlement(s) of the old source", *counts)


def load_files(paths: list[str], objects: criteria.Objects) -> None:
    """The same from exported files: a snapshot holds them all, a configuration export some. Profiles and
    sources bring their lifecycle states and provisioning policies when the files have them. A type
    absent from the files is not analysed."""
    from isc_reporter.identity_profiles import iter_objects
    from isc_reporter.snapshot import KINDS as SNAPSHOT_KINDS

    found: dict[str, list[dict]] = {attribute: [] for attribute in KINDS}
    for obj in iter_objects(paths):
        attribute = _kind(obj)
        if attribute:
            found[attribute].append(obj)
    for attribute, objs in found.items():
        setattr(objects, attribute, objs or None)
        if not objs:
            objects.notes.append(f"{SNAPSHOT_KINDS[KINDS[attribute]].label} not in the exported files: not "
                                 "analysed.")
    objects.notes.append("Entitlements are not in exported files: SoD policies and roles are checked against "
                         "the entitlements of the access profiles of the old source only.")


def _kind(obj: dict) -> str | None:
    """The attribute of :class:`criteria.Objects` an exported object belongs to, if one of :data:`KINDS`."""
    if "sourceCode" in obj:
        return "rules"
    if "definition" in obj and "trigger" in obj:
        return "workflows"
    if "conflictingAccessCriteria" in obj or "policyQuery" in obj:
        return "sod_policies"
    if "sourceIds" in obj and ("minLength" in obj or "maxLength" in obj):
        return "password_policies"
    if "entitlements" in obj and "source" in obj and "membership" not in obj:
        return "access_profiles"
    return None


def analysed(objects: criteria.Objects, profiles: list[dict]) -> dict[str, bool]:
    """For each type of :data:`TYPE_ORDER`, whether its objects were read."""
    sources = objects.sources is not None
    return {criteria.ROLE: objects.roles is not None, criteria.DIMENSION: objects.roles is not None,
            ACCESS_PROFILE: objects.access_profiles is not None, SOD: objects.sod_policies is not None,
            LIFECYCLE: any(isinstance(p.get("lifecycleStates"), list) for p in profiles),
            criteria.CORRELATION: sources, criteria.MANAGER: sources,
            POLICY: any("provisioningPolicies" in s for s in objects.sources or []),
            criteria.SEGMENT: objects.segments is not None, WORKFLOW: objects.workflows is not None,
            RULE: objects.rules is not None, PASSWORD: objects.password_policies is not None}


# -- analysis -----------------------------------------------------------------------------------


def find_source(objects: criteria.Objects, ref) -> dict | None:
    """The source *ref* (a :class:`isc_reporter.source_migration.Source`) among the sources read."""
    return next((source for source in objects.sources or [] if criteria._is(source, ref)), None)


def policy_reads(objects: criteria.Objects, old, new, readers) -> list[dict]:
    """The fields of the provisioning policies of the other sources that read the *old* source:
    ``{"source", "where", "field", "kind", "name"}``, *kind* being "attribute" (an account attribute of
    the old source) or "transform" (a named transform among *readers*, those that read it)."""
    from isc_reporter.source_migration import old_attributes, references

    found = []
    for source, policy, field in _fields(objects, old, new):
        parts = [field.get("transform"), field.get("attributes")]
        base = {"source": source.get("name", ""), "where": _field_where(policy, field), "field": field}
        found += [{**base, "kind": "attribute", "name": name} for name in old_attributes(parts, old)]
        found += [{**base, "kind": "transform", "name": name}
                  for name in references(parts) if name in readers]
    return found


def findings(objects: criteria.Objects, profiles: list[dict], transforms: dict[str, dict], old, new,
             attributes: dict[str, dict], impacted: dict[str, str], readers, created) -> list[dict]:
    """Rows (:data:`criteria.COLUMNS`) for everything that depends on the *old* source besides the
    identity profiles, transforms and criteria. *attributes*: old account attribute -> its counterpart
    (``new_attribute``); *impacted*: identity attribute -> why its value may change; *readers*: the
    named transforms that read the old source; *created*: the identity profiles replaced by new ones."""
    old_profiles = {ap["id"]: ap for ap in objects.access_profiles or []
                    if ap.get("id") and _refers(ap.get("source"), old)}
    entitlements = {e["id"]: str(e.get("name") or e["id"]) for e in objects.entitlements or []
                    if e.get("id") and (not e.get("source") or _refers(e["source"], old))}
    for ap in old_profiles.values():
        for ref in ap.get("entitlements") or []:
            if isinstance(ref, dict) and ref.get("id"):
                entitlements.setdefault(ref["id"], str(ref.get("name") or ref["id"]))
    rows = _role_access_rows(objects, old, new, old_profiles, entitlements)
    rows += _access_profile_rows(objects, profiles, old, new, old_profiles)
    rows += _sod_rows(objects, old, new, entitlements)
    rows += _lifecycle_rows(objects, profiles, old, new, old_profiles, created)
    rows += _policy_rows(objects, transforms, old, new, attributes, impacted, readers, profiles)
    rows += _workflow_rows(objects, old, new)
    rows += _rule_rows(objects, old, new)
    rows += _password_rows(objects, old, new)
    return rows


def _row(*values) -> dict:
    return dict(zip(criteria.COLUMNS, values, strict=True))


def _refers(ref, source) -> bool:
    """Whether the reference ``{"id", "name"}`` is *source*."""
    if not isinstance(ref, dict):
        return False
    same_id = bool(source.id) and ref.get("id") == source.id
    return same_id or str(ref.get("name") or "").lower() == source.name.lower()


def _yes(flag: bool) -> str:
    return "yes" if flag else "no"


def _names(refs) -> str:
    return ", ".join(str(ref.get("name") or ref.get("id") or "")
                     for ref in refs or [] if isinstance(ref, dict))


def _holders(role: dict):
    """``(type, name, object)`` of a role and of each of its dimensions."""
    yield criteria.ROLE, str(role.get("name", "")), role
    for dimension in role.get("dimensions") or []:
        yield criteria.DIMENSION, f"{role.get('name', '')} / {dimension.get('name', '')}", dimension


def _role_access_rows(objects, old, new, old_profiles, entitlements) -> list[dict]:
    rows = []
    impact = f"Grants access of the old source: the members get nothing from it {GONE}."
    for role in objects.roles or []:
        enabled = role.get("enabled") is not False
        for kind, name, holder in _holders(role):
            parts = (("Access profiles", holder.get("accessProfiles")),
                     ("Entitlements", holder.get("entitlements")))
            definition = "\n".join(f"{label}: {_names(refs)}" for label, refs in parts if refs)
            for ref in holder.get("accessProfiles") or []:
                if isinstance(ref, dict) and ref.get("id") in old_profiles:
                    profile = old_profiles[ref["id"]].get("name") or ref.get("name", "")
                    rows.append(_row(WARNING if enabled else INFO, kind, name, _yes(enabled),
                                     "access profiles", f"{old.name} access profile '{profile}'", impact,
                                     f"Replace it with the access profile recreated on {new.name}.",
                                     definition))
            for ref in holder.get("entitlements") or []:
                if isinstance(ref, dict) and ref.get("id") in entitlements:
                    rows.append(_row(WARNING if enabled else INFO, kind, name, _yes(enabled), "entitlements",
                                     f"{old.name} entitlement '{entitlements[ref['id']]}'", impact,
                                     f"Replace it with the matching entitlement of {new.name}.", definition))
    return rows


def _grantors(objects, profiles) -> dict[str, list[str]]:
    """Access profile id -> the roles, dimensions and lifecycle states that grant it."""
    found: dict[str, list[str]] = {}
    for role in objects.roles or []:
        for kind, name, holder in _holders(role):
            for ref in holder.get("accessProfiles") or []:
                if isinstance(ref, dict) and ref.get("id"):
                    found.setdefault(ref["id"], []).append(f"{kind.lower()} {name}")
    for profile in profiles:
        for state in profile.get("lifecycleStates") or []:
            for ap_id in state.get("accessProfileIds") or []:
                found.setdefault(ap_id, []).append(
                    f"lifecycle state {profile.get('name', '')} / {state.get('name', '')}")
    return found


def _access_profile_rows(objects, profiles, old, new, old_profiles) -> list[dict]:
    grantors = _grantors(objects, profiles)
    segments = {s.get("id"): s.get("name", "") for s in objects.segments or []}
    rows = []
    for ap_id, ap in old_profiles.items():
        enabled = ap.get("enabled") is not False
        names = [str(ref.get("name") or ref.get("id") or "") for ref in ap.get("entitlements") or []
                 if isinstance(ref, dict)]
        action = f"Recreate it on {new.name} with the matching entitlements"
        if grantors.get(ap_id):
            action += f", then grant it instead in: {'; '.join(grantors[ap_id])}"
        assigned = [segments.get(s, s) for s in ap.get("segments") or []]
        if assigned:
            action += f"; assign it to the segments {', '.join(assigned)}"
        if ap.get("requestable"):
            action += "; make it requestable with the same approvals"
        rows.append(_row(WARNING if enabled else INFO, ACCESS_PROFILE, str(ap.get("name", "")), _yes(enabled),
                         "source and entitlements", f"{len(names)} entitlement(s) of {old.name}",
                         f"Grants access of the old source: it grants nothing {GONE}.", action + ".",
                         "\n".join(names)))
    return rows


def _sod_rows(objects, old, new, entitlements) -> list[dict]:
    rows = []
    for policy in objects.sod_policies or []:
        enforced = str(policy.get("state") or "").upper() == "ENFORCED"
        severity = WARNING if enforced else INFO
        conflict = policy.get("conflictingAccessCriteria") or {}
        sides = [(label, conflict.get(key) or {}) for key, label in (("leftCriteria", "left side"),
                                                                     ("rightCriteria", "right side"))]
        definition = " versus ".join(
            f"{side.get('name') or label}: {_names(side.get('criteriaList'))}" for label, side in sides)
        for label, side in sides:
            for item in side.get("criteriaList") or []:
                if isinstance(item, dict) and item.get("id") in entitlements:
                    entitlement = item.get("name") or entitlements[item["id"]]
                    rows.append(_row(severity, SOD, str(policy.get("name", "")), _yes(enforced),
                                     f"{label} ({side.get('name', '')})",
                                     f"{old.name} entitlement '{entitlement}'",
                                     f"Looks for an entitlement of the old source: the conflict is no longer "
                                     f"detected {GONE}.",
                                     f"Replace it with the matching entitlement of {new.name}.", definition))
        query = policy.get("policyQuery")
        if isinstance(query, str) and _mentions(query, old.id, old.name):
            rows.append(_row(severity, SOD, str(policy.get("name", "")), _yes(enforced), "query",
                             f"{old.name} (named in the query)", "Selects access of the old source.",
                             f"Rewrite the query for {new.name}.", query))
    return rows


def _lifecycle_rows(objects, profiles, old, new, old_profiles, created) -> list[dict]:
    sources = {s.get("id", ""): s.get("name", "") for s in objects.sources or [] if s.get("id")}
    if old.id:
        sources.setdefault(old.id, old.name)
    rows = []
    for profile in profiles:
        for state in profile.get("lifecycleStates") or []:
            enabled = state.get("enabled") is not False
            severity = WARNING if enabled else INFO
            name = f"{profile.get('name', '')} / {state.get('name') or state.get('technicalName', '')}"
            actions = [f"{verb}: {target}" for verb, target in lifecycle.account_actions(state, sources)]
            granted = [old_profiles.get(i, {}).get("name") or i for i in state.get("accessProfileIds") or []]
            definition = "\n".join(actions + ([f"Grant: {', '.join(granted)}"] if granted else []))
            found = []                                     # (severity, where, uses, impact, action)
            if profile.get("name") in created:
                found.append((severity, "state of the identity profile of the old source", "identity profile",
                              "The new identity profile starts without lifecycle states.",
                              f"Recreate it on the new identity profile with the technical name "
                              f"'{state.get('technicalName', '')}' (the cloudLifecycleState mapping returns "
                              "it) and the same actions."))
            for action in state.get("accountActions") or []:
                kind = str(action.get("action", ""))
                verb = lifecycle.VERBS.get(kind.upper(), kind)
                if old.id and old.id in (action.get("sourceIds") or []):
                    found.append((severity, f"{verb} accounts", f"accounts of {old.name}",
                                  f"Acts on the accounts of the old source: it does nothing {GONE}.",
                                  f"Replace {old.name} by {new.name} in this action, if {new.name} can "
                                  f"{verb.lower()} its accounts."))
                if old.id and action.get("allSources") and old.id in (action.get("excludeSourceIds") or []):
                    found.append((INFO, f"{verb} accounts of all sources but {old.name}",
                                  f"{old.name} (excluded)",
                                  "Leaves the accounts of the old source as they are.",
                                  f"Exclude {new.name} too, if its accounts must stay as they are."))
            for ap_id in state.get("accessProfileIds") or []:
                if ap_id in old_profiles:
                    found.append((severity, "access profiles granted",
                                  f"{old.name} access profile '{old_profiles[ap_id].get('name')}'",
                                  f"Grants access of the old source: it grants nothing {GONE}.",
                                  f"Replace it with the access profile recreated on {new.name}."))
            rows += [_row(level, LIFECYCLE, name, _yes(enabled), where, uses, impact, action, definition)
                     for level, where, uses, impact, action in found]
    return rows


def _fields(objects, old, new):
    """``(source, policy, field)`` of the provisioning policies of the sources other than *old* and *new*."""
    for source in objects.sources or []:
        if criteria._is(source, old) or criteria._is(source, new):
            continue
        for policy in source.get("provisioningPolicies") or []:
            fields = policy.get("fields") if isinstance(policy, dict) else None
            for field in fields or []:
                if isinstance(field, dict):
                    yield source, policy, field


def _field_where(policy: dict, field: dict) -> str:
    return f"{policy.get('usageType') or policy.get('name') or '?'} policy, field '{field.get('name', '?')}'"


def _policy_rows(objects, transforms, old, new, attributes, impacted, readers, profiles) -> list[dict]:
    rows, seen = [], set()

    def add(severity, source, where, uses, impact, action, field):
        if (source, where, uses) not in seen:
            seen.add((source, where, uses))
            definition = logic(field["transform"], transforms) if field.get("transform") else ""
            rows.append(_row(severity, POLICY, source, "", where, uses, impact, action, definition))

    for read in policy_reads(objects, old, new, readers):
        if read["kind"] == "attribute":
            target = (attributes.get(read["name"]) or {}).get("new_attribute")
            uses = f"{old.name} account attribute '{read['name']}'"
            action = (f"Rewrite on {new.name}: {new.name}.{target}" if target
                      else criteria._pending("Rewrite", new, read["name"]))
        else:
            uses = f"transform '{read['name']}' (reads {old.name})"
            action = f"Use the version of '{read['name']}' that reads {new.name} (see the Transforms sheet)."
        add(WARNING, read["source"], read["where"], uses,
            f"Reads the old source: the field gets no value {GONE}.", action, read["field"])
    configs = [p.get("identityAttributeConfig") or {} for p in profiles]
    mappings = [m for config in configs for m in config.get("attributeTransforms") or []]
    known = set(impacted) | {m.get("identityAttributeName", "") for m in mappings}
    for source, policy, field in _fields(objects, old, new):
        name = source.get("name", "")
        for attribute, _ in attribute_usage.references([field.get("transform"), field.get("attributes")],
                                                      transforms, known):
            reason = criteria._lookup(impacted, attribute)
            if reason:
                add(INFO, name, _field_where(policy, field), f"identity attribute '{attribute}'",
                    f"Identity attribute {reason}: the accounts this policy creates or updates on {name} get "
                    "the new values.",
                    f"Compare the values before and after; if attribute synchronization is on for {name}, "
                    "run analyze-before-sync on it before the switch.", field)
    return rows


def _mentions(text: str, source_id: str, name: str) -> bool:
    """Whether *text* holds the id of the source, or its name alone or between quotes."""
    lowered, name = text.lower(), name.lower()
    if source_id and source_id.lower() in lowered:
        return True
    return bool(name) and (lowered.strip() == name or f'"{name}"' in lowered or f"'{name}'" in lowered)


def _places(value, source_id: str, name: str, path: tuple = ()):
    """The paths of the texts of *value* that name the source."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield from _places(item, source_id, name, (*path, str(key)))
    elif isinstance(value, list):
        for item in value:
            yield from _places(item, source_id, name, path)
    elif isinstance(value, str) and _mentions(value, source_id, name):
        yield path


def _workflow_rows(objects, old, new) -> list[dict]:
    rows = []
    for workflow in objects.workflows or []:
        places = []
        parts = {key: value for key, value in workflow.items() if key in ("trigger", "definition")}
        for path in _places(parts, old.id, old.name):
            if path[:2] == ("definition", "steps") and len(path) > 2:
                places.append(f"step '{path[2]}'")
            else:
                places.append(path[0] if path else "workflow")
        if not places:
            continue
        enabled = bool(workflow.get("enabled"))
        trigger = workflow.get("trigger") or {}
        event = (trigger.get("attributes") or {}).get("id") or trigger.get("type") or "?"
        rows.append(_row(WARNING if enabled else INFO, WORKFLOW, str(workflow.get("name", "")), _yes(enabled),
                         ", ".join(dict.fromkeys(places)), f"{old.name} (named in the workflow)",
                         f"Names the old source: {GONE}, the workflow acts on nothing or fails.",
                         f"Point it to {new.name}.",
                         f"Trigger: {event}"))
    return rows


def attached_rules(source: dict, rules: list[dict] | None) -> list[tuple[str, str]]:
    """``(rule name, where)`` of the rules a source runs: its rule references, and the connector
    attributes named like a rule (beforeRule, buildMapRule...) whose value is a rule name."""
    known = {str(r.get("name", "")).lower() for r in rules or []}
    found = [(str(source[key]["name"]), key) for key in SOURCE_RULES
             if isinstance(source.get(key), dict) and source[key].get("name")]

    def walk(value, key=""):
        if isinstance(value, dict):
            for k, item in value.items():
                walk(item, str(k))
        elif isinstance(value, list):
            for item in value:
                walk(item, key)
        elif isinstance(value, str) and value.strip() and RULE_KEY.search(key):
            text = value.strip()
            if (text.lower() in known) if rules is not None else (text.lower() not in FLAGS
                                                                   and not text.isdigit()):
                found.append((text, key))

    walk(source.get("connectorAttributes") or {})
    return list(dict.fromkeys(found))


def _rule_rows(objects, old, new) -> list[dict]:
    rows = []
    types = {str(r.get("name", "")).lower(): str(r.get("type", "")) for r in objects.rules or []}
    source = find_source(objects, old)
    for name, key in attached_rules(source, objects.rules) if source else []:
        rows.append(_row(INFO, RULE, name, "", f"run by {old.name} ({key})", old.name,
                         "Runs for the old source only: the new source does not run it.",
                         f"Attach it, or a version written for the connector of {new.name}, to {new.name} "
                         "if it is still needed.", f"type {types.get(name.lower()) or '?'}"))
    for rule in objects.rules or []:
        script = (rule.get("sourceCode") or {}).get("script")
        if isinstance(script, str) and _mentions(script, old.id, old.name):
            rows.append(_row(WARNING, RULE, str(rule.get("name", "")), "", "code",
                             f"{old.name} (named in the code)",
                             "Its code names the old source: it may read its accounts or act on them.",
                             f"Review the code and replace {old.name} by {new.name}.",
                             f"type {rule.get('type') or '?'}"))
    return rows


def _password_rows(objects, old, new) -> list[dict]:
    sources = {s.get("id", ""): s.get("name", "") for s in objects.sources or [] if s.get("id")}
    rows = []
    for policy in objects.password_policies or []:
        ids = policy.get("sourceIds") or []
        if old.id and old.id in ids:
            rows.append(_row(INFO, PASSWORD, str(policy.get("name", "")), "", "sources of the policy",
                             old.name,
                             "Sets the password rules of the old source.",
                             f"Add {new.name} to it, if ISC manages its passwords.",
                             ", ".join(sources.get(i, i) for i in ids)))
    return rows


# -- the old source itself ----------------------------------------------------------------------


def source_rows(objects: criteria.Objects, old, new, used: int) -> list[dict]:
    """The configuration of the old source to recreate on the new one (Setting, Old source, On the new
    source). *used*: the number of its account attributes used elsewhere."""
    source = find_source(objects, old)
    if source is None:
        return [_setting("Source", f"{old.name}: not found", "Check the name of the old source.")]
    access = [ap for ap in objects.access_profiles or [] if _refers(ap.get("source"), old)]
    rules = [f"{name} ({key})" for name, key in attached_rules(source, objects.rules)]
    policies = [f"{p.get('usageType', '')}: {p.get('name', '')} ({len(p.get('fields') or [])} field(s))"
                for p in source.get("provisioningPolicies") or [] if isinstance(p, dict)]
    pairs = [f"{a.get('property', '')} = {a.get('value', '')}"
             for a in (source.get("correlationConfig") or {}).get("attributeAssignments") or []]
    manager = source.get("managerCorrelationMapping") or {}
    manager_text = ""
    if manager.get("accountAttributeName"):
        manager_text = (f"{old.name}.{manager['accountAttributeName']} = identity."
                        f"{manager.get('identityAttributeName', '')}")
    accounts = [s for s in source.get("schemas") or [] if isinstance(s, dict) and s.get("name") == "account"]
    schema = old.schema if old.schema is not None else [a.get("name") for s in accounts
                                                         for a in s.get("attributes") or []]
    passwords = [str(p.get("name", "")) for p in objects.password_policies or []
                 if source.get("id") and source["id"] in (p.get("sourceIds") or [])]
    entitlements = objects.entitlements
    threshold = source.get("deleteThreshold")
    authoritative = bool(source.get("authoritative"))
    rows = [
        ("Connector", source.get("connectorName") or source.get("type") or source.get("connector") or "",
         f"The connector of {new.name}."),
        ("Authoritative", _yes(authoritative),
         "Its identities need a new identity profile (see Profiles)." if authoritative else ""),
        ("Owner", (source.get("owner") or {}).get("name", ""), f"The owner of {new.name}."),
        ("Management workgroup", (source.get("managementWorkgroup") or {}).get("name", "") or "none",
         "The same governance group, if it still applies." if source.get("managementWorkgroup") else ""),
        ("Virtual appliance cluster", (source.get("cluster") or {}).get("name", "") or "none",
         f"A cluster that reaches {new.name}, if its connector needs one." if source.get("cluster") else ""),
        ("Features", ", ".join(str(f) for f in source.get("features") or []) or "none",
         f"Check that {new.name} offers the features in use (provisioning, enable, password...)."),
        ("Delete threshold", f"{threshold}%" if threshold is not None else "none",
         "Set one, so that an aggregation cannot remove too many accounts by mistake."),
        ("Account correlation", " AND ".join(pairs) or "none",
         "Recreate it with the attribute names of the new source, before its first aggregation."
         if pairs else "Define one, so that the accounts of the new source join the existing identities."),
        ("Manager correlation", manager_text or "none",
         "Recreate it with the attribute names of the new source." if manager_text else ""),
        ("Rules", "\n".join(rules) or "none",
         "Attach them, or versions written for the new connector, if still needed." if rules else ""),
        ("Provisioning policies", "\n".join(policies) or "none",
         "Recreate them with the attribute names of the new source." if policies else ""),
        ("Account schema", f"{len(schema)} attribute(s), {used} used by the configuration" if schema
         else f"not read, {used} attribute(s) used by the configuration",
         "Give each attribute used its counterpart (see Attribute mapping)." if used else ""),
        ("Access profiles", str(len(access)),
         "Recreate them on the new source (see Impacts)." if access else ""),
        ("Entitlements", "not read" if entitlements is None else
         f"{len(entitlements)} ({sum(bool(e.get('privileged')) for e in entitlements)} privileged)",
         "Aggregate them from the new source, and match each one in use (see Impacts)."
         if entitlements else ""),
        ("Password policies", ", ".join(passwords) or "none",
         "Add the new source to them, if ISC manages its passwords." if passwords else ""),
    ]
    return [_setting(*row) for row in rows]


def _setting(setting: str, today: str, action: str) -> dict:
    return {"Setting": setting, "Old source": today, "On the new source": action}
