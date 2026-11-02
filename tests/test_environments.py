"""Tenant environments: secrets in the credential store, switching between tenants."""

import io
import json
import os

import pytest

from isc_reporter import cli, client, environments
from isc_reporter.errors import UsageError

DEV, PROD = "https://dev.api.identitynow.com", "https://prod.api.identitynow.com"


def test_secrets_go_to_the_credential_store_never_to_the_file(isolated_credentials):
    assert environments.add("dev", DEV + "/", "dev-id", "dev-secret") is True          # the first is current
    assert environments.add("prod", PROD, "prod-id", "prod-secret") is False
    assert [(e.name, e.base_url, e.current) for e in environments.listing()] == [
        ("dev", DEV, True), ("prod", PROD, False)]
    saved = open(os.path.join(environments.folder(), "environments.json"), encoding="utf-8").read()
    assert "secret" not in saved and "prod-id" in saved
    assert isolated_credentials.secrets == {("isc-reporter", "dev"): "dev-secret",
                                            ("isc-reporter", "prod"): "prod-secret"}
    assert environments.credentials("prod") == (PROD, "prod-id", "prod-secret")
    assert environments.use("prod").base_url == PROD and environments.current() == "prod"
    environments.remove("prod")
    assert environments.current() is None and ("isc-reporter", "prod") not in isolated_credentials.secrets
    with pytest.raises(UsageError, match="no environment named 'prod'"):
        environments.use("prod")
    for name, url, secret in (("my env", DEV, "s"), ("qa", "http://qa", "s"), ("qa", DEV, " ")):
        with pytest.raises(UsageError):                     # a name with a space, not https, no secret
            environments.add(name, url, "id", secret)
    del isolated_credentials.secrets[("isc-reporter", "dev")]
    with pytest.raises(UsageError, match="not in the credential store"):
        environments.credentials("dev")


def test_which_credentials_are_used(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    environments.add("dev", DEV, "dev-id", "dev-secret")
    environments.add("prod", PROD, "prod-id", "prod-secret")
    sail = {"SAIL_BASE_URL": "https://ci", "SAIL_CLIENT_ID": "ci", "SAIL_CLIENT_SECRET": "ci"}
    assert client.resolve_credentials(environ={}).origin == "environment 'dev'"            # the current one
    assert client.resolve_credentials(environ=sail).origin == "the SAIL_* environment variables"
    chosen = client.resolve_credentials(environ=sail, environment="prod")
    assert chosen[:3] == (PROD, "prod-id", "prod-secret")
    assert client.resolve_credentials(environ={**sail, "ISCR_ENV": "prod"}).origin == "environment 'prod'"
    environments.remove("dev")
    environments.remove("prod")
    (tmp_path / "config.json").write_text(json.dumps({"BaseURL": DEV, "ClientId": "f", "ClientSecret": "f"}))
    # config.json is still read, with a warning
    assert client.resolve_credentials(environ={}).origin == "config.json"


def test_env_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    checked = []
    def session(base_url, client_id, secret, origin=""):
        checked.append((base_url, client_id, secret))
        return type("Isc", (), {"base_url": base_url})()

    monkeypatch.setattr(client, "open_session", session)
    base = ["--log-dir", ""]
    monkeypatch.setattr("sys.stdin", io.StringIO("dev-secret\n"))
    assert cli.main(["env", "add", "dev", "--url", DEV, "--client-id", "dev-id", "--secret-stdin",
                     *base]) == 0
    assert checked == [(DEV, "dev-id", "dev-secret")]                                       # checked first
    monkeypatch.setattr("getpass.getpass", lambda prompt: "prod-secret")
    assert cli.main(["env", "add", "prod", "--url", PROD, "--client-id", "prod-id", "--no-check", *base]) == 0
    assert len(checked) == 1
    capsys.readouterr()
    assert cli.main(["env", "list", *base]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("* dev ") and lines[1].startswith("  prod ") and "prod-id" in lines[1]
    assert cli.main(["env", "use", "prod", *base]) == 0 and environments.current() == "prod"
    assert cli.main(["env", "test", *base]) == 0 and checked[-1] == (PROD, "prod-id", "prod-secret")
    assert cli.main(["env", "use", "qa", *base]) == 2                                       # unknown

    def refused(*args, **kwargs):
        raise UsageError("Could not get an access token")

    monkeypatch.setattr(client, "open_session", refused)
    monkeypatch.setattr("getpass.getpass", lambda prompt: "wrong")
    assert cli.main(["env", "add", "qa", "--url", DEV, "--client-id", "x", *base]) == 2   # not saved
    assert [e.name for e in environments.listing()] == ["dev", "prod"]

    config = tmp_path / "config.json"
    config.write_text(json.dumps({"BaseURL": DEV, "ClientId": "old-id", "ClientSecret": "old-secret"}))
    assert cli.main(["env", "import", str(config), "--name", "legacy", "--no-check", "--delete", *base]) == 0
    assert not config.exists() and environments.credentials("legacy") == (DEV, "old-id", "old-secret")
    assert cli.main(["env", "remove", "legacy", *base]) == 0
    assert [e.name for e in environments.listing()] == ["dev", "prod"]


def test_commands_connect_with_the_environment_given(monkeypatch):
    environments.add("dev", DEV, "dev-id", "dev-secret")
    environments.add("prod", PROD, "prod-id", "prod-secret")
    used = []

    def session(*args):
        used.append(args)
        raise UsageError("stop here")

    monkeypatch.setattr(client, "open_session", session)
    assert cli.main(["search", "identities", "--env", "prod", "--log-dir", ""]) == 2
    assert used == [(PROD, "prod-id", "prod-secret", "environment 'prod'")]
