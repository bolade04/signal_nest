# Phase 3A.2 — Production Runtime Hardening

Second vertical slice of Phase 3. It closes the three documented follow-ups from Phase
3A.1 before production infrastructure work begins. Everything here is additive behind the
existing configuration, adapter and auth seams; Phase 1–2 behavior, the Phase 3A.1
contracts, and the four-market isolation guarantees are unchanged. **No external service
is activated, required, or paid for.**

## Follow-up 1 — Production requires a production stack

Phase 3A.1 coupled its production checks to `app_mode`, so `environment=production` with
the default local stack could still start silently. Enforcement is now driven by the
**environment** and fails during `Settings` construction (before any traffic is served).

`environment=production` now requires `app_mode=full` and rejects each local-only backend
**by name** so the operator knows exactly what is misconfigured:

| Offending setting | Rejection |
| --- | --- |
| `app_mode=local` | requires `app_mode=full` |
| SQLite `database_url` | requires a PostgreSQL database |
| `queue_backend=inprocess` | requires a durable queue backend |
| `cache_backend=memory` | requires a shared cache backend |
| `storage_backend=local` | requires durable object storage |
| `vector_backend=bruteforce` | requires a persistent vector backend |
| `storage_backend=s3` without `s3_bucket` | requires `s3_bucket` |

The existing production-like guards (mock LLM, insecure/missing secret key, real provider
without an API key, dev fallback) remain, and reject **empty or whitespace-only** values:
a blank `secret_key` in staging/production (which would otherwise sign JWTs with an empty
key) and an empty `database_url` in any mode both fail fast. Two hardening details keep
secrets out of crash output: `hide_input_in_errors=True` on the settings model (pydantic
no longer echoes the raw constructor input in a `ValidationError`) and `repr=False` on
every secret field. A regression test asserts a configuration error **never contains a
secret value**.

## Follow-up 2 — Detailed runtime info is operator-only

The detailed backend topology is no longer visible to ordinary customers. This slice adds
the **smallest safe operator foundation** — it is *not* broad production RBAC.

- `User.is_operator` (boolean, `server_default=false`, migration
  `b2d4e6f8a1c3`) — a **server-controlled** attribute. There is no `is_operator` field on
  the register schema and the registration service never sets it, so a client cannot
  self-assign. A dev operator is seeded **only** in `development`/`test`.
- `require_operator` dependency (`app/auth/dependencies.py`) — reusable, returns `403`
  for a non-operator.
- **Option A (separate endpoints):**

  | Endpoint | Auth | Body |
  | --- | --- | --- |
  | `GET /system/capabilities` | any authenticated user | coarse summary: `app_mode`, `environment`, `is_local_mode`, `all_configured` |
  | `GET /internal/system/capabilities` | operator only | detailed per-capability topology (secret-free) |
  | `GET /internal/system/readiness` | operator only | per-probe diagnostics (safe summary + detail) |

  Anonymous → `401`; authenticated non-operator → `403`; operator → detailed safe report.
  The frontend gates the operator-only Settings panel on `user.is_operator` and only then
  issues the internal request.

## Follow-up 3 — Real bounded readiness probes

`/system/readiness` is now backed by concrete, **bounded** probes instead of a static
config read.

- `ProbeStatus` = `healthy | degraded | unavailable | not_configured | timeout`; the last
  three are **blocking**. Placeholders never self-report healthy.
- Each probe carries `name`, `status`, `required`, `duration_ms`, `timestamp`, a **safe
  public summary**, an operator-only `detail`, and `retryable`.
- Timeouts are enforced per-probe **and** in total via
  `READINESS_PROBE_TIMEOUT_SECONDS` / `READINESS_TOTAL_TIMEOUT_SECONDS` (validated
  positive; per-probe ≤ total). Probes fan out on a thread pool that is torn down
  **non-blocking** (`shutdown(wait=False, cancel_futures=True)`) so a hung probe cannot
  wedge the endpoint.

| Probe | Required | Healthy when | Safe / bounded behavior |
| --- | --- | --- | --- |
| database | yes | `SELECT 1` + expected schema tables present | read-only connectivity only |
| queue | yes | Redis `PING` (full) or the in-process adapter is constructed | no job execution |
| cache | yes | Redis `PING` (full) or in-memory healthy | — |
| storage | yes | local root (nearest writable ancestor) OK; **path traversal rejected** | s3 = config-only (`not_configured` without a bucket, else `degraded`) |
| vector | yes | brute-force in allowed modes | pgvector on SQLite → `not_configured`; else `degraded` |
| llm | no | mock provider | real provider = `degraded` (config only) — **no paid call** |

- **Public** readiness (`/system/readiness`) excludes hosts/ports/URLs/adapter
  details/exceptions/secret-var-names/buckets/paths. **Operator** diagnostics
  (`/internal/system/readiness`) add safe summaries and internal detail. A probe that
  raises reports only the exception **class name** — never the raw message, which could
  embed a host/port/URL — so no raw adapter exception reaches even the operator surface.
- Liveness (`/system/health`) stays cheap and independent of the probes.

## Guarantees demonstrated by tests

Backend `61 → 86`, frontend `19 → 20`.

- `test_runtime_foundation.py` — production rejects the local stack backend-by-backend,
  accepts a valid full/production stack, requires an s3 bucket, validates readiness
  timeout bounds, and never leaks a secret in an error.
- `test_readiness_probes.py` — local stack healthy; public view excludes infra detail;
  s3-without-bucket is `not_configured`; storage rejects path traversal; real LLM is
  `degraded` without a live call; a hung probe is bounded and non-blocking; a failed
  required probe blocks readiness.
- `test_api_isolation.py` — `/system/capabilities` needs auth and is coarse/secret-free;
  `/internal/system/*` is `401` anonymous, `403` non-operator, detailed + secret-free for
  an operator.
- `settings-runtime.test.tsx` — operators see the infrastructure detail; non-operators
  never trigger the internal endpoint.

## Rollback plan

- The runtime hardening (config enforcement, probes, endpoints) is **additive behind
  existing seams**. Revert = revert the single squash commit for this PR.
- The one schema change is the additive, nullable `users.is_operator` column
  (`server_default=false`). Reverting the code leaves the column harmless; a full revert
  can `alembic downgrade` one step to drop it. No data repair is required because the
  column defaults to `false` for every existing row.
- Existing local implementations (SQLite / in-process queue / in-memory cache /
  brute-force vector / local storage / mock LLM) are **not deleted or altered**, so a
  revert returns to the accepted Phase 3A.1 baseline with no manual steps.

## Cost posture (Phase 3A.2 = effectively zero-cost)

- No paid LLM/embedding/moderation/storage/queue/observability/connector service is
  activated; the LLM probe is **config-only** and makes no provider call.
- No new runtime dependency is added; production adapters remain interfaces + validated
  configuration + explicit not-configured/degraded states.
- No network calls in unit tests/CI; local zero-dependency dev is preserved.
