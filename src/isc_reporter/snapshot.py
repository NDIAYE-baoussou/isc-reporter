"""Configuration snapshot: every configuration object of the tenant saved as JSON, one file per object,
in a folder meant to be kept under version control (git).

Taken again into the same folder, a snapshot only rewrites the files of the objects that changed in
the tenant, adds the new ones and removes the deleted ones: the history of the folder is the history
of the configuration. The JSON is what the API returns, in a fixed order (keys, and lists of
references the API returns in any order), without the fields that change on their own (modification
dates, counts, health, aggregation state). Secrets (passwords, tokens, client secrets, encrypted
connector attributes) are replaced by ``"(redacted)"``.

The objects keep the shape ``--from`` reads, so ``mappings``, ``transforms`` and ``migration`` can run
on a snapshot instead of the tenant: identity profiles hold their ``lifecycleStates``, sources their
``schemas``, ``provisioningPolicies`` and ``correlationConfig``, dynamic roles their ``dimensions``.
Nothing is changed in the tenant.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlparse

from isc_reporter import __version__
from isc_reporter.errors import UsageError

log = logging.getLogger("isc_reporter")

MANIFEST = "snapshot.json"
REDACTED = "(redacted)"
ADDED, CHANGED, REMOVED = "added", "changed", "removed"


@dataclass(frozen=True)
class Kind:
    label: str
    api: str                             # attribute of isc_reporter.client.Isc
    method: str                          # list method of the SDK
    endpoint: str
    page: int | None = 250               # None: one call returns everything
    params: tuple = ()                   # (name, value) arguments of every call


KINDS = {
    "transforms": Kind("Transforms", "transforms", "list_transforms_v1", "/transforms/v1"),
    "identity-profiles": Kind("Identity profiles", "identity_profiles", "list_identity_profiles_v1",
                              "/identity-profiles/v1"),
    "identity-attributes": Kind("Identity attributes", "identity_attributes", "list_identity_attributes_v1",
                                "/identity-attributes/v1", None,
                                (("include_system", True), ("include_silent", True))),
    "sources": Kind("Sources", "sources", "list_sources_v1", "/sources/v1"),
    "roles": Kind("Roles", "roles", "list_roles_v1", "/roles/v1", 50),
    "access-profiles": Kind("Access profiles", "access_profiles", "list_access_profiles_v1",
                            "/access-profiles/v1"),
    "segments": Kind("Segments", "segments", "list_segments_v1", "/segments/v1"),
    "connector-rules": Kind("Connector rules", "connector_rules", "get_connector_rule_list_v1",
                            "/connector-rules/v1", 50),
    "workflows": Kind("Workflows", "workflows", "list_workflows_v1", "/workflows/v1", None),
    "sod-policies": Kind("SoD policies", "sod_policies", "list_sod_policies_v1", "/sod-policies/v1"),
    "governance-groups": Kind("Governance groups", "governance_groups", "list_workgroups_v1",
                              "/workgroups/v1"),
    "password-policies": Kind("Password policies", "password_policies", "list_password_policies_v1",
                              "/password-policies/v1", 250, (("x_sail_point_experimental", "true"),)),
}

#: Fields that change without a change of configuration, left out of the objects and their sub-objects.
VOLATILE = frozenset({"modified", "lastUpdated", "identityCount", "executionCount", "dailyExecutionCount",
                      "failureCount", "memberCount", "connectionCount", "identityRefreshRequired"})
#: The same, of sources only: health checks and aggregations update them.
SOURCE_VOLATILE = frozenset({"healthy", "status", "since"})
CONNECTOR_VOLATILE = frozenset({"healthy", "status", "since", "deltaAggregation", "cloudCacheUpdate",
                                "slpt-source-diagnostics"})
CONNECTOR_VOLATILE_PREFIXES = ("lastAggregationDate",)
SUB_OBJECTS = ("lifecycleStates", "schemas", "provisioningPolicies", "dimensions", "correlationConfig")
REFERENCE_KEYS = frozenset({"type", "id", "name", "displayName"})
FIRST_KEYS = ("id", "name", "type")

#: Keys holding a secret: their text values are redacted...
SECRET_KEY = re.compile(r"pass(word|wd|phrase)|pwd|secret|token|api_?key|private_?key|credential|"
                        r"authorization", re.I)
#: ... unless the name says they hold something else: a URL, a type, a name, a policy, a location...
NOT_SECRET_KEY = re.compile(r"(url|uri|type|name|names|id|ids|policy|policies|field|fields|attribute|"
                            r"attributes|expiration|expiry|length|format|enabled|lifetime|duration|"
                            r"location)$", re.I)
#: Static values of secret-like provisioning fields that are flags, not secrets.
FLAGS = frozenset({"true", "false", "yes", "no"})

_UNSAFE = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)$", re.I)       # Windows device names


@dataclass
class Snapshot:
    tenant: str
    objects: dict[str, list[dict]] = field(default_factory=dict)    # type -> objects; types not read absent
    notes: list[str] = field(default_factory=list)


@dataclass
class Result:
    folder: str
    counts: dict[str, int] = field(default_factory=dict)            # type -> objects in the snapshot
    changes: list[dict] = field(default_factory=list)               # {"type", "file", "change"}
    redacted: int = 0
    since: str = ""                                                 # when the earlier snapshot was taken

    def changed(self, kind: str) -> Counter:
        return Counter(c["change"] for c in self.changes if c["type"] == kind)


# -- reading ------------------------------------------------------------------------------------


def fetch(isc, kinds: list[str]) -> Snapshot:
    """The objects of each type in *kinds*, with their sub-objects. A type that cannot be read is
    noted and left out (the files of an earlier snapshot are then kept)."""
    from isc_reporter.api import error_message

    found = Snapshot(isc.base_url)
    for name in kinds:
        kind = KINDS[name]
        try:
            objects = [obj for obj in _list(isc, kind) if isinstance(obj, dict)]
        except Exception as exc:
            found.notes.append(f"{kind.label} not read ({error_message(exc)}): the files of an earlier "
                               "snapshot are kept.")
            continue
        details = DETAILS.get(name)
        if details:
            details(isc, objects, found.notes)
        found.objects[name] = objects
        log.info("%s: %d", kind.label, len(objects))
    return found


def _list(isc, kind: Kind) -> list:
    from isc_reporter.api import call_json, list_json

    method = getattr(getattr(isc, kind.api), kind.method + "_without_preload_content")
    if kind.page is None:
        return call_json(method, **dict(kind.params)) or []
    return list_json(method, page_size=kind.page, **dict(kind.params))


def _attach(objects: list[dict], key: str, read, notes: list[str], what: str, of: str) -> None:
    """``obj[key] = read(obj)`` for each object; those that cannot be read are noted once (404: none)."""
    from isc_reporter.api import error_message

    unread, error = [], ""
    for obj in objects:
        try:
            value = read(obj)
        except Exception as exc:
            if getattr(exc, "status", None) != 404:
                unread.append(str(obj.get("name", "")))
                error = error or error_message(exc)
            continue
        if value is not None:
            obj[key] = value
    if unread:
        notes.append(f"{what} not read for {len(unread)} {of}(s) ({error}): {', '.join(unread[:10])}"
                     f"{' ...' if len(unread) > 10 else ''}.")


def _by_name(item) -> tuple:
    if not isinstance(item, dict):
        return "", ""
    return str(item.get("name", "")).lower(), str(item.get("id", ""))


def _state_order(state) -> tuple:
    priority = state.get("priority") if isinstance(state.get("priority"), (int, float)) else 999
    return priority, str(state.get("technicalName", ""))


def _profile_details(isc, profiles: list[dict], notes: list[str]) -> None:
    from isc_reporter.api import list_json

    method = isc.lifecycle_states.get_lifecycle_states_v1_without_preload_content
    _attach(profiles, "lifecycleStates",
            lambda p: sorted(list_json(method, identity_profile_id=p.get("id", "")), key=_state_order),
            notes, "Lifecycle states", "identity profile")


def _source_details(isc, sources: list[dict], notes: list[str]) -> None:
    from isc_reporter.api import call_json, list_json

    api = isc.sources
    _attach(sources, "schemas",
            lambda s: sorted(call_json(api.get_source_schemas_v1_without_preload_content,
                                       source_id=s.get("id", "")) or [], key=_by_name),
            notes, "Schemas", "source")
    _attach(sources, "provisioningPolicies",
            lambda s: sorted(list_json(api.list_provisioning_policies_v1_without_preload_content,
                                       source_id=s.get("id", "")),
                             key=lambda p: (str(p.get("usageType", "")), str(p.get("name", "")))),
            notes, "Provisioning policies", "source")
    _attach(sources, "correlationConfig",
            lambda s: call_json(api.get_correlation_config_v1_without_preload_content, id=s.get("id", "")),
            notes, "Account correlation", "source")


def _role_details(isc, roles: list[dict], notes: list[str]) -> None:
    from isc_reporter.api import list_json

    method = isc.dimensions.list_dimensions_v1_without_preload_content
    _attach([role for role in roles if role.get("dimensional")], "dimensions",
            lambda r: sorted(list_json(method, page_size=50, role_id=r.get("id", "")), key=_by_name),
            notes, "Dimensions", "role")


DETAILS = {"identity-profiles": _profile_details, "sources": _source_details, "roles": _role_details}


# -- what is written ----------------------------------------------------------------------------


def stable(kind: str, obj: dict) -> dict:
    """A copy of *obj* without the fields that change on their own."""
    obj = json.loads(json.dumps(obj))
    _drop(obj, VOLATILE | (SOURCE_VOLATILE if kind == "sources" else frozenset()))
    for key in SUB_OBJECTS:
        value = obj.get(key)
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                _drop(item, VOLATILE)
    connector = obj.get("connectorAttributes")
    if kind == "sources" and isinstance(connector, dict):
        _drop(connector, CONNECTOR_VOLATILE | {key for key in connector
                                               if str(key).startswith(CONNECTOR_VOLATILE_PREFIXES)})
    _sort_references(obj)
    return obj


def _drop(obj: dict, keys) -> None:
    for key in [key for key in obj if key in keys]:
        del obj[key]


def _sort_references(value) -> None:
    """Sort, in place, the lists made only of references (``{"type", "id", "name"}``): the API returns
    them in any order. Other lists keep theirs (approval levels, firstValid values, schema attributes)."""
    items = value.values() if isinstance(value, dict) else value if isinstance(value, list) else ()
    for item in items:
        _sort_references(item)
    if isinstance(value, list) and value and all(
            isinstance(item, dict) and "id" in item and set(item) <= REFERENCE_KEYS for item in value):
        value.sort(key=lambda ref: (str(ref.get("type", "")), str(ref.get("name", "")).lower(),
                                    str(ref.get("id", ""))))


def _secret_key(key) -> bool:
    return isinstance(key, str) and bool(SECRET_KEY.search(key)) and not NOT_SECRET_KEY.search(key)


def redact(value, hidden: frozenset = frozenset()) -> int:
    """Replace the secrets in *value* (in place) by :data:`REDACTED`; returns how many were replaced.

    A secret is the text value of a key named like one (password, clientSecret, token...), a value
    listed as encrypted by the ``encrypted`` key next to it (connector attributes), or the static
    value of a provisioning policy field of type ``secret`` or named like a secret (except flags such
    as ``true``)."""
    count = 0
    if isinstance(value, list):
        return sum(redact(item) for item in value)
    if not isinstance(value, dict):
        return 0
    encrypted = value.get("encrypted")
    if isinstance(encrypted, str):           # "a,b", sometimes written as a list: "['a', 'b']"
        hidden = hidden | {name.strip(" []'\"") for name in encrypted.split(",") if name.strip(" []'\"")}
    for key, item in value.items():
        if item in (None, "", [], {}, REDACTED):
            continue
        if (key in hidden and not isinstance(item, dict)) or (isinstance(item, str) and _secret_key(key)):
            value[key] = REDACTED
            count += 1
        else:
            count += redact(item)
    transform, name = value.get("transform"), value.get("name")
    secret_field = isinstance(name, str) and (
        value.get("type") == "secret" or (_secret_key(name) and value.get("type") != "boolean"))
    if secret_field and isinstance(transform, dict) and transform.get("type") == "static":
        attributes = transform.get("attributes")
        static = attributes.get("value") if isinstance(attributes, dict) else None
        if static not in (None, "", REDACTED) and str(static).strip().lower() not in FLAGS:
            attributes["value"] = REDACTED
            count += 1
    return count


def to_json(obj) -> str:
    """The JSON of a file: keys in a fixed order (``id``, ``name`` and ``type`` first, then the others
    sorted), so that the files only change when the values do."""
    return json.dumps(_ordered(obj), indent=2, ensure_ascii=False) + "\n"


def _ordered(value):
    if isinstance(value, dict):
        keys = sorted(value, key=lambda key: (FIRST_KEYS.index(key) if key in FIRST_KEYS else len(FIRST_KEYS),
                                              str(key)))
        return {key: _ordered(value[key]) for key in keys}
    if isinstance(value, list):
        return [_ordered(item) for item in value]
    return value


def file_name(name: str) -> str:
    """*name* made safe as a file name on Windows, macOS and Linux (without extension)."""
    safe = _UNSAFE.sub("_", name).strip().rstrip(". ")[:120].rstrip(". ") or "_"
    return "_" + safe if _RESERVED.match(safe.split(".")[0]) else safe


def file_names(objects: list[dict]) -> list[str]:
    """One file name per object, from its name; names that clash (ignoring case, as Windows and macOS
    do) get the start of the object id."""
    stems = [file_name(str(obj.get("name") or obj.get("id") or "unnamed")) for obj in objects]
    clashes = Counter(stem.lower() for stem in stems)
    names, used = [], set()
    for obj, stem in zip(objects, stems, strict=True):
        if clashes[stem.lower()] > 1 and obj.get("id"):
            stem = f"{stem} ({str(obj['id'])[:8]})"
        candidate, number = stem, 2
        while candidate.lower() in used:
            candidate, number = f"{stem} {number}", number + 1
        used.add(candidate.lower())
        names.append(candidate + ".json")
    return names


def tenant_name(base_url: str) -> str:
    """``acme-sb`` for ``https://acme-sb.api.identitynow.com``."""
    host = urlparse(base_url).hostname or ""
    return file_name(host.split(".")[0]) if host else "tenant"


# -- writing ------------------------------------------------------------------------------------


def write(snapshot: Snapshot, folder: str, now: datetime | None = None) -> Result:
    """Write *snapshot* into *folder*: a new or empty folder, or the folder of an earlier snapshot of
    the same tenant, which is brought up to date (files of the types read are added, rewritten when
    they changed, removed when the object no longer exists)."""
    previous = _previous(folder, snapshot.tenant)
    os.makedirs(folder, exist_ok=True)
    taken =(now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    result = Result(folder, since=str((previous or {}).get("taken", "")))
    types = dict((previous or {}).get("types") or {})
    for kind, objects in snapshot.objects.items():
        target = os.path.join(folder, kind)
        os.makedirs(target, exist_ok=True)
        existing = {name.lower(): name for name in os.listdir(target) if name.endswith(".json")}
        objects = sorted(objects, key=_by_name)
        written = set()
        for name, obj in zip(file_names(objects), objects, strict=True):
            obj = stable(kind, obj)
            result.redacted += redact(obj)
            text, path = to_json(obj), os.path.join(target, name)
            old_name = existing.get(name.lower())
            old = _read(os.path.join(target, old_name)) if old_name else None
            if old_name and old_name != name:            # same name, other case: renamed in the tenant
                os.remove(os.path.join(target, old_name))
                old = None
            if old != text:
                with open(path, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(text)
                result.changes.append({"type": kind, "file": f"{kind}/{name}",
                                       "change": ADDED if old_name is None else CHANGED})
            written.add(name.lower())
        for lower, name in sorted(existing.items()):
            if lower not in written:
                os.remove(os.path.join(target, name))
                result.changes.append({"type": kind, "file": f"{kind}/{name}", "change": REMOVED})
        result.counts[kind] = len(objects)
        types[kind] = {"objects": len(objects), "taken": taken, "endpoint": "GET " + KINDS[kind].endpoint}
    manifest = {
        "tool": f"isc-reporter {__version__}", "tenant": snapshot.tenant, "taken": taken,
        "types": {kind: types[kind] for kind in [*KINDS, *sorted(types)] if kind in types},
        "redacted": result.redacted, "notes": snapshot.notes,
        "fieldsLeftOut": {"all": sorted(VOLATILE), "sources": sorted(
            SOURCE_VOLATILE | {f"connectorAttributes.{key}" for key in CONNECTOR_VOLATILE}
            | {f"connectorAttributes.{prefix}*" for prefix in CONNECTOR_VOLATILE_PREFIXES})},
        "changes": result.changes if previous is not None else [],
    }
    with open(os.path.join(folder, MANIFEST), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    return result


def _previous(folder: str, tenant: str) -> dict | None:
    """The manifest of the earlier snapshot in *folder*; None for a new or empty folder."""
    if os.path.exists(folder) and not os.path.isdir(folder):
        raise UsageError(f"'{folder}' is a file: give a folder for the snapshot")
    if not os.path.isdir(folder):
        return None
    path = os.path.join(folder, MANIFEST)
    if not os.path.isfile(path):
        if any(not entry.startswith(".") for entry in os.listdir(folder)):     # .git is fine
            raise UsageError(f"'{folder}' is not empty and holds no snapshot: give a new or empty folder, or "
                             "the folder of an earlier snapshot")
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            manifest = json.load(fh)
    except (OSError, ValueError) as exc:
        raise UsageError(f"'{path}' cannot be read ({exc}): give another folder") from exc
    earlier = str(manifest.get("tenant") or "").rstrip("/").lower()
    if earlier and tenant and earlier != tenant.rstrip("/").lower():
        raise UsageError(f"'{folder}' holds a snapshot of {manifest['tenant']}, not of {tenant}: give a "
                         "folder per tenant")
    return manifest


def _read(path: str) -> str | None:
    try:
        with open(path, encoding="utf-8", newline="") as fh:
            return fh.read()
    except OSError:
        return None
