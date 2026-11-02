"""Tenant environments: one per tenant (dev, sandbox, prod...), to create once and switch between.

The base URL and client ID of each environment are kept in ``environments.json`` in the user's
profile (``%APPDATA%\\isc-reporter`` on Windows, ``~/Library/Application Support/isc-reporter`` on
macOS, ``~/.config/isc-reporter`` elsewhere; ``ISCR_HOME`` overrides it). The client secret is never
written to a file: it goes to the operating system's credential store (Windows Credential Manager,
macOS Keychain, Secret Service on Linux) through the ``keyring`` package.
"""

from __future__ import annotations

import json
import os
import re
import sys
from dataclasses import dataclass

from isc_reporter.errors import UsageError

SERVICE = "isc-reporter"           # the name the secrets are filed under in the credential store
FILE = "environments.json"
VALID_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


@dataclass
class Environment:
    name: str
    base_url: str
    client_id: str
    current: bool = False


def folder(environ=os.environ) -> str:
    if environ.get("ISCR_HOME"):
        return environ["ISCR_HOME"]
    if os.name == "nt":
        base = environ.get("APPDATA") or os.path.expanduser("~")
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "isc-reporter")


def _load(environ=os.environ) -> dict:
    path = os.path.join(folder(environ), FILE)
    if not os.path.isfile(path):
        return {"current": None, "environments": {}}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise UsageError(f"'{path}' is not valid JSON ({exc}): fix or delete it") from exc
    data.setdefault("environments", {})
    return data


def _save(data: dict, environ=os.environ) -> None:
    os.makedirs(folder(environ), exist_ok=True)
    path = os.path.join(folder(environ), FILE)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    if os.name != "nt":
        os.chmod(path, 0o600)


def _store():
    """The credential store; a clear error where the computer has none."""
    import keyring
    from keyring.backends import fail

    backend = keyring.get_keyring()
    if isinstance(backend, fail.Keyring):
        raise UsageError("No credential store is available on this computer, so secrets cannot be saved "
                         "safely. Set SAIL_BASE_URL, SAIL_CLIENT_ID and SAIL_CLIENT_SECRET instead.")
    return keyring


def store_name() -> str:
    """The credential store in use, for messages (e.g. Windows Credential Manager)."""
    import keyring

    backend = type(keyring.get_keyring()).__name__
    return {"WinVaultKeyring": "Windows Credential Manager", "Keyring": "the macOS Keychain"}.get(
        backend, f"the credential store ({backend})")


def check_name(name: str) -> str:
    if not VALID_NAME.match(name or ""):
        raise UsageError(f"environment name {name!r}: use letters, digits, '.', '-' or '_' (64 at most)")
    return name


def listing(environ=os.environ) -> list[Environment]:
    data = _load(environ)
    return [Environment(name, info.get("baseUrl", ""), info.get("clientId", ""), name == data.get("current"))
            for name, info in sorted(data["environments"].items(), key=lambda item: item[0].lower())]


def current(environ=os.environ) -> str | None:
    data = _load(environ)
    name = data.get("current")
    return name if name in data["environments"] else None


def add(name: str, base_url: str, client_id: str, secret: str, make_current: bool = False,
        environ=os.environ) -> bool:
    """Create or update an environment; returns whether it is now the current one."""
    check_name(name)
    base_url = base_url.strip().rstrip("/")
    if not base_url.startswith("https://"):
        raise UsageError(f"base URL {base_url!r} must start with https:// "
                         "(e.g. https://<tenant>.api.identitynow.com)")
    if not client_id.strip() or not secret.strip():
        raise UsageError("the client ID and the client secret cannot be empty")
    _store().set_password(SERVICE, name, secret.strip())
    data = _load(environ)
    data["environments"][name] = {"baseUrl": base_url, "clientId": client_id.strip()}
    if make_current or not current(environ):
        data["current"] = name
    _save(data, environ)
    return data["current"] == name


def use(name: str, environ=os.environ) -> Environment:
    data = _load(environ)
    if name not in data["environments"]:
        raise UsageError(_unknown(name, data))
    data["current"] = name
    _save(data, environ)
    info = data["environments"][name]
    return Environment(name, info.get("baseUrl", ""), info.get("clientId", ""), True)


def remove(name: str, environ=os.environ) -> None:
    import keyring.errors

    data = _load(environ)
    if name not in data["environments"]:
        raise UsageError(_unknown(name, data))
    del data["environments"][name]
    if data.get("current") == name:
        data["current"] = None
    _save(data, environ)
    try:
        _store().delete_password(SERVICE, name)
    except keyring.errors.PasswordDeleteError:
        pass                                   # already gone from the credential store


def credentials(name: str, environ=os.environ) -> tuple[str, str, str]:
    """``(base_url, client_id, client_secret)`` of an environment."""
    data = _load(environ)
    info = data["environments"].get(name)
    if info is None:
        raise UsageError(_unknown(name, data))
    secret = _store().get_password(SERVICE, name)
    if not secret:
        raise UsageError(f"the client secret of the environment '{name}' is not in the credential store of "
                         f"this computer: save it again with 'iscr env add {name} --url "
                         f"{info.get('baseUrl', '')} --client-id {info.get('clientId', '')}'")
    return info.get("baseUrl", ""), info.get("clientId", ""), secret


def has_secret(name: str) -> bool:
    try:
        return bool(_store().get_password(SERVICE, name))
    except UsageError:
        return False


def _unknown(name: str, data: dict) -> str:
    known = ", ".join(sorted(data["environments"])) or "none yet"
    return f"no environment named '{name}' (environments: {known}); create it with 'iscr env add {name}'"
