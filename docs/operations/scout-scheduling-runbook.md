# Scout Scheduling Runbook (Phase 3B — SB-D)

Operating the **recurring scouting schedule** subsystem: enabling it, rolling it
back, and troubleshooting. Scheduling is **dark-deployed** — every code path ships
disabled: the `scout_scheduling_enabled` flag defaults to `False` and no workspace
override exists, so no recurring scouting run can occur until an operator explicitly
enables it.

> **Since P6-CAP-1 availability is decided per workspace by the capability resolver**
> (safety ceiling → workspace override → global flag → secure default), not by the raw
> flag. Every scheduling enforcement point — the schedule mutation endpoints, the
> customer reflection `GET …/workspaces/{id}/scheduling-capability`, tick seeding on
> create/resume/failure recovery, and the tick itself — calls the same
> `resolve_scheduling_capability`, and the operator console's effective view resolves
> the same pair. An honored workspace **enable** override therefore activates scheduling
> for that workspace even while the global flag is `False`, and a **disable** override
> stops it there even while the flag is `True`.

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
4. If a tick **fails for the last time** (`failed`, or `dead_lettered` — retries
   exhausted, or its lease expired with no attempts left), its terminal-failure hook
   re-seeds the next occurrence instead (P6-PLAT-6): the failed occurrence is skipped
   (never back-filled), `last_tick_at` is not advanced, and the loss is logged.

Source: `apps/api/app/scouting_requests/schedules.py` (service),
`apps/api/app/jobs/handlers.py` (`execute_schedule_tick`).

### Invariants the operator can rely on

- **Dark by default.** While scheduling resolves disabled for a workspace, a tick
  that somehow exists there is a self-terminating no-op (`run_schedule_tick` returns
  `feature_disabled`) — no run, no successor. Turning the global flag off is a
  kill-switch **for every workspace without an enable override**; see Rolling back
  for workspaces that have one.
- **A failed tick does not end the schedule.** The chain survives a `failed` /
  `dead_lettered` tick on either dead-letter path and stops only where a healthy tick
  would also stop (schedule deleted or paused, or scheduling unavailable for the
  workspace). Re-seeding uses the ordinary per-occurrence idempotency key and is
  skipped when a live tick already exists, so it can never fork the chain; the failed
  job itself stays terminal (no status is ever revived).
- **One schedule per request; at most four *enabled* schedules per workspace.**
  The cap is enforced under a `SELECT … FOR UPDATE` workspace-row lock, so
  concurrent creates/resumes cannot exceed it.
- **At-least-once, idempotent.** Ticks and fanned-out runs carry idempotency keys
  (`schedule-tick:{id}:{occurrence}` and `schedule:{id}:{occurrence}`), so a
  retried or duplicated delivery collapses onto the same job row — never a storm.
- **Overlap coalescing.** A tick never stacks a second run on a request that
  already has one in flight; the schedule still continues.
- **Derived state, never persisted.** `paused` / `active` / `activation_required` /
  `stalled` and `last_tick_failed_at` are computed from observed job evidence, so the
  reported state can never drift from reality. `stalled` means enabled, no live tick
  chain, and the last finished tick *failed* — a chain the failure hook could not
  re-seed; `last_tick_failed_at` is set while the most recently finished tick failed and
  clears by itself once a later tick succeeds.

## Enabling scheduling (rollout)

Enabling is a single flag flip. **Prerequisite:** at least one durable worker is
healthy (see [worker_operations.md](./worker_operations.md)) — ticks accumulate as
`SCHEDULED` jobs and only fire when a worker claims them.

1. **Announce** a change window; scheduling changes tenant-visible behavior
   (recurring runs begin appearing in run history).
2. **Set the flag** in the API + worker environment and restart both so the
   `get_settings()` cache is rebuilt (for a single workspace instead, record an enable
   override from the operator console — that activates scheduling for that workspace
   only, with no restart):

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
2. **Clear every scheduling enable override** (operator console → Capability
   activation → *Clear override*, or record a *disable* override). A workspace with an
   honored enable override keeps scheduling after the global flag is off — that is
   what the override means. A global stop that outranks overrides would be a
   safety-ceiling input, which is a deferred decision (4A-C plan §8.28 Q1) and is not
   wired.
3. **In-flight ticks self-terminate.** The next time any existing tick fires where
   scheduling resolves disabled it returns `feature_disabled` and does **not** chain
   a successor, so every chain drains itself within one interval. No manual job purge
   is required.
4. **No data is destroyed.** Schedule rows are retained; they simply stop fanning
   out. Re-enabling later returns to step 3 of Enabling (rows report
   `activation_required` until re-activated).

For a **per-tenant** stop without a global flip, pause or delete the individual
schedules (editor endpoints below). Pausing flips `enabled → False` and clears
`next_run_at`, so any in-flight tick becomes a no-op.

## Operator / customer controls

All mutations require an **editor** role (`owner` / `admin` / `marketer`) and are
**capability-gated** — while scheduling resolves disabled for the workspace they
answer `503 capability_unavailable`. Reads, and the workspace-effective
`GET /workspaces/{id}/scheduling-capability` reflection (`{"enabled": bool}`, the
same decision the gate makes), are open to any workspace member and are **not**
capability-gated.

| Action | Endpoint | Notes |
| --- | --- | --- |
| Read | `GET  …/scout-requests/{id}/schedule` | `404` when none exists (the "no schedule yet" signal). Not feature-gated. |
| Create | `POST …/schedule` | One per request; four-enabled-per-workspace cap. |
| Pause | `POST …/schedule/pause` | Idempotent; clears `next_run_at`. |
| Resume / Activate | `POST …/schedule/resume` | Recomputes next run from *now*; the activation path for `activation_required` and the restart for `stalled`. |
| Delete | `DELETE …/schedule` | Hard delete; in-flight tick self-terminates. |

Source: `apps/api/app/scouting_requests/routes.py`.

## Monitoring & audit

- **Structured log events** (via `log_event`, see [observability.md](./observability.md)):
  `scout_schedule_created`, `scout_schedule_paused`, `scout_schedule_resumed`,
  `scout_schedule_deleted`, and per-fire `scout_schedule_tick`
  (`outcome`, `run_enqueued`, `coalesced`, `request_paused`). A healthy live schedule
  emits one `scout_schedule_tick` per interval with `run_enqueued=true` (or
  `coalesced=true` when a run was already in flight, or `outcome=request_paused`
  while the customer has paused the scout request itself: no run is fanned out, the
  schedule stays enabled and the chain reseeds, so resuming the request restores
  scheduled scouting at the next boundary). A paused request's already-queued run is
  skipped by the worker (`result_summary.outcome=skipped_request_paused`) rather
  than executed: no source fetch, no model call, no new LLM budget reservation and
  no usage row. (The skip's attempt-end release may still settle an earlier attempt
  of the same job whose reservation was left in flight, at its worst case; that is
  the ledger's normal recovery, not new spend.)

  **A pause during a run: cooperative stop (PROPOSED pending D-25; not
  operator-accepted).** Candidate of `D-25-COOPERATIVE-PAUSE-CONTRACT-01` §6, measured on
  PostgreSQL 16 in lane evidence `P6-W13-PAUSECOOP-01`:
  - **Pause recorded is not work stopped.** The pause response (`200`, the request
    `paused`) means the pause is recorded: the route commits `paused` and a new run
    fence before it answers. It does not mean the active work has stopped. Work has
    stopped when the run's job reaches a terminal state: `succeeded` with
    `result_summary.outcome` `stopped_request_paused` (or `skipped_request_paused` for
    a run that had not been claimed yet), or any other terminal state.
  - **What the pause waits for.** Only short database transactions that make no
    external call: an admission, a claim, or a run's terminal write. The run's claim is
    committed in a transaction of its own, so the pause no longer waits for the rest of
    a run (the previous option-(1) behaviour). No latency bound is promised; the lane's
    measurements are observations.
  - **Nothing is admitted after the pause commits.** Every live fetch attempt (first
    try and each retry), every model attempt (each retry included), a post-fetch
    boundary and the run's terminal write pass an admission that reads the request row
    `FOR SHARE` and requires `running` with the run's fence. It is not "nothing is sent"
    and not "stops immediately": an operation admitted before the pause commits may
    still be transmitted and completed after it, and its charge stands (reservation,
    settlement and usage row as the #269 ledger defines them; nothing is undone).
  - **The bound.** Per run, after the cut-off at most one admitted operation can still
    complete: one model attempt (charge at most its reserved worst case), or one fetch
    attempt (at most 4 HTTP requests in one redirect chain, at most 20 s total
    deadline). This is per run, not per Scout: several attempts of one Scout can be
    active at once (a manual run racing a schedule tick; Pause -> Resume -> Run now
    while the old run is still in flight; a lease that expired while the old worker is
    alive and a re-claim by another worker), and each can have one admitted operation
    in flight when the pause commits. Retries and lease recovery do not add operations
    to one attempt; a re-claim adds an attempt (at most the job's attempts in all).
  - **No escape through Pause -> Resume.** Every pause advances the request's run fence;
    every execute job carries the fence it was queued under. A job queued, claimed,
    retried or recovered before a pause skips or stops even after a resume. Resume is
    unchanged (`draft`).
  - **The stopped run's own uncommitted findings are discarded (PROPOSED, §6.2 Q-1):**
    a run's findings become visible only if its terminal write commits before the pause.
    Everything committed before (earlier runs' findings, configuration, run history,
    job records) stays.
  - **Status after a failed attempt (PROPOSED, §6.2 Q-2):** the claim is committed on
    its own, so a failed attempt puts back the pre-claim status for its retry (never
    over a newer pause, resume, claim or terminal result). Visible change: a worker
    crash mid-run now leaves `running` committed until lease recovery retries or
    dead-letters the job; before, the rollback left the pre-claim status.
  - **`running` is committed for the whole run (PROPOSED).** Because the claim commits
    on its own, a `GET` of the request during a run shows `running`, and an edit
    (`PUT`) is refused with `422` ("Cannot edit a request while it is running") for the
    whole run. Before, on PostgreSQL the pre-claim status (for example `queued`) stayed
    visible until the run's transaction committed.
  - **After a crash or a lost lease, a customer cancel can leave `running` (PROPOSED;
    existing F-03 class, not resolved here).** If the worker crashes or loses its lease
    and the customer then cancels the re-queued job, the request stays `running` (before:
    `queued`, the F-03 shape) until Pause -> Resume; Run now answers `422` meanwhile.
    Cancel semantics are unchanged and the stuck state is the existing F-03 defect class.
    If the old attempt is still alive, the calls it had already been admitted to make are
    charged before its rollback; that spend predates this change (a lease check in the
    admission is a proposed option, not implemented).
  - **A scheduled occurrence can be lost after Pause -> Resume (PROPOSED, §6.2 Q-6):**
    if the next tick fires while the fenced job of the previous occurrence is still
    pending, the tick coalesces against it and that occurrence is skipped, not caught
    up. The chain itself survives; the following occurrence runs normally.
  - **Refusal and failure after a pause** leave the request `paused`; a refusal's stats
    are no longer stored on a request paused since the run claimed it.
  - **SQLite (development and tests only):** the logic is the same, but from a run's
    first write until its commit the run holds the database write lock, so a pause
    then waits up to `busy_timeout` (5 s) and may fail. The prompt pause is a
    PostgreSQL property.
- **Audit trail** (`record_audit`): every lifecycle mutation writes a
  `scout_schedule.{created,paused,resumed,deleted}` audit record with the acting
  user — the deletion record is written *before* the row is removed so the delete
  is itself auditable.
- **Durable-job health** for tick/run jobs is the same as any durable job — watch
  `SCHEDULED`/`RETRY_WAIT`/`DEAD_LETTERED` counts on the worker dashboards.
- **Tick failures** (P6-PLAT-6): every final tick failure emits
  `scout_schedule_tick_failed` at ERROR with `outcome` (`reseeded`, `chain_live`,
  `disabled`, `feature_disabled`, `schedule_missing`, `unrecoverable_payload`),
  `chained` (whether recurrence survived), `job_status` and `error_class`. The job-level
  counters `jobs_dead_lettered_total` / `jobs_failed_total` carry
  `job_type="scout_schedule.tick"` — lease-expiry dead-letters included — and back
  alert §5a in [alerts.md](./alerts.md). The operator overview's
  `schedules.state_counts.stalled` and `GET /internal/system/schedules`
  (`state`, `last_tick_failed_at`, `last_tick_error_code`) show which schedules are
  affected.

### Health checks

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Schedule stuck in `activation_required` after enabling | Never activated (by design) | Call resume/activate on the schedule. |
| Schedule reports `last_tick_failed_at` while `active` | One occurrence's tick failed; the chain was re-seeded | Inspect the failed `scout_schedule.tick` job (`error_class`); the next occurrence runs normally and the signal clears once it succeeds. |
| Schedule in `stalled` | A tick failed and its re-seed could not run (scheduling unavailable for the workspace at the time, or the hook itself failed — see `worker.terminal_failure_hook_failed`) | Fix the cause, then resume/activate the schedule; it seeds one fresh tick. |
| Ticks pile up in `SCHEDULED`, never fire | No healthy worker, or flag off on the worker | Check worker fleet; confirm the worker env carries the flag. |
| A run failed | Pipeline error for that request | The failure is **market-isolated** — other schedules are unaffected. Inspect the failed `scout_request.execute` job; the chain continues. |
| Duplicate runs feared | — | Ticks and their fanned-out runs cannot duplicate: idempotency keys collapse them to one row. Two attempts of one Scout can still run at once (a manual run racing a tick, or a re-claim after a lease expired while the old worker was alive); a pause stops each at its next admission (PROPOSED pending D-25, see above). |

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
