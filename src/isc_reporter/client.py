"""Authenticated access to the SailPoint APIs used by isc-reporter."""

from __future__ import annotations

import json
import logging
import os
from typing import NamedTuple

from isc_reporter import __version__, environments
from isc_reporter.errors import UsageError

log = logging.getLogger("isc_reporter")


class Isc:
    """SailPoint API objects sharing one authenticated ApiClient, created on first use."""

    def __init__(self, api_client, base_url: str = "", origin: str = ""):
        self.api_client = api_client
        self.base_url = base_url
        self.origin = origin                  # where the credentials came from, for the log
        self._apis: dict = {}

    def _api(self, name: str):
        if name not in self._apis:
            import sailpoint

            self._apis[name] = getattr(sailpoint, name)(self.api_client)
        return self._apis[name]

    @property
    def access_profiles(self):
        return self._api("AccessProfilesApi")

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
    def connector_rules(self):
        return self._api("ConnectorRuleManagementApi")

    @property
    def dimensions(self):
        return self._api("DimensionsApi")

    @property
    def governance_groups(self):
        return self._api("GovernanceGroupsApi")

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
    def lifecycle_states(self):
        return self._api("LifecycleStatesApi")

    @property
    def managed_clusters(self):
        return self._api("ManagedClustersApi")

    @property
    def password_policies(self):
        return self._api("PasswordPoliciesApi")

    @property
    def roles(self):
        return self._api("RolesApi")

    @property
    def service_desk_integrations(self):
        return self._api("ServiceDeskIntegrationApi")

    @property
    def sod_policies(self):
        return self._api("SODPoliciesApi")

    @property
    def task_management(self):
        return self._api("TaskManagementApi")

    @property
    def transforms(self):
        return self._api("TransformsApi")

    @property
    def workflows(self):
        return self._api("WorkflowsApi")

    @property
    def search(self):
        return self._api("SearchApi")

    @property
    def segments(self):
        return self._api("SegmentsApi")

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
FILE_WARNING = ("%s holds the client secret in clear text: move it to the credential store with "
                "'iscr env import %s --name <name> --delete'.")


class Credentials(NamedTuple):
    base_url: str
    client_id: str
    client_secret: str
    origin: str                               # e.g. "environment 'prod'"


def connect(config_path: str | None = None, environment: str | None = None) -> Isc:
    """Authenticate and return an :class:`Isc`, with the credentials :func:`resolve_credentials`
    finds."""
    credentials = resolve_credentials(config_path, environment=environment)
    return open_session(*credentials)


def open_session(base_url: str, client_id: str, client_secret: str, origin: str = "") -> Isc:
    from sailpoint import ApiClient, Configuration
    from sailpoint.configuration import ConfigurationParams

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
    return Isc(ApiClient(config), base_url=str(config.base_url), origin=origin)


def resolve_credentials(config_path: str | None = None, environ=os.environ,
                        environment: str | None = None) -> Credentials:
    """The credentials to use, from the first of: *config_path* (a JSON file, not recommended), the
    *environment* named (or ``ISCR_ENV``), the ``SAIL_*`` / ``SAILPOINT_*`` environment variables,
    the current environment (see :mod:`isc_reporter.environments`), then ``./config.json`` (not
    recommended)."""
    if config_path:
        log.warning(FILE_WARNING, config_path, config_path)
        return Credentials(*_from_file(config_path), f"the file {config_path}")
    name = environment or environ.get("ISCR_ENV", "").strip()
    if name:
        return Credentials(*environments.credentials(name), f"environment '{name}'")
    for prefix in _ENV_PREFIXES:
        values = tuple(environ.get(prefix + key, "").strip()
                       for key in ("BASE_URL", "CLIENT_ID", "CLIENT_SECRET"))
        if all(values):
            return Credentials(*values, f"the {prefix}* environment variables")
    name = environments.current()
    if name:
        return Credentials(*environments.credentials(name), f"environment '{name}'")
    if os.path.isfile(DEFAULT_CONFIG):
        log.warning(FILE_WARNING, DEFAULT_CONFIG, DEFAULT_CONFIG)
        return Credentials(*_from_file(DEFAULT_CONFIG), DEFAULT_CONFIG)
    raise UsageError(
        "No SailPoint credentials found. Add an environment (the client secret is asked for and kept in the "
        "credential store of this computer): iscr env add <name> --url https://<tenant>.api.identitynow.com "
        "--client-id <client id>. Or set SAIL_BASE_URL, SAIL_CLIENT_ID and SAIL_CLIENT_SECRET."
    )


def read_config_file(path: str) -> tuple[str, str, str]:
    """``(base_url, client_id, client_secret)`` from a JSON file (``iscr env import``)."""
    return _from_file(path)


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
