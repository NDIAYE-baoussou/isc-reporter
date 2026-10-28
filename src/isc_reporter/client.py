"""Authenticated access to the SailPoint APIs used by isc-reporter."""

from __future__ import annotations

import json
import os

from isc_reporter import __version__
from isc_reporter.errors import UsageError


class Isc:
    """SailPoint API objects sharing one authenticated ApiClient, created on first use."""

    def __init__(self, api_client, base_url: str = ""):
        self.api_client = api_client
        self.base_url = base_url
        self._apis: dict = {}

    def _api(self, name: str):
        if name not in self._apis:
            import sailpoint

            self._apis[name] = getattr(sailpoint, name)(self.api_client)
        return self._apis[name]

    @property
    def accounts(self):
        return self._api("AccountsApi")

    @property
    def campaigns(self):
        return self._api("CertificationCampaignsApi")

    @property
    def campaign_filters(self):
        return self._api("CertificationCampaignFiltersApi")

    @property
    def certifications(self):
        return self._api("CertificationsApi")

    @property
    def certification_summaries(self):
        return self._api("CertificationSummariesApi")

    @property
    def identity_attributes(self):
        return self._api("IdentityAttributesApi")

    @property
    def identity_history(self):
        return self._api("IdentityHistoryApi")

    @property
    def identity_profiles(self):
        return self._api("IdentityProfilesApi")

    @property
    def transforms(self):
        return self._api("TransformsApi")

    @property
    def search(self):
        return self._api("SearchApi")

    @property
    def sources(self):
        return self._api("SourcesApi")


#: (base URL, client id, client secret) key names accepted in config.json, in order.
_FILE_KEYS = (("BaseURL", "ClientId", "ClientSecret"),       # SailPoint SDK / CLI format
              ("baseUrl", "clientId", "clientSecret"),       # camelCase
              ("base_url", "client_id", "client_secret"))
#: Environment variable prefixes: the SDK's own, then the common SAILPOINT_ alternative.
_ENV_PREFIXES = ("SAIL_", "SAILPOINT_")
DEFAULT_CONFIG = "config.json"


def connect(config_path: str | None = None) -> Isc:
    """Authenticate and return an :class:`Isc`.

    Credentials come from *config_path* when given, otherwise from the
    ``SAIL_BASE_URL`` / ``SAIL_CLIENT_ID`` / ``SAIL_CLIENT_SECRET`` environment
    variables (``SAILPOINT_*`` also accepted), then ``./config.json``.
    """
    from sailpoint import ApiClient, Configuration
    from sailpoint.configuration import ConfigurationParams

    base_url, client_id, client_secret = resolve_credentials(config_path)
    params = ConfigurationParams()
    params.base_url = base_url.rstrip("/")
    params.client_id = client_id
    params.client_secret = client_secret
    params.token_url = params.base_url + "/oauth/token"
    config = Configuration(params)
    if not config.access_token:
        raise UsageError(
            f"Could not get an access token from {config.token_url}. "
            "Check the client ID, client secret and base URL."
        )
    config.experimental = True
    config.suppress_experimental_warnings = True
    config.consumer_identifier = "isc-reporter"
    config.consumer_version = __version__
    return Isc(ApiClient(config), base_url=str(config.base_url))


def resolve_credentials(config_path: str | None = None, environ=os.environ) -> tuple[str, str, str]:
    """Return ``(base_url, client_id, client_secret)`` from the first complete source."""
    if config_path:
        return _from_file(config_path)
    for prefix in _ENV_PREFIXES:
        values = tuple(environ.get(prefix + name, "").strip()
                       for name in ("BASE_URL", "CLIENT_ID", "CLIENT_SECRET"))
        if all(values):
            return values
    if os.path.isfile(DEFAULT_CONFIG):
        return _from_file(DEFAULT_CONFIG)
    raise UsageError(
        "No SailPoint credentials found. Set SAIL_BASE_URL, SAIL_CLIENT_ID and "
        "SAIL_CLIENT_SECRET, or create a config.json (see README)."
    )


def _from_file(path: str) -> tuple[str, str, str]:
    if not os.path.isfile(path):
        raise UsageError(f"Config file '{path}' does not exist")
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except ValueError as exc:
        raise UsageError(f"Config file '{path}' is not valid JSON ({exc})") from exc
    for keys in _FILE_KEYS:
        values = tuple(str(data.get(key) or "").strip() for key in keys)
        if all(values):
            return values
    raise UsageError(f"Config file '{path}' must contain BaseURL, ClientId and ClientSecret")
