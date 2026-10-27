"""Credentials, retries and search paging."""

import json

import pytest

from conftest import FakeApi, FakeIsc
from isc_reporter import api
from isc_reporter.client import resolve_credentials
from isc_reporter.errors import UsageError
from isc_reporter.search import date_range, search_all

# -- credentials --------------------------------------------------------------


def test_credentials_from_file_accept_both_key_styles(tmp_path):
    sdk = tmp_path / "a.json"
    sdk.write_text(json.dumps({"BaseURL": "https://t", "ClientId": "id", "ClientSecret": "s"}))
    legacy = tmp_path / "b.json"
    legacy.write_text(json.dumps({"baseUrl": "https://t", "clientId": "id", "clientSecret": "s"}))
    assert resolve_credentials(str(sdk)) == ("https://t", "id", "s")
    assert resolve_credentials(str(legacy)) == ("https://t", "id", "s")


def test_credentials_from_env_sdk_names_first(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    env = {"SAILPOINT_BASE_URL": "https://old", "SAILPOINT_CLIENT_ID": "o",
           "SAILPOINT_CLIENT_SECRET": "o", "SAIL_BASE_URL": "https://new",
           "SAIL_CLIENT_ID": "n", "SAIL_CLIENT_SECRET": "n"}
    assert resolve_credentials(environ=env) == ("https://new", "n", "n")
    del env["SAIL_CLIENT_SECRET"]
    assert resolve_credentials(environ=env) == ("https://old", "o", "o")


def test_credentials_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(UsageError, match="No SailPoint credentials"):
        resolve_credentials(environ={})
    bad = tmp_path / "c.json"
    bad.write_text("{}")
    with pytest.raises(UsageError, match="BaseURL"):
        resolve_credentials(str(bad))


# -- api ----------------------------------------------------------------------


class HttpError(Exception):
    def __init__(self, status, headers=None, body=None):
        super().__init__(f"HTTP {status}")
        self.status, self.headers, self.body = status, headers or {}, body


def test_call_retries_throttling_with_retry_after():
    attempts, sleeps = [], []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise HttpError(429, {"Retry-After": "7"})
        return "ok"

    assert api.call(flaky, sleep=sleeps.append) == "ok"
    assert sleeps == [7.0, 7.0]


def test_call_does_not_retry_client_errors():
    def bad():
        raise HttpError(400, body=b'{"messages":[{"text":"name is required"}]}')

    with pytest.raises(HttpError) as info:
        api.call(bad, sleep=lambda _: None)
    assert api.error_message(info.value) == "HTTP 400: name is required"


# -- search -------------------------------------------------------------------


def test_search_all_pages_with_search_after(monkeypatch):
    monkeypatch.setattr("isc_reporter.search.PAGE_SIZE", 2)
    docs = [{"id": str(i), "created": f"2026-01-0{i}"} for i in range(1, 6)]
    bodies = []

    def search_post_v1(search, limit):
        bodies.append(search.to_dict())
        after = search.search_after
        start = 0 if not after else next(i for i, d in enumerate(docs) if d["id"] == after[1]) + 1
        return docs[start:start + limit]

    found = search_all(FakeIsc(search=FakeApi(search_post_v1=search_post_v1)), "events", "*")
    assert found == docs
    assert bodies[0]["sort"] == ["-created", "id"]
    assert bodies[1]["searchAfter"] == ["2026-01-02", "2"]


def test_date_range():
    assert date_range("2026-05-01", "2026-05-31") == \
        "created:[2026-05-01T00:00:00.000Z TO 2026-05-31T23:59:59.999Z]"
    with pytest.raises(UsageError):
        date_range("2026-05-31", "2026-05-01")
    with pytest.raises(UsageError):
        date_range("31/05/2026", "2026-06-01")
