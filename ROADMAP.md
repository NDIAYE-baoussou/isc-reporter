# Roadmap

isc-reporter aims to be the toolkit a SailPoint ISC engineer can try in demo mode, trust on a real
tenant, and extend. This page gives the direction, not dates. Comments and ideas are welcome in
the [issues](https://github.com/NDIAYE-baoussou/isc-reporter/issues).

## Done

- **Reports**: search, certification campaigns, identity attribute changes, identity profile
  mappings with where each attribute is used, leavers who still have access.
- **Change impact**: source migration analysis with draft JSON and the role, segment and
  correlation criteria it affects; attribute synchronization forecast.
- **Trust**: read-only by design, a demo of every command on fictional data, tests that need no
  tenant, continuous integration, automated releases to PyPI, a security policy, and the
  permissions of each command.

## Next: findings and JSON output

- One format for every finding, whichever command produces it: check id, severity, resource,
  explanation, evidence, recommendation. `identity-profiles`, `migration` and `analyze-before-sync` already
  produce checks; they would share it.
- `--format json`, so results can feed scripts and other tools, and a short summary in the
  terminal.
- An option to end with a failure exit code when a check fails (for example `--fail-on warning`),
  so checks can run in a pipeline.
- Validation of each command on more real tenants, to move it from Beta to Stable.

## Later: a diagnostic toolkit

- More configuration checks: account correlation, lifecycle states, unused or broken transforms,
  provisioning policies.
- A simple way to add a check (one function, its test and its documentation), so that the
  community can contribute checks without knowing the reporting code.

## Later: operational intelligence

- Compare configuration exports between environments, such as sandbox and production.
- Compare two runs of a report to see what changed in between.
- Migration readiness assessments.
- Audit evidence packages: the reports with the queries used, the collection time and a manifest.

## How to help

Tell us which of these matters most to you, or take one: open an issue to discuss it first, then
see [CONTRIBUTING.md](CONTRIBUTING.md). Trying a command on your tenant and reporting what you
found is as useful as code.
