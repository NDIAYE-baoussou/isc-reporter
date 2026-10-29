"""Interactive view of the identity attribute mappings: one HTML page that works offline.

The main view is a map of the named transforms: which one calls which, which identity attributes use
them and which identity attributes they read, laid out so that each transform comes after what it
depends on; selecting one lights up everything it depends on and everything that depends on it. A
second view shows, step by step, how one identity attribute or one named transform computes its
value. The page is self-contained (no internet access, no external script) and the data is embedded
as JSON.
"""

from __future__ import annotations

import html
import json
from importlib import resources

from isc_reporter import dashboard, identity_profiles, transform_graph, transform_tree

TEMPLATE = "templates/mappings.html"
DATA_MARKER = '<script id="data" type="application/json">__DATA__</script>'


def page_data(profiles: list[dict], transforms: dict[str, dict] | None = None,
              display_names: dict[str, str] | None = None, origin: str = "",
              policies: list[dict] | None = None) -> dict:
    """What the page shows: the dependencies between named transforms (``graph``), the profiles with the
    steps of each attribute, and the named transforms with their own steps."""
    transforms = transforms or {}
    profiles, all_rows, _ = identity_profiles.analyse(profiles, transforms, display_names, policies)
    used_by: dict[str, list[list[str]]] = {}
    out = []
    for profile, rows in zip(profiles, all_rows, strict=True):
        name = profile.get("name", "")
        for row in rows:
            for transform in filter(None, row["transforms"].split("\n")):
                used_by.setdefault(transform, []).append([name, row["identity_attribute"]])
        out.append({
            "name": name, "priority": profile.get("priority"), "identities": profile.get("identityCount"),
            "source": (profile.get("authoritativeSource") or {}).get("name", ""),
            "attributes": [{
                "name": row["identity_attribute"], "displayName": row["display_name"],
                "mapping": row["mapping"], "explanation": row["explanation"],
                "usedIn": row.get("used_in", ""),
                "checks": [{"severity": severity, "check": check, "details": details}
                           for severity, check, details in row["checks"]],
                "tree": transform_tree.to_dict(row["tree"])} for row in rows]})
    called_by: dict[str, list[str]] = {}
    for caller, definition in transforms.items():
        for called in transform_tree.calls(definition):
            called_by.setdefault(called, []).append(caller)
    named = []
    for name in sorted(set(transforms) | set(used_by), key=str.lower):
        definition = transforms.get(name)
        tree = transform_tree.build({"type": "reference", "attributes": {"id": name}}, transforms)
        named.append({
            "name": name, "type": (definition or {}).get("type", ""),
            "calls": transform_tree.calls(definition) if definition else [],
            "calledBy": sorted(called_by.get(name, []), key=str.lower), "usedBy": used_by.get(name, []),
            "tree": transform_tree.to_dict(tree),
            "definition": json.dumps(definition, ensure_ascii=False, indent=2) if definition else ""})
    graph = transform_graph.build(profiles, all_rows, transforms, set(display_names or {}) or None)
    for node in graph.nodes.values():
        if node["kind"] != transform_graph.ATTRIBUTE:
            node["usedIn"] = sorted(transform_graph.used_in(graph, node["id"]), key=str.lower)
    return {"subtitle": dashboard.subtitle(origin), "profiles": out, "transforms": named,
            "graph": transform_graph.to_dict(graph)}


def render(data: dict, title: str = "Identity attribute mappings") -> str:
    """The page, with *data* embedded. ``<``, ``>`` and ``&`` are escaped in the JSON, so that no value
    can end the script element."""
    template = resources.files("isc_reporter").joinpath(TEMPLATE).read_text(encoding="utf-8")
    payload = (json.dumps(data, ensure_ascii=False)
               .replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026"))
    page, marker, script = template.partition(DATA_MARKER)
    return page.replace("__TITLE__", html.escape(title)) + marker.replace("__DATA__", payload) + script


def write_html(path: str, profiles: list[dict], transforms: dict[str, dict] | None = None,
               display_names: dict[str, str] | None = None, origin: str = "",
               policies: list[dict] | None = None) -> int:
    """Write the interactive page; returns the number of profiles shown."""
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(render(page_data(profiles, transforms, display_names, origin, policies)))
    return len(profiles)
