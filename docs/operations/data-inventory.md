# Data inventory — personal and tenant data, and how each is erased (P6-PRIV-1)

**Status:** implementation record for `P6-PRIV-1` (branch `feat/p6-privacy-deletion-01`).
It describes what the code does; it is not a legal retention policy. Retention
**durations** are an operator/legal decision (`O-5` / `P6-D05`, undecided) — see
[Retention durations](#retention-durations-undecided) below.

**Derived from the code**, not from earlier documents: every table below is a SQLAlchemy
model registered in `apps/api/app/db/models.py` (43 tables, all of them listed here); the
column lists and foreign-key delete rules were read from `Base.metadata` on the branch
base `origin/main` `3b0d9e36`. The machine-checked counterpart is
`apps/api/app/privacy/inventory.py`; `apps/api/app/tests/test_privacy_inventory.py` fails
if a table is added without an inventory entry, if a foreign key is added whose delete
rule the erasure walk cannot follow, or if this document stops naming a table.

## Data subjects

| Subject | Where they appear |
|---|---|
| **Account holder** (a registered user) | `users` and everything keyed to `users.id` |
| **Invitee who never registered** | `organization_invitations.email`; invitation audit states (`audit_logs.previous_state` / `new_state`) |
| **Third parties in collected signals** | authors and text of scraped/fixture content (`raw_signals`, `normalized_signals`, intelligence/opportunity evidence) — removal of a *specific* third party is the takedown path of `P6-PRIV-4` (REALDATA lane), not this row |
| **Customer organization** (tenant; usually a business, not a natural person) | every table keyed to `organizations.id` / `workspaces.id`; may contain personal data of sole traders (phone, address) |

## Erasure mechanisms (what the code implements)

| Mechanism | Entry point | Effect |
|---|---|---|
| **Account deletion** (self-serve, re-authenticated with the current password and the confirmation `DELETE`) | `POST /api/v1/account/delete` → `app.privacy.erasure.delete_account` | the user row and every row that cascades from it are deleted; attribution columns are set to NULL; audit records that name the account are anonymised; the invitations the account **accepted** (`accepted_by_user_id`) are deleted, and its address cleared from the audit records about them, whether or not it is verified; **only when the account's email address is verified** (`email_verified_at`) are the other invitations addressed to that address deleted and other audit values naming it anonymised too, and the receipt says which (`email_keyed_data`); an occurrence counts only when the shared matcher (`app.privacy.matching`) finds it `WHOLE`; organizations in which the account is the **only member** are deleted in full; deletion is **refused** (409 `account_deletion_sole_owner`) while the account is the only OWNER of an organization that has other members |
| **Organization deletion** (exact role OWNER, re-authenticated, organization name typed) | `POST /api/v1/organizations/{id}/delete` → `app.privacy.erasure.delete_organization` | every row reachable from the organization through the foreign-key graph is deleted (workspaces, all tenant content, jobs and job events, members, invitations); the organization's audit records are **anonymised and retained** (see `audit_logs`); refused (409 `organization_jobs_active`) while a job of the organization is claimed or running |
| **Invitee-email erasure** (operator, for a person with no account) | `python -m app.privacy.cli erase-email` → `app.privacy.erasure.erase_invitee_email` | invitations addressed to the email are deleted and audit records naming it anonymised; refused when an account with that **verified** email holds it (use account deletion); an unverified account holding it does not block it |
| **Retention purge** (per data class, bounded batches) | `python -m app.privacy.cli sweep` (+ the `retention.purge` durable job per workspace) → `app.privacy.retention` | rows older than the class's configured duration are deleted with their dependents; **a class whose duration is not configured is refused and nothing is deleted** |
| **Independent verification** (E10) | `python -m app.privacy.cli verify-account` / `verify-organization` → `app.privacy.verification`; every deletion response carries its **reference check** only | reflects the **live database schema** (not the inventory, not the deletion code, not audit records) and reports every remaining reference to the subject and every text/JSON value naming its identifiers `WHOLE` by the deletion's own matcher (`app.privacy.matching`), in any form: NFC/NFD, another letter case (one shared with a twin address the product holds is flagged instead), JSON escapes (up to three layers), percent-encoding (one layer), IDNA A-/U-label and other UTS46 domain spellings. It lists `AMBIGUOUS` or undecided occurrences, a letter case shared with a twin address the product holds included, for operator review, exit `4` (interim, pending `D-PRIV-7`); exactly the texts described under "Not detected" in `privacy-operations.md` §1 pass unseen; the text/JSON scan reads every text column, so it runs in the operator command, not in the request |

The deletion code does **not** rely on the database's `ON DELETE` actions firing (SQLite
only enforces them with `PRAGMA foreign_keys=ON`): `app.privacy.erasure.delete_rows`
walks the declared foreign keys itself and issues the child `DELETE` (for `CASCADE`) or
`UPDATE … SET NULL` (for `SET NULL`) statements explicitly, children first, in one
transaction. A foreign key with any other delete rule makes the walk raise, and a test
proves no such key exists.

## Table inventory

Legend — **On account deletion** / **On organization deletion**: `delete` (row removed),
`set null` (the named column is cleared, row kept), `anonymise` (identifying columns
cleared, row kept), `—` (not affected). **Retention class**: the time-based purge that
applies to the table's rows (durations undecided; see below).

### Account and credential data (natural persons)

| Table | Personal / identifying columns | On account deletion | On organization deletion | Retention class |
|---|---|---|---|---|
| `users` | `email`, `full_name`, `hashed_password` (credential), `email_verified_at`, `is_active`, `is_operator`, `auth_epoch`, timestamps | delete | — (members' accounts survive; see [Members of a deleted organization](#members-of-a-deleted-organization)) | none — kept while the account exists |
| `organization_members` | `user_id`, `organization_id`, `role` | delete | delete | none |
| `auth_sessions` | `user_id`, `created_at`, `expires_at`, `revoked_at` (no IP, no user agent) | delete | — | `auth_sessions` (by `expires_at`) |
| `password_reset_tokens` | `user_id`, `token_hash` (digest only) | delete | — | `account_tokens` (by `expires_at`) |
| `email_verification_tokens` | `user_id`, `token_hash`, `email_snapshot` | delete | — | `account_tokens` (by `expires_at`) |
| `organization_invitations` | `email` (invitee — may never register), `invited_by_user_id`, `accepted_by_user_id`, `token_hash`, `role` | rows the account accepted: delete; other rows addressed to the account's email (case-insensitive), **only if that address is verified**: delete; `invited_by_user_id` / `accepted_by_user_id`: set null | delete | `invitations` (accepted, revoked or expired rows, by the latest of those times) |

### Compliance and attribution records

| Table | Personal / identifying columns | On account deletion | On organization deletion | Retention class |
|---|---|---|---|---|
| `audit_logs` | `actor_user_id`; `entity_id` (a user id for member and registration audits); `reason` (free text); `previous_state` / `new_state` (invitation audits carry the invitee `email`, `accepted_by_user_id`); `context` | anonymise every row that names the account: `actor_user_id`/`entity_id` equal to its id → NULL; `reason` containing its id or email → NULL; any JSON string value equal to its id or containing its email → `"[erased]"` | anonymise and **retain** (`actor_user_id`, `entity_id`, `reason`, `previous_state`, `new_state` → NULL, `context` → `{}`; `organization_id`, `workspace_id`, `action`, `entity_type` and timestamps kept) — follows the Phase-5 resolution `O-6` (`REC_compliance_records_survive_FKless_scope_columns`, rider: survival is subject to `O-5` and must not mean indefinite retention by default) | `audit_logs` (by `created_at`) |
| `source_takedown_cases` | **restricted operator case record** of each live-source takedown request (`P6-PRIV-4`, operator decision `D-9e`, 2026-10-27): the operator's free-text `reason` and own `external_reference` (private; either may name a person or legal matter), the acting operator `actor_user_id`, `source_key`, the opaque case id (the only reference customer-visible audit entries carry) and `created_at` | `actor_user_id`: set null (the case is kept) | not tenant-keyed (platform-wide); not touched | **none — retention duration pending operator decision (D-9e access/retention policy)**; no time-based class and no job deletes these rows. Operator free text (reason, external_reference) is not reached by account erasure: if it names a person who is later erased, the text remains and the E10 content verifier reports findings on this table; this is pending the operator's access/retention/redaction decision (D-9e). Not exported (`exported=False`); read only through the operator-only `GET /api/v1/internal/system/data-sources/{source_key}/cases`; never in a tenant API, export, log or metric |
| `opportunity_feedback` | `submitted_by_user_id` | set null | delete | follows `opportunities` / `signal_evidence` (dependent rows) |
| `workspace_capability_overrides` | `set_by_user_id`, `reason` (operator note) | set null (`set_by_user_id`) | delete | none |

### Tenant business content (customer-provided; may contain personal data of sole traders)

All of these are deleted on organization deletion and untouched by account deletion. None
has a time-based purge: they live as long as the workspace (they are the customer's
configuration).

| Table | Columns that can hold personal or confidential data |
|---|---|
| `organizations` | `name`, `slug` |
| `workspaces` | `name`, `slug` |
| `brands` | `name`, `industry`, `business_type` |
| `business_profiles` | `company_name`, `website`, `alternative_presence`, `social_links`, `google_business_profile`, `marketplace_links`, `description`, `target_audience`, `ideal_customer_profile`, `compliance_notes`, `sensitive_topics`, `pricing_model`, `buying_process`, … (every free-text column) |
| `business_locations` | `address`, `city`, `state_province`, `postal_code`, `latitude`, `longitude`, `local_phone`, `local_website_page`, `local_social_links`, `google_business_profile`, `local_competitors`, `local_notes`, `overrides` |
| `geo_coverage_rules` | `business_address`, `center_latitude`, `center_longitude`, markets |
| `product_profiles`, `audience_profiles`, `competitor_profiles`, `brand_voice_profiles`, `offer_calendar`, `claims_library`, `source_preferences`, `channel_preferences`, `campaigns` | free-text and JSON business context (competitor names and websites are third-party business data; `offer_calendar.promo_code`, `brand_voice_profiles.example_copy`) |
| `scout_requests` | `name`, `keywords`, `notes`, `resolved_market`, `stats` |
| `scout_schedules` | scheduling metadata only |

### Collected signals and generated results (third-party content and derived insight)

Deleted on organization deletion. Time-based purge by the two scouting classes. When a
root row is purged, its dependents go with it through the foreign-key walk (for example,
purging a `raw_signals` row deletes its `normalized_signals`, their location evidence and
intelligence records, and feedback given on those records).

| Table | Columns that can hold third-party personal data | Retention class (root) |
|---|---|---|
| `raw_signals` | `author`, `content`, `source_url`, `raw_metadata` | `signal_evidence` (by `created_at`) |
| `normalized_signals` | `author`, `excerpt`, `source_url`, `ingest_metadata`, `embedding` (derived vector) | dependent of `raw_signals` |
| `signal_location_evidence` | `evidence`, `resolved_market` | dependent of `normalized_signals` |
| `signal_clusters` | `label`, `centroid` (derived) | `signal_evidence` (by `created_at`) |
| `signal_intelligence_records` | `facts`, `inference`, `provenance`, `rationale` | dependent of `normalized_signals` |
| `opportunities` | `title`, `observed_evidence`, `source_summary`, `why_it_matters`, `who_cares`, `ai_inference`, `recommended_action`, …; `finding_key` (W16 customer repairs, PROPOSED: the finding's identity within its Scout, a SHA-256 digest over its member items' source types, source identities and content digests; no content of its own; NULL on cards created before it existed, never back-filled) | `opportunities` (by `created_at`) |
| `opportunity_observations` | **none** (W16 customer repairs, PROPOSED): one row per sighting of a finding by a run (`first_seen` / `seen_again`), with the card, the Scout, the run's job and cluster (`job_id` / `cluster_id` set null when those rows go), `observed_at` and timestamps; ids, a kind and instants only, no content; exported with the organization | dependent of `opportunities` |
| `opportunity_scores` | score breakdown only | dependent of `opportunities` |
| `validation_evidence` | `detail`, `source_url` | dependent of `opportunities` |

### Operational records

| Table | Content | On organization deletion | Retention class |
|---|---|---|---|
| `jobs` | tenant scope ids, `payload` (entity ids plus the run's control codes: `run_fence`, `trigger`, `prior_status`; no content), `result_summary` (counts), `last_error_summary` (bounded, secret-free) | delete | `job_history` (terminal jobs, by `updated_at`) |
| `scout_admissions` | **operational control state** (D-25 execution controls, PROPOSED): one row per admitted external operation of a Scout run: `scout_request_id`, `organization_id`, `workspace_id`, `job_id` (set null when the job row goes), `attempt`, `worker_id`, `operation` (fetch or model), `attempt_key`, `admitted_at`, `ended_at`, `end_kind` (known or unknown), `resolved_at`, `resolved_by` (an operator name), `resolution_note` (a reason code plus at most 200 characters), `deadline_at` with its basis `deadline_rule` / `deadline_client_seconds` / `deadline_lease_seconds` (D-25 recovery, PROPOSED: the instant after which an admission with no recorded end becomes eligible for reconciliation, and the configuration seconds it was computed from; `unrecorded` and empty on rows created before it was persisted), timestamps; no content | delete (cascades with its Scout, workspace and organization); not exported | none (pruning is a follow-up) |
| `job_events` | denormalised tenant ids (no foreign key to the organization), `event_metadata` (safe, bounded) | delete (dependent of `jobs`) | dependent of `jobs` |
| `llm_usage_events` | **tenant-linked usage metadata** (the LLM usage ledger, `P6-LLM-4`; one row per provider attempt): `organization_id`, `workspace_id`, `scout_request_id` and `job_id` (set null when the request or job row goes), `task`, `provider`, `model`, `input_tokens`, `output_tokens`, `cost_usd`, `outcome`, timestamps; no prompt text, no content, no user id | delete (cascades with its workspace in the organization deletion walk) | none — **no time-based class: kept for the life of its workspace for usage accounting and reconciliation** (unpaid pilot; operator decision `D-9c`, 2026-10-27; to be revisited before paid launch) |
| `llm_budget_periods` | **tenant-linked usage metadata** (the LLM budget ledger, `D-LLM-1` option B; one row per workspace and UTC calendar month): `workspace_id`, `period_start`, `organization_id`, `reserved_usd` (budget consumed: the sum of the calls' worst-case bounds), `settled_usd` (what finished calls were charged), `bound_violations` (a count of calls whose reported usage exceeded their bound), timestamps; money and counts only, no prompt text, no content, no user id | delete (cascades with its workspace and organization) | none: kept for the life of its workspace for usage accounting and reconciliation (`D-9c` pattern, ruled for these two tables 2026-10-27) |
| `llm_budget_reservations` | **tenant-linked usage metadata** (one row per reserved provider call): `organization_id`, `workspace_id`, `period_start`, `job_id` (set null when the job row goes), `attempt_key` (job id, worker attempt, call ordinal), `reserved_usd` (the call's worst-case bound), `settled_usd`, `state` (reserved, settled, released or expired), `expires_at`, `settled_at`, `usage_recorded_at` (when the attempt's usage row was written), timestamps; money, keys, states and instants only | delete (cascades with its workspace and organization) | none: as `llm_budget_periods` (`D-9c` pattern, ruled for these two tables 2026-10-27) |
| `worker_registrations` | worker identity, a non-reversible host fingerprint; **no personal or tenant data** | — | none (not a privacy data class) |
| `source_takedowns` | **permanent suppression record** of a live source an operator took down (`P6-PRIV-4`): `source_key`, `taken_down_at`, `purge_summary` (counts), `repeat_purges` (`[{at, purged counts}]`); **no user id, free text or reference** (operator decision `D-9d`, 2026-10-27) — attribution (the acting operator), a fixed generic explanation and the opaque case id are in each affected workspace's `audit_logs` row (`data_source.taken_down`) under that table's retention; the private reason is in `source_takedown_cases` | — (platform-wide; no tenant key) | none — **permanent**: the row is the suppression itself (the source is never fetched again while it exists); anonymising or expiring the audit rows or the case records cannot re-enable the source |

`llm_usage_events` is **included in the organization export** with every recorded
usage/accounting column, scoped by `organization_id` through the export's existing
authorization (exact role OWNER) and tenant filter (operator decisions `D-9a`/`D-9b`,
2026-10-27). It is **not in the account export**: the table has no user key, so there is no
supported relationship between an account and the organization's usage, and organization-wide
usage is not attributed to an individual account. Its actual lifecycle is the one above: no
retention sweep touches it; its rows exist exactly as long as their workspace and go with the
organization deletion walk. Its rows are exported completely, page by page, like every other
exported table (see *Portability* below).

`llm_budget_periods` and `llm_budget_reservations` (the budget ledger behind the hard
ceiling on PostgreSQL, `app.llm.budget`) follow the `llm_usage_events` pattern: **included
in the organization export** with every column listed above, scoped by `organization_id`
through the same authorization and tenant filter (`D-9b` pattern); **not in the account
export** (no user key, no supported relationship to an account); no retention sweep touches
them, their rows exist exactly as long as their workspace and go with the organization
deletion walk (`D-9c` pattern, ruled for these two tables on 2026-10-27). A settlement,
release or sweep that arrives after the workspace or organization was deleted finds no row
and creates none (it only logs and counts `llm_settlements_orphaned_total`).

`alembic_version` holds only the schema revision.

## Retention durations (undecided)

Seven data classes carry a time-based purge. Each duration is a setting with **no
default**; the values are `O-5` / `P6-D05`, reserved to the operator and legal, and this
repository does not choose them. The decision material is in the lane's
`DECISION-MEMO.md` (evidence set `P6-W1-PRIV-01`).

| Class | Setting (`env`) | Rows | Age measured from |
|---|---|---|---|
| `auth_sessions` | `RETENTION_AUTH_SESSIONS_DAYS` | `auth_sessions` | `expires_at` |
| `account_tokens` | `RETENTION_ACCOUNT_TOKENS_DAYS` | `password_reset_tokens`, `email_verification_tokens` | `expires_at` |
| `invitations` | `RETENTION_INVITATIONS_DAYS` | accepted, revoked or expired `organization_invitations` | `accepted_at` / `revoked_at` / `expires_at` |
| `signal_evidence` | `RETENTION_SIGNAL_EVIDENCE_DAYS` | `raw_signals`, `signal_clusters` (+ dependents) | `created_at` |
| `opportunities` | `RETENTION_OPPORTUNITIES_DAYS` | `opportunities` (+ dependents) | `created_at` |
| `job_history` | `RETENTION_JOB_HISTORY_DAYS` | terminal `jobs` (+ `job_events`) | `updated_at` |
| `audit_logs` | `RETENTION_AUDIT_LOGS_DAYS` | `audit_logs` | `created_at` |

**Unset is not a default.** A class whose duration is unset is *undecided*: the purge
refuses it and deletes nothing (the sweep reports `policy_unset` and exits non-zero).
`environment=production` **refuses to start** until all seven are set explicitly
(1–36500 days); development, test and staging may leave them unset. The cut-off is the
start of the current UTC day (database clock) minus the duration, so a purge never
removes a row earlier than the policy allows.

## Members of a deleted organization

Deleting an organization removes every membership in it; the members' **accounts** are
not deleted (they may belong to other organizations, and deleting another person's
account is not the owner's decision). A member left with no organization can still sign
in, export their data and delete their account.

## Outside the database — stated limitations and operating procedure

These stores are **not** reached by the deletion code. Each is a limitation of the
mechanism, with the procedure an operator follows.

| Store | What it can hold | Limitation | Procedure |
|---|---|---|---|
| **RDS automated backups / snapshots** | a full copy of every table at backup time | deleted rows persist in backups until those age out (`data_sql` module default `backup_retention_period` = 7 days; the root's configured value is not asserted here) and in any manual snapshot until it is deleted | after **any restore**, re-apply every erasure completed after the backup's timestamp, using the `privacy.account.deleted` / `privacy.organization.deleted` log events (they carry the pseudonymous subject id); manual snapshots must be covered by the same policy. This requires log retention ≥ backup retention — an operator item |
| **Application logs (CloudWatch)** | pseudonymous user ids on security events (`security.*`, `privacy.*`), request metadata; no passwords, tokens or emails by the logging contract | log events are not edited or deleted per subject; they expire with the log group (the `ecs` module's staging baseline is 30 days) | treat log-group retention as the deletion bound; state it in the privacy notice |
| **Redis** | job-available notifications and short-lived advisory locks; the cache adapter exists but no product path stores tenant data in it | — | none needed; TTL-bound |
| **Object storage (S3 adapter)** | no product path writes customer data to object storage on `origin/main` | — | re-assess if a feature starts storing objects |
| **Amazon SES** | account mail (reset and verification links) in transit; the SES **account-level suppression list** can hold an address that bounced or complained | SES content is not stored by the application; a suppressed address is not removed by account deletion | on an erasure request, check the SES suppression list for the address and remove it (operator, AWS-side) |
| **Third-party LLM provider** | prompt text sent for classification | see `docs/operations/llm-data-processing.md` (`P6-PRIV-2`) | per that document |
| **The user's browser** | the bearer token and the selected workspace in `localStorage` | cleared by sign-out and by the deletion flow in the web app | none |
| **Evidence sets / exports handed to the customer** | whatever was exported | the export is the customer's copy | none |

## Portability (export)

| Endpoint | Who | Contains |
|---|---|---|
| `GET /api/v1/account/export` | the signed-in account | profile, memberships, sessions (no tokens), invitations it accepted, and the others addressed to the account (no token digests; the others **only when its address is verified**, as `email_keyed_data` states), invitations it sent (no invitee address), feedback it submitted, audit events it performed (action, entity type, time, organization); no `llm_usage_events` rows (no user key: no supported relationship to an account) |
| `GET /api/v1/organizations/{id}/export` | exact role OWNER | the **export index**: the subject, `members` (id, email and name), `page_size_default` (1000) and `page_size_cap` (5000), `complete`, a `consistency` statement, `not_exported`, and for every exported tenant table its **exact `row_count`**, its **first page** of rows and the `next_cursor` of the next page (null when the first page holds every row; `complete` is true when that holds for every table) |
| `GET /api/v1/organizations/{id}/export/{table}?cursor=&limit=` | exact role OWNER | one **page** of one exported table, in `(created_at, id)` order after `cursor` (absent = the first page); `limit` 1–5000 (default 1000; above 5000 is refused with 422); the answer carries `rows` and `next_cursor` (null = the table is finished). An unknown or not-exported table is one and the same 404; a cursor that is not well-formed for that table (not URL-safe base64 of a JSON array of exactly three bounded strings `[table, created_at, id]` without control characters, or naming another table) is 422 `invalid_cursor`; any well-formed cursor naming the table is accepted |

Tables, rows and columns are those the inventory marks exported: every tenant table of the
organization (invitations without token digests; all workspace content, signals and results;
audit records), with credential and derived columns excluded (`token_hash`, `hashed_password`,
`embedding`, `centroid`); `llm_usage_events` (tenant-linked usage metadata, all recorded
columns, scoped by `organization_id`; `D-9b`, 2026-10-27) is included; jobs and job events are
operational and excluded. **No row is dropped for size** (operator ruling `D-9f`, 2026-10-27):
the earlier per-table cap that reported `truncated`, and so kept only the oldest 5,000 rows
of a large table (for the usage ledger, losing the current month), is gone. Every table is
completely retrievable by walking its pages; the per-page bound (5,000 rows) is the resource
bound, and the exact-OWNER authorization and the tenant filter apply to every page. Nothing
about the usage ledger's rows or any accounting balance is changed to fit an export.

**Cursors** are opaque to the client (URL-safe base64 of `[table, created_at, id]`) and are
**validated, not signed**. The server accepts a cursor only if it is well-formed for the table
it is used with (exactly three strings, the table named, an ISO-8601 timestamp of at most 40
characters, a non-empty id of at most 128, no control, format or NUL characters) and refuses
everything else with 422 `invalid_cursor`. It does not check that the server issued it: **any
well-formed cursor naming the table is accepted**, and a forged position can only move where
the read starts inside the requesting organization's own rows, because the tenant filter is
applied to every page (a foreign row's key and a missing key give identical answers). There is
no `offset` pagination.

**Concurrent changes.** No snapshot isolation is claimed: each page is read on its own, and
the index carries `generated_at`. A traversal that starts at the index and follows the
cursors sees, for each table: a row created after the traversal began **is included only if
its `(created_at, id)` key sorts after the cursor in use** (so an ordinary new row, stamped
now, is picked up while the walk is before the end of that table); a row created with a key
before the cursor is not seen by that traversal; **a row deleted before its page is read is
absent**; a row already delivered stays delivered; **no row is delivered twice**. The index
`row_count` is the exact count when the index was read; if the rows received differ, the data
changed during the download (the browser download records this as
`assembly.changed_during_download = true`). Equal counts do **not** prove nothing changed (a
row created and another deleted leaves the count equal): `changed_during_download = false`
means only that the counts matched. For a stable copy, run the download when the organization
is quiet and repeat it when a difference is reported.

**Page cost.** A page is the query `WHERE <tenant filter> AND (created_at, id) > cursor ORDER
BY created_at, id LIMIT n+1`. Measured on SQLite (`EXPLAIN QUERY PLAN`, pinned by a test for
`llm_usage_events`): the database seeks a tenant index, then reads and sorts **all of the
organization's rows of that table** with a temporary b-tree on every page; the keyset
predicate is not an index range. For tables that carry a non-null `workspace_id` beside
`organization_id` (`llm_usage_events` among them) the workspace list is added to the filter so
the seek uses `(workspace_id, created_at)` (`ix_llm_usage_events_workspace_created`) instead
of scanning every tenant's rows; the other tables use their single-column tenant index. The
response of a page is bounded (at most 5,000 rows); the database work per page is linear in
that organization's table, so **a full walk of one table costs O(n²/page) row reads** for
n rows. That is accepted for the unpaid pilot. **No PostgreSQL plan is read by any test** (the
PostgreSQL proofs in CI do not cover this query); the PostgreSQL behaviour is reasoned from the
SQL, not measured. Follow-up **FU-246-1**: an index that makes the keyset an index range, for
example `(organization_id, created_at, id)` or per-table keyset indexes; it needs a migration
and is deliberately not part of this change.

**Browser download.** *Settings → Organization data → Download organization data* reads the
index, follows every table's cursors (pages of the stated cap), shows progress, stops at the
first failed request without producing a file, and then saves **one** JSON file
(`signalnest-organization-export-YYYY-MM-DD.json`, `schema_version` 2): the index document
with `complete: true`, `tables.<name> = { row_count, rows_received, rows }` holding every
row, and `assembly = { started_at, finished_at, pages_fetched, changed_during_download }`.
The file is built in the browser's memory (streaming is not required for the pilot).
An operator can reproduce the same export with `curl` loops (`privacy-operations.md` §6).

**The account export is not paged**: it holds one person's own rows (profile, memberships,
sessions, tokens' metadata, the invitations, feedback, overrides and audit events that account
produced), which one account's activity bounds; it does not carry tenant tables, and no
`llm_usage_events` row.

Exports are read-only: they write nothing, not even an audit record.
