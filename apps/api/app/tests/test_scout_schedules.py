"""SB-B: recurrence math, schedule service, tick handler and SB-A regression tests
for the dark-deployed scouting-schedule foundation.

Everything here exercises the *backend* seam directly (no HTTP): the schedule
service and the ``scout_schedule.tick`` durable handler. Tests run against a
throwaway four-market demo seed (Dallas, London, Lagos, Nairobi). The seed creates
**no** durable ``Job`` rows, so any job observed here was enqueued by the code under
test, letting the suite assert exactly what fan-out did (or, while dark, did not)
happen.

Sessions never commit: each test rolls back, so schedules/jobs created by one test
never leak into another and the per-request / per-workspace limits start clean.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.enums import ScheduleInterval
from app.core.errors import ConflictError, ValidationDomainError
from app.db import seed as seed_mod
from app.db.models import Base
from app.jobs.models import Job
from app.jobs.status import JobStatus, JobType
from app.scouting_requests import schedules as sched
from app.scouting_requests.models import ScoutRequest, ScoutSchedule
from app.scouting_requests.run_history import get_run_history

_MARKETS = ("dallas", "london", "lagos", "nairobi")
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)


def _naive(dt: datetime | None) -> datetime | None:
    """Normalise to tz-naive UTC. Durable ``Job`` datetime columns read back naive
    on SQLite, while the ``ScoutSchedule`` object keeps its tz-aware Python value;
    both denote the same UTC instant, so compare on the naive form."""
    if dt is None:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scout_schedules")
    engine = create_engine(
        f"sqlite:///{tmp/'sched.db'}",
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


@pytest.fixture
def s(factory):
    """A session whose writes are always rolled back (test isolation)."""
    session = factory()
    try:
        yield session
    finally:
        session.rollback()
        session.close()


@pytest.fixture
def flag_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", True)


@pytest.fixture(autouse=True)
def flag_off_by_default(monkeypatch):
    # The feature is dark unless a test opts in via ``flag_on``.
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", False)


def _request(s, market_key: str) -> ScoutRequest:
    return s.get(ScoutRequest, seed_mod.sid("scout", market_key))


def _extra_request(s, name: str) -> ScoutRequest:
    """A fresh scout request in the same workspace (for per-workspace limit tests)."""
    template = _request(s, "dallas")
    req = ScoutRequest(
        organization_id=template.organization_id,
        workspace_id=template.workspace_id,
        brand_id=template.brand_id,
        location_id=None,
        name=name,
        status="draft",
        source_types=[],
        keywords=[],
        stats={},
    )
    s.add(req)
    s.flush()
    return req


def _jobs(s, request_id: str, job_type: JobType) -> list[Job]:
    return list(
        s.execute(
            select(Job).where(
                Job.scout_request_id == request_id,
                Job.job_type == job_type.value,
            )
        ).scalars()
    )


def _mk(s, market_key: str, *, interval=ScheduleInterval.DAILY, **kw):
    """Create a schedule for a seeded market at ``_NOW`` (test convenience)."""
    return sched.create_schedule(
        s, request=_request(s, market_key), interval=interval, now=_NOW, **kw
    )


def _fire(s, schedule, occurrence, now):
    """Process one tick for ``schedule`` at ``occurrence``, observed at ``now``."""
    return sched.run_schedule_tick(
        s, schedule_id=schedule.id, occurrence_at=occurrence, now=now
    )


def _active(s, req) -> bool:
    return sched.has_active_execution(
        s,
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        scout_request_id=req.id,
    )


def _execs(s, request_id: str) -> list[Job]:
    return _jobs(s, request_id, JobType.SCOUT_REQUEST_EXECUTE)


def _ticks(s, request_id: str) -> list[Job]:
    return _jobs(s, request_id, JobType.SCOUT_SCHEDULE_TICK)


# --------------------------------------------------------------------------- #
# Pure recurrence math (no db)
# --------------------------------------------------------------------------- #
class TestRecurrence:
    def test_interval_deltas(self):
        assert sched.interval_delta(ScheduleInterval.DAILY) == timedelta(hours=24)
        assert sched.interval_delta(ScheduleInterval.WEEKLY) == timedelta(days=7)

    def test_next_occurrence_is_one_interval_out_when_future(self):
        nxt = sched.next_future_occurrence(_NOW, ScheduleInterval.DAILY, _NOW)
        assert nxt == _NOW + timedelta(days=1)

    def test_weekly_next_occurrence(self):
        nxt = sched.next_future_occurrence(_NOW, ScheduleInterval.WEEKLY, _NOW)
        assert nxt == _NOW + timedelta(days=7)

    def test_missed_occurrences_are_skipped_not_backfilled(self):
        # Base three-and-a-half days ago on a daily cadence: the next boundary is the
        # first one strictly in the future — intermediate ones are skipped, not run.
        base = _NOW - timedelta(days=3, hours=12)
        nxt = sched.next_future_occurrence(base, ScheduleInterval.DAILY, _NOW)
        assert nxt > _NOW
        assert nxt == base + timedelta(days=4)

    def test_next_occurrence_always_strictly_future(self):
        base = _NOW - timedelta(days=100)
        nxt = sched.next_future_occurrence(base, ScheduleInterval.WEEKLY, _NOW)
        assert nxt > _NOW


# --------------------------------------------------------------------------- #
# Service lifecycle + limits
# --------------------------------------------------------------------------- #
class TestServiceLifecycle:
    def test_create_sets_next_run_and_audit(self, s):
        req = _request(s, "dallas")
        schedule = sched.create_schedule(
            s,
            request=req,
            interval=ScheduleInterval.DAILY,
            actor_user_id=seed_mod.sid("user"),
            now=_NOW,
        )
        assert schedule.enabled is True
        assert schedule.next_run_at == _NOW + timedelta(days=1)
        assert schedule.location_id == req.location_id
        assert schedule.organization_id == req.organization_id

    def test_one_schedule_per_request(self, s):
        req = _request(s, "london")
        sched.create_schedule(s, request=req, interval=ScheduleInterval.DAILY, now=_NOW)
        with pytest.raises(ConflictError):
            sched.create_schedule(s, request=req, interval=ScheduleInterval.WEEKLY, now=_NOW)

    def test_invalid_interval_rejected(self, s):
        req = _request(s, "lagos")
        with pytest.raises(ValidationDomainError):
            sched.create_schedule(s, request=req, interval="hourly", now=_NOW)

    def test_workspace_active_limit_of_four(self, s):
        for key in _MARKETS:  # four enabled schedules — at the cap
            _mk(s, key)
        fifth = _extra_request(s, "fifth scout")
        with pytest.raises(ConflictError):
            sched.create_schedule(s, request=fifth, interval=ScheduleInterval.DAILY, now=_NOW)

    def test_disabled_create_does_not_count_toward_limit(self, s):
        # A disabled schedule is inert and must not consume an active slot.
        for key in _MARKETS:
            _mk(s, key, enabled=False)
        fifth = _extra_request(s, "fifth scout")
        schedule = sched.create_schedule(
            s, request=fifth, interval=ScheduleInterval.DAILY, now=_NOW
        )
        assert schedule.enabled is True

    def test_pause_is_inert(self, s):
        schedule = _mk(s, "dallas")
        sched.pause_schedule(s, schedule=schedule)
        assert schedule.enabled is False
        assert schedule.next_run_at is None

    def test_resume_recomputes_from_now(self, s):
        schedule = _mk(s, "dallas", interval=ScheduleInterval.WEEKLY)
        sched.pause_schedule(s, schedule=schedule)
        later = _NOW + timedelta(days=10)
        sched.resume_schedule(s, schedule=schedule, now=later)
        assert schedule.enabled is True
        assert schedule.next_run_at == later + timedelta(days=7)

    def test_delete_is_hard_delete(self, s):
        schedule = _mk(s, "nairobi")
        sid = schedule.id
        sched.delete_schedule(s, schedule=schedule)
        assert s.get(ScoutSchedule, sid) is None


# --------------------------------------------------------------------------- #
# Feature-flag gating of enqueue (dark by default)
# --------------------------------------------------------------------------- #
class TestFeatureFlag:
    def test_create_while_dark_enqueues_no_tick(self, s):
        req = _request(s, "dallas")
        _mk(s, "dallas")
        assert _ticks(s, req.id) == []

    def test_create_while_live_enqueues_first_tick(self, s, flag_on):
        req = _request(s, "dallas")
        schedule = _mk(s, "dallas")
        ticks = _ticks(s, req.id)
        assert len(ticks) == 1
        tick = ticks[0]
        assert _naive(tick.scheduled_for) == _naive(schedule.next_run_at)
        assert tick.status == JobStatus.SCHEDULED.value
        expected_key = f"schedule-tick:{schedule.id}:{schedule.next_run_at.isoformat()}"
        assert tick.idempotency_key == expected_key

    def test_resume_while_dark_enqueues_no_tick(self, s):
        schedule = _mk(s, "dallas")
        sched.pause_schedule(s, schedule=schedule)
        sched.resume_schedule(s, schedule=schedule, now=_NOW)
        assert _ticks(s, schedule.scout_request_id) == []


# --------------------------------------------------------------------------- #
# Tick fan-out (run_schedule_tick) — the durable-handler core
# --------------------------------------------------------------------------- #
class TestTickFanOut:
    def _armed(self, s):
        """A live, enabled schedule whose first tick is due now."""
        req = _request(s, "dallas")
        schedule = sched.create_schedule(s, request=req, interval=ScheduleInterval.DAILY, now=_NOW)
        return req, schedule

    def test_fans_out_one_run_and_chains(self, s, flag_on):
        req, schedule = self._armed(s)
        occurrence = schedule.next_run_at
        fire = occurrence + timedelta(seconds=1)
        result = sched.run_schedule_tick(
            s, schedule_id=schedule.id, occurrence_at=occurrence, now=fire
        )
        assert result["outcome"] == "fanned_out"
        runs = _execs(s, req.id)
        assert len(runs) == 1
        assert runs[0].payload.get("trigger") == "scheduled"
        assert runs[0].idempotency_key == f"schedule:{schedule.id}:{occurrence.isoformat()}"
        # Self-chained: exactly one successor tick, one interval past the occurrence.
        ticks = [
            t for t in _ticks(s, req.id)
            if _naive(t.scheduled_for) == _naive(occurrence + timedelta(days=1))
        ]
        assert len(ticks) == 1
        assert schedule.last_tick_at == fire
        assert schedule.next_run_at == occurrence + timedelta(days=1)

    def test_dark_tick_is_inert_and_stops_chain(self, s, flag_on):
        req, schedule = self._armed(s)
        occurrence = schedule.next_run_at
        # First tick was seeded while live; now go dark and fire it.
        get_settings().scout_scheduling_enabled = False
        before_ticks = len(_ticks(s, req.id))
        result = sched.run_schedule_tick(
            s, schedule_id=schedule.id, occurrence_at=occurrence, now=occurrence
        )
        assert result == {"outcome": "feature_disabled", "run_enqueued": False, "chained": False}
        assert _execs(s, req.id) == []
        assert len(_ticks(s, req.id)) == before_ticks  # no successor

    def test_disabled_schedule_tick_is_noop(self, s, flag_on):
        req, schedule = self._armed(s)
        occurrence = schedule.next_run_at
        sched.pause_schedule(s, schedule=schedule)
        result = sched.run_schedule_tick(
            s, schedule_id=schedule.id, occurrence_at=occurrence, now=occurrence
        )
        assert result["outcome"] == "disabled"
        assert _execs(s, req.id) == []

    def test_missing_schedule_tick_is_noop(self, s, flag_on):
        result = sched.run_schedule_tick(
            s, schedule_id="does-not-exist", occurrence_at=_NOW, now=_NOW
        )
        assert result["outcome"] == "schedule_missing"

    def test_overlap_coalesces_but_still_chains(self, s, flag_on):
        req, schedule = self._armed(s)
        occurrence = schedule.next_run_at
        # A run is already in flight for this request.
        from app.jobs.service import enqueue_scout_request

        inflight = enqueue_scout_request(
            s,
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=req.id,
            location_id=req.location_id,
        )
        inflight.status = JobStatus.RUNNING.value
        s.flush()
        result = sched.run_schedule_tick(
            s, schedule_id=schedule.id, occurrence_at=occurrence, now=occurrence
        )
        assert result["coalesced"] is True and result["run_enqueued"] is False
        assert result["chained"] is True
        # Only the pre-existing in-flight run exists; no second run was piled on.
        assert len(_execs(s, req.id)) == 1
        assert schedule.next_run_at == occurrence + timedelta(days=1)

    def test_missed_tick_skips_to_future_without_backfill(self, s, flag_on):
        req, schedule = self._armed(s)
        occurrence = schedule.next_run_at
        # Fire the tick a week late: the next occurrence must be strictly future and
        # only one successor tick is enqueued (no catch-up storm).
        late = occurrence + timedelta(days=7, hours=3)
        sched.run_schedule_tick(
            s, schedule_id=schedule.id, occurrence_at=occurrence, now=late
        )
        assert schedule.next_run_at > late
        future_ticks = [
            t for t in _ticks(s, req.id)
            if _naive(t.scheduled_for) == _naive(schedule.next_run_at)
        ]
        assert len(future_ticks) == 1

    def test_tick_run_enqueue_is_idempotent(self, s, flag_on):
        req, schedule = self._armed(s)
        occurrence = schedule.next_run_at
        # First fire enqueues the run; settle it so overlap does not mask idempotency.
        _fire(s, schedule, occurrence, occurrence)
        run = _execs(s, req.id)[0]
        run.status = JobStatus.SUCCEEDED.value
        s.flush()
        # A retried tick for the same occurrence must not create a second run.
        _fire(s, schedule, occurrence, occurrence)
        assert len(_execs(s, req.id)) == 1


# --------------------------------------------------------------------------- #
# SB-A run-history regression: ticks never appear as runs; scheduled runs do
# --------------------------------------------------------------------------- #
class TestRunHistoryRegression:
    def test_tick_jobs_never_appear_in_run_history(self, s, flag_on):
        req = _request(s, "dallas")
        schedule = _mk(s, "dallas")
        occurrence = schedule.next_run_at
        _fire(s, schedule, occurrence, occurrence)
        s.flush()

        history = get_run_history(
            s,
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=req.id,
        )
        # Exactly the one scheduled execution run — never any tick bookkeeping job.
        assert history.total == 1
        assert history.items[0].trigger == "scheduled"
        tick_ids = {t.id for t in _ticks(s, req.id)}
        assert tick_ids and all(item.id not in tick_ids for item in history.items)


# --------------------------------------------------------------------------- #
# Four-market isolation
# --------------------------------------------------------------------------- #
class TestIsolation:
    def test_schedules_and_runs_are_market_scoped(self, s, flag_on):
        schedules = {key: _mk(s, key) for key in _MARKETS}
        # Fire only Dallas.
        d = schedules["dallas"]
        _fire(s, d, d.next_run_at, d.next_run_at)
        # Dallas got a run; the other markets did not.
        assert len(_execs(s, _request(s, "dallas").id)) == 1
        for key in ("london", "lagos", "nairobi"):
            assert _execs(s, _request(s, key).id) == []

    def test_overlap_check_is_request_scoped(self, s, flag_on):
        dallas = _request(s, "dallas")
        london = _request(s, "london")
        from app.jobs.service import enqueue_scout_request

        job = enqueue_scout_request(
            s,
            organization_id=dallas.organization_id,
            workspace_id=dallas.workspace_id,
            scout_request_id=dallas.id,
            location_id=dallas.location_id,
        )
        job.status = JobStatus.RUNNING.value
        s.flush()
        assert _active(s, dallas) is True
        # London shares the workspace but must not see Dallas's in-flight run.
        assert _active(s, london) is False
