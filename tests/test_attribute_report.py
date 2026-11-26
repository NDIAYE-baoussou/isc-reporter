"""Detailed report on identity attributes: definition, uses and the quality of their values."""

import csv

import pytest
from openpyxl import load_workbook

from conftest import FakeApi, FakeIsc, Raw
from isc_reporter import attribute_report as report
from isc_reporter import cli, criteria
from isc_reporter.errors import UsageError

DEFINITIONS = [
    {"name": "department", "displayName": "Department", "type": "string", "searchable": True},
    {"name": "uid", "displayName": "Username", "standard": True, "system": True},
    {"name": "email", "displayName": "Email", "standard": True},
    {"name": "startDate", "displayName": "Start date"},
    {"name": "cloudLifecycleState", "displayName": "Lifecycle state", "system": True},
    {"name": "manager", "displayName": "Manager", "type": "sailpoint.object.Identity"},
    {"name": "costCenter", "displayName": "Cost center"},
]


def account(attribute, source="HR"):
    return {"type": "accountAttribute", "attributes": {"sourceName": source, "attributeName": attribute}}


def identity(name):
    return {"type": "identityAttribute", "attributes": {"name": name}}


def mapping(name, definition):
    return {"identityAttributeName": name, "transformDefinition": definition}


PROFILES = [
    {"id": "p-emp", "name": "Employees", "authoritativeSource": {"id": "hr", "name": "HR"},
     "identityAttributeConfig": {"attributeTransforms": [
         mapping("uid", account("id")), mapping("email", account("mail")),
         mapping("department", account("dept")),
         mapping("startDate", account("hireDate")), mapping("manager", account("managerId")),
         mapping("cloudLifecycleState", {"type": "reference", "attributes": {"id": "Lifecycle"}}),
         mapping("title", {"type": "concat",
                           "attributes": {"values": [identity("department"), " analyst"]}})]}},
    {"id": "p-con", "name": "Contractors", "authoritativeSource": {"id": "ext", "name": "Register"},
     "identityAttributeConfig": {"attributeTransforms": [
         mapping("uid", account("login", "Register")), mapping("email", account("email", "Register"))]}},
]
TRANSFORMS = {
    "Lifecycle": {"name": "Lifecycle", "type": "static", "attributes": {"value": "active"}},
    "Department code": {"name": "Department code", "type": "lookup",
                        "attributes": {"input": identity("department"),
                                       "table": {"Finance": "FIN", "default": "X"}}},
}
POLICIES = [{"source": "AD", "usageType": "CREATE", "name": "Account",
             "fields": [{"name": "description", "transform": identity("department")}]}]


def leaf(attribute, value):
    return {"operation": "EQUALS", "key": {"type": "IDENTITY", "property": f"attribute.{attribute}"},
            "stringValue": value}


ROLES = [{"id": "r1", "name": "Finance", "enabled": True, "dimensional": True,
          "membership": {"type": "STANDARD", "criteria": {"operation": "AND", "children": [
              leaf("department", "Finance"), leaf("cloudLifecycleState", "active")]}}}]
DIMENSIONS = [{"name": "Paris", "membership": {"criteria": leaf("department", "Finance")}}]
SEGMENTS = [{"name": "Finance apps", "active": True, "visibilityCriteria": {"expression": {
    "operator": "AND", "children": [{"operator": "EQUALS", "attribute": "department",
                                     "value": {"type": "STRING", "value": "Finance"}}]}}}]
SOURCES = [{"id": "ad", "name": "AD",
            "correlationConfig": {"attributeAssignments": [{"property": "email", "value": "mail"}]},
            "managerCorrelationMapping": {"accountAttributeName": "manager", "identityAttributeName": "uid"}}]
WORKFLOWS = [
    {"name": "Department change", "enabled": True, "definition": {"steps": {}},
     "trigger": {"attributes": {"filter.$": "$.changes[?(@.attribute == 'department')]"}}},
    {"name": "Welcome", "enabled": False, "trigger": {},
     "definition": {"steps": {"Send": {"attributes": {"body": "Hello $.getIdentity.attributes.email"}}}}},
]
LIFECYCLE = {"p-emp": {"active": {"technicalName": "active"}, "inactive": {"technicalName": "inactive"}}}


def doc(name, profile, manager="boss", **attributes):
    found = {"id": name, "name": name, "displayName": name.title(),
             "identityProfile": {"id": f"p-{profile[:3].lower()}", "name": profile},
             "attributes": {k: v for k, v in attributes.items() if v is not None}}
    if manager:
        found["manager"] = {"id": manager, "name": manager}
    return found


def people():
    departments = ["Finance"] * 20 + ["Sales"] * 7 + ["finance ", "N/A", "Finanace", None]
    found = []
    for n, department in enumerate(departments):
        email = "dup@example.com" if n in (1, 2) else "bob@" if n == 3 else f"e{n}@example.com"
        start = "soon" if n == 4 else "31/01/2024" if n in (5, 6) else f"2024-0{1 + n % 9}-15"
        state = "ghost" if n == 7 else "inactive" if n % 5 == 0 else "active"
        found.append(doc(f"e{n}", "Employees", manager=None if n in (8, 9) else "boss", department=department,
                         email=email, startDate=start, cloudLifecycleState=state))
    found += [doc(f"c{n}", "Contractors", manager=None, email=f"c{n}@example.com") for n in range(3)]
    return found


ALL = ["department", "email", "startDate", "cloudLifecycleState", "manager", "costCenter", "uid"]


def data(names=ALL):
    objects = criteria.Objects([{**ROLES[0], "dimensions": DIMENSIONS}], SEGMENTS, SOURCES,
                               workflows=WORKFLOWS)
    return report.AttributeData(names, {d["name"]: d for d in DEFINITIONS}, PROFILES, TRANSFORMS, POLICIES,
                                objects, people(), LIFECYCLE, origin="https://tenant.api.identitynow.com")


def checks(result, name):
    return {row["Check"] for row in result.quality if row["Attribute"] == name}


def test_names_are_resolved_by_technical_or_display_name():
    definitions = {d["name"]: d for d in DEFINITIONS}
    mapped = report.mapped_attributes(PROFILES)
    assert report.resolve(["Department", "EMAIL", "uid", "Start date"], definitions, mapped) == [
        "department", "email", "uid", "startDate"]
    assert report.resolve(["title"], definitions, mapped) == ["title"]           # mapped, no definition
    with pytest.raises(UsageError, match="did you mean department"):
        report.resolve(["departmnt"], definitions, mapped)


def test_everything_that_uses_an_attribute():
    result = report.analyse(data())
    uses = {}
    for row in result.usages:
        uses.setdefault(row["Attribute"], set()).add(row["Type"])
    assert uses["department"] == {"Identity profile", "Transform", "Provisioning policy", criteria.ROLE,
                                  criteria.DIMENSION, criteria.SEGMENT, report.WORKFLOW}
    assert uses["email"] == {criteria.CORRELATION, report.WORKFLOW}
    assert uses["uid"] == {criteria.MANAGER}
    assert uses["cloudLifecycleState"] == {criteria.ROLE}
    role = next(r for r in result.usages if r["Attribute"] == "department" and r["Type"] == criteria.ROLE)
    assert role["Where"] == 'identity.department EQUALS "Finance"'
    assert "cloudLifecycleState" in role["Definition"]
    flow = next(r for r in result.usages if r["Attribute"] == "email" and r["Type"] == report.WORKFLOW)
    assert (flow["Object"], flow["Enabled"], flow["Where"]) == ("Welcome", "no", "definition")


def test_definitions_profile_by_profile():
    result = report.analyse(data(["department"]))
    rows = {row["Identity profile"]: row for row in result.mappings}
    assert rows["Employees"]["Explanation"] == "Copied from the account attribute 'dept' of the source HR."
    assert rows["Employees"]["Tree"] and rows["Contractors"]["Mapping"] == "Not mapped"
    summary = result.summary[0]
    assert (summary["Mapped in"], summary["Identities"], summary["Filled"], summary["Distinct values"]) == (
        "1 / 2", 34, 30, 5)
    assert summary["Searchable"] == "yes" and summary["Top value"] == "Finance (20)"


def test_data_quality_checks():
    result = report.analyse(data())
    assert checks(result, "department") == {"Empty although mapped", "Not mapped in their profile",
                                            "Extra spaces", "Variants of the same value",
                                            "Placeholder values", "Rare values"}
    variants = next(r for r in result.quality if r["Check"] == "Variants of the same value")
    assert "'Finance' (20) / 'finance ' (1)" in variants["Details"]
    assert checks(result, "email") == {"Duplicates", "Invalid e-mail addresses"}
    assert checks(result, "startDate") >= {"Dates that cannot be read", "Several date formats", "Date range"}
    assert "Rare values" not in checks(result, "startDate")                       # a date is not a typo
    unknown = next(r for r in result.quality if r["Check"] == "Not a lifecycle state")
    assert unknown["Identities"] == 1 and "'ghost (Employees)'" in unknown["Details"]
    empty = next(r for r in result.quality
                 if r["Attribute"] == "manager" and r["Check"] == "Empty although mapped")
    assert empty["Identities"] == 2                                     # managers read outside "attributes"
    assert checks(result, "costCenter") == {"Not mapped in their profile", "No value"}
    assert [(r["Value"], r["Identities"]) for r in result.duplicates] == [("dup@example.com", 2)]
    by_profile = {(r["Attribute"], r["Identity profile"]): r for r in result.profiles}
    assert by_profile[("department", "Contractors")]["Mapped"] == "no"
    assert by_profile[("department", "Employees")]["Empty"] == 1


def test_reports(tmp_path):
    result = report.analyse(data(), top=3)
    xlsx, pdf, out = (str(tmp_path / f"a.{ext}") for ext in ("xlsx", "pdf", "csv"))
    report.write_xlsx(xlsx, result)
    report.write_pdf(pdf, result)
    report.write_csv(out, result)
    book = load_workbook(xlsx)
    assert book.sheetnames == ["Dashboard", "Attributes", "Mappings", "Used by", "Quality", "Values",
                               "By profile", "Duplicates", "Settings", "Chart data", "Read me"]
    shown = [r for r in book["Values"].iter_rows(min_row=2, values_only=True) if r[0] == "department"]
    assert len(shown) == 3                                               # --top
    assert (tmp_path / "a.pdf").read_bytes()[:4] == b"%PDF"
    with open(out, encoding="utf-8-sig", newline="") as fh:
        rows = list(csv.reader(fh, delimiter=";"))
    assert rows[0] == ["Attribute", "Value", "Identities", "Share", "Identity profiles"]
    assert sum(1 for r in rows if r[0] == "department") == 5            # every value


class Obj(dict):
    """A dict that also reads as an SDK model (source.id)."""

    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name) from None


def fake_isc():
    def one_page(items):
        return lambda limit, offset, **kwargs: [] if offset else items

    return FakeIsc(
        identity_attributes=FakeApi(list_identity_attributes_v1=lambda include_system: DEFINITIONS),
        identity_profiles=FakeApi(list_identity_profiles_v1=one_page(PROFILES)),
        transforms=FakeApi(list_transforms_v1=one_page(list(TRANSFORMS.values()))),
        sources=FakeApi(list_sources_v1=one_page([Obj(SOURCES[0])]),
                        list_provisioning_policies_v1=one_page(POLICIES),
                        get_correlation_config_v1=lambda id: SOURCES[0]["correlationConfig"]),
        roles=FakeApi(list_roles_v1=one_page(ROLES)),
        dimensions=FakeApi(list_dimensions_v1=one_page(DIMENSIONS)),
        segments=FakeApi(list_segments_v1=one_page(SEGMENTS)),
        workflows=FakeApi(list_workflows_v1_without_preload_content=lambda **kwargs: Raw(WORKFLOWS)),
        lifecycle_states=FakeApi(get_lifecycle_states_v1=lambda identity_profile_id, limit, offset:
                                 [] if offset else list(LIFECYCLE.get(identity_profile_id, {}).values())),
        search=FakeApi(search_post_v1=lambda search, limit: people()[:limit]))


def test_cli(tmp_path, monkeypatch):
    isc = fake_isc()
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: isc)
    out = tmp_path / "attr"
    assert cli.main(["attribute", "Department", "cloudLifecycleState", "--format", "xlsx", "csv",
                     "-o", str(out), "--log-dir", ""]) == 0
    search = isc.search.called("search_post_v1")[0]["search"]
    assert "attributes" in search.query_result_filter.includes                  # only the fields needed
    book = load_workbook(str(out) + ".xlsx")
    summary = {r[0]: r for r in book["Attributes"].iter_rows(min_row=2, values_only=True)}
    assert set(summary) == {"department", "cloudLifecycleState"} and summary["department"][7] == 7  # used by
    assert "Not a lifecycle state" in {r[2] for r in book["Quality"].iter_rows(min_row=2, values_only=True)}
    assert cli.main(["attribute", "nothing", "--log-dir", "", "-o", str(tmp_path / "x")]) == 2
