"""Search API helpers: complete result sets and per-identity access, cached for the run."""

from __future__ import annotations

from datetime import date

from isc_reporter.api import call
from isc_reporter.errors import UsageError
from isc_reporter.query import quote

PAGE_SIZE = 250


def search_all(isc, index: str, query: str, *, sort_field: str | None = "created",
               descending: bool = True, include_nested: bool = True,
               max_results: int | None = None, progress=None) -> list[dict]:
    """Every document matching *query* (at most *max_results*).

    Pages with ``searchAfter`` (sorted on *sort_field* then ``id``, or on ``id`` alone when
    *sort_field* is None) rather than ``offset``, so result sets beyond the API's
    10,000-offset limit are complete. *progress* is called with the count after each page.
    """
    from sailpoint.search.models import Search

    fields = [sort_field, "id"] if sort_field else ["id"]
    sort = [("-" if descending and sort_field else "") + fields[0]] + fields[1:]
    body = {"indices": [index], "query": {"query": query}, "sort": sort,
            "includeNested": include_nested}
    results: list[dict] = []
    while True:
        size = PAGE_SIZE if max_results is None else min(PAGE_SIZE, max_results - len(results))
        page = call(isc.search.search_post_v1, search=Search.from_dict(body), limit=size) or []
        results.extend(page)
        if progress:
            progress(len(results))
        if len(page) < size or (max_results is not None and len(results) >= max_results):
            return results
        last = page[-1]
        body["searchAfter"] = [str(last.get(field, "")) for field in fields]


def date_range(start: str, end: str, field: str = "created") -> str:
    """Query clause covering whole days from *start* to *end* (``YYYY-MM-DD``, inclusive)."""
    for value in (start, end):
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise UsageError(f"date {value!r} must look like YYYY-MM-DD") from exc
    if end < start:
        raise UsageError(f"end date {end} is before start date {start}")
    return f"{field}:[{start}T00:00:00.000Z TO {end}T23:59:59.999Z]"


class IdentityAccess:
    """What identities currently hold, from the identities search index, one call each."""

    def __init__(self, isc):
        self.isc = isc
        self._access: dict[str, list[dict] | None] = {}
        self._profiles_with: dict[str, list[dict]] = {}

    def items(self, identity_id: str) -> list[dict] | None:
        """Access items of the identity, or None if the identity does not exist."""
        if identity_id not in self._access:
            hits = call(self.isc.search.search_post_v1, search=_search(
                "identities", f"id:{quote(identity_id)}"), limit=1) or []
            self._access[identity_id] = (hits[0].get("access") or []) if hits else None
        return self._access[identity_id]

    def entitlement(self, identity_id: str, value: str, source: str = "") -> dict | None:
        """The held entitlement whose value (or name) matches, optionally on a given source."""
        for item in self.items(identity_id) or []:
            if item.get("type") != "ENTITLEMENT":
                continue
            if source and (item.get("source") or {}).get("name", "").lower() != source.lower():
                continue
            if value.lower() in (str(item.get("value", "")).lower(), str(item.get("name", "")).lower()):
                return item
        return None

    def holds(self, identity_id: str, item_id: str) -> bool:
        return any(item.get("id") == item_id for item in self.items(identity_id) or [])

    def profiles_with(self, entitlement_id: str) -> list[dict]:
        """Access profiles that contain the entitlement (search documents)."""
        if entitlement_id not in self._profiles_with:
            self._profiles_with[entitlement_id] = call(
                self.isc.search.search_post_v1,
                search=_search("accessprofiles", f"entitlements.id:{quote(entitlement_id)}"),
                limit=50) or []
        return self._profiles_with[entitlement_id]

    def held_profiles_with(self, identity_id: str, entitlement_id: str) -> list[dict]:
        """Access profiles containing the entitlement that the identity holds."""
        return [profile for profile in self.profiles_with(entitlement_id)
                if self.holds(identity_id, profile["id"])]


def _search(index: str, query: str):
    from sailpoint.search.models import Search

    return Search.from_dict({"indices": [index], "query": {"query": query}, "includeNested": True})
