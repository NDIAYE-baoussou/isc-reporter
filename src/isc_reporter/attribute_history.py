"""Identity attribute change history: who changed department, manager, location... with the old
and new values, over a period.

ISC search does not expose old/new attribute values, so the report combines two APIs:

1. Search finds the identities modified during the period (a superset of those that changed);
2. the Identity History API lists each one's ``AttributesChanged`` events, which carry every
   changed attribute with its previous and new value.

:func:`fetch` reads the tenant; everything else works on plain dicts and can be tested offline.
"""

from __future__ import annotations

import logging
import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from isc_reporter import dashboard
from isc_reporter.dashboard import AMBER, BLUE, GREEN, GREY
from isc_reporter.errors import UsageError
from isc_reporter.identity_profiles import STANDARD_NAMES
from isc_reporter.reporting import get

log = logging.getLogger("isc_reporter")

EVENT_TYPE = "AttributesChanged"
PAGE = 250
_ID = re.compile(r"^[0-9a-fA-F]{32}$")
# Identity columns of the report, and where each comes from in the identity search document.
IDENTITY_COLUMNS = (("Employee number", ("employeeNumber", "attributes.employeeNumber")),
                    ("First name", ("firstName", "attributes.firstname")),
                    ("Last name", ("lastName", "attributes.lastname")),
                    ("Email", ("email", "attributes.email")),
                    ("Identity", ("displayName", "name")))


@dataclass
class HistoryData:
    start: datetime
    end: datetime
    attributes: list[str] | None          # None = every attribute
    identities: list[dict] = field(default_factory=list)   # search documents scanned
    changes: list[dict] = field(default_factory=list)      # one per changed attribute value
    origin: str = ""
    query: str = ""


def period(days: int | None = None, since: str | None = None,
           now: datetime | None = None) -> tuple[datetime, datetime]:
    now = now or datetime.now(timezone.utc)
    if since:
        try:
            start = datetime.combine(date.fromisoformat(since), datetime.min.time(), timezone.utc)
        except ValueError as exc:
            raise UsageError(f"--since {since!r} must look like YYYY-MM-DD") from exc
    else:
        start = now - timedelta(days=days or 30)
    if start >= now:
        raise UsageError("the period must start in the past")
    return start, now


def label(attribute: str) -> str:
    """``cloudLifecycleState`` -> ``Lifecycle State``, ``department`` -> ``Department``."""
    if attribute in STANDARD_NAMES:
        return STANDARD_NAMES[attribute]
    spaced = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", attribute)
    return spaced[:1].upper() + spaced[1:]


# -- reading the tenant -------------------------------------------------------------


def fetch(isc, start: datetime, end: datetime, attributes: list[str] | None = None,
          query: str = "", max_identities: int | None = None,
          exclude: list[str] | None = None) -> HistoryData:
    from isc_reporter.api import HttpError, call_json
    from isc_reporter.search import search_all

    window = f"modified:[{start:%Y-%m-%dT%H:%M:%S.000Z} TO now]"
    full_query = f"({query}) AND {window}" if query else window
    data = HistoryData(start, end, attributes, query=full_query)
    data.identities = search_all(isc, "identities", full_query, sort_field="modified",
                                 include_nested=False, max_results=max_identities)
    log.info("%d identit(ies) modified since %s: reading their history", len(data.identities),
             f"{start:%Y-%m-%d}")
    wanted = {a.lower() for a in attributes} if attributes else None
    excluded = {a.lower() for a in exclude or []}
    api = isc.identity_history.get_historical_identity_events_v1_without_preload_content
    for number, identity in enumerate(data.identities, start=1):
        events, offset = [], 0
        while True:
            try:
                page = call_json(api, id=identity["id"], event_types=[EVENT_TYPE], limit=PAGE,
                                 offset=offset, x_sail_point_experimental="true") or []
            except HttpError as exc:
                if exc.status in (401, 403) and number == 1:
                    raise UsageError("the Identity History API refused access (HTTP "
                                     f"{exc.status}); the API client needs the "
                                     "idn:identity-history:read scope") from exc
                raise
            events += page
            if len(page) < PAGE:
                break
            offset += PAGE
        data.changes += changes_from(identity, events, start, end, wanted, excluded)
        if number % 100 == 0:
            log.info("  %d / %d identities read, %d change(s) so far", number, len(data.identities),
                     len(data.changes))
    _resolve_identity_ids(isc, data.changes)
    log.info("%d change(s) found", len(data.changes))
    return data


def changes_from(identity: dict, events: list[dict], start: datetime, end: datetime,
                 wanted: set[str] | None, excluded: set[str] = frozenset()) -> list[dict]:
    """One change per (event, attribute) inside the period, oldest first."""
    found = []
    for event in events:
        if event.get("eventType", EVENT_TYPE) != EVENT_TYPE:
            continue
        moment = _time(event.get("dateTime"))
        if moment is None or not start <= moment <= end:
            continue
        for change in event.get("attributeChanges") or []:
            name = change.get("name") or ""
            if (wanted is None or name.lower() in wanted) and name.lower() not in excluded:
                found.append({"identity": identity, "time": moment, "attribute": name,
                              "old": _value(change.get("previousValue")),
                              "new": _value(change.get("newValue"))})
    return sorted(found, key=lambda c: c["time"])


def _resolve_identity_ids(isc, changes: list[dict]) -> None:
    """Manager-like values stored as identity ids are replaced by the identity's name."""
    from isc_reporter.api import call
    from isc_reporter.query import quote

    ids = {v for c in changes for v in (c["old"], c["new"]) if _ID.match(v or "")}
    if not ids:
        return
    from sailpoint.search.models import Search

    names = {}
    for identity_id in ids:
        hits = call(isc.search.search_post_v1, search=Search.from_dict(
            {"indices": ["identities"], "query": {"query": f"id:{quote(identity_id)}"},
             "includeNested": False}), limit=1) or []
        if hits:
            names[identity_id] = hits[0].get("displayName") or hits[0].get("name") or identity_id
    for change in changes:
        for key in ("old", "new"):
            change[key] = names.get(change[key], change[key])


def _time(value) -> datetime | None:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _value(value) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):  # reference objects: keep their readable part
        return str(value.get("displayName") or value.get("name") or value.get("id") or value)
    if isinstance(value, list):
        return " | ".join(_value(v) for v in value)
    return str(value)


# -- analysis -------------------------------------------------------------------------


def identity_fields(identity: dict) -> dict:
    out = {}
    for column, paths in IDENTITY_COLUMNS:
        out[column] = next((str(v) for v in (get(identity, p) for p in paths) if v), "")
    return out


def changed_attributes(data: HistoryData) -> list[str]:
    """The attributes to show: those requested, in order, else every changed one (most first)."""
    if data.attributes:
        return list(data.attributes)
    return [name for name, _ in Counter(c["attribute"] for c in data.changes).most_common()]


def change_rows(data: HistoryData) -> list[dict]:
    return [{"Date": f"{c['time']:%Y-%m-%d %H:%M}", **identity_fields(c["identity"]),
             "Attribute": label(c["attribute"]), "Old value": c["old"], "New value": c["new"]}
            for c in data.changes]


def identity_rows(data: HistoryData) -> list[dict]:
    """One line per identity: for each attribute, the value before its first change in the
    period and after its last one (blank when that attribute did not change)."""
    attributes = changed_attributes(data)
    by_identity: dict[str, list[dict]] = defaultdict(list)
    for change in data.changes:
        by_identity[change["identity"]["id"]].append(change)
    rows = []
    for changes in by_identity.values():
        row = identity_fields(changes[0]["identity"])
        for attribute in attributes:
            mine = [c for c in changes if c["attribute"].lower() == attribute.lower()]
            row[f"Old {label(attribute)}"] = mine[0]["old"] if mine else ""
            row[f"New {label(attribute)}"] = mine[-1]["new"] if mine else ""
        row["Changes"] = len(changes)
        row["Last change"] = f"{changes[-1]['time']:%Y-%m-%d}"
        rows.append(row)
    return sorted(rows, key=lambda r: (r["Last name"].lower(), r["First name"].lower(), r["Identity"]))


DAILY_NOTE = ("Changes on most days for most identities: probably computed from the current date "
              "(e.g. a transform that includes today's date). It triggers an attribute change event "
              "every day; exclude it with --exclude.")


def daily_attributes(data: HistoryData, share: float = 0.5, min_days: int = 5) -> set[str]:
    """Attributes that change on most days of the period for most of the identities having them."""
    days = max(1, (data.end - data.start).days)
    per_identity: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
    for change in data.changes:
        per_identity[change["attribute"]][change["identity"]["id"]].add(change["time"].date())
    daily = set()
    for attribute, identities in per_identity.items():
        busy = [d for d in identities.values() if len(d) >= min_days and len(d) >= share * days]
        if len(busy) >= share * len(identities):
            daily.add(attribute)
    return daily


def attribute_rows(data: HistoryData) -> list[dict]:
    counts = Counter(c["attribute"] for c in data.changes)
    people, days = defaultdict(set), defaultdict(set)
    for change in data.changes:
        people[change["attribute"]].add(change["identity"]["id"])
        days[change["attribute"]].add(change["time"].date())
    daily = daily_attributes(data)
    return [{"Attribute": label(name), "Technical name": name, "Changes": n, "Identities": len(people[name]),
             "Days with changes": len(days[name]), "Note": DAILY_NOTE if name in daily else ""}
            for name, n in counts.most_common()]


def transition_rows(data: HistoryData) -> list[dict]:
    counts = Counter((c["attribute"], c["old"] or "(empty)", c["new"] or "(empty)") for c in data.changes)
    return [{"Attribute": label(a), "From": old, "To": new, "Changes": n}
            for (a, old, new), n in counts.most_common()]


def kpis(data: HistoryData) -> list[tuple[str, str, str]]:
    changed = {c["identity"]["id"] for c in data.changes}
    attributes = attribute_rows(data)
    top = attributes[0] if attributes else None
    days = max(1, (data.end - data.start).days)
    tiles = [
        ("Period", f"{data.start:%Y-%m-%d} → {data.end:%Y-%m-%d}  ({days} days)", BLUE),
        ("Identities with changes", f"{len(changed)}  (of {len(data.identities)} modified)", BLUE),
        ("Attribute changes", str(len(data.changes)), GREEN if data.changes else GREY),
        ("Most changed attribute", f"{top['Attribute']} ({top['Changes']})" if top else "-", BLUE),
    ]
    daily = daily_attributes(data)
    if daily:
        tiles.append(("Recomputed every day (see Attributes)", ", ".join(sorted(label(a) for a in daily)),
                      AMBER))
    return tiles


def charts(data: HistoryData, top: int = 10) -> list[dashboard.Chart]:
    result = []
    attributes = attribute_rows(data)
    if attributes:
        shown = attributes[:15]
        result.append(dashboard.Chart("Changes per attribute", "bar", [a["Attribute"] for a in shown],
                                      [("Changes", [a["Changes"] for a in shown], BLUE)]))
        per_day = Counter(f"{c['time']:%Y-%m-%d}" for c in data.changes)
        days = sorted(per_day)
        result.append(dashboard.Chart("Changes per day", "bar", days,
                                      [("Changes", [per_day[d] for d in days], "8E24AA")]))
    for attribute in changed_attributes(data)[:4]:
        moves = Counter(f"{c['old'] or '(empty)'} → {c['new'] or '(empty)'}" for c in data.changes
                        if c["attribute"].lower() == attribute.lower()).most_common(top)
        if moves:
            result.append(dashboard.Chart(f"Top {label(attribute)} changes", "bar",
                                          [_short(m) for m, _ in moves],
                                          [("Changes", [n for _, n in moves], GREEN)]))
    return result


def _short(text: str, size: int = 45) -> str:
    """Cut long labels in the middle: values often differ only at the end (dates, numbers)."""
    if len(text) <= size:
        return text
    head = (size - 1) // 2
    return text[:head] + "…" + text[-(size - 1 - head):]


# -- rendering ------------------------------------------------------------------------

READ_ME = [
    ("Dashboard", "Key figures and charts for the period."),
    ("By identity", "One line per identity that changed: for each attribute, the value before its "
                    "first change in the period (Old) and after its last one (New)."),
    ("Changes", "Every change, oldest first: date, identity, attribute, old and new value."),
    ("Attributes", "How many changes and identities per attribute."),
    ("Transitions", "Each from → to movement and how often it happened (e.g. department moves)."),
    ("Chart data", "The numbers behind each chart."),
    ("Source", "Identities modified in the period (Search), then their AttributesChanged events "
               "(Identity History API). Values that are identity ids (e.g. manager) are shown as names."),
]


def build_dashboard(data: HistoryData, title: str) -> dashboard.Dashboard:
    attributes = changed_attributes(data)
    by_identity = identity_rows(data)
    pairs = [f"{prefix} {label(a)}" for a in attributes[:3] for prefix in ("Old", "New")]
    wide = {"Old value", "New value", "Identity"} | {k for r in by_identity[:1] for k in r
                                                      if k.startswith(("Old ", "New "))}
    tables = [dashboard.Table("By identity", by_identity, wide=wide),
              dashboard.Table("Changes", change_rows(data), wide=wide),
              dashboard.Table("Attributes", attribute_rows(data), wide={"Note"}),
              dashboard.Table("Transitions", transition_rows(data), wide={"From", "To"})]
    sections = [
        dashboard.Section("By identity", by_identity,
                          ["Employee number", "First name", "Last name", "Email", *pairs, "Changes"]),
        dashboard.Section("Changes", change_rows(data),
                          ["Date", "Identity", "Attribute", "Old value", "New value"]),
    ]
    scope = f"attributes: {', '.join(attributes) if data.attributes else 'all'}"
    return dashboard.Dashboard(title, dashboard.subtitle(data.origin, scope), kpis(data), charts(data),
                               tables, sections, READ_ME,
                               wide_pdf={"Email", "Identity", "Old value", "New value", *pairs})


def write_xlsx(data: HistoryData, path: str, title: str) -> str:
    return dashboard.write_xlsx(build_dashboard(data, title), path)


def write_pdf(data: HistoryData, path: str, title: str, max_rows: int = 500) -> str:
    return dashboard.write_pdf(build_dashboard(data, title), path, max_rows)


def write_csv(data: HistoryData, base: str) -> list[str]:
    from isc_reporter.csvio import write_table

    identity = [column for column, _ in IDENTITY_COLUMNS]
    pairs = [f"{prefix} {label(a)}" for a in changed_attributes(data) for prefix in ("Old", "New")]
    layouts = (("", identity_rows(data), identity + pairs + ["Changes", "Last change"]),
               ("-changes", change_rows(data), ["Date", *identity, "Attribute", "Old value", "New value"]))
    # The header is written even without changes, so the files keep the same columns.
    return [write_table(f"{base}{suffix}.csv", headers, [[r.get(h, "") for h in headers] for r in rows],
                        delimiter=";")
            for suffix, rows, headers in layouts]
