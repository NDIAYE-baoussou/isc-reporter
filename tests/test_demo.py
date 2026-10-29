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
    assert {"leavers.xlsx", "leavers.pdf", "leavers.csv"} <= written
    assert {"role-impact.xlsx", "role-impact.pdf", "role-impact.csv"} <= written
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


def test_demo_role_impact_shows_every_outcome():
    import random

    from isc_reporter import demo, role_impact

    data = demo.sample_role_impact(random.Random(17), demo.identities(random.Random(7)))
    result = role_impact.analyse(data)
    assert set(result.outcomes) == set(role_impact.OUTCOMES)
    assert result.new_accounts and any(row["Privileged"] == "yes" for row in result.provisioning)
