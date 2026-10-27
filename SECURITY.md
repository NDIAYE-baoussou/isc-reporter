# Security policy

If you find a security problem (for example credentials ending up in a log or a report), please
**do not open a public issue**. Use GitHub's private vulnerability reporting ("Report a
vulnerability" in the Security tab of the repository) and give enough detail to reproduce it.

isc-reporter only reads from a tenant, keeps credentials out of its logs and reports, and
verifies TLS certificates through the SailPoint SDK. Use a token with read-only rights.
