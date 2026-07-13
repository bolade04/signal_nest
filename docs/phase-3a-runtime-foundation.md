# Phase 3A.1 — Production Runtime Foundation

First vertical slice of Phase 3. It adds the runtime scaffolding required to run
SignalNest safely in production **without activating, requiring, or paying for any
external service**. Everything here is additive behind the existing configuration and
adapter seams; Phase 1–2 behavior and the four-market isolation guarantees are
unchanged.

## What shipped

| Area | File(s) | Summary |
| --- | --- | --- |
| Error taxonomy | `app/core/errors.py` | `ConfigurationError`, `AdapterNotConfiguredError`, `CapabilityUnavailableError` (all `503`). Production placeholders **fail explicitly** rather than partially activating. |
| Runtime capability model | `app/core/runtime.py` | A **secret-free**, pure read model over `Settings`: per-capability backend, `configured`, `is_local`, `requires_external`. Never surfaces URLs, keys, buckets or endpoints. |
| System endpoints | `app/system/routes.py` | `GET {prefix}/system/health` (liveness, anonymous probe), `/system/readiness` (schema migrated + all backends configured → `503` when not ready, anonymous probe), `/system/capabilities` (secret-free introspection, **requires authentication** so infrastructure topology is not anonymously enumerable). |
| Tenant execution context | `app/jobs/context.py` | `ExecutionContext` (org / workspace / location / campaign + correlation ids) so isolation travels **with the job**, plus a `scope_matches` guard. |
| Versioned job contracts | `app/jobs/contracts.py` | `JobEnvelope` with `contract_version="1"`, deterministic `envelope_hash`, JSON-serializable message form. `unwrap()` accepts both the envelope and the legacy bare payload. |
| Pipeline wiring | `app/jobs/pipeline.py`, `app/scouting_requests/routes.py` | The run endpoint now enqueues a versioned envelope carrying tenant scope; the handler unwraps either form and **refuses** a request whose scope does not match the declared context. |
| Frontend runtime status | `apps/web/src/pages/Settings.tsx` (+ typed client) | A read-only Settings panel that shows runtime mode and per-capability readiness from `/system/capabilities`. |

## Guarantees demonstrated by tests

- **No silent production fallback.** Config rejects full-mode+SQLite, mock LLM in
  production, missing secret key, real provider without an API key, and dev fallback in
  production (`test_runtime_foundation.py`).
- **Local mode still works with zero external services** — `demo:setup` + full suite +
  the readiness endpoint report `ready` in seeded local mode.
- **Secret hygiene** — capability views never contain credentials
  (`test_capability_public_dict_never_leaks_secrets`, and the HTTP-level check in
  `test_api_isolation.py`).
- **Deterministic, versioned jobs** — stable `envelope_hash`; unknown versions rejected.
- **Explicit tenant/location context** — `scope_matches` + the handler isolation guard;
  four-market isolation tests remain green.

## Rollback plan

- The slice is **entirely additive** behind existing seams; no schema/migration change,
  so there is **no data to repair** on revert.
- Revert = revert the single squash commit for this PR. Local `main` returns to the
  accepted baseline with no manual steps.
- The job handler stays backward compatible: reverting the producer (the run endpoint)
  leaves the legacy `{"scout_request_id": ...}` payload working, and reverting the
  consumer still accepts the legacy payload — so a partial revert cannot wedge the queue.
- Existing local implementations (SQLite / in-process queue / in-memory cache /
  brute-force vector / local storage / mock LLM) are **not deleted or altered**.

## Cost posture (Phase 3A.1 = effectively zero-cost)

- No paid LLM/embedding/moderation/storage/queue/observability/connector service is
  activated; no provider credentials are added; no cloud infrastructure is created.
- No new dependency is added; production adapters remain represented by interfaces,
  factories, validated configuration, and explicit not-configured states.
- No network calls in unit tests/CI (the existing localhost smoke is unchanged).
- No scheduler, worker execution, or background job runs against an external provider.

## Future cost-bearing integration points (documented, not implemented)

When real providers/connectors are enabled in later slices, each must support:
per-tenant quotas · per-provider budgets · request-level accounting · token/unit
tracking · estimated-cost metadata · hard spending ceilings · timeouts · retry limits ·
circuit breakers · explicit feature flags · administrative kill switches. The runtime
capability model and `LLMUsage.estimated_cost_usd` field are the seams where these
controls will attach.
