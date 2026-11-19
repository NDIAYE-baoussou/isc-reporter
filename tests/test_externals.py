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


def elsewhere(source, name, disabled):
    return {"id": f"other-{name}-{source}", "source": {"id": source, "name": name}, "disabled": disabled}


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
    person("ann", "contractors", "inactive", "2020-03-31", [elsewhere(AD, "AD", disabled=True)]),
    person("ben", "contractors", "inactive", "2020-05-01", [elsewhere(AD, "AD", disabled=False)]),
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
    assert status == {"ann": TO_DELETE, "ivy": TO_DELETE, "jo": TO_DELETE, "kim": TO_DELETE,
                      "ben": TO_REVIEW, "cara": TO_REVIEW, "dev": TO_REVIEW, "hal": TO_REVIEW,
                      "lou": TO_REVIEW, "max": TO_REVIEW, "ned": TO_REVIEW, "eli": RECENT}
    labels = {name: {label for label, _ in row.reasons} for name, row in rows.items()}
    assert labels["ben"] == {externals.OTHER_ENABLED}
    assert labels["cara"] == {externals.OWNS, externals.MANAGER}
    assert labels["dev"] == {externals.NOT_LEAVER, externals.IDENTITY_DATE}
    assert labels["hal"] == {externals.NO_RECORD}                  # ended per its account attribute
    assert labels["lou"] == {externals.UNREADABLE}
    assert labels["max"] == {externals.NOT_AUTHORITATIVE}
    assert externals.UNSUPPORTED in labels["ned"]
    assert "1 disabled account(s) on AD will become uncorrelated" in rows["ann"].notes
    assert rows["ivy"].notes == ["Uncorrelated account: no identity to delete"]
    assert rows["kim"].end == date(2020, 3, 4)                     # dd/MM, as 31/03 in the same file tells
    assert rows["eli"].eligible == TODAY - timedelta(days=20) + timedelta(days=90)
    counts = result.counts
    assert (counts[NE]["Accounts"], counts[NE]["No end date"], counts[NE]["End date to come"]) == (9, 1, 1)
    assert [row.status for row in result.rows][:7] == [TO_REVIEW] * 7          # to review first
    shown = externals.display(rows["cara"])
    assert shown["Owns"] == 2 and shown["Manages"] == 3
    assert "2 role(s): reassign them first" in shown["Reasons"]
    assert externals.display(rows["jo"])["Action"].startswith("Remove the row from the file")
    assert "and the identity" in externals.display(rows["ann"])["Action"]


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
    assert set(entries) == {"ann", "ivy", "jo", "kim"}
    assert entries["ann"] == {"source": "Contractors", "sourceId": NE, "kind": externals.NON_EMPLOYEE,
                              "accountId": "acc-ann", "account": "ann", "recordId": "rec-ann",
                              "identityId": "ann", "identity": "ann", "endDate": "2020-03-31"}
    assert entries["jo"]["recordId"] == "" and entries["jo"]["kind"] == externals.DELIMITED
    assert externals.Settings.from_json(plan["settings"]) == externals.Settings(90)


def test_reports(tmp_path):
    result = externals.analyse(data())
    xlsx, pdf, csv = (str(tmp_path / f"x.{ext}") for ext in ("xlsx", "pdf", "csv"))
    externals.write_xlsx(xlsx, result)
    externals.write_pdf(pdf, result)
    externals.write_csv(csv, result)
    book = load_workbook(xlsx)
    assert book.sheetnames == ["Dashboard", TO_DELETE, TO_REVIEW, RECENT, "By source", "Settings",
                               "Chart data", "Read me"]
    to_delete = [r for r in book[TO_DELETE].iter_rows(min_row=2, values_only=True)]
    assert {r[1] for r in to_delete} == {"ann", "ivy", "jo", "kim"}
    by_source = {r[0]: r for r in book["By source"].iter_rows(min_row=2, values_only=True)}
    # type, accounts, no end date, end date to come, left recently, to review, to delete
    assert by_source["Contractors"][1:8] == (externals.NON_EMPLOYEE, 9, 1, 1, 1, 4, 2)
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
            raise RuntimeError("forbidden")
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
    result = externals.analyse(found)
    to_delete = {externals.display(r)["Account"] for r in result.rows if r.status == TO_DELETE}
    assert to_delete == {"ann", "ivy", "jo", "kim"}


def test_fetch_without_the_records():
    found = externals.fetch(fake_isc(records=False), ["Contractors"], externals.Settings(90), TODAY)
    assert any("records of 'Contractors' not read" in note for note in found.notes)
    rows = rows_by_account(externals.analyse(found))
    assert rows["hal"].status == TO_REVIEW
    assert {label for label, _ in rows["hal"].reasons} == {externals.NO_RECORD}


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
    assert {e["account"] for e in plan["deletions"]} == {"ann", "ivy", "jo", "kim"}
    assert (tmp_path / "externals.xlsx").exists() and (tmp_path / "externals.csv").exists()
    assert not (tmp_path / "externals.pdf").exists()


def test_cli_without_anything_to_delete_writes_no_plan(tmp_path, monkeypatch):
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: fake_isc())
    out = tmp_path / "externals"
    assert cli.main(["externals", "--source", "Badges", "--days", "90", "--format", "csv", "-o", str(out),
                     "--log-dir", ""]) == 0
    assert not (tmp_path / "externals.plan.json").exists()
    assert cli.main(["externals", "--source", "Badges", "--days", "-1", "--log-dir", ""]) == 2
