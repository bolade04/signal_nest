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

- **Batch 1 (this session) — concurrency and data-plane hardening.** The seven
  accepted Phase 3A.4a follow-ups below that concern correctness under concurrency
  and hostile input. Opened as a **draft** PR once green.
- **Batch 2 — structured logging, redaction, request/job correlation.**
- **Batch 3 — bounded metrics.**
- **Batch 4 — distributed tracing.**
- **Batch 5 — production containers + graceful lifecycle + migration strategy.**
- **Batch 6 — operational runbooks, dashboards/alerts, failure-injection expansion,
  CI hardening, security review, acceptance report.**

If the draft PR's diff grows beyond what stays reviewable, the remaining
observability and deployment batches split into their own follow-up PRs.

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
