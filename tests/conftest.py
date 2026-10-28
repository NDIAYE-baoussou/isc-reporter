"""Test doubles: SDK API objects that record their calls."""

from __future__ import annotations


class FakeApi:
    """Any method call is recorded; its return value comes from *responses*
    (a value, or a callable receiving the call's keyword arguments)."""

    def __init__(self, **responses):
        self.calls: list[tuple[str, dict]] = []
        self.responses = responses

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)

        def method(**kwargs):
            self.calls.append((name, kwargs))
            response = self.responses.get(name)
            return response(**kwargs) if callable(response) else response

        method.__name__ = name
        return method

    def called(self, name: str) -> list[dict]:
        return [kwargs for called, kwargs in self.calls if called == name]


class FakeIsc:
    """Stands in for :class:`isc_reporter.client.Isc`."""

    APIS = ("accounts", "campaigns", "campaign_filters", "certifications", "certification_summaries",
            "identity_attributes", "identity_history", "identity_profiles", "search", "sources",
            "transforms")

    def __init__(self, **apis):
        for name in self.APIS:
            setattr(self, name, apis.get(name, FakeApi()))
        self.base_url = "https://tenant.api.identitynow.com"
