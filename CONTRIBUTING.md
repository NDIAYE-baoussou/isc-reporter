# Contributing

Thanks for helping! Bug reports, ideas and pull requests are welcome. Looking for something to
work on? See the [roadmap](ROADMAP.md). Everyone taking part follows the
[Code of Conduct](CODE_OF_CONDUCT.md).

## Reporting a problem

Open an issue with the command you ran, what you expected and what happened, and the relevant
lines of the log (`logs/`, or `--log-level DEBUG`). **Never attach real reports, logs with
personal data, or credentials**: replace names, emails and ids with fictional ones.

## Making a change

1. Create a virtual environment and install the project with its development tools:
   `pip install -e ".[dev]"`.
2. Make your change with tests. Tests need no tenant: `tests/conftest.py` provides `FakeApi`
   and `FakeIsc`, and the real SailPoint SDK models are used to check what would be read.
3. Run `pytest` and `ruff check src tests`.
4. If the change is visible to users, add a line to `CHANGELOG.md` and update the README. If a
   command calls a new API endpoint, add it to `docs/permissions.md` with its scope.
5. Open a pull request describing what changes and why.

## Design notes

- Commands only **read** from the tenant. Anything that changes a tenant is out of scope.
- Reading (`fetch` functions) is separate from analysis (plain dicts in, rows out) and from
  rendering (`dashboard.Dashboard` describes a report; `dashboard.write_xlsx` / `write_pdf`
  render it), so most code is tested without the SDK.
- Excel files are written with XlsxWriter and PDFs with reportlab.
