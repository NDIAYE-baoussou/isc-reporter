"""Transform trees: how a value is computed, drawn as a tree with what each step does in plain words.

The root is what is computed (an identity attribute, or a named transform); below it, indented,
what feeds it: account attributes, identity attributes, fixed values, and the transforms in between.
A named transform is expanded where it is used, so the tree goes down to the data that is read.

    email
    └─ firstValid                 takes the first non-empty value
       ├─ choice 1: AD.mail       reads the account attribute 'mail' of AD
       ├─ choice 2: HR.e-mail     reads the account attribute 'e-mail' of HR
       └─ choice 3: "none"        fixed value
"""

from __future__ import annotations

from dataclasses import dataclass, field

MAX_DEPTH = 8

# What common transform types do, for people who do not know them by heart.
TRANSFORM_TYPES = {
    "accountAttribute": "reads an account attribute",
    "base64Decode": "decodes Base64", "base64Encode": "encodes in Base64",
    "concat": "joins several values", "conditional": "chooses between two values",
    "dateCompare": "compares two dates", "dateFormat": "changes a date format",
    "dateMath": "adds or subtracts time from a date",
    "decomposeDiacriticalMarks": "removes accents", "displayName": "builds a display name",
    "e164phone": "formats a phone number (E.164)", "firstValid": "takes the first non-empty value",
    "generateRandomString": "generates a random string",
    "getEndOfString": "takes the end of a string",
    "getReferenceIdentityAttribute": "reads an attribute of another identity (e.g. the manager)",
    "identityAttribute": "reads another identity attribute", "indexOf": "finds text in a string",
    "iso3166": "converts a country code", "lastIndexOf": "finds text from the end of a string",
    "leftPad": "pads on the left", "lookup": "translates values with a table",
    "lower": "converts to lower case", "normalizeNames": "normalizes person names",
    "randomAlphaNumeric": "generates a random alphanumeric value",
    "randomNumeric": "generates a random number", "reference": "calls a named transform",
    "replace": "replaces text (regular expression)", "replaceAll": "replaces text (table)",
    "rfc5646": "converts a language code", "rightPad": "pads on the right",
    "rule": "runs a rule", "split": "splits text and keeps one part",
    "static": "returns a fixed value or a Velocity expression", "substring": "takes part of a string",
    "trim": "removes surrounding spaces", "upper": "converts to upper case",
    "usernameGenerator": "generates a unique username", "uuid": "generates a unique id",
}
# Types that read or return a value themselves: a named transform of one of these types is drawn
# with its definition below it rather than merged into one line.
LEAF_TYPES = {"accountAttribute", "identityAttribute", "static", "rule", "reference"}
# How the items of a list are named, by transform type.
ITEM_NAMES = {"firstValid": "choice", "concat": "part"}
ACCOUNT_KEYS = {"sourceName", "applicationName", "sourceId", "applicationId", "attributeName"}
IGNORED_KEYS = {"requiresPeriodicRefresh"}
# What a node is, for colours and legends: the attribute computed, the data read, the steps between.
ATTRIBUTE, ACCOUNT, IDENTITY, VALUE = "attribute", "account", "identity", "value"
NAMED, MISSING, TRANSFORM, RULE = "named", "missing", "transform", "rule"

# A reference transform names the transform it calls in "id"; one without is shown under this name.
NO_ID = "(reference without id)"

# Line prefixes: (child, last child, below a child, below the last child).
BOX = ("├─ ", "└─ ", "│  ", "   ")
# For PDF fonts without box drawing characters; non-breaking spaces keep the indentation.
PLAIN = ("› ", "› ", " " * 4, " " * 4)


def describe_type(kind: str) -> str:
    return TRANSFORM_TYPES.get(kind, f"'{kind}' transform")


def attributes_of(definition: dict) -> dict:
    """The ``attributes`` of a transform definition, or ``{}`` when they are missing or not an object."""
    attributes = definition.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def reference_name(attrs: dict) -> str:
    """The named transform a reference calls: its ``id``, or :data:`NO_ID` when it has none."""
    name = attrs.get("id")
    return str(name) if name else NO_ID


@dataclass
class Node:
    label: str                             # what it is: HR.endDate, "none", firstValid, a transform name
    what: str = ""                         # what it does, in plain words
    details: str = ""                      # its options: date formats, table, separator...
    role: str = ""                         # its place in the parent: input, choice 1, firstDate...
    kind: str = TRANSFORM                  # what it is: ATTRIBUTE, ACCOUNT, NAMED... (above)
    children: list[Node] = field(default_factory=list)

    @property
    def text(self) -> str:
        return f"{self.role}: {self.label}" if self.role else self.label


def build(definition, transforms: dict[str, dict], role: str = "", seen: tuple[str, ...] = (),
          depth: int = 0) -> Node:
    """The tree of a transform definition; named transforms are expanded from *transforms*."""
    if not _is_transform(definition):
        return Node(_value(definition), "fixed value", role=role, kind=VALUE)
    kind, attrs = definition["type"], attributes_of(definition)
    if depth > MAX_DEPTH:
        return Node(kind, "not drawn further (too deep)", role=role)
    if kind == "accountAttribute":
        source = attrs.get("sourceName") or attrs.get("applicationName") or attrs.get("sourceId", "?")
        attribute = attrs.get("attributeName", "?")
        return Node(f"{source}.{attribute}", f"reads the account attribute '{attribute}' of {source}",
                    _details(attrs, ACCOUNT_KEYS), role, ACCOUNT)
    if kind == "identityAttribute":
        name = attrs.get("name", "?")
        return Node(f"identity.{name}", f"reads the identity attribute '{name}'", role=role, kind=IDENTITY)
    if kind == "static":
        value = str(attrs.get("value", ""))
        velocity = "$" in value or "#" in value
        return Node(f'"{value}"', "Velocity expression" if velocity else "fixed value", role=role, kind=VALUE)
    if kind == "rule":
        return Node(f"rule '{attrs.get('name', '?')}'", "runs a rule (code deployed by SailPoint, not shown)",
                    role=role, kind=RULE)
    if kind == "reference":
        return _reference(attrs, transforms, role, seen, depth)
    node = Node(kind, describe_type(kind), role=role)
    plain = {}
    for key, value in attrs.items():
        if _is_transform(value):
            node.children.append(build(value, transforms, key, seen, depth + 1))
        elif isinstance(value, list) and any(_is_transform(item) for item in value):
            item_name = ITEM_NAMES.get(kind, key)
            node.children += [build(item, transforms, f"{item_name} {index}", seen, depth + 1)
                              for index, item in enumerate(value, start=1)]
        else:
            plain[key] = value
    node.details = _details(plain)
    return node


def _reference(attrs: dict, transforms: dict[str, dict], role: str, seen: tuple[str, ...],
               depth: int) -> Node:
    name = reference_name(attrs)
    node = Node(name, role=role, kind=NAMED)
    if "input" in attrs:
        node.children.append(build(attrs["input"], transforms, "input", seen, depth + 1))
    target = transforms.get(name)
    if name == NO_ID:
        node.what, node.kind = "reference without id: it calls no named transform", MISSING
    elif name in seen:
        node.what = "named transform, already drawn above: it calls itself"
    elif target is None:
        node.what = "named transform, not found in the tenant or export" if transforms else "named transform"
        node.kind = MISSING if transforms else NAMED
    elif target.get("type") in LEAF_TYPES:
        node.what = "named transform"
        node.children.append(build(target, transforms, "", (*seen, name), depth + 1))
    else:
        inner = build(target, transforms, "", (*seen, name), depth + 1)
        node.what = f"named transform ({target.get('type')}): {inner.what}"
        node.details, node.children = inner.details, node.children + inner.children
    return node


def to_dict(node: Node) -> dict:
    """The tree as plain data (for JSON); empty fields are left out."""
    data = {"label": node.label, "what": node.what, "details": node.details, "role": node.role,
            "kind": node.kind,
            "children": [to_dict(child) for child in node.children]}
    return {key: value for key, value in data.items() if value}


def calls(definition) -> list[str]:
    """Names of the named transforms a definition calls directly (not those they call in turn)."""
    found: list[str] = []
    if isinstance(definition, list):
        for item in definition:
            found += calls(item)
    elif isinstance(definition, dict):
        if definition.get("type") == "reference":
            found.append(reference_name(attributes_of(definition)))
        for value in definition.values():
            found += calls(value)
    return list(dict.fromkeys(found))


def lines(root: Node, style: tuple[str, str, str, str] = BOX) -> list[tuple[int, str, Node]]:
    """``(depth, text with its prefix, node)`` for the root and every node below it, in order."""
    out = [(0, root.text, root)]

    def walk(node: Node, prefix: str, depth: int) -> None:
        for index, child in enumerate(node.children):
            last = index == len(node.children) - 1
            out.append((depth, prefix + style[1 if last else 0] + child.text, child))
            walk(child, prefix + style[3 if last else 2], depth + 1)

    walk(root, "", 1)
    return out


def draw(root: Node, style: tuple[str, str, str, str] = BOX) -> str:
    """The tree as text, one node per line."""
    return "\n".join(text for _, text, _ in lines(root, style))


def _is_transform(value) -> bool:
    return isinstance(value, dict) and "type" in value


def _value(value) -> str:
    return f'"{value}"' if isinstance(value, str) else str(value)


def _details(attrs: dict, skip: set[str] = frozenset()) -> str:
    """The options of a transform, e.g. ``inputFormat: dd/MM/yyyy; outputFormat: ISO8601``."""
    parts = []
    for key, value in attrs.items():
        if key in skip or key in IGNORED_KEYS:
            continue
        if isinstance(value, dict):
            pairs = [f"{k} = {v}" for k, v in list(value.items())[:3]]
            more = f", ... ({len(value)} in all)" if len(value) > 3 else ""
            parts.append(f"{key}: {', '.join(pairs)}{more}")
        elif isinstance(value, list):
            parts.append(f"{key}: {', '.join(_value(v) for v in value)}")
        else:
            parts.append(f"{key}: {value}")
    return "; ".join(parts)
