"""Everything that depends on a source, besides profiles and transforms: what replacing it means."""

import json
import os

import pytest
from openpyxl import load_workbook

from conftest import FakeApi, FakeIsc, Raw
from isc_reporter import cli, criteria, source_usage
from isc_reporter.source_migration import TO_CHOOSE, Source, analyse

OLD_ID, SAP_ID = "a" * 32, "s" * 32


def account(attribute, source="AD"):
    return {"type": "accountAttribute", "attributes": {"sourceName": source, "attributeName": attribute}}


def identity(name):
    return {"type": "identityAttribute", "attributes": {"name": name}}


def ref(kind, ref_id, name):
    return {"type": kind, "id": ref_id, "name": name}


AD_REF = {"id": OLD_ID, "name": "AD"}
AD = {"id": OLD_ID, "name": "AD", "connectorName": "Active Directory", "authoritative": False,
      "owner": ref("IDENTITY", "o1", "Ada Owner"), "cluster": ref("CLUSTER", "c1", "Paris"),
      "features": ["PROVISIONING", "ENABLE", "PASSWORD"], "deleteThreshold": 20,
      "beforeProvisioningRule": ref("RULE", "r1", "AD Before Provisioning"),
      "connectorAttributes": {"password": "secret-value", "enableRules": "true",
                              "connectionParameters": [{"beforeRule": "AD Web Before"}]},
      "correlationConfig": {"attributeAssignments": [{"property": "uid", "value": "sAMAccountName"}]},
      "provisioningPolicies": [{"name": "Create", "usageType": "CREATE",
                                "fields": [{"name": "mail", "transform": identity("email")}]}],
      "schemas": [{"name": "account",
                   "attributes": [{"name": n} for n in ("sAMAccountName", "mail", "title")]}]}
SAP = {"id": SAP_ID, "name": "SAP", "connector": "sap", "provisioningPolicies": [{
    "name": "Account", "usageType": "CREATE", "fields": [
        {"name": "login", "transform": account("sAMAccountName")},              # reads the old source
        {"name": "mail", "transform": {"type": "reference", "attributes": {"id": "AD mail"}}},
        {"name": "email", "transform": identity("email")},                      # computed from the old source
        {"name": "dept", "transform": identity("department")}]}]}               # not impacted
HR = {"id": "h" * 32, "name": "HR", "connector": "delimited-file", "authoritative": True,
      "provisioningPolicies": []}
PROFILES = [{
    "id": "p1", "name": "IP_Employees", "priority": 10,
    "authoritativeSource": {"type": "SOURCE", "id": "h" * 32, "name": "HR"},
    "identityAttributeConfig": {"attributeTransforms": [
        {"identityAttributeName": "uid", "transformDefinition": account("id", "HR")},
        {"identityAttributeName": "email", "transformDefinition": account("mail")},
        {"identityAttributeName": "department", "transformDefinition": account("dept", "HR")}]},
    "lifecycleStates": [
        {"name": "Active", "technicalName": "active", "enabled": True, "accessProfileIds": ["ap1"],
         "accountActions": [{"action": "ENABLE", "sourceIds": [OLD_ID, SAP_ID]}]},
        {"name": "Leaver", "technicalName": "terminated", "enabled": True,
         "accountActions": [{"action": "DISABLE", "allSources": True, "excludeSourceIds": [OLD_ID]}]}]}]
TRANSFORMS = {"AD mail": {"id": "t1", "name": "AD mail", "type": "lower",
                          "attributes": {"input": account("mail")}}}
ENTITLEMENTS = [{"id": "e1", "name": "Staff", "source": AD_REF},
                {"id": "e2", "name": "VPN", "source": AD_REF},
                {"id": "e3", "name": "Domain Admins", "privileged": True, "source": AD_REF}]
STAFF = {"id": "ap1", "name": "AD - Staff", "enabled": True, "requestable": True, "segments": ["g1"],
         "source": ref("SOURCE", OLD_ID, "AD"),
         "entitlements": [ref("ENTITLEMENT", "e1", "Staff"), ref("ENTITLEMENT", "e2", "VPN")]}
SAP_USER = {"id": "ap2", "name": "SAP - User", "source": ref("SOURCE", SAP_ID, "SAP"), "entitlements": []}
STAFF_REF = ref("ACCESS_PROFILE", "ap1", "AD - Staff")
ROLES = [
    {"id": "r1", "name": "Employee", "enabled": True, "membership": None, "accessProfiles": [STAFF_REF],
     "entitlements": [ref("ENTITLEMENT", "e3", "Domain Admins")]},
    {"id": "r2", "name": "Dynamic", "enabled": True, "membership": None, "dimensional": True,
     "dimensions": [{"id": "d1", "name": "Paris", "parentId": "r2", "accessProfiles": [STAFF_REF]}]},
    {"id": "r3", "name": "SAP users", "enabled": True, "membership": None,
     "accessProfiles": [ref("ACCESS_PROFILE", "ap2", "SAP - User")]},
]
SOD = [
    {"id": "sod1", "name": "Admin vs audit", "state": "ENFORCED", "conflictingAccessCriteria": {
        "leftCriteria": {"name": "Admins", "criteriaList": [ref("ENTITLEMENT", "e3", "Domain Admins")]},
        "rightCriteria": {"name": "Audit", "criteriaList": [ref("ENTITLEMENT", "x9", "Auditor")]}}},
    {"id": "sod2", "name": "Query policy", "state": "NOT_ENFORCED", "type": "GENERAL",
     "policyQuery": '@access(source.name:"AD")'},
]
WORKFLOWS = [
    {"id": "w1", "name": "On AD aggregation", "enabled": True,
     "trigger": {"type": "EVENT", "attributes": {"id": "idn:account-aggregation-completed",
                                                 "filter.$": f"$[?(@.source.id == '{OLD_ID}')]"}},
     "definition": {"start": "Get", "steps": {"Get": {"actionId": "sp:get-accounts",
                                                      "attributes": {"sourceName": "AD"}}}}},
    {"id": "w2", "name": "Welcome", "enabled": False,
     "trigger": {"type": "EVENT", "attributes": {"id": "idn:identity-created"}},
     "definition": {"steps": {"Mail": {"attributes": {"subject": "Hi ADmin"}}}}},
]
RULES = [
    {"id": "r1", "name": "AD Before Provisioning", "type": "BeforeProvisioning",
     "sourceCode": {"script": "x"}},
    {"id": "r2", "name": "AD Web Before", "type": "WebServiceBeforeOperationRule",
     "sourceCode": {"script": "y"}},
    {"id": "r3", "name": "Generic", "type": "BuildMap",
     "sourceCode": {"script": 'Application app = context.getObjectByName(Application.class, "AD");'}},
]
PASSWORDS = [{"id": "pp1", "name": "Default", "sourceIds": [OLD_ID, SAP_ID], "minLength": 8},
             {"id": "pp2", "name": "SAP only", "sourceIds": [SAP_ID], "minLength": 12}]


def objects():
    return criteria.Objects(ROLES, [{"id": "g1", "name": "Staff"}], [AD, SAP, HR],
                            access_profiles=[STAFF, SAP_USER], sod_policies=SOD, workflows=WORKFLOWS,
                            rules=RULES, password_policies=PASSWORDS, entitlements=ENTITLEMENTS)


@pytest.fixture
def plan():
    """AD is to be replaced, by a source not chosen yet."""
    return analyse(PROFILES, TRANSFORMS, Source("AD"), objects=objects())


def found(plan):
    return {(r["Type"], r["Object"], r["Where"]): r for r in plan.criteria}


def test_every_object_that_depends_on_the_old_source(plan):
    rows = found(plan)
    assert set(rows) == {
        ("Role", "Employee", "access profiles"), ("Role", "Employee", "entitlements"),
        ("Role dimension", "Dynamic / Paris", "access profiles"),
        ("Access profile", "AD - Staff", "source and entitlements"),
        ("SoD policy", "Admin vs audit", "left side (Admins)"), ("SoD policy", "Query policy", "query"),
        ("Lifecycle state", "IP_Employees / Active", "Enable accounts"),
        ("Lifecycle state", "IP_Employees / Active", "access profiles granted"),
        ("Lifecycle state", "IP_Employees / Leaver", "Disable accounts of all sources but AD"),
        ("Account correlation", "AD", "uid = sAMAccountName"),
        ("Provisioning policy", "SAP", "CREATE policy, field 'login'"),
        ("Provisioning policy", "SAP", "CREATE policy, field 'mail'"),
        ("Provisioning policy", "SAP", "CREATE policy, field 'email'"),
        ("Workflow", "On AD aggregation", "trigger, step 'Get'"),
        ("Connector rule", "AD Before Provisioning", "run by AD (beforeProvisioningRule)"),
        ("Connector rule", "AD Web Before", "run by AD (beforeRule)"),
        ("Connector rule", "Generic", "code"),
        ("Password policy", "Default", "sources of the policy"),
    }
    staff = rows[("Access profile", "AD - Staff", "source and entitlements")]
    assert (staff["Severity"], staff["Uses"], staff["Definition"]) == (
        "Warning", "2 entitlement(s) of AD", "Staff\nVPN")
    assert staff["Action"] == (
        "Recreate it on the new source with the matching entitlements, then grant it instead in: role "
        "Employee; role dimension Dynamic / Paris; lifecycle state IP_Employees / Active; assign it to the "
        "segments Staff; make it requestable with the same approvals.")
    admins = rows[("Role", "Employee", "entitlements")]
    assert (admins["Uses"], admins["Definition"]) == (
        "AD entitlement 'Domain Admins'", "Access profiles: AD - Staff\nEntitlements: Domain Admins")
    sod = rows[("SoD policy", "Admin vs audit", "left side (Admins)")]
    assert (sod["Severity"], sod["Enabled"], sod["Definition"]) == (
        "Warning", "yes", "Admins: Domain Admins versus Audit: Auditor")
    assert rows[("SoD policy", "Query policy", "query")]["Severity"] == "Info"            # not enforced
    enable = rows[("Lifecycle state", "IP_Employees / Active", "Enable accounts")]
    assert enable["Action"] == (
        "Replace AD by the new source in this action, if the new source can enable its accounts.")
    assert enable["Definition"] == "Enable: AD, SAP\nGrant: AD - Staff"
    leaver = rows[("Lifecycle state", "IP_Employees / Leaver", "Disable accounts of all sources but AD")]
    assert leaver["Severity"] == "Info"
    login = rows[("Provisioning policy", "SAP", "CREATE policy, field 'login'")]
    assert (login["Severity"], login["Uses"]) == ("Warning", "AD account attribute 'sAMAccountName'")
    assert login["Action"] == (
        "Rewrite on the new source once 'sAMAccountName' has a counterpart in attribute-mapping.csv.")
    mail = rows[("Provisioning policy", "SAP", "CREATE policy, field 'mail'")]
    assert mail["Uses"] == "transform 'AD mail' (reads AD)"
    email = rows[("Provisioning policy", "SAP", "CREATE policy, field 'email'")]
    assert (email["Severity"], email["Uses"]) == ("Info", "identity attribute 'email'")
    assert rows[("Connector rule", "Generic", "code")]["Severity"] == "Warning"
    # the connection settings of the source are never shown
    assert "secret-value" not in json.dumps([plan.criteria, plan.source])


def test_not_chosen_new_source_gives_no_drafts(plan):
    assert not plan.chosen and plan.drafts == {}
    assert plan.attributes["sAMAccountName"] == {
        "used_by": ["provisioning policy: SAP, CREATE policy, field 'login'"], "new_attribute": "",
        "status": TO_CHOOSE}
    assert "provisioning policy: SAP, CREATE policy, field 'mail'" in plan.transforms["AD mail"]["used_by"]
    checks = {c[2] for c in plan.checks}
    assert "New source not chosen" in checks
    assert not checks & {"New attribute to define", "New schema unknown", "New source id unknown",
                         "Transform not used by a profile"}
    assert all("the new source" in r["Action"] or r["Action"].startswith("Compare") for r in plan.criteria)

    from isc_reporter.source_migration import build_dashboard, steps

    report = build_dashboard(plan)
    assert report.title == "Source migration: AD (new source not chosen)"
    assert ("New source", "not chosen") in [(label, value) for label, value, _ in report.kpis]
    assert steps(plan)[0].startswith("Choose the new source")


def test_overview_identity_attributes_and_the_old_source(plan):
    from isc_reporter.source_migration import attribute_rows, overview_rows

    overview = {r["Object type"]: (r["To change"], r["Warnings"]) for r in overview_rows(plan)}
    assert overview["Identity profile"] == (1, 0) and overview["Identity attribute"] == (1, "")
    assert overview["Access profile"] == (1, 1) and overview["Provisioning policy"] == (1, 1)
    assert overview["Segment"] == (0, 0) and overview["Manager correlation"] == (0, 0)
    assert attribute_rows(plan) == [{
        "Identity attribute": "email", "Used by": 1, "Objects": "Provisioning policy: SAP",
        "Why its value may change": "computed from the old source (IP_Employees)"}]
    settings = {r["Setting"]: (r["Old source"], r["On the new source"]) for r in plan.source}
    assert settings["Owner"] == ("Ada Owner", "The owner of the new source.")
    assert settings["Rules"][0] == (
        "AD Before Provisioning (beforeProvisioningRule)\nAD Web Before (beforeRule)")
    assert settings["Provisioning policies"][0] == "CREATE: Create (1 field(s))"
    assert settings["Account schema"][0] == "3 attribute(s), 2 used by the configuration"
    assert settings["Entitlements"][0] == "3 (1 privileged)"
    assert settings["Password policies"][0] == "Default"
    assert settings["Account correlation"][0] == "uid = sAMAccountName"
    assert settings["Manager correlation"] == ("none", "")


def test_chosen_new_source_names_it_and_writes_the_drafts():
    new = Source("Entra ID", "n" * 32, ["onPremisesSamAccountName", "mail"])
    plan = analyse(PROFILES, TRANSFORMS, Source("AD"), new, {"sAMAccountName": "onPremisesSamAccountName"},
                   objects=objects())
    rows = found(plan)
    assert rows[("Provisioning policy", "SAP", "CREATE policy, field 'login'")]["Action"] == (
        "Rewrite on Entra ID: Entra ID.onPremisesSamAccountName")
    assert rows[("Access profile", "AD - Staff", "source and entitlements")]["Action"].startswith(
        "Recreate it on Entra ID")
    assert sorted(plan.drafts) == ["identity-profiles/IP_Employees (updated).json",
                                   "transforms/AD mail - Entra ID.json"]
    assert "lifecycleStates" not in plan.drafts["identity-profiles/IP_Employees (updated).json"]


def test_a_source_that_does_not_exist_is_reported():
    plan = analyse(PROFILES, TRANSFORMS, Source("ADX"), objects=objects())
    checks = {(c[1], c[2]) for c in plan.checks}
    assert ("ADX", "Old source not found") in checks and ("", "Nothing found") in checks
    assert plan.source == [{"Setting": "Source", "Old source": "ADX: not found",
                            "On the new source": "Check the name of the old source."}]


def pages(items, status=200):
    return lambda limit=None, offset=0, **kwargs: Raw(items if not offset else [], status)


def test_fetch_reads_every_type_and_notes_what_fails():
    isc = FakeIsc(
        access_profiles=FakeApi(list_access_profiles_v1_without_preload_content=pages([STAFF])),
        sod_policies=FakeApi(list_sod_policies_v1_without_preload_content=pages(SOD)),
        workflows=FakeApi(list_workflows_v1_without_preload_content=lambda **kw: Raw(
            {"messages": [{"text": "Forbidden"}]}, 403)),
        connector_rules=FakeApi(get_connector_rule_list_v1_without_preload_content=pages(RULES)),
        password_policies=FakeApi(list_password_policies_v1_without_preload_content=pages(PASSWORDS)),
        lifecycle_states=FakeApi(get_lifecycle_states_v1_without_preload_content=pages(
            PROFILES[0]["lifecycleStates"])),
        sources=FakeApi(list_provisioning_policies_v1_without_preload_content=pages(
            SAP["provisioningPolicies"])),
        search=FakeApi(search_post_v1=lambda search, limit: ENTITLEMENTS))
    read = criteria.Objects(sources=[{"id": SAP_ID, "name": "SAP"}])
    profiles = [{k: v for k, v in PROFILES[0].items() if k != "lifecycleStates"}]
    source_usage.fetch(isc, read, profiles, Source("AD", OLD_ID))
    assert (read.access_profiles, read.sod_policies, read.rules) == ([STAFF], SOD, RULES)
    assert read.workflows is None and read.notes == ["Workflows not read (HTTP 403: Forbidden)."]
    assert read.entitlements == ENTITLEMENTS
    assert isc.search.called("search_post_v1")[0]["search"].query.query == f'source.id:"{OLD_ID}"'
    assert profiles[0]["lifecycleStates"] == PROFILES[0]["lifecycleStates"]
    assert read.sources[0]["provisioningPolicies"] == SAP["provisioningPolicies"]
    assert isc.password_policies.called("list_password_policies_v1_without_preload_content")[0][
        "x_sail_point_experimental"] == "true"


def test_load_files_from_a_snapshot(tmp_path):
    for folder, items in (("sources", [AD, SAP]), ("access-profiles", [STAFF]), ("sod-policies", SOD),
                          ("workflows", WORKFLOWS), ("connector-rules", RULES), ("roles", ROLES)):
        os.makedirs(tmp_path / folder)
        for number, item in enumerate(items):
            (tmp_path / folder / f"{number}.json").write_text(json.dumps(item), encoding="utf-8")
    exported = criteria.load_files([str(tmp_path)])
    source_usage.load_files([str(tmp_path)], exported)
    assert [r["name"] for r in exported.rules] == ["AD Before Provisioning", "AD Web Before", "Generic"]
    assert [w["name"] for w in exported.workflows] == ["On AD aggregation", "Welcome"]
    assert [p["name"] for p in exported.sod_policies] == ["Admin vs audit", "Query policy"]
    assert [p["name"] for p in exported.access_profiles] == ["AD - Staff"]
    assert exported.password_policies is None and exported.entitlements is None
    assert "Password policies not in the exported files: not analysed." in exported.notes
    plan = analyse(PROFILES, TRANSFORMS, Source("AD"), objects=exported)
    rows = found(plan)
    # without the entitlements of the source, those of its access profiles are still recognised
    assert ("Role", "Employee", "access profiles") in rows
    assert ("Role", "Employee", "entitlements") not in rows
    assert plan.analysed["Password policy"] is False and plan.analysed["Workflow"] is True


def test_the_demo_shows_every_type_of_object(tmp_path):
    from isc_reporter import demo, snapshot

    profiles, transforms = demo.sample_profiles()
    states = demo.sample_lifecycle().states
    staged = [{**profile, "lifecycleStates": states.get(profile["id"], [])} for profile in profiles]
    plan = analyse(staged, transforms, Source("HR System"), objects=demo.sample_criteria())
    assert {r["Type"] for r in plan.criteria} == set(source_usage.TYPE_ORDER)
    assert all(plan.analysed.values())
    # the same from a snapshot of the demo, which holds every type
    folder, out = tmp_path / "snapshot", tmp_path / "out"
    snapshot.write(demo.sample_snapshot(), str(folder))
    assert cli.main(["migration", "--old-source", "HR System", "--from", str(folder), "--no-pdf",
                     "-o", str(out), "--log-dir", ""]) == 0
    overview = load_workbook(out / "source-migration.xlsx")["Overview"]
    assert not [row[0] for row in overview.iter_rows(min_row=2, values_only=True) if row[1] == "not analysed"]


def test_cli_without_new_source(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    exported = [*PROFILES, *TRANSFORMS.values(), AD, SAP, HR, STAFF, *ROLES, *SOD, *WORKFLOWS, *RULES,
                *PASSWORDS]
    (exports / "config.json").write_text(json.dumps(exported), encoding="utf-8")
    out = tmp_path / "out"
    assert cli.main(["migration", "--old-source", "AD", "--from", str(exports), "-o", str(out),
                     "--log-dir", ""]) == 0
    assert sorted(os.listdir(out)) == ["attribute-mapping.csv", "source-migration.pdf",
                                       "source-migration.xlsx"]
    book = load_workbook(out / "source-migration.xlsx")
    impacts = [(row[1], row[2]) for row in book["Impacts"].iter_rows(min_row=2, values_only=True)]
    assert ("Workflow", "On AD aggregation") in impacts and ("Password policy", "Default") in impacts
    settings = {row[0]: row[1] for row in book["Old source"].iter_rows(min_row=2, values_only=True)}
    assert settings["Connector"] == "Active Directory"
    mapping = (out / "attribute-mapping.csv").read_text(encoding="utf-8-sig")
    assert "sAMAccountName;;to define once the new source is chosen" in mapping
