# Changelog

All notable changes are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/)
and the project uses [semantic versioning](https://semver.org/).

## [Unreleased]

- `migration`: everything that depends on the source to replace, with what to do for each object.
  - `--new-source` is now optional: when the new source is not chosen yet, the report lists the
    impacts and the attributes in use, without drafts.
  - *Impacts* replaces the *Criteria* sheet and adds the roles and dimensions that grant access of
    the old source, its access profiles, SoD policies on its entitlements, lifecycle states,
    provisioning policies of the other sources, workflows and connector rules that name it, the
    rules it runs and password policies. Its columns *Criterion* and *All criteria* are now *Where*
    and *Definition*. The objects are no longer repeated in the *Checks* sheet.
  - New sheets: *Overview* (what to do by type of object), *Identity attributes* (those computed
    from the old source and what uses them) and *Old source* (its configuration to recreate).
  - New checks: *Old source not found*, *New source not chosen*.
- Fixed: `migration` wrote the lifecycle states of a snapshot into the identity profile drafts.
- Fixed: `migration --from` missed the role criteria reading a source that no profile is
  authoritative on, when the export has no schema for it.

## [0.5.0] - 2026-11-02

- `sources`: status and capabilities of every source, one line each. It shows:
  - health and the status of the virtual appliance cluster;
  - how ISC provisions the source: its connector, a service desk integration, manual work items, or
    nothing for an authoritative source it only reads;
  - its features, provisioning policies, account and manager correlation, attribute synchronization
    and aggregation schedule;
  - the last account aggregation, the accounts and the uncorrelated accounts.

  Checks flag sources and clusters in error, failed or old aggregations, a missing schedule or
  correlation, uncorrelated accounts, high delete thresholds and sources that can provision without
  an account create policy. `--source` selects sources by name, and `--quick` reads only the source
  list. The connection settings of the sources are left out.

- `analyze-before-sync`: `--exclude` and `--exclude-file` leave out the attributes that will never
  be synchronised: account or identity attribute names, with `*` and `?` as wildcards, or
  `IDENTITY=ACCOUNT` pairs. They are neither compared nor counted in the verdict, and are listed in
  the new *Excluded* sheet; a name that matches nothing is reported.

- `transforms`: optimization plan for the whole tenant. The new *Optimization plan* sheet lists
  the suggestions in the order to apply them, with their effect, and the number of custom named
  transforms now and after the plan. *Cost by profile* counts the transform steps each identity
  profile runs for one identity and for all its identities, now and with the optimized drafts.
  New checks: `requiresPeriodicRefresh` set on a value that does not change on its own
  (*Performance*), and named transforms that apply the same logic to different inputs, which one
  transform without input can replace (*Same logic*). *Complexity* shows where each named transform
  is used and for how many identities. `requiresPeriodicRefresh` is also read next to the
  attributes of a transform.

- `snapshot`: configuration snapshot. Transforms, identity profiles with their lifecycle states,
  identity attributes, sources with their schemas, provisioning policies and correlation, roles with
  their dimensions, access profiles, segments, connector rules, workflows, SoD policies, governance
  groups and password policies, saved as one JSON file per object. Taken again into the same folder,
  only the files of what changed are rewritten, so the folder can be kept under git as the history of
  the configuration; `snapshot.json` lists the changes since the previous snapshot. Fields that
  change on their own are left out and secrets are replaced by `(redacted)`. `mappings`,
  `transforms` and `migration` run on a snapshot with `--from`; `mappings --from` now takes the
  display names of the identity attributes from the files, and `migration --from` the account schema
  of the sources. Connector rules in exported files are no longer read as transforms.

- `mappings`: lifecycle states. The *Lifecycle states* sheet shows what each state of each profile
  provisions: accounts enabled, disabled or deleted and on which sources, access profiles granted,
  all access removed, who is notified, identities in the state. *Lifecycle by source* shows, per
  source, what each state does to its accounts. New checks: values of the `cloudLifecycleState`
  mapping that are not an enabled state of the profile, leaver states that remove nothing, disabled
  states that still hold identities, unknown sources and access profiles.

- Environments: `env add`, `env list`, `env use`, `env test`, `env remove`. One environment per
  tenant (dev, sandbox, prod...), chosen with `--env` or `ISCR_ENV`, or the current one. The client
  secret is asked for without being shown, checked against the tenant and kept in the credential
  store of the computer (Windows Credential Manager, macOS Keychain, Secret Service), never in a
  file. The `SAIL_*` variables still work, for pipelines. New dependency: `keyring`.
- `config.json` is no longer recommended: it is still read, with a warning, and will be removed.
  `env import config.json --name <name> --delete` moves its credentials into an environment.

- `transforms`: transform analysis with optimized drafts. Every named transform, identity profile
  mapping and provisioning policy field is checked against SailPoint's transform guidance: broken
  references and loops, lookups without default, identity attributes read within profile mappings,
  a transform reading one source used by profiles of other sources, comparisons with `now` not
  refreshed, missing fallbacks, duplicates, copies and repeated blocks, needless steps, unused and
  complex transforms, rules and Velocity logic. The `optimized` folder holds the rewritten transforms,
  profiles and policies for the changes that give the same result, with `changes.csv` and
  `delete.csv`. Offline with `--from`.

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
