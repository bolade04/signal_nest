# Phase 3A.4b — Observability and Deployment Hardening (Plan)

Base branch: `feat/phase-3a-observability-deployment`
Base `main` SHA at branch creation: `3fefb36d432c7c9c46118e29bf631c32120f5e65`

## Objective

Harden SignalNest's production runtime with:

- Concurrency-safe recovery and worker identity
- Structured logs
- Safe metrics
- Distributed tracing
- Production API and worker containers
- Graceful lifecycle behavior
- Deployment and migration procedures
- Operational dashboards and alerts
- Failure-injection and recovery tests

## Delivery strategy

This phase is large. It is delivered as reviewable batches, each a coherent set of
focused commits that passes the full repository gate before the next batch begins.

- **Batch 1 (complete) — concurrency and data-plane hardening.** The seven
  accepted Phase 3A.4a follow-ups below that concern correctness under concurrency
  and hostile input. Opened as a **draft** PR (#31) once green.
- **Batch 2 (this session) — structured logging, redaction, request/job
  correlation, and bounded/provider-neutral metrics.** Merges the original
  "structured logging" and "bounded metrics" batches: the two share the same
  telemetry seams (config, contextvars, failure isolation) and are cheaper to land
  and review together.
- **Batch 3 (this session) — distributed tracing + end-to-end telemetry.** A
  provider-neutral, OTel-compatible tracing seam (no-op default, in-memory test
  exporter, optional import-guarded OTLP), HTTP server spans, durable-job trace-context
  propagation, worker execution spans, bounded Redis/S3/DB spans, trace/log correlation,
  configurable parent-based sampling, exporter-failure isolation, and operator-safe
  trace diagnostics.
- **Batch 4 — production containers + graceful lifecycle + migration strategy.**
- **Batch 5 — operational runbooks, dashboards/alerts, failure-injection expansion,
  CI hardening, security review, acceptance report.**

### Batch 1 — completion record

Delivered on this branch (base `main` @ `3fefb36`), all focused commits:

- Single-winner expired-lease recovery (dialect-aware guarded CAS; exactly one
  winner, exactly one `lease_recovered` event).
- True PostgreSQL `FOR UPDATE SKIP LOCKED` lock-contention test (CI, `postgres:16`).
- Worker-registration generation fencing (opaque token rotated on register; a stale
  generation cannot heartbeat or transition its replacement).
- SQLite claim compare-and-set isolated in a per-candidate SAVEPOINT (a lost race
  rolls back only that attempt and preserves uncommitted caller work).
- Final S3 tenant-key validation (org/workspace segments validated individually and
  the fully composed key re-validated).
- Collision-resistant Redis cache-key encoding (percent-encoded components).
- Additive migration `df66ff0426d2` (nullable `worker_registrations.generation_token`).

Gate at completion: backend **224 passed** / 2 skipped (PG-gated), frontend
**20/20**, smoke **13/13** (four-market isolation, no cross-market contamination),
migration upgrade/check/downgrade/re-upgrade green, ruff clean, no contract drift,
`npm audit` 0 vulnerabilities.

### Batch 2 — scope (this session)

1. **Structured logging.** Centralized JSON logging with a stable field set; a
   human-readable dev formatter behind validated configuration.
2. **Secret redaction.** One recursive redaction layer applied before any structured
   value is emitted; never raises into application code.
3. **HTTP request correlation.** Strict, bounded request-id middleware storing an id
   in request-local context, echoing it in a response header, and clearing context
   after every request.
4. **Durable-job correlation.** A safe opaque correlation id, distinct from the
   database job id / lease token / worker id / tenant ids, generated at enqueue,
   persisted (additive migration), and propagated through the whole job lifecycle.
5. **Worker correlation restoration.** The worker restores a job's correlation id
   into logging context for the duration of execution and clears it afterward.
6. **Provider-neutral metrics abstraction.** Counter/histogram/gauge behind an
   interface with a no-op default and an in-memory test backend; no hosted vendor in
   core code.
7. **Safe metric instrumentation.** Counters incremented only at authoritative
   commit points; durations recorded with monotonic time.
8. **Cardinality controls.** A controlled label allow-list with enforced rejection of
   forbidden high-cardinality labels; routes normalized to templates.
9. **Telemetry failure isolation.** Formatter/redaction/metrics/exporter failure can
   never break a request, a commit, job claiming, worker execution, or shutdown.
10. **Operator-safe observability diagnostics.** Coarse, operator-only telemetry
    status (modes/enabled flags/recent failure counts) with no secrets or ids.

Deferred to later batches (not in Batch 2): distributed tracing, production
containers, graceful deployment lifecycle, deployment runbooks, dashboards and
alerts, and the broad failure-injection suite.

If the draft PR's diff grows beyond what stays reviewable, the remaining
observability and deployment batches split into their own follow-up PRs.

### Batch 2 — completion record

Delivered on this branch as focused commits:

- **Structured logging + redaction** — a stable-field JSON formatter (human-readable
  console formatter in development) with a reusable recursive secret-redaction layer
  applied to every emitted structured value; the formatter never raises into the
  application (`app/core/logging.py`, `app/core/redaction.py`).
- **Request + job correlation** — strict bounded request-id middleware (accepts a
  valid inbound id or mints one, echoes it in a response header, resets context after
  every request) and an opaque durable-job `correlation_id` distinct from the row id /
  lease token / worker id / tenant ids, persisted via additive nullable migration
  `e7c2a9b4f1d3`, restored into the worker (and heartbeat thread) logging context for
  the duration of execution and cleared afterward (`app/core/middleware.py`,
  `app/core/log_context.py`, `app/jobs/service.py`, `app/jobs/worker.py`).
- **Provider-neutral metrics** — a fixed metric-name catalog and a strict label
  allow-list that rejects unknown names and any high-cardinality/identifying label at
  dev/test time; no-op default, in-memory test backend, runtime failures isolated and
  counted (`app/core/metrics.py`).
- **Lifecycle instrumentation** — bounded metrics at authoritative commit points for
  the HTTP path, enqueue, claim, completion, retry/fail/dead-letter, execution
  duration, single-winner lease recovery and Redis notify (counted separately from
  enqueue); idle polls increment an aggregated counter instead of logging.
- **Operator-only telemetry status** — `GET /internal/system/telemetry` (401 anon /
  403 non-operator / 200 operator) reporting logging mode, metrics enabled, exporter
  health, swallowed-failure count, and correlation/redaction flags — bounded status
  only, no ids/URLs/credentials/tokens.

Gate at completion: backend **309 passed** / 2 skipped (PG-gated), ruff clean,
migration head `e7c2a9b4f1d3` upgrade/check/downgrade/re-upgrade green. Full
repository gate (frontend, smoke, contracts, `npm audit`) recorded in
`phase-3a-4b-acceptance.md`.

**Batch 2 is complete.**

### Batch 3 — scope (this session)

Distributed tracing and end-to-end telemetry, delivered as focused commits. Twelve
items:

1. **OpenTelemetry-compatible tracing abstraction.** A pure-Python provider-neutral
   seam (`app/core/tracing.py`) mirroring the Batch 2 metrics seam: no-op default
   tracer, in-memory test exporter, optional import-guarded OTLP exporter, W3C
   `traceparent` propagation. Core code never imports a hosted vendor SDK.
2. **HTTP request spans.** A server span per request when tracing is enabled, extending
   `CorrelationMiddleware`: strict inbound `traceparent` extraction, normalized route
   templates, method + status class, controlled error class, `trace_id` bound into the
   log context and cleared on exit.
3. **Durable-job trace propagation.** A safe W3C `traceparent` captured at enqueue and
   persisted in a new additive nullable `jobs.trace_context` column (migration chaining
   after `e7c2a9b4f1d3`); the worker restores it from the claimed row.
4. **Worker execution spans.** A linked execution span for the whole `run_claimed`
   lifecycle, restored from the persisted parent context.
5. **Dependency spans.** Bounded Redis (notify/cache/lock), S3 (upload/sign-url) and
   controlled DB spans (claim txn, recovery, registration, readiness) — no keys, URLs,
   tenant ids or payloads.
6. **Trace/log correlation.** Logs carry `trace_id` (and optionally `span_id`) only
   while a span is active; no leakage between requests/jobs; redaction preserved; trace
   ids never become metric labels.
7. **Trace/metric correlation.** The existing bounded metrics keep firing at the same
   authoritative commit points; spans and metrics agree on outcome without spans adding
   any high-cardinality label.
8. **Configurable sampling.** Parent-based ratio sampling, conservative default,
   reduced/suppressed for health/readiness and idle polls, validated config.
9. **Exporter-failure isolation.** A collector down at startup degrades to no-op;
   an export failure mid-request/job never breaks the request, the DB transaction, job
   claiming, worker execution, or shutdown; a bounded flush on shutdown.
10. **Operator-safe trace diagnostics.** `GET /internal/system/telemetry` gains coarse
    tracing fields (enabled, provider, exporter state, sampling mode/ratio, export
    failure count, last failure category, flush status) — never an endpoint, credential,
    trace id or span id.
11. **In-memory tracing tests.** Assertions run against the in-memory exporter with no
    external collector.
12. **CI validation.** The existing gates plus the new tracing tests; no real collector,
    hosted vendor, container, or deployment-pipeline change.

Deferred to later batches (not in Batch 3): production containers, graceful deployment
lifecycle, migration-execution containers, deployment/incident runbooks, dashboards and
alerts, the broad failure-injection suite, and Phase 3B.

### Batch 3 — completion record

_(recorded on completion.)_

## Accepted Phase 3A.4a follow-ups (Batch 1 scope)

1. Make expired-lease recovery single-winner under concurrent workers.
2. Add a true simultaneous PostgreSQL `FOR UPDATE SKIP LOCKED` contention test.
3. Add atomic worker registration or registration-generation fencing.
4. Revalidate the fully composed S3 tenant key.
5. Clarify and harden SQLite compare-and-set rollback and retry behavior.
6. Encode Redis key components to prevent delimiter collisions.
7. Measure worker-registry readiness-query performance before adding a composite index.

Item 7 is a measurement task, not a schema change; it is recorded here and carried
into Batch 6 with evidence, so Batch 1 does **not** add a speculative composite index.

## Non-goals

Explicitly excluded from Phase 3A.4b:

- Phase 3B
- Real external connectors
- Paid data providers
- Customer-facing observability UI
- Automated production posting
- Billing changes
- Large product-feature additions
- Redis becoming the authoritative job queue (the database remains authoritative)
- Removing SQLite support
- Changing public API contracts without explicit evidence it is required
- Kubernetes manifests (no approved Kubernetes direction exists in this repo)

## Completion gates

Phase 3A.4b is not complete until all of the following pass. (Batch 1 must satisfy
the Concurrency and Regression gates; later batches satisfy the rest.)

### Concurrency
- Expired-lease recovery has exactly one winner and creates exactly one event.
- True PostgreSQL lock contention is exercised in CI (not two sequential calls).
- Duplicate first registration is handled safely.
- An old registration generation cannot heartbeat or transition the replacement.
- Job lease-token fencing remains intact.
- SQLite compare-and-set semantics remain safe and match documented behavior.

### Observability
- Structured production logs exist with the agreed field set.
- Secret-redaction tests pass for the known secret patterns.
- Request/job correlation propagates across the enqueue→claim→execute→terminal flow.
- Metrics cover API, jobs, workers, Redis, S3, and database.
- Metric labels are bounded (no ids/keys/urls/messages as labels).
- Traces cover the API-to-worker flow; export failure never breaks core behavior.

### Deployment
- Separate API and worker production images exist.
- Both run as non-root and handle SIGTERM.
- Worker drains correctly; API readiness and liveness stay distinct.
- Migration execution is single-actor; no secret is baked into images.

### Operations
- Deployment, migration, worker, and incident-response runbooks exist.
- Alert recommendations and a rollback procedure exist.

### Regression
- Frontend lint, type-check, tests pass.
- Backend Ruff and full suite pass.
- PostgreSQL integration, Redis, and S3 tests pass.
- Migration upgrade/check/downgrade/re-upgrade passes.
- Smoke and four-market isolation pass.
- Generated contracts have no drift.
- `npm audit` reports zero vulnerabilities or every exception is documented
  (without running `npm audit fix`).
