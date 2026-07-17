# Next safe implementation batch — Sandbox Scouting Orchestration & Run History

**Status:** OWNER DECISIONS RECORDED (see §24) — PENDING INDEPENDENT REVIEW & PROTECTED MERGE.
No code, tests, migrations, contracts, dependencies, or CI have been changed by this document.
It records the product owner's decisions only; it invents no legal approval and authorizes no
implementation. Implementation may begin only after this document passes exact-head CI,
receives an eligible independent approval, merges through the protected-branch workflow, and
the exact merge commit passes post-merge CI.

_Independent-review status: NO INDEPENDENT THIRD-PARTY REVIEW COMPLETED._

## 1. Executive summary

The next capability SignalNest can safely build **now** — with no live RSS, no external
egress, no PR #34, and no dependence on the open B-1 (owner) or B-7 (legal/ToS) decisions —
is a **sandbox-only scouting-orchestration and run-history batch** built entirely on the
already-merged durable-job foundation and the deterministic four-market fixture path.

Two facts from source inspection shape this recommendation:

1. **Manual rerun already exists.** `POST /workspaces/{id}/scout-requests/{id}/run`
   (`app/scouting_requests/routes.py:198`) already flips a settled request to `QUEUED` via
   an atomic compare-and-set and enqueues a durable `scout_request.execute` job. The
   frontend already renders live job state through `JobsPanel` with 2-second bounded
   polling. **This must not be rebuilt.**
2. **Recurrence does not exist.** The durable-job model has only a one-shot `scheduled_for`
   field (`app/jobs/models.py`, `store.enqueue`). There is no `next_run`, recurrence rule,
   cron concept, or consolidated per-request run history anywhere in the codebase.

The genuinely-missing, high-leverage, safe work is therefore: (a) a **read-only run-history
surface** aggregating existing `Job` + `JobEvent` rows per scout request, market-scoped; and
(b) **recurring sandbox schedules** implemented as self-chaining durable jobs (reusing the
existing `scheduled_for` primitive and worker claim loop — **no parallel queue, no cron
daemon**), feature-flagged **off by default**. The recurring executions run against the same
deterministic fixtures/sample feeds the demo already uses, so they need **zero network
access** and are safe on by default only in local/dev.

The batch is decomposed into **four controlled sub-batches**, each leaving `main`
deployable, with the no-migration read-history slice first.

**Classification:** `NEXT SAFE IMPLEMENTATION BATCH DEFINED — OWNER DECISIONS RECORDED (§24)`.

## 2. Current verified baseline

| Item | Value |
| --- | --- |
| `main` SHA | `5c652823523c4084dbf04a676b95cb007ea47b43` |
| Working tree | clean; `main` == `origin/main`; ahead/behind `0/0` |
| Alembic head | `0155a5c468e3` (single head) |
| Merged & verified foundations | four-market seed; seed/demo data; connector foundation; durable job execution; operator diagnostics; intelligence read API; intelligence panel; migration & API-contract foundation; Batch 4A–4D closeout |
| Live RSS | disabled (`connector_rss_enabled=False`) |
| PR #34 (`feat/phase-3b-live-rss-controlled-egress`) | OPEN, draft, unmerged, off-limits |
| PR #6 | OPEN, unmerged |
| Ruleset `18820692` "main protection" | active; bypass actors 0; current-user bypass `never` |
| Required ruleset checks | Frontend quality · Backend quality · Migrations and API contract · Integration smoke |
| CI jobs (5) | `frontend-quality`, `backend-quality`, `migration-and-contract`, `container-build`, `integration-smoke` |

> Transparency note: **Container build and security** (`container-build`) runs in CI but is
> **not** presently configured as a required ruleset context. This batch does not change that.

## 3. Why live-RSS Batch 2 remains blocked (and is out of scope here)

The literal next sequenced roadmap item, Batch 2 live HTTP RSS egress (PR #34), is blocked on
**two outstanding owner/legal decisions** recorded in the plan-of-record:

- **B-1** — owner must approve a specific connector for live egress. **Open.**
- **B-7** — legal must confirm RSS/news ToS and legal feasibility in writing. **Open.**

This document **does not** touch PR #34, its branch, `connector_rss_enabled`, or any live
connector, and it **assumes neither B-1 nor B-7**. The recommended batch is deliberately
sandbox-only so it can proceed in parallel while those decisions remain open.

## 4. Candidate comparison

All candidates are grounded in existing code/docs; none require live egress or external APIs.

| # | Candidate | Net-new value | Migration | Owner/legal decision | Notes |
|---|-----------|---------------|-----------|----------------------|-------|
| C1 | **Sandbox Scouting Orchestration & Run History** (recommended) | Recurring sandbox schedules (new) + consolidated run history (new) | Additive (`scout_schedules`) in a later sub-batch | Refinement defaults only (see §24) | Full reuse of durable jobs; self-chaining tick |
| C2 | Read-only Run History only | Consolidated run history (new) | None | None | Effectively sub-batch A of C1; smaller |
| C3 | Manual-rerun UX polish | Low — backend rerun already exists | None | None | Mostly re-presentation; thin |
| C4 | Operator job-fleet UI | Operator-only visibility of existing diagnostics | None | None | No customer/demo value; orthogonal |
| C5 | Retry / dead-letter visibility | Subset of run history | None | None | Absorbed into C1 sub-batch A |

C1 subsumes the safe parts of C2/C3/C5 and sequences them so value ships early. C4 is
deferred as a separate, lower-priority operator surface.

## 5. Candidate scoring

Scored 1–5 (5 = best/lowest-risk). Weights reflect the current goal: ship safe, demo-worthy,
architecturally-leveraged value with zero legal exposure while Batch 2 is blocked.

| Criterion (weight) | C1 | C2 | C3 | C4 | C5 |
|---|---|---|---|---|---|
| Customer value (×3) | 5 | 4 | 2 | 2 | 3 |
| Investor/demo value (×3) | 5 | 4 | 2 | 2 | 3 |
| Architectural leverage (×3) | 5 | 3 | 2 | 2 | 2 |
| Implementation readiness (×2) | 4 | 5 | 5 | 4 | 5 |
| Testability (×2) | 5 | 5 | 4 | 4 | 5 |
| Four-market compatibility (×2) | 5 | 5 | 4 | 3 | 4 |
| Operational safety (×3) | 5 | 5 | 5 | 5 | 5 |
| Security risk — inverse (×3) | 5 | 5 | 5 | 4 | 5 |
| Legal risk — inverse (×3) | 5 | 5 | 5 | 5 | 5 |
| Dependency risk — inverse (×2) | 5 | 5 | 5 | 5 | 5 |
| Future-rework risk — inverse (×2) | 4 | 4 | 3 | 4 | 4 |
| Implementation effort — inverse (×1) | 2 | 5 | 5 | 4 | 5 |
| **Weighted total (max 145)** | **136** | **129** | **112** | **107** | **123** |

**Weights explained.** Customer value, demo value, architectural leverage, operational
safety, and the three risk-inverse axes (security/legal/dependency) carry ×3 because the
current objective is *safe, high-signal progress while a legal gate is open*. Readiness,
testability, four-market fit, rework, and effort carry ×2 or ×1 as tie-breakers.

**Ranking:** C1 (136) > C2 (129) > C5 (123) > C3 (112) > C4 (107).

## 6. Selected recommendation

**Sandbox Scouting Orchestration & Run History (C1).** It reuses the durable-job foundation,
preserves four-market independence and tenant/workspace isolation, keeps intelligence
read-only, avoids Phase 3C feedback and undefined Batch 5 scope, introduces no external
egress, and is decomposable into ≤4 controlled sub-batches with measurable acceptance
criteria. The one new subsystem (recurrence) is expressed as self-chaining durable jobs, not
a parallel scheduler.

## 7. Scope

**In scope:**
1. **Run history (read-only):** a consolidated, market-scoped view of past executions for a
   scout request, derived from existing `Job` + `JobEvent` + `ScoutRequest.stats/last_run_at`.
2. **Recurring sandbox schedules:** a per-scout-request schedule (interval-based) that, when
   enabled, enqueues the request's `scout_request.execute` job on a cadence via a
   self-chaining `scout_schedule.tick` durable job. Sandbox connector resolution only.
3. **Schedule management:** create / pause / resume / delete a schedule; last-run and
   next-run visibility.
4. **Feature flag** `scout_scheduling_enabled` (bool, default `False`) gating all recurrence
   behavior; the read-history surface is inert and safe on by default.
5. **Observability & audit** for schedule lifecycle and tick execution, reusing existing
   metrics, `JobEvent`, and `AuditLog`.

**Roadmap placement (open):** named by capability, not a batch number, pending owner
confirmation of where this sits relative to the deferred live-RSS/Phase-3C sequence.

## 8. Non-goals (explicit)

- Live RSS or any live connector; changing `connector_rss_enabled`.
- External social-platform integrations; arbitrary connector selection or URL entry.
- Autonomous posting, ad publishing, or any outbound egress.
- Phase 3C human-feedback capture, votes, ratings, or learning loops.
- Any undefined Batch 5 behavior.
- PR #34 code, its branch, or any B-1/B-7 assumption.
- Operator **force-retry** endpoint (retries remain automatic/backoff-driven; UI only
  *presents* retry state).
- Cross-market shared schedules unless a user explicitly opts in (default is per-request).
- Any change to intelligence write/read semantics (stays read-only).

## 9. UX flow (screen-by-screen)

All surfaces live on the existing **Scout Request Detail** page
(`apps/web/src/pages/ScoutRequestDetail.tsx`), which already hosts `JobsPanel`.

**A. Run History panel (sub-batch A)**
- *Entry point:* Scout Request Detail → "Run history" section below the existing Jobs panel.
- *No-data state:* neutral "No runs yet" (not an error) for requests never executed.
- *Loading state:* skeleton rows (reuse `LoadingRows`).
- *Error state:* `ErrorState` with retry and correlation id.
- *Success state:* reverse-chronological list of runs; each row shows outcome badge
  (succeeded/failed/dead-lettered/cancelled), started/completed timestamps (relative),
  attempt count, and the per-run stats (`scanned`, `noise_filtered`, `signals_analyzed`,
  `opportunities`) sourced from the job result summary / request stats.
- *Simulated labeling:* each row carries a "Simulated" tag (all sandbox executions are
  `is_simulated=True`).
- *Last-run display:* header shows `last_run_at` relative time.

**B. Schedule panel (sub-batches B–C, behind `scout_scheduling_enabled`)**
- *Primary action:* "Schedule runs" → dialog to choose an interval from the **bounded enum**
  (owner-approved v1 set: **daily / weekly**, 24h minimum — §24.3) and enable.
- *Confirmation:* explicit enable; shows the computed **next-run** time.
- *Running/queued/completed/failed/cancelled states:* continue to surface through the
  existing `JobsPanel` for the concrete execution jobs; the schedule row shows enabled/paused
  and next-run.
- *Retry guidance:* presentational only — "will retry automatically" copy; **no** force-retry
  button.
- *Four-market switching:* the panel is request-scoped; switching the active market/location
  loads a distinct query key so no stale cross-market data is shown.
- *Mobile/responsive & accessibility:* reuse existing Card/skeleton/`role="alert"` patterns;
  dialog is keyboard-navigable; status changes use `aria-live`.

## 10. Backend architecture

Reuse-first. **No parallel queue.**

**Run history (sub-batch A):**
- *Read service* `app/scouting_requests/run_history.py`: query `Job` filtered by
  `scout_request_id` within the tenant/workspace scope (existing isolation path), join
  bounded `JobEvent` summary, map into a public run-history payload. Read-only; no writes.
- *Route:* `GET /workspaces/{workspace_id}/scout-requests/{request_id}/runs` (new, read-only)
  — or, if preferred, reuse the existing `GET /jobs?scout_request_id=...` + `/jobs/{id}/events`
  and compose client-side. Plan recommends the dedicated read endpoint for a bounded,
  N+1-free contract mirroring the intelligence read pattern.

**Recurrence (sub-batches B–C):**
- *New job type* `JobType.SCOUT_SCHEDULE_TICK = "scout_schedule.tick"` (additive enum value).
- *Handler* `@register_handler(SCOUT_SCHEDULE_TICK)`: on execution, within one transaction —
  (1) load the schedule; if disabled, no-op and stop the chain; (2) enqueue the request's
  `scout_request.execute` job via existing `enqueue_scout_request` (idempotency key derived
  from `schedule_id + occurrence_timestamp` so a re-delivered tick cannot double-enqueue);
  (3) compute the next occurrence and enqueue the next `scout_schedule.tick` with
  `scheduled_for = next_occurrence`; (4) update `scout_schedules.last_tick_at` / `next_run_at`.
- *Bootstrap:* creating/enabling a schedule enqueues the first tick with
  `scheduled_for = first_occurrence`; pausing/deleting marks the schedule disabled so the next
  tick self-terminates (no orphaned recurring work).
- *Service* `app/scouting_requests/schedules.py`: create/pause/resume/delete with tenant scope,
  transaction ownership at the service boundary, `AuditLog` writes
  (`scout_schedule.created/paused/resumed/deleted`).
- *State transitions:* schedule is a simple `enabled|paused` flag plus `next_run_at`; the
  actual execution lifecycle stays entirely inside the existing durable-job state machine.
- *Timing & occurrence semantics (owner-approved, §24.4–24.5):* all schedule timestamps are
  stored in **UTC**; next occurrence is **pure interval-from-enable** (`next_run_at =
  last_tick_at + interval`), not clock-of-day — so there is **no timezone or DST handling** in
  v1. **Missed occurrences are skipped** (no catch-up/backfill): a late tick simply computes the
  next *future* occurrence. **Overlapping runs coalesce:** the tick skips the execute-enqueue
  when the same scout request already has an active run, and the `schedule_id +
  occurrence_timestamp` idempotency key remains the final durable guard.
- *Feature flag:* every schedule route and the tick handler are inert unless
  `scout_scheduling_enabled=True`.
- *Metrics:* reuse existing job counters; add low-cardinality
  `scout_schedule_ticks_total{outcome}` and `scout_schedules_active` gauge only if needed
  (labels are outcome/enabled — never IDs).
- *Error handling:* tick failures ride the existing retry/backoff/dead-letter path; a
  dead-lettered tick stops the chain and is visible in diagnostics — it never blocks other
  markets or schedules.

## 11. API contracts

| Method | Path | Auth/role | Request | Response | Errors | Status |
|--------|------|-----------|---------|----------|--------|--------|
| GET | `/workspaces/{ws}/scout-requests/{id}/runs` | any member (TenantContext) | `?limit&offset` (bounded: default 20, max 100) | `RunHistoryOut { items: RunItem[], total, limit, offset }`; each `RunItem` carries outcome, timestamps, attempt count, aggregate stats, `trigger` (`manual`\|`scheduled`), `is_simulated` | 401/403/404 | **new** |
| GET | `/workspaces/{ws}/scout-requests/{id}/schedule` | any member | — | `ScheduleOut \| null` | 401/403/404 | **new** (flagged) |
| POST | `/workspaces/{ws}/scout-requests/{id}/schedule` | EDITORS (owner/admin/marketer) | `{ interval: enum(daily\|weekly), enabled: bool }` | `ScheduleOut` | 400/401/403/404/409 | **new** (flagged) |
| POST | `/workspaces/{ws}/scout-requests/{id}/schedule/pause` | EDITORS | — | `ScheduleOut` | 401/403/404 | **new** (flagged) |
| POST | `/workspaces/{ws}/scout-requests/{id}/schedule/resume` | EDITORS | — | `ScheduleOut` | 401/403/404 | **new** (flagged) |
| DELETE | `/workspaces/{ws}/scout-requests/{id}/schedule` | EDITORS | — | 204 | 401/403/404 | **new** (flagged) |
| POST | `/workspaces/{ws}/scout-requests/{id}/run` | EDITORS | — | `ScoutRunResult` | 400/401/403/404 | **exists, unchanged** |
| GET | `/workspaces/{ws}/jobs` · `/jobs/{id}` · `/jobs/{id}/events` | member | — | existing | existing | **exists, unchanged** |
| POST | `/workspaces/{ws}/jobs/{id}/cancel` | EDITORS | — | `JobOut` | existing | **exists, unchanged** |

No live-connector or source-management APIs are added. New endpoints are minimized (one
read + a small schedule CRUD, all flag-gated except run history).

## 12. Database impact

- **Sub-batch A (run history):** **no migration.** Reuses `jobs`, `job_events`,
  `scout_requests`.
- **Sub-batches B–C (recurrence):** **one additive migration** — new table `scout_schedules`.
  No existing table altered; no destructive operation; single head preserved.

Proposed `scout_schedules` (all NOT NULL unless noted):

| Column | Type | Notes |
|--------|------|-------|
| `id` | str PK | deterministic where seeded via `sid()` |
| `organization_id` | FK organizations | tenant scope |
| `workspace_id` | FK workspaces | tenant scope |
| `location_id` | FK business_locations, nullable | market scope |
| `scout_request_id` | FK scout_requests | subject |
| `interval` | str (bounded enum) | v1 set `daily`\|`weekly` (§24.3) |
| `enabled` | bool, default `False` | pause flag |
| `next_run_at` | datetime (UTC), nullable | computed (interval-from-enable, §24.4) |
| `last_tick_at` | datetime (UTC), nullable | audit |
| `created_at` / `updated_at` | datetime | standard |

- **Indexes:** `(workspace_id)`, `(scout_request_id)`, `(location_id)`.
- **Uniqueness:** `uq_scout_schedule_request (workspace_id, scout_request_id)` — one schedule
  per request per workspace (per-market isolation preserved via distinct requests/locations).
- **Upgrade:** create table + indexes + constraint (additive, safe on live DB).
- **Downgrade:** drop table + indexes only; no other data touched.
- **Compatibility:** pre-existing requests simply have no schedule row (valid null state); no
  backfill.

## 13. Frontend architecture

- *Pages/components:* `RunHistoryPanel.tsx` and `SchedulePanel.tsx` under
  `apps/web/src/pages/scouts/`, mounted on Scout Request Detail below `JobsPanel`.
- *Hooks:* `useScoutRunHistory`, `useScoutSchedule` (query) + `useScheduleMutations`
  (create/pause/resume/delete), mirroring `useOpportunityIntelligence` conventions
  (disabled until IDs present; skip retry on 4xx; 200-with-null is a neutral empty).
- *Query keys* (extend `apps/web/src/api/queryKeys.ts`, embedding every scope dimension):
  - `scoutRequestRuns: (workspaceId, requestId) => ['workspaces', workspaceId, 'scout-requests', requestId, 'runs']`
  - `scoutRequestSchedule: (workspaceId, requestId) => ['workspaces', workspaceId, 'scout-requests', requestId, 'schedule']`
- *API client:* add `listScoutRuns`, `getScoutSchedule`, `createScoutSchedule`,
  `pauseScoutSchedule`, `resumeScoutSchedule`, `deleteScoutSchedule` in `endpoints.ts`,
  reusing `apiRequest`/`ApiError`/`AbortSignal`.
- *Types:* regenerate `schema.d.ts` from the updated OpenAPI in the implementation PR (kept in
  sync by the existing contract gate — no hand-editing).
- *Route protection / cache isolation:* request- and workspace-scoped keys; switching market
  or workspace yields distinct cache entries (no stale bleed).
- *Polling:* reuse `JobsPanel`'s bounded 2s poll-while-active for live executions; run history
  refetches on demand / invalidation, not on a tight loop.
- *States:* loading/empty/error/success per the intelligence-panel pattern; schedule dialog is
  gated/hidden when the backend reports scheduling disabled.

## 14. Four-market behavior (Dallas · London · Lagos · Nairobi)

- Each scout request is already single-market (`resolved_market` + `location_id`); schedules
  and runs inherit that scope. A schedule for the Dallas request only ever enqueues the Dallas
  request's execution job.
- Each execution is an **independent** durable job with independent state; a failed/dead-
  lettered London tick or run does **not** block Dallas, Lagos, or Nairobi.
- Run-history and schedule queries are keyed by `(workspace, request)`; switching the active
  market loads a distinct cache entry, so no stale cross-market data renders.
- Connector resolution stays on the deterministic per-market fixtures/sample feeds, so the
  four markets remain byte-isolated exactly as verified in the seed/closeout suites.
- No shared/global schedule is created unless a user explicitly opts in (default per-request).

## 15. Authorization matrix

Uses existing roles only (`app/core/enums.py`): OWNER(4), ADMIN(3), MARKETER(2), REVIEWER(1),
COMPLIANCE_REVIEWER(1), VIEWER(0). `EDITORS` = OWNER/ADMIN/MARKETER (as used by the existing
run endpoint).

| Action | Owner | Admin | Marketer | Reviewer | Compliance reviewer | Viewer | Platform operator |
|--------|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| View run history | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ (diagnostics) |
| View schedule | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Manual run (existing) | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | — |
| Create/pause/resume/delete schedule | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | — |
| Cancel a job (existing) | ✅ | ✅ | ✅ | ❌ | ❌ | ❌ | — |
| Force-retry | ❌ (endpoint does not exist) | | | | | | |

Platform operators retain only the existing **read-only** fleet/diagnostics visibility; this
batch adds no operator mutation.

## 16. Security / threat analysis

| Threat | Mitigation |
|--------|------------|
| Cross-tenant / cross-workspace access | All routes go through the existing `get_tenant_context` scoping; object lookups are workspace-scoped (reuse `_get_scoped`). |
| Cross-market bleed | Request-scoped schedules/runs; fixture resolution is per-market; query keys include workspace+request. |
| Duplicate/replayed schedule tick → duplicate work | Idempotency key `schedule_id + occurrence_ts` on the enqueued execute job; existing tenant-scoped idempotency constraint is the final guard. |
| Unauthorized execute/cancel/schedule | EDITORS gate on all mutating routes; reviewers/viewers read-only. |
| Payload abuse / oversized input | Interval is a bounded server-side enum; existing `job_max_payload_bytes` guard applies; no free-text URL/connector input accepted. |
| Arbitrary connector / URL selection | Not exposed; connector resolution stays internal and defaults to fixtures; `connector_rss_enabled` untouched. |
| External egress | None: sandbox fixtures/sample feeds only; no HTTP client on this path. |
| Secret / payload / lease-token leakage | Reuse existing redactor; run-history payload is bounded (no raw payloads, lease tokens, worker IDs, or source text). |
| Runaway recurrence | Self-chaining tick stops when schedule disabled; bounded interval enum; optional per-workspace active-schedule cap (§24); dead-lettered tick terminates the chain. |
| Disclosure of simulation status | Every run/opportunity keeps `is_simulated=True` labeling in the UI. |

## 17. Operational requirements

- *Metrics:* reuse `JOBS_ENQUEUED/CLAIMED/COMPLETED/FAILED/RETRIED/DEAD_LETTERED_TOTAL`,
  `JOB_EXECUTION_DURATION_MS`, `JOBS_QUEUE_DEPTH`; optionally add
  `scout_schedule_ticks_total{outcome}` and `scout_schedules_active` (low-cardinality only).
- *Logs/traces:* tick execution rides the existing correlation/trace propagation and
  `JobEvent` trail; schedule lifecycle writes `AuditLog`.
- *Latency/failure/retry/cancel visibility:* already surfaced by durable-job metrics and the
  operator diagnostics endpoints; run history exposes per-run outcomes to customers.
- *Stale-job recovery:* unchanged — lease/heartbeat recovery already covers tick and execute
  jobs.
- *Readiness impact:* none; no new external dependency, no new required worker type.
- *Rollback:* layered (see §21).
- *Feature flag:* `scout_scheduling_enabled=False` by default; enabling is an ops action.
- *Deployment sequencing:* migrate before deploying code that reads/writes `scout_schedules`
  (existing startup schema guard enforces this).
- *No new monitoring vendor.*

## 18. Test plan

Every DB test uses temporary/disposable SQLite or dependency overrides; **no** test touches
the persistent `signalnest.db` or the public network (matches existing suite conventions).

- **Backend unit:** next-occurrence computation; schedule enable/pause/resume/delete service
  logic; idempotency-key derivation.
- **Backend API:** run-history endpoint shape & pagination; schedule CRUD happy/again/error
  paths; flag-off returns inert/withdrawn behavior.
- **Durable-job:** tick handler enqueues execute job + next tick; disabled schedule stops the
  chain; dead-lettered tick terminates cleanly (extend `test_durable_jobs.py` patterns).
- **Authorization/isolation:** reviewers/viewers cannot mutate; cross-workspace/cross-request
  access denied (extend `test_api_isolation.py`).
- **Four-market:** four independent schedules/runs; one market's failure does not affect the
  others; no cross-market bleed (extend the closeout four-market pattern).
- **Frontend component:** RunHistoryPanel and SchedulePanel loading/empty/error/success;
  flag-off hides schedule controls; market switch shows no stale data (MSW handlers).
- **Frontend API-client:** new endpoint functions issue correctly-scoped calls; 4xx not
  retried.
- **Contract/OpenAPI:** regenerate `openapi.json`/`schema.d.ts`; contract-drift gate passes.
- **Migration:** `scout_schedules` up/down round-trip on throwaway SQLite; single head
  preserved (extend `test_signal_intelligence_migration.py` pattern).
- **Integration smoke:** existing `integration-smoke` job stays green.
- **Manual QA:** enable flag in a local seeded DB; create a daily schedule for the Dallas
  request; confirm ticks chain, executions appear in run history, pause stops the chain,
  delete removes the schedule — all offline.

## 19. Acceptance criteria (objective pass/fail)

1. No external egress on any new path (no HTTP client imported).
2. Live RSS remains disabled; `connector_rss_enabled` unchanged; PR #34 unmerged.
3. Four markets execute independently; one market's failure blocks no other.
4. A duplicate manual run or a replayed tick does **not** create duplicate execution jobs.
5. Reviewer/viewer roles cannot create/pause/resume/delete schedules, run, or cancel.
6. Run history accurately reflects job outcomes and per-run stats.
7. Loading/empty/error/success states are usable; empty is neutral (not an error).
8. No cross-workspace or cross-market leakage; query keys carry every scope dimension.
9. No raw payload, lease token, worker ID, or source text is exposed by any new response.
10. API/OpenAPI contract gate passes; `schema.d.ts` matches `openapi.json`.
11. Alembic remains a single head; migration is additive with a working downgrade.
12. All five CI jobs run; the four required ruleset checks (Frontend quality, Backend quality,
    Migrations and API contract, Integration smoke) are green.
13. `scout_scheduling_enabled=False` by default; all recurrence behavior is inert when off.
14. No Phase 3C feedback behavior; no undefined Batch 5 behavior.

> Note: **Container build and security** runs in CI but is not a required ruleset context;
> this batch does not change that configuration.

## 20. Sub-batch sequence (≤4, each leaves main deployable)

**SB-A — Run history (read-only, no migration).**
- *Scope:* read service + `GET .../runs` endpoint + `RunHistoryPanel` + hook/query key.
- *Files (likely):* `app/scouting_requests/run_history.py`, `routes.py`, `schemas.py`;
  `apps/web/.../scouts/RunHistoryPanel.tsx`, `endpoints.ts`, `queryKeys.ts`,
  regenerated contracts; new backend+frontend tests.
- *Tests/acceptance:* criteria 1,3,6,7,8,9,10,12. *Rollback:* hide panel / withdraw endpoint.
- *Must not start yet:* any schedule table or job type.

**SB-B — Recurrence data model + tick (backend, flag-off).**
- *Scope:* `scout_schedules` migration; `SCOUT_SCHEDULE_TICK` job type + handler; schedule
  service; `scout_scheduling_enabled` flag (default False).
- *Files:* new migration; `app/jobs/status.py`, `handlers.py`; `app/scouting_requests/
  schedules.py`, `models.py`; `app/core/config.py`; durable-job + migration tests.
- *Acceptance:* criteria 4,5,11,13,14. *Rollback:* flag off → inert; `alembic downgrade` drops
  table. *Must not start yet:* schedule UI.

**SB-C — Schedule management API + UI (flag-gated).**
- *Scope:* schedule CRUD routes; `SchedulePanel` + hooks/mutations; last-run/next-run display.
- *Files:* `routes.py`, `schemas.py`; `apps/web/.../scouts/SchedulePanel.tsx`, `endpoints.ts`,
  `queryKeys.ts`, contracts; component + API-client + isolation tests.
- *Acceptance:* criteria 5,7,8,13. *Rollback:* flag off hides controls; endpoints inert.

**SB-D — Four-market hardening + observability + closeout.**
- *Scope:* four-market schedule/run isolation tests; optional schedule metrics; ops doc note;
  end-to-end closeout suite.
- *Files:* tests; `docs/operations/*` note; optional metric registration.
- *Acceptance:* full §19. *Rollback:* docs/tests only.

## 21. Rollback strategy (layered)

1. **Frontend panels** — remove/hide `SchedulePanel`/`RunHistoryPanel`; rest of the app intact.
2. **Feature flag** — `scout_scheduling_enabled=False` makes all recurrence inert; existing
   schedules stop chaining on the next disabled tick.
3. **API** — schedule endpoints can return inert/withdrawn; run-history endpoint can return
   empty; contracts stay backward-compatible.
4. **Schema** — `alembic downgrade` drops only `scout_schedules`; jobs/requests untouched.

Because everything is additive and flag-gated, every layer degrades to the pre-batch behavior
without error and without touching Phase 2 opportunity or Batch 3/4 intelligence data.

## 22. Dependencies

- Durable-job foundation (present, verified).
- Scout-request manual-run endpoint + fixtures/sample feeds (present, verified).
- Migration & API-contract gate + generated `schema.d.ts` pipeline (present).
- No external service, vendor, or network dependency. No dependency on B-1, B-7, PR #34,
  PR #6, Phase 3C, or Batch 5.

## 23. Open questions — RESOLVED by §24

All questions previously open here have been resolved by the recorded owner decisions in §24:

1. Interval set & floor → `{daily, weekly}`, 24h minimum (§24.3).
2. Per-workspace active-schedule cap → 4 (§24.3).
3. Dedicated run-history endpoint vs. client composition → dedicated endpoint (§24.1).
4. Run-history retention/pruning policy → deferred; use existing job retention for now (§24.10).
5. Roadmap number/placement → deferred (§24.10).

## 24. Owner decisions — RECORDED (owner-approved)

The product owner has reviewed this plan and recorded the decisions below. They are binding
inputs for the implementation sub-batches; no decision here assumes or requires any B-1/B-7
legal approval. This section records decisions only — it authorizes no implementation (see the
closing note and §26 for the required CI → independent approval → protected merge → post-merge
CI gate).

**24.1 Run history (D3, D7, D8, D9) — approved**
- Dedicated, read-only, request-scoped run-history endpoint.
- Returns outcome, timestamps, attempt count, aggregate statistics, `trigger` type, and
  simulated-data status.
- Reverse-chronological offset pagination; default 20, maximum 100.
- Includes queued, running, succeeded, failed, dead-lettered, and cancelled runs.
- Never exposes raw payloads, payload hashes, idempotency keys, lease tokens, trace context,
  worker IDs, host details, or raw exception messages.

**24.2 Scheduling feature flag (D6) — approved**
- Flag `scout_scheduling_enabled`, default `False`.
- Run history remains available independently of this flag.
- Scheduling deploys dark first, then is enabled in development before any production opt-in.
- Live RSS remains separately disabled.

**24.3 Recurrence (D1, D4) — approved**
- Daily and weekly recurrence only in v1.
- Minimum interval 24 hours.
- One active schedule per scouting request; no more than four active schedules per workspace.
- Every schedule scoped to one request and one market.

**24.4 Time behavior (D17) — approved**
- All schedule timestamps stored in UTC.
- Pure interval-from-enable timing (not clock-of-day).
- No timezone or daylight-saving behavior in v1.

**24.5 Missed and overlapping runs (D18, D19) — approved**
- Skip missed occurrences; no catch-up or backfill.
- Continue by computing the next future occurrence.
- Coalesce overlapping runs: skip a scheduled enqueue when the same scouting request already
  has an active run.
- Preserve the durable idempotency guard keyed on `schedule_id + occurrence_timestamp`.

**24.6 Schedule lifecycle (D11) — approved**
- Pausing keeps the record but makes it inert; resuming recalculates the next future occurrence.
- Deleting permanently removes the schedule record; no soft-delete behavior in v1.

**24.7 Permissions (D10) — approved**
- Owners, admins, and marketers may manually run and create/edit/pause/resume/delete schedules.
- All existing workspace members may view run history and schedule status.
- Reviewers, compliance users, and viewers remain read-only.
- No new roles; no force-retry endpoint.

**24.8 Database scope (D12) — approved**
- SB-A requires no migration; SB-B may add one additive `scout_schedules` table.
- No existing job table is altered merely to support scheduling.
- No timezone or soft-delete columns in v1.
- Single Alembic head and a valid downgrade preserved.

**24.9 Implementation sequence (D-seq) — approved**
- Order SB-A → SB-B → SB-C → SB-D; begin with SB-A (run-history foundation).
- Backend API contracts frozen before parallel frontend integration.
- No more than two writing agents and one read-only verifier.
- Each sub-batch independently deployable and separately reviewable.

**24.10 Deferred (D5, D16)**
- Final roadmap phase/batch number deferred.
- Dedicated run-history pruning/retention policy deferred; use existing job retention for now.

**24.11 Explicit exclusions (owner)**
- No live-RSS enablement; no modifications to PR #34; no Phase 3C; no undefined Batch 5.
- No external network egress, arbitrary connector URLs, autonomous posting, or ad publishing.

No legal decision is required: the batch is sandbox-only with zero external egress, so B-7 and
B-1 remain untouched and irrelevant to it.

## 25. Definition of done

- All §19 acceptance criteria pass on the implementation PR head and again on the post-merge
  exact-SHA CI run.
- Alembic single head preserved; contract gate green; all five CI jobs run and the four
  required checks are green.
- Feature flag defaults off; live RSS still disabled; PR #34 still unmerged.
- Docs updated (`docs/operations/*` gains a short scheduling note; this plan referenced).
- No net-new external dependency; no Phase 3C/Batch 5 behavior.

## 26. Proposed implementation branch and PR workflow

- **Branch (per sub-batch):** `feat/scout-orchestration-run-history` (SB-A), then
  `feat/scout-orchestration-recurrence` (SB-B), etc. — one PR per sub-batch.
- **Commits:** small, conventional (`feat(...)`, `test(...)`, `docs(...)`), signed as configured.
- **Draft PR** opened early per sub-batch; targets `main`; do not mark ready until CI is green
  and the owner has reviewed.
- **Reviewer:** requested only when the owner authorizes.
- **CI gates:** all five jobs must run; the four required ruleset checks must be green.
- **Exact-head approval:** approval must reference the exact head SHA under the active ruleset.
- **Merge:** squash-merge via the standard protected-branch flow; **no** admin merge, **no**
  bypass, **no** force-push, ruleset `18820692` unchanged.
- **Post-merge:** verify CI on the exact merged SHA; then delete the remote feature branch and
  clean the local branch. Safety branch and live-RSS branch remain untouched.

---

_This document is planning only. The owner's §24 decisions are recorded, but this document
still authorizes no implementation: work may begin only after it passes exact-head CI, receives
an eligible independent approval, merges through the protected-branch workflow (ruleset
`18820692` unchanged, no bypass, no force-push), and the exact merge commit passes post-merge
CI._
