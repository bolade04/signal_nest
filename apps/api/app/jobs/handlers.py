"""Durable job handlers.

Each handler performs one job type. Handlers are thin: they validate their
payload, honour cancellation, delegate to existing domain logic, and return a
**safe** result summary (counts and status only — never secrets, raw payloads or
customer content). They must not commit or roll back; the worker owns the
transaction boundary so the job's bookkeeping and the handler's writes are one
atomic unit.

Importing this module registers every handler on the
:mod:`app.jobs.registry` registry.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings
from app.core.enums import ScoutRequestStatus
from app.core.logging import get_logger, log_event
from app.infra.vector import SignalVolumeAboveBoundError
from app.jobs.admission import RunAdmission, RunStopped, admitted_run
from app.jobs.context import ExecutionContext
from app.jobs.pipeline import _run, metered_run, refused_run_stats
from app.jobs.registry import (
    HandlerContext,
    TerminalFailureContext,
    register_handler,
    register_terminal_failure_hook,
)
from app.jobs.status import JobErrorCode, JobExecutionError, JobType
from app.llm.budget import release_for_job
from app.llm.metering import LLMMeter
from app.scouting_requests.models import ScoutRequest

logger = get_logger("signalnest.jobs.handlers")


def _record_refused_run(
    scout_request_id: str,
    context: ExecutionContext,
    exc: SignalVolumeAboveBoundError,
    run_fence: int | None = None,
) -> Callable[[Session], None]:
    """An after-rollback writer that settles a run refused at the in-process bound.

    The attempt is rolled back (the refusal happens before any signal is written),
    so the outcome is recorded in the worker's after-rollback transaction: the
    request becomes ``failed`` with ``stats`` carrying the stable reason code and
    counts (P6-PLAT-1 B). Tenant guard as in :func:`settle_failed_scout_request`:
    the context is the job row's, so a mis-enqueued id never settles another
    tenant's request.

    D-25: the refusal never overwrites a pause. ``failed`` is written with a
    compare-and-set that excludes ``paused``: a pause that waited behind the run's
    row lock commits as soon as the attempt rolls back, i.e. before this writer runs,
    and a pause committing between the load below and the write is not overwritten
    either. A paused request keeps its status; the refusal's ``stats`` are stored as
    for any other run (the run's terminal guard keeps a paused run's stats too).

    With ``run_fence`` (the durable worker path, D-25 cooperative pause, PROPOSED) the
    stats and ``failed`` are written in ONE compare-and-set that also requires the run's
    fence: a request paused (or paused and resumed, and run again) since this run claimed
    it is left exactly as it is, stats included, so an old run can never settle a newer
    one. Without it (legacy callers) the behaviour above is unchanged.
    """

    def write(db: Session) -> None:
        request = db.get(ScoutRequest, scout_request_id)
        if request is None:
            return
        if (
            request.organization_id != context.organization_id
            or request.workspace_id != context.workspace_id
        ):
            return
        stats = refused_run_stats(
            scanned=exc.scanned, bound=exc.bound, fetch_limits=exc.fetch_limits
        )
        if run_fence is None:
            request.stats = stats
            db.flush()
            settled = db.execute(
                update(ScoutRequest)
                .where(
                    ScoutRequest.id == request.id,
                    ScoutRequest.status != ScoutRequestStatus.PAUSED.value,
                )
                .values(status=ScoutRequestStatus.FAILED.value)
                .execution_options(synchronize_session=False)
            )
        else:
            settled = db.execute(
                update(ScoutRequest)
                .where(
                    ScoutRequest.id == request.id,
                    ScoutRequest.status != ScoutRequestStatus.PAUSED.value,
                    ScoutRequest.run_fence == run_fence,
                )
                .values(status=ScoutRequestStatus.FAILED.value, stats=stats)
                .execution_options(synchronize_session=False)
            )
        db.refresh(request, attribute_names=["status", "stats"])
        log_event(
            logger,
            "scout_request.refused_at_bound",
            component="jobs",
            outcome="refused",
            reason=exc.reason,
            scanned=exc.scanned,
            bound=exc.bound,
            partial_fetch=stats.get("partial_fetch"),
            request_paused=settled.rowcount != 1,
        )

    return write


@register_handler(JobType.SCOUT_REQUEST_EXECUTE)
def execute_scout_request(ctx: HandlerContext) -> dict[str, Any]:
    """Run the scouting pipeline for one scout request.

    The tenant/location isolation guard inside :func:`app.jobs.pipeline._run`
    re-verifies that the loaded request matches the job's declared execution
    context, so a mis-enqueued id can never blend one market's data into
    another's.

    Model calls are paid for when they are made (P6-LLM-4). On PostgreSQL the meter
    reserves each call's worst case against the workspace's month before the call and
    settles it after, each in a short transaction of its own on a session built from
    this attempt's bind (``app.llm.metering``, the shared-ledger path); the usage row
    is written at settlement. On SQLite the run's usage rows are written in the
    worker's transaction, so a successful attempt commits them with its results; a
    failed attempt is rolled back and may be retried, so the same rows are also
    handed to the worker as an after-rollback write. Every attempt's spend then
    counts against the workspace's monthly budget, the next attempt's included.
    When the attempt ends, any reservation of this job still in flight (a settlement
    that could not be written) is released at its worst case.
    """
    scout_request_id = ctx.payload.get("scout_request_id")
    if not isinstance(scout_request_id, str) or not scout_request_id:
        raise JobExecutionError(
            JobErrorCode.VALIDATION,
            "scout_request.execute payload requires a string 'scout_request_id'",
        )

    # D-25 run fence (PROPOSED): the value this job was enqueued under; a job enqueued
    # before the fence existed carries none and is treated as fence 0.
    run_fence = ctx.payload.get("run_fence", 0)
    if isinstance(run_fence, bool) or not isinstance(run_fence, int) or run_fence < 0:
        raise JobExecutionError(
            JobErrorCode.VALIDATION,
            "scout_request.execute payload 'run_fence' must be a non-negative integer",
        )

    # Honour cancellation requested before the attempt starts. The pipeline runs
    # as a single unit, so this is the safe interruption point.
    if ctx.is_cancelled():
        raise JobExecutionError(JobErrorCode.CANCELLED, "cancelled before execution")

    # The run is this job: the call cap counts its earlier attempts' recorded rows.
    # The session factory is the seam for the shared ledger's own short transactions;
    # it is built from this attempt's bind, never from the process-wide engine, so a
    # test's or a worker's engine is the one the ledger writes to.
    meter = LLMMeter(
        get_settings(),
        job_id=ctx.job_id,
        attempt=ctx.attempt,
        session_factory=sessionmaker(
            bind=ctx.db.get_bind(), autoflush=False, expire_on_commit=False, future=True
        ),
    )
    # D-25 cooperative pause (PROPOSED, ``app.jobs.admission``): the run's committed
    # claim and its admissions, on short transactions from the same bind, scoped by the
    # durable job row's tenant context.
    admission = RunAdmission(
        scout_request_id=scout_request_id,
        organization_id=ctx.context.organization_id,
        workspace_id=ctx.context.workspace_id,
        fence=run_fence,
        session_factory=sessionmaker(
            bind=ctx.db.get_bind(), autoflush=False, expire_on_commit=False, future=True
        ),
        job_id=ctx.job_id,
        worker_id=ctx.worker_id,
        attempt=ctx.attempt,
    )
    ctx.after_rollback.append(meter.flush)
    with metered_run(meter):
        with admitted_run(admission):
            try:
                result = _run(ctx.db, scout_request_id, ctx.context)
                # The run's write phase ran in a savepoint (``admission.begin_writes``).
                admission.keep_writes()
            except RunStopped as stop:
                # A refused admission (the request was paused, or paused and resumed, since
                # this run claimed it). Roll back the run's savepoint: its findings are
                # discarded, never the job's bookkeeping around it. Its accounting stays: the
                # shared ledger committed every reservation, settlement and usage row in its
                # own transactions, and the same-session usage rows are written again here.
                # Not a failure: the job ends ``succeeded`` with the stop as its outcome, and
                # is not retried.
                admission.discard_writes()
                meter.flush(ctx.db)
                result = {"scout_request_id": scout_request_id, "outcome": stop.outcome}
                log_event(
                    logger,
                    "scout_request.run_stopped",
                    component="jobs",
                    outcome=stop.outcome,
                    stopped_at=stop.operation,
                    admitted=dict(admission.admitted),
                )
            except SignalVolumeAboveBoundError as exc:
                # P6-PLAT-1 (strategy B): the run would index more signals than the
                # per-run in-process bound. Non-retryable - the condition is a property
                # of the request's source volume, not of this attempt - and recorded as
                # the run's visible outcome after the rollback (nothing was written).
                # The refusal precedes every model call of the run, so it reserved and
                # spent nothing (P6-LLM-4); the terminal-failure hook below still
                # releases anything this job holds, as for every final failure.
                ctx.after_rollback.append(
                    _record_refused_run(
                        scout_request_id,
                        ctx.context,
                        exc,
                        run_fence=admission.fence if admission.claimed else None,
                    )
                )
                raise JobExecutionError(JobErrorCode.NON_RETRYABLE, exc.reason) from exc
            except Exception:
                # Any other failure of a claimed run: the worker rolls the attempt back, but
                # the claim was committed on its own. Put back the status it replaced (a
                # lease-aware no-op if another attempt owns the job by then), so a request
                # waiting for its retry shows what it showed before the claim (§6.2 Q-2).
                restore = admission.restore_writer(now=ctx.now)
                if restore is not None:
                    ctx.after_rollback.append(restore)
                raise
    # Attempt end: whatever this job still holds reserved could not be settled by the
    # process that reserved it; charge it its worst case in the run's transaction.
    release_for_job(ctx.db, job_id=ctx.job_id, now=ctx.now, outcome="attempt_end")

    # ``_run`` returns only safe aggregates: the run ``outcome`` (D-25: ``completed``;
    # ``skipped_request_paused`` when the request was paused, or its fence moved, before
    # the claim; ``request_gone``; and from a cooperative stop above
    # ``stopped_request_paused`` / ``status_changed_during_run`` / ``request_gone``) and,
    # for a run that completed, the four counts, the coverage outcome (P6-DATA-2) and the
    # per-source fetch facts (``fetch_limits`` / ``partial_fetch`` /
    # ``partial_fetch_reasons``, #268), which is exactly the shape we persist.
    return dict(result)


@register_handler(JobType.SCOUT_SCHEDULE_TICK)
def execute_schedule_tick(ctx: HandlerContext) -> dict[str, Any]:
    """Advance one recurring schedule (SB-B).

    A tick never runs the scouting pipeline itself: it enqueues at most one
    ``scout_request.execute`` run and its own successor tick. All recurrence,
    dark-deploy, overlap-coalescing and missed-run-skip decisions live in
    :func:`app.scouting_requests.schedules.run_schedule_tick`; this handler only
    validates the payload and adapts the cancellation probe. It returns a safe
    summary (outcome + booleans + next occurrence) — never any payload or secret.
    """
    # Deferred import breaks the cycle: the enqueue service imports this module to
    # register handlers, and the schedule service imports the enqueue service.
    from app.scouting_requests.schedules import run_schedule_tick

    schedule_id = ctx.payload.get("schedule_id")
    occurrence_raw = ctx.payload.get("occurrence_at")
    if not isinstance(schedule_id, str) or not schedule_id:
        raise JobExecutionError(
            JobErrorCode.VALIDATION,
            "scout_schedule.tick payload requires a string 'schedule_id'",
        )
    if not isinstance(occurrence_raw, str) or not occurrence_raw:
        raise JobExecutionError(
            JobErrorCode.VALIDATION,
            "scout_schedule.tick payload requires an ISO 'occurrence_at'",
        )
    try:
        occurrence_at = datetime.fromisoformat(occurrence_raw)
    except ValueError as exc:
        raise JobExecutionError(
            JobErrorCode.VALIDATION,
            "scout_schedule.tick 'occurrence_at' is not a valid ISO datetime",
        ) from exc

    return run_schedule_tick(
        ctx.db,
        schedule_id=schedule_id,
        occurrence_at=occurrence_at,
        now=ctx.now,
        is_cancelled=ctx.is_cancelled,
    )


@register_handler(JobType.RETENTION_PURGE)
def execute_retention_purge(ctx: HandlerContext) -> dict[str, Any]:
    """Purge one bounded batch of one workspace retention class (P6-PRIV-1).

    All validation, the policy re-check and the successor link live in
    :func:`app.privacy.retention.run_purge_job`; the deletion is confined to the
    workspace of the context rebuilt from the durable job row. Returns counts only.
    """
    # Deferred import: the privacy package imports the enqueue service, which imports
    # this module to register handlers.
    from app.privacy.retention import run_purge_job

    return run_purge_job(
        ctx.db,
        context=ctx.context,
        payload=ctx.payload,
        now=ctx.now,
        is_cancelled=ctx.is_cancelled,
    )


@register_terminal_failure_hook(JobType.SCOUT_SCHEDULE_TICK)
def recover_failed_schedule_tick(ctx: TerminalFailureContext) -> None:
    """Keep a recurring schedule alive after one of its ticks failed for good (P6-PLAT-6).

    A tick is the only thing that enqueues the next tick, so a tick that reaches
    ``failed`` or ``dead_lettered`` would otherwise end the schedule silently and
    forever. This hook hands the failure to
    :func:`app.scouting_requests.schedules.recover_after_tick_failure`, which re-seeds
    the next occurrence (skipping the failed one, never back-filling) under the same
    capability and enabled checks a healthy tick applies, and logs the loss.

    The tenant scope is the job row's (``ctx.context``), never the payload's, so a
    tampered payload naming another tenant's schedule is refused downstream. A
    payload this hook cannot parse is logged and left alone: there is no schedule it
    could safely identify. The worker owns the transaction: this writes and flushes,
    never commits.
    """
    from app.scouting_requests.schedules import recover_after_tick_failure

    schedule_id = ctx.payload.get("schedule_id")
    occurrence_raw = ctx.payload.get("occurrence_at")
    occurrence_at: datetime | None = None
    if isinstance(occurrence_raw, str) and occurrence_raw:
        try:
            occurrence_at = datetime.fromisoformat(occurrence_raw)
        except ValueError:
            occurrence_at = None
    if not isinstance(schedule_id, str) or not schedule_id or occurrence_at is None:
        log_event(
            logger,
            "scout_schedule_tick_failed",
            level=logging.ERROR,
            component="jobs",
            outcome="unrecoverable_payload",
            chained=False,
            job_status=ctx.status,
            error_class=ctx.error_code.value,
        )
        return

    recover_after_tick_failure(
        ctx.db,
        schedule_id=schedule_id,
        occurrence_at=occurrence_at,
        organization_id=ctx.context.organization_id,
        workspace_id=ctx.context.workspace_id,
        job_status=ctx.status,
        error_code=ctx.error_code.value,
        now=ctx.now,
    )


#: The in-flight states a scout request occupies only while a run is expected to
#: make progress. A final job failure means no run is coming, so a request left in
#: one of these is stranded: ``POST .../run`` rejects it with "already queued or
#: running", making the scout permanently un-runnable from the UI.
_IN_FLIGHT_SCOUT_STATUSES = frozenset(
    {ScoutRequestStatus.QUEUED.value, ScoutRequestStatus.RUNNING.value}
)


@register_terminal_failure_hook(JobType.SCOUT_REQUEST_EXECUTE)
def settle_failed_scout_request(ctx: TerminalFailureContext) -> None:
    """Settle a scout request whose execution job failed for the last time.

    Runs only on a *final* failure (``failed`` / ``dead_lettered``), never on a
    retryable attempt, and only from in-flight states — so a request a human has
    since paused, or one already settled, is left exactly as it is. ``failed`` is
    an existing settled state that ``POST .../run`` accepts, which is what makes
    the scout runnable again.

    The worker owns the transaction: this writes and flushes, never commits.

    The job's LLM reservations still in flight are released first (charged their
    worst case, ``app.llm.budget``): no attempt of this job will settle them now.
    """
    release_for_job(ctx.db, job_id=ctx.job_id, now=ctx.now, outcome="terminal")

    scout_request_id = ctx.payload.get("scout_request_id")
    if not isinstance(scout_request_id, str) or not scout_request_id:
        return

    request = ctx.db.get(ScoutRequest, scout_request_id)
    if request is None:
        return
    # Tenant guard, mirroring the pipeline's: the context is rebuilt from the
    # durable job row, so a mis-enqueued id can never settle another org's request.
    if (
        request.organization_id != ctx.context.organization_id
        or request.workspace_id != ctx.context.workspace_id
    ):
        return
    if request.status not in _IN_FLIGHT_SCOUT_STATUSES:
        return

    # D-25: a compare-and-set on the in-flight states, so a pause that commits after
    # the read above is not overwritten either (the read alone leaves that window). The
    # cooperative-pause candidate (PROPOSED) also requires this job's run fence (a legacy
    # payload without one is fence 0): after Pause -> Resume -> Run now the request's
    # ``queued``/``running`` belongs to the newer job, which this failure must not settle.
    predicates = [
        ScoutRequest.id == request.id,
        ScoutRequest.status.in_(sorted(_IN_FLIGHT_SCOUT_STATUSES)),
    ]
    run_fence = ctx.payload.get("run_fence", 0)
    if isinstance(run_fence, int) and not isinstance(run_fence, bool):
        predicates.append(ScoutRequest.run_fence == run_fence)
    settled = ctx.db.execute(
        update(ScoutRequest)
        .where(*predicates)
        .values(status=ScoutRequestStatus.FAILED.value)
        .execution_options(synchronize_session=False)
    )
    ctx.db.refresh(request, attribute_names=["status"])
    if settled.rowcount != 1:
        return
    log_event(
        logger,
        "scout_request.settled_after_job_failure",
        component="jobs",
        outcome="failed",
        job_type=ctx.job_type,
        job_status=ctx.status,
        error_class=ctx.error_code.value,
    )


__all__ = [
    "execute_retention_purge",
    "execute_schedule_tick",
    "execute_scout_request",
    "recover_failed_schedule_tick",
    "settle_failed_scout_request",
]
