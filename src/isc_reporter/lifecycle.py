"""Lifecycle states of the identity profiles and what they provision: when an identity enters a state,
the accounts enabled, disabled or deleted on which sources, the access profiles granted, whether all
access is removed, and who is notified.

The checks compare the states with the ``cloudLifecycleState`` mapping of the profile (every value
it can return should be an enabled state) and look for leaver states that remove nothing, and for
sources or access profiles that no longer exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from isc_reporter.transform_tree import attributes_of, reference_name

INACTIVE = {"INACTIVE_SHORT_TERM", "INACTIVE_LONG_TERM"}
VERBS = {"ENABLE": "Enable", "DISABLE": "Disable", "DELETE": "Delete"}
ALL_SOURCES = "(all sources)"
WARNING, INFO = "Warning", "Info"


@dataclass
class Lifecycle:
    states: dict[str, list[dict]] = field(default_factory=dict)      # identity profile id -> its states
    sources: dict[str, str] = field(default_factory=dict)            # source id -> name
    access_profiles: dict[str, str] = field(default_factory=dict)    # access profile id -> name
    notes: list[str] = field(default_factory=list)


# -- reading ------------------------------------------------------------------------------------


def fetch(isc, profiles: list[dict]) -> Lifecycle:
    """The lifecycle states of each profile, the names of the sources and of the access profiles they
    name. What cannot be read is noted, and the report goes on."""
    from isc_reporter.api import error_message, list_all, model_json
    from isc_reporter.search import search_by_ids

    result = Lifecycle()
    for profile in profiles:
        if not profile.get("id"):
            continue
        try:
            found = list_all(isc.lifecycle_states.get_lifecycle_states_v1, identity_profile_id=profile["id"])
        except Exception as exc:
            result.notes.append(f"Lifecycle states of the profile '{profile.get('name', '')}' not read "
                                f"({error_message(exc)}).")
            continue
        result.states[profile["id"]] = [model_json(state) for state in found]
    try:
        result.sources = {source.id: source.name for source in list_all(isc.sources.list_sources_v1)}
    except Exception as exc:
        result.notes.append(f"Sources not read ({error_message(exc)}): their ids are shown.")
    ids = sorted({i for states in result.states.values() for state in states
                  for i in state.get("accessProfileIds") or []})
    if ids:
        try:
            result.access_profiles = {doc.get("id", ""): doc.get("name", "")
                                      for doc in search_by_ids(isc, "accessprofiles", ids)}
        except Exception as exc:
            result.notes.append(f"Access profile names not read ({error_message(exc)}): their ids are shown.")
    return result


def from_files(profiles: list[dict], paths: list[str]) -> Lifecycle:
    """The same from exported files: the states a profile export lists (``lifecycleStates``), with
    the sources and access profiles found in the files."""
    from isc_reporter.identity_profiles import iter_objects

    result = Lifecycle()
    for profile in profiles:
        if isinstance(profile.get("lifecycleStates"), list):
            result.states[profile.get("id") or profile.get("name", "")] = profile["lifecycleStates"]
    for obj in iter_objects(paths):
        if obj.get("id") and ("connector" in obj or "connectorAttributes" in obj):
            result.sources[obj["id"]] = obj.get("name", "")
        elif obj.get("id") and "entitlements" in obj and "source" in obj:
            result.access_profiles[obj["id"]] = obj.get("name", "")
    if profiles and not result.states:
        result.notes.append("No lifecycle state in the exported files.")
    return result


def states_of(lifecycle: Lifecycle, profile: dict) -> list[dict]:
    states = lifecycle.states.get(profile.get("id") or profile.get("name", "")) or []
    return sorted(states, key=lambda s: (s.get("priority") if s.get("priority") is not None else 999,
                                         str(s.get("name", "")).lower()))


# -- what a state does --------------------------------------------------------------------------


def _names(ids, names: dict[str, str]) -> list[str]:
    return [names.get(i, i) for i in ids or []]


def account_actions(state: dict, sources: dict[str, str]) -> list[tuple[str, str]]:
    """``(verb, sources)`` of each account action, e.g. ``("Disable", "all sources except HR")``."""
    found = []
    for action in state.get("accountActions") or []:
        verb = VERBS.get(str(action.get("action", "")).upper(), str(action.get("action", "")))
        if action.get("allSources"):
            excluded = _names(action.get("excludeSourceIds"), sources)
            target = "all sources" + (f" except {', '.join(excluded)}" if excluded else "")
        else:
            target = ", ".join(_names(action.get("sourceIds"), sources)) or "no source"
        found.append((verb, target))
    return found


def per_source(state: dict, sources: dict[str, str]) -> dict[str, str]:
    """Source name -> what the state does to its accounts. Actions on all sources apply to every source
    read (or to ALL_SOURCES when the sources are not known)."""
    found: dict[str, str] = {}
    for action in state.get("accountActions") or []:
        verb = VERBS.get(str(action.get("action", "")).upper(), str(action.get("action", "")))
        if action.get("allSources"):
            excluded = set(action.get("excludeSourceIds") or [])
            targets = [name for source_id, name in sources.items() if source_id not in excluded]
            targets = targets or [ALL_SOURCES]
        else:
            targets = _names(action.get("sourceIds"), sources)
        for name in targets:
            found.setdefault(name, verb)
    return found


def notified(state: dict) -> str:
    option = state.get("emailNotificationOption") or {}
    who = [label for key, label in (("notifyManagers", "managers"), ("notifyAllAdmins", "all admins"))
           if option.get(key)]
    if option.get("notifySpecificUsers"):
        who += option.get("emailAddressList") or ["specific users"]
    return ", ".join(who)


def state_rows(profiles: list[dict], lifecycle: Lifecycle) -> list[dict]:
    rows = []
    for profile in profiles:
        for state in states_of(lifecycle, profile):
            access = state.get("accessActionConfiguration") or {}
            actions = account_actions(state, lifecycle.sources)
            granted = _names(state.get("accessProfileIds"), lifecycle.access_profiles)
            rows.append({
                "Identity profile": profile.get("name", ""), "Lifecycle state": state.get("name", ""),
                "Technical name": state.get("technicalName", ""),
                "Enabled": "yes" if state.get("enabled") else "no",
                "Identity state": state.get("identityState") or "", "Identities": state.get("identityCount"),
                "Accounts": "\n".join(f"{verb}: {target}" for verb, target in actions),
                "Access profiles granted": "\n".join(granted),
                "Remove all access": "yes" if access.get("removeAllAccessEnabled") else "no",
                "Notified": notified(state), "Description": state.get("description") or ""})
    return rows


def source_rows(profiles: list[dict], lifecycle: Lifecycle) -> list[dict]:
    """One row per source, one column per lifecycle state: what the state does to the source's accounts."""
    columns, actions = [], {}
    for profile in profiles:
        for state in states_of(lifecycle, profile):
            done = per_source(state, lifecycle.sources)
            if done:
                column = f"{profile.get('name', '')}: {state.get('technicalName') or state.get('name', '')}"
                columns.append(column)
                actions[column] = done
    names = sorted({name for done in actions.values() for name in done}, key=str.lower)
    return [{"Source": name, **{column: actions[column].get(name, "") for column in columns}}
            for name in names]


# -- checks -------------------------------------------------------------------------------------


def values(definition, transforms: dict[str, dict], seen: frozenset = frozenset()) -> set[str] | None:
    """The fixed values a lifecycle state mapping can return, or None when they cannot be known (a copied
    account attribute, a template, a rule)."""
    if isinstance(definition, str):
        return None if "$" in definition or "#" in definition else {definition}
    if not isinstance(definition, dict) or "type" not in definition:
        return None
    kind, attrs = definition["type"], attributes_of(definition)
    if kind == "static":
        return values(attrs.get("value"), transforms, seen) if isinstance(attrs.get("value"), str) else None
    if kind in ("dateCompare", "conditional"):
        found = [values(attrs.get(key), transforms, seen)
                 for key in ("positiveCondition", "negativeCondition")]
        return None if None in found else set().union(*found)
    if kind == "lookup" and isinstance(attrs.get("table"), dict):
        return {str(v) for v in attrs["table"].values() if isinstance(v, str) and "$" not in v} or None
    if kind == "firstValid" and isinstance(attrs.get("values"), list):
        found = [values(v, transforms, seen) for v in attrs["values"]]
        return None if None in found else set().union(*found)
    if kind in ("lower", "upper", "trim"):
        inner = values(attrs.get("input"), transforms, seen) if "input" in attrs else None
        if inner is None:
            return None
        return {v.lower() if kind == "lower" else v.upper() if kind == "upper" else v.strip() for v in inner}
    if kind == "reference":
        name = reference_name(attrs)
        if name in seen or name not in transforms:
            return None
        return values({"type": transforms[name].get("type"), "attributes": attributes_of(transforms[name])},
                      transforms, seen | {name})
    return None


def checks(profile: dict, lifecycle: Lifecycle,
           transforms: dict[str, dict]) -> list[tuple[str, str, str, str]]:
    """``(severity, attribute or state, check, details)`` for the lifecycle states of a profile."""
    states = states_of(lifecycle, profile)
    if not states:
        return []
    found = []
    by_name = {str(s.get("technicalName", "")).lower(): s for s in states}
    mapping = next((m.get("transformDefinition") for m in (profile.get("identityAttributeConfig") or {}).get(
        "attributeTransforms") or [] if m.get("identityAttributeName") == "cloudLifecycleState"), None)
    for value in sorted(values(mapping, transforms) or []) if mapping else []:
        state = by_name.get(value.lower())
        if state is None:
            found.append((WARNING, "cloudLifecycleState", "Value without lifecycle state",
                          f"The mapping can return '{value}', which is not a lifecycle state of this profile "
                          f"({', '.join(sorted(by_name)) or 'none'})."))
        elif not state.get("enabled"):
            found.append((WARNING, "cloudLifecycleState", "Lifecycle state disabled",
                          f"The mapping can return '{value}', but that lifecycle state is disabled."))
    for state in states:
        label = f"state {state.get('technicalName', '')}"
        access = state.get("accessActionConfiguration") or {}
        removes = [verb for verb, _ in account_actions(state, lifecycle.sources)
                   if verb in ("Disable", "Delete")]
        if (state.get("enabled") and str(state.get("identityState") or "").upper() in INACTIVE and not removes
                and not access.get("removeAllAccessEnabled")):
            found.append((WARNING, label, "Leaver state that removes nothing",
                          "Configured as inactive, but it disables no account and does not remove access: "
                          "leavers keep what they have."))
        if not state.get("enabled") and state.get("identityCount"):
            found.append((INFO, label, "Disabled state with identities",
                          f"{state['identityCount']} identit(ies) are in this disabled lifecycle state."))
        if lifecycle.sources:
            unknown = [i for action in state.get("accountActions") or []
                       for i in (action.get("sourceIds") or []) + (action.get("excludeSourceIds") or [])
                       if i not in lifecycle.sources]
            if unknown:
                found.append((WARNING, label, "Unknown source",
                              f"Its account actions name {len(unknown)} source(s) that do not exist "
                              "(deleted?): "
                              f"{', '.join(unknown)}."))
        if lifecycle.access_profiles:
            unknown = [i for i in state.get("accessProfileIds") or [] if i not in lifecycle.access_profiles]
            if unknown:
                found.append((WARNING, label, "Unknown access profile",
                              f"Grants {len(unknown)} access profile(s) that were not found: "
                              f"{', '.join(unknown)}."))
    return found
