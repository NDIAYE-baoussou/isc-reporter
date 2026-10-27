"""Search-based reports: what to show for each kind of ISC object, and how to summarise it.

A report turns search documents into

* **rows**: one line per object; nested lists (an identity's access, a role's access profiles...)
  are consolidated into columns instead of repeating the object on several lines;
* optional **detail rows**: one line per nested item (e.g. identity x access item), like the UI export;
* **figures** and **breakdowns** (counts per category) for the summary and charts.

This module has no I/O; :mod:`isc_reporter.report_writers` renders reports to Excel, PDF and CSV.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field

from isc_reporter.errors import UsageError

LIST_SEP = " | "
TOP = 15
OTHER = "(other)"
EMPTY = "(empty)"


# -- reading documents ----------------------------------------------------------


def get(doc, path: str):
    """Value at a dotted *path* (``manager.displayName``); lists of objects give lists of values."""
    value = doc
    for part in path.split("."):
        if isinstance(value, list):
            value = [v.get(part) if isinstance(v, dict) else None for v in value]
            value = [v for v in value if v not in (None, "", [])]
        elif isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def text(value) -> str:
    """Cell text: lists joined with `` | ``, objects as JSON, booleans as yes/no."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return LIST_SEP.join(text(v) for v in value if v not in (None, ""))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def cell(value):
    """Keep numbers as numbers (sortable, chartable in Excel); everything else as text."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return value
    return text(value)


# -- report definition -----------------------------------------------------------


@dataclass(frozen=True)
class Col:
    header: str
    value: Callable[[dict], object]
    width: int = 18
    in_pdf: bool = True


@dataclass(frozen=True)
class Breakdown:
    """A count of documents (or nested items) per category, shown as a table and a chart."""

    title: str
    values: Callable[[dict], list]
    top: int = TOP
    date: bool = False          # group dates by day or month
    order: list[str] | None = None  # fixed category order (e.g. buckets)


@dataclass(frozen=True)
class Expand:
    """Detail rows: one per nested item, with a few columns of the parent object."""

    path: str
    title: str
    parent: list[Col]
    item: list[Col]


@dataclass
class ReportType:
    name: str
    index: str
    description: str
    default_query: str = "*"
    columns: list[Col] = field(default_factory=list)
    breakdowns: list[Breakdown] = field(default_factory=list)
    figures: list[tuple[str, Callable[[list[dict]], object]]] = field(default_factory=list)
    expands: dict[str, Expand] = field(default_factory=dict)
    sort_field: str | None = "created"


def col(path: str, header: str | None = None, width: int = 18, in_pdf: bool = True) -> Col:
    return Col(header or _header(path), lambda doc: get(doc, path), width, in_pdf)


def _header(path: str) -> str:
    last = path.split(".")[-1] if not path.startswith("attributes.") else path[len("attributes."):]
    spaced = "".join(" " + c.lower() if c.isupper() else c for c in last)
    return spaced[:1].upper() + spaced[1:]


def items(doc, path: str, kind: str | None = None) -> list[dict]:
    """The nested objects of a list field, optionally only those of a given ``type``."""
    found = doc.get(path) if isinstance(doc, dict) else None
    return [i for i in found or [] if isinstance(i, dict) and (kind is None or i.get("type") == kind)]


def access_label(item: dict) -> str:
    name = item.get("displayName") or item.get("name") or item.get("value") or item.get("id", "")
    source = (item.get("source") or {}).get("name")
    return f"{name} ({source})" if source and item.get("type") == "ENTITLEMENT" else str(name)


def account_label(item: dict) -> str:
    source = (item.get("source") or {}).get("name", "")
    name = item.get("name") or item.get("accountId") or item.get("id", "")
    flags = [f for f, on in (("disabled", item.get("disabled")), ("locked", item.get("locked"))) if on]
    return f"{source}: {name}" + (f" ({', '.join(flags)})" if flags else "")


def nested(path: str, header: str, label=access_label, kind: str | None = None, width: int = 45,
           in_pdf: bool = True) -> Col:
    return Col(header, lambda doc: sorted({label(i) for i in items(doc, path, kind)}, key=str.lower),
               width, in_pdf)


def count(path: str, header: str, kind: str | None = None) -> Col:
    return Col(header, lambda doc: len(items(doc, path, kind)), 18)


def by(path: str, title: str | None = None, **kwargs) -> Breakdown:
    def values(doc):
        value = get(doc, path)
        return value if isinstance(value, list) else [value]
    return Breakdown(title or _header(path), values, **kwargs)


def buckets(path: str, title: str, limits=(0, 5, 20, 50, 100)) -> Breakdown:
    """Numeric field grouped in ranges: limits (0, 5, 20) give 0, 1-5, 6-20, > 20."""
    labels, low = [], None
    for limit in limits:
        labels.append(str(limit) if low is None or low == limit else f"{low}-{limit}")
        low = limit + 1
    labels.append(f"> {limits[-1]}")

    def values(doc):
        number = get(doc, path) or 0
        for limit, label in zip(limits, labels, strict=False):  # one more label than limits
            if number <= limit:
                return [label]
        return [labels[-1]]
    return Breakdown(title, values, order=labels)


def share(predicate) -> Callable[[list[dict]], str]:
    def figure(docs):
        n = sum(1 for d in docs if predicate(d))
        return f"{n} ({n / len(docs):.0%})" if docs else "0"
    return figure


def average(path: str) -> Callable[[list[dict]], str]:
    def figure(docs):
        values = [get(d, path) or 0 for d in docs]
        return f"{sum(values) / len(values):.1f} (max {max(values)})" if values else "-"
    return figure


def _attribute_changes(request: dict) -> list[str]:
    return [f"{a.get('op', '')} {a.get('name', '')}={text(a.get('value'))}"
            for a in request.get("attributeRequests") or []]


IDENTITY_KEY = [col("displayName", "Identity", 24), col("attributes.uid", "Username", 16),
                col("email", "Email", 26)]

REPORT_TYPES = {r.name: r for r in [
    ReportType(
        "identities", "identities", "Identities with their access, accounts and manager on one line",
        columns=[
            col("displayName", "Identity", 24), col("attributes.uid", "Username", 16),
            col("email", "Email", 26), col("attributes.cloudLifecycleState", "Lifecycle state", 14),
            col("identityProfile.name", "Identity profile", 20, in_pdf=False),
            col("source.name", "Authoritative source", 18, in_pdf=False),
            col("attributes.department", "Department", 18), col("manager.displayName", "Manager", 22),
            col("inactive", "Inactive", 9, in_pdf=False),
            count("access", "Roles", "ROLE"), count("access", "Access profiles", "ACCESS_PROFILE"),
            count("access", "Entitlements", "ENTITLEMENT"), count("accounts", "Accounts"),
            nested("access", "Role list", kind="ROLE"),
            nested("access", "Access profile list", kind="ACCESS_PROFILE"),
            nested("access", "Entitlement list", kind="ENTITLEMENT", in_pdf=False),
            nested("accounts", "Account list", label=account_label, in_pdf=False),
            col("created", "Created", 20, in_pdf=False),
        ],
        breakdowns=[
            by("attributes.cloudLifecycleState", "Lifecycle state"),
            by("identityProfile.name", "Identity profile"),
            by("attributes.department", "Department"),
            buckets("accessCount", "Access items per identity"),
            Breakdown("Most held roles", lambda d: [access_label(i) for i in items(d, "access", "ROLE")]),
            Breakdown("Most held access profiles",
                      lambda d: [access_label(i) for i in items(d, "access", "ACCESS_PROFILE")]),
            Breakdown("Accounts per source",
                      lambda d: [(i.get("source") or {}).get("name", "") for i in items(d, "accounts")]),
        ],
        figures=[
            ("Inactive identities", share(lambda d: d.get("inactive"))),
            ("Identities without a manager", share(lambda d: not get(d, "manager.id"))),
            ("Access items per identity (average)", average("accessCount")),
            ("Accounts per identity (average)", average("accountCount")),
        ],
        expands={
            "access": Expand("access", "Access", IDENTITY_KEY, [
                col("type", "Access type", 15), Col("Access", access_label, 40),
                col("source.name", "Source", 20), col("privileged", "Privileged", 10),
                col("standalone", "Standalone", 10)]),
            "accounts": Expand("accounts", "Accounts", IDENTITY_KEY, [
                col("source.name", "Source", 22), col("name", "Account", 30),
                col("disabled", "Disabled", 9), col("locked", "Locked", 9),
                col("privileged", "Privileged", 10)]),
            "apps": Expand("apps", "Apps", IDENTITY_KEY, [
                col("name", "Application", 30), col("source.name", "Source", 22),
                col("account.accountId", "Account", 26)]),
        },
    ),
    ReportType(
        "access-profiles", "accessprofiles", "Access profiles with their entitlements on one line",
        columns=[
            col("name", "Access profile", 30), col("source.name", "Source", 20),
            col("owner.name", "Owner", 20), col("enabled", "Enabled", 9),
            col("requestable", "Requestable", 11), count("entitlements", "Entitlements"),
            nested("entitlements", "Entitlement list"),
            col("description", "Description", 40, in_pdf=False), col("created", "Created", 20, in_pdf=False),
        ],
        breakdowns=[by("source.name", "Source"), by("enabled", "Enabled"),
                    by("requestable", "Requestable"), by("owner.name", "Owner"),
                    buckets("entitlementCount", "Entitlements per access profile", (0, 1, 5, 20, 50))],
        figures=[("Disabled", share(lambda d: d.get("enabled") is False)),
                 ("Not requestable", share(lambda d: d.get("requestable") is False)),
                 ("Without entitlements", share(lambda d: not d.get("entitlements")))],
        expands={"entitlements": Expand("entitlements", "Entitlements", [
            col("name", "Access profile", 30), col("source.name", "Source", 20)], [
            col("name", "Entitlement", 34), col("value", "Value", 40), col("attribute", "Attribute", 14),
            col("privileged", "Privileged", 10)])},
    ),
    ReportType(
        "roles", "roles", "Roles with their access profiles and entitlements on one line",
        columns=[
            col("name", "Role", 30), col("owner.name", "Owner", 20), col("enabled", "Enabled", 9),
            col("requestable", "Requestable", 11), count("accessProfiles", "Access profiles"),
            count("entitlements", "Entitlements"), nested("accessProfiles", "Access profile list"),
            nested("entitlements", "Entitlement list", in_pdf=False),
            col("description", "Description", 40, in_pdf=False), col("created", "Created", 20, in_pdf=False),
        ],
        breakdowns=[by("enabled", "Enabled"), by("requestable", "Requestable"), by("owner.name", "Owner"),
                    buckets("accessProfileCount", "Access profiles per role", (0, 1, 5, 10, 20))],
        figures=[("Disabled", share(lambda d: d.get("enabled") is False)),
                 ("Without access profiles or entitlements",
                  share(lambda d: not d.get("accessProfiles") and not d.get("entitlements")))],
        expands={"access-profiles": Expand("accessProfiles", "Access profiles", [
            col("name", "Role", 30), col("owner.name", "Owner", 20)], [
            col("name", "Access profile", 34), col("source.name", "Source", 22)])},
    ),
    ReportType(
        "entitlements", "entitlements", "Entitlements with their source, owner and flags",
        columns=[
            col("displayName", "Entitlement", 30), col("value", "Value", 40),
            col("attribute", "Attribute", 14), col("source.name", "Source", 22),
            col("owner.name", "Owner", 20), col("privileged", "Privileged", 10),
            col("requestable", "Requestable", 11), col("description", "Description", 40, in_pdf=False),
        ],
        breakdowns=[by("source.name", "Source"), by("privileged", "Privileged"),
                    by("requestable", "Requestable"), by("attribute", "Attribute")],
        figures=[("Privileged", share(lambda d: d.get("privileged"))),
                 ("Requestable", share(lambda d: d.get("requestable"))),
                 ("Without owner", share(lambda d: not get(d, "owner.id")))],
    ),
    ReportType(
        "events", "events", "Audit events", default_query="*",
        columns=[
            col("created", "Date", 20), col("name", "Event", 30), col("type", "Type", 16),
            col("status", "Status", 10), col("actor.name", "Actor", 22), col("target.name", "Target", 22),
            col("attributes.sourceName", "Source", 18, in_pdf=False),
            col("technicalName", "Technical name", 28, in_pdf=False),
            col("trackingNumber", "Tracking number", 22, in_pdf=False),
        ],
        breakdowns=[Breakdown("Events per day", lambda d: [d.get("created")], date=True),
                    by("type", "Type"), by("status", "Status"), by("technicalName", "Technical name"),
                    by("actor.name", "Actor")],
        figures=[("Failed", share(lambda d: str(d.get("status", "")).upper() == "FAILED")),
                 ("Distinct actors", lambda docs: len({get(d, "actor.name") for d in docs}))],
    ),
    ReportType(
        "account-activities", "accountactivities", "Account activities with their account requests",
        columns=[
            col("created", "Date", 20), col("action", "Action", 18), col("status", "Status", 12),
            col("requester.name", "Requester", 20), col("recipient.name", "Recipient", 20),
            col("sources", "Sources", 22),
            nested("accountRequests", "Account requests", width=50, label=lambda r: (
                f"{r.get('op', '')} {(r.get('source') or {}).get('name', '')} {r.get('accountId', '')}: "
                f"{(r.get('result') or {}).get('status', '')}").strip()),
            col("trackingNumber", "Tracking number", 22, in_pdf=False),
        ],
        breakdowns=[Breakdown("Activities per day", lambda d: [d.get("created")], date=True),
                    by("action", "Action"), by("status", "Status"), by("sources", "Source")],
        figures=[("Not completed", share(lambda d: str(d.get("status", "")).lower()
                                         not in ("complete", "completed", "success")))],
        expands={"account-requests": Expand("accountRequests", "Account requests", [
            col("created", "Date", 20), col("action", "Action", 16),
            col("recipient.name", "Recipient", 20)], [
            col("op", "Operation", 10), col("source.name", "Source", 20), col("accountId", "Account", 30),
            col("result.status", "Result", 12), Col("Attribute changes", _attribute_changes, 50)])},
    ),
    ReportType(
        "workflows", "workflowsummaries", "Workflow executions",
        breakdowns=[Breakdown("Executions per day", lambda d: [d.get("created")], date=True)],
    ),
]}


# -- running a report ---------------------------------------------------------------


@dataclass
class Report:
    title: str
    report_type: ReportType
    query: str
    docs: list[dict]
    columns: list[Col]
    breakdowns: list[Breakdown]
    expand: Expand | None = None
    origin: str = ""
    truncated: bool = False

    @property
    def headers(self) -> list[str]:
        return [c.header for c in self.columns]

    def rows(self) -> list[list]:
        return [[cell(c.value(doc)) for c in self.columns] for doc in self.docs]

    def detail_headers(self) -> list[str]:
        return [c.header for c in self.expand.parent + self.expand.item] if self.expand else []

    def detail_rows(self) -> list[list]:
        """One line per nested item; objects without items still get one line."""
        if not self.expand:
            return []
        rows = []
        for doc in self.docs:
            parent = [cell(c.value(doc)) for c in self.expand.parent]
            children = items(doc, self.expand.path) or [None]
            for child in children:
                rows.append(parent + [cell(c.value(child)) if child else "" for c in self.expand.item])
        return rows

    def figures(self) -> list[tuple[str, object]]:
        base = [("Results", len(self.docs))]
        if self.truncated:
            base.append(("Note", "limited by --limit: more results exist"))
        return base + [(label, func(self.docs)) for label, func in self.report_type.figures]

    def breakdown(self, b: Breakdown) -> list[tuple[str, int]]:
        """``(category, count)``, most frequent first, the tail grouped as (other)."""
        counts: Counter = Counter()
        for doc in self.docs:
            # An empty list (no nested items) adds nothing; a blank field counts as (empty).
            for value in b.values(doc):
                if b.date:
                    value = str(value or "")[:10 if self._daily() else 7] or None
                counts[text(value) or EMPTY] += 1
        if b.date:
            return sorted(counts.items())
        if b.order:
            return [(label, counts.get(label, 0)) for label in b.order]
        ranked = counts.most_common()
        if len(ranked) > b.top:
            ranked = ranked[:b.top] + [(OTHER, sum(n for _, n in ranked[b.top:]))]
        return ranked

    def _daily(self) -> bool:
        days = sorted({str(d.get("created", ""))[:10] for d in self.docs if d.get("created")})
        return len(days) <= 62


def build(report_type: ReportType, docs: list[dict], *, query: str = "", title: str = "",
          columns: list[str] | None = None, group_by: list[str] | None = None,
          expand: str | None = None, origin: str = "", truncated: bool = False) -> Report:
    """Assemble a report; *columns* / *group_by* (dotted paths) replace the type's defaults."""
    chosen = [col(path) for path in columns] if columns else (report_type.columns or auto_columns(docs))
    breakdowns = [by(path) for path in group_by] if group_by else report_type.breakdowns
    detail = None
    if expand:
        detail = report_type.expands.get(expand)
        if detail is None:
            options = ", ".join(report_type.expands) or "none"
            raise UsageError(f"'{report_type.name}' cannot expand {expand!r} (choose: {options})")
    default_title = report_type.name.replace("-", " ").capitalize() + " report"
    return Report(title or default_title, report_type, query or report_type.default_query,
                  docs, chosen, breakdowns, detail, origin, truncated)


def auto_columns(docs: list[dict], limit: int = 25) -> list[Col]:
    """Columns for an index without a predefined layout: the most common scalar fields."""
    seen: Counter = Counter()
    for doc in docs[:500]:
        for key, value in doc.items():
            if key.startswith("_"):
                continue
            if isinstance(value, dict):
                seen.update(f"{key}.{k}" for k, v in value.items() if not isinstance(v, (dict, list)))
            elif not isinstance(value, list):
                seen[key] += 1
    preferred = ["name", "displayName", "created"]
    paths = sorted(seen, key=lambda p: (p not in preferred, preferred.index(p) if p in preferred else 0,
                                        -seen[p], p))
    return [col(path) for path in paths[:limit]] or [col("id")]


def load_definition(path: str) -> dict:
    """A saved report: JSON with ``type`` and optional ``query``, ``title``, ``columns``,
    ``group_by``, ``expand`` and ``limit``."""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as exc:
        raise UsageError(f"cannot read report definition '{path}' ({exc})") from exc
    unknown = set(data) - {"type", "query", "title", "columns", "group_by", "expand", "limit", "description"}
    if unknown:
        raise UsageError(f"unknown key(s) in '{path}': {', '.join(sorted(unknown))}")
    return data


def report_type(name: str) -> ReportType:
    """A predefined type, or a bare index name (generic columns)."""
    if name in REPORT_TYPES:
        return REPORT_TYPES[name]
    for known in REPORT_TYPES.values():
        if known.index == name:
            return known
    if name == "*":
        return ReportType("all", "*", "Search results", sort_field=None)
    raise UsageError(f"unknown report type {name!r}; choose one of {', '.join(REPORT_TYPES)}")
