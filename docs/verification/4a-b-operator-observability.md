# 4A-B — Backend Read-Only Operator Observability

**Phase:** 4A, batch 4A-B (backend read-only operator observability).
**Nature:** additive operator-only read routes + one centralized stuck-job
classifier + additive response schemas + router registration + backend tests +
regenerated contract + docs — **no new migration, no schema/model change, no
mutation endpoint, no capability override, no UI, no feature enabled.**
**Branch:** `feat/phase-4a-b-operator-observability` (from `main` at
`51801b476d73a6d67f6a5b32cb313af9eb715e7a`, i.e. immediately after the Phase 4A
plan merge, PR #67).
**Alembic head:** single head `4945b98229e6` — **unchanged**; 4A-B adds no migration.

## Scope & non-goals

4A-B extends the existing operator disclosure tier (`/internal/system/*`,
`require_operator`) with read-only observability over the durable-job queue, the
worker fleet and scouting schedules. Every route is operator-gated, cross-tenant
by design (operator diagnostics), and returns only the already-established
secret-free operator projection. It deliberately does **not**: add any migration,
model column or index; add any mutation, requeue, retry, cancel or recovery
action; add any capability override or activation control (that is a later batch,
4A-C); add any frontend; enable any feature flag; or change any job-execution,
worker or scheduling behaviour.

Worker-fleet/heartbeat visibility (plan §7.7) was already served by the existing
`GET /internal/system/workers` route (`app.system.internal_routes`); 4A-B reuses
it and additionally composes fleet health into the new `/overview` snapshot
rather than duplicating it.

## Endpoints

All nested under the operator tier, all `require_operator`, all read-only:

```
GET /api/v1/internal/system/overview                     -> operational snapshot
GET /api/v1/internal/system/jobs/list                    -> bounded/filtered job page
GET /api/v1/internal/system/jobs/stuck                   -> live stuck-job summary
GET /api/v1/internal/system/jobs/dead-letter             -> dead-letter count + page
GET /api/v1/internal/system/jobs/{job_id}                -> single-job operator detail
GET /api/v1/internal/system/jobs/{job_id}/events         -> sanitized event timeline
GET /api/v1/internal/system/schedules                    -> schedule visibility
```

- **Authorization** — every route requires an authenticated operator. Anonymous →
  401; authenticated non-operator → 403. Operator status is the server-controlled
  `user.is_operator`, never derived from client input.
- **Privacy** — responses use the existing `JobOperatorOut` / `JobEventOut`
  projections and a new operator `ScheduleOperatorOut`. None expose a raw payload,
  lease token, correlation id, trace context, or a raw error summary secret. The
  event timeline reuses the customer-safe `JobEventOut` (it omits `worker_id`).
- **Route ordering** — the static `/jobs/list`, `/jobs/stuck`, `/jobs/dead-letter`
  paths are declared before the parametric `/jobs/{job_id}` so they are not
  captured by the path parameter.
- **Bounds** — listings clamp `limit` (1–200; events 1–500) and `offset` (≥0) via
  FastAPI `Query`, so an out-of-range value is a 422 and the surface is never
  unbounded.

## Centralized stuck-job classification (plan §8.10)

- `apps/api/app/jobs/stuck.py` — a single source of truth for "stuck": one pure
  predicate `is_job_stuck(job, *, now, stale_after_seconds)` and the matching SQL
  conditions `_stuck_conditions(...)` kept in lockstep, plus `count_stuck` /
  `list_stuck`. A job is stuck iff its status is in the claimed/running candidate
  set **and** (its lease deadline has passed **or** its heartbeat is older than the
  configured worker stale threshold). Evaluated live against an injected clock —
  never persisted — mirroring the worker-registry "stale is derived from heartbeat
  age" principle and the store's lease-recovery predicate.
- `CANCEL_REQUESTED` is deliberately excluded (a cooperative stop already in
  progress), matching the recovery-candidate set.
- The heartbeat-staleness threshold is the configured
  `worker_stale_after_seconds` (`60.0`), the same bound the worker registry uses —
  resolving the plan's open question on threshold source.

## Deliverables

### Classifier
- `apps/api/app/jobs/stuck.py` — `STUCK_CANDIDATE_STATUSES`, `is_job_stuck`,
  `_stuck_conditions`, `count_stuck`, `list_stuck`.

### Schemas
- `apps/api/app/jobs/schemas.py` — three additive operator schemas: `JobPageOut`
  (filterable job page), `StuckJobsOut` (self-describing live count + clock +
  page), `DeadLetterJobsOut` (dead-letter count + page). All reuse the existing
  `JobOperatorOut` item projection.

### Routes
- `apps/api/app/system/internal_observability_routes.py` — the seven read-only
  operator routes above plus their operator-safe inline schemas
  (`OperationalOverviewOut` and its `jobs`/`workers`/`schedules` sub-shapes,
  `ScheduleOperatorOut`, `ScheduleFleetOut`). Schedule `state` is derived live via
  `derive_schedule_state` so it can never drift from actual job state.
- `apps/api/app/api/router.py` — registers `internal_observability_router` in the
  aggregate router (after the existing internal system router).

### Contract
- `apps/api/openapi.json` and `apps/web/src/api/schema.d.ts` regenerated via
  `scripts/gen-types.sh` — **purely additive**: seven new operator paths and the
  new schemas; zero removals, no change to any existing path or schema. Generation
  is idempotent (a second run produces no diff).

### Tests
- `apps/api/app/tests/test_operator_observability_api.py` — **51 tests** over a
  self-contained two-tenant SQLite graph (`get_db` overridden, rows inserted
  directly for full lifecycle/lease/heartbeat control):
  - **Pure classifier units** — expired lease, stale heartbeat, fresh lease+
    heartbeat, no-lease-no-heartbeat, exact-threshold boundary (equal is not
    stale, one second past is), and every non-candidate status (pending/scheduled/
    retry_wait/terminal/**cancel_requested**) never stuck.
  - **Authorization** — 401 anonymous and 403 non-operator on all seven routes.
  - **Overview** — shape, cross-tenant counts, stuck=1, dead-letter=2, derived
    schedule state counts, secret-free.
  - **Jobs listing** — envelope, cross-tenant total, per-tenant workspace/status/
    type/scout-request/location filter isolation, bounded+stable pagination, limit
    bounds 422, invalid-enum 422, operator-safe projection (payload never echoed;
    `payload_hash`/`worker_id` present as safe diagnostics).
  - **Stuck listing** — only the stuck job, self-describing clock + threshold,
    bounds.
  - **Dead-letter** — all dead-lettered jobs cross-tenant.
  - **Job detail + events** — 200/404, sanitized oldest-first timeline that omits
    `worker_id`, unknown-job 404, event limit bounds.
  - **Schedules** — all three derived states (active/activation_required/paused),
    workspace-filter isolation, safe projection.
  - **Non-mutating** — a snapshot of jobs/events/schedules is unchanged after
    exercising every read.

### Docs
- This verification doc.

## Validation evidence (local)

- **Ruff:** clean across `apps/api`.
- **Backend pytest:** **696 passed, 8 skipped** (PostgreSQL-gated), 0 failed. New
  suite `test_operator_observability_api.py`: **51 passed**.
- **Alembic:** single head `4945b98229e6`, **unchanged** — 4A-B adds no migration.
- **Contract:** `scripts/gen-types.sh` regenerated `openapi.json` + `schema.d.ts`
  with only additive operator changes; regeneration is idempotent (no drift).
- **Frontend:** `eslint` clean; `tsc -b --noEmit` clean; `vitest` 76 passed.

## Governance & safety posture (unchanged)

- `opportunity_feedback_enabled = False`; `scout_scheduling_enabled = False`;
  `connector_rss_enabled = False` — all capabilities remain **dark**.
- No mutation, requeue, recovery, capability override or activation control added.
- No worker, scheduler or scoring behaviour changed.
- No production rollout; no feature enabled.

## Closeout status

Deliverable is a **DRAFT** 4A-B PR targeting `main`. Do not mark ready, request
review, or merge as part of this batch. All capabilities remain dark. Capability
overrides / activation control (4A-C) and any operability *actions* (requeue,
recovery) are separate, later, explicitly-approved batches and are not started
here.
