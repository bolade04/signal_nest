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
    If the old attempt is still alive, it keeps passing its admissions (the request is
    still `running` with its fence) and can run its remaining operations to completion;
    every call it makes is charged, and its writes are rolled back when it loses its lease
    at completion. A Pause stops it at its next admission. That behaviour predates this
    change (a lease check in the admission, option O1, is proposed, not implemented).
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

  **Execution controls O1/O2 (PROPOSED pending D-25; not operator-accepted).** Candidate of
  `D-25-EXECUTION-CONTROL-CONTRACT-01` §3/§8:
  - **O1, ownership at every admission.** Each admission, terminal write and status
    restore re-checks the attempt's identity captured at its claim (job, attempt, worker,
    lease token, fence) against fresh rows, with the lease still running by database time.
    A stale attempt (lease lost or lapsed, Cancel, owner superseded, fence moved) admits
    nothing further; the operation it already admitted may finish and is charged.
  - **O2, one owner per Scout.** A claim against a valid owner is refused
    (`skipped_owner_active`): a manual run and a scheduled run of one Scout no longer run
    together. A new attempt takes over only from an owner whose authority has lapsed. The
    pause clears the owner.
  - **K = 1.** Every connector fetch attempt and every model attempt commits a ledger row
    (`scout_admissions`) before it is sent; at most ONE unresolved row per Scout, enforced by
    the database. A row is resolved only by a known recorded end or by reconciliation -
    never by lease expiry, recovery, Cancel, Pause -> Resume or elapsed time. So at most one
    external operation of a Scout can be in progress or of unknown outcome, across attempts,
    workers, recoveries and zombies, **under four assumptions** (contract §9.4): one
    admission per client call, with no transport retries (a fetch attempt's redirect chain
    of at most 4 requests is one operation); a known end only after a reply or a proven
    non-transmission (classified as below); no external call outside the fetch and model
    seams; and reconciliation is an operator's declaration about the external party, made
    after the client-side deadline. PostgreSQL only (on SQLite the ledger is not written).
  - **Waiting.** An admission refused behind an outstanding row waits, holding no lock, up
    to that row's own deadline (fetch: 20 s + lease; model: 3 x 30 s + lease). It never
    waits for a row with an unknown end. **Wait expiry:** the job fails (`NON_RETRYABLE`,
    `admission_unresolved`), the request ends `failed`, the admission stays unresolved.
  - **Persisted deadlines (D-25 recovery, PROPOSED).** Each admission stores its deadline
    (`deadline_at`) and the basis it was computed from (`deadline_rule = client_plus_lease`,
    the admitting worker's client deadline for the operation and its lease, in seconds),
    written by the admitting INSERT from one database-clock reading shared with
    `admitted_at`. The waiting rule, reconciliation's gate, the listings and the Scout's
    derived state all read that stored value and compare it strictly with the database clock
    (eligible once `clock_timestamp() > deadline_at`): a worker, CLI or later settings change
    can neither shorten nor lengthen an existing admission's gate. The deadline is a
    procedural gate, not evidence that the external party stopped. **Legacy rows** (created
    before the deadline was stored) were not back-filled: `deadline_rule = unrecorded`, no
    deadline, listed as "not recorded". One with a recorded unknown end is reconcilable as
    before; one with no recorded end is never made eligible by time (see Reconciliation); a
    run blocked by one waits up to its own computed bound (the end of a wait resolves
    nothing).
  - **Unknown outcomes block the Scout (the availability cost).** An unknown end leaves an
    unresolved admission: its retry is not admitted, the run fails (`admission_unresolved`),
    and the Scout admits nothing - and its schedule fans out no run (`admission_unresolved`
    while nothing is in flight; `coalesced`, as before, while a run is) - until an operator
    reconciles it. Other Scouts and tenants are unaffected. **Unknown ends (contract
    §9.2):** for a model call, every timeout, every transport error after dispatch, and
    every reply with status >= 500 - **including a provider 5xx / 529 "overloaded"** - or any
    other non-2xx that is not a 4xx (a gateway 5xx does not prove the origin stopped; the
    call is charged its worst case); for a feed fetch, a timeout or a connection lost after
    dispatch. **Known ends:** a model 2xx, 4xx or 429 reply, or `httpx.ConnectError` (before
    dispatch); for a feed fetch, a received reply of any status, 5xx included (an unbilled
    read-only GET under a 20 s deadline). Operator alternatives, none adopted: R3 (a fetch's
    unknown outcome resolves), R5 (a provider-originated 5xx counts as known: it blocks less),
    R6 (a fetch 5xx counts as unknown: the bound is strict).
  - **Recovery state (D-25 recovery, PROPOSED).** Every response that returns the Scout
    itself (`ScoutRequestOut`: create, read, update, pause and resume) carries a derived,
    read-only `execution` field `{state, reason, next_action, since}`; since W16 (PROPOSED)
    the list carries it too, computed for the whole page in a bounded number of statements
    (four on PostgreSQL, whatever the page size) with the same classification as the
    single-Scout read; Run now's and the schedule routes' responses do not carry it. It is computed on each read from committed rows and the database clock;
    nothing is stored. `needs_attention` when an unresolved admission is
    eligible for reconciliation (its attempt holds no valid authority, and its end is unknown,
    or it has no end and its deadline has passed, or it is a legacy row with no end), whatever
    the request status; else `paused`; else `in_progress` while a live job not fenced out by a
    pause exists or an admission is still finishing (a healthy in-flight one is never
    `needs_attention`); else `stopped` (`queued`/`running` with no live job is
    `run_interrupted`, contact support: Run now refuses those statuses; or a failed or
    cancelled last run); else `idle`. Reasons and next actions are closed codes; no id,
    worker, token, attempt, payload, URL or setting is exposed; visible to every role that
    can read the Scout, scoped to its tenancy. The Scout page shows a notice for
    `needs_attention`, `paused` and `stopped`, and keeps polling while `in_progress`.
    **Run now on a `needs_attention` Scout is refused** (HTTP 422, `scout_needs_attention`)
    and creates no job; Resume returns the Scout to `draft` but the state stays
    `needs_attention`. Customers cannot clear an unresolved operation: no customer route
    resolves, reconciles or hides one. **Operator view:**
    `python -m app.jobs.reconcile blocked [--organization <id>] [--workspace <id>]` lists every
    Scout in `needs_attention` with its tenancy, the operation, `admitted_at`, the stored
    deadline (or "not recorded") and its basis, whether it has passed, the end kind (or "no
    recorded end"), the job's status and the exact `resolve` command to run.
  - **Customer workflow repairs (W16, PROPOSED; not operator-accepted).**
    - *One card per finding.* A finding is identified within its Scout by its evidence
      (each member item's source type, source identity, content digest and the market it was
      collected for, as its stored row records it), never by its
      title, label, scores or model text: `opportunities.finding_key`, unique per Scout where
      set. A run whose finding already has a card adds a `seen_again` row to
      `opportunity_observations` and creates no card; the card keeps its status, feedback,
      scores, text and intelligence link. All identity writes happen after the run's last
      model call, inside its write savepoint, in ascending key order (opposite orders were
      measured to deadlock), each new card in a savepoint of its own; a unique conflict
      re-uses the winner. A stopped run records no card and no sighting. Cards created before
      the identity have no key; a later run adopts the oldest whose own stored members give
      the same key, and younger copies stay as they are. A re-run makes and is charged for the
      same model calls. Run stats add `opportunities_new` and `opportunities_seen_again`
      (`opportunities` is still the findings the run produced).
    - *Edit.* `PUT .../scout-requests/{id}` locks the Scout row, then refuses with 422
      `scout_in_progress` while the request is queued or running or a run it has not been
      paused out of is live, and writes only the sent fields under a compare-and-set on the
      status and run fence it read; Run now waits for it. Allowed while paused, stopped, idle
      or needing attention; never writes status, fence, owner or execution state. Edit-only
      validation (create is unchanged, FU-W16-1); a `scout_request.updated` audit record names
      the changed fields, never their values.
    - *Run now in the UI* mirrors the API's refusals exactly (queued or running, paused, needs
      attention), with a visible reason tied to the button; it stays available while an
      earlier operation is finishing (the run waits). The list shows "Needs attention".
    - *Run outcomes (a proposed Q1 presentation, not a Q1 ruling).* `JobOut` and `RunItem`
      carry a read-only `outcome` / `outcome_reason` derived from the job's status, its
      result's outcome, its error code and an allow-list of error-summary codes (never the
      summary itself): `completed` (also a run recorded before outcomes existed, and the
      legacy `paused_during_run` with a note); `stopped_paused` (`stopped_request_paused`);
      `skipped_paused` (`skipped_request_paused`); `skipped` (`skipped_owner_active`: another
      run of the Scout was in progress); `stopped` (`stopped_owner_superseded`,
      `stopped_lease_lost`, `status_changed_during_run`, `request_gone`, or an unknown
      outcome); `cancelled`; `failed` with `earlier_operation_timed_out`
      (`admission_unresolved`), `too_many_signals`, `retries_exhausted` (dead-lettered
      transient), `worker_stopped_responding` (dead-lettered timeout) or `internal`. A
      succeeded job whose run stopped or was skipped is never shown as Completed; job
      lifecycle states are unchanged and the raw codes stay visible as secondary text.
    - *Markets.* For a Scout tied to a location, the sources see its market text read in the
      location's country ("Lagos, Lagos" of a Lagos, NG location → "Lagos, Lagos, NG", served
      by the "Lagos, NG" sample market). Text that names a country is unchanged (an explicit
      market wins); a Scout without a location gets no country; nothing is broadened. The
      stored text is not rewritten. When an approved live source serves the stored text, the
      sources get exactly that text, as before; only otherwise is the qualified text used (for
      the sample library and coverage). Because the market is part of a finding's identity, an
      edit of a Scout's market starts new cards for the new market; the old market's cards stay
      as history.
    - *Empty results.* The Scout page says why the latest run made no new card, from its
      recorded coverage and stats, stating only what that run recorded, in the past tense: no
      source covered the market in the last run; the live sources failed in it; its sources
      returned no items for the source types and keywords that run used; it collected items but
      filtered all of them (noise or duplicates, derived); all its findings were already known
      (seen again); otherwise only that it found nothing new. No line predicts a future run or
      describes the Scout's current settings (an edit may have changed them since).
  - **Reconciliation (procedure).** `python -m app.jobs.reconcile list --scout <id>` shows
    exactly three categories - the current owner's admissions, earlier owners' outstanding
    admissions, admissions with an unknown end - each with its job's status and
    `lease_expires_at`, its deadline and whether that deadline has passed;
    `python -m app.jobs.reconcile resolve --admission <id> --operator <name> --reason <code>
    [--note <= 200 chars]` resolves one after the operator has ascertained the operation is
    over. Refused while the admitting attempt still holds valid authority (**whatever the end
    kind**, an unknown end included: D-25 recovery A2, PROPOSED), and refused for any
    admission with no recorded end until its own **stored** deadline has passed by the
    database clock; a legacy admission with no recorded end and no stored deadline requires
    `--acknowledge-unrecorded-deadline` (recorded in the audit row; refused where it does not
    apply). Idempotent; audited; accounting unchanged.
    **Caveat:** the deadline guard covers SignalNest's client side only. After the deadline
    the external party (the provider or the feed's server) may still be working;
    reconciling is the operator's declaration that it is not, and the bound then rests on
    that declaration. A blocked Scout is visible (the `blocked` listing and the customer
    indication above). **Operator signal (FU-267-A1, PROPOSED):** every worker's registry
    heartbeat sets the gauges `scouts_blocked` and `scouts_blocked_oldest_age_seconds`
    (exactly the Scouts `blocked` lists; no labels; no traffic needed), and
    `GET /api/v1/internal/system/overview` reports `scouts.blocked_count` and
    `scouts.oldest_blocked_age_seconds`. On SQLite (no ledger) the gauges are absent and the
    overview says `ledger_kept: false`. The alert is defined in
    [alerts.md](./alerts.md) §21 and the response in
    [incident_response.md](./incident_response.md) §13; it fires only where metrics are
    exported (`METRICS_ENABLED=true`, `METRICS_EXPORTER=emf` on the worker) and an alarm
    is configured (missing data notifies as `INSUFFICIENT_DATA`, never reads as OK) —
    neither is wired in any environment yet, so until then operators must poll the
    overview. The heartbeat's read is bounded on PostgreSQL (`SET LOCAL lock_timeout` 2 s,
    `statement_timeout` 5 s by default): a read that hits the bound is logged and sets no
    gauge, and delays the next registry heartbeat by at most about that bound.
  - **Cancel (a PROPOSED cancellation change).** Cancelling a running scout job now stops
    the run at its next admission (including while it waits): the job ends `cancelled`.
    **Cancel recovery (D-25 recovery, PROPOSED):** whenever Cancel makes an execute job
    terminal (before its claim, after the job claim but before the run's, during the run,
    while it waits, after a lease recovery), the request returns, in the same transaction as
    the job's terminal write, to the status it had before the run was requested, recorded
    once on the job (Run now records it when it creates the job; a scheduled job at its first
    claim), or `draft` for a job with none recorded (legacy, or a scheduled job never
    claimed): never `queued`/`running`, never a result the run did not produce. A
    compare-and-set preserves a newer pause, resume, claim, owner, fence or completed result,
    and another live job of the Scout not fenced out by a pause; it may clear an owner that is
    an attempt of the cancelled job. The Cancel route's job transition is itself a
    compare-and-set on the status it read, so a job that completed meanwhile keeps its result.
    Cancel never resolves an admission and writes no accounting row: a Scout cancelled while
    an operation is still unresolved is idle but `in_progress`/`needs_attention`, and its
    next admission is still refused (K = 1). Requests left `queued`/`running` by an earlier
    candidate's Cancel are not repaired; they show `stopped` / `run_interrupted`.
  - **Lock order of the terminal transactions (defect fixes, PROPOSED).** Every transaction
    that writes both a scout job row and its request row now locks the job row first: the
    worker's fenced failure / Cancel transaction (before its writers update the request), the
    run's success commit (before `finish` updates the request; the worker's `complete` then
    updates the job), the run's stop path, lease recovery's dead-letter, and the Cancel route.
    Before these fixes the fenced transaction and the success commit took request -> job and
    could deadlock (`40P01`) against a stale attempt's admission check (job -> request); when
    the success commit lost, a completed run was rolled back and retried. No end state
    changed. Other orders, which cannot form that cycle: the admission takes job -> request
    -> ledger -> period -> reservation, while a settlement takes period -> reservation ->
    its own admission row (other rows; no cycle); Run now updates the request and then
    inserts a new job row.
  - **Stale-stop end states.** Lease lapsed (not yet recovered): a retryable failure, so the
    run is retried, never lost; the request stays `running` with the owner cleared until the
    retry claims it. Token rotated or owner superseded: the attempt stops and writes nothing
    over the newer owner - every request write of an attempt that can commit outside its
    token-fenced job transaction carries its full identity, and the settlement of a run
    refused at the in-process bound commits only inside that fenced failure transaction
    (contract §9.1), so a stale attempt's refusal is discarded with its lost lease. Fence
    moved: `stopped_request_paused`. Request gone: `request_gone`. Missing claim identity:
    refused (`claim_identity_incomplete`).
  - **Missed scheduled occurrences (the earlier "at most one" is withdrawn).** An
    occurrence is missed for every tick while an admission stays unresolved, for each lost
    overlap race (`skipped_owner_active`), and after Pause -> Resume while a fenced job is
    pending; as before, also while a run is in flight (`coalesced`) or the request is paused
    (`request_paused`). No catch-up.
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
