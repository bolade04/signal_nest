"""The Scout pause promise, clause by clause (P6 coordination decision D-25, half 1).

"Pause" on a scout request (``POST .../scout-requests/{id}/pause``) promises that the
request does no new work until it is resumed: no manual run, no scheduled run, no
connector fetch and no model call -- while its configuration, history and findings are
kept. The request pause is a different switch from the schedule pause
(``.../schedule/pause``) and from the workspace scheduling capability; those two are
pinned in ``test_scout_schedules.py`` and the capability suites. This module pins the
request-level clauses:

1. a paused request refuses a manual run (HTTP);
2. a paused request gets no scheduled run: the tick is a no-op that keeps the chain
   alive, so resuming the request lets scheduling continue without customer action;
3. a run already queued when the pause lands is skipped by the worker, leaves the
   request paused and records the skip on its job;
4. a run already in flight when the pause lands may finish and store what it produced,
   but never flips the paused request to ``completed``;
5. pause and resume preserve configuration and findings; resume re-arms scheduling.

Adopted from the GEO-CAP lane's specification test (P6-W8-GEO-CAP-01). Harness copied
from ``test_scout_schedules.py``: a throwaway file-backed SQLite with the four-market
demo seed, its own scout runs cleared so every job seen here was made by the code under
test. Service-level tests roll back; the HTTP and worker tests commit through the app's
``get_db`` override / the worker's own sessions and restore the seed afterwards.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings, get_settings
from app.core.enums import ScheduleInterval, ScoutRequestStatus
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.jobs import pipeline
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.service import enqueue_scout_request
from app.jobs.status import JobStatus, JobType
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner

# Importing the app registers every ORM model + job handler, so the worker test can
# resolve the execute handler and the HTTP test has the real routes.
from app.main import app
from app.scouting_requests import schedules as sched
from app.scouting_requests.models import ScoutRequest, ScoutSchedule
from app.tests._auth2_support import live_bearer
from app.tests._llm_offline import offline_llm
from app.tests._seed_support import clear_seeded_runs

API = get_settings().api_prefix
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
PAUSED = ScoutRequestStatus.PAUSED.value
DRAFT = ScoutRequestStatus.DRAFT.value


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def factory(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("scout_pause_promise")
    engine = create_engine(
        f"sqlite:///{tmp / 'pause.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        seed_mod.seed(reset=True)
        clear_seeded_runs(make)
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


@pytest.fixture(autouse=True)
def flag_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "scout_scheduling_enabled", True)


@pytest.fixture
def no_fetch(monkeypatch):
    """Fail the test if the pipeline reaches for its sources: a paused request must
    trigger no fetch, and every model call sits behind the fetch. ``fetch_signals`` is
    the pipeline's one source-selection entry on main since #255/#268."""

    def _refuse(*args, **kwargs):
        raise AssertionError("connector fetch attempted for a paused scout request")

    monkeypatch.setattr(pipeline, "fetch_signals", _refuse)


@pytest.fixture
def committed(factory):
    """For tests that must commit (HTTP, worker): restore the seed afterwards --
    every request back to ``draft`` and no jobs -- so the rollback-only tests and the
    next committing test start from the same state."""
    yield
    with factory() as session:
        for market in ("dallas", "london", "lagos", "nairobi"):
            req = session.get(ScoutRequest, seed_mod.sid("scout", market))
            req.status = DRAFT
        session.query(ScoutSchedule).delete(synchronize_session=False)
        session.commit()
    clear_seeded_runs(factory)


@pytest.fixture
def client(factory, committed):
    def _override_get_db():
        session = factory()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.clear()


def _jobs(s, rid: str, job_type: JobType) -> list[Job]:
    return list(
        s.execute(
            select(Job).where(Job.scout_request_id == rid, Job.job_type == job_type.value)
        ).scalars()
    )


def _execs(s, rid: str) -> list[Job]:
    return _jobs(s, rid, JobType.SCOUT_REQUEST_EXECUTE)


def _ticks(s, rid: str) -> list[Job]:
    return _jobs(s, rid, JobType.SCOUT_SCHEDULE_TICK)


def _naive(dt: datetime) -> datetime:
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def _request(s, market: str = "dallas") -> ScoutRequest:
    return s.get(ScoutRequest, seed_mod.sid("scout", market))


def _scheduled_request(s, market: str = "dallas"):
    req = _request(s, market)
    schedule = sched.create_schedule(s, request=req, interval=ScheduleInterval.DAILY, now=_NOW)
    first = _ticks(s, req.id)[0]
    return req, schedule, first


def _tick(s, schedule: ScoutSchedule, tick: Job) -> dict:
    return sched.run_schedule_tick(
        s, schedule_id=schedule.id, occurrence_at=tick.scheduled_for, now=tick.scheduled_for
    )


def _pause_row(db, rid: str) -> None:
    """Write ``paused`` to the request row without touching the session's ORM
    instance -- what a pause committed from another session looks like to a run that
    already loaded the request."""
    db.execute(
        update(ScoutRequest)
        .where(ScoutRequest.id == rid)
        .values(status=PAUSED)
        .execution_options(synchronize_session=False)
    )


def _context(req: ScoutRequest) -> ExecutionContext:
    return ExecutionContext.for_scout_request(
        organization_id=req.organization_id,
        workspace_id=req.workspace_id,
        location_id=req.location_id,
        request_id=req.id,
    )


# --------------------------------------------------------------------------- #
# Clause 1: a paused request refuses a manual run (HTTP)
# --------------------------------------------------------------------------- #
def test_clause_paused_request_refuses_a_manual_run(client, factory):
    """``POST .../run`` on a paused request is refused with the resume hint, enqueues
    nothing and leaves the request paused; after ``resume`` the same run is accepted
    (positive control, mirroring ``test_lifecycle_positive_on_own_request``)."""
    rid = seed_mod.sid("scout", "nairobi")
    auth = {"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"}
    base = f"{API}/workspaces/{seed_mod.sid('ws')}/scout-requests/{rid}"

    paused = client.post(base + "/pause", headers=auth)
    assert paused.status_code == 200, paused.text
    assert paused.json()["status"] == PAUSED

    refused = client.post(base + "/run", headers=auth)
    assert refused.status_code == 422, refused.text
    error = refused.json()["error"]
    assert error["code"] == "validation_error"
    assert error["message"] == "Resume the request before running it."
    with factory() as s:
        assert s.get(ScoutRequest, rid).status == PAUSED
        assert _execs(s, rid) == []

    resumed = client.post(base + "/resume", headers=auth)
    assert resumed.status_code == 200, resumed.text
    assert resumed.json()["status"] == DRAFT
    ran = client.post(base + "/run", headers=auth)
    assert ran.status_code == 200, ran.text
    assert ran.json()["status"] == ScoutRequestStatus.QUEUED.value
    with factory() as s:
        execs = _execs(s, rid)
        assert [j.id for j in execs] == [ran.json()["stats"]["job_id"]]


# --------------------------------------------------------------------------- #
# Clause 2: a paused request gets no scheduled run; the chain stays alive
# --------------------------------------------------------------------------- #
def test_clause_paused_request_gets_no_scheduled_run(s):
    """Pausing a scout (request status=paused) must stop SCHEDULED scouting too."""
    req, schedule, tick = _scheduled_request(s)
    req.status = PAUSED  # what POST .../pause does
    s.flush()
    out = _tick(s, schedule, tick)
    assert out["run_enqueued"] is False, out
    assert out["outcome"] == "request_paused"
    assert _execs(s, req.id) == []


def test_clause_pausing_request_keeps_its_schedule_enabled_and_reseeded(s):
    """Chosen shape (D-25): the request pause is honoured by the *tick*, not by pausing
    the schedule row. The schedule stays enabled, the tick returns the paused no-op and
    reseeds the next occurrence exactly like a coalesced tick, so resuming the request
    lets the chain continue without the customer touching the schedule.

    The alternative shape -- ``pause_scout_request`` also pausing the schedule (the
    GEO-CAP specification's ``test_clause_pausing_request_pauses_its_schedule``) -- was
    not chosen: it would make a request resume silently leave its schedule paused (or
    require resume to restart the chain), changing schedule semantics.
    """
    req, schedule, tick = _scheduled_request(s)
    req.status = PAUSED
    s.flush()

    out = _tick(s, schedule, tick)

    assert s.get(ScoutSchedule, schedule.id).enabled is True
    assert out == {
        "outcome": sched.REQUEST_PAUSED_OUTCOME,
        "run_enqueued": False,
        "coalesced": False,
        "chained": True,
        "next_run_at": out["next_run_at"],
    }
    assert _execs(s, req.id) == []
    successors = [t for t in _ticks(s, req.id) if t.id != tick.id]
    assert len(successors) == 1
    expected_next = sched.next_future_occurrence(
        tick.scheduled_for, schedule.interval_enum(), tick.scheduled_for
    )
    assert _naive(successors[0].scheduled_for) == _naive(expected_next)
    assert out["next_run_at"] == expected_next.isoformat()
    assert _naive(schedule.next_run_at) == _naive(expected_next)
    assert _naive(schedule.last_tick_at) == _naive(tick.scheduled_for)


def test_clause_resume_after_pause_lets_the_next_tick_enqueue_again(s):
    """Resume (status back to ``draft``) re-arms scheduling: the successor tick the
    paused no-op seeded fans out a scheduled run as usual."""
    req, schedule, tick = _scheduled_request(s)
    req.status = PAUSED
    s.flush()
    first = _tick(s, schedule, tick)
    assert first["outcome"] == sched.REQUEST_PAUSED_OUTCOME and first["chained"] is True
    assert _execs(s, req.id) == []

    req.status = DRAFT  # what POST .../resume does
    s.flush()
    successor = max(_ticks(s, req.id), key=lambda t: _naive(t.scheduled_for))
    assert successor.id != tick.id
    second = _tick(s, schedule, successor)

    assert second["outcome"] == "fanned_out" and second["run_enqueued"] is True
    execs = _execs(s, req.id)
    assert len(execs) == 1
    assert execs[0].payload.get("trigger") == sched.SCHEDULED_TRIGGER
    assert execs[0].status in (JobStatus.PENDING.value, JobStatus.SCHEDULED.value)


# --------------------------------------------------------------------------- #
# Clause 3: a run already queued when the pause lands is skipped, not executed
# --------------------------------------------------------------------------- #
def test_clause_paused_request_is_not_executed_by_an_already_queued_job(s, no_fetch):
    """A run queued before the pause must not flip a paused request back to running:
    the execute path refuses before any connector fetch or model call, leaves the
    status and ``last_run_at`` alone and reports the skip."""
    req = _request(s)
    req.status = PAUSED
    s.flush()
    last_run_before = req.last_run_at

    with offline_llm():
        result = pipeline._run(s, req.id, _context(req))

    assert result == {"scout_request_id": req.id, "outcome": pipeline.SKIPPED_REQUEST_PAUSED}
    assert s.get(ScoutRequest, req.id).status == PAUSED
    assert req.last_run_at == last_run_before


def test_clause_pause_landing_between_the_load_and_the_claim_is_not_overwritten(
    s, monkeypatch, no_fetch
):
    """The entry guard's window (review r1 F4): the run loaded the request as
    ``draft`` and the customer's pause commits before the run claims ``running``.
    The claim is a compare-and-set, so it matches no row, the request stays paused,
    nothing is fetched and the job records the skip."""
    req = _request(s)
    real_scope_matches = pipeline.scope_matches

    def pause_then_match(*args, **kwargs):
        # Called between the request load and the running claim; the loaded instance
        # still says ``draft``.
        assert req.status == DRAFT
        _pause_row(s, req.id)
        return real_scope_matches(*args, **kwargs)

    monkeypatch.setattr(pipeline, "scope_matches", pause_then_match)
    with offline_llm():
        result = pipeline._run(s, req.id, _context(req))

    assert result == {"scout_request_id": req.id, "outcome": "skipped_request_paused"}
    s.expire_all()
    assert s.get(ScoutRequest, req.id).status == PAUSED


def test_clause_queued_run_for_a_paused_request_finishes_skipped_in_its_job_record(
    factory, committed, no_fetch
):
    """Through the real worker: the queued ``scout_request.execute`` job of a paused
    request completes as a skip -- the job succeeds (no retry, no dead letter) with the
    skip recorded in its ``result_summary`` -- and the request is still paused."""
    rid = seed_mod.sid("scout", "london")
    with factory() as session:
        req = session.get(ScoutRequest, rid)
        req.status = PAUSED
        job = enqueue_scout_request(
            session,
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=rid,
            location_id=req.location_id,
            request_id=rid,
            now=_NOW,
        )
        session.commit()
        job_id = job.id

    runner = JobRunner(
        settings=Settings(_env_file=None),
        session_factory=factory,
        store=DurableJobStore(),
        clock=lambda: _NOW + timedelta(minutes=1),
    )
    with offline_llm():
        assert runner.poll_once(worker_id="w-pause") is True
        assert runner.poll_once(worker_id="w-pause") is False

    with factory() as session:
        job = session.get(Job, job_id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.result_summary == {"scout_request_id": rid, "outcome": "skipped_request_paused"}
        assert session.get(ScoutRequest, rid).status == PAUSED


# --------------------------------------------------------------------------- #
# Clause 4: a run already in flight when the pause lands never un-pauses it
# --------------------------------------------------------------------------- #
def test_clause_pause_landing_mid_run_is_not_flipped_to_completed(s, monkeypatch):
    """The run had already set ``running`` when the customer paused. The run may
    finish and keep its findings and stats, but its terminal write must not overwrite
    the pause: the request ends ``paused``, not ``completed``."""
    req = _request(s)
    real_build = pipeline._build_opportunities

    def pause_then_build(db, request, *args, **kwargs):
        assert request.status == ScoutRequestStatus.RUNNING.value
        # The customer's pause lands while the run is in flight. It is written to the
        # row the way another session's commit would be seen: the run's own ORM
        # instance still says ``running``, so only a fresh re-read of the row can
        # observe it (review r1 F1).
        _pause_row(db, request.id)
        assert request.status == ScoutRequestStatus.RUNNING.value
        return real_build(db, request, *args, **kwargs)

    monkeypatch.setattr(pipeline, "_build_opportunities", pause_then_build)
    with offline_llm():
        result = pipeline._run(s, req.id, _context(req))

    s.expire_all()
    assert s.get(ScoutRequest, req.id).status == PAUSED
    assert result["outcome"] == pipeline.PAUSED_DURING_RUN
    # What the run produced is kept: stats and the run timestamp are stored.
    assert req.stats["scanned"] == result["scanned"] > 0
    assert req.last_run_at is not None


def test_clause_uninterrupted_run_still_completes(s):
    """Control for the guard: a run nobody paused settles as ``completed``."""
    req = _request(s)
    with offline_llm():
        result = pipeline._run(s, req.id, _context(req))
    assert s.get(ScoutRequest, req.id).status == ScoutRequestStatus.COMPLETED.value
    assert result["outcome"] == "completed"
    assert result["scanned"] > 0


# --------------------------------------------------------------------------- #
# The outcome literals are a contract (runbook, log and job-record consumers)
# --------------------------------------------------------------------------- #
def test_outcome_literals_are_the_documented_contract():
    """The runbook and anything reading ``result_summary.outcome`` or the
    ``scout_schedule_tick`` event depend on these exact strings, not on the symbol
    names (review r1 F2)."""
    assert sched.REQUEST_PAUSED_OUTCOME == "request_paused"
    assert pipeline.SKIPPED_REQUEST_PAUSED == "skipped_request_paused"
    assert pipeline.PAUSED_DURING_RUN == "paused_during_run"
    assert pipeline.RUN_COMPLETED == "completed"


# --------------------------------------------------------------------------- #
# Clause 5: pause and resume lose nothing
# --------------------------------------------------------------------------- #
def test_clause_pause_and_resume_preserve_findings_and_configuration(s):
    """Pause/resume (route logic is status-only) loses no opportunities/signals/config."""
    from app.opportunities.models import Opportunity

    req = _request(s)
    before_cfg = (req.name, list(req.keywords or []), list(req.source_types or []), req.location_id)
    before_opps = s.scalar(select(func.count()).select_from(Opportunity))
    req.status = PAUSED
    s.flush()
    req.status = DRAFT  # resume
    s.flush()
    assert (req.name, list(req.keywords or []), list(req.source_types or []), req.location_id) == (
        before_cfg
    )
    assert s.scalar(select(func.count()).select_from(Opportunity)) == before_opps


def test_clause_schedule_pause_then_resume_keeps_row_and_reseeds(s):
    req, schedule, _ = _scheduled_request(s)
    sid_ = schedule.id
    sched.pause_schedule(s, schedule=schedule)
    sched.resume_schedule(s, schedule=schedule, now=_NOW)
    assert s.get(ScoutSchedule, sid_).enabled is True
    assert len(_ticks(s, req.id)) >= 2 or sched.derive_schedule_state(s, schedule).value == "active"
