"""Lifecycle states in the mappings report."""

import json
import types

from openpyxl import load_workbook

from conftest import FakeApi, FakeIsc
from isc_reporter import cli, identity_profiles, lifecycle

AD, SAP, HR = "a" * 32, "s" * 32, "h" * 32
SOURCES = {AD: "AD", SAP: "SAP", HR: "HR"}


def act(action, sources=(), all_sources=False, excluded=()):
    return {"action": action, "sourceIds": list(sources), "allSources": all_sources,
            "excludeSourceIds": list(excluded)}


def state(name, identity_state=None, *actions, enabled=True, count=0, granted=(), remove_all=False,
          notify=None, priority=10):
    return {"name": name.title(), "technicalName": name, "enabled": enabled, "identityState": identity_state,
            "identityCount": count, "priority": priority, "accountActions": list(actions),
            "accessProfileIds": list(granted),
            "accessActionConfiguration": {"removeAllAccessEnabled": remove_all},
            "emailNotificationOption": notify or {}}


STATES = [
    state("active", "ACTIVE", act("ENABLE", all_sources=True), granted=["ap1"], priority=1,
          notify={"notifyManagers": True}),
    state("inactive", "INACTIVE_SHORT_TERM", act("DISABLE", all_sources=True, excluded=[HR]), priority=2,
          notify={"notifySpecificUsers": True, "emailAddressList": ["iam@example.com"]}),
    state("leave", "INACTIVE_SHORT_TERM", priority=3),                          # removes nothing
    state("gone", "INACTIVE_LONG_TERM", act("DELETE", [AD]), act("DISABLE", [SAP, "deleted"]),
          remove_all=True, granted=["ap-gone"], priority=4),
    state("old", None, enabled=False, count=3, priority=5),
]
LIFECYCLE_TRANSFORM = {"type": "dateCompare", "attributes": {
    "firstDate": {"type": "accountAttribute", "attributes": {"sourceName": "HR", "attributeName": "start"}},
    "secondDate": "now", "operator": "LTE", "positiveCondition": "active", "negativeCondition": "prehire"}}
PROFILE = {"id": "p" * 32, "name": "Employees", "priority": 10, "authoritativeSource": {"name": "HR"},
           "identityAttributeConfig": {"attributeTransforms": [
               {"identityAttributeName": "cloudLifecycleState",
                "transformDefinition": {"type": "reference", "attributes": {"id": "Lifecycle"}}}]}}
TRANSFORMS = {"Lifecycle": {"name": "Lifecycle", **LIFECYCLE_TRANSFORM}}


def states(**changes):
    values = {"states": {PROFILE["id"]: STATES}, "sources": SOURCES,
              "access_profiles": {"ap1": "Birthright"}}
    return lifecycle.Lifecycle(**{**values, **changes})


def test_what_each_state_does():
    found = states()
    rows = {r["Technical name"]: r for r in lifecycle.state_rows([PROFILE], found)}
    assert list(rows) == ["active", "inactive", "leave", "gone", "old"]               # by priority
    assert rows["active"]["Accounts"] == "Enable: all sources"
    assert rows["active"]["Access profiles granted"] == "Birthright"
    assert rows["active"]["Notified"] == "managers"
    assert rows["inactive"]["Accounts"] == "Disable: all sources except HR"
    assert rows["inactive"]["Notified"] == "iam@example.com"
    assert rows["gone"]["Accounts"] == "Delete: AD\nDisable: SAP, deleted"
    assert (rows["gone"]["Remove all access"], rows["gone"]["Access profiles granted"]) == ("yes", "ap-gone")
    by_source = {r["Source"]: r for r in lifecycle.source_rows([PROFILE], found)}
    assert by_source["AD"] == {"Source": "AD", "Employees: active": "Enable",
                               "Employees: inactive": "Disable", "Employees: gone": "Delete"}
    assert by_source["HR"]["Employees: inactive"] == ""                               # excluded
    unknown = lifecycle.source_rows([PROFILE], states(sources={}))
    assert [r["Source"] for r in unknown] == [lifecycle.ALL_SOURCES, AD, "deleted", SAP]


def test_values_the_mapping_can_return():
    assert lifecycle.values(LIFECYCLE_TRANSFORM, {}) == {"active", "prehire"}
    assert lifecycle.values({"type": "reference", "attributes": {"id": "Lifecycle"}}, TRANSFORMS) == {
        "active", "prehire"}
    lookup = {"type": "lookup", "attributes": {"table": {"A": "active", "T": "gone", "default": "Leave"}}}
    lowered = {"type": "lower", "attributes": {"input": lookup}}
    assert lifecycle.values(lowered, {}) == {"active", "gone", "leave"}
    copied = {"type": "accountAttribute", "attributes": {"attributeName": "status"}}
    assert lifecycle.values(copied, {}) is None                                       # cannot be known
    assert lifecycle.values({"type": "firstValid", "attributes": {"values": [copied, "active"]}}, {}) is None
    assert lifecycle.values({"type": "static", "attributes": {"value": "$status"}}, {}) is None


def test_checks():
    found = {(where, check): details
             for _, where, check, details in lifecycle.checks(PROFILE, states(), TRANSFORMS)}
    assert found[("cloudLifecycleState", "Value without lifecycle state")].startswith(
        "The mapping can return 'prehire', which is not a lifecycle state of this profile")
    assert ("state leave", "Leaver state that removes nothing") in found
    assert ("state inactive", "Leaver state that removes nothing") not in found
    assert found[("state gone", "Unknown source")].endswith("(deleted?): deleted.")
    assert found[("state gone", "Unknown access profile")].endswith("not found: ap-gone.")
    assert found[("state old", "Disabled state with identities")].startswith("3 identit(ies)")
    disabled = [state("prehire", enabled=False)] + STATES
    found = {check for _, _, check, _ in lifecycle.checks(PROFILE, states(states={PROFILE["id"]: disabled}),
                                                          TRANSFORMS)}
    assert "Lifecycle state disabled" in found and "Value without lifecycle state" not in found


def test_workbook_sheets(tmp_path):
    path = str(tmp_path / "m.xlsx")
    identity_profiles.write_xlsx(path, [PROFILE], TRANSFORMS, states=states())
    book = load_workbook(path)
    assert book.sheetnames[-5:] == ["Lifecycle states", "Lifecycle by source", "Checks", "Chart data",
                                    "Read me"]
    header = [c.value for c in book["Lifecycle states"][1]]
    assert header == ["Identity profile", "Lifecycle state", "Technical name", "Enabled", "Identity state",
                      "Identities", "Accounts", "Access profiles granted", "Remove all access", "Notified",
                      "Description"]
    checks = {row[3] for row in book["Checks"].iter_rows(min_row=2, values_only=True)}
    assert {"Value without lifecycle state", "Leaver state that removes nothing"} <= checks
    assert identity_profiles.write_pdf(str(tmp_path / "m.pdf"), [PROFILE], TRANSFORMS, states=states()) == 1
    identity_profiles.write_xlsx(str(tmp_path / "n.xlsx"), [PROFILE], TRANSFORMS)
    # lifecycle states not read: no sheet
    assert "Lifecycle states" not in load_workbook(str(tmp_path / "n.xlsx")).sheetnames


def test_from_the_tenant_and_from_files(tmp_path, monkeypatch):
    from sailpoint.identity_profiles.models import IdentityProfile
    from sailpoint.lifecycle_states.models import LifecycleState

    def first_page(items):
        return lambda limit, offset, **kwargs: items if not offset else []

    models = [LifecycleState.from_dict(s) for s in STATES]
    isc = FakeIsc(
        identity_profiles=FakeApi(list_identity_profiles_v1=first_page([IdentityProfile.from_dict(PROFILE)])),
        transforms=FakeApi(list_transforms_v1=first_page([])),
        identity_attributes=FakeApi(list_identity_attributes_v1=lambda **kw: []),
        lifecycle_states=FakeApi(get_lifecycle_states_v1=first_page(models)),
        sources=FakeApi(list_sources_v1=first_page(
            [types.SimpleNamespace(id=i, name=n) for i, n in SOURCES.items()])),
        search=FakeApi(search_post_v1=lambda search, limit: [{"id": "ap1", "name": "Birthright"}]))
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: isc)
    out = tmp_path / "tenant"
    assert cli.main(["mappings", "--format", "xlsx", "-o", str(out), "--log-dir", ""]) == 0
    sheet = load_workbook(str(out) + ".xlsx")["Lifecycle states"]
    rows = {r[2]: r for r in sheet.iter_rows(values_only=True)}
    assert rows["active"][7] == "Birthright" and rows["inactive"][6] == "Disable: all sources except HR"

    export = [{**PROFILE, "lifecycleStates": STATES},
              {"id": AD, "name": "AD", "connector": "active-directory"}]
    (tmp_path / "export.json").write_text(json.dumps(export), encoding="utf-8")
    found = lifecycle.from_files([export[0]], [str(tmp_path / "export.json")])
    assert len(found.states[PROFILE["id"]]) == 5 and found.sources == {AD: "AD"}
