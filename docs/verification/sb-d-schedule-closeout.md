# SB-D — Four-Market Scheduling Hardening & Closeout

**Phase:** 3B, batch SB-D (hardening + operational readiness + closeout).
**Nature:** tests, documentation, and verification only — **no new product
capability, no schema change, no feature flag enabled.**
**Branch:** `feat/sb-d-schedule-hardening-closeout` (from `main` at
`6fe83f33c6476e55c558dd17d2a7eacf095ee895`).
**Alembic head:** single head `b2c3d4e5f6a7` (unchanged; no new migration).

Markets exercised: **Dallas (TX, USA), London (UK), Lagos (Nigeria), Nairobi
(Kenya)** — four independent tenants proving cross-market isolation.

## Scope & non-goals

SB-D proves the *existing* schedule system behaves correctly across multiple
independent markets and operational states, and delivers operator enablement /
rollback docs. It deliberately does **not**: enable `scout_scheduling_enabled` or
`connector_rss_enabled`; add hourly/monthly/custom-cron recurrence, timezones/DST,
missed-run backfill, or force-retry; raise the four-schedule workspace cap or the
one-schedule-per-request limit; add a queue framework; or weaken
idempotency/lease-fencing/authorization.

## Evidence delivered

### Backend — end-to-end worker integration

`apps/api/app/tests/test_scout_schedule_worker_integration.py` drives the **real
durable worker** (`JobRunner.poll_once`) end-to-end, closing the gap left by the
service-level suite (`test_scout_schedules.py`, which calls `run_schedule_tick`
in-process). A pinned worker clock advances past each tick's future `scheduled_for`
without real waiting. Coverage:

- **Tick → fan-out → chain → run, through the worker, per market** (parametrized
  over all four markets): a due tick is claimed and executed by production code,
  fans out exactly one `scout_request.execute`, and self-chains one successor tick.
- **Four markets advance independently** — each schedule chains on its own cadence.
- **Only the due market fires** (Dallas daily vs. others weekly, clock between
  boundaries) — no cross-market bleed.
- **Stale-tick recovery**: a claimed-then-lease-expired tick is recovered
  (`recover_expired_leases`) and fans out **exactly once** — at-least-once without
  duplication.
- **Duplicate-tick delivery collapses to one run** via the tenant idempotency key.
- **Scheduled-run failure is market-isolated**: injecting a pipeline failure for
  Lagos only leaves Lagos `FAILED` while the other three markets `SUCCEED`.
- **Manual and scheduled runs coexist**: run history honestly reports both
  `manual` and `scheduled` triggers for the same request.

### Frontend — per-market UI isolation

`apps/web/src/pages/scouts/__tests__/schedule-panel.isolation.test.tsx` renders
four `SchedulePanel`s (one per market) under one `QueryClient` and proves:

- Each market resolves its **own** derived state (`active` / `paused` /
  `activation_required` / no-schedule-create) from its own query key — no
  cross-contamination of badges between panels.
- A lifecycle action (pause) on one market transitions only that panel; the
  sibling market stays untouched and never receives the mutation call.

This complements the existing `schedule-panel.test.tsx` (create, activation hint,
pause, delete-with-confirm, 503 graceful, viewer read-only).

### Documentation

- **Operator runbook:** [`../operations/scout-scheduling-runbook.md`](../operations/scout-scheduling-runbook.md)
  — enablement (single flag flip; existing schedules require explicit activation),
  rollback (safe kill-switch; chains self-drain within one interval), per-tenant
  stop, controls table, monitoring via `scout_schedule_*` log events + audit trail,
  and a symptom/cause/action health table.
- **This closeout document.**

## Retained observations — disposition

Four non-blocking review observations were evaluated. All are **accepted as
intentional design** with no code change; each is safe and consistent with the
product surface.

| # | Observation | Disposition |
| --- | --- | --- |
| 1 | `GET …/schedule` returns `404` (not `200`/null) when no schedule exists | **Accepted.** `404` is the deliberate "no schedule yet" contract; the UI (`SchedulePanel`) already treats it as a normal signal, not an error, and does not retry it. |
| 2 | Pause & delete are feature-gated (`503`) while scheduling is disabled | **Accepted.** Consistent with all mutations. With the flag off there is no live tick chain to stop, and the global kill-switch already halts fan-out, so gating causes no reachable stuck state. |
| 3 | Pending-tick lookup (`_has_pending_tick`) is request-scoped | **Accepted.** Safe under the enforced one-schedule-per-request uniqueness: a request's live tick is unambiguously that schedule's chain. |
| 4 | Create accepts an interval only and is activation-oriented | **Accepted.** Matches the fixed daily/weekly surface; richer recurrence (cron, hourly, timezones) is explicitly out of SB-D scope. |

Rationale: none rises to a concrete reliability, safety, or severe-UX defect, and
each carries meaning the current contract and UI already depend on. Changing any
would break compatibility for no safety gain.

## Regression evidence

- Backend schedule + durable-job suites (`.venv/bin/python -m pytest`):
  `test_scout_schedule_worker_integration.py`, `test_scout_schedules.py`,
  `test_scout_schedule_api.py`, `test_scout_run_history_api.py`,
  `test_durable_jobs.py` — **122 passed**.
- Frontend scouts suites (`vitest run src/pages/scouts`) — **9 passed** (2 files:
  `schedule-panel.test.tsx`, `schedule-panel.isolation.test.tsx`).
- Frontend typecheck (`tsc --noEmit`) and lint (`eslint`) — **clean**.
- Alembic: single head `b2c3d4e5f6a7`; no new migration.
- No contamination: SB-D adds only two test files and two docs. No changes under
  `apps/api/app/scouting_requests/schedules.py`, `routes.py`,
  `apps/api/app/jobs/**`, or any feature-flag default.

## Closeout status

Deliverable is a **DRAFT** SB-D PR. Do not mark ready, request review, or merge.
Scheduling remains dark (`scout_scheduling_enabled = False`).
