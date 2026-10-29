"""Interactive HTML view of the mappings."""

import json
import re

from isc_reporter import cli, mappings_html


def account(attribute, source="HR"):
    return {"type": "accountAttribute", "attributes": {"sourceName": source, "attributeName": attribute}}


TRANSFORMS = {
    "ISO date": {"name": "ISO date", "type": "dateFormat",
                 "attributes": {"inputFormat": "dd/MM/yyyy", "outputFormat": "ISO8601"}},
    "Lifecycle": {"name": "Lifecycle", "type": "dateCompare", "attributes": {
        "firstDate": {"type": "reference", "attributes": {"id": "ISO date", "input": account("startDate")}},
        "secondDate": "now", "operator": "LTE", "positiveCondition": "active",
        "negativeCondition": "prehire"}},
    "Unused": {"name": "Unused", "type": "upper", "attributes": {}},
}
PROFILE = {"name": "Employees", "priority": 10, "authoritativeSource": {"name": "HR"},
           "identityAttributeConfig": {"attributeTransforms": [
               {"identityAttributeName": "uid", "transformDefinition": account("id")},
               {"identityAttributeName": "cloudLifecycleState",
                "transformDefinition": {"type": "reference", "attributes": {"id": "Lifecycle"}}}]}}


def embedded(page: str) -> dict:
    found = re.search(r'<script id="data" type="application/json">(.*?)</script>', page, re.S)
    return json.loads(found.group(1))


def test_page_data_has_a_tree_per_attribute_and_the_relations_of_named_transforms():
    data = mappings_html.page_data([PROFILE], TRANSFORMS, origin="demo")
    profile = data["profiles"][0]
    assert (profile["name"], profile["source"]) == ("Employees", "HR")
    lifecycle = {a["name"]: a for a in profile["attributes"]}["cloudLifecycleState"]
    assert lifecycle["mapping"] == "Named transform"
    tree = lifecycle["tree"]
    assert (tree["label"], tree["kind"]) == ("cloudLifecycleState", "attribute")
    step = tree["children"][0]
    assert (step["label"], step["kind"]) == ("Lifecycle", "named")
    iso = step["children"][0]
    assert (iso["role"], iso["label"], iso["children"][0]["kind"]) == ("firstDate", "ISO date", "account")
    assert {c["check"] for c in lifecycle["checks"]} <= {"Same mapping", "Other source"}
    named = {t["name"]: t for t in data["transforms"]}
    assert named["Lifecycle"]["calls"] == ["ISO date"] and named["ISO date"]["calledBy"] == ["Lifecycle"]
    assert named["ISO date"]["usedBy"] == [["Employees", "cloudLifecycleState"]]       # through Lifecycle
    assert named["Unused"]["usedBy"] == [] and named["Unused"]["calledBy"] == []        # listed all the same
    assert json.loads(named["ISO date"]["definition"])["type"] == "dateFormat"


def test_page_data_has_the_map_of_dependencies():
    graph = mappings_html.page_data([PROFILE], TRANSFORMS)["graph"]
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert {(e["from"], e["to"], e["kind"]) for e in graph["edges"]} == {
        ("t:ISO date", "t:Lifecycle", "calls"), ("t:Lifecycle", "a:Employees/cloudLifecycleState", "uses")}
    assert nodes["t:ISO date"]["usedIn"] == ["Employees"]                   # through Lifecycle
    assert nodes["t:Unused"]["usedIn"] == []
    assert nodes["a:Employees/cloudLifecycleState"]["profile"] == "Employees"
    assert "a:Employees/uid" not in nodes


def test_values_cannot_break_out_of_the_page():
    hostile = {**PROFILE, "name": "</script><script>alert(1)</script>"}
    page = mappings_html.render(mappings_html.page_data([hostile], TRANSFORMS), title="<b>Mappings</b> & co")
    assert "<script>alert(1)" not in page
    assert "<title>&lt;b&gt;Mappings&lt;/b&gt; &amp; co</title>" in page
    assert embedded(page)["profiles"][0]["name"] == hostile["name"]                     # intact once parsed
    assert page.count("<script") == 2                                                    # data and code only


def test_the_command_writes_the_page_by_default(tmp_path):
    (tmp_path / "export.json").write_text(json.dumps([PROFILE, *TRANSFORMS.values()]), encoding="utf-8")
    out = tmp_path / "mappings"
    args = ["mappings", "--from", str(tmp_path / "export.json"), "-o", str(out), "--log-dir", ""]
    assert cli.main(args) == 0
    page = (tmp_path / "mappings.html").read_text(encoding="utf-8")
    assert page.startswith("<!doctype html>")
    assert not re.search(r'(src|href)="https?:|@import|url\(https?:', page)           # works offline
    assert [p["name"] for p in embedded(page)["profiles"]] == ["Employees"]
    assert (tmp_path / "mappings.xlsx").exists() and (tmp_path / "mappings.pdf").exists()
