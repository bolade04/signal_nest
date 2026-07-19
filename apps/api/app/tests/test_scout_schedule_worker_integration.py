"""SB-D: end-to-end scheduling hardening through the *real* durable worker path.

Where ``test_scout_schedules.py`` exercises the schedule service seam directly (calling
``run_schedule_tick`` in-process) and ``test_scout_schedule_api.py`` exercises the HTTP
contract, this suite closes the remaining gap: it drives the actual worker
(:meth:`app.jobs.worker.JobRunner.poll_once`) so a ``scout_schedule.tick`` is *claimed,
executed, chained and completed* by the same code that runs in production, and the
``scout_request.execute`` run it fans out is then claimed and run by the worker too.

It proves the operational invariants that only appear once the durable claim/lease/
recover machinery is in the loop, across the four independent demo markets
(Dallas, London, Lagos, Nairobi):

* a live tick fans out exactly one scheduled run, self-chains its successor, and both
  jobs reach a clean terminal state through the worker;
* the four markets advance independently — firing one market's chain never enqueues,
  runs or disturbs another's;
* a crashed/stale tick whose lease expired is recovered and re-run to a single fan-out
  (at-least-once delivery never double-runs a market);
* duplicate delivery of the same (schedule, occurrence) tick collapses to one run;
* a scheduled run that fails in one market fails in *isolation* — the other markets'
  scheduled runs still succeed;
* manual and scheduled runs coexist in one request and are labelled honestly in the
  customer-safe run history.

The throwaway four-market seed creates no ``Job`` rows, so every job observed here was
produced by the code under test. The database is file-backed because the worker opens
its own sessions; the feature flag is turned on per test (dark by default).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.core.enums import ScheduleInterval
from app.db import seed as seed_mod
from app.db.models import Base
from app.jobs.models import Job
from app.jobs.status import JobErrorCode, JobExecutionError, JobStatus, JobType
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner

# Importing the app registers every ORM model + job handler on the shared Base/registry
# so the throwaway schema is complete and the worker can resolve tick/execute handlers.
from app.main import app  # noqa: F401
from app.scouting_requests import schedules as sched
from app.scouting_requests.models import ScoutRequest, ScoutSchedule
from app.scouting_requests.run_history import get_run_history

_MARKETS = ("dallas", "london", "lagos", "nairobi")
# A fixed enable instant. The seeded tick is scheduled_for T0 + interval, so a worker
# clock past that boundary is what makes the tick claimable.
_T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)


# --------------------------------------------------------------------------- #
# Harness — file-backed seed + real worker
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scout_schedule_worker")
    engine = create_engine(
        f"sqlite:///{tmp/'sched_worker.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        seed_mod.seed(reset=True)
        yield make
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


@pytest.fixture(autouse=True)
def _clean(factory):
    """Every test starts with no schedules and no jobs, and the feature dark.

    Cleanup runs after the test so an assertion failure leaves the rows for
    inspection; the flag is reset here too so a test that turned it on cannot leak
    into the next.
    """
    get_settings().scout_scheduling_enabled = False
    yield
    with factory() as s:
        s.query(Job).delete(synchronize_session=False)
        s.query(ScoutSchedule).delete(synchronize_session=False)
        s.commit()
    get_settings().scout_scheduling_enabled = False


@pytest.fixture
def flag_on():
    get_settings().scout_scheduling_enabled = True


def _store() -> DurableJobStore:
    return DurableJobStore()


def _runner(factory, *, clock: datetime) -> JobRunner:
    """A real JobRunner bound to the seeded engine with a pinned clock.

    The pinned clock is what advances the worker past a future-dated tick's
    ``scheduled_for`` so it becomes claimable — without waiting real wall-clock time.
    """
    return JobRunner(
        settings=Settings(_env_file=None),
        session_factory=factory,
        store=_store(),
        clock=lambda: clock,
    )


def _drain(factory, *, clock: datetime, limit: int = 20) -> int:
    """Poll the worker to quiescence at ``clock``; return how many jobs it ran."""
    runner = _runner(factory, clock=clock)
    ran = 0
    for _ in range(limit):
        if not runner.poll_once(worker_id="w-sbd"):
            break
        ran += 1
    else:  # pragma: no cover - a runaway chain would trip this guard
        raise AssertionError("worker did not reach quiescence within the poll budget")
    return ran


def _req(s, market: str) -> ScoutRequest:
    return s.get(ScoutRequest, seed_mod.sid("scout", market))


def _create_live(factory, market: str, *, interval=ScheduleInterval.DAILY) -> str:
    """Create an enabled schedule for a market at _T0 (seeds the first tick). Returns id."""
    with factory() as s:
        schedule = sched.create_schedule(
            s, request=_req(s, market), interval=interval, now=_T0
        )
        s.commit()
        return schedule.id


def _jobs(factory, market: str, job_type: JobType) -> list[Job]:
    with factory() as s:
        return list(
            s.execute(
                select(Job).where(
                    Job.scout_request_id == seed_mod.sid("scout", market),
                    Job.job_type == job_type.value,
                )
            ).scalars()
        )


def _execs(factory, market: str) -> list[Job]:
    return _jobs(factory, market, JobType.SCOUT_REQUEST_EXECUTE)


def _ticks(factory, market: str) -> list[Job]:
    return _jobs(factory, market, JobType.SCOUT_SCHEDULE_TICK)


# --------------------------------------------------------------------------- #
# One market: fan-out + self-chain + run, all through the worker
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("market", _MARKETS)
def test_tick_fans_out_chains_and_runs_through_worker(factory, flag_on, market):
    _create_live(factory, market)
    # The seeded tick is scheduled_for _T0 + 24h; advance the worker just past it.
    clock = _T0 + timedelta(hours=24, minutes=1)

    _drain(factory, clock=clock)

    ticks = _ticks(factory, market)
    execs = _execs(factory, market)
    # Exactly one scheduled run was fanned out and it ran to success.
    assert len(execs) == 1
    assert execs[0].status == JobStatus.SUCCEEDED.value
    assert execs[0].payload.get("trigger") == sched.SCHEDULED_TRIGGER
    # The originating tick completed and a single successor tick was chained; the
    # successor is future-dated (not yet due at this clock), so it stays enqueued.
    terminal = [t for t in ticks if t.status == JobStatus.SUCCEEDED.value]
    pending = [t for t in ticks if t.status != JobStatus.SUCCEEDED.value]
    assert len(terminal) == 1
    assert len(pending) == 1
    assert pending[0].status == JobStatus.SCHEDULED.value

    # SB-A regression through the worker: the scheduled run appears in run history
    # labelled "scheduled"; the tick bookkeeping job never does.
    with factory() as s:
        req = _req(s, market)
        history = get_run_history(
            s,
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=req.id,
        )
    assert history.total == 1
    assert history.items[0].trigger == "scheduled"
    assert history.items[0].id == execs[0].id


# --------------------------------------------------------------------------- #
# Regression (issue #59): fan-out timestamps derive from the injected clock
# --------------------------------------------------------------------------- #
def test_fan_out_timestamps_derive_from_injected_clock(factory, flag_on):
    """The tick must stamp its fan-out from the *worker's* clock, not the wall clock.

    With the worker pinned decades away from real wall time, the run it fans out — and
    the schedule's ``last_tick_at`` bookkeeping — must both derive from that injected
    clock, and the run must be immediately claimable at the very same clock. Before the
    fix the tick fell back to real ``utcnow()``: the run was stamped with wall-clock
    time and, once real time crossed the pinned claim boundary, was never due and stayed
    ``pending``. This asserts pinned-clock equality (tz-normalised for the SQLite
    round-trip), which the old wall-clock path fails regardless of when it runs.
    """
    schedule_id = _create_live(factory, "dallas")
    # A clock decades from real wall time: no real ``utcnow()`` can coincide with it,
    # so an equality check cleanly separates injected-clock from wall-clock behaviour.
    pinned = datetime(2099, 1, 2, 9, 0, 0, tzinfo=UTC)

    _drain(factory, clock=pinned)

    execs = _execs(factory, "dallas")
    assert len(execs) == 1
    # The fanned-out run's readiness (``available_at``, the field the claim predicate
    # ``available_at <= now`` tests) is stamped on the injected clock, not the wall
    # clock. Before the fix this was real ``utcnow()``; once real time ran ahead of the
    # pinned claim clock the run was never due and stayed pending.
    assert execs[0].available_at.replace(tzinfo=None) == pinned.replace(tzinfo=None)
    # Being due at exactly that clock, it was claimed and run to success by the same
    # pinned-clock worker — no wall-clock drift could leave it pending.
    assert execs[0].status == JobStatus.SUCCEEDED.value

    # The schedule's tick bookkeeping is stamped on the injected clock too.
    with factory() as s:
        schedule = s.get(ScoutSchedule, schedule_id)
        assert schedule.last_tick_at.replace(tzinfo=None) == pinned.replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Four markets advance independently through the worker
# --------------------------------------------------------------------------- #
def test_four_markets_advance_independently(factory, flag_on):
    for market in _MARKETS:
        _create_live(factory, market)
    # All four daily ticks fall due together at _T0+24h. Drain the worker and prove
    # each market's fan-out is scoped strictly to its own request — no scheduled run
    # is ever attributed to, or shared with, another market.
    clock = _T0 + timedelta(hours=24, minutes=1)
    _drain(factory, clock=clock)

    # Every market fanned out exactly its own single run — never another market's.
    for market in _MARKETS:
        execs = _execs(factory, market)
        assert len(execs) == 1, market
        assert execs[0].scout_request_id == seed_mod.sid("scout", market)
        assert execs[0].status == JobStatus.SUCCEEDED.value

    # No run id is shared across two markets (no cross-request bleed).
    seen: set[str] = set()
    for market in _MARKETS:
        ids = {e.id for e in _execs(factory, market)}
        assert seen.isdisjoint(ids), market
        seen |= ids


def test_only_the_fired_market_runs_when_others_are_not_yet_due(factory, flag_on):
    # Stagger cadences so only Dallas's tick is due at the chosen clock: Dallas daily
    # (tick at _T0+24h), the rest weekly (ticks at _T0+7d). A clock between the two
    # boundaries makes exactly one market claimable.
    _create_live(factory, "dallas", interval=ScheduleInterval.DAILY)
    for market in ("london", "lagos", "nairobi"):
        _create_live(factory, market, interval=ScheduleInterval.WEEKLY)

    clock = _T0 + timedelta(hours=24, minutes=1)  # past daily, before weekly
    _drain(factory, clock=clock)

    # Dallas fanned out and ran; the weekly markets' ticks are still scheduled and no
    # run was produced for them.
    assert len(_execs(factory, "dallas")) == 1
    for market in ("london", "lagos", "nairobi"):
        assert _execs(factory, market) == [], market
        ticks = _ticks(factory, market)
        assert len(ticks) == 1 and ticks[0].status == JobStatus.SCHEDULED.value, market


# --------------------------------------------------------------------------- #
# Stale/crashed tick: lease recovery re-runs to a single fan-out
# --------------------------------------------------------------------------- #
def test_stale_tick_is_recovered_and_fans_out_exactly_once(factory, flag_on):
    _create_live(factory, "dallas")
    store = _store()
    claim_at = _T0 + timedelta(hours=24, minutes=1)

    # Worker A claims + starts the tick, then "crashes" (never completes).
    with factory() as sa:
        tick = store.claim_one(sa, worker_id="A", lease_seconds=30, now=claim_at)
        assert tick is not None and tick.job_type == JobType.SCOUT_SCHEDULE_TICK.value
        store.mark_running(sa, tick, worker_id=tick.worker_id,
                           lease_token=tick.lease_token, now=claim_at)
        sa.commit()

    # After the lease expires, recovery returns the abandoned tick to the queue.
    recover_at = claim_at + timedelta(hours=1)
    with factory() as sr:
        assert store.recover_expired_leases(sr, now=recover_at) == 1
        sr.commit()

    # A healthy worker then drains it: the recovered tick runs *once*, fanning out a
    # single scheduled run despite the earlier abandoned claim (at-least-once safety).
    _drain(factory, clock=recover_at + timedelta(minutes=1))

    execs = _execs(factory, "dallas")
    assert len(execs) == 1
    assert execs[0].status == JobStatus.SUCCEEDED.value
    terminal_ticks = [t for t in _ticks(factory, "dallas")
                      if t.status == JobStatus.SUCCEEDED.value]
    assert len(terminal_ticks) == 1


# --------------------------------------------------------------------------- #
# Duplicate delivery of the same (schedule, occurrence) tick collapses to one run
# --------------------------------------------------------------------------- #
def test_duplicate_tick_delivery_collapses_to_one_run(factory, flag_on):
    with factory() as s:
        schedule = sched.create_schedule(
            s, request=_req(s, "dallas"), interval=ScheduleInterval.DAILY, now=_T0
        )
        occurrence = schedule.next_run_at
        # A redundant delivery of the very same occurrence (e.g. an at-least-once
        # re-enqueue) must collapse onto the already-seeded tick via its idempotency
        # key rather than creating a second tick job.
        again = sched.enqueue_schedule_tick(
            s, schedule=schedule, occurrence_at=occurrence, now=_T0
        )
        s.commit()
        seeded = [t for t in _ticks(factory, "dallas")]
        assert again.id == seeded[0].id
        assert len(seeded) == 1

    _drain(factory, clock=_T0 + timedelta(hours=24, minutes=1))

    # Exactly one scheduled run resulted from the (doubly-delivered) single occurrence.
    assert len(_execs(factory, "dallas")) == 1


# --------------------------------------------------------------------------- #
# A scheduled run that fails does so in isolation from the other markets
# --------------------------------------------------------------------------- #
def test_scheduled_run_failure_is_market_isolated(factory, flag_on, monkeypatch):
    from app.jobs import handlers as handlers_mod

    real_run = handlers_mod._run
    lagos_id = seed_mod.sid("scout", "lagos")

    def _run_with_lagos_failure(db, scout_request_id, context=None):
        if scout_request_id == lagos_id:
            # A permanent (non-retryable) failure so the run fails fast without burning
            # a backoff schedule — the point is cross-market isolation, not retry timing.
            raise JobExecutionError(JobErrorCode.VALIDATION, "injected market failure")
        return real_run(db, scout_request_id, context)

    monkeypatch.setattr(handlers_mod, "_run", _run_with_lagos_failure)

    for market in _MARKETS:
        _create_live(factory, market)
    _drain(factory, clock=_T0 + timedelta(hours=24, minutes=1))

    # Lagos's scheduled run failed fast...
    lagos_execs = _execs(factory, "lagos")
    assert len(lagos_execs) == 1
    assert lagos_execs[0].status == JobStatus.FAILED.value
    assert lagos_execs[0].last_error_code == JobErrorCode.VALIDATION.value
    # ...while every other market's scheduled run still succeeded, unaffected.
    for market in ("dallas", "london", "nairobi"):
        execs = _execs(factory, market)
        assert len(execs) == 1, market
        assert execs[0].status == JobStatus.SUCCEEDED.value, market


# --------------------------------------------------------------------------- #
# Manual and scheduled runs coexist in one request, labelled honestly
# --------------------------------------------------------------------------- #
def test_manual_and_scheduled_runs_coexist(factory, flag_on):
    from app.jobs.service import enqueue_scout_request

    _create_live(factory, "dallas")
    clock = _T0 + timedelta(hours=24, minutes=1)
    # Run the scheduled chain first (drains the tick + its scheduled run to success).
    _drain(factory, clock=clock)

    # Then a customer triggers a manual run for the same request (no trigger marker).
    # Pin the manual enqueue to the same injected clock as the worker so the harness
    # never depends on real wall time (a manual run is enqueued "now"; here "now" is
    # the pinned clock, exactly as the scheduled chain above was driven).
    with factory() as s:
        req = _req(s, "dallas")
        enqueue_scout_request(
            s,
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=req.id,
            location_id=req.location_id,
            now=clock,
        )
        s.commit()
    _drain(factory, clock=clock + timedelta(minutes=1))

    execs = _execs(factory, "dallas")
    assert len(execs) == 2
    assert all(e.status == JobStatus.SUCCEEDED.value for e in execs)

    with factory() as s:
        req = _req(s, "dallas")
        history = get_run_history(
            s,
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=req.id,
        )
    # Both runs are present and labelled by how they were enqueued — one scheduled,
    # one manual — never conflated.
    assert history.total == 2
    triggers = sorted(item.trigger for item in history.items)
    assert triggers == ["manual", "scheduled"]
