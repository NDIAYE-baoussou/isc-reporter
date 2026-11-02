"""Status and capabilities of every source: its health and virtual appliance cluster, how ISC provisions
it (through its connector, a service desk integration or manual work items), its features, provisioning
policies, account correlation, attribute synchronization, aggregation schedule and last aggregation, and
its accounts, with checks.

What cannot be read for a source (a scope the token lacks, an endpoint the source does not support) is
noted, and the rest of the report is still produced. The connection settings of the sources
(``connectorAttributes``, where their credentials are kept) are left out of the report.
"""

from __future__ import annotations

import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from fnmatch import fnmatchcase

from isc_reporter import dashboard
from isc_reporter.api import call_json, count_json, error_message, list_json
from isc_reporter.csvio import write_table
from isc_reporter.query import quote

log = logging.getLogger("isc_reporter")

WARNING, INFO = "Warning", "Info"
SEVERITY_FILLS = {WARNING: "F8CBAD", INFO: "DDEBF7"}
HEALTHY, ERROR, UNCHECKED, UNKNOWN = "Healthy", "Error", "Not checked", "Unknown"
HEALTH = (HEALTHY, ERROR, UNCHECKED, UNKNOWN)
STATUSES = {
    "SOURCE_STATE_HEALTHY": (HEALTHY, "Healthy"),
    "SOURCE_STATE_ERROR_SOURCE": (ERROR, "Error on the source"),
    "SOURCE_STATE_ERROR_CLUSTER": (ERROR, "Error on the cluster"),
    "SOURCE_STATE_ERROR_VA": (ERROR, "Error on the virtual appliance"),
    "SOURCE_STATE_FAILURE_SOURCE": (ERROR, "Health check failed on the source"),
    "SOURCE_STATE_FAILURE_CLUSTER": (ERROR, "Health check failed on the cluster"),
    "SOURCE_STATE_ERROR_ACCOUNT_FILE_IMPORT": (ERROR, "Error importing the account file"),
    "SOURCE_STATE_UNCHECKED_SOURCE": (UNCHECKED, "Not checked"),
    "SOURCE_STATE_UNCHECKED_SOURCE_NO_ACCOUNTS": (UNCHECKED, "Not checked: no accounts"),
    "SOURCE_STATE_UNCHECKED_CLUSTER": (UNCHECKED, "Cluster not checked"),
    "SOURCE_STATE_UNCHECKED_CLUSTER_NO_SOURCES": (UNCHECKED, "Cluster not checked: no sources"),
}
CONNECTOR, SERVICE_DESK, MANUAL, READ_ONLY = "Connector", "Service desk", "Manual work items", "Read only"
PROVISIONING = (CONNECTOR, SERVICE_DESK, MANUAL, READ_ONLY)
RESULTS = {"SUCCESS": "Success", "WARNING": "Warning", "ERROR": "Error", "TERMINATED": "Terminated",
           "TEMPERROR": "Temporary error"}
FAILED = {"Error", "Terminated", "Temporary error"}
RUNNING = "Running"
FEATURES = ("PROVISIONING", "SYNC_PROVISIONING", "ENABLE", "UNLOCK", "PASSWORD", "CURRENT_PASSWORD",
            "GROUP_PROVISIONING", "DIRECT_PERMISSIONS", "NO_PERMISSIONS_PROVISIONING",
            "NO_GROUP_PERMISSIONS_PROVISIONING", "ACCOUNT_ONLY_REQUEST", "ADDITIONAL_ACCOUNT_REQUEST",
            "AUTHENTICATE", "SEARCH", "DISCOVER_SCHEMA", "MANAGER_LOOKUP", "NO_RANDOM_ACCESS",
            "NO_AGGREGATION", "GROUPS_HAVE_MEMBERS", "COMPOSITE", "PROXY", "USES_UUID", "PREFER_UUID")
USAGES = ("CREATE", "UPDATE", "ENABLE", "DISABLE", "DELETE", "ASSIGN", "UNASSIGN", "CHANGE_PASSWORD",
          "UNLOCK")
DELETE_LIMIT = 50          # delete thresholds from this percentage are flagged
UNCORRELATED_LIMIT = 10    # from this share of uncorrelated accounts, a warning instead of an info

# What is read source by source, in SourceData.details.
POLICIES, SCHEDULES, CORRELATION, SYNC = "policies", "schedules", "correlation", "sync"
ACCOUNTS, UNCORRELATED, AGGREGATION = "accounts", "uncorrelated", "aggregation"
LABELS = {POLICIES: "Provisioning policies", SCHEDULES: "Aggregation schedules",
          CORRELATION: "Account correlation", SYNC: "Attribute synchronization", ACCOUNTS: "Account counts",
          UNCORRELATED: "Uncorrelated account counts", AGGREGATION: "Last aggregations",
          "clusters": "Virtual appliance clusters", "service desks": "Service desk integrations"}

SOURCE_COLUMNS = ["Source", "Type", "Connector", "Connection", "Authoritative", "Health", "Status",
                  "Checked on", "Cluster", "Cluster status", "Provisioning", "Service desk",
                  "Enable / disable", "Passwords", "Unlock", "Features", "Provisioning policies",
                  "Before provisioning rule", "Account correlation", "Manager correlation", "Attribute sync",
                  "Aggregation schedule", "Last aggregation", "Last aggregation result",
                  "Days since aggregation", "Accounts", "Uncorrelated", "Delete threshold", "Owner",
                  "Management group", "Created", "Modified", "Description", "Id"]


@dataclass
class SourceData:
    sources: list[dict]                                          # as GET /sources/v1 returns them
    details: dict[str, dict] = field(default_factory=dict)       # source id -> what was read for it
    clusters: dict[str, dict] = field(default_factory=dict)      # cluster id -> managed cluster
    service_desks: dict[str, str] = field(default_factory=dict)  # source id -> service desk integration
    quick: bool = False                                          # only the source list was read
    stale_days: int = 7
    patterns: list[str] = field(default_factory=list)            # --source
    unmatched: list[str] = field(default_factory=list)           # --source matching no source
    now: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    notes: list[str] = field(default_factory=list)
    origin: str = ""


# -- reading ------------------------------------------------------------------------------


def select(sources: list[dict], patterns: list[str] | None) -> tuple[list[dict], list[str]]:
    """The sources whose name matches one of *patterns* (any case, ``*`` and ``?`` as wildcards), and
    the patterns that match no source."""
    if not patterns:
        return sources, []

    def matches(source, pattern):
        return fnmatchcase(str(source.get("name", "")).lower(), pattern.lower())

    chosen = [s for s in sources if any(matches(s, p) for p in patterns)]
    return chosen, [p for p in patterns if not any(matches(s, p) for s in sources)]


class _Reader:
    """Calls that may fail one by one: each failure is noted, and a kind of call the token is not
    allowed to make (HTTP 401, 403) is not tried again for the next sources."""

    def __init__(self):
        self.failures: dict[str, list[tuple[str, str]]] = {}
        self.denied: set[str] = set()

    def read(self, kind: str, name: str, func):
        if kind in self.denied:
            return None
        try:
            return func()
        except Exception as exc:
            if getattr(exc, "status", None) in (401, 403):
                self.denied.add(kind)
            self.failures.setdefault(kind, []).append((name, error_message(exc)))
            return None

    def notes(self) -> list[str]:
        notes = []
        for kind in sorted(self.failures, key=list(LABELS).index):
            failed = self.failures[kind]
            label, message = LABELS[kind], failed[0][1]
            names = [name for name, _ in failed if name]
            if kind in self.denied or not names:
                notes.append(f"{label} not read ({message}).")
            else:
                more = f" and {len(names) - 10} more" if len(names) > 10 else ""
                notes.append(f"{label} not read for {len(names)} source(s) ({message}): "
                             f"{', '.join(names[:10])}{more}.")
        return notes


def fetch(isc, patterns: list[str] | None = None, quick: bool = False, stale_days: int = 7) -> SourceData:
    """The sources (those matching *patterns*, by default all), the clusters and the service desk
    integrations, then, unless *quick*, what is read source by source."""
    found = list_json(isc.sources.list_sources_v1_without_preload_content)
    sources, unmatched = select(found, patterns)
    data = SourceData(sources, quick=quick, stale_days=stale_days, patterns=list(patterns or []),
                      unmatched=unmatched, origin=isc.base_url)
    log.info("%d source(s) read", len(sources))
    if not sources:
        return data
    reader = _Reader()
    clusters = reader.read("clusters", "", lambda: list_json(
        isc.managed_clusters.get_managed_clusters_v1_without_preload_content))
    data.clusters = {c.get("id", ""): c for c in clusters or []}
    desks = reader.read("service desks", "", lambda: list_json(
        isc.service_desk_integrations.get_service_desk_integrations_v1_without_preload_content))
    for desk in desks or []:
        refs = (desk.get("provisioningConfig") or {}).get("managedResourceRefs") or []
        for source_id in [*(desk.get("managedSources") or []), *(r.get("id", "") for r in refs)]:
            if source_id:
                data.service_desks.setdefault(source_id, desk.get("name", ""))
    if not quick:
        log.info("Reading the policies, schedules, correlation, attribute sync, last aggregation and "
                 "accounts of each source")
        for index, source in enumerate(sources, 1):
            data.details[source.get("id", "")] = _details(isc, source, reader)
            if index % 25 == 0 and index < len(sources):
                log.info("%d of %d source(s) read", index, len(sources))
    data.notes = reader.notes()
    return data


def _details(isc, source: dict, reader: _Reader) -> dict:
    api, source_id, name = isc.sources, source.get("id", ""), source.get("name", "")
    of_source = f"sourceId eq {quote(source_id)}"

    def accounts(filters):
        return count_json(isc.accounts.list_accounts_v1_without_preload_content, filters=filters,
                          detail_level="SLIM")

    detail = {
        POLICIES: reader.read(POLICIES, name, lambda: list_json(
            api.list_provisioning_policies_v1_without_preload_content, source_id=source_id)),
        SCHEDULES: reader.read(SCHEDULES, name, lambda: call_json(
            api.get_source_schedules_v1_without_preload_content, source_id=source_id) or []),
        CORRELATION: reader.read(CORRELATION, name, lambda: call_json(
            api.get_correlation_config_v1_without_preload_content, id=source_id) or {})
        if source.get("accountCorrelationConfig") else {},
        ACCOUNTS: reader.read(ACCOUNTS, name, lambda: accounts(of_source)),
    }
    if detail[ACCOUNTS] == 0:
        detail[UNCORRELATED] = 0
    elif detail[ACCOUNTS]:
        detail[UNCORRELATED] = reader.read(UNCORRELATED, name,
                                           lambda: accounts(f"{of_source} and uncorrelated eq true"))
    if "PROVISIONING" in (source.get("features") or []):
        found = reader.read(SYNC, name, lambda: call_json(
            api.get_source_attr_sync_config_v1_without_preload_content, id=source_id,
            x_sail_point_experimental="true"))
        detail[SYNC] = None if found is None else list(found.get("attributes") or [])
    tasks = reader.read(AGGREGATION, name, lambda: call_json(
        isc.task_management.get_task_status_list_v1_without_preload_content, limit=1, sorters="-created",
        filters=f'{of_source} and type eq "CLOUD_ACCOUNT_AGGREGATION"'))
    detail[AGGREGATION] = None if tasks is None else (tasks[0] if tasks else {})
    return detail


# -- analysis -----------------------------------------------------------------------------


def health(source: dict) -> tuple[str, str]:
    """The health of a source (Healthy, Error, Not checked or Unknown) and its status in words."""
    status = str(source.get("status") or "")
    if status in STATUSES:
        return STATUSES[status]
    if "ERROR" in status or "FAILURE" in status:
        return ERROR, status
    if "UNCHECKED" in status:
        return UNCHECKED, status
    if source.get("healthy"):
        return HEALTHY, status or "Healthy"
    return UNKNOWN, status or "No status"


def provisioning(source: dict, service_desk: str = "") -> str:
    """How ISC changes the accounts of a source: its connector, a service desk integration (tickets),
    manual work items for the source owner, or nothing (an authoritative source, only read)."""
    if service_desk:
        return SERVICE_DESK
    if "PROVISIONING" in (source.get("features") or []):
        return CONNECTOR
    return READ_ONLY if source.get("authoritative") else MANUAL


def when(value) -> datetime | None:
    """A date and time of the API (ISO 8601, any number of decimals), in UTC when it has no zone."""
    text = re.sub(r"\.(\d+)", lambda m: "." + (m.group(1) + "000000")[:6], str(value or "").strip())
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def last_aggregation(task: dict | None) -> tuple[str, datetime | None, str]:
    """The result, end (or start) and first error message of an account aggregation task."""
    if not task:
        return "", None, ""
    status = str(task.get("completionStatus") or "").upper()
    result = RESULTS.get(status, status.title()) if status else RUNNING
    errors = [m for m in task.get("messages") or [] if str(m.get("type", "")).upper() == "ERROR"]
    message = ""
    if errors:
        text = (errors[0].get("localizedText") or {}).get("message") or errors[0].get("key") or ""
        message = str(text)[:300]
    return result, when(task.get("completed") or task.get("launched") or task.get("created")), message


def _date(moment: datetime | None) -> str:
    return f"{moment:%Y-%m-%d %H:%M}" if moment else ""


def _yes(value) -> str:
    return "yes" if value else "no"


def _feature_order(feature: str) -> tuple:
    return (FEATURES.index(feature) if feature in FEATURES else len(FEATURES), feature)


def _usage_order(usage: str) -> tuple:
    return (USAGES.index(usage) if usage in USAGES else len(USAGES), usage)


def _policies_text(policies: list[dict] | None) -> str:
    if policies is None:
        return ""
    usages = sorted({str(p.get("usageType") or "") for p in policies} - {""}, key=_usage_order)
    return ", ".join(usages) or "none"


def _correlation_text(source: dict, config: dict | None) -> str:
    parts = []
    if config:
        pairs = [f"{a.get('property', '')} = {a.get('value', '')}"
                 for a in config.get("attributeAssignments") or []]
        if pairs:
            parts.append(" AND ".join(pairs))
    rule = (source.get("accountCorrelationRule") or {}).get("name")
    if rule:
        parts.append(f"rule: {rule}")
    if parts:
        return "\n".join(parts)
    return "configured" if config is None and source.get("accountCorrelationConfig") else "none"


def _manager_text(source: dict) -> str:
    mapping = source.get("managerCorrelationMapping") or {}
    if mapping.get("accountAttributeName"):
        return f"{mapping['accountAttributeName']} = {mapping.get('identityAttributeName', '')}"
    rule = (source.get("managerCorrelationRule") or {}).get("name")
    return f"rule: {rule}" if rule else ""


def _sync_text(attributes: list[dict] | None) -> str:
    if attributes is None:
        return ""
    enabled = sum(bool(a.get("enabled")) for a in attributes)
    return f"{enabled} of {len(attributes)} attribute(s)" if enabled else "off"


def _schedules_text(schedules: list[dict] | None) -> str:
    if schedules is None:
        return ""
    names = {"ACCOUNT_AGGREGATION": "Accounts", "GROUP_AGGREGATION": "Groups"}
    lines = [f"{names.get(s.get('type', ''), s.get('type', ''))}: {s.get('cronExpression', '')}"
             for s in schedules]
    return "\n".join(lines) or "none"


def source_row(data: SourceData, source: dict) -> dict:
    source_id = source.get("id", "")
    detail = data.details.get(source_id, {})
    features = [str(f) for f in source.get("features") or []]
    state, status = health(source)
    ref = source.get("cluster") or {}
    cluster = data.clusters.get(ref.get("id", ""), {})
    desk = data.service_desks.get(source_id, "")
    result, finished, _ = last_aggregation(detail.get(AGGREGATION))
    return {
        "Source": source.get("name", ""), "Type": source.get("type") or "",
        "Connector": source.get("connectorName") or source.get("connector") or "",
        "Connection": str(source.get("connectionType") or "").capitalize(),
        "Authoritative": _yes(source.get("authoritative")), "Health": state, "Status": status,
        "Checked on": _date(when(source.get("since"))),
        "Cluster": ref.get("name") or cluster.get("name") or "",
        "Cluster status": cluster.get("status") or "",
        "Provisioning": provisioning(source, desk), "Service desk": desk,
        "Enable / disable": _yes("ENABLE" in features), "Passwords": _yes("PASSWORD" in features),
        "Unlock": _yes("UNLOCK" in features), "Features": ", ".join(sorted(features, key=_feature_order)),
        "Provisioning policies": _policies_text(detail.get(POLICIES)),
        "Before provisioning rule": (source.get("beforeProvisioningRule") or {}).get("name", ""),
        "Account correlation": "" if data.quick else _correlation_text(source, detail.get(CORRELATION)),
        "Manager correlation": _manager_text(source), "Attribute sync": _sync_text(detail.get(SYNC)),
        "Aggregation schedule": _schedules_text(detail.get(SCHEDULES)), "Last aggregation": _date(finished),
        "Last aggregation result": result,
        "Days since aggregation": (data.now - finished).days if finished else None,
        "Accounts": detail.get(ACCOUNTS), "Uncorrelated": detail.get(UNCORRELATED),
        "Delete threshold": source.get("deleteThreshold"),
        "Owner": (source.get("owner") or {}).get("name", ""),
        "Management group": (source.get("managementWorkgroup") or {}).get("name", ""),
        "Created": _date(when(source.get("created"))), "Modified": _date(when(source.get("modified"))),
        "Description": source.get("description") or "", "Id": source_id,
    }


def _check(severity: str, obj: str, check: str, details: str, recommendation: str) -> dict:
    return {"Severity": severity, "Object": obj, "Check": check, "Details": details,
            "Recommendation": recommendation}


def source_checks(data: SourceData, source: dict, row: dict) -> list[dict]:
    detail = data.details.get(source.get("id", ""), {})
    features = set(source.get("features") or [])
    authoritative = bool(source.get("authoritative"))
    found: list[dict] = []

    def add(severity, check, details, recommendation):
        found.append(_check(severity, row["Source"], check, details, recommendation))

    checked = f" (checked on {row['Checked on']})" if row["Checked on"] else ""
    if row["Health"] == ERROR:
        add(WARNING, "Source in error", row["Status"] + checked,
            "Test the connection on the source's page, then check its credentials and virtual appliances: "
            "aggregation and provisioning fail until it is fixed.")
    elif row["Health"] in (UNCHECKED, UNKNOWN):
        add(INFO, "Health not known", row["Status"] + checked,
            "Test the connection on the source's page to know whether it answers.")
    result, finished, message = last_aggregation(detail.get(AGGREGATION))
    reason = f": {message}" if message else ""
    if result in FAILED:
        add(WARNING, "Last aggregation failed", f"{result} on {_date(finished)}{reason}",
            "Fix the cause and aggregate again: until then, ISC works with the accounts of the last "
            "successful aggregation.")
    elif result == "Warning":
        add(INFO, "Last aggregation ended with warnings", f"On {_date(finished)}{reason}",
            "Read the warnings of the aggregation on the source's page.")
    aggregated = "NO_AGGREGATION" not in features
    stale = "Check the aggregation schedule: the accounts and their access may be out of date."
    days = row["Days since aggregation"]
    if aggregated and detail.get(AGGREGATION) == {}:
        add(INFO, "No recent aggregation", "No account aggregation in the task history kept by ISC.", stale)
    elif aggregated and days is not None and days > data.stale_days:
        add(INFO, "No recent aggregation",
            f"Last account aggregation {days} day(s) ago, on {_date(finished)}.", stale)
    schedules = detail.get(SCHEDULES)
    if (aggregated and schedules is not None and row["Connection"] != "File"
            and not any(s.get("type") == "ACCOUNT_AGGREGATION" for s in schedules)):
        add(INFO, "No aggregation schedule", "Accounts are aggregated only when someone starts it.",
            "Schedule the account aggregation if ISC should follow the changes made in the source.")
    correlation = detail.get(CORRELATION)
    if (not authoritative and correlation is not None and not correlation.get("attributeAssignments")
            and not source.get("accountCorrelationRule") and row["Accounts"] != 0):
        add(WARNING, "No account correlation",
            "No correlation attribute and no correlation rule: new accounts are not linked to their "
            "identity.",
            "Set the account correlation of the source (identity attribute = account attribute).")
    accounts, uncorrelated = row["Accounts"], row["Uncorrelated"]
    if not authoritative and accounts and uncorrelated:
        share = 100 * uncorrelated / accounts
        add(WARNING if share >= UNCORRELATED_LIMIT else INFO, "Uncorrelated accounts",
            f"{uncorrelated} of {accounts} account(s) ({share:.1f}%) belong to no identity.",
            "Correlate them to their identity, or confirm that they are service or test accounts.")
    threshold = source.get("deleteThreshold")
    if isinstance(threshold, int) and not isinstance(threshold, bool) and threshold >= DELETE_LIMIT:
        details = (f"{threshold}%: an aggregation can delete up to {threshold}% of the accounts before ISC "
                   "skips the delete phase.")
        if authoritative:
            details += " The source is authoritative: its accounts make the identities."
        add(WARNING if authoritative else INFO, "High delete threshold", details,
            "Lower it (10% by default) so that an incomplete file or an empty answer stops before accounts "
            "are deleted.")
    policies = detail.get(POLICIES)
    if ("PROVISIONING" in features and not authoritative and policies is not None
            and not any(str(p.get("usageType") or "").upper() == "CREATE" for p in policies)):
        add(INFO, "No account create policy",
            "The source can provision, but no create policy sets the attributes of new accounts.",
            "If ISC creates accounts here (roles, access requests, lifecycle states), add a create policy.")
    return found


def cluster_rows(data: SourceData) -> list[dict]:
    used = Counter((s.get("cluster") or {}).get("id", "") for s in data.sources)
    rows = []
    for cluster_id, cluster in data.clusters.items():
        if data.patterns and not used[cluster_id]:
            continue
        rows.append({
            "Cluster": cluster.get("name") or cluster_id,
            "Type": cluster.get("clientType") or cluster.get("type") or "",
            "Status": cluster.get("status") or "",
            "Health": cluster.get("consolidatedHealthIndicatorsStatus") or "",
            "Virtual appliances": len(cluster.get("clientIds") or []),
            "Version": cluster.get("currentInstalledReleaseVersion") or cluster.get("ccgVersion") or "",
            "Update available": cluster.get("updatePackage") or "", "Sources": used[cluster_id]})
    return sorted(rows, key=lambda r: str(r["Cluster"]).lower())


def cluster_checks(rows: list[dict]) -> list[dict]:
    found = []
    for row in rows:
        status = str(row["Status"]).upper()
        if status and status != "NORMAL" and row["Sources"]:
            details = f"Status {row['Status']}; {row['Sources']} source(s) use it"
            details += "; no virtual appliance." if not row["Virtual appliances"] else "."
            found.append(_check(WARNING if status in ("FAILED", "NO_CLIENTS") else INFO,
                                f"Cluster {row['Cluster']}", "Cluster not normal", details,
                                "Check the virtual appliances of the cluster: its sources cannot aggregate "
                                "or provision while it is down."))
    return found


def feature_rows(data: SourceData) -> list[dict]:
    found = sorted({str(f) for s in data.sources for f in s.get("features") or []}, key=_feature_order)
    rows = []
    for source in data.sources:
        features = set(source.get("features") or [])
        desk = data.service_desks.get(source.get("id", ""), "")
        # Not "Provisioning": the column of the PROVISIONING feature (Excel headers ignore case).
        rows.append({"Source": source.get("name", ""), "Provisioned by": provisioning(source, desk),
                     **{f: "yes" if f in features else "" for f in found}})
    return sorted(rows, key=lambda r: r["Source"].lower())


def policy_rows(data: SourceData) -> list[dict]:
    rows = []
    for source in data.sources:
        for policy in data.details.get(source.get("id", ""), {}).get(POLICIES) or []:
            fields = [str(f.get("name") or "") for f in policy.get("fields") or []]
            rows.append({"Source": source.get("name", ""), "Usage": policy.get("usageType") or "",
                         "Policy": policy.get("name") or "", "Fields": len(fields),
                         "Attributes": "\n".join(fields)})
    return sorted(rows, key=lambda r: (r["Source"].lower(), _usage_order(r["Usage"])))


def sync_rows(data: SourceData) -> list[dict]:
    rows = []
    for source in data.sources:
        for attribute in data.details.get(source.get("id", ""), {}).get(SYNC) or []:
            rows.append({"Source": source.get("name", ""),
                         "Identity attribute": attribute.get("displayName") or attribute.get("name") or "",
                         "Technical name": attribute.get("name") or "",
                         "Account attribute": attribute.get("target") or "",
                         "Synchronised": _yes(attribute.get("enabled"))})
    return sorted(rows, key=lambda r: (r["Source"].lower(), r["Synchronised"] != "yes",
                                       r["Identity attribute"].lower()))


@dataclass
class Result:
    data: SourceData
    sources: list[dict]                    # one row per source (SOURCE_COLUMNS)
    features: list[dict]
    policies: list[dict]
    sync: list[dict]
    clusters: list[dict]
    checks: list[dict]

    @property
    def warnings(self) -> int:
        return sum(c["Severity"] == WARNING for c in self.checks)


def analyse(data: SourceData) -> Result:
    rows, checks = [], []
    for source in sorted(data.sources, key=lambda s: str(s.get("name", "")).lower()):
        row = source_row(data, source)
        rows.append(row)
        checks += source_checks(data, source, row)
    clusters = cluster_rows(data)
    checks += cluster_checks(clusters)
    checks.sort(key=lambda c: (c["Severity"] != WARNING, c["Object"].lower()))
    return Result(data, rows, feature_rows(data), policy_rows(data), sync_rows(data), clusters, checks)


# -- report -------------------------------------------------------------------------------

COLOURS = {HEALTHY: dashboard.GREEN, ERROR: dashboard.RED, UNCHECKED: dashboard.GREY,
           UNKNOWN: dashboard.AMBER, CONNECTOR: dashboard.BLUE, SERVICE_DESK: "8E24AA",
           MANUAL: dashboard.AMBER, READ_ONLY: "546E7A"}
WIDE = {"Status", "Features", "Provisioning policies", "Account correlation", "Aggregation schedule",
        "Description", "Details", "Recommendation", "Attributes", "Value"}
READ_ME = [
    ("What is this?", "The status and the capabilities of the sources of the tenant, read on the date "
                      "above. Nothing was changed in the tenant."),
    ("Health", f"{HEALTHY}, {ERROR}, {UNCHECKED} or {UNKNOWN}, from the last health check of ISC "
               "(Checked on); Status gives its reason."),
    ("Provisioning", f"How ISC changes the accounts. {CONNECTOR}: the connector creates and changes them "
                     f"(the PROVISIONING feature). {SERVICE_DESK}: a service desk integration opens "
                     f"tickets. {MANUAL}: ISC asks the source owner to do it. {READ_ONLY}: an "
                     "authoritative source that ISC only reads."),
    ("Enable / disable, Passwords, Unlock", "The source has the ENABLE, PASSWORD or UNLOCK feature."),
    ("Features", "The features of the connector, one column each in the Features sheet. PROVISIONING: "
                 "creates and changes accounts. SYNC_PROVISIONING: at once, not queued. ENABLE: enabled "
                 "or disabled accounts. PASSWORD: password changes. UNLOCK: locked accounts. "
                 "GROUP_PROVISIONING: creates and changes groups. AUTHENTICATE: pass-through "
                 "authentication. NO_AGGREGATION: no aggregation. NO_RANDOM_ACCESS: one account cannot be "
                 "read on its own."),
    ("Provisioning policies", "The usage types of the source's provisioning policies (CREATE: the "
                              "attributes of new accounts); the policies and their fields are in their own "
                              "sheet."),
    ("Account correlation", "How an account is linked to its identity: identity attribute = account "
                            "attribute pairs, or a correlation rule. Manager correlation: how the manager "
                            "is found."),
    ("Attribute sync", "Identity attributes synchronised to account attributes, out of those the source "
                       "can receive (Attribute sync sheet). Before enabling more, see "
                       "'analyze-before-sync'."),
    ("Last aggregation", "The last account aggregation in the task history that ISC keeps, its result and "
                         "the days since then."),
    ("Accounts, Uncorrelated", "The accounts of the source, and those linked to no identity."),
    ("Delete threshold", "If an aggregation would delete more than this share of the accounts, ISC skips "
                         "the delete phase."),
    ("Checks", f"Warnings: a source or cluster in error, a failed aggregation, no account correlation, "
               f"{UNCORRELATED_LIMIT}% or more uncorrelated accounts, a delete threshold of "
               f"{DELETE_LIMIT}% or more on an authoritative source. Infos: points to review."),
    ("Empty columns", "With --quick, only the source list is read: no policies, schedules, correlation, "
                      "attribute sync, last aggregation or account counts. A column is also empty when it "
                      "could not be read: see Settings."),
    ("Left out", "The connection settings of the sources, where their credentials are kept, are not in "
                 "this report."),
]


def settings_rows(data: SourceData) -> list[dict]:
    rows = [{"Setting": "Sources", "Value": ", ".join(data.patterns) or "all"},
            {"Setting": "Read", "Value": "the source list only (--quick)" if data.quick else
             "the source list, then the details of each source"},
            {"Setting": "No recent aggregation", "Value": f"more than {data.stale_days} day(s)"}]
    rows += [{"Setting": "Not found", "Value": f"--source {pattern} matches no source"}
             for pattern in data.unmatched]
    return rows + [{"Setting": "Note", "Value": note} for note in data.notes]


def _kpis(result: Result) -> list[tuple[str, str, str]]:
    data, rows = result.data, result.sources
    total = len(rows)
    states = Counter(r["Health"] for r in rows)
    modes = Counter(r["Provisioning"] for r in rows)
    authoritative = sum(r["Authoritative"] == "yes" for r in rows)
    kpis = [("Sources", f"{total}  ({authoritative} authoritative)", dashboard.BLUE),
            ("Healthy", f"{states[HEALTHY]} of {total}",
             dashboard.GREEN if states[HEALTHY] == total else dashboard.AMBER),
            ("In error", str(states[ERROR]), dashboard.RED if states[ERROR] else dashboard.GREEN),
            ("Provisioned by their connector",
             f"{modes[CONNECTOR]}  ({modes[SERVICE_DESK]} service desk, {modes[MANUAL]} manual)",
             dashboard.BLUE)]
    if not data.quick:
        failed = sum(r["Last aggregation result"] in FAILED for r in rows)
        kpis.append(("Last aggregation failed", str(failed), dashboard.RED if failed else dashboard.GREEN))
        counted = [r for r in rows if r["Authoritative"] != "yes" and r["Accounts"]]
        accounts = sum(r["Accounts"] for r in counted)
        uncorrelated = sum(r["Uncorrelated"] or 0 for r in counted)
        share = f" ({100 * uncorrelated / accounts:.1f}%)" if accounts else ""
        kpis.append(("Uncorrelated accounts", f"{uncorrelated} of {accounts}{share}",
                     dashboard.AMBER if uncorrelated else dashboard.GREEN))
    if result.clusters:
        down = sum(str(c["Status"]).upper() not in ("NORMAL", "") for c in result.clusters)
        kpis.append(("Clusters not normal", f"{down} of {len(result.clusters)}",
                     dashboard.RED if down else dashboard.GREEN))
    infos = len(result.checks) - result.warnings
    kpis.append(("Warnings", f"{result.warnings}  ({infos} to review)",
                 dashboard.RED if result.warnings else dashboard.GREEN))
    return kpis


def _charts(result: Result) -> list[dashboard.Chart]:
    rows = result.sources
    states = Counter(r["Health"] for r in rows)
    modes = Counter(r["Provisioning"] for r in rows)
    charts = []
    labels = [s for s in HEALTH if states[s]]
    if labels:
        charts.append(dashboard.Chart("Sources by health", "doughnut", labels,
                                      [("Sources", [states[s] for s in labels], "")]))
    labels = [m for m in PROVISIONING if modes[m]]
    if labels:
        charts.append(dashboard.Chart("Sources by provisioning", "doughnut", labels,
                                      [("Sources", [modes[m] for m in labels], "")]))
    counted = sorted((r for r in rows if r["Accounts"]), key=lambda r: (-r["Accounts"], r["Source"].lower()))
    if counted:
        counted = counted[:12]
        uncorrelated = [r["Uncorrelated"] or 0 for r in counted]
        charts.append(dashboard.Chart(
            "Accounts by source", "stacked", [r["Source"] for r in counted],
            [("Correlated", [r["Accounts"] - u for r, u in zip(counted, uncorrelated, strict=True)],
              dashboard.BLUE),
             ("Uncorrelated", uncorrelated, dashboard.AMBER)]))
    connectors = Counter(r["Connector"] or "(none)" for r in rows).most_common(10)
    if connectors:
        charts.append(dashboard.Chart("Sources by connector", "bar", [c for c, _ in connectors],
                                      [("Sources", [n for _, n in connectors], dashboard.BLUE)]))
    return charts


def build_dashboard(result: Result, title: str = "") -> dashboard.Dashboard:
    colours = {"Health": {HEALTHY: "green", ERROR: "red", UNCHECKED: "grey"},
               "Last aggregation result": {"Success": "green", "Error": "red", "Terminated": "red",
                                           "Temporary": "red"}}
    tables = [
        dashboard.Table("Checks", result.checks, wide=WIDE, fills={"Severity": SEVERITY_FILLS}),
        dashboard.Table("Sources", result.sources, wide=WIDE, text_colours=colours,
                        red_positive={"Uncorrelated"}),
        dashboard.Table("Features", result.features),
        dashboard.Table("Provisioning policies", result.policies, wide=WIDE),
        dashboard.Table("Attribute sync", result.sync, text_colours={"Synchronised": {"yes": "green"}}),
        dashboard.Table("Clusters", result.clusters,
                        text_colours={"Status": {"NORMAL": "green", "FAILED": "red", "NO_CLIENTS": "red"}}),
        dashboard.Table("Settings", settings_rows(result.data), wide=WIDE),
    ]
    sections = [
        dashboard.Section("Checks", result.checks, ["Severity", "Object", "Check", "Details"]),
        dashboard.Section("Sources", result.sources, ["Source", "Connector", "Health", "Provisioning",
                                                      "Last aggregation", "Last aggregation result",
                                                      "Accounts", "Uncorrelated"]),
        dashboard.Section("Clusters", result.clusters, ["Cluster", "Status", "Virtual appliances", "Version",
                                                        "Sources"]),
    ]
    return dashboard.Dashboard(
        title or "Source status", dashboard.subtitle(result.data.origin), _kpis(result), _charts(result),
        tables, sections, READ_ME, colours=COLOURS, wide_pdf={"Details", "Source"},
        alert=lambda row: row.get("Severity") == WARNING or row.get("Health") == ERROR)


def write_xlsx(path: str, result: Result, title: str = "") -> str:
    return dashboard.write_xlsx(build_dashboard(result, title), path)


def write_pdf(path: str, result: Result, title: str = "", max_rows: int = 500) -> str:
    return dashboard.write_pdf(build_dashboard(result, title), path, max_rows)


def write_csv(path: str, result: Result) -> str:
    rows = [["" if row[c] is None else row[c] for c in SOURCE_COLUMNS] for row in result.sources]
    return write_table(path, SOURCE_COLUMNS, rows, ";")
