"""Criteria that select identities or match accounts on identity attributes, or that read a source
directly: role and dimension membership, segment visibility, account and manager correlation.

The source migration analysis uses them. When identity attributes are computed from the old
source, whatever selects or matches on those attributes may behave differently after the switch
(identities gaining or losing a role and its access, accounts no longer correlated), and criteria
that read the old source itself must be rewritten for the new one.
"""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import dataclass, field

from isc_reporter import attribute_usage
from isc_reporter.identity_profiles import INFO, WARNING

log = logging.getLogger("isc_reporter")

ROLE, DIMENSION, SEGMENT = "Role", "Role dimension", "Segment"
CORRELATION, MANAGER = "Account correlation", "Manager correlation"
TYPE_ORDER = (ROLE, DIMENSION, CORRELATION, MANAGER, SEGMENT)
ROLE_PAGE = 50                 # the roles and dimensions APIs return 50 items per call at most
PREFIX = "attribute."          # role criteria keys look like "attribute.department"
COLUMNS = ["Severity", "Type", "Object", "Enabled", "Criterion", "Uses", "Impact", "Action", "All criteria"]


@dataclass
class Objects:
    """Objects holding criteria. None means "not read"; *notes* says why."""

    roles: list[dict] | None = None          # dynamic roles carry their "dimensions"
    segments: list[dict] | None = None
    sources: list[dict] | None = None        # with their "correlationConfig" when read
    notes: list[str] = field(default_factory=list)


# -- reading ----------------------------------------------------------------------------------


def fetch(isc) -> Objects:
    """Roles (with the dimensions of dynamic roles), segments and sources (with their account
    correlation) from the tenant. A type that cannot be read is noted, and the analysis goes on."""
    from isc_reporter.api import call_or_none, error_message, list_all, model_json

    objects = Objects()
    try:
        roles = [model_json(r) for r in list_all(isc.roles.list_roles_v1, page_size=ROLE_PAGE)]
    except Exception as exc:
        objects.notes.append(f"Roles not read ({error_message(exc)}).")
    else:
        for role in roles:
            if not role.get("dimensional"):
                continue
            try:
                role["dimensions"] = [model_json(d) for d in list_all(
                    isc.dimensions.list_dimensions_v1, page_size=ROLE_PAGE, role_id=role.get("id", ""))]
            except Exception as exc:
                objects.notes.append(f"Dimensions of the role '{role.get('name', '')}' not read "
                                     f"({error_message(exc)}).")
        objects.roles = roles
    try:
        objects.segments = [model_json(s) for s in list_all(isc.segments.list_segments_v1)]
    except Exception as exc:
        objects.notes.append(f"Segments not read ({error_message(exc)}).")
    try:
        sources = [model_json(s) for s in list_all(isc.sources.list_sources_v1)]
    except Exception as exc:
        objects.notes.append(f"Sources not read ({error_message(exc)}): correlation not analysed.")
    else:
        unread = []
        for source in sources:
            try:
                config = call_or_none(isc.sources.get_correlation_config_v1, id=source.get("id", ""))
            except Exception:
                unread.append(source.get("name", ""))
                continue
            if config is not None:
                source["correlationConfig"] = model_json(config)
        if unread:
            objects.notes.append(f"Account correlation not read for {len(unread)} source(s): "
                                 f"{', '.join(unread[:10])}{' ...' if len(unread) > 10 else ''}.")
        objects.sources = sources
    counts = ["not" if found is None else str(len(found))
              for found in (objects.roles, objects.segments, objects.sources)]
    log.info("Criteria: %s role(s), %s segment(s), %s source(s) read", *counts)
    return objects


def load_files(paths: list[str]) -> Objects:
    """Roles, dimensions, segments and sources found in exported JSON files (a configuration export
    holds them as ROLE, SEGMENT and SOURCE objects). A type absent from the files is not analysed."""
    from isc_reporter.identity_profiles import iter_objects

    roles, dimensions, segments, sources = [], [], [], []
    for obj in iter_objects(paths):
        if "membership" in obj and obj.get("parentId"):
            dimensions.append(obj)
        elif "membership" in obj:
            roles.append(obj)
        elif "visibilityCriteria" in obj:
            segments.append(obj)
        elif "connector" in obj or "connectorAttributes" in obj:
            sources.append(obj)
    by_id = {role.get("id"): role for role in roles}
    for dimension in dimensions:
        if dimension["parentId"] in by_id:
            by_id[dimension["parentId"]].setdefault("dimensions", []).append(dimension)
    objects = Objects(roles or None, segments or None, sources or None)
    for kind, found in (("role", roles), ("segment", segments), ("source", sources)):
        if not found:
            objects.notes.append(f"No {kind} in the exported files: {kind} criteria not analysed.")
    return objects


# -- analysis ---------------------------------------------------------------------------------


def impacted_attributes(profiles: list[dict], transforms: dict[str, dict],
                        touched: dict[str, list[str]]) -> dict[str, str]:
    """Identity attributes whose values may change, with why: those computed from the old source
    (*touched*: attribute -> profiles), then those computed from them, until nothing is added."""
    impacted = {name: f"computed from the old source ({', '.join(where)})"
                for name, where in touched.items()}
    definitions = []
    for profile in profiles:
        config = profile.get("identityAttributeConfig") or {}
        definitions += [(m.get("identityAttributeName", ""), m.get("transformDefinition") or {})
                        for m in config.get("attributeTransforms") or []]
    known = {name for name, _ in definitions if name}
    changed = True
    while changed:
        changed = False
        lowered = {name.lower() for name in impacted}
        for name, definition in definitions:
            if not name or name.lower() in lowered:
                continue
            read = [attribute for attribute, _ in attribute_usage.references(definition, transforms, known)
                    if attribute.lower() in lowered]
            if read:
                impacted[name] = f"computed from '{read[0]}'"
                lowered.add(name.lower())
                changed = True
    return impacted


def old_source_attributes(objects: Objects, old) -> list[tuple[str, str]]:
    """``(account attribute of the old source, where)`` read by role criteria and by the manager
    correlation of the old source; they need a counterpart in the new source too."""
    found = []
    for role in objects.roles or []:
        for leaf in leaves((role.get("membership") or {}).get("criteria")):
            key = leaf.get("key") or {}
            if str(key.get("type", "")).upper() == "ACCOUNT" and old.id and key.get("sourceId") == old.id:
                found.append((key_attribute(key), f"role criteria: {role.get('name', '')}"))
    for source in objects.sources or []:
        account = (source.get("managerCorrelationMapping") or {}).get("accountAttributeName")
        if account and _is(source, old):
            found.append((account, "manager correlation"))
    return found


def findings(objects: Objects, impacted: dict[str, str], old, new, attributes: dict[str, dict]) -> list[dict]:
    """One row per criterion to review (see :data:`COLUMNS`), worst first."""
    names = {s.get("id", ""): s.get("name", "") for s in objects.sources or [] if s.get("id")}
    for source in (old, new):
        if source.id:
            names.setdefault(source.id, source.name)
    rows = []
    for role in objects.roles or []:
        enabled = role.get("enabled") is not False
        membership = role.get("membership") or {}
        if membership.get("criteria"):
            rows += _criteria_rows(ROLE, role.get("name", ""), enabled, membership["criteria"], impacted,
                                   old, new, attributes, names)
        for dimension in role.get("dimensions") or []:
            criteria = (dimension.get("membership") or {}).get("criteria")
            if criteria:
                rows += _criteria_rows(DIMENSION, f"{role.get('name', '')} / {dimension.get('name', '')}",
                                       enabled, criteria, impacted, old, new, attributes, names)
    for segment in objects.segments or []:
        rows += _segment_rows(segment, impacted)
    for source in objects.sources or []:
        if not _is(source, new):
            rows += _correlation_rows(source, impacted, old, new, attributes)
    rows.sort(key=lambda r: (r["Severity"] != WARNING, TYPE_ORDER.index(r["Type"]), r["Object"].lower()))
    return rows


def checks(rows: list[dict], notes: list[str]) -> list[tuple[str, str, str, str]]:
    """One ``(severity, object, check, details)`` per object to review, and the reading notes."""
    by_object: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for row in rows:
        by_object[(row["Type"], row["Object"])].append(row)
    out = []
    for (kind, name), items in by_object.items():
        severity = WARNING if any(r["Severity"] == WARNING for r in items) else INFO
        uses = ", ".join(dict.fromkeys(r["Uses"] for r in items))
        out.append((severity, name, f"{kind} criteria to review", f"Uses {uses} (see the Criteria sheet)."))
    out += [(INFO, "", "Criteria not fully analysed", note) for note in notes]
    return out


def _criteria_rows(kind, name, enabled, criteria, impacted, old, new, attributes, names) -> list[dict]:
    full = criteria_text(criteria, names)
    rows = []
    for leaf in leaves(criteria):
        key = leaf.get("key") or {}
        key_type = str(key.get("type", "")).upper()
        attribute = key_attribute(key)
        value = leaf.get("stringValue", "")
        if key_type == "IDENTITY":
            reason = _lookup(impacted, attribute)
            if reason is None:
                continue
            uses = f"identity attribute '{attribute}'"
            subject = "role" if kind == ROLE else "dimension"
            impact = (f"Identity attribute {reason}: if its values change, identities gain or lose the "
                      f"{subject} (and its access) at the next identity refresh.")
            action = f'Check that the new source gives the value "{value}" to the same identities.'
        elif old.id and key.get("sourceId") == old.id and key_type in ("ACCOUNT", "ENTITLEMENT"):
            if key_type == "ACCOUNT":
                uses = f"{old.name} account attribute '{attribute}'"
                target = (attributes.get(attribute) or {}).get("new_attribute")
                action = (f"Rewrite on {new.name}: {new.name}.{target}" if target
                          else _pending("Rewrite", new, attribute))
            else:
                uses = f"{old.name} entitlement '{attribute}'"
                action = f"Rewrite with the matching entitlement of {new.name}."
            impact = ("Reads the old source: once its accounts are gone the criterion no longer matches, "
                      "and the members lose the role and its access.")
        else:
            continue
        rows.append(_row(WARNING if enabled else INFO, kind, name, "yes" if enabled else "no",
                         _leaf_text(leaf, names), uses, impact, action, full))
    return rows


def _segment_rows(segment: dict, impacted: dict[str, str]) -> list[dict]:
    expression = (segment.get("visibilityCriteria") or {}).get("expression")
    full = segment_text(expression)
    rows = []
    for leaf in _segment_leaves(expression):
        attribute = leaf.get("attribute", "")
        reason = _lookup(impacted, attribute)
        if reason is not None:
            active = "no" if segment.get("active") is False else "yes"
            rows.append(_row(INFO, SEGMENT, segment.get("name", ""), active,
                             segment_text(leaf), f"identity attribute '{attribute}'",
                             f"Identity attribute {reason}: who can see and request the access of this "
                             "segment depends on it.",
                             "Check that the new values still match the segment.", full))
    return rows


def _correlation_rows(source, impacted, old, new, attributes) -> list[dict]:
    name = source.get("name", "")
    assignments = (source.get("correlationConfig") or {}).get("attributeAssignments") or []
    pairs = [f"{a.get('property', '')} = {a.get('value', '')}" for a in assignments]
    full = " AND ".join(pairs)
    manager = source.get("managerCorrelationMapping") or {}
    account, identity = manager.get("accountAttributeName", ""), manager.get("identityAttributeName", "")
    manager_text = f"{name}.{account} = identity.{identity}"
    rows = []
    if _is(source, old):
        for pair in pairs:
            rows.append(_row(INFO, CORRELATION, name, "", pair, f"the correlation of {old.name}",
                             f"Joins the accounts of {old.name} to identities.",
                             f"Recreate it on {new.name} (with its attribute names) before the first "
                             "aggregation, so that its accounts join the existing identities.", full))
        if account and identity:
            target = (attributes.get(account) or {}).get("new_attribute")
            action = (f"Recreate on {new.name}: {new.name}.{target} = identity.{identity}" if target
                      else _pending("Recreate", new, account))
            rows.append(_row(INFO, MANAGER, name, "", manager_text,
                             f"{old.name} account attribute '{account}'",
                             "Finds the manager of each identity.", action, manager_text))
        return rows
    for pair, assignment in zip(pairs, assignments, strict=True):
        # The API does not say which side is the identity attribute: both are checked.
        sides = [side for side in (assignment.get("property"), assignment.get("value")) if side]
        hit = next((side for side in sides if _lookup(impacted, side)), None)
        if hit:
            rows.append(_row(WARNING, CORRELATION, name, "", pair, f"identity attribute '{hit}'",
                             f"Identity attribute {_lookup(impacted, hit)}: if its values change, accounts "
                             f"of {name} may stop correlating (orphan accounts) or match another identity.",
                             "Compare its values before and after on a sample, then check the uncorrelated "
                             "accounts of this source after the switch.", full))
    reason = _lookup(impacted, identity) if identity else None
    if account and reason:
        rows.append(_row(WARNING, MANAGER, name, "", manager_text, f"identity attribute '{identity}'",
                         f"Identity attribute {reason}: the managers found through it may change.",
                         "Check the managers of a sample of identities after the switch.", manager_text))
    return rows


def _pending(verb: str, new, attribute: str) -> str:
    return f"{verb} on {new.name} once '{attribute}' has a counterpart in attribute-mapping.csv."


def _row(*values) -> dict:
    """A Criteria sheet row from its values, in the order of :data:`COLUMNS`."""
    return dict(zip(COLUMNS, values, strict=True))


def _is(source: dict, ref) -> bool:
    same_id = bool(ref.id) and source.get("id") == ref.id
    return same_id or str(source.get("name", "")).lower() == ref.name.lower()


def _lookup(impacted: dict[str, str], attribute: str) -> str | None:
    lowered = attribute.lower()
    return next((why for name, why in impacted.items() if name.lower() == lowered), None)


# -- readable criteria ------------------------------------------------------------------------


def key_attribute(key: dict) -> str:
    prop = str(key.get("property") or "")
    return prop[len(PREFIX):] if prop.startswith(PREFIX) else prop


def leaves(node):
    """The leaf criteria (one attribute, one operation, one value) of a role or dimension."""
    if not isinstance(node, dict):
        return
    if node.get("children"):
        for child in node["children"]:
            yield from leaves(child)
    elif node.get("key"):
        yield node


def _leaf_text(leaf: dict, names: dict[str, str]) -> str:
    key = leaf.get("key") or {}
    key_type = str(key.get("type", "")).upper()
    if key_type in ("ACCOUNT", "ENTITLEMENT"):
        source = names.get(key.get("sourceId") or "", key.get("sourceId") or "?")
        subject = f"{'entitlement ' if key_type == 'ENTITLEMENT' else ''}{source}.{key_attribute(key)}"
    else:
        subject = f"identity.{key_attribute(key)}"
    return f'{subject} {str(leaf.get("operation", "")).upper()} "{leaf.get("stringValue", "")}"'


def criteria_text(node, names: dict[str, str], depth: int = 0) -> str:
    """Readable form of role or dimension criteria, e.g. ``identity.department EQUALS "Finance"``."""
    if not isinstance(node, dict):
        return ""
    if node.get("children"):
        operation = str(node.get("operation") or "AND").upper()
        parts = [part for part in (criteria_text(c, names, depth + 1) for c in node["children"]) if part]
        joined = f" {operation} ".join(parts)
        return f"({joined})" if depth and len(parts) > 1 else joined
    return _leaf_text(node, names) if node.get("key") else ""


def _segment_leaves(expression):
    if not isinstance(expression, dict):
        return
    children = expression.get("children")
    if isinstance(children, list) and children:
        for child in children:
            yield from _segment_leaves(child)
    elif expression.get("attribute"):
        yield expression


def segment_text(expression) -> str:
    """Readable form of segment visibility criteria."""
    if not isinstance(expression, dict):
        return ""
    children = expression.get("children")
    if isinstance(children, list) and children:
        parts = [part for part in (segment_text(c) for c in children) if part]
        return f" {expression.get('operator') or 'AND'} ".join(parts)
    value = (expression.get("value") or {}).get("value", "")
    return f'identity.{expression.get("attribute", "")} {expression.get("operator", "")} "{value}"'
