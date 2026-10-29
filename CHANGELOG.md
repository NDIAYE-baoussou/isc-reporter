# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

## [0.4.1] - 2026-10-29

- Fixed: `mappings` stopped with `KeyError: 't:?'` when a reference transform has no `id`. Such a
  reference is now shown as *(reference without id)*, a transform that cannot be found, and the
  *Checks* sheet flags it (*Reference without id*). Transforms whose attributes are not an object no
  longer stop the report either.
- The SDK's warnings about values it does not know yet (new source features, for example) are no
  longer printed.

## [0.4.0] - 2026-10-29

- `role-impact`: what enabling a role would do. The identities its membership criteria select (or
  its identity list), the entitlements ISC would provision to them and the accounts it would
  create, who would lose the role, who has it outside the criteria, and who holds all of its access
  without matching. `--from` tests a draft role with new criteria before it is saved.
- `identity-profiles` is renamed `mappings`. `identity-profiles` and `profiles` still work, with a
  warning, and will be removed in a future version. The reports are named `mappings_<date>` and
  titled *Identity attribute mappings*.
- `mappings`: dependencies between named transforms. The new *Transform dependencies* sheet draws,
  for each identity attribute that uses a named transform, the tree of what it depends on: the
  transforms it calls, and the identity attributes they read, computed in turn by other transforms.
  The named transforms that nothing uses are listed too. The *Transforms* sheet lists every named
  transform, used or not, with what it depends on; the PDF has the same trees.
- `mappings`: interactive map in `mappings.html`, a page that works offline. Every named transform
  and the identity attributes that link them, each one after what it depends on; selecting one
  lights up its whole chain. Step-by-step diagrams of each identity attribute and named transform,
  with zoom, folding, search and SVG export. Written by default with the Excel and PDF files
  (`--format`).
- `demo` also writes the role enablement impact and `mappings.html`, and its named transforms now
  depend on each other.

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
