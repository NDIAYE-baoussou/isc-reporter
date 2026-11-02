"""Source status and capabilities."""

import csv
from datetime import datetime, timezone

from openpyxl import load_workbook

from conftest import FakeApi, FakeIsc, Raw
from isc_reporter import cli, demo, source_status
from isc_reporter.api import HttpError

NOW = datetime(2026, 10, 15, 12, tzinfo=timezone.utc)
AD = {"id": "ad", "name": "Active Directory", "connectorName": "Active Directory", "connectionType": "direct",
      "features": ["PROVISIONING", "ENABLE", "PASSWORD"], "healthy": True, "status": "SOURCE_STATE_HEALTHY",
      "accountCorrelationConfig": {"id": "ad-correlation"}, "cluster": {"id": "c1", "name": "Paris"},
      "connectorAttributes": {"password": "do-not-export"}}
HR = {"id": "hr", "name": "HR", "connectionType": "file", "features": ["NO_RANDOM_ACCESS"],
      "authoritative": True, "healthy": False, "status": "SOURCE_STATE_ERROR_ACCOUNT_FILE_IMPORT",
      "deleteThreshold": 60}


def test_health_and_provisioning():
    assert source_status.health(AD) == (source_status.HEALTHY, "Healthy")
    assert source_status.health(HR) == (source_status.ERROR, "Error importing the account file")
    assert source_status.health({"status": "SOURCE_STATE_ERROR_NEW"})[0] == source_status.ERROR
    assert source_status.health({"healthy": True}) == (source_status.HEALTHY, "Healthy")
    assert source_status.health({}) == (source_status.UNKNOWN, "No status")
    assert source_status.provisioning(AD) == source_status.CONNECTOR
    assert source_status.provisioning(AD, "ServiceNow") == source_status.SERVICE_DESK
    assert source_status.provisioning(HR) == source_status.READ_ONLY
    assert source_status.provisioning({"features": []}) == source_status.MANUAL


def test_dates_and_last_aggregation():
    assert source_status.when("2026-10-14T02:00:13.1234567Z") == datetime(2026, 10, 14, 2, 0, 13, 123456,
                                                                          tzinfo=timezone.utc)
    assert source_status.when("2026-10-14T02:00:13.5") == datetime(2026, 10, 14, 2, 0, 13, 500000,
                                                                   tzinfo=timezone.utc)
    assert source_status.when("") is None and source_status.when("soon") is None
    failed = {"completionStatus": "ERROR", "created": "2026-10-14T02:00:00Z",
              "messages": [{"type": "INFO", "key": "started"},
                           {"type": "ERROR", "key": "x", "localizedText": {"message": "Connection refused"}}]}
    result, finished, message = source_status.last_aggregation(failed)
    assert (result, finished.day, message) == ("Error", 14, "Connection refused")
    assert source_status.last_aggregation({"completionStatus": None})[0] == source_status.RUNNING
    assert source_status.last_aggregation({}) == ("", None, "")


def test_select_sources_by_name():
    chosen, unmatched = source_status.select([AD, HR], ["active*", "nothing"])
    assert chosen == [AD] and unmatched == ["nothing"]
    assert source_status.select([AD, HR], None) == ([AD, HR], [])


def fake_isc(tasks_denied=False):
    def accounts(filters, **kwargs):
        total = {"ad": 200, "hr": 0}["ad" if '"ad"' in filters else "hr"]
        return Raw([], headers={"X-Total-Count": str(30 if "uncorrelated" in filters else total)})

    def tasks(**kwargs):
        if tasks_denied:
            raise HttpError(403, {}, b'{"messages": [{"text": "Forbidden"}]}')
        return Raw([{"completionStatus": "SUCCESS", "completed": "2026-10-15T08:00:00Z"}])

    def schedules(source_id):
        if source_id == "hr":
            raise HttpError(400, {}, b'{"messages": [{"text": "Not supported"}]}')
        return Raw([{"type": "ACCOUNT_AGGREGATION", "cronExpression": "0 0 * * * ?"}])

    sources = FakeApi(
        list_sources_v1_without_preload_content=Raw([AD, HR]),
        list_provisioning_policies_v1_without_preload_content=lambda source_id, **kw: Raw(
            [{"name": "Create", "usageType": "CREATE", "fields": [{"name": "mail"}]}]
            if source_id == "ad" else []),
        get_source_schedules_v1_without_preload_content=schedules,
        get_correlation_config_v1_without_preload_content=Raw(
            {"attributeAssignments": [{"property": "employeeNumber", "value": "employeeID"}]}),
        get_source_attr_sync_config_v1_without_preload_content=Raw(
            {"attributes": [{"name": "email", "displayName": "Email", "enabled": True, "target": "mail"},
                            {"name": "title", "displayName": "Title", "enabled": False, "target": "title"}]}))
    return FakeIsc(
        sources=sources, accounts=FakeApi(list_accounts_v1_without_preload_content=accounts),
        task_management=FakeApi(get_task_status_list_v1_without_preload_content=tasks),
        managed_clusters=FakeApi(get_managed_clusters_v1_without_preload_content=Raw(
            [{"id": "c1", "name": "Paris", "status": "FAILED", "clientIds": []}])),
        service_desk_integrations=FakeApi(get_service_desk_integrations_v1_without_preload_content=Raw([])))


def test_fetch_reads_each_source_and_notes_what_fails():
    isc = fake_isc(tasks_denied=True)
    data = source_status.fetch(isc)
    ad, hr = data.details["ad"], data.details["hr"]
    assert (ad["accounts"], ad["uncorrelated"], hr["accounts"], hr["uncorrelated"]) == (200, 30, 0, 0)
    assert len(isc.accounts.called("list_accounts_v1_without_preload_content")) == 3   # HR has no account
    assert ad["schedules"] and hr["schedules"] is None
    assert [a["name"] for a in ad["sync"]] == ["email", "title"]
    assert "sync" not in hr                              # HR cannot provision
    assert isc.sources.called("get_source_attr_sync_config_v1_without_preload_content") == [
        {"id": "ad", "x_sail_point_experimental": "true"}]
    assert hr["correlation"] == {}                       # no correlation configured: nothing to read
    assert ad["aggregation"] is None and hr["aggregation"] is None
    assert len(isc.task_management.calls) == 1           # denied once: not asked again for HR
    assert data.notes == ["Aggregation schedules not read for 1 source(s) (HTTP 400: Not supported): HR.",
                          "Last aggregations not read (HTTP 403: Forbidden)."]


def test_quick_reads_the_source_list_only():
    isc = fake_isc()
    data = source_status.fetch(isc, ["hr"], quick=True)
    assert [s["name"] for s in data.sources] == ["HR"] and data.details == {}
    assert [name for name, _ in isc.sources.calls] == ["list_sources_v1_without_preload_content"]
    assert not isc.accounts.calls and not isc.task_management.calls
    result = source_status.analyse(data)
    assert result.sources[0]["Accounts"] is None and result.sources[0]["Account correlation"] == ""
    assert {c["Check"] for c in result.checks} == {"Source in error", "High delete threshold"}


def test_rows_and_checks():
    data = source_status.fetch(fake_isc())
    data.now = NOW
    result = source_status.analyse(data)
    ad, hr = result.sources
    assert (ad["Health"], ad["Provisioning"], ad["Cluster status"], ad["Enable / disable"]) == (
        source_status.HEALTHY, source_status.CONNECTOR, "FAILED", "yes")
    assert (ad["Provisioning policies"], ad["Attribute sync"], ad["Account correlation"]) == (
        "CREATE", "1 of 2 attribute(s)", "employeeNumber = employeeID")
    assert (ad["Last aggregation result"], ad["Days since aggregation"], ad["Uncorrelated"]) == (
        "Success", 0, 30)
    assert (hr["Provisioning"], hr["Aggregation schedule"], hr["Account correlation"]) == (
        source_status.READ_ONLY, "", "none")
    assert "do-not-export" not in str(result.sources)                     # connection settings left out
    checks = [(c["Severity"], c["Object"], c["Check"]) for c in result.checks]
    assert checks == [
        ("Warning", "Active Directory", "Uncorrelated accounts"),          # 15%
        ("Warning", "Cluster Paris", "Cluster not normal"),
        ("Warning", "HR", "Source in error"),
        ("Warning", "HR", "High delete threshold"),
    ]
    assert result.features[0] == {"Source": "Active Directory", "Provisioned by": source_status.CONNECTOR,
                                  "PROVISIONING": "yes", "ENABLE": "yes", "PASSWORD": "yes",
                                  "NO_RANDOM_ACCESS": ""}
    assert result.policies == [{"Source": "Active Directory", "Usage": "CREATE", "Policy": "Create",
                                "Fields": 1, "Attributes": "mail"}]
    synced = [(r["Technical name"], r["Synchronised"]) for r in result.sync]
    assert synced == [("email", "yes"), ("title", "no")]


def test_demo_shows_every_check(tmp_path):
    result = source_status.analyse(demo.sample_sources(NOW))
    found = {(c["Object"], c["Check"]) for c in result.checks}
    assert found >= {
        ("SAP", "Source in error"), ("SAP", "Last aggregation failed"),
        ("Legacy Finance", "Health not known"), ("Legacy Finance", "Last aggregation ended with warnings"),
        ("Legacy Finance", "No recent aggregation"),
        ("Salesforce", "No recent aggregation"), ("Salesforce", "No aggregation schedule"),
        ("Salesforce", "No account correlation"), ("Salesforce", "Uncorrelated accounts"),
        ("Salesforce", "No account create policy"), ("HR System", "High delete threshold"),
        ("Cluster Lyon cluster", "Cluster not normal")}
    assert {r["Provisioning"] for r in result.sources} == set(source_status.PROVISIONING)
    path = source_status.write_xlsx(str(tmp_path / "sources.xlsx"), result)
    assert load_workbook(path).sheetnames == [
        "Dashboard", "Checks", "Sources", "Features", "Provisioning policies", "Attribute sync", "Clusters",
        "Settings", "Chart data", "Read me"]
    assert source_status.write_pdf(str(tmp_path / "sources.pdf"), result)


def test_cli(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: fake_isc())
    out = tmp_path / "sources"
    assert cli.main(["sources", "--source", "Active*", "--source", "typo", "--format", "xlsx", "csv",
                     "-o", str(out), "--log-dir", ""]) == 0
    assert "--source typo matches no source" in capsys.readouterr().err
    with open(str(out) + ".csv", encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.DictReader(fh, delimiter=";"))
    assert [(r["Source"], r["Health"], r["Accounts"]) for r in rows] == [
        ("Active Directory", "Healthy", "200")]
    settings = list(load_workbook(str(out) + ".xlsx")["Settings"].iter_rows(min_row=2, values_only=True))
    assert ("Not found", "--source typo matches no source") in settings
    assert cli.main(["sources", "--source", "typo", "--format", "csv", "-o", str(out), "--log-dir", ""]) == 2
