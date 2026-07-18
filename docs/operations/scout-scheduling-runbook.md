# Scout Scheduling Runbook (Phase 3B — SB-D)

Operating the **recurring scouting schedule** subsystem: enabling it, rolling it
back, and troubleshooting. Scheduling is **dark-deployed** — every code path ships
disabled behind the `scout_scheduling_enabled` flag (default `False`), so no
recurring scouting run can occur until an operator explicitly turns it on.

Related runbooks: the durable worker in [worker_operations.md](./worker_operations.md);
telemetry in [observability.md](./observability.md); dashboards and alerts in
[dashboards.md](./dashboards.md) and [alerts.md](./alerts.md); migrations in
[migrations.md](./migrations.md).

> There is **no separate scheduler process, cron daemon, or timer service**.
> Recurrence is carried entirely by self-chaining `scout_schedule.tick` durable
> jobs on the existing worker fleet. Enabling scheduling adds *no new process* to
> operate — it only lets ticks fan out.

## What the subsystem is

A **schedule** attaches a fixed recurrence (`daily` = 24h, `weekly` = 7 days, UTC,
no DST) to one scout request. When live, each occurrence:

1. A due `scout_schedule.tick` job is claimed by a worker.
2. The tick fans out **exactly one** `scout_request.execute` run (labelled
   `scheduled`), coalescing if a run for that request is already in flight.
3. The tick **self-chains**: it enqueues the next tick one interval out, skipping
   any missed boundaries (**no backfill**), and advances `last_tick_at` /
   `next_run_at`.

Source: `apps/api/app/scouting_requests/schedules.py` (service),
`apps/api/app/jobs/handlers.py` (`execute_schedule_tick`).

### Invariants the operator can rely on

- **Dark by default.** With the flag off, a tick that somehow exists is a
  self-terminating no-op (`run_schedule_tick` returns `feature_disabled`) — no run,
  no successor. Turning the flag off is therefore a safe global kill-switch.
- **One schedule per request; at most four *enabled* schedules per workspace.**
  The cap is enforced under a `SELECT … FOR UPDATE` workspace-row lock, so
  concurrent creates/resumes cannot exceed it.
- **At-least-once, idempotent.** Ticks and fanned-out runs carry idempotency keys
  (`schedule-tick:{id}:{occurrence}` and `schedule:{id}:{occurrence}`), so a
  retried or duplicated delivery collapses onto the same job row — never a storm.
- **Overlap coalescing.** A tick never stacks a second run on a request that
  already has one in flight; the schedule still continues.
- **Derived state, never persisted.** `paused` / `active` / `activation_required`
  are computed from observed job evidence, so the reported state can never drift
  from reality.

## Enabling scheduling (rollout)

Enabling is a single flag flip. **Prerequisite:** at least one durable worker is
healthy (see [worker_operations.md](./worker_operations.md)) — ticks accumulate as
`SCHEDULED` jobs and only fire when a worker claims them.

1. **Announce** a change window; scheduling changes tenant-visible behavior
   (recurring runs begin appearing in run history).
2. **Set the flag** in the API + worker environment and restart both so the
   `get_settings()` cache is rebuilt:

   ```bash
   SCOUT_SCHEDULING_ENABLED=true
   ```

   Both the API and the worker read the same flag; both must carry it.
3. **Existing schedules do not auto-start.** Any schedule created while the flag
   was dark is `enabled` but has no live tick chain — it reports
   `activation_required`. It stays inert until the customer (or operator, via the
   resume endpoint) explicitly **activates** it. This is intentional restart-safe
   behavior: flipping the flag never triggers a thundering herd of back-runs.
4. **Newly created schedules** (created after the flag is on) seed their first tick
   immediately, one interval out.
5. **Verify** (see Monitoring below): ticks move `SCHEDULED → SUCCEEDED`, each
   fans out one `scheduled` run, and each self-chains exactly one successor tick.

## Rolling back (kill-switch)

Rollback is the reverse flip and is always safe:

1. **Set `SCOUT_SCHEDULING_ENABLED=false`** on the API + worker and restart.
2. **In-flight ticks self-terminate.** The next time any existing tick fires it
   returns `feature_disabled` and does **not** chain a successor, so every chain
   drains itself within one interval. No manual job purge is required.
3. **No data is destroyed.** Schedule rows are retained; they simply stop fanning
   out. Re-enabling later returns to step 3 of Enabling (rows report
   `activation_required` until re-activated).

For a **per-tenant** stop without a global flip, pause or delete the individual
schedules (editor endpoints below). Pausing flips `enabled → False` and clears
`next_run_at`, so any in-flight tick becomes a no-op.

## Operator / customer controls

All mutations require an **editor** role (`owner` / `admin` / `marketer`) and are
**feature-gated** — while the flag is off they answer `503 capability_unavailable`.
Reads are open to any workspace member and are **not** feature-gated.

| Action | Endpoint | Notes |
| --- | --- | --- |
| Read | `GET  …/scout-requests/{id}/schedule` | `404` when none exists (the "no schedule yet" signal). Not feature-gated. |
| Create | `POST …/schedule` | One per request; four-enabled-per-workspace cap. |
| Pause | `POST …/schedule/pause` | Idempotent; clears `next_run_at`. |
| Resume / Activate | `POST …/schedule/resume` | Recomputes next run from *now*; the activation path for `activation_required`. |
| Delete | `DELETE …/schedule` | Hard delete; in-flight tick self-terminates. |

Source: `apps/api/app/scouting_requests/routes.py`.

## Monitoring & audit

- **Structured log events** (via `log_event`, see [observability.md](./observability.md)):
  `scout_schedule_created`, `scout_schedule_paused`, `scout_schedule_resumed`,
  `scout_schedule_deleted`, and per-fire `scout_schedule_tick`
  (`outcome`, `run_enqueued`, `coalesced`). A healthy live schedule emits one
  `scout_schedule_tick` per interval with `run_enqueued=true` (or
  `coalesced=true` when a run was already in flight).
- **Audit trail** (`record_audit`): every lifecycle mutation writes a
  `scout_schedule.{created,paused,resumed,deleted}` audit record with the acting
  user — the deletion record is written *before* the row is removed so the delete
  is itself auditable.
- **Durable-job health** for tick/run jobs is the same as any durable job — watch
  `SCHEDULED`/`RETRY_WAIT`/`DEAD_LETTERED` counts on the worker dashboards.

### Health checks

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Schedule stuck in `activation_required` after enabling | Never activated (by design) | Call resume/activate on the schedule. |
| Ticks pile up in `SCHEDULED`, never fire | No healthy worker, or flag off on the worker | Check worker fleet; confirm the worker env carries the flag. |
| A run failed | Pipeline error for that request | The failure is **market-isolated** — other schedules are unaffected. Inspect the failed `scout_request.execute` job; the chain continues. |
| Duplicate runs feared | — | Cannot occur: idempotency keys collapse duplicate ticks/runs to one row. |

## Retained design observations

Four non-blocking observations from review were evaluated during SB-D and
**accepted as intentional** (no code change; documented for operators). Full
rationale in [../verification/sb-d-schedule-closeout.md](../verification/sb-d-schedule-closeout.md):

1. **`GET` returns `404` when a request has no schedule** (rather than `200`/null).
   `404` is the deliberate "no schedule yet" contract the UI already treats as a
   normal, non-error signal.
2. **Pause and delete are feature-gated (`503`) while scheduling is disabled.**
   Consistent with all mutations; with the flag off there is no live chain to stop,
   and the global kill-switch already halts fan-out.
3. **Pending-tick lookup is request-scoped.** Safe under the one-schedule-per-request
   uniqueness invariant — a request's tick chain is unambiguously that schedule's.
4. **Create accepts an interval only and is activation-oriented.** Matches the
   fixed daily/weekly product surface; richer recurrence is explicitly out of scope.
