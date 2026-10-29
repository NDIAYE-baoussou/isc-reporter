# Permissions

isc-reporter connects with the client ID and secret of a
[personal access token](https://documentation.sailpoint.com/saas/help/common/api_keys.html) and
only calls read endpoints: `GET` requests, plus `POST /search/v1`, which runs a search and changes
nothing. This page lists, for each command, the API endpoints it calls, the scopes they require and
the user levels allowed to call them.

The scopes and user levels come from SailPoint's API specification
([sailpoint-oss/api-specs](https://github.com/sailpoint-oss/api-specs), `idn/apis/*/paths`), read
on 2026-10-28. SailPoint can change them: the API reference is authoritative. For every endpoint,
the specification also lists the matching `:manage` scope next to the `:read` one shown here.

## By command

| Command | User levels allowed | Scopes for a token restricted to what the command needs |
| --- | --- | --- |
| `demo`, `types` | no tenant access | none |
| `search` | `ORG_ADMIN`, `CERT_ADMIN`, `REPORT_ADMIN`, `SOURCE_ADMIN`, `SOURCE_SUBADMIN`, `ROLE_ADMIN`, `ROLE_SUBADMIN` | `sp:search:read` |
| `campaigns` | `ORG_ADMIN`, `CERT_ADMIN` (also `REPORT_ADMIN` with `--no-summaries` and without `--items`) | `idn:campaign:read`, `idn:campaign-filter:manage` (see below) |
| `changes` | the user levels of `search` (identity history lists none) | `sp:search:read`, `idn:identity-history:read` |
| `identity-profiles` | `ORG_ADMIN` | `idn:identity-profile:read`, `idn:transform:read`, `idn:identity-profile-attribute:read`, `idn:sources:read`, `idn:provisioning-policy:read` |
| `identity-profiles --from` | no tenant access (reads exported JSON) | none |
| `migration` | `ORG_ADMIN` | `idn:identity-profile:read`, `idn:transform:read`, `idn:identity-profile-attribute:read`, `idn:sources:read`, `idn:source-schema:read`, `idn:role-unchecked:read`, `idn:segment:read` |
| `migration --from` | no tenant access (reads exported JSON) | none |
| `leavers` | `ORG_ADMIN` | `idn:identity-profile:read`, `idn:identity-profile-lifecycle-state:read`, `idn:sources:read`, `sp:search:read` |
| `analyze-before-sync` | `ORG_ADMIN` | `idn:sources:read`, `idn:provisioning-policy:read`, `idn:accounts:read`, `sp:search:read` |
| `analyze-before-sync --only-map` | `ORG_ADMIN`, `SOURCE_ADMIN`, `SOURCE_SUBADMIN` | `idn:sources:read`, `idn:accounts:read`, `sp:search:read` |

`ORG_ADMIN` is the *Admin* user level. A token of an Admin can run every command. For the other
user levels, the column lists those allowed by every endpoint the command calls, where the
specification states them.

**Campaign filters.** For listing campaign filters (the *Filters* sheet of `campaigns`), the
specification lists only `idn:campaign-filter:manage`: there is no read scope for it. The call
itself is a `GET` and changes nothing.

**Provisioning policies in `identity-profiles`.** They are read source by source; a source whose policies
cannot be read is skipped with a warning, and the rest of the report is still produced.

**Criteria in `migration`.** Roles, segments and the correlation of the sources are read type by
type. A type that cannot be read (for example without `idn:segment:read`) is reported in the
*Checks* sheet, and the rest of the analysis is still produced.

## By endpoint

| Endpoint | Used by | Scope | User levels in the specification |
| --- | --- | --- | --- |
| `POST /search/v1` | `search`, `changes`, `analyze-before-sync`, `leavers` | `sp:search:read` | `ORG_ADMIN`, `CERT_ADMIN`, `REPORT_ADMIN`, `SOURCE_ADMIN`, `SOURCE_SUBADMIN`, `ROLE_ADMIN`, `ROLE_SUBADMIN` |
| `GET /historical-identities/v1/{id}/events` | `changes` | `idn:identity-history:read` | not stated |
| `GET /campaigns/v1` | `campaigns` | `idn:campaign:read` (or `idn:campaign-report:read`) | `ORG_ADMIN`, `CERT_ADMIN`, `REPORT_ADMIN` |
| `GET /certifications/v1` | `campaigns` | `idn:campaign:read` | not stated |
| `GET /certifications/v1/{id}/decision-summary` | `campaigns` (not with `--no-summaries`) | `idn:campaign:read` | `ORG_ADMIN`, `CERT_ADMIN` |
| `GET /certifications/v1/{id}/access-review-items` | `campaigns --items` | `idn:campaign:read` | `ORG_ADMIN`, `CERT_ADMIN` |
| `GET /campaign-filters/v1` | `campaigns` | `idn:campaign-filter:manage` | not stated |
| `GET /identity-profiles/v1` | `identity-profiles`, `migration`, `leavers` | `idn:identity-profile:read` | `ORG_ADMIN`, `API` |
| `GET /transforms/v1` | `identity-profiles`, `migration` | `idn:transform:read` | `ORG_ADMIN` |
| `GET /identity-attributes/v1` | `identity-profiles`, `migration` | `idn:identity-profile-attribute:read` | not stated |
| `GET /sources/v1` | `identity-profiles`, `migration`, `analyze-before-sync`, `leavers` | `idn:sources:read` | `ORG_ADMIN`, `SOURCE_ADMIN`, `SOURCE_SUBADMIN`, `ROLE_SUBADMIN` |
| `GET /sources/v1/{sourceId}/schemas` | `migration` | `idn:source-schema:read` | `ORG_ADMIN`, `SOURCE_ADMIN`, `SOURCE_SUBADMIN` |
| `GET /sources/v1/{sourceId}/provisioning-policies` | `identity-profiles`, `analyze-before-sync` | `idn:provisioning-policy:read` | `ORG_ADMIN` |
| `GET /identity-profiles/v1/{identity-profile-id}/lifecycle-states` | `leavers` | `idn:identity-profile-lifecycle-state:read` | `ORG_ADMIN` |
| `GET /sources/v1/{id}/correlation-config` | `migration` | `idn:sources:read` | `ORG_ADMIN`, `SOURCE_ADMIN`, `SOURCE_SUBADMIN` |
| `GET /roles/v1` | `migration` | `idn:role-unchecked:read` (or `idn:role-checked:read`) | `ORG_ADMIN`, `ROLE_ADMIN`, `ROLE_SUBADMIN` |
| `GET /roles/v1/{roleId}/dimensions` | `migration` (dynamic roles) | `idn:role-unchecked:read` (or `idn:role-checked:read`) | `ORG_ADMIN`, `ROLE_ADMIN`, `ROLE_SUBADMIN` |
| `GET /segments/v1` | `migration` | `idn:segment:read` | `ORG_ADMIN` |
| `GET /accounts/v1` | `analyze-before-sync` | `idn:accounts:read` | `ORG_ADMIN`, `SOURCE_ADMIN`, `SOURCE_SUBADMIN`, `HELPDESK` |

## Load on the tenant

- Searches are paged with `searchAfter`, so results beyond 10,000 are complete.
- Throttled calls (HTTP 429), gateway errors and network errors are retried (6 attempts in all),
  after the delay given by the `Retry-After` header, or with an increasing delay.
- Some commands make one call per object. `changes` reads the history of each modified identity,
  and `campaigns` reads each certification's decision summary (and its items with `--items`).
  `analyze-before-sync` reads every account of the source, then runs one search per 100 of the
  identities they belong to. `migration` reads the correlation of each source and the dimensions
  of each dynamic role, one call each.
  Narrow large tenants with each command's options (`--query`, `--max-identities`, `--campaign`,
  `--no-summaries`...).
