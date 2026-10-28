# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [semantic versioning](https://semver.org/).

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
