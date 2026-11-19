# Security policy

If you find a security problem (for example credentials ending up in a log or a report), please
**do not open a public issue**. Use GitHub's private vulnerability reporting ("Report a
vulnerability" in the Security tab of the repository) and give enough detail to reproduce it.

`isc-reporter` (`iscr`) only reads from a tenant, keeps credentials out of its logs and reports,
and verifies TLS certificates through the SailPoint SDK. Use a token with read-only rights for it,
and save it with `isc-reporter env add`: the client secret then goes to the credential store of the computer
(Windows Credential Manager, macOS Keychain, Secret Service), not to a file. Avoid `config.json`
files, which hold the secret in clear text; `isc-reporter env import config.json --name <name>
--delete` moves one to the credential store and deletes it.

`iscr-clean` is a separate command and the only one that changes a tenant: it deletes the accounts
listed in a plan of `isc-reporter externals`, after checking each one again, and only with
`--apply` and a typed confirmation. Give it its own environment, with a token that has only the
delete rights it needs (see [docs/permissions.md](docs/permissions.md)), so that the token of
`iscr` stays read-only. A way to make `iscr` change a tenant, or `iscr-clean` delete something
that is not in the plan, is a security problem: report it privately, as above.

`isc-reporter snapshot` saves the configuration of the tenant to local files and replaces the
secrets it recognises (passwords, tokens, client secrets, API keys, encrypted connector attributes,
static values of password fields) by `(redacted)`. A secret it does not recognise and that is left
in a snapshot is a security problem: report it privately, as above, without the value.
