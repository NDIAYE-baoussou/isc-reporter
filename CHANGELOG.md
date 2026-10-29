# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

## [0.3.0] - 2026-10-29

- `iscr`: a short name for the command. `iscr <command>` and `python -m iscr <command>` work like
  `isc-reporter <command>` and `python -m isc_reporter <command>`, which keep working.
- `profiles` is renamed `identity-profiles`. The old name still works, with a warning, and will be
  removed in a future version.
- `leavers`: identities in a leaver lifecycle state that still have enabled accounts, with the cause
  for each account (the lifecycle state should have disabled it, or does not cover its source),
  days since leaving and a grace period, by source and by lifecycle state.
- `migration`: new *Criteria* sheet. Role and dimension membership criteria, segment visibility,
  and account and manager correlation of the sources that use an identity attribute computed from
  the old source, or that read the old source itself, with the action for each. Account attributes
  of the old source used by these criteria are added to `attribute-mapping.csv`. Read from the
  tenant, or from the export with `--from`.
- `analyze-before-sync`: when nothing can be compared, the error now says which policies the source
  has and what each field does instead of copying an identity attribute.
- Fixed: values from the SDK's string enums (role criteria key types, for example) were passed on
  as enum objects, whose text form is `Type.VALUE`; they are now plain values.
- Fixed: `migration` matches the attribute mapping case-insensitively when it is given in code (the
  demo's `managerId` mapping was ignored).
- `demo` also writes the attribute synchronization forecast, from fictional Active Directory
  accounts.
- README reorganised for people discovering the project: use cases, commands with their maturity,
  a quick start, and safety, permissions and privacy first. Images and links now also work on PyPI.
- `docs/permissions.md`: the endpoints, scopes and user levels each command needs.
- `ROADMAP.md` and `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1).

## [0.2.0] - 2026-10-28

- `analyze-before-sync` (alias `analyzebeforesync`): forecast the bulk updates ISC could send to the
  target if attribute synchronization is enabled on a source, from its provisioning policy (and
  optional `--map` / `--map-file` attribute pairs).

## [0.1.1] - 2026-10-27

- `profiles`: for every identity attribute, where it is used: other mappings, named transforms and
  source provisioning policies (new *Attribute usage* and *Usage detail* sheets, *Also used in* column).

## [0.1.0] - 2026-10-27

First public version.

- `search`: reports on any search index, one line per object with nested access consolidated,
  summary charts, optional per-item detail, saved report definitions.
- `campaigns`: certification campaign dashboard (progress, decisions by access type, reviewers,
  overdue certifications, reviewed items).
- `changes`: identity attribute changes with old and new values over a period.
- `profiles`: identity profile mapping documentation with checks.
- `migration`: source replacement impact analysis with draft JSON.
- `demo`: every report from fictional data.
