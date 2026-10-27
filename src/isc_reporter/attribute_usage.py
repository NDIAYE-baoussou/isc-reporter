"""Where each identity attribute is used: identity profile mappings, named transforms and the
provisioning policies of the sources.

A usage is a dict ``{attribute, where, object, location, role, detail}``:

* ``where`` is "Identity profile", "Transform" or "Provisioning policy";
* ``role`` is "Mapped" (the profile computes the attribute) or "Read" (something reads its value).
"""

from __future__ import annotations

import re
from collections import defaultdict

PROFILE, TRANSFORM, POLICY = "Identity profile", "Transform", "Provisioning policy"
MAPPED, READ = "Mapped", "Read"
MAX_DEPTH = 8
VELOCITY = re.compile(r"\$!?\{?([A-Za-z_]\w*)")
BRACES = re.compile(r"\{([A-Za-z_]\w*)\}")

SUMMARY_HEADERS = ["Identity attribute", "Display name", "Mapped in", "Read by profile mappings",
                   "Read by transforms", "Used in provisioning policies", "Sources", "Uses"]


def references(node, transforms: dict[str, dict], known: set[str], *, via: str = "",
               seen: frozenset = frozenset(), depth: int = 0) -> list[tuple[str, str]]:
    """``(identity attribute, how)`` for every identity attribute read by a transform definition.

    Named transforms called with ``reference`` are followed (``how`` then names the transform).
    Velocity expressions ($name, ${name}) and ``{name}`` templates count when *name* is a known
    identity attribute. Rules are code and cannot be read.
    """
    found: list[tuple[str, str]] = []
    if depth > MAX_DEPTH:
        return found
    if isinstance(node, list):
        for item in node:
            found += references(item, transforms, known, via=via, seen=seen, depth=depth + 1)
    elif isinstance(node, dict):
        kind, attrs = node.get("type"), node.get("attributes")
        if kind and isinstance(attrs, dict):
            if kind == "identityAttribute" and attrs.get("name"):
                found.append((attrs["name"], f"identityAttribute{_via(via)}"))
            elif kind == "getReferenceIdentityAttribute" and attrs.get("attributeName"):
                found.append((attrs["attributeName"], f"attribute of a referenced identity{_via(via)}"))
            elif kind == "reference":
                name = attrs.get("id")
                if name in transforms and name not in seen:
                    found += references(transforms[name], transforms, known, via=name,
                                        seen=seen | {name}, depth=depth + 1)
            for key, value in attrs.items():
                if isinstance(value, str):
                    found += _in_text(value, known, template=key == "template" or kind == "static",
                                      via=via)
                else:
                    found += references(value, transforms, known, via=via, seen=seen, depth=depth + 1)
        else:
            for value in node.values():
                found += references(value, transforms, known, via=via, seen=seen, depth=depth + 1)
    return list(dict.fromkeys(found))


def _via(via: str) -> str:
    return f" (through the transform '{via}')" if via else ""


def _in_text(text: str, known: set[str], *, template: bool, via: str) -> list[tuple[str, str]]:
    names = VELOCITY.findall(text) + (BRACES.findall(text) if template else [])
    return [(name, f"mentioned in a value or template{_via(via)}") for name in names if name in known]


def collect(profiles: list[dict], mapping_rows: list[list[dict]], transforms: dict[str, dict],
            policies: list[dict], known: set[str]) -> list[dict]:
    """Every usage of an identity attribute, in profiles, named transforms and provisioning policies."""
    usages: list[dict] = []

    def add(attribute, where, obj, location, role, detail):
        usages.append({"attribute": attribute, "where": where, "object": obj, "location": location,
                       "role": role, "detail": detail})

    for profile, rows in zip(profiles, mapping_rows, strict=True):
        name = profile.get("name", "")
        config = profile.get("identityAttributeConfig") or {}
        definitions = {m.get("identityAttributeName", ""): m.get("transformDefinition") or {}
                       for m in config.get("attributeTransforms") or []}
        for row in rows:
            attribute = row["identity_attribute"]
            add(attribute, PROFILE, name, f"mapping of '{attribute}'", MAPPED, row["explanation"])
            for read, how in references(definitions.get(attribute), transforms, known):
                add(read, PROFILE, name, f"mapping of '{attribute}'", READ, how)
    for name, definition in sorted(transforms.items(), key=lambda kv: kv[0].lower()):
        for read, how in references(definition, transforms, known, seen=frozenset({name})):
            add(read, TRANSFORM, name, f"type {definition.get('type', '?')}", READ, how)
    for policy in policies:
        source, usage = policy.get("source", ""), policy.get("usageType", "")
        for field in policy.get("fields") or []:
            field_name = field.get("name", "?")
            parts = [field.get("transform"), field.get("attributes")]
            for read, how in references(parts, transforms, known):
                add(read, POLICY, f"{source} / {usage}", f"field '{field_name}'", READ, how)
                usages[-1]["source"] = source
    return usages


def detail_rows(usages: list[dict]) -> list[dict]:
    return [{"Identity attribute": u["attribute"], "Where": u["where"], "Object": u["object"],
             "Location": u["location"], "Role": u["role"], "Detail": u["detail"]}
            for u in sorted(usages, key=lambda u: (u["attribute"].lower(), u["where"], u["object"].lower(),
                                                   u["role"] != MAPPED, u["location"]))]


def used_in(usages: list[dict]) -> dict[str, str]:
    """Per attribute, where something *reads* it, one line each (for the profile sheets)."""
    lines: dict[str, list[str]] = defaultdict(list)
    for u in usages:
        if u["role"] == READ:
            line = f"{u['where']} {u['object']}: {u['location']}"
            if line not in lines[u["attribute"]]:
                lines[u["attribute"]].append(line)
    return {attribute: "\n".join(items) for attribute, items in lines.items()}


def summary_rows(usages: list[dict], profile_names: list[str], display_names) -> list[dict]:
    """One line per identity attribute (mapped in a profile, or read somewhere)."""
    by_attribute: dict[str, list[dict]] = defaultdict(list)
    for u in usages:
        by_attribute[u["attribute"]].append(u)
    rows = []
    for attribute in sorted(by_attribute, key=str.lower):
        items = by_attribute[attribute]
        mapped = list(dict.fromkeys(u["object"] for u in items if u["role"] == MAPPED))
        reads = [u for u in items if u["role"] == READ]

        policies = [u for u in reads if u["where"] == POLICY]
        rows.append({
            "Identity attribute": attribute, "Display name": display_names(attribute),
            "Mapped in": (f"{len(mapped)} / {len(profile_names)}: " + ", ".join(mapped)) if mapped
            else f"0 / {len(profile_names)}",
            "Read by profile mappings": _join(reads, PROFILE),
            "Read by transforms": _join(reads, TRANSFORM, with_location=False),
            "Used in provisioning policies": _join(reads, POLICY),
            "Sources": "\n".join(dict.fromkeys(u["source"] for u in policies)),
            "Uses": len(reads)})
    return rows


def _join(reads: list[dict], where: str, with_location: bool = True) -> str:
    return "\n".join(dict.fromkeys(
        f"{u['object']}: {u['location']}" if with_location else u["object"]
        for u in reads if u["where"] == where))
