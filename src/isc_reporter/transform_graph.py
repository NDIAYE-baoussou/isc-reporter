"""Dependencies between named transforms: which named transform calls which, which identity attributes
use them, and which identity attributes they read (attributes that other transforms compute in turn).

Only named transforms and the identity attributes that link them are nodes; what happens inside a
transform (account attributes, fixed values, inline steps) is left out. An edge goes from a
dependency to what depends on it:

* a named transform *calls* another one (``reference``);
* an identity attribute *uses* a named transform (its mapping in a profile calls it);
* a named transform *reads* an identity attribute (``identityAttribute``, Velocity), whose mapping
  may itself use other named transforms.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from isc_reporter import attribute_usage, transform_tree
from isc_reporter.transform_tree import describe_type

TRANSFORM, MISSING, ATTRIBUTE = "transform", "missing", "attribute"
CALLS, USES, READS = "calls", "uses", "reads"
TREE_KINDS = {TRANSFORM: transform_tree.NAMED, MISSING: transform_tree.MISSING,
              ATTRIBUTE: transform_tree.ATTRIBUTE}
NOT_USED = "not used by any identity profile"


@dataclass
class Graph:
    nodes: dict[str, dict] = field(default_factory=dict)      # id -> {id, kind, label, what, ...}
    edges: list[dict] = field(default_factory=list)           # {from: dependency, to: dependent, kind}
    _dependencies: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    _dependents: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def add(self, dependency: str, dependent: str, kind: str) -> None:
        if (dependency, kind) in self._dependencies.get(dependent, []):
            return
        self.edges.append({"from": dependency, "to": dependent, "kind": kind})
        self._dependencies.setdefault(dependent, []).append((dependency, kind))
        self._dependents.setdefault(dependency, []).append((dependent, kind))

    def dependencies(self, node_id: str) -> list[tuple[str, str]]:
        """``(node id, edge kind)`` of what the node depends on."""
        return self._dependencies.get(node_id, [])

    def dependents(self, node_id: str) -> list[tuple[str, str]]:
        """``(node id, edge kind)`` of what depends on the node."""
        return self._dependents.get(node_id, [])


def transform_id(name: str) -> str:
    return f"t:{name}"


def attribute_id(profile: str, attribute: str) -> str:
    return f"a:{profile}/{attribute}"


def build(profiles: list[dict], all_rows: list[list[dict]], transforms: dict[str, dict],
          known: set[str] | None = None) -> Graph:
    """The dependency graph of the named transforms (every one of *transforms*, used or not) and of the
    identity attributes of *profiles* that use them or that they read."""
    graph = Graph()

    def transform(name: str) -> str:
        node_id = transform_id(name)
        if node_id not in graph.nodes:
            definition = transforms.get(name)
            kind = (definition or {}).get("type", "")
            graph.nodes[node_id] = {
                "id": node_id, "kind": TRANSFORM if definition else MISSING, "label": name, "type": kind,
                "what": f"{kind}: {describe_type(kind)}" if definition
                else "not found in the tenant or export"}
        return node_id

    def attribute(profile: str, row: dict) -> str:
        node_id = attribute_id(profile, row["identity_attribute"])
        graph.nodes.setdefault(node_id, {
            "id": node_id, "kind": ATTRIBUTE, "label": row["identity_attribute"], "profile": profile,
            "mapping": row["mapping"], "what": f"identity attribute ({profile}, {row['mapping'].lower()})"})
        return node_id

    for name in sorted(transforms, key=str.lower):
        transform(name)
    for name, definition in transforms.items():
        for called in transform_tree.calls(definition):
            graph.add(transform(called), transform(name), CALLS)
    mapped: dict[str, list[tuple[str, dict]]] = {}
    for profile, rows in zip(profiles, all_rows, strict=True):
        name = profile.get("name", "")
        for row in rows:
            mapped.setdefault(row["identity_attribute"].lower(), []).append((name, row))
            for called in transform_tree.calls(json.loads(row.get("definition") or "{}")):
                graph.add(transform(called), attribute(name, row), USES)
    known = set(known or ()) | {row["identity_attribute"] for rows in all_rows for row in rows}
    for name, definition in transforms.items():
        # A transform reads the attributes of the identity it computes a value for: their mappings in the
        # profiles where it is used (in every profile when it is not used). Only what it reads itself:
        # the transforms it calls have their own edges.
        where = used_in(graph, transform_id(name))
        for read, _ in attribute_usage.references(definition, {}, known):
            for profile, row in mapped.get(read.lower(), []):
                if not where or profile in where:
                    graph.add(attribute(profile, row), transform(name), READS)
    return graph


def used_in(graph: Graph, node_id: str, seen: frozenset = frozenset()) -> set[str]:
    """The profiles whose identity attributes use the node, directly or through other transforms."""
    found: set[str] = set()
    for dependent, kind in graph.dependents(node_id):
        if kind == USES:
            found.add(graph.nodes[dependent]["profile"])
        elif kind == CALLS and dependent not in seen:
            found |= used_in(graph, dependent, seen | {node_id})
    return found


def tree(graph: Graph, node_id: str, role: str = "", seen: tuple[str, ...] = (),
         profile: str | None = None) -> transform_tree.Node:
    """What *node_id* depends on, as a tree: the transforms it calls or uses, the attributes it reads.
    Below an identity attribute, only the attributes of its own profile are read."""
    info = graph.nodes[node_id]
    if info["kind"] == ATTRIBUTE:
        profile = info["profile"]
    label = f"{info['label']} ({info['profile']})" if info["kind"] == ATTRIBUTE and seen else info["label"]
    node = transform_tree.Node(label, info["what"], role=role, kind=TREE_KINDS[info["kind"]])
    if node_id in seen:
        node.what = "already shown above: circular dependency"
        return node
    for dependency, kind in graph.dependencies(node_id):
        if kind == READS and profile is not None and graph.nodes[dependency]["profile"] != profile:
            continue
        node.children.append(tree(graph, dependency, kind, (*seen, node_id), profile))
    return node


def roots(graph: Graph, profiles: list[dict], all_rows: list[list[dict]]) -> list[tuple[str, str]]:
    """``(starting point, node id)`` of every tree to draw: each identity attribute that uses a named
    transform, in profile order, then each named transform that nothing uses."""
    found = []
    for profile, rows in zip(profiles, all_rows, strict=True):
        name = profile.get("name", "")
        for row in rows:
            node_id = attribute_id(name, row["identity_attribute"])
            if any(kind == USES for _, kind in graph.dependencies(node_id)):
                found.append((f"{name}: {row['identity_attribute']}", node_id))
    unused = [node_id for node_id, info in graph.nodes.items()
              if info["kind"] != ATTRIBUTE and not used_in(graph, node_id)]
    shown: set[str] = set()
    tops = [node_id for node_id in unused if not graph.dependents(node_id)]
    for node_id in tops + unused:          # then those still left out: they call each other in a loop
        if node_id not in shown:
            found.append((NOT_USED, node_id))
            shown |= reachable(graph, node_id)
    return found


def reachable(graph: Graph, node_id: str) -> set[str]:
    """The node and everything it depends on, directly or not."""
    found, stack = set(), [node_id]
    while stack:
        current = stack.pop()
        if current not in found:
            found.add(current)
            stack += [dependency for dependency, _ in graph.dependencies(current)]
    return found


def rows(graph: Graph, profiles: list[dict], all_rows: list[list[dict]],
         style: tuple[str, str, str, str] = transform_tree.BOX) -> list[dict]:
    """One row per line of every dependency tree (see :func:`roots`)."""
    out = []
    for start, node_id in roots(graph, profiles, all_rows):
        for depth, line, node in transform_tree.lines(tree(graph, node_id), style):
            out.append({"Used by": start, "Tree": line, "What it is": node.what, "Level": depth})
    return out


def to_dict(graph: Graph) -> dict:
    return {"nodes": list(graph.nodes.values()), "edges": graph.edges}
