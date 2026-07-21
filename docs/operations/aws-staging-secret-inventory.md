# AWS staging secret inventory and G5 secret-readiness contract (Phase 4B-C INFRA-3)

> **Scope guard.** This document is **planning and scaffolding only**. It creates,
> reads, rotates, or stores **no** secret; it calls **no** AWS API; it authenticates
> to **no** account; it deploys **nothing**; and it activates **no** capability. Every
> value shown is a **name or a disposition**, never a credential. The three global
> feature flags (`opportunity_feedback_enabled`, `scout_scheduling_enabled`,
> `connector_rss_enabled`) remain Boolean **`False`**. Implementing the inventory
> checks and the G5 gate is the later, separately authorized INFRA-3 *implementation*
> tranche.

Related documents: the ECS/Fargate selection ([adr-0001](../architecture/adr-0001-aws-ecs-fargate-staging.md)),
the authoritative runtime/security/cost contract
([aws-staging-runtime-contract.md](./aws-staging-runtime-contract.md)), and the roadmap
([phase-4b-c-infra-plan.md](../phase-4b-c-infra-plan.md), section INFRA-3). The
non-secret placeholder template is the root [`.env.canary.example`](../../.env.canary.example).

---

## 1. Purpose and scope

This is the repository-side **secrets contract** for the internal, non-customer
**SIGNALNEST_STAGING** canary. It enumerates **every** configurable setting of the
application, maps each to its exact environment-variable name, and assigns each
**exactly one reviewed staging disposition**:

- **AWS Secrets Manager** — secret-bearing; injected only via a Secrets Manager
  reference.
- **IAM-derived** — an AWS credential that must come from the task role's default
  credential chain, never a committed or injected literal.
- **Non-secret configuration** — safe to supply as plaintext task-definition
  environment (tuning, bounds, identifiers, feature flags).
- **Absent in staging** — not part of the INFRA-3 staging profile; must be unset.
- **Local-development-only** — a developer-convenience setting that must be unset or
  pinned to its safe default in staging (the runtime validator rejects the unsafe
  value).

It also defines the actor-boundary (minimum-necessary) matrix, the ECS injection
contract, the operational rules (rotation, failure, incident, evidence, rollback),
and the fail-closed **G5 secret-readiness gate**.

**Out of scope:** any live secret operation, AWS provisioning, IaC, deployment,
capability activation, or override creation. Those belong to later, separately
authorized tranches (INFRA-4 onward and the separate activation gate).

## 2. Source methodology

The inventory is derived **only** from repository evidence, not assumption:

- The single source of truth is the Pydantic settings model
  `Settings` in [`apps/api/app/core/config.py`](../../apps/api/app/core/config.py).
  There is **no `env_prefix`**, so every environment-variable name is the field name
  **UPPERCASED** (`SettingsConfigDict(case_sensitive=False)`).
- Field enumeration was taken directly from the model's annotated assignments. As of
  this document the model declares **87 fields**, of which **9** are marked
  `repr=False` (secret-bearing or host-identifying) and **0** use `SecretStr`.
- Secret sensitivity is read from the `repr=False` markers plus the field semantics
  documented in `config.py` and the runtime contract.
- Staging requirement/forbidden conditions are read from the unconditional
  `@model_validator(mode="after") _validate_runtime`, which runs at `Settings`
  construction in **every** actor (see §4).
- The SPA exclusion is read from [`apps/web/src/api/config.ts`](../../apps/web/src/api/config.ts):
  the browser build reads **only** `VITE_API_BASE_URL` and no backend setting.

The nine `repr=False` fields are: `otlp_endpoint`, `secret_key`, `database_url`,
`redis_url`, `s3_bucket`, `s3_endpoint_url`, `s3_access_key_id`,
`s3_secret_access_key`, `llm_api_key`. Every one is assigned a disposition in §3 (see
the reconciliation in §3.7).

## 3. Complete field inventory (every field, exactly one disposition)

Environment-variable names are the UPPERCASED field name. Grouped by disposition so
that the one-disposition-per-field rule is auditable; §3.7 reconciles the count.

### 3.1 AWS Secrets Manager (secret-bearing) — 4 fields

| Field | Env var | `repr=False` | Rationale |
| --- | --- | --- | --- |
| `secret_key` | `SECRET_KEY` | yes | Token-signing secret; validator rejects the dev default / empty in staging. |
| `database_url` | `DATABASE_URL` | yes | PostgreSQL DSN embedding credentials; required (non-SQLite) in staging. |
| `redis_url` | `REDIS_URL` | yes | Redis DSN embedding credentials. Injected from Secrets Manager **only when** the staging profile enables a Redis-backed cache/queue path; when that path is disabled the field is absent (see runtime contract). Disposition when present: Secrets Manager. |
| `llm_api_key` | `LLM_API_KEY` | yes | Provider API key; required because staging forbids `llm_provider=mock`. |

### 3.2 IAM-derived (task-role credential chain; never a literal) — 2 fields

| Field | Env var | `repr=False` | Rationale |
| --- | --- | --- | --- |
| `s3_access_key_id` | `S3_ACCESS_KEY_ID` | yes | Must remain **unset** in staging so the SDK uses the ECS task-role credential chain; a committed/injected literal is prohibited. |
| `s3_secret_access_key` | `S3_SECRET_ACCESS_KEY` | yes | Same; the two are all-or-nothing per the validator and both stay unset. |

### 3.3 Absent in staging — 2 fields

| Field | Env var | `repr=False` | Rationale |
| --- | --- | --- | --- |
| `otlp_endpoint` | `OTLP_ENDPOINT` | yes | The staging observability path is CloudWatch, not OTLP; tracing exporter is not `otlp`, so this host-identifying endpoint is unset. If OTLP is ever enabled it becomes a managed non-secret host endpoint under a future tranche — not in the INFRA-3 profile. |
| `s3_endpoint_url` | `S3_ENDPOINT_URL` | yes | Only for a local/custom S3 endpoint (e.g. MinIO). Native AWS S3 uses the default endpoint, so this is unset in staging. |

### 3.4 Local-development-only (unset / safe default in staging) — 3 fields

| Field | Env var | `repr=False` | Rationale |
| --- | --- | --- | --- |
| `local_storage_dir` | `LOCAL_STORAGE_DIR` | no | Local-filesystem object storage; staging uses `storage_backend=s3`. |
| `llm_mock_seed` | `LLM_MOCK_SEED` | no | Only affects the mock LLM provider, which staging forbids. |
| `llm_allow_dev_fallback` | `LLM_ALLOW_DEV_FALLBACK` | no | Developer fallback; validator rejects `true` in staging, so it stays at its `False` default. |

### 3.5 Non-secret configuration — 76 fields

Safe as plaintext task-definition environment (or the model default). Grouped by
concern; every row's disposition is **non-secret configuration**.

**Feature flags (non-secret; pinned `False` for the dark canary):**

| Field | Env var | Note |
| --- | --- | --- |
| `opportunity_feedback_enabled` | `OPPORTUNITY_FEEDBACK_ENABLED` | Must remain `False`; activation is a separate authorized gate, not INFRA-3. |
| `scout_scheduling_enabled` | `SCOUT_SCHEDULING_ENABLED` | Must remain `False`. |
| `connector_rss_enabled` | `CONNECTOR_RSS_ENABLED` | Must remain `False`. |

**General / API:** `app_name` (`APP_NAME`), `app_mode` (`APP_MODE`, staging=`full`),
`environment` (`ENVIRONMENT`, staging=`staging`), `debug` (`DEBUG`),
`api_prefix` (`API_PREFIX`).

**Observability / tracing:** `service_name` (`SERVICE_NAME`), `log_format`
(`LOG_FORMAT`), `metrics_enabled` (`METRICS_ENABLED`), `tracing_enabled`
(`TRACING_ENABLED`), `tracing_exporter` (`TRACING_EXPORTER`), `tracing_sample_ratio`
(`TRACING_SAMPLE_RATIO`), `tracing_export_timeout_seconds`
(`TRACING_EXPORT_TIMEOUT_SECONDS`), `tracing_shutdown_flush_seconds`
(`TRACING_SHUTDOWN_FLUSH_SECONDS`), `tracing_max_queue_size`
(`TRACING_MAX_QUEUE_SIZE`), `tracing_propagation` (`TRACING_PROPAGATION`).

**Security (non-secret):** `access_token_expire_minutes`
(`ACCESS_TOKEN_EXPIRE_MINUTES`), `cors_origins` (`CORS_ORIGINS`).

**PostgreSQL pool:** `db_pool_size` (`DB_POOL_SIZE`), `db_max_overflow`
(`DB_MAX_OVERFLOW`), `db_pool_timeout_seconds` (`DB_POOL_TIMEOUT_SECONDS`),
`db_pool_recycle_seconds` (`DB_POOL_RECYCLE_SECONDS`), `db_connect_timeout_seconds`
(`DB_CONNECT_TIMEOUT_SECONDS`), `db_application_name` (`DB_APPLICATION_NAME`).

**S3 (non-credential):** `storage_backend` (`STORAGE_BACKEND`, staging=`s3`),
`s3_bucket` (`S3_BUCKET`, `repr=False` but identifying-not-credential; required when
`storage_backend=s3`), `s3_region` (`S3_REGION`), `s3_use_ssl` (`S3_USE_SSL`),
`s3_max_object_bytes` (`S3_MAX_OBJECT_BYTES`), `s3_signed_url_ttl_seconds`
(`S3_SIGNED_URL_TTL_SECONDS`), `s3_operation_timeout_seconds`
(`S3_OPERATION_TIMEOUT_SECONDS`), `s3_max_retries` (`S3_MAX_RETRIES`).

**Redis tuning:** `redis_pool_size` (`REDIS_POOL_SIZE`),
`redis_operation_timeout_seconds` (`REDIS_OPERATION_TIMEOUT_SECONDS`),
`redis_key_prefix` (`REDIS_KEY_PREFIX`), `redis_notify_channel`
(`REDIS_NOTIFY_CHANNEL`), `redis_lock_ttl_seconds` (`REDIS_LOCK_TTL_SECONDS`).

**Vector / jobs backends:** `vector_backend` (`VECTOR_BACKEND`), `embedding_dim`
(`EMBEDDING_DIM`), `queue_backend` (`QUEUE_BACKEND`), `cache_backend`
(`CACHE_BACKEND`).

**Durable job / worker execution:** `job_queue_backend` (`JOB_QUEUE_BACKEND`),
`worker_id` (`WORKER_ID`, optional identity, non-secret), `worker_concurrency`
(`WORKER_CONCURRENCY`), `worker_poll_interval_seconds`
(`WORKER_POLL_INTERVAL_SECONDS`), `worker_lease_seconds` (`WORKER_LEASE_SECONDS`),
`worker_heartbeat_seconds` (`WORKER_HEARTBEAT_SECONDS`),
`worker_shutdown_grace_seconds` (`WORKER_SHUTDOWN_GRACE_SECONDS`),
`worker_force_shutdown_grace_seconds` (`WORKER_FORCE_SHUTDOWN_GRACE_SECONDS`),
`job_default_max_attempts` (`JOB_DEFAULT_MAX_ATTEMPTS`), `job_retry_base_seconds`
(`JOB_RETRY_BASE_SECONDS`), `job_retry_max_seconds` (`JOB_RETRY_MAX_SECONDS`),
`job_max_payload_bytes` (`JOB_MAX_PAYLOAD_BYTES`), `job_claim_batch_size`
(`JOB_CLAIM_BATCH_SIZE`), `readiness_cache_ttl_seconds`
(`READINESS_CACHE_TTL_SECONDS`).

**Worker fleet registry:** `worker_type` (`WORKER_TYPE`), `worker_stale_after_seconds`
(`WORKER_STALE_AFTER_SECONDS`), `worker_registration_retry_limit`
(`WORKER_REGISTRATION_RETRY_LIMIT`), `worker_registration_retry_delay_seconds`
(`WORKER_REGISTRATION_RETRY_DELAY_SECONDS`), `worker_id_max_length`
(`WORKER_ID_MAX_LENGTH`), `require_worker_fleet` (`REQUIRE_WORKER_FLEET`),
`application_version` (`APPLICATION_VERSION`), `build_revision` (`BUILD_REVISION`).

**Scouting connectors (dark):** `connector_rss_markets` (`CONNECTOR_RSS_MARKETS`),
`connector_rss_rate_capacity` (`CONNECTOR_RSS_RATE_CAPACITY`),
`connector_rss_rate_refill_per_second` (`CONNECTOR_RSS_RATE_REFILL_PER_SECOND`),
`connector_rss_max_attempts` (`CONNECTOR_RSS_MAX_ATTEMPTS`).

**LLM (non-secret):** `llm_provider` (`LLM_PROVIDER`, staging=`openai`|`anthropic`),
`llm_model` (`LLM_MODEL`), `llm_timeout_seconds` (`LLM_TIMEOUT_SECONDS`),
`llm_max_retries` (`LLM_MAX_RETRIES`), `llm_temperature` (`LLM_TEMPERATURE`).

**Readiness probes:** `readiness_probe_timeout_seconds`
(`READINESS_PROBE_TIMEOUT_SECONDS`), `readiness_total_timeout_seconds`
(`READINESS_TOTAL_TIMEOUT_SECONDS`).

### 3.6 `s3_bucket` classification note

`s3_bucket` is `repr=False` yet classified **non-secret configuration**: a bucket name
is an identifier, not a credential. It is kept out of reprs to avoid leaking a
resource name into logs, but it is safe to supply as plaintext task environment. It is
**required** when `storage_backend=s3` (validator §3 of `config.py`).

### 3.7 Completeness reconciliation (zero unclassified)

| Disposition | Count |
| --- | --- |
| AWS Secrets Manager | 4 |
| IAM-derived | 2 |
| Absent in staging | 2 |
| Local-development-only | 3 |
| Non-secret configuration | 76 |
| **Total** | **87** |

`4 + 2 + 2 + 3 + 76 = 87`, equal to the model's declared field count. **Zero fields
are unclassified**, and **all 9 `repr=False` fields** (`secret_key`, `database_url`,
`redis_url`, `llm_api_key` → Secrets Manager; `s3_access_key_id`,
`s3_secret_access_key` → IAM-derived; `otlp_endpoint`, `s3_endpoint_url` → absent;
`s3_bucket` → non-secret) carry exactly one disposition. There are **0** `SecretStr`
fields to reconcile. If a future field is added to `Settings`, this table's total and
this reconciliation MUST be updated in the same change, and G5 fails closed until it is.

## 4. Actor-boundary (minimum-necessary) matrix

The runtime ships three server actors from one image (`apps/api/Dockerfile`): the API
(`uvicorn app.main:app`), the worker (`python -m app.jobs.worker`), and the one-shot
migration actor (`python -m app.db.migrate`). The SPA build (`apps/web`) is a fourth,
secret-free artifact.

**Load-bearing evidence finding — validator coupling.** Every actor calls
`get_settings()`, and `Settings` construction unconditionally runs `_validate_runtime`.
In a staging (`is_production_like`) environment that validator requires a strong
`secret_key`, a non-mock `llm_provider` **with** `llm_api_key`, and `s3_bucket` when
`storage_backend=s3` — **regardless of whether the actor functionally uses them**.
Consequently the migration actor, whose only functional need is `database_url`, must
still be *able to construct* Settings with `secret_key`, `llm_api_key`, and `s3_bucket`
present. This is a documented **over-provisioning** of the migration actor's secret
surface and a **hardening opportunity** for a later, separately authorized code change
(e.g. a construction path that relaxes non-DB requirements for the migration actor). It
is recorded here as evidence; **INFRA-3 changes no application code.**

| Setting | API | Worker | Migration actor | SPA build |
| --- | --- | --- | --- | --- |
| `SECRET_KEY` | functional (token signing) + construction | construction-required | construction-required only | never |
| `DATABASE_URL` | functional | functional (durable job store) | functional (the only functional need) | never |
| `REDIS_URL` | functional if Redis profile | functional (job availability pub/sub) | not needed | never |
| `LLM_API_KEY` | construction + functional if endpoint calls LLM | construction + functional if a job calls LLM | construction-required only | never |
| S3 access (IAM role) | functional (object storage) | functional if a job uses storage | not needed | never |
| `S3_BUCKET` | functional if storage used | functional if storage used | construction-required only (when `storage_backend=s3`) | never |
| Non-secret config | as needed | as needed | as needed | only `VITE_API_BASE_URL` (build-time, non-secret) |

**SPA exclusion.** No backend secret ever reaches the `apps/web` build output. The
browser bundle reads only `VITE_API_BASE_URL` at build time
([`apps/web/src/api/config.ts`](../../apps/web/src/api/config.ts)). Any backend secret
appearing in a web artifact is a G5 failure (`G5-10`).

## 5. ECS injection contract

- **Secrets Manager settings (§3.1)** are injected **only** through the ECS
  task-definition `secrets` block (`valueFrom` a Secrets Manager ARN resolved at task
  start). They are **never** placed in the task-definition `environment` block,
  **never** passed as a Docker build argument, **never** written to an image label,
  and **never** encoded into a mutable image tag. Images are digest-pinned and
  immutable; a secret is resolved at runtime, not baked in.
- **IAM-derived credentials (§3.2)** are supplied by the ECS **task role** default
  credential chain. `S3_ACCESS_KEY_ID` / `S3_SECRET_ACCESS_KEY` are never set. The
  execution role may read only the specific Secrets Manager references this inventory
  lists (least privilege); the task role grants only the specific S3/other AWS actions
  the workload needs.
- **Non-secret configuration (§3.5)** is supplied as plaintext task-definition
  `environment` (or left at the model default).
- **Absent-in-staging (§3.3)** and **local-only (§3.4)** settings are not set in any
  staging task definition.
- **Web build** receives only `VITE_API_BASE_URL`; no backend secret is available to
  Vite at build or at runtime.

## 6. Operational rules (rotation, failure, incident, evidence, rollback)

- **Rotation.** Secrets rotate in AWS Secrets Manager; task definitions reference a
  secret by ARN (and, where pinned, a specific version stage), so a rotation is picked
  up on the next task start with no image rebuild. Operational rotation *procedures*
  are owned by INFRA-6; this document owns only the inventory and disposition contract.
- **Failure.** A missing, empty, malformed, disabled-version, or inaccessible required
  secret must **fail closed**: the actor fails at `Settings` construction / task start
  rather than serving traffic with a degraded or default credential. No silent
  fallback to a dev default is permitted in staging (the validator enforces this for
  `secret_key`, `llm_provider`, and dev fallback).
- **Incident.** A suspected exposure triggers immediate rotation of the affected
  Secrets Manager entry and redeployment; the exposed value is treated as
  compromised. Incident *runbooks* are INFRA-6/INFRA-7 scope.
- **Evidence.** Any verification evidence (including G5 output) is **sanitized-only**:
  it records field names, dispositions, ARNs-by-reference, presence/absence, and
  pass/fail identifiers. A secret **value** is **never** printed, logged, diffed, or
  retained. Sanitized identifiers (§7) are the only failure surface.
- **Rollback.** This document and its scaffolding are reverted by reverting the docs
  PR; no runtime state exists to roll back (nothing is provisioned).

## 7. G5 — secret-readiness gate (contract)

**G5** is a future, separately implemented gate. Its **properties** are mandatory and
non-negotiable:

- **Fail-closed** — any doubt blocks the canary.
- **Read-only and non-mutating** — verifies; never creates, edits, or rotates a secret.
- **Repeatable** — same inputs, same result; safe to re-run.
- **Required before any runtime-canary authorization** — no override, feature flag, or
  operator action may proceed to a live canary until G5 passes.
- **Not bypassable by any capability override** — the governed override control plane
  cannot satisfy or skip G5.
- **Incapable of authorizing feature activation** — passing G5 permits *readiness*, not
  activation; enabling `opportunity_feedback_enabled` remains a separate authorized gate.
- **Sanitized-only evidence** — never prints/logs/diffs/retains a secret value.

### 7.1 Verification obligations

G5 MUST verify that: (a) every secret-bearing setting has exactly one reviewed
disposition (§3); (b) every `repr=False` field is inventoried; (c) `SecretStr` and
other secret-like settings are inventoried (currently zero); (d) ECS `secrets`
references match this inventory; (e) each required secret object, key, and enabled
version exists; (f) the execution role can access **only** the required references
(least privilege, no excess); (g) API, worker, and migration each receive only their
minimum-necessary subset (§4); (h) no backend secret reaches any browser/SPA artifact;
(i) no required secret is supplied via committed values, Docker build args, image
labels, plaintext task environment, mutable tags, dev fallbacks, tenant-controlled
values, or mock providers; (j) AWS service access uses task roles where supported; and
(k) PostgreSQL, Redis, object-storage, and non-mock-LLM requirements fail closed.

### 7.2 Stable sanitized failure identifiers

Each failure emits one stable identifier and a sanitized reason (**never** the value).
Any single identifier firing **blocks the canary** (fail-closed).

| ID | Fires when |
| --- | --- |
| `G5-01 SECRET_DISPOSITION_INCOMPLETE` | A setting has zero or more-than-one reviewed disposition. |
| `G5-02 REPR_FALSE_UNINVENTORIED` | A `repr=False` field is missing from the inventory. |
| `G5-03 SECRETLIKE_UNINVENTORIED` | A `SecretStr`/secret-like field is not inventoried. |
| `G5-04 ECS_REFERENCE_MISMATCH` | A task-definition `secrets` reference does not match the inventory. |
| `G5-05 SECRET_OBJECT_ABSENT` | A required Secrets Manager object/key does not exist. |
| `G5-06 SECRET_VERSION_DISABLED` | The referenced secret has no enabled/current version. |
| `G5-07 EXECUTION_ROLE_OVERBROAD` | The execution role can read secrets beyond the required set. |
| `G5-08 EXECUTION_ROLE_UNAUTHORIZED` | The execution role cannot read a required reference. |
| `G5-09 ACTOR_SUBSET_EXCEEDED` | An actor is granted a secret outside its minimum-necessary subset. |
| `G5-10 SPA_SECRET_LEAK` | A backend secret is present in a browser/SPA artifact. |
| `G5-11 PLAINTEXT_ENV_SECRET` | A secret-disposition field appears in plaintext task `environment`. |
| `G5-12 BUILD_ARG_SECRET` | A secret is passed as a Docker build argument. |
| `G5-13 IMAGE_LABEL_SECRET` | A secret is written to an image label. |
| `G5-14 MUTABLE_TAG_SECRET` | A secret is bound to a mutable image tag instead of a digest. |
| `G5-15 DEV_FALLBACK_ENABLED` | `llm_allow_dev_fallback` or an equivalent dev fallback is active in staging. |
| `G5-16 TENANT_SUPPLIED_SECRET` | A required secret is sourced from a tenant-controlled value. |
| `G5-17 MOCK_PROVIDER_IN_STAGING` | `llm_provider=mock` (or any mock backend) in staging. |
| `G5-18 IAM_STATIC_CREDENTIAL_PRESENT` | `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY` (or another static AWS key) is set instead of using the task role. |
| `G5-19 BACKEND_REQUIREMENT_UNMET` | A PostgreSQL, Redis, object-storage, or non-mock-LLM requirement is not satisfied. |
| `G5-20 SECRET_MALFORMED` | A required secret is present but empty/malformed/unparseable. |
| `G5-21 SECRET_MISCLASSIFIED` | A field's live handling contradicts its reviewed disposition. |

21 identifiers cover the §7.1 obligations (well above the minimum). The list is
extended, never silently renumbered, as obligations are added.

### 7.3 What G5 explicitly does not do

G5 does not provision, rotate, deploy, or activate anything; it does not authorize a
capability override; it does not enable any feature flag; and passing it does not by
itself authorize a live canary — that remains a separate human gate. No AWS-backed or
live G5 check runs as part of INFRA-3 scaffolding.
