"""Impact analysis of replacing the HR source."""

import json
import os

import pytest

from isc_reporter import cli
from isc_reporter.source_migration import NEW_SOURCE_ID, Source, analyse


def account(attribute, source="HR"):
    return {"type": "accountAttribute",
            "attributes": {"sourceName": source, "attributeName": attribute, "sourceId": source.lower() * 4}}


def ref(name, **attrs):
    return {"type": "reference", "attributes": {"id": name, **attrs}}


def mapping(name, definition):
    return {"identityAttributeName": name, "transformDefinition": definition}


def profile(name, source, *mappings, priority=10):
    return {"id": name.lower(), "name": name, "priority": priority, "identityCount": 5,
            "authoritativeSource": {"type": "SOURCE", "id": source.lower() * 4, "name": source},
            "identityAttributeConfig": {"enabled": True, "attributeTransforms": list(mappings)}}


PROFILES = [
    profile("IP_HR", "HR",
            mapping("uid", account("id")),
            mapping("email", account("mail", "AD")),
            mapping("endDate", ref("Format date", input=account("endDate"))),
            mapping("cloudLifecycleState", ref("LCS")),
            mapping("manager", account("manager"))),
    profile("IP_Contractors", "Contractors",
            mapping("uid", account("login", "Contractors")),
            mapping("department", account("department")), priority=20),
    profile("IP_Other", "Other", mapping("uid", account("id", "Other")), priority=30),
]
TRANSFORMS = {
    "Format date": {"id": "t1", "name": "Format date", "type": "dateFormat",
                    "attributes": {"outputFormat": "ISO8601"}},
    "HR status": {"id": "t2", "name": "HR status", "type": "lookup",
                  "attributes": {"table": {"A": "active", "default": "inactive"},
                                 "input": account("status")}},
    "LCS": {"id": "t3", "name": "LCS", "type": "firstValid",
            "attributes": {"values": [ref("HR status"), {"type": "static", "attributes": {"value": "x"}}]}},
    "Unused HR": {"id": "t4", "name": "Unused HR", "type": "upper", "attributes": {"input": account("x")}},
}
NEW = Source("Workday", "w" * 32, ["employeeId", "endDate", "manager", "status"])


@pytest.fixture
def plan():
    return analyse(PROFILES, TRANSFORMS, Source("HR"), NEW, {"id": "employeeId"})


def test_finds_impacted_profiles_and_transform_chain(plan):
    assert [(p["profile"]["name"], p["authoritative"], p["touched"]) for p in plan.profiles] == [
        ("IP_HR", True, ["uid", "endDate", "cloudLifecycleState", "manager"]),
        ("IP_Contractors", False, ["department"])]
    assert {n: i["impact"] for n, i in plan.transforms.items()} == {
        "HR status": "reads the old source", "Unused HR": "reads the old source",
        "LCS": "calls an impacted transform"}
    assert plan.transforms["LCS"]["used_by"] == ["IP_HR: cloudLifecycleState"]


def test_attribute_mapping_statuses(plan):
    status = {a: (i["new_attribute"], i["status"]) for a, i in plan.attributes.items()}
    assert status["id"] == ("employeeId", "from the mapping file")
    assert status["endDate"] == ("endDate", "same name in the new schema")
    assert status["status"] == ("status", "same name in the new schema")
    assert status["department"] == ("", "TO DEFINE")
    assert status["x"] == ("", "TO DEFINE")
    assert plan.attributes["department"]["used_by"] == ["IP_Contractors: department"]


def test_before_after_and_checks(plan):
    rows = {(m["profile"], m["identity_attribute"]): m for m in plan.mappings}
    assert rows[("IP_HR", "email")]["change"] == "unchanged"
    end = rows[("IP_HR", "endDate")]
    assert (end["before"], end["after"]) == ("Format date(HR.endDate)", "Format date(Workday.endDate)")
    assert rows[("IP_HR", "cloudLifecycleState")]["after"] == "LCS - Workday()"
    assert rows[("IP_Contractors", "department")]["change"] == "to complete"
    checks = {(c[1], c[2]) for c in plan.checks}
    assert ("department", "New attribute to define") in checks
    assert ("Unused HR", "Transform not used by a profile") in checks
    assert ("IP_HR", "Priority to choose") in checks


def test_drafts(plan):
    new_profile = plan.drafts["identity-profiles/IP_HR - Workday.json"]
    assert "id" not in new_profile and "identityCount" not in new_profile
    assert new_profile["authoritativeSource"] == {"type": "SOURCE", "id": "w" * 32, "name": "Workday"}
    uid = new_profile["identityAttributeConfig"]["attributeTransforms"][0]["transformDefinition"]
    assert uid["attributes"] == {"sourceName": "Workday", "attributeName": "employeeId", "sourceId": "w" * 32}
    # the original profile and transforms are left untouched
    original = PROFILES[0]["identityAttributeConfig"]["attributeTransforms"][0]
    assert original["transformDefinition"] == account("id")
    status = plan.drafts["transforms/HR status - Workday.json"]
    assert "id" not in status and status["attributes"]["input"]["attributes"]["sourceName"] == "Workday"
    lcs = plan.drafts["transforms/LCS - Workday.json"]
    assert lcs["attributes"]["values"][0]["attributes"]["id"] == "HR status - Workday"
    contractors = plan.drafts["identity-profiles/IP_Contractors (updated).json"]
    assert contractors["id"] == "ip_contractors"
    department = contractors["identityAttributeConfig"]["attributeTransforms"][1]["transformDefinition"]
    assert department["attributes"]["attributeName"] == "TODO_department"


def test_in_place_keeps_transform_names_and_ids():
    plan = analyse(PROFILES, TRANSFORMS, Source("HR"), Source("Workday"), in_place=True)
    lcs = plan.drafts["transforms/LCS.json"]
    assert lcs["id"] == "t3" and lcs["attributes"]["values"][0]["attributes"]["id"] == "HR status"
    status = plan.drafts["transforms/HR status.json"]["attributes"]["input"]["attributes"]
    assert status["sourceId"] == NEW_SOURCE_ID
    # without a schema for the new source, same names are assumed
    assert plan.attributes["department"]["status"] == "same name assumed (new schema unknown)"


def test_nothing_found_is_reported():
    plan = analyse(PROFILES, TRANSFORMS, Source("Nope"), NEW)
    assert not plan.profiles and ("", "Nothing found") in {(c[1], c[2]) for c in plan.checks}


def test_cli_offline(tmp_path):
    exports = tmp_path / "exports"
    exports.mkdir()
    (exports / "profiles.json").write_text(json.dumps(PROFILES), encoding="utf-8")
    (exports / "transforms.json").write_text(json.dumps(list(TRANSFORMS.values())), encoding="utf-8")
    attribute_map = tmp_path / "map.csv"
    attribute_map.write_text("old_attribute;new_attribute\nid;employeeId\ndepartment;\n", encoding="utf-8")
    out = tmp_path / "out"
    assert cli.main(["migration", "--old-source", "HR", "--new-source", "Workday",
                     "--from", str(exports), "--attribute-map", str(attribute_map), "-o", str(out),
                     "--log-dir", ""]) == 0
    assert sorted(os.listdir(out)) == ["attribute-mapping.csv", "identity-profiles",
                                       "source-migration.pdf", "source-migration.xlsx", "transforms"]
    template = (out / "attribute-mapping.csv").read_text(encoding="utf-8-sig").splitlines()
    assert template[0] == "old_attribute;new_attribute;status"
    assert "id;employeeId;from the mapping file" in template

    from openpyxl import load_workbook

    book = load_workbook(out / "source-migration.xlsx")
    assert book.sheetnames == ["Dashboard", "Next steps", "Profiles", "Attribute mapping", "Mappings",
                               "Transforms", "Checks", "Chart data", "Read me"]


def test_unknown_transforms_are_flagged():
    plan = analyse(PROFILES, {}, Source("HR"), NEW)
    flagged = {c[1] for c in plan.checks if c[2] == "Transform not analysed"}
    assert flagged == {"Format date", "LCS"}


def test_model_json_handles_dates_and_enums():
    from sailpoint.identity_profiles.models import IdentityProfile
    from sailpoint.sources.models.model_schema import ModelSchema

    from isc_reporter.api import model_json

    schema = ModelSchema.from_dict({"id": "s1", "name": "account", "nativeObjectType": "User",
                                    "created": "2024-03-17T09:31:59.493Z",
                                    "attributes": [{"name": "employeeId", "type": "STRING"}]})
    data = model_json(schema)
    assert data["created"].startswith("2024-03-17T09:31:59")
    assert (data["attributes"][0]["name"], data["attributes"][0]["type"]) == ("employeeId", "STRING")
    # The traceback case: a model with datetimes (some read-only, left out by the SDK).
    profile = IdentityProfile.from_dict({**PROFILES[0], "created": "2024-03-17T09:31:59.493Z",
                                         "modified": "2026-10-11T13:11:58.273Z"})
    assert model_json(profile)["name"] == "IP_HR"


def test_cli_against_tenant(tmp_path, monkeypatch):
    from sailpoint.identity_profiles.models import IdentityProfile
    from sailpoint.sources.models.model_schema import ModelSchema
    from sailpoint.transforms.models import Transform

    from conftest import FakeApi, FakeIsc

    dated = {"created": "2024-03-17T09:31:59.493Z", "modified": "2026-10-11T13:11:58.273Z"}
    profiles = [IdentityProfile.from_dict({**p, **dated}) for p in PROFILES]
    transforms = [Transform.from_dict(t) for t in TRANSFORMS.values()]
    sources = {"HR": "h" * 32, "Workday": "w" * 32}
    schemas = {"h" * 32: ["id", "endDate", "manager", "status", "department", "x"],
               "w" * 32: ["employeeId", "endDate", "manager", "status"]}

    def list_sources_v1(filters, limit):
        name = filters.split('"')[1]
        return [type("Source", (), {"id": sources[name], "name": name})()] if name in sources else []

    def get_source_schemas_v1(source_id, include_types):
        return [ModelSchema.from_dict({"name": "account", "nativeObjectType": "User", **dated,
                                       "attributes": [{"name": n, "type": "STRING"}
                                                      for n in schemas[source_id]]})]

    def first_page(items):
        return lambda limit, offset: items if not offset else []

    isc = FakeIsc(
        identity_profiles=FakeApi(list_identity_profiles_v1=first_page(profiles)),
        transforms=FakeApi(list_transforms_v1=first_page(transforms)),
        identity_attributes=FakeApi(list_identity_attributes_v1=lambda **kw: []),
        sources=FakeApi(list_sources_v1=list_sources_v1, get_source_schemas_v1=get_source_schemas_v1))
    monkeypatch.setattr("isc_reporter.client.connect", lambda config=None: isc)
    out = tmp_path / "out"
    assert cli.main(["migration", "--old-source", "HR", "--new-source", "Workday", "--no-pdf",
                     "-o", str(out), "--log-dir", ""]) == 0
    template = (out / "attribute-mapping.csv").read_text(encoding="utf-8-sig")
    assert "endDate;endDate;same name in the new schema" in template
    assert "id;;TO DEFINE" in template
    assert isc.sources.called("get_source_schemas_v1")[0]["include_types"] == "account"
    assert not (out / "source-migration.pdf").exists()
