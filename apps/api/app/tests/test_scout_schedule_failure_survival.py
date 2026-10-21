"""P6-PLAT-6: a dead-lettered (or failed) tick no longer ends a recurring schedule.

The schedule chain is self-propagating with no external cron: a tick is the only thing
that enqueues the next tick. ``dead_lettered`` and ``failed`` are terminal with no
outgoing transition, so before this change one tick that exhausted its retries — or whose
lease expired with no attempts left — ended the schedule forever, with no requeue path and
nothing an alarm could see.

These tests drive the real durable worker (:meth:`app.jobs.worker.JobRunner.poll_once`)
over the four-market demo seed and prove:

* **Survival, on both dead-letter paths.** A tick that dead-letters in ``_fail`` (retries
  exhausted) or inside lease recovery (lease expired, no attempts left), or fails
  non-retryably, re-seeds the next occurrence through the tick's terminal-failure hook.
  The failed occurrence is skipped, never back-filled, and the chain cannot fork.
* **The job-store invariants are untouched.** The failed tick stays terminal (no
  status is ever revived); recovery stays single-winner; the successor is an ordinary
  idempotent enqueue.
* **The chain stops only when a healthy tick would also have stopped** — schedule
  deleted, paused, or scheduling unavailable for the workspace — and is never touched
  across a tenant boundary.
* **Visibility.** Every final tick failure logs ``scout_schedule_tick_failed`` (ERROR);
  ``jobs_dead_lettered_total{job_type="scout_schedule.tick"}`` counts both dead-letter
  paths; the API reports ``last_tick_failed_at`` and, when the chain did stop, the
  derived state ``stalled`` (customer and operator views, plus the operator overview
  count) — and ``resume`` restarts a stalled schedule.

The seed creates no ``Job`` rows, so every job observed here was produced by the code
under test. Scheduling is dark by default and enabled per test.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

import app.scouting_requests.schedules as sched
from app.core.config import Settings, get_settings
from app.core.enums import ScheduleState, ScoutRequestStatus
from app.core.metrics import JOBS_DEAD_LETTERED_TOTAL, InMemoryMetrics, configure_metrics
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.jobs.context import ExecutionContext
from app.jobs.handlers import recover_failed_schedule_tick
from app.jobs.models import Job
from app.jobs.registry import TerminalFailureContext
from app.jobs.service import enqueue_scout_request
from app.jobs.status import JobErrorCode, JobExecutionError, JobStatus, JobType
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.main import app
from app.organizations.models import User
from app.scouting_requests.models import ScoutRequest, ScoutSchedule
from app.tests._auth2_support import live_bearer

API = get_settings().api_prefix
TICK = JobType.SCOUT_SCHEDULE_TICK.value
# Enable instant. A daily schedule created here seeds its first tick for _T0 + 24h.
_T0 = datetime(2026, 7, 18, 12, 0, 0, tzinfo=UTC)
_FIRST = _T0 + timedelta(hours=24)
_SECOND = _T0 + timedelta(hours=48)
_OPERATOR = "operator-plat6"


def _naive(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo on the round trip; compare instants as naive UTC."""
    return None if dt is None else dt.replace(tzinfo=None)


# --------------------------------------------------------------------------- #
# Harness — file-backed seed, real worker, HTTP client on the same database
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("schedule_failure_survival")
    engine = create_engine(
        f"sqlite:///{tmp/'plat6.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        seed_mod.seed(reset=True)
        with make() as s:
            s.add(User(id=_OPERATOR, email="op6@example.com", full_name="Operator",
                       hashed_password="x", is_active=True, is_operator=True))
            s.commit()
        yield make
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


@pytest.fixture(autouse=True)
def _clean(factory, monkeypatch):
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", False)
    yield
    with factory() as s:
        s.query(Job).delete(synchronize_session=False)
        s.query(ScoutSchedule).delete(synchronize_session=False)
        for req in s.scalars(select(ScoutRequest)):
            req.status = ScoutRequestStatus.DRAFT.value
        s.commit()


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", True)


@pytest.fixture
def metrics():
    backend = InMemoryMetrics()
    configure_metrics(backend)
    try:
        yield backend
    finally:
        configure_metrics(None)


@pytest.fixture
def client(factory):
    def _override_get_db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _runner(factory, clock: datetime) -> JobRunner:
    return JobRunner(
        settings=Settings(_env_file=None),
        session_factory=factory,
        store=DurableJobStore(),
        clock=lambda: clock,
    )


def _poll_until_quiet(factory, clock: datetime, limit: int = 20) -> None:
    runner = _runner(factory, clock)
    for _ in range(limit):
        if not runner.poll_once(worker_id="w-plat6"):
            return
    raise AssertionError("worker did not reach quiescence")  # pragma: no cover


def _req_id(market: str = "dallas") -> str:
    return seed_mod.sid("scout", market)


def _create_live(factory, market: str = "dallas") -> str:
    with factory() as s:
        schedule = sched.create_schedule(
            s, request=s.get(ScoutRequest, _req_id(market)), interval="daily", now=_T0
        )
        s.commit()
        return schedule.id


def _ticks(factory, market: str = "dallas") -> list[Job]:
    with factory() as s:
        return list(s.scalars(
            select(Job)
            .where(Job.scout_request_id == _req_id(market), Job.job_type == TICK)
            .order_by(Job.created_at)
        ))


def _schedule(factory, schedule_id: str) -> ScoutSchedule:
    with factory() as s:
        return s.get(ScoutSchedule, schedule_id)


def _state(factory, schedule_id: str):
    with factory() as s:
        row = s.get(ScoutSchedule, schedule_id)
        return sched.derive_schedule_state(s, row), sched.last_tick_failure(s, row)


def _break_fan_out(monkeypatch, exc: Exception) -> dict:
    """Make every tick's fan-out raise ``exc`` until ``switch["broken"]`` is cleared."""
    switch = {"broken": True}
    real = sched.enqueue_scout_request

    def _maybe_fail(*args, **kwargs):
        if switch["broken"]:
            raise exc
        return real(*args, **kwargs)

    monkeypatch.setattr(sched, "enqueue_scout_request", _maybe_fail)
    return switch


def _crash_on_last_attempt(factory, *, at: datetime) -> str:
    """Claim + start the due tick on its last permitted attempt, then abandon it.

    Leaves the tick ``running`` with a 30s lease and ``attempt_count == max_attempts``,
    so the next lease recovery must dead-letter it — the second dead-letter path.
    """
    store = DurableJobStore()
    with factory() as s:
        (tick,) = s.scalars(select(Job).where(Job.job_type == TICK)).all()
        tick.max_attempts = 1
        s.commit()
        claimed = store.claim_one(s, worker_id="crashed", lease_seconds=30, now=at)
        assert claimed is not None and claimed.id == tick.id
        store.mark_running(s, claimed, worker_id=claimed.worker_id,
                           lease_token=claimed.lease_token, now=at)
        s.commit()
        return claimed.id


def _failure_logs(caplog) -> list[dict]:
    return [
        r.extra_fields for r in caplog.records
        if r.getMessage() == "scout_schedule_tick_failed"
    ]


# --------------------------------------------------------------------------- #
# Survival — handler-path dead-letter (retries exhausted)
# --------------------------------------------------------------------------- #
def test_dead_lettered_tick_reseeds_the_next_occurrence(
    factory, flag_on, monkeypatch, metrics, caplog
):
    caplog.set_level(logging.INFO)
    schedule_id = _create_live(factory)
    switch = _break_fan_out(monkeypatch, RuntimeError("transient fan-out fault"))

    # Drive the failing tick through every retry (backoff <= 300s) until it is final.
    clock = _FIRST + timedelta(minutes=1)
    for _ in range(12):
        _poll_until_quiet(factory, clock)
        if _ticks(factory)[0].status not in (
            JobStatus.RETRY_WAIT.value, JobStatus.PENDING.value
        ):
            break
        clock += timedelta(minutes=10)

    first, *rest = _ticks(factory)
    assert first.status == JobStatus.DEAD_LETTERED.value
    assert first.attempt_count == first.max_attempts
    assert first.last_error_code == JobErrorCode.TRANSIENT.value
    # Exactly one successor, for the next boundary; the failed occurrence is skipped.
    assert len(rest) == 1
    successor = rest[0]
    assert successor.status == JobStatus.SCHEDULED.value
    assert _naive(successor.scheduled_for) == _naive(_SECOND)
    assert successor.idempotency_key == f"schedule-tick:{schedule_id}:{_SECOND.isoformat()}"
    schedule = _schedule(factory, schedule_id)
    assert _naive(schedule.next_run_at) == _naive(_SECOND)
    assert schedule.last_tick_at is None  # no occurrence actually fired

    state, failure = _state(factory, schedule_id)
    assert state is ScheduleState.ACTIVE
    assert failure is not None and failure.job_status == JobStatus.DEAD_LETTERED.value

    # Alarm signals: the dead-letter counter with the tick's job_type, and one ERROR log
    # saying the chain survived.
    assert metrics.counter_value(
        JOBS_DEAD_LETTERED_TOTAL, job_type=TICK, error_class=JobErrorCode.TRANSIENT.value
    ) == 1
    (logged,) = _failure_logs(caplog)
    assert logged["outcome"] == "reseeded" and logged["chained"] is True
    assert logged["job_status"] == JobStatus.DEAD_LETTERED.value
    assert [r.levelno for r in caplog.records
            if r.getMessage() == "scout_schedule_tick_failed"] == [logging.ERROR]

    # Recurrence really continues: once the fault clears, the successor fans out a run,
    # chains on, and the failure signal clears by itself.
    switch["broken"] = False
    _poll_until_quiet(factory, _SECOND + timedelta(minutes=1))
    assert _ticks(factory)[1].status == JobStatus.SUCCEEDED.value
    with factory() as s:
        runs = s.scalars(select(Job).where(
            Job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value
        )).all()
    assert len(runs) == 1
    state, failure = _state(factory, schedule_id)
    assert state is ScheduleState.ACTIVE and failure is None


def test_non_retryable_failed_tick_also_reseeds(factory, flag_on, monkeypatch):
    schedule_id = _create_live(factory)
    _break_fan_out(
        monkeypatch, JobExecutionError(JobErrorCode.VALIDATION, "injected permanent fault")
    )
    _poll_until_quiet(factory, _FIRST + timedelta(minutes=1))

    first, successor = _ticks(factory)
    assert first.status == JobStatus.FAILED.value
    assert successor.status == JobStatus.SCHEDULED.value
    assert _naive(successor.scheduled_for) == _naive(_SECOND)
    state, failure = _state(factory, schedule_id)
    assert state is ScheduleState.ACTIVE
    assert failure is not None and failure.error_code == JobErrorCode.VALIDATION.value


# --------------------------------------------------------------------------- #
# Survival — lease-recovery dead-letter (lease expired, no attempts left)
# --------------------------------------------------------------------------- #
def test_lease_expired_dead_letter_reseeds_and_is_counted(factory, flag_on, metrics):
    schedule_id = _create_live(factory)
    crashed = _crash_on_last_attempt(factory, at=_FIRST + timedelta(minutes=1))

    # The next poll's recovery dead-letters the abandoned tick and runs the hook.
    _runner(factory, _FIRST + timedelta(hours=1)).poll_once(worker_id="w-plat6")

    first, successor = _ticks(factory)
    assert first.id == crashed
    assert first.status == JobStatus.DEAD_LETTERED.value
    assert first.last_error_code == JobErrorCode.TIMEOUT.value
    assert successor.status == JobStatus.SCHEDULED.value
    assert _naive(successor.scheduled_for) == _naive(_SECOND)
    assert metrics.counter_value(
        JOBS_DEAD_LETTERED_TOTAL, job_type=TICK, error_class=JobErrorCode.TIMEOUT.value
    ) == 1
    state, failure = _state(factory, schedule_id)
    assert state is ScheduleState.ACTIVE
    assert failure is not None and failure.error_code == JobErrorCode.TIMEOUT.value


def test_missed_boundaries_after_a_late_failure_are_skipped_not_backfilled(factory, flag_on):
    _create_live(factory)
    _crash_on_last_attempt(factory, at=_FIRST + timedelta(minutes=1))
    # Recovery happens 3.5 days after the failed occurrence: the boundaries at +1d,
    # +2d and +3d are in the past and must not be replayed.
    late = _FIRST + timedelta(days=3, hours=12)
    _runner(factory, late).poll_once(worker_id="w-plat6")

    _, successor = _ticks(factory)
    assert _naive(successor.scheduled_for) == _naive(_FIRST + timedelta(days=4))
    with factory() as s:
        assert s.scalars(select(Job).where(
            Job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value
        )).all() == []


def test_lease_expired_execute_job_now_settles_its_scout_request(factory):
    """The recovery path runs *every* job type's terminal-failure hook, not only the tick's.

    A ``scout_request.execute`` that dead-letters through lease recovery used to leave
    its request stranded in ``queued`` (only the ``_fail`` path ran the hook).
    """
    store = DurableJobStore()
    req_id = _req_id("london")
    with factory() as s:
        req = s.get(ScoutRequest, req_id)
        req.status = ScoutRequestStatus.QUEUED.value
        job = enqueue_scout_request(
            s, organization_id=req.organization_id, workspace_id=req.workspace_id,
            scout_request_id=req.id, location_id=req.location_id, now=_T0,
        )
        job.max_attempts = 1
        s.commit()
        claimed = store.claim_one(s, worker_id="crashed", lease_seconds=30, now=_T0)
        store.mark_running(s, claimed, worker_id=claimed.worker_id,
                           lease_token=claimed.lease_token, now=_T0)
        s.commit()

    _runner(factory, _T0 + timedelta(hours=1)).poll_once(worker_id="w-plat6")

    with factory() as s:
        assert s.get(Job, job.id).status == JobStatus.DEAD_LETTERED.value
        assert s.get(ScoutRequest, req_id).status == ScoutRequestStatus.FAILED.value


# --------------------------------------------------------------------------- #
# The chain stops only where a healthy tick would also stop — and it is visible
# --------------------------------------------------------------------------- #
def test_chain_stops_visibly_when_scheduling_is_off_at_failure_then_resume_restarts(
    factory, client, monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", True)
    schedule_id = _create_live(factory)
    _crash_on_last_attempt(factory, at=_FIRST + timedelta(minutes=1))
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", False)
    _runner(factory, _FIRST + timedelta(hours=1)).poll_once(worker_id="w-plat6")

    assert len(_ticks(factory)) == 1  # no successor
    (logged,) = _failure_logs(caplog)
    assert logged["outcome"] == "feature_disabled" and logged["chained"] is False

    # Customer view: the broken chain is reported as such, not as activation_required.
    ws = seed_mod.sid("ws")
    owner = {"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"}
    url = f"{API}/workspaces/{ws}/scout-requests/{_req_id()}/schedule"
    body = client.get(url, headers=owner).json()
    assert body["state"] == ScheduleState.STALLED.value
    assert body["last_tick_failed_at"] is not None

    # Operator views: the fleet row carries the classification, the overview counts it.
    op = {"Authorization": f"Bearer {live_bearer(_OPERATOR)}"}
    (row,) = client.get(f"{API}/internal/system/schedules", headers=op).json()["items"]
    assert row["state"] == ScheduleState.STALLED.value
    assert row["last_tick_error_code"] == JobErrorCode.TIMEOUT.value
    overview = client.get(f"{API}/internal/system/overview", headers=op).json()
    assert overview["schedules"]["state_counts"][ScheduleState.STALLED.value] == 1

    # Resume is the supervised restart: it seeds exactly one fresh tick.
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", True)
    r = client.post(f"{url}/resume", headers=owner)
    assert r.status_code == 200, r.text
    assert r.json()["state"] == ScheduleState.ACTIVE.value
    live = [t for t in _ticks(factory) if t.status == JobStatus.SCHEDULED.value]
    assert len(live) == 1
    assert _state(factory, schedule_id)[0] is ScheduleState.ACTIVE


def test_paused_schedule_is_not_reseeded(factory, flag_on):
    schedule_id = _create_live(factory)
    _crash_on_last_attempt(factory, at=_FIRST + timedelta(minutes=1))
    with factory() as s:
        sched.pause_schedule(s, schedule=s.get(ScoutSchedule, schedule_id))
        s.commit()
    _runner(factory, _FIRST + timedelta(hours=1)).poll_once(worker_id="w-plat6")
    assert len(_ticks(factory)) == 1
    assert _state(factory, schedule_id)[0] is ScheduleState.PAUSED


def test_deleted_schedule_is_not_reseeded(factory, flag_on, caplog):
    caplog.set_level(logging.INFO)
    schedule_id = _create_live(factory)
    _crash_on_last_attempt(factory, at=_FIRST + timedelta(minutes=1))
    with factory() as s:
        sched.delete_schedule(s, schedule=s.get(ScoutSchedule, schedule_id))
        s.commit()
    _runner(factory, _FIRST + timedelta(hours=1)).poll_once(worker_id="w-plat6")
    assert len(_ticks(factory)) == 1
    assert _failure_logs(caplog)[0]["outcome"] == "schedule_missing"


def test_a_failing_recovery_still_records_the_job_failure_and_shows_stalled(
    factory, flag_on, monkeypatch
):
    schedule_id = _create_live(factory)
    _crash_on_last_attempt(factory, at=_FIRST + timedelta(minutes=1))

    def _boom(*_a, **_k):
        raise RuntimeError("re-seed failed")

    monkeypatch.setattr(sched, "recover_after_tick_failure", _boom)
    _runner(factory, _FIRST + timedelta(hours=1)).poll_once(worker_id="w-plat6")

    (only,) = _ticks(factory)
    assert only.status == JobStatus.DEAD_LETTERED.value  # the failure is never lost
    state, failure = _state(factory, schedule_id)
    assert state is ScheduleState.STALLED and failure is not None


# --------------------------------------------------------------------------- #
# Recovery invariants, exercised directly
# --------------------------------------------------------------------------- #
def _recover(factory, schedule_id: str, *, org: str | None = None, now: datetime) -> dict:
    with factory() as s:
        row = s.get(ScoutSchedule, schedule_id)
        result = sched.recover_after_tick_failure(
            s, schedule_id=schedule_id, occurrence_at=_FIRST,
            organization_id=org or row.organization_id, workspace_id=row.workspace_id,
            job_status=JobStatus.DEAD_LETTERED.value,
            error_code=JobErrorCode.TRANSIENT.value, now=now,
        )
        s.commit()
        return result


def _retire_seeded_tick(factory) -> None:
    """Mark the seeded first tick dead-lettered, as the store would have."""
    with factory() as s:
        (tick,) = s.scalars(select(Job).where(Job.job_type == TICK)).all()
        tick.status = JobStatus.DEAD_LETTERED.value
        tick.completed_at = _FIRST + timedelta(minutes=5)
        tick.last_error_code = JobErrorCode.TRANSIENT.value
        s.commit()


def test_repeated_recovery_never_forks_the_chain(factory, flag_on):
    schedule_id = _create_live(factory)
    _retire_seeded_tick(factory)
    now = _FIRST + timedelta(minutes=10)
    assert _recover(factory, schedule_id, now=now)["outcome"] == "reseeded"
    again = _recover(factory, schedule_id, now=now)
    assert again["outcome"] == "chain_live" and again["chained"] is True
    assert len([t for t in _ticks(factory) if t.status == JobStatus.SCHEDULED.value]) == 1


def test_recovery_never_crosses_a_tenant_boundary(factory, flag_on):
    schedule_id = _create_live(factory)
    _retire_seeded_tick(factory)
    result = _recover(factory, schedule_id, org="org-not-the-owner",
                      now=_FIRST + timedelta(minutes=10))
    assert result["outcome"] == "schedule_missing" and result["chained"] is False
    assert len(_ticks(factory)) == 1


def test_unparseable_payload_is_logged_never_guessed(factory, flag_on, caplog):
    caplog.set_level(logging.INFO)
    _create_live(factory)
    with factory() as s:
        recover_failed_schedule_tick(TerminalFailureContext(
            db=s,
            context=ExecutionContext.for_scout_request(
                organization_id=seed_mod.sid("org"), workspace_id=seed_mod.sid("ws"),
            ),
            payload={"schedule_id": "", "occurrence_at": "not-a-date"},
            job_id="job-x", job_type=TICK, status=JobStatus.FAILED.value,
            error_code=JobErrorCode.VALIDATION, attempt=1, now=_FIRST,
        ))
        s.commit()
    assert len(_ticks(factory)) == 1
    assert _failure_logs(caplog)[0]["outcome"] == "unrecoverable_payload"


def test_a_recreated_schedule_does_not_inherit_its_predecessors_failure(factory, flag_on):
    first_id = _create_live(factory)
    _retire_seeded_tick(factory)
    assert _state(factory, first_id)[1] is not None
    with factory() as s:
        sched.delete_schedule(s, schedule=s.get(ScoutSchedule, first_id))
        s.commit()
    second_id = _create_live(factory)
    state, failure = _state(factory, second_id)
    assert state is ScheduleState.ACTIVE and failure is None


def test_failure_signal_is_absent_for_a_healthy_or_never_run_schedule(factory, flag_on):
    schedule_id = _create_live(factory)
    state, failure = _state(factory, schedule_id)
    assert state is ScheduleState.ACTIVE and failure is None
    with factory() as s:
        dark = sched.create_schedule(
            s, request=s.get(ScoutRequest, _req_id("lagos")), interval="weekly",
            enabled=True, now=_T0,
        )
        s.commit()
        dark_id = dark.id
    # Seeded (flag on) — then drop the tick to emulate a never-started schedule.
    with factory() as s:
        s.query(Job).filter(Job.scout_request_id == _req_id("lagos")).delete()
        s.commit()
    state, failure = _state(factory, dark_id)
    assert state is ScheduleState.ACTIVATION_REQUIRED and failure is None
