"""The demo builds every report from fictional data, end to end."""

import os

from isc_reporter import cli


def test_demo_writes_every_report(tmp_path):
    out = tmp_path / "demo"
    assert cli.main(["demo", "-o", str(out), "--log-dir", ""]) == 0
    written = {os.path.relpath(os.path.join(d, f), out).replace("\\", "/")
               for d, _, files in os.walk(out) for f in files}
    for report in ("search-identities", "campaigns", "identity-changes", "identity-profiles"):
        assert {f"{report}.xlsx", f"{report}.pdf"} <= written
    assert {"source-migration/source-migration.xlsx", "source-migration/source-migration.pdf",
            "source-migration/attribute-mapping.csv"} <= written
    for path in written:
        if path.endswith(".pdf"):
            assert (out / path).read_bytes()[:4] == b"%PDF"
