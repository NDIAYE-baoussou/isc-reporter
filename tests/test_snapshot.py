"""Configuration snapshot: what is read, what is written, and reading it back with --from."""

import json
import os
from datetime import datetime, timezone

import pytest
from openpyxl import load_workbook

from conftest import FakeApi, FakeIsc, Raw
from isc_reporter import cli, demo, identity_profiles, snapshot, transform_analysis
from isc_reporter.errors import UsageError

NOW = datetime(2026, 11, 2, 9, tzinfo=timezone.utc)
LATER = datetime(2026, 11, 3, 9, tzinfo=timezone.utc)


def pages(items, status=200):
    """A paged ``..._without_preload_content`` method returning *items* on its first page."""
    return lambda limit=None, offset=0, **kwargs: Raw(items if not offset else [], status)


def tenant(**changes):
    """A tenant with one object of each type, the sub-objects of profiles, sources and roles."""
    apis = dict(
        transforms=FakeApi(list_transforms_v1_without_preload_content=pages(
            [{"id": "t1", "name": "Lower", "type": "lower", "attributes": {}, "internal": False}])),
        identity_profiles=FakeApi(list_identity_profiles_v1_without_preload_content=pages(
            [{"id": "p1", "name": "Employees", "identityCount": 42, "identityRefreshRequired": True,
              "identityAttributeConfig": {"attributeTransforms": []}}])),
        lifecycle_states=FakeApi(get_lifecycle_states_v1_without_preload_content=pages(
            [{"technicalName": "inactive", "priority": 20, "identityCount": 3},
             {"technicalName": "active", "priority": 10, "modified": "2026-11-01"}])),
        identity_attributes=FakeApi(list_identity_attributes_v1_without_preload_content=lambda **kw: Raw(
            [{"name": "department", "displayName": "Department", "standard": False, "system": False}])),
        sources=FakeApi(
            list_sources_v1_without_preload_content=pages(
                [{"id": "s1", "name": "AD", "connector": "active-directory", "healthy": False,
                  "connectorAttributes": {"host": "dc01", "password": "2:ACP:xyz", "encrypted": "password",
                                          "cloudCacheUpdate": 1785137182946}}]),
            get_source_schemas_v1_without_preload_content=lambda **kw: Raw(
                [{"name": "group"}, {"name": "account", "attributes": [{"name": "sAMAccountName"}]}]),
            list_provisioning_policies_v1_without_preload_content=pages(
                [{"usageType": "UPDATE", "fields": []}, {"usageType": "CREATE", "fields": []}]),
            get_correlation_config_v1_without_preload_content=lambda **kw: Raw({"message": "none"}, 404)),
        roles=FakeApi(list_roles_v1_without_preload_content=pages(
            [{"id": "r1", "name": "Regional", "dimensional": True, "membership": None},
             {"id": "r2", "name": "Finance", "membership": None}])),
        dimensions=FakeApi(list_dimensions_v1_without_preload_content=pages(
            [{"id": "d2", "name": "West"}, {"id": "d1", "name": "East"}])),
        access_profiles=FakeApi(list_access_profiles_v1_without_preload_content=pages(
            [{"id": "a1", "name": "Birthright", "source": {"id": "s1"}, "entitlements": []}])),
        segments=FakeApi(list_segments_v1_without_preload_content=pages([{"id": "g1", "name": "Finance"}])),
        connector_rules=FakeApi(get_connector_rule_list_v1_without_preload_content=pages(
            [{"id": "c1", "name": "Build map", "type": "BuildMap", "attributes": None,
              "sourceCode": {"script": "return map;"}}])),
        workflows=FakeApi(list_workflows_v1_without_preload_content=lambda **kw: Raw(
            {"messages": [{"text": "forbidden"}]}, 403)),
        sod_policies=FakeApi(list_sod_policies_v1_without_preload_content=pages(
            [{"id": "x1", "name": "SoD"}])),
        governance_groups=FakeApi(list_workgroups_v1_without_preload_content=pages(
            [{"id": "w1", "name": "IAM", "memberCount": 4}])),
        password_policies=FakeApi(list_password_policies_v1_without_preload_content=pages(
            [{"id": "pp", "name": "Default", "lastUpdated": "2026-11-01"}])),
    )
    apis.update(changes)
    return FakeIsc(**apis)


def test_fetch_reads_every_type_with_their_sub_objects():
    isc = tenant()
    found = snapshot.fetch(isc, list(snapshot.KINDS))
    assert set(found.objects) == set(snapshot.KINDS) - {"workflows"}               # 403: left out
    assert found.notes == ["Workflows not read (HTTP 403: forbidden): the files of an earlier snapshot "
                           "are kept."]
    profile = found.objects["identity-profiles"][0]
    assert [s["technicalName"] for s in profile["lifecycleStates"]] == ["active", "inactive"]  # by priority
    source = found.objects["sources"][0]
    assert [s["name"] for s in source["schemas"]] == ["account", "group"]
    assert [p["usageType"] for p in source["provisioningPolicies"]] == ["CREATE", "UPDATE"]
    assert "correlationConfig" not in source                                        # 404: none
    regional, finance = found.objects["roles"]
    assert [d["name"] for d in regional["dimensions"]] == ["East", "West"] and "dimensions" not in finance
    assert isc.dimensions.called("list_dimensions_v1_without_preload_content")[0]["limit"] == 50
    assert isc.identity_attributes.called("list_identity_attributes_v1_without_preload_content") == [
        {"include_system": True, "include_silent": True}]
    assert isc.password_policies.called("list_password_policies_v1_without_preload_content")[0][
        "x_sail_point_experimental"] == "true"


def test_sub_objects_that_cannot_be_read_are_noted():
    isc = tenant(lifecycle_states=FakeApi(get_lifecycle_states_v1_without_preload_content=pages([], 403)))
    found = snapshot.fetch(isc, ["identity-profiles"])
    assert "lifecycleStates" not in found.objects["identity-profiles"][0]
    assert found.notes[0].startswith("Lifecycle states not read for 1 identity profile(s) (HTTP 403")


def test_secrets_are_redacted():
    source = {"connectorAttributes": {
        "password": "2:ACP:xyz", "encrypted": "password,['accessToken']", "accessToken": "2:abc",
        "clientSecret": "s3cret", "token_url": "https://login.example.com/token", "apiKey": "",
        "authenticationType": "OAUTH", "httpCookieSpecsStandard": "standard", "pageSize": 100,
        "domainSettings": [{"domain": "example.com", "password": "pw"}]}}
    workflow = {"definition": {"steps": {"HTTP Request": {"attributes": {
        "oAuthClientSecret": "s", "oAuthCredentialLocation": "oAuthInHeader",
        "requestHeaders": {"Authorization": "Bearer abc"}, "jsonRequestBody": {"secret": "x", "id": "1"}}}}}}
    def static(value):
        return {"type": "static", "attributes": {"value": value}}

    policy = {"fields": [
        {"name": "password", "type": "secret", "transform": static("Pa55")},
        {"name": "RESET_PASSWORD", "type": "string", "transform": static("abc")},
        {"name": "RESET_PASSWORD_FLAG", "transform": static("yes")},
        {"name": "forceChangePasswordNextLogin", "type": "boolean", "transform": static("true")},
        {"name": "pwdLastSet", "type": "string",
         "transform": {"type": "generateRandomString", "attributes": {}}}]}
    assert snapshot.redact(source) == 4
    attributes = source["connectorAttributes"]
    assert {k for k, v in attributes.items() if v == snapshot.REDACTED} == {"password", "accessToken",
                                                                             "clientSecret"}
    assert attributes["domainSettings"][0] == {"domain": "example.com", "password": snapshot.REDACTED}
    assert attributes["token_url"].startswith("https") and attributes["httpCookieSpecsStandard"] == "standard"
    assert snapshot.redact(workflow) == 3
    step = workflow["definition"]["steps"]["HTTP Request"]["attributes"]
    assert step["oAuthCredentialLocation"] == "oAuthInHeader" and step["jsonRequestBody"]["id"] == "1"
    assert step["requestHeaders"]["Authorization"] == snapshot.REDACTED
    assert snapshot.redact(policy) == 2
    assert [f["transform"]["attributes"].get("value") for f in policy["fields"]] == [
        snapshot.REDACTED, snapshot.REDACTED, "yes", "true", None]


def test_files_are_stable(tmp_path):
    found = snapshot.fetch(tenant(), list(snapshot.KINDS))
    folder = tmp_path / "snap"
    result = snapshot.write(found, str(folder), now=NOW)
    assert result.counts["sources"] == 1 and result.since == "" and result.redacted == 1
    text = (folder / "sources" / "AD.json").read_text(encoding="utf-8")
    assert text.startswith('{\n  "id": "s1",\n  "name": "AD",\n')                   # id and name first
    source = json.loads(text)
    assert source["connectorAttributes"] == {"encrypted": "password", "host": "dc01",
                                             "password": snapshot.REDACTED}
    assert "healthy" not in source
    profile = json.loads((folder / "identity-profiles" / "Employees.json").read_text(encoding="utf-8"))
    assert "identityCount" not in profile and "identityRefreshRequired" not in profile
    assert all("identityCount" not in s and "modified" not in s for s in profile["lifecycleStates"])
    manifest = json.loads((folder / snapshot.MANIFEST).read_text(encoding="utf-8"))
    assert manifest["tenant"] == "https://tenant.api.identitynow.com" and manifest["redacted"] == 1
    assert list(manifest["types"])[:2] == ["transforms", "identity-profiles"]
    assert manifest["types"]["roles"] == {"objects": 2, "taken": "2026-11-02T09:00:00+00:00",
                                          "endpoint": "GET /roles/v1"}
    assert manifest["changes"] == [] and manifest["notes"] == found.notes
    references = {"criteria": [{"type": "ENTITLEMENT", "id": "2", "name": "RH"},
                               {"type": "ENTITLEMENT", "id": "1", "name": "DEV"}],
                  "approvalSchemes": [{"approverType": "MANAGER"}, {"approverType": "APP_OWNER"}]}
    stable = snapshot.stable("sod-policies", references)
    assert [r["name"] for r in stable["criteria"]] == ["DEV", "RH"]                  # references: any order
    assert [a["approverType"] for a in stable["approvalSchemes"]] == ["MANAGER", "APP_OWNER"]   # kept

    again = snapshot.write(snapshot.fetch(tenant(), list(snapshot.KINDS)), str(folder), now=LATER)
    assert again.changes == [] and again.since == "2026-11-02T09:00:00+00:00"       # nothing changed


def test_an_earlier_snapshot_is_brought_up_to_date(tmp_path):
    folder = str(tmp_path / "snap")
    first = snapshot.Snapshot("https://acme.api.identitynow.com", {
        "transforms": [{"id": "1", "name": "Lower", "type": "lower"},
                       {"id": "2", "name": "Old", "type": "trim"},
                       {"id": "3", "name": "same", "type": "upper"}],
        "roles": [{"id": "r", "name": "Finance"}]})
    snapshot.write(first, folder, now=NOW)
    roles_file = os.path.join(folder, "roles", "Finance.json")
    stamp = os.stat(roles_file).st_mtime_ns
    second = snapshot.Snapshot("https://acme.api.identitynow.com/", {           # only transforms read
        "transforms": [{"id": "1", "name": "Lower", "type": "upper"},
                       {"id": "3", "name": "SAME", "type": "upper"},
                       {"id": "4", "name": "New", "type": "trim"}]}, ["Roles not read (HTTP 403)."])
    result = snapshot.write(second, folder, now=LATER)
    assert sorted((c["file"], c["change"]) for c in result.changes) == [
        ("transforms/Lower.json", "changed"), ("transforms/New.json", "added"),
        ("transforms/Old.json", "removed"), ("transforms/SAME.json", "changed")]   # renamed: same, other case
    assert sorted(os.listdir(os.path.join(folder, "transforms"))) == ["Lower.json", "New.json", "SAME.json"]
    assert os.stat(roles_file).st_mtime_ns == stamp                                 # kept as it was
    manifest = json.loads(open(os.path.join(folder, snapshot.MANIFEST), encoding="utf-8").read())
    assert manifest["types"]["roles"]["taken"] == "2026-11-02T09:00:00+00:00"
    assert manifest["types"]["transforms"] == {"objects": 3, "taken": "2026-11-03T09:00:00+00:00",
                                               "endpoint": "GET /transforms/v1"}
    assert len(manifest["changes"]) == 4 and manifest["notes"] == ["Roles not read (HTTP 403)."]


def test_folders_that_are_not_a_snapshot_are_refused(tmp_path):
    found = snapshot.Snapshot("https://acme.api.identitynow.com", {"segments": []})
    other = tmp_path / "other"
    other.mkdir()
    (other / "notes.txt").write_text("mine", encoding="utf-8")
    with pytest.raises(UsageError, match="not empty and holds no snapshot"):
        snapshot.write(found, str(other))
    repository = tmp_path / "repo"
    (repository / ".git").mkdir(parents=True)                                       # a new git repository
    snapshot.write(found, str(repository))
    with pytest.raises(UsageError, match="holds a snapshot of https://acme.api.identitynow.com, not of"):
        snapshot.write(snapshot.Snapshot("https://acme-sb.api.identitynow.com", {}), str(repository))
    with pytest.raises(UsageError, match="is a file"):
        snapshot.write(found, str(other / "notes.txt"))


def test_file_names():
    assert snapshot.file_name('HR: "dept" / code?') == "HR_ _dept_ _ code_"
    assert snapshot.file_name("CON") == "_CON" and snapshot.file_name("aux.old") == "_aux.old"
    assert snapshot.file_name(" trailing. ") == "trailing" and snapshot.file_name("...") == "_"
    assert snapshot.file_names([{"id": "2c9180845d", "name": "Lower"}, {"id": "8a8f1c2e77", "name": "lower"},
                                {"name": "Upper"}, {"name": "Upper"}, {}]) == [
        "Lower (2c918084).json", "lower (8a8f1c2e).json", "Upper.json", "Upper 2.json", "unnamed.json"]
    assert snapshot.tenant_name("https://acme-sb.api.identitynow.com") == "acme-sb"


def test_command(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: tenant())
    folder = str(tmp_path / "snap")
    args = ["snapshot", "--only", "transforms", "sources", "-o", folder, "--log-dir", ""]
    assert cli.main(args) == 0
    assert sorted(os.listdir(folder)) == ["snapshot.json", "sources", "transforms"]
    assert "1 secret value(s) replaced by (redacted)" in capsys.readouterr().err
    assert cli.main(args) == 0
    assert "0 change(s) since the snapshot of" in capsys.readouterr().err
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: tenant(
        segments=FakeApi(list_segments_v1_without_preload_content=pages([], 403))))
    assert cli.main(["snapshot", "--only", "segments", "--output-dir", str(tmp_path), "--log-dir", ""]) == 2
    assert "nothing could be read" in capsys.readouterr().err
    assert not os.path.exists(tmp_path / "snapshot-tenant")


def test_reports_run_from_a_snapshot(tmp_path):
    folder = tmp_path / "demo"
    result = snapshot.write(demo.sample_snapshot(), str(folder), now=NOW)
    assert result.redacted == 1
    connector = json.loads((folder / "sources" / "Active Directory.json").read_text(encoding="utf-8"))
    assert connector["connectorAttributes"]["password"] == snapshot.REDACTED

    attribute = folder / "identity-attributes" / "department.json"
    attribute.write_text(attribute.read_text(encoding="utf-8").replace('"Department"', '"Service"'),
                         encoding="utf-8")
    assert identity_profiles.load_names([str(folder)])["department"] == "Service"
    out = tmp_path / "mappings"
    args = ["mappings", "--from", str(folder), "--format", "xlsx", "-o", str(out), "--log-dir", ""]
    assert cli.main(args) == 0
    book = load_workbook(str(out) + ".xlsx", read_only=True)
    states = list(book["Lifecycle states"].iter_rows(min_row=2, values_only=True))
    assert len(states) == 7 and {row[0] for row in states} == {"Employees", "Contractors"}
    assert "Service" in {row[1] for row in book["Employees"].iter_rows(min_row=2, values_only=True)}

    rule = folder / "connector-rules"                     # the demo has some: one more, without code
    rule.mkdir(exist_ok=True)
    (rule / "Build map.json").write_text(json.dumps({"id": "c", "name": "Build map", "type": "BuildMap",
                                                     "attributes": None, "sourceCode": {}}), encoding="utf-8")
    profiles, transforms, policies = transform_analysis.load_files([str(folder)])
    assert "Build map" not in transforms and len(policies) == 2 and len(profiles) == 2
    assert cli._source_from_files([str(folder)], "Active Directory").schema is None   # no schema exported
    schemas = [{"name": "account", "attributes": [{"name": "employeeID"}, {"name": "mail"}]}]
    exported = tmp_path / "hr.json"
    exported.write_text(json.dumps({"id": "n", "name": "New HR", "connector": "x", "schemas": schemas}),
                        encoding="utf-8")
    assert cli._source_from_files([str(exported)], "New HR").schema == ["employeeID", "mail"]
