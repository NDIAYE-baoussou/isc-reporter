"""Test doubles: SDK API objects that record their calls, and a credential store in memory."""

from __future__ import annotations

import json

import keyring.backend
import keyring.errors
import pytest


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A credential store in memory: the tests never touch the computer's own."""

    priority = 1

    def __init__(self):
        super().__init__()
        self.secrets: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.secrets.get((service, username))

    def set_password(self, service, username, password):
        self.secrets[(service, username)] = password

    def delete_password(self, service, username):
        if self.secrets.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError(username)


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path_factory, monkeypatch):
    """The environments of each test live in a temporary folder, their secrets in memory."""
    monkeypatch.setenv("ISCR_HOME", str(tmp_path_factory.mktemp("iscr-home")))
    for name in ("ISCR_ENV", "SAIL_BASE_URL", "SAIL_CLIENT_ID", "SAIL_CLIENT_SECRET", "SAILPOINT_BASE_URL",
                 "SAILPOINT_CLIENT_ID", "SAILPOINT_CLIENT_SECRET"):
        monkeypatch.delenv(name, raising=False)
    store = MemoryKeyring()
    monkeypatch.setattr("keyring.core._keyring_backend", store)
    return store


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


class Raw:
    """What a ``..._without_preload_content`` call returns."""

    def __init__(self, payload, status=200, headers=None):
        self.status, self.headers = status, dict(headers or {})
        self.data = json.dumps(payload).encode()


class FakeIsc:
    """Stands in for :class:`isc_reporter.client.Isc`."""

    APIS = ("access_profiles", "accounts", "campaigns", "campaign_filters", "certifications",
            "certification_summaries", "connector_rules", "dimensions", "governance_groups",
            "identity_attributes", "identity_history", "identity_profiles", "lifecycle_states",
            "managed_clusters", "password_policies", "roles", "search", "segments",
            "service_desk_integrations", "sod_policies", "sources", "task_management", "transforms",
            "workflows")

    def __init__(self, **apis):
        for name in self.APIS:
            setattr(self, name, apis.get(name, FakeApi()))
        self.base_url = "https://tenant.api.identitynow.com"
