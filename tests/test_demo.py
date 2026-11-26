"""The demo builds every report from fictional data, end to end."""

import os

from isc_reporter import cli


def test_demo_writes_every_report(tmp_path):
    out = tmp_path / "demo"
    assert cli.main(["demo", "-o", str(out), "--log-dir", ""]) == 0
    written = {os.path.relpath(os.path.join(d, f), out).replace("\\", "/")
               for d, _, files in os.walk(out) for f in files}
    for report in ("search-identities", "campaigns", "identity-changes", "mappings"):
        assert {f"{report}.xlsx", f"{report}.pdf"} <= written
    assert {"source-migration/source-migration.xlsx", "source-migration/source-migration.pdf",
            "source-migration/attribute-mapping.csv"} <= written
    assert {"sync-impact.xlsx", "sync-impact.pdf", "sync-impact.csv"} <= written
    assert "mappings.html" in written
    assert {"transform-analysis/transform-analysis.xlsx",
            "transform-analysis/optimized/changes.csv"} <= written
    assert {"leavers.xlsx", "leavers.pdf", "leavers.csv"} <= written
    assert {"attribute.xlsx", "attribute.pdf", "attribute.csv"} <= written
    assert {"externals.xlsx", "externals.pdf", "externals.csv", "externals.plan.json"} <= written
    assert {"role-impact.xlsx", "role-impact.pdf", "role-impact.csv"} <= written
    assert {"sources.xlsx", "sources.pdf", "sources.csv"} <= written
    for path in written:
        if path.endswith(".pdf"):
            assert (out / path).read_bytes()[:4] == b"%PDF"


def test_demo_forecast_shows_every_kind_of_update():
    import random

    from isc_reporter import demo, sync_impact

    accounts, identities = demo.sync_accounts(random.Random(11), demo.identities(random.Random(7)))
    mappings, ignored = sync_impact.policy_mappings(demo.sample_policies())
    result = sync_impact.analyse("Active Directory", accounts, identities, mappings, ignored)
    kinds = set().union(*(stats.kinds for stats in result.stats.values()))
    assert kinds == {sync_impact.ADD, sync_impact.CHANGE, sync_impact.CLEAR, sync_impact.CASE}
    assert result.uncorrelated == 3 and result.verdict == "Bulk update expected"
    assert [field["Field"] for field in ignored] == ["sAMAccountName", "password"]


def test_demo_externals_shows_every_status_and_reason():
    import random
    from datetime import date

    from isc_reporter import demo, externals

    data = demo.sample_externals(random.Random(19), demo.identities(random.Random(7)), date(2026, 10, 15))
    result = externals.analyse(data)
    assert {row.status for row in result.rows} == set(externals.STATUSES)
    reasons = {label for row in result.rows for label, _ in row.reasons}
    assert reasons == {externals.IDENTITY_DATE, externals.NOT_AUTHORITATIVE, externals.NO_RECORD,
                       externals.UNREADABLE}
    deleted = [row for row in result.rows if row.status == externals.TO_DELETE]
    assert {label for row in deleted for label, _ in row.warnings} == set(externals.LEFT_BEHIND)
    kinds = {row.source.kind for row in result.rows if row.status == externals.TO_DELETE}
    assert kinds == {externals.NON_EMPLOYEE, externals.DELIMITED}
    assert any(r["Enabled"] == "yes" for r in externals.uncorrelated_rows(result))
    assert any("Uncorrelated" in note for row in result.rows for note in row.notes)
    counts = [result.counts[s.id] for s in data.sources]
    assert sum(c["No end date"] for c in counts) and sum(c["End date to come"] for c in counts)


def test_demo_attribute_report_finds_each_kind_of_problem():
    import random

    from isc_reporter import attribute_report, demo

    data = demo.sample_attributes(random.Random(23), demo.identities(random.Random(7)))
    result = attribute_report.analyse(data)
    found = {(row["Attribute"], row["Check"]) for row in result.quality}
    assert {("department", "Variants of the same value"), ("department", "Placeholder values"),
            ("department", "Rare values"), ("department", "Empty although mapped"),
            ("department", "Extra spaces"), ("department", "Not mapped in their profile"),
            ("employeeNumber", "Duplicates"),
            ("email", "Invalid e-mail addresses"), ("cloudLifecycleState", "Not a lifecycle state")} <= found
    assert {row["Type"] for row in result.usages if row["Attribute"] == "department"} >= {"Role", "Segment"}


def test_demo_role_impact_shows_every_outcome():
    import random

    from isc_reporter import demo, role_impact

    data = demo.sample_role_impact(random.Random(17), demo.identities(random.Random(7)))
    result = role_impact.analyse(data)
    assert set(result.outcomes) == set(role_impact.OUTCOMES)
    assert result.new_accounts and any(row["Privileged"] == "yes" for row in result.provisioning)
