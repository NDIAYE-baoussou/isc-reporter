"""iscr-clean: the only command that changes a tenant, and only from a reviewed plan."""

import csv
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from isc_reporter import clean, externals
from test_externals import IDENTITIES, data, elsewhere, fake_isc

SRC = Path(__file__).resolve().parent.parent / "src" / "isc_reporter"
TENANT = "https://tenant.api.identitynow.com"


class ApiError(Exception):
    def __init__(self, status):
        super().__init__(f"HTTP {status}")
        self.status, self.body, self.reason = status, None, "error"


class Download:
    def __init__(self, status=200, data=b"accountName,endDate\nann,2020-03-31\n"):
        self.status, self.headers, self.data = status, {}, data


def write_plan(tmp_path, **changes):
    plan = externals.plan(externals.analyse(data()))
    plan.update(changes)
    path = tmp_path / "x.plan.json"
    path.write_text(json.dumps(plan), encoding="utf-8")
    return str(path)


def tenant(identities=None, delete_record=None, delete_account=None, export=None):
    """The fake tenant of test_externals, which also deletes."""
    isc = fake_isc(identities=identities)
    isc.non_employee.responses["delete_non_employee_record_v1"] = delete_record or (lambda id: None)
    isc.non_employee.responses["export_non_employee_records_v1_without_preload_content"] = (
        export or (lambda id: Download()))
    isc.accounts.responses["delete_account_v1"] = delete_account or (lambda id: {"id": f"task-{id}"})
    return isc


def run(tmp_path, monkeypatch, isc, *options, plan=None):
    monkeypatch.setattr("isc_reporter.client.connect", lambda *args, **kwargs: isc)
    return clean.main(["externals", plan or write_plan(tmp_path), "--env", "prod",
                       "-o", str(tmp_path / "out"), "--log-dir", "", *options])


def deletions(isc):
    return ([kw["id"] for kw in isc.non_employee.called("delete_non_employee_record_v1")]
            + [kw["id"] for kw in isc.accounts.called("delete_account_v1")])


def audit(tmp_path, name):
    with open(tmp_path / "out" / name, encoding="utf-8-sig", newline="") as fh:
        return {row["Account"]: row for row in csv.DictReader(fh, delimiter=";")}


def test_only_iscr_clean_calls_write_endpoints():
    write = re.compile(r"\.(delete|create|update|patch|put|set|disable|enable|start|import|submit|remove|"
                       r"approve|reject|cancel|send|move|reassign|sync|unlock|reload)_\w*v\d")
    offenders = sorted(p.name for p in SRC.rglob("*.py") if p.name != "clean.py" and write.search(p.read_text(
        encoding="utf-8")))
    assert offenders == []
    assert write.search((SRC / "clean.py").read_text(encoding="utf-8"))
    assert "clean" not in re.findall(r"from isc_reporter import ([\w, ]+)", (SRC / "cli.py").read_text(
        encoding="utf-8"))[0]
    assert "isc_reporter.clean" not in (SRC / "cli.py").read_text(encoding="utf-8")


def test_read_plan_refuses_what_it_cannot_trust(tmp_path):
    good = write_plan(tmp_path)
    assert len(clean.read_plan(good, 7)["deletions"]) == 4
    later = datetime.now(timezone.utc) + timedelta(days=8)
    with pytest.raises(clean.UsageError, match="8 day"):
        clean.read_plan(good, 7, now=later)
    bad = tmp_path / "bad.json"
    for content, message in (("{", "not valid JSON"), ('{"format": "other"}', "not a plan"),
                             (json.dumps({"format": externals.PLAN_FORMAT, "created": "2026-01-01",
                                          "settings": {}, "deletions": [{"source": "x"}]}),
                              "entry 1 has no")):
        bad.write_text(content, encoding="utf-8")
        with pytest.raises(clean.UsageError, match=message):
            clean.read_plan(str(bad), 100000)
    with pytest.raises(clean.UsageError, match="cannot read"):
        clean.read_plan(str(tmp_path / "missing.json"), 7)


def test_without_apply_nothing_is_deleted(tmp_path, monkeypatch):
    isc = tenant()
    assert run(tmp_path, monkeypatch, isc) == 0
    assert deletions(isc) == []
    assert not isc.non_employee.called("export_non_employee_records_v1_without_preload_content")
    rows = audit(tmp_path, "checked.csv")
    assert {name: row["Result"] for name, row in rows.items()} == dict.fromkeys(["ann", "ivy", "jo", "kim"],
                                                                                 clean.WOULD_DELETE)


def test_apply_deletes_what_is_still_to_delete(tmp_path, monkeypatch):
    plan = write_plan(tmp_path)
    jo = {**IDENTITIES["jo"], "accounts": [elsewhere("a" * 32, "AD", disabled=False)]}
    changed = {**IDENTITIES, "jo": jo}
    isc = tenant(identities=changed)                        # jo has an enabled account again since the plan
    assert run(tmp_path, monkeypatch, isc, "--apply", "--confirm", "3", plan=plan) == 0
    assert deletions(isc) == ["rec-ivy", "rec-ann", "acc-kim"]             # records, then delimited accounts
    rows = audit(tmp_path, "deleted.csv")
    assert {name: row["Result"] for name, row in rows.items()} == {
        "jo": clean.SKIPPED, "ann": clean.DELETED, "ivy": clean.DELETED, "kim": clean.DELETED}
    assert "Enabled accounts elsewhere" in rows["jo"]["Detail"]
    assert rows["kim"]["Detail"].startswith("account delete task task-acc-kim")
    backup = tmp_path / "out" / "backup"
    saved = json.loads((backup / "before-deletion.json").read_text(encoding="utf-8"))
    assert [item["plan"]["account"] for item in saved] == ["ivy", "kim", "ann"]   # longest gone first
    assert (backup / "non-employees_Contractors.csv").read_bytes().startswith(b"accountName")
    exports = isc.non_employee.called("export_non_employee_records_v1_without_preload_content")
    assert exports == [{"id": "e" * 32}]                                  # the non-employee source id


def test_confirmation_is_required(tmp_path, monkeypatch):
    isc = tenant()
    assert run(tmp_path, monkeypatch, isc, "--apply", "--confirm", "2") == 2     # 4 to delete, not 2
    assert run(tmp_path, monkeypatch, isc, "--apply") == 2                        # no terminal to type it in
    assert deletions(isc) == []
    clean.confirm(4, TENANT, None, ask=lambda prompt: " Delete 4 ", interactive=True)
    with pytest.raises(clean.UsageError, match="not confirmed"):
        clean.confirm(4, TENANT, None, ask=lambda prompt: "yes", interactive=True)


def test_safeguards(tmp_path, monkeypatch):
    isc = tenant()
    other = write_plan(tmp_path, tenant="https://other.api.identitynow.com")
    assert run(tmp_path, monkeypatch, isc, "--apply", "--confirm", "4", plan=other) == 2
    assert run(tmp_path, monkeypatch, isc, "--apply", "--confirm", "4", "--max", "3") == 2
    failing = tenant(export=lambda id: Download(403, b'{"message": "forbidden"}'))
    assert run(tmp_path, monkeypatch, failing, "--apply", "--confirm", "4") == 2     # no backup, no deletion
    assert deletions(isc) == [] and deletions(failing) == []
    with pytest.raises(SystemExit):
        clean.main(["externals", write_plan(tmp_path), "--log-dir", ""])            # --env is required


def test_a_permission_error_stops_the_run(tmp_path, monkeypatch):
    def refuse(id):
        raise ApiError(403)

    isc = tenant(delete_record=refuse)
    assert run(tmp_path, monkeypatch, isc, "--apply", "--confirm", "4") == 1
    assert deletions(isc) == ["rec-ivy"]                                    # stopped at the first refusal
    results = {name: row["Result"] for name, row in audit(tmp_path, "deleted.csv").items()}
    assert results == {"ivy": clean.FAILED}


def test_accounts_already_gone_and_failures(tmp_path, monkeypatch):
    def gone(id):
        raise ApiError(404)

    def broken(id):
        raise ApiError(500)

    isc = tenant(delete_record=gone, delete_account=broken)
    assert run(tmp_path, monkeypatch, isc, "--apply", "--confirm", "4") == 1
    results = {name: row["Result"] for name, row in audit(tmp_path, "deleted.csv").items()}
    assert results == {"ann": clean.NOT_FOUND, "ivy": clean.NOT_FOUND, "jo": clean.FAILED,
                       "kim": clean.FAILED}


def test_an_empty_plan(tmp_path, monkeypatch):
    isc = tenant()
    assert run(tmp_path, monkeypatch, isc, "--apply", plan=write_plan(tmp_path, deletions=[])) == 0
    assert deletions(isc) == []
