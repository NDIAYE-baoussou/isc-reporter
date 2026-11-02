"""Transform analysis and optimized drafts."""

import csv
import json
import types

from openpyxl import load_workbook

from conftest import FakeApi, FakeIsc
from isc_reporter import cli
from isc_reporter import transform_analysis as analysis
from isc_reporter.identity_profiles import INFO, WARNING


def account(attribute, source="HR"):
    return {"type": "accountAttribute", "attributes": {"sourceName": source, "attributeName": attribute}}


def reference(name, given=None):
    return {"type": "reference", "attributes": {"id": name, **({"input": given} if given else {})}}


def identity(name):
    return {"type": "identityAttribute", "attributes": {"name": name}}


def op(kind, **attributes):
    return {"type": kind, "attributes": attributes}


def named(name, kind, internal=False, **attributes):
    return {"name": name, "type": kind, "attributes": attributes, **({"internal": True} if internal else {})}


def profile(name, source, *mappings):
    return {"name": name, "authoritativeSource": {"name": source}, "identityAttributeConfig": {
        "attributeTransforms": [{"identityAttributeName": a, "transformDefinition": d} for a, d in mappings]}}


def simplify(definition, renames=None):
    notes = []
    return analysis.simplify(definition, renames or {}, notes), notes


def test_simplify_only_rewrites_what_keeps_the_result():
    a, b, c = account("a"), account("b"), account("c")
    assert simplify(op("firstValid", values=[op("firstValid", values=[a, b]), c]))[0] == \
        op("firstValid", values=[a, b, c])
    keeps_errors = op("firstValid", values=[op("firstValid", values=[a], ignoreErrors=True), c])
    assert simplify(keeps_errors)[0] == keeps_errors                         # different error handling
    flat, notes = simplify(op("firstValid", values=[a, "none", b]))
    assert flat == op("firstValid", values=[a, "none"]) and "never used" in notes[0]
    templated = op("firstValid", values=[a, "$fallback", b])
    assert simplify(templated)[0] == templated                               # a template can be empty
    nested = op("concat", values=[op("concat", values=[a, "."]), b])
    assert simplify(nested)[0] == op("concat", values=[a, ".", b])
    assert simplify(op("concat", values=[a]))[0] == a
    assert simplify(op("firstValid", values=["n/a"]))[0] == op("static", value="n/a")
    assert simplify(op("lower", input=op("lower", input=a)))[0] == op("lower", input=a)
    assert simplify(op("upper", input=op("lower", input=a)))[0] == op("upper", input=a)
    trimmed = op("trim", input=op("lower", input=a))
    assert simplify(trimmed)[0] == trimmed                                    # not the same result
    assert simplify(op("dateFormat", inputFormat="yyyy", outputFormat="yyyy", input=a))[0] == a
    implicit = op("dateFormat", inputFormat="yyyy", outputFormat="yyyy")
    assert simplify(implicit)[0] == implicit                                  # its input is not known here
    renamed, notes = simplify(op("upper", input=reference("Old", a)), {"Old": "New"})
    assert renamed == op("upper", input=reference("New", a)) and notes == [
        "calls 'New' instead of 'Old', which repeats it"]


TRANSFORMS = {t["name"]: t for t in [
    named("Country", "lookup", table={"FR": "France"}),
    named("Email", "lower", input=op("concat", values=[identity("firstname"), "@example.com"])),
    named("Start", "dateCompare", firstDate=account("start"), secondDate="now", operator="LTE",
          positiveCondition="active", negativeCondition="prehire"),
    named("Start refreshed", "dateCompare", firstDate=account("start"), secondDate="now", operator="LTE",
          positiveCondition="active", negativeCondition="prehire", requiresPeriodicRefresh=True),
    named("Broken", "upper", input=op("reference")),
    named("Calls gone", "upper", input=reference("Gone")),
    named("Loop A", "upper", input=reference("Loop B")),
    named("Loop B", "lower", input=reference("Loop A")),
    named("HR department", "upper", input=account("department")),
    named("Old", "lower"),
    named("Built in", "lower", internal=True),
]}
PROFILES = [
    profile("Employees", "HR", ("email", reference("Email")), ("status", reference("Start")),
            ("status2", reference("Start refreshed")), ("dept", reference("HR department")),
            ("phone", op("firstValid", values=[account("mobile"), account("phone")])),
            ("country", reference("Country", account("country"))), ("broken", reference("Broken")),
            ("gone", reference("Calls gone")),
            ("category", op("static", value="#if($dept == 'IT')Tech#{else}Business#end"))),
    profile("Contractors", "CR", ("dept", reference("HR department"))),
]
POLICIES = [{"source": "AD", "usageType": "CREATE", "fields": [
    {"name": "mail", "transform": identity("email")},
    {"name": "login", "transform": op("rule", name="Unique login")}]}]


def test_findings():
    result = analysis.analyse(PROFILES, TRANSFORMS, POLICIES)
    found = {(f["Object"], f["Check"]): f for f in result.findings}
    assert found[("Country", "Lookup without default")]["Severity"] == WARNING
    assert found[("Email", "Identity attribute read in a profile mapping")]["Location"] == "input > values[1]"
    assert ("AD / CREATE: mail", "Identity attribute read in a profile mapping") not in found   # a policy may
    assert found[("Start", "Compared with today, not refreshed")]["Severity"] == INFO
    assert ("Start refreshed", "Compared with today, not refreshed") not in found
    assert found[("Broken", "Reference without id")]["Category"] == analysis.BROKEN
    assert found[("Calls gone", "Missing transform")]["Details"] == "Calls 'Gone', which does not exist."
    assert found[("Loop A", "Circular reference")]["Details"].startswith("Loop A > Loop B > Loop A")
    assert found[("HR department", "Reads a fixed source in several profiles")]["Details"] == (
        "Used by profiles of CR, HR but reads HR only: for the identities of Contractors the value is "
        "usually empty.")
    assert ("Employees: phone", "No fallback value") in found
    assert ("AD / CREATE: login", "Calls a rule") in found
    assert ("Employees: category", "Logic in a Velocity template") in found
    unused = {row["Transform"]: row["Why"] for row in result.unused}
    assert unused == {"Old": "called by no identity profile, transform or provisioning policy",
                      "Loop A": "only called by transforms that are not used either: Loop B",
                      "Loop B": "only called by transforms that are not used either: Loop A"}
    assert not any(f["Object"] == "Built in" for f in result.findings)        # SailPoint's own: left alone
    assert result.warnings == sum(f["Severity"] == WARNING for f in result.findings) == 5


def test_duplicates_aliases_and_repeated_blocks_go_to_the_drafts(tmp_path):
    nickname = op("concat", values=[op("lower", input=account("first")), ".",
                                    op("lower", input=account("last"))])
    transforms = {t["name"]: t for t in [
        named("Built in lower", "lower", internal=True), named("Lower", "lower"),
        named("Lower copy", "lower"),
        named("Old lower", "reference", id="Lower"),
        named("Upper first", "upper", input=account("first"))]}
    profiles = [
        profile("Employees", "HR", ("alias", nickname), ("login", reference("Old lower", account("login"))),
                ("code", op("concat", values=[op("upper", input=account("first")), "-1"]))),
        profile("Contractors", "CR", ("alias", json.loads(json.dumps(nickname))),
                ("login", reference("Lower copy", account("login"))))]
    result = analysis.analyse(profiles, transforms, [])
    assert [(d["Transform kept"], d["Duplicate"]) for d in result.duplicates] == [
        ("Built in lower", "Lower"), ("Built in lower", "Lower copy")]               # SailPoint's own is kept
    assert {row["Transform"] for row in result.to_delete} == {"Lower", "Lower copy", "Old lower"}
    assert [(s["name"], s["occurrences"], s["steps"]) for s in result.shared] == [("Shared concat 1", 2, 5)]
    employees = result.drafts["identity-profiles/Employees.json"]["identityAttributeConfig"][
        "attributeTransforms"]
    assert employees[0]["transformDefinition"] == reference("Shared concat 1")
    # Old lower calls Lower, a copy of Built in lower: the draft calls Built in lower directly
    assert employees[1]["transformDefinition"] == reference("Built in lower", account("login"))
    assert employees[2]["transformDefinition"] == op("concat", values=[reference("Upper first"), "-1"])
    checks = {(f["Object"], f["Check"]) for f in result.findings}
    assert {("Employees: alias", "Repeated block"), ("Employees: code", "Copy of a named transform"),
            ("Old lower", "Transform that only calls another one"),
            ("Lower copy", "Duplicate transform")} <= checks
    assert PROFILES[0]["identityAttributeConfig"]["attributeTransforms"][0]["transformDefinition"] == \
        reference("Email")                                                     # the input is not changed

    written = analysis.write_outputs(result, str(tmp_path / "out"))
    names = {path.replace("\\", "/").split("/out/")[-1] for path in written}
    assert names == {"transform-analysis.xlsx", "transform-analysis.pdf", "optimized/changes.csv",
                     "optimized/delete.csv", "optimized/identity-profiles/Employees.json",
                     "optimized/identity-profiles/Contractors.json",
                     "optimized/transforms/Shared concat 1.json"}
    shared = json.loads((tmp_path / "out/optimized/transforms/Shared concat 1.json").read_text("utf-8"))
    assert list(shared) == ["name", "type", "attributes"] and shared["attributes"] == nickname["attributes"]
    with open(tmp_path / "out/optimized/changes.csv", encoding="utf-8-sig", newline="") as fh:
        changes = list(csv.reader(fh, delimiter=";"))
    assert changes[0] == ["Where", "Object", "Change", "Before", "After"] and len(changes) == 6
    book = load_workbook(str(tmp_path / "out/transform-analysis.xlsx"))
    assert book.sheetnames == ["Dashboard", "Optimization plan", "Findings", "Cost by profile", "Duplicates",
                               "Same logic", "Repeated blocks", "Not used", "Complexity", "Changes",
                               "Chart data", "Read me"]


def test_platform_view_plans_fewer_transforms_and_less_work():
    tenure = named("Tenure", "dateCompare", firstDate=account("start"), secondDate="now", operator="LTE",
                   positiveCondition="yes", negativeCondition="no", requiresPeriodicRefresh=True)
    country = {**named("Country", "lookup", table={"FR": "France", "default": "Other"}),
               "requiresPeriodicRefresh": True}                               # next to the attributes
    transforms = {t["name"]: t for t in [
        named("Lower", "lower"), named("Lower mail", "lower", input=account("mail", "AD")),
        named("Lower login", "lower", input=account("login", "AD")),
        named("Upper dept", "upper", input=account("department")),
        named("Upper title", "upper", input=account("title")), named("Unused", "trim"), tenure, country]}
    employees = {**profile(
        "Employees", "HR", ("mail", reference("Lower mail")), ("login", reference("Lower login")),
        ("dept", reference("Upper dept")), ("title", reference("Upper title")),
        ("tenure", reference("Tenure")),
        ("country", reference("Country", account("country"))),
        ("nickname", op("lower", input=op("lower", input=account("nick")))),
        ("site", op("upper", input=account("site"), requiresPeriodicRefresh=True)),
        ("code", op("rule", name="Code", requiresPeriodicRefresh=True))), "identityCount": 100}
    contractors = {**profile("Contractors", "CR", ("mail", reference("Lower", account("mail", "CR"))),
                             ("dept", reference("Upper dept"))), "identityCount": 10}
    result = analysis.analyse([contractors, employees], transforms, [])

    nightly = {f["Object"] for f in result.findings if f["Check"] == analysis.NIGHTLY}
    assert nightly == {"Country", "Employees: site"}           # a date and a rule change on their own
    assert [(r["Transform kept"], r["Transform"], r["Input"]) for r in result.similar] == [
        ("Lower", "Lower", "(given by the caller)"), ("Lower", "Lower login", "AD.login"),
        ("Lower", "Lower mail", "AD.mail"), ("Upper dept", "Upper dept", "HR.department"),
        ("Upper dept", "Upper title", "HR.title")]
    assert result.similar[3]["Change"] == "kept without its input, which its 2 caller(s) then give"
    assert result.merged == ["Lower login", "Lower mail", "Upper title"]
    assert {f["Object"] for f in result.findings if f["Check"] == analysis.SAME_LOGIC} == set(result.merged)

    costs = {row["Profile"]: row for row in result.profile_costs}
    assert [row["Profile"] for row in result.profile_costs] == ["Employees", "Contractors"]
    assert (costs["Employees"]["Steps per identity"], costs["Employees"]["After the drafts"],
            costs["Employees"]["Steps per refresh"]) == (18, 17, 1800)
    assert costs["Employees"]["Refreshed every night"] == "yes, 4 attribute(s)"
    assert costs["Employees"]["Rules"] == 1 and costs["Contractors"]["Refreshed every night"] == "no"
    usage = {row["Transform"]: row for row in result.complexity}
    assert (usage["Upper dept"]["Places used"], usage["Upper dept"]["Identities"]) == (2, 110)

    assert result.custom == (8, 4)
    assert [(row["Step"], row["Suggestion"].split(" ")[0], row["Effect"]) for row in result.plan] == [
        (1, "Delete", "-1 named transform(s), after checking that no form, workflow or other configuration "
                      "calls them"),
        (2, "Keep", "-3 named transform(s): 5 transforms become 2"),
        (3, "Apply", "1 transform step(s) fewer per identity, over all the profiles; 100 fewer for a refresh "
                     "of every identity (-5%)"),
        (4, "Remove", "2 place(s) the nightly identity refresh computes again for nothing"),
        (5, "Replace", "1 rule call(s) run for every identity of their profile: code SailPoint must review "
                       "and deploy"),
        ("", "Result", "8 custom named transform(s) now, 4 after these steps (-50%)")]
    assert analysis.Costs(TRANSFORMS).call("Loop A").steps == 2               # a loop is followed once


def test_cli_from_files_and_from_the_tenant(tmp_path, monkeypatch):
    export = [*PROFILES, *TRANSFORMS.values(), {"name": "AD", "connector": "ad", "provisioningPolicies": [
        {"usageType": "CREATE", "fields": POLICIES[0]["fields"]}]}]
    (tmp_path / "export.json").write_text(json.dumps(export), encoding="utf-8")
    out = tmp_path / "files"
    args = ["transforms", "--from", str(tmp_path / "export.json"), "--log-dir", ""]
    assert cli.main([*args, "-o", str(out)]) == 0
    assert (out / "transform-analysis.xlsx").exists() and (out / "optimized").is_dir()
    bare = tmp_path / "bare"
    assert cli.main([*args, "--no-drafts", "--format", "xlsx", "-o", str(bare)]) == 0
    assert sorted(p.name for p in bare.iterdir()) == ["transform-analysis.xlsx"]

    source = types.SimpleNamespace(id="s" * 32, name="AD")
    def first_page(items):
        return lambda limit, offset, **kwargs: items if not offset else []

    isc = FakeIsc(
        identity_profiles=FakeApi(list_identity_profiles_v1=first_page(PROFILES)),
        transforms=FakeApi(list_transforms_v1=first_page(list(TRANSFORMS.values()))),
        sources=FakeApi(list_sources_v1=first_page([source]), list_provisioning_policies_v1=first_page(
            [{"usageType": "CREATE", "fields": POLICIES[0]["fields"]}])))
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: isc)
    tenant = tmp_path / "tenant"
    assert cli.main(["transforms", "--format", "xlsx", "-o", str(tenant), "--log-dir", ""]) == 0
    book = load_workbook(str(tenant / "transform-analysis.xlsx"))
    findings = list(book["Findings"].iter_rows(values_only=True))
    assert ("AD / CREATE: login", "Calls a rule") in {(row[3], row[5]) for row in findings}
