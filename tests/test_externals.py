"""Externals who left: the analysis, the report and the plan (read-only)."""

import json
from datetime import date, timedelta

from openpyxl import load_workbook

from conftest import FakeApi, FakeIsc
from isc_reporter import cli, externals
from isc_reporter.externals import RECENT, TO_DELETE, TO_REVIEW

TODAY = date.today()
NE, INTERNS, AD, BADGES = "n" * 32, "i" * 32, "a" * 32, "b" * 32
NELM = "e" * 32                                   # the non-employee source id of NE


def ago(days):
    return (TODAY - timedelta(days=days)).isoformat()


def account(name, source, identity=None, **attributes):
    return {"id": f"acc-{name}", "name": name, "nativeIdentity": name, "sourceId": source,
            "identityId": identity, "uncorrelated": not identity,
            "identity": {"id": identity, "name": identity} if identity else None, "attributes": attributes}


def record(name, end):
    return {"id": f"rec-{name}", "accountName": name, "endDate": end, "sourceId": NELM}


def elsewhere(source, name, disabled, owner=""):
    return {"id": f"other-{name}-{source}", "accountId": f"{owner}-{name}",
            "source": {"id": source, "name": name}, "disabled": disabled}


def person(name, profile, state, end, accounts=(), owns=None, manager=False):
    return {"id": name, "name": name, "displayName": name.title(),
            "identityProfile": {"id": profile, "name": profile.title()},
            "attributes": {"cloudLifecycleState": state, "endDate": end}, "accounts": list(accounts),
            "owns": owns or {}, "isManager": manager}


SOURCES = {
    NE: externals.SourceInfo(NE, "Contractors", externals.NON_EMPLOYEE, NELM, {"contractors": "Contractors"}),
    INTERNS: externals.SourceInfo(INTERNS, "Interns", externals.DELIMITED, "", {"interns": "Interns"}),
    BADGES: externals.SourceInfo(BADGES, "Badges", externals.OTHER),
}
ACCOUNTS = {
    NE: [account("ann", NE, "ann"), account("ben", NE, "ben"), account("cara", NE, "cara"),
         account("dev", NE, "dev"), account("eli", NE, "eli"), account("fay", NE, "fay"),
         account("gus", NE, "gus"), account("hal", NE, "hal", endDate="2020-01-10"), account("ivy", NE)],
    INTERNS: [account("jo", INTERNS, "jo", endDate="31/03/2020"),
              account("kim", INTERNS, "kim", endDate="04/03/2020"),
              account("lou", INTERNS, "lou", endDate="next year"),
              account("max", INTERNS, "max", endDate="15/08/2020")],
    BADGES: [account("ned", BADGES, "ned", endDate="2020-01-01")],
}
RECORDS = {NE: [record("ann", "2020-03-31T00:00:00Z"), record("ben", "2020-05-01T00:00:00Z"),
                record("cara", "2020-04-15T00:00:00Z"), record("dev", "2020-06-01T00:00:00Z"),
                record("eli", ago(20) + "T00:00:00Z"), record("fay", "2099-01-31T00:00:00Z"),
                record("gus", None), record("ivy", "2020-02-01T00:00:00Z")]}
IDENTITIES = {doc["id"]: doc for doc in [
    person("ann", "contractors", "inactive", "2020-03-31", [elsewhere(AD, "AD", True, owner="ann")]),
    person("ben", "contractors", "inactive", "2020-05-01", [elsewhere(AD, "AD", False, owner="ben")]),
    person("cara", "contractors", "inactive", "2020-04-15", owns={"roles": [{"id": "r1"}, {"id": "r2"}]},
           manager=True),
    person("dev", "contractors", "active", ago(10)),
    person("hal", "contractors", "inactive", "2020-01-10"),
    person("jo", "interns", "out", "2020-03-31"),
    person("kim", "interns", "out", "2020-03-04"),
    person("max", "employees", "out", "2020-08-15"),
    person("ned", "contractors", "inactive", "2020-01-01"),
]}
LIFECYCLE = {
    "contractors": {"inactive": {"technicalName": "inactive", "identityState": "INACTIVE_SHORT_TERM"},
                    "active": {"technicalName": "active", "identityState": "ACTIVE"}},
    "interns": {"out": {"technicalName": "out", "identityState": "INACTIVE_LONG_TERM"}}}


def data(**changes):
    values = {"sources": list(SOURCES.values()), "accounts": ACCOUNTS, "records": RECORDS,
              "identities": IDENTITIES, "managed": {"cara": 3}, "lifecycle": LIFECYCLE,
              "settings": externals.Settings(90), "today": TODAY, "origin": "https://tenant.api.identitynow.com"}
    return externals.ExternalData(**{**values, **changes})


def rows_by_account(result):
    return {externals.display(row)["Account"]: row for row in result.rows}


def test_dates():
    assert externals.day_order(["31/03/2026", "04/03/2026"]) == "dmy"
    assert externals.day_order(["03/31/2026", "04/03/2026"]) == "mdy"
    assert externals.day_order(["31/03/2026", "03/31/2026"]) == "mixed"
    assert externals.day_order(["04/03/2026", "2026-01-01", None]) == ""
    parse = externals.parse_date
    assert parse("2026-03-31T22:00:00Z") == (date(2026, 3, 31), "")
    assert parse("20260331") == (date(2026, 3, 31), "")
    assert parse("04/03/2026", order="dmy")[0] == date(2026, 3, 4)
    assert parse("04/03/2026", order="mdy")[0] == date(2026, 4, 3)
    assert parse("04/04/2026")[0] == date(2026, 4, 4)                # the same both ways
    assert parse("31/03/2026")[0] == date(2026, 3, 31)               # only one way
    assert parse("04/03/2026")[0] is None and "--date-format" in parse("04/03/2026")[1]   # never guessed
    assert parse("04/03/2026", order="mixed")[0] is None
    assert parse("2026.03.04", "%Y.%m.%d")[0] == date(2026, 3, 4)
    assert parse("04/03/2026", "%Y.%m.%d")[0] is None
    assert parse("") == (None, "") and parse(None) == (None, "")
    assert parse("soon")[0] is None and parse("31/02/2026")[0] is None


def test_each_account_gets_a_status_and_its_reasons():
    result = externals.analyse(data())
    rows = rows_by_account(result)
    status = {name: row.status for name, row in rows.items()}
    # The end dates decide: what deleting leaves behind (ben, cara) is a warning.
    assert status == {"ann": TO_DELETE, "ben": TO_DELETE, "cara": TO_DELETE, "ivy": TO_DELETE,
                      "jo": TO_DELETE, "kim": TO_DELETE, "dev": TO_REVIEW, "hal": TO_REVIEW,
                      "lou": TO_REVIEW, "max": TO_REVIEW, "ned": TO_REVIEW, "eli": RECENT}
    labels = {name: {label for label, _ in row.reasons} for name, row in rows.items()}
    warnings = {name: {label for label, _ in row.warnings} for name, row in rows.items()}
    assert warnings["ben"] == {externals.OTHER_ENABLED} and labels["ben"] == set()
    assert warnings["cara"] == {externals.OWNS, externals.MANAGER} and labels["cara"] == set()
    assert labels["dev"] == {externals.IDENTITY_DATE} and warnings["dev"] == {externals.NOT_LEAVER}
    assert labels["hal"] == {externals.NO_RECORD}                  # ended per its account attribute
    assert "none of the 8 non-employee record(s)" in rows["hal"].reasons[0][1]
    assert labels["lou"] == {externals.UNREADABLE}
    assert labels["max"] == {externals.NOT_AUTHORITATIVE}
    assert externals.UNSUPPORTED in labels["ned"]
    assert "1 disabled account(s) on AD will become uncorrelated" in rows["ann"].notes
    assert rows["ivy"].notes == ["Uncorrelated account: no identity to delete"]
    assert rows["kim"].end == date(2020, 3, 4)                     # dd/MM, as 31/03 in the same file tells
    assert rows["eli"].eligible == TODAY - timedelta(days=20) + timedelta(days=90)
    counts = result.counts
    assert (counts[NE]["Accounts"], counts[NE]["No end date"], counts[NE]["End date to come"]) == (9, 1, 1)
    assert [row.status for row in result.rows][:5] == [TO_REVIEW] * 5          # to review first
    shown = externals.display(rows["cara"])
    assert shown["Owns"] == 2 and shown["Manages"] == 3
    assert "2 role(s): reassign them" in shown["Warnings"] and shown["Reasons"] == ""
    assert externals.display(rows["jo"])["Action"].startswith("Remove the row from the file")
    assert "and the identity" in externals.display(rows["ann"])["Action"]
    assert externals.display(rows["ben"])["Action"].endswith("disable the 1 account(s) left uncorrelated")
    left = externals.uncorrelated_rows(result)
    assert [(r["Identity"], r["Source"], r["Account"], r["Enabled"]) for r in left] == [
        ("ben", "AD", "ben-AD", "yes"), ("ann", "AD", "ann-AD", "no")]          # enabled first


def test_strict_keeps_for_review_what_would_be_left_behind():
    result = externals.analyse(data(settings=externals.Settings(90, strict=True)))
    rows = rows_by_account(result)
    assert {name for name, row in rows.items() if row.status == TO_DELETE} == {"ann", "ivy", "jo", "kim"}
    assert {label for label, _ in rows["ben"].reasons} == {externals.OTHER_ENABLED}
    assert {label for label, _ in rows["cara"].reasons} == {externals.OWNS, externals.MANAGER}
    assert {label for label, _ in rows["dev"].reasons} == {externals.NOT_LEAVER, externals.IDENTITY_DATE}
    assert not any(row.warnings for row in result.rows)
    assert externals.Settings.from_json(externals.Settings(90, strict=True).to_json()).strict


def test_records_match_by_account_name_or_id_never_by_guess():
    records = [{"id": "r-1", "accountName": "ann"}, {"id": "r-2", "accountName": "bob"}]
    index = externals.record_index(records)
    match = externals.match_record
    assert match({"nativeIdentity": "ANN"}, index)[0]["id"] == "r-1"                 # case does not matter
    assert match({"nativeIdentity": "r-2", "name": "Bob B."}, index)[0]["id"] == "r-2"  # the record id
    assert match({"nativeIdentity": "x", "attributes": {"accountName": "bob"}}, index)[0]["id"] == "r-2"
    assert match({"nativeIdentity": "nobody"}, index) == (None, "")
    record, why = match({"nativeIdentity": "ann", "name": "bob"}, index)            # two records
    assert record is None and why == "2 non-employee records match this account"


def test_settings_change_the_checks():
    loose = externals.analyse(data(settings=externals.Settings(
        90, identity_attribute="", states=["inactive", "active", "out"])))
    assert rows_by_account(loose)["dev"].status == TO_DELETE     # leaver states given, identity date ignored
    short = externals.analyse(data(settings=externals.Settings(5)))
    assert rows_by_account(short)["eli"].status == TO_REVIEW      # eli's identity was not read
    formatted = externals.analyse(data(settings=externals.Settings(90, date_format="%m/%d/%Y")))
    rows = rows_by_account(formatted)
    assert rows["kim"].end == date(2020, 4, 3) and rows["jo"].status == TO_REVIEW   # 31 is not a month


def test_plan_lists_only_the_accounts_to_delete(tmp_path):
    result = externals.analyse(data())
    path = externals.write_plan(str(tmp_path / "x.plan.json"), result)
    plan = json.load(open(path, encoding="utf-8"))
    assert plan["format"] == externals.PLAN_FORMAT and plan["tenant"] == "https://tenant.api.identitynow.com"
    assert plan["settings"]["days"] == 90
    assert plan["settings"]["sources"] == ["Contractors", "Interns", "Badges"]
    entries = {e["account"]: e for e in plan["deletions"]}
    assert set(entries) == {"ann", "ben", "cara", "ivy", "jo", "kim"}
    assert entries["ann"] == {"source": "Contractors", "sourceId": NE, "kind": externals.NON_EMPLOYEE,
                              "accountId": "acc-ann", "account": "ann", "recordId": "rec-ann",
                              "identityId": "ann", "identity": "ann", "endDate": "2020-03-31",
                              "warnings": []}
    assert entries["ben"]["warnings"] == [
        "Enabled accounts elsewhere: 1 on AD: they stay, uncorrelated, with their access"]
    assert entries["jo"]["recordId"] == "" and entries["jo"]["kind"] == externals.DELIMITED
    assert externals.Settings.from_json(plan["settings"]) == externals.Settings(90)


def test_reports(tmp_path):
    result = externals.analyse(data())
    xlsx, pdf, csv = (str(tmp_path / f"x.{ext}") for ext in ("xlsx", "pdf", "csv"))
    externals.write_xlsx(xlsx, result)
    externals.write_pdf(pdf, result)
    externals.write_csv(csv, result)
    book = load_workbook(xlsx)
    assert book.sheetnames == ["Dashboard", TO_DELETE, externals.UNCORRELATED, TO_REVIEW, RECENT, "By source",
                               "Settings", "Chart data", "Read me"]
    to_delete = [r for r in book[TO_DELETE].iter_rows(min_row=2, values_only=True)]
    assert {r[1] for r in to_delete} == {"ann", "ben", "cara", "ivy", "jo", "kim"}
    assert len(list(book[externals.UNCORRELATED].iter_rows(min_row=2, values_only=True))) == 2
    by_source = {r[0]: r for r in book["By source"].iter_rows(min_row=2, values_only=True)}
    # type, accounts, no end date, end date to come, left recently, to review, to delete
    assert by_source["Contractors"][1:8] == (externals.NON_EMPLOYEE, 9, 1, 1, 1, 2, 4)
    settings = {r[0]: r[1] for r in book["Settings"].iter_rows(min_row=2, values_only=True)}
    assert settings["Non-employee records of Contractors"] == "8"
    assert settings["What is left behind"] == "warnings"
    assert open(pdf, "rb").read(4) == b"%PDF"
    import csv as csv_module

    with open(csv, encoding="utf-8-sig", newline="") as fh:
        lines = list(csv_module.reader(fh, delimiter=";"))
    assert lines[0][:3] == ["Status", "Source", "Account"] and len(lines) == 13


def fake_isc(records=True, identities=None):
    identities = IDENTITIES if identities is None else identities

    def search(search, limit):
        query = search.query.query
        if query.startswith("manager.id:"):
            return [{"id": f"r{n}", "manager": {"id": "cara"}} for n in range(3)]
        return [doc for doc in identities.values() if f'"{doc["id"]}"' in query]

    def records_of(limit, offset, filters):
        if not records:
            raise ApiError(403)
        return [r for r in RECORDS.get(NE, []) if f'"{r["sourceId"]}"' in filters] if not offset else []

    sources = {"Contractors": {"id": NE, "name": "Contractors", "type": "Non-Employee"},
               "Interns": {"id": INTERNS, "name": "Interns", "type": "DelimitedFile",
                           "connector": "delimited-file"},
               "Badges": {"id": BADGES, "name": "Badges", "type": "Web Services"}}
    profiles = [{"id": "contractors", "name": "Contractors", "authoritativeSource": {"id": NE}},
                {"id": "interns", "name": "Interns", "authoritativeSource": {"id": INTERNS}},
                {"id": "employees", "name": "Employees", "authoritativeSource": {"id": AD}}]
    return FakeIsc(
        non_employee=FakeApi(
            list_non_employee_sources_v1=lambda limit, offset:
            [] if offset else [{"id": NELM, "sourceId": NE}],
            list_non_employee_records_v1=records_of),
        identity_profiles=FakeApi(list_identity_profiles_v1=lambda limit, offset: [] if offset else profiles),
        sources=FakeApi(list_sources_v1=lambda filters, limit:
                        [s for n, s in sources.items() if f'"{n}"' in filters]),
        accounts=FakeApi(list_accounts_v1=lambda limit, offset, filters: [] if offset else [
            a for source_id, found in ACCOUNTS.items() for a in found if f'"{source_id}"' in filters]),
        lifecycle_states=FakeApi(get_lifecycle_states_v1=lambda identity_profile_id, limit, offset:
                                 [] if offset else list(LIFECYCLE.get(identity_profile_id, {}).values())),
        search=FakeApi(search_post_v1=search))


def test_fetch_reads_sources_records_identities_and_managers():
    isc = fake_isc()
    found = externals.fetch(isc, ["Contractors", "Interns", "Badges"], externals.Settings(90), TODAY)
    assert [(s.name, s.kind, s.nelm_id) for s in found.sources] == [
        ("Contractors", externals.NON_EMPLOYEE, NELM), ("Interns", externals.DELIMITED, ""),
        ("Badges", externals.OTHER, "")]
    assert found.sources[0].profiles == {"contractors": "Contractors"}
    assert len(found.records[NE]) == 8 and found.managed == {"cara": 3}
    assert "eli" not in found.identities and "ann" in found.identities      # only those ended 90 days ago
    assert set(found.lifecycle) == {"contractors", "interns"}
    assert isc.non_employee.called("list_non_employee_records_v1")[0]["filters"] == f'sourceId eq "{NELM}"'
    assert "1 of the 9 account(s) of 'Contractors' match none of its 8 non-employee record(s)" in " ".join(
        found.notes)                                                        # hal
    result = externals.analyse(found)
    to_delete = {externals.display(r)["Account"] for r in result.rows if r.status == TO_DELETE}
    assert to_delete == {"ann", "ben", "cara", "ivy", "jo", "kim"}


def test_fetch_without_the_records():
    found = externals.fetch(fake_isc(records=False), ["Contractors"], externals.Settings(90), TODAY)
    notes = " ".join(found.notes)
    assert "records of 'Contractors' not read" in notes and "idn:nelm:read" in notes
    assert NE not in found.records
    result = externals.analyse(found)
    rows = rows_by_account(result)
    # Only hal has an endDate of its own: the others have no end date without their record.
    assert set(rows) == {"hal"} and result.counts[NE]["No end date"] == 8
    assert rows["hal"].status == TO_REVIEW
    assert rows["hal"].reasons == [(externals.NO_RECORD, "the non-employee records of this source could not "
                                                         "be read: see Settings")]


class ApiError(Exception):
    def __init__(self, status, text="refused"):
        super().__init__(f"HTTP {status}")
        self.status, self.body, self.reason = status, None, text


def records_only_for(source_id, status=400):
    """The records API of a tenant that knows the non-employee source by *source_id* only."""
    def records_of(limit, offset, filters):
        if f'"{source_id}"' not in filters:
            raise ApiError(status, f"Referenced sourceId was not found ({filters})")
        return [] if offset else RECORDS[NE]
    return records_of


def test_ids_with_or_without_dashes():
    bare, dashed = "ac11000a84871b978184af0a47db0e4e", "ac11000a-8487-1b97-8184-af0a47db0e4e"
    assert externals.id_variants(bare) == [bare, dashed]
    assert externals.id_variants(dashed) == [dashed, bare]
    assert externals.id_variants("not-a-uuid") == ["not-a-uuid", "notauuid"]
    assert externals.id_variants("") == []
    info = externals.SourceInfo(bare, "Contractors", externals.NON_EMPLOYEE, "x1")
    assert externals.source_ids(info) == ["x1", bare, dashed]       # the non-employee source id first
    lookup = externals.nelm_lookup([{"id": "x1", "sourceId": dashed, "name": "Contractors"}])
    assert externals.nelm_id_of({"id": bare, "name": "Other"}, lookup) == "x1"       # same id, dashes aside
    assert externals.nelm_id_of({"id": "zz", "name": "contractors"}, lookup) == "x1"    # by name
    assert externals.nelm_id_of({"id": "zz", "name": "Interns"}, lookup) == ""


def test_records_are_read_with_whichever_id_the_tenant_knows():
    for known in (NELM, NE):                       # the non-employee source id, or the ISC source id
        isc = fake_isc()
        isc.non_employee.responses["list_non_employee_records_v1"] = records_only_for(known)
        found = externals.fetch(isc, ["Contractors"], externals.Settings(90), TODAY)
        assert len(found.records[NE]) == 8 and not any("not read" in note for note in found.notes)
    isc = fake_isc()                               # listed by name only, with another id
    listed = [{"id": NELM, "sourceId": "elsewhere", "name": "Contractors"}]
    isc.non_employee.responses["list_non_employee_sources_v1"] = lambda limit, offset: (
        [] if offset else listed)
    isc.non_employee.responses["list_non_employee_records_v1"] = records_only_for(NELM)
    found = externals.fetch(isc, ["Contractors"], externals.Settings(90), TODAY)
    assert found.sources[0].nelm_id == NELM and len(found.records[NE]) == 8


def test_records_that_cannot_be_read_say_why():
    isc = fake_isc()
    isc.non_employee.responses["list_non_employee_records_v1"] = records_only_for("unknown")
    found = externals.fetch(isc, ["Contractors"], externals.Settings(90), TODAY)
    note = next(n for n in found.notes if "not read" in n)
    assert "3 source id(s) tried" in note and "idn:nelm:read" not in note        # not a permission problem
    isc.non_employee.responses["list_non_employee_records_v1"] = records_only_for("unknown", status=403)
    found = externals.fetch(isc, ["Contractors"], externals.Settings(90), TODAY)
    assert "idn:nelm:read" in next(n for n in found.notes if "not read" in n)
    assert len(isc.non_employee.called("list_non_employee_records_v1")) == 3 + 1  # stops at the refusal


NERM_ID = "f" * 32
NERM_SOURCE = {"id": NERM_ID, "name": "External form", "type": "39946170-edb9-46ac-b5f4-3c9bccef4cbb",
               "connector": "39946170-edb9-46ac-b5f4-3c9bccef4cbb",
               "connectorName": "SailPoint Non-Employee Risk Management"}
ZOE = person("zoe", "nerm", "out", "2017-01-22", [elsewhere(AD, "AD", False, owner="zoe"),
                                                  elsewhere("g" * 32, "ISC Governance", False, owner="zoe")])


def test_nerm_sources_are_analysed_but_deleted_in_nerm():
    assert externals.source_kind(NERM_SOURCE) == externals.NERM
    assert externals.source_kind({"type": "Non-Employee"}) == externals.NON_EMPLOYEE
    assert externals.source_kind({"type": "DelimitedFile"}) == externals.DELIMITED
    info = externals.SourceInfo(NERM_ID, "External form", externals.NERM, "", {"nerm": "NERM"})
    result = externals.analyse(data(
        sources=[info], accounts={NERM_ID: [account("zoe", NERM_ID, "zoe", endDate="2017-01-22")]},
        records={}, identities={"zoe": ZOE}, managed={},
        lifecycle={"nerm": {"out": {"technicalName": "out", "identityState": "INACTIVE_LONG_TERM"}}}))
    row = result.rows[0]                       # the row of the test on a real tenant
    assert row.status == TO_DELETE and row.reasons == []
    assert {label for label, _ in row.warnings} == {externals.OTHER_ENABLED}
    assert "2 on AD, ISC Governance" in row.warnings[0][1]
    assert "profile in Non-Employee Risk Management" in externals.display(row)["Action"]
    assert externals.plan(result)["deletions"] == []                   # iscr-clean cannot delete it


def test_cli_with_a_nerm_source(tmp_path, monkeypatch):
    isc = fake_isc(identities={"zoe": ZOE})
    isc.sources.responses["list_sources_v1"] = lambda filters, limit: [NERM_SOURCE]
    isc.accounts.responses["list_accounts_v1"] = lambda limit, offset, filters: (
        [] if offset else [account("zoe", NERM_ID, "zoe", endDate="2017-01-22")])
    isc.identity_profiles.responses["list_identity_profiles_v1"] = lambda limit, offset: (
        [] if offset else [{"id": "nerm", "name": "NERM", "authoritativeSource": {"id": NERM_ID}}])
    isc.lifecycle_states.responses["get_lifecycle_states_v1"] = lambda identity_profile_id, limit, offset: (
        [] if offset else [{"technicalName": "out", "identityState": "INACTIVE_LONG_TERM"}])
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: isc)
    out = tmp_path / "externals"
    assert cli.main(["externals", "--source", "External form", "--days", "180", "--format", "csv",
                     "-o", str(out), "--log-dir", ""]) == 0
    assert not isc.non_employee.called("list_non_employee_records_v1")    # no record to look for
    assert not (tmp_path / "externals.plan.json").exists()
    assert "To delete;External form;zoe" in (tmp_path / "externals.csv").read_text(encoding="utf-8-sig")


def test_missing_account_attribute_is_reported():
    settings = externals.Settings(90, account_attribute="contractEnd")
    found = externals.fetch(fake_isc(), ["Interns"], settings, TODAY)
    assert any("has the attribute 'contractEnd'" in note and "endDate" in note for note in found.notes)


def test_cli_writes_the_report_and_the_plan(tmp_path, monkeypatch):
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: fake_isc())
    out = tmp_path / "externals"
    assert cli.main(["externals", "--source", "Contractors", "--source", "Interns", "--days", "90",
                     "--format", "xlsx", "csv", "-o", str(out), "--log-dir", ""]) == 0
    plan = json.load(open(str(out) + ".plan.json", encoding="utf-8"))
    assert {e["account"] for e in plan["deletions"]} == {"ann", "ben", "cara", "ivy", "jo", "kim"}
    assert (tmp_path / "externals.xlsx").exists() and (tmp_path / "externals.csv").exists()
    assert not (tmp_path / "externals.pdf").exists()
    strict = tmp_path / "strict"
    assert cli.main(["externals", "--source", "Contractors", "--source", "Interns", "--days", "90",
                     "--strict", "--format", "csv", "-o", str(strict), "--log-dir", ""]) == 0
    plan = json.load(open(str(strict) + ".plan.json", encoding="utf-8"))
    assert {e["account"] for e in plan["deletions"]} == {"ann", "ivy", "jo", "kim"}
    assert plan["settings"]["strict"] is True


def test_cli_without_anything_to_delete_writes_no_plan(tmp_path, monkeypatch):
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: fake_isc())
    out = tmp_path / "externals"
    assert cli.main(["externals", "--source", "Badges", "--days", "90", "--format", "csv", "-o", str(out),
                     "--log-dir", ""]) == 0
    assert not (tmp_path / "externals.plan.json").exists()
    assert cli.main(["externals", "--source", "Badges", "--days", "-1", "--log-dir", ""]) == 2
