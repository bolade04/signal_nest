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
   but its terminal write never overwrites the pause: not the ``completed`` write, not the
   #268 refusal's ``failed`` write, not the terminal-failure hook's ``failed`` write;
5. pause and resume preserve configuration and findings; resume re-arms scheduling.

A skipped run makes no connector fetch and no model call, and writes nothing to the
#269 LLM ledger (``llm_budget_reservations``, ``llm_usage_events``,
``llm_budget_periods``): asserted on the tables, with an unpaused run as the control
that the same assertion sees the rows a real run writes. Resume is pinned as it is
implemented today (the request lands in ``draft``; a run still queued then executes);
those are D-25-a / D-25-b behaviours, pinned here, not decided here. The PostgreSQL
behaviour (row locks, separate sessions, already-dispatched work) is in
``test_scout_pause_postgres.py``.

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

from app.connectors.coverage import Coverage, CoverageReason
from app.core.config import Settings, get_settings
from app.core.enums import ScheduleInterval, ScoutRequestStatus
from app.db import seed as seed_mod
from app.db.models import Base
from app.db.session import get_db
from app.infra.vector import SIGNAL_VOLUME_ABOVE_BOUND, SignalVolumeAboveBoundError
from app.jobs import handlers, pipeline
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.registry import TerminalFailureContext
from app.jobs.service import enqueue_scout_request
from app.jobs.status import JobErrorCode, JobStatus, JobType
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.llm.mock import MockLLMProvider
from app.llm.models import LLMBudgetPeriod, LLMBudgetReservation, LLMUsageEvent

# Importing the app registers every ORM model + job handler, so the worker test can
# resolve the execute handler and the HTTP test has the real routes.
from app.main import app
from app.opportunities.models import Opportunity
from app.scouting_requests import schedules as sched
from app.scouting_requests.connectors import FixtureConnector, SignalBatch, fetch_with_reports
from app.scouting_requests.models import ScoutRequest, ScoutSchedule
from app.signals.models import NormalizedSignal, RawSignal
from app.tests._auth2_support import live_bearer
from app.tests._llm_offline import offline_llm
from app.tests._seed_support import clear_seeded_runs

API = get_settings().api_prefix
_NOW = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
PAUSED = ScoutRequestStatus.PAUSED.value
DRAFT = ScoutRequestStatus.DRAFT.value
QUEUED = ScoutRequestStatus.QUEUED.value
RUNNING = ScoutRequestStatus.RUNNING.value
COMPLETED = ScoutRequestStatus.COMPLETED.value
FAILED = ScoutRequestStatus.FAILED.value
_MARKETS = ("dallas", "london", "lagos", "nairobi")


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
    """Fail the test if the pipeline reaches for its sources, or for a model: a paused
    request must trigger no connector fetch and no model call. ``fetch_signals`` is the
    pipeline's one source-selection entry since #255/#268 (live feeds, sandbox feed,
    demo library); every model call of a run sits behind it, and the mock provider is
    refused too so that a regression cannot hide behind the fetch."""

    def _refuse_fetch(*args, **kwargs):
        raise AssertionError("connector fetch attempted for a paused scout request")

    def _refuse_model(*args, **kwargs):
        raise AssertionError("model call attempted for a paused scout request")

    monkeypatch.setattr(pipeline, "fetch_signals", _refuse_fetch)
    monkeypatch.setattr(MockLLMProvider, "generate", _refuse_model)


@pytest.fixture
def counted(monkeypatch):
    """Count the run's source fetches and model calls (mock provider attempts) without
    changing them."""
    calls = {"fetch": 0, "model": 0}
    real_fetch = pipeline.fetch_signals
    real_generate = MockLLMProvider.generate

    def _fetch(*args, **kwargs):
        calls["fetch"] += 1
        return real_fetch(*args, **kwargs)

    def _generate(self, *args, **kwargs):
        calls["model"] += 1
        return real_generate(self, *args, **kwargs)

    monkeypatch.setattr(pipeline, "fetch_signals", _fetch)
    monkeypatch.setattr(MockLLMProvider, "generate", _generate)
    return calls


@pytest.fixture
def committed(factory):
    """For tests that must commit (HTTP, worker): restore the seed afterwards --
    every request back to its seeded status, stats and run timestamp, and no jobs --
    so the rollback-only tests and the next committing test start from the same state."""
    with factory() as session:
        seeded = {
            m: (r.status, r.stats, r.last_run_at)
            for m in _MARKETS
            for r in [session.get(ScoutRequest, seed_mod.sid("scout", m))]
        }
    yield
    with factory() as session:
        for market in _MARKETS:
            req = session.get(ScoutRequest, seed_mod.sid("scout", market))
            req.status = DRAFT
            _status, req.stats, req.last_run_at = seeded[market]
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


def _ledger(s, workspace_id: str) -> dict:
    """The workspace's #269 LLM ledger: row counts and amounts of all three tables."""

    def count(model) -> int:
        return s.scalar(
            select(func.count()).select_from(model).where(model.workspace_id == workspace_id)
        )

    def total(column, model) -> float:
        return float(
            s.scalar(
                select(func.coalesce(func.sum(column), 0)).where(model.workspace_id == workspace_id)
            )
        )

    return {
        "reservations": count(LLMBudgetReservation),
        "usage_events": count(LLMUsageEvent),
        "periods": count(LLMBudgetPeriod),
        "reserved_usd": total(LLMBudgetPeriod.reserved_usd, LLMBudgetPeriod),
        "settled_usd": total(LLMBudgetPeriod.settled_usd, LLMBudgetPeriod),
        "usage_cost_usd": total(LLMUsageEvent.cost_usd, LLMUsageEvent),
    }


def _findings(s, rid: str) -> dict:
    """What a request's completed runs left behind."""

    def count(model) -> int:
        return s.scalar(
            select(func.count()).select_from(model).where(model.scout_request_id == rid)
        )

    return {
        "opportunities": count(Opportunity),
        "normalized_signals": count(NormalizedSignal),
        "raw_signals": count(RawSignal),
    }


def _runner(factory, clock=None) -> JobRunner:
    kwargs = {"clock": clock} if clock is not None else {}
    return JobRunner(
        settings=Settings(_env_file=None),
        session_factory=factory,
        store=DurableJobStore(),
        **kwargs,
    )


def _queue(factory, rid: str, *, max_attempts: int | None = None) -> str:
    """Put a request in ``queued`` with one execute job, as ``POST .../run`` does."""
    with factory() as session:
        req = session.get(ScoutRequest, rid)
        req.status = QUEUED
        job = enqueue_scout_request(
            session,
            organization_id=req.organization_id,
            workspace_id=req.workspace_id,
            scout_request_id=rid,
            location_id=req.location_id,
            request_id=rid,
            now=_NOW,
        )
        if max_attempts is not None:
            job.max_attempts = max_attempts
        session.commit()
        return job.id


def _pause_after_the_attempt_rolls_back(factory, monkeypatch, rid: str) -> None:
    """Commit the customer's pause from another session right after the worker rolls the
    attempt back and before its after-rollback writes and failure bookkeeping run.

    That is the ordering PostgreSQL produces for an HTTP pause issued mid-run: the pause
    waits behind the run's row lock and commits as soon as the rollback releases it
    (measured in ``test_scout_pause_postgres.py``). SQLite has one writer, so the pause
    cannot be issued while the attempt holds the lock; it is committed at that point."""
    real = JobRunner._write_after_rollback

    def pause_then_write(self, db, writers, *, job_type):
        with factory() as other:
            other.get(ScoutRequest, rid).status = PAUSED
            other.commit()
        return real(self, db, writers, job_type=job_type)

    monkeypatch.setattr(JobRunner, "_write_after_rollback", pause_then_write)


def _fixture_fetch(count: int | None = None, fail: Exception | None = None):
    """A stand-in for ``pipeline.fetch_signals``: the demo library's signals for the
    request's market (optionally exactly ``count`` of them, cycling), reported like any
    connector that does not report itself, or ``fail`` raised in place of a fetch."""

    def fetch(*, market, keywords, source_types, rss_enabled, takedowns=frozenset(), settings=None):
        if fail is not None:
            raise fail
        signals = FixtureConnector().fetch(market=market, keywords=[], source_types=[])
        if count is not None:
            signals = [signals[i % len(signals)] for i in range(count)]

        class _Stub:
            name = "stub"

            def fetch(self, **_kw):
                return signals

        outcome = fetch_with_reports(
            _Stub(), market=market, keywords=list(keywords), source_types=list(source_types)
        )
        return SignalBatch(
            signals=outcome.signals,
            coverage=Coverage(CoverageReason.FIXTURE_LIBRARY, is_simulated=True),
            reports=outcome.reports,
        )

    return fetch


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

    runner = _runner(factory, clock=lambda: _NOW + timedelta(minutes=1))
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


# --------------------------------------------------------------------------- #
# (a) + (c) through the product surface: a pause before the worker claims
# --------------------------------------------------------------------------- #
def test_clause_http_pause_before_the_worker_claims_makes_no_fetch_model_call_or_ledger_row(
    client, factory, counted
):
    """``POST .../run`` queues a job, ``POST .../pause`` lands before any worker claims
    it, then the real worker runs the job: it is skipped (job ``succeeded`` with the
    skip recorded), no source is fetched, no model is called, and the #269 ledger gains
    no reservation row, no usage row, no period row and no amount.

    Control in the same test: after ``resume`` and a new run, the same worker executes
    the request, fetches, calls the model and writes ledger rows -- so the unchanged
    ledger above is a real observation, not a blind assertion."""
    rid = seed_mod.sid("scout", "lagos")
    ws = seed_mod.sid("ws")
    auth = {"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"}
    base = f"{API}/workspaces/{ws}/scout-requests/{rid}"

    ran = client.post(base + "/run", headers=auth)
    assert ran.status_code == 200, ran.text
    job_id = ran.json()["stats"]["job_id"]
    paused = client.post(base + "/pause", headers=auth)
    assert paused.status_code == 200 and paused.json()["status"] == PAUSED
    with factory() as s:
        before = _ledger(s, ws)
        findings_before = _findings(s, rid)

    with offline_llm():
        assert _runner(factory).poll_once(worker_id="w-pause-http") is True

    assert counted == {"fetch": 0, "model": 0}
    with factory() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.result_summary == {"scout_request_id": rid, "outcome": "skipped_request_paused"}
        assert s.get(ScoutRequest, rid).status == PAUSED
        assert _ledger(s, ws) == before
        assert _findings(s, rid) == findings_before

    # Control: resume (lands in draft today) and run again -> real work, real ledger rows.
    resumed = client.post(base + "/resume", headers=auth)
    assert resumed.status_code == 200 and resumed.json()["status"] == DRAFT
    rerun = client.post(base + "/run", headers=auth)
    assert rerun.status_code == 200, rerun.text
    with offline_llm():
        assert _runner(factory).poll_once(worker_id="w-pause-http") is True
    assert counted["fetch"] == 1 and counted["model"] > 0
    with factory() as s:
        assert s.get(ScoutRequest, rid).status == COMPLETED
        after = _ledger(s, ws)
        # Same-session path (SQLite, mock provider): one usage row and one settled
        # reservation row per model attempt, written with the run.
        assert after["usage_events"] == before["usage_events"] + counted["model"]
        assert after["reservations"] == before["reservations"] + counted["model"]
        assert after["periods"] >= 1


# --------------------------------------------------------------------------- #
# (d) the pause survives every terminal settlement of a run
# --------------------------------------------------------------------------- #
def test_clause_refused_run_keeps_a_pause_that_landed_while_it_ran(
    factory, committed, counted, monkeypatch
):
    """#268 refusal path through the real worker. The run is refused at the per-run
    bound; the customer's pause lands while it runs (committed as soon as the attempt
    rolls back, the PostgreSQL ordering). The refusal's after-rollback writer must not
    overwrite it: the request stays ``paused`` and carries the refusal's stats, the
    job fails non-retryably with the stable reason code, and no model was called (the
    refusal precedes every model call)."""
    rid = seed_mod.sid("scout", "london")
    monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", 2)
    monkeypatch.setattr(pipeline, "fetch_signals", _fixture_fetch(count=3))
    job_id = _queue(factory, rid)
    _pause_after_the_attempt_rolls_back(factory, monkeypatch, rid)

    with offline_llm():
        assert _runner(factory, clock=lambda: _NOW + timedelta(minutes=1)).poll_once(
            worker_id="w-refused"
        )

    assert counted["model"] == 0
    with factory() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.FAILED.value
        assert job.last_error_code == JobErrorCode.NON_RETRYABLE.value
        assert job.last_error_summary == SIGNAL_VOLUME_ABOVE_BOUND
        req = s.get(ScoutRequest, rid)
        assert req.status == PAUSED
        assert req.stats["refused"] is True
        assert req.stats["refusal_reason"] == SIGNAL_VOLUME_ABOVE_BOUND
        assert (req.stats["scanned"], req.stats["bound"]) == (3, 2)


def test_clause_failed_run_keeps_a_pause_that_landed_while_it_ran(factory, committed, monkeypatch):
    """Failure path through the real worker: the run's last attempt fails, the pause
    lands as the attempt rolls back, and the terminal-failure hook
    (``settle_failed_scout_request``) leaves the paused request alone."""
    rid = seed_mod.sid("scout", "nairobi")
    monkeypatch.setattr(pipeline, "fetch_signals", _fixture_fetch(fail=RuntimeError("source down")))
    job_id = _queue(factory, rid, max_attempts=1)
    _pause_after_the_attempt_rolls_back(factory, monkeypatch, rid)

    with offline_llm():
        assert _runner(factory, clock=lambda: _NOW + timedelta(minutes=1)).poll_once(
            worker_id="w-failed"
        )

    with factory() as s:
        job = s.get(Job, job_id)
        # Retryable error, no attempt left: the final failure that runs the hook.
        assert job.status == JobStatus.DEAD_LETTERED.value
        assert job.last_error_code == JobErrorCode.TRANSIENT.value
        assert s.get(ScoutRequest, rid).status == PAUSED


def test_clause_failed_run_without_a_pause_is_still_settled_failed(factory, committed, monkeypatch):
    """Control for the hook's compare-and-set: with nobody pausing, the same final
    failure settles the stranded ``queued`` request as ``failed`` (runnable again)."""
    rid = seed_mod.sid("scout", "nairobi")
    monkeypatch.setattr(pipeline, "fetch_signals", _fixture_fetch(fail=RuntimeError("source down")))
    job_id = _queue(factory, rid, max_attempts=1)
    with offline_llm():
        assert _runner(factory, clock=lambda: _NOW + timedelta(minutes=1)).poll_once(
            worker_id="w-failed"
        )
    with factory() as s:
        assert s.get(Job, job_id).status == JobStatus.DEAD_LETTERED.value
        assert s.get(ScoutRequest, rid).status == FAILED


def test_refusal_writer_keeps_a_paused_request_paused_and_records_the_refusal(s):
    """The refusal's after-rollback writer meets a request that is already paused (the
    pause committed when the attempt's rollback released the row): status kept, the
    refusal's stats stored (as the completed-run guard keeps a paused run's stats)."""
    req = _request(s)
    req.status = PAUSED
    s.flush()
    exc = SignalVolumeAboveBoundError(scanned=101, bound=100)
    handlers._record_refused_run(req.id, _context(req), exc)(s)
    s.expire_all()
    row = s.get(ScoutRequest, req.id)
    assert row.status == PAUSED
    assert row.stats == pipeline.refused_run_stats(scanned=101, bound=100)


def test_refusal_writer_does_not_overwrite_a_pause_committed_after_its_read(s, monkeypatch):
    """The writer's own window: it loaded the request as ``queued`` and the pause is
    written to the row before its status write (between the load and the write). The
    write is a compare-and-set, so the pause wins."""
    req = _request(s)
    req.status = QUEUED
    s.flush()
    real_stats = handlers.refused_run_stats

    def pause_then_stats(**kwargs):
        _pause_row(s, req.id)  # the writer's instance still says ``queued``
        return real_stats(**kwargs)

    monkeypatch.setattr(handlers, "refused_run_stats", pause_then_stats)
    exc = SignalVolumeAboveBoundError(scanned=101, bound=100)
    handlers._record_refused_run(req.id, _context(req), exc)(s)
    s.expire_all()
    row = s.get(ScoutRequest, req.id)
    assert row.status == PAUSED
    assert row.stats["refused"] is True


def test_terminal_failure_hook_does_not_overwrite_a_pause_committed_after_its_read(s):
    """``settle_failed_scout_request`` reads an in-flight status (here a stale identity
    map says ``running``) while the row is already paused. Its ``failed`` write is a
    compare-and-set on the in-flight states, so the pause wins."""
    req = _request(s)
    req.status = RUNNING
    s.flush()
    _pause_row(s, req.id)
    assert req.status == RUNNING  # the instance the hook will get is stale
    handlers.settle_failed_scout_request(
        TerminalFailureContext(
            db=s,
            context=_context(req),
            payload={"scout_request_id": req.id},
            job_id="job-pause-hook",
            job_type=JobType.SCOUT_REQUEST_EXECUTE.value,
            status=JobStatus.FAILED.value,
            error_code=JobErrorCode.TRANSIENT,
            attempt=1,
            now=_NOW,
        )
    )
    s.expire_all()
    assert s.get(ScoutRequest, req.id).status == PAUSED


# --------------------------------------------------------------------------- #
# (e) + (f) through HTTP: pause keeps completed findings, configuration and stats;
# resume behaves as implemented today
# --------------------------------------------------------------------------- #
def test_clause_http_pause_and_resume_keep_completed_findings_and_resume_lands_in_draft(
    client, factory
):
    """A completed run's findings, the request's configuration, its ``stats`` and
    ``last_run_at`` survive ``pause`` and ``resume`` unchanged. Resume is pinned as
    implemented today: the request lands in ``draft`` even though it was ``completed``
    before the pause (D-25-b, not decided here), and it is runnable at once."""
    rid = seed_mod.sid("scout", "dallas")
    ws = seed_mod.sid("ws")
    auth = {"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"}
    base = f"{API}/workspaces/{ws}/scout-requests/{rid}"
    assert client.post(base + "/run", headers=auth).status_code == 200
    with offline_llm():
        assert _runner(factory).poll_once(worker_id="w-findings") is True

    def snapshot() -> tuple:
        with factory() as s:
            r = s.get(ScoutRequest, rid)
            config = (
                r.name,
                list(r.keywords or []),
                list(r.source_types or []),
                r.location_id,
                r.resolved_market,
                r.campaign_id,
            )
            return r.status, config, r.stats, r.last_run_at, _findings(s, rid)

    status, config, stats, last_run_at, findings = snapshot()
    assert status == COMPLETED and findings["opportunities"] > 0 and last_run_at is not None
    shown = client.get(base, headers=auth).json()

    paused = client.post(base + "/pause", headers=auth)
    assert paused.status_code == 200 and paused.json()["status"] == PAUSED
    assert snapshot() == (PAUSED, config, stats, last_run_at, findings)

    resumed = client.post(base + "/resume", headers=auth)
    assert resumed.status_code == 200
    assert resumed.json()["status"] == DRAFT  # not ``completed``: today's resume
    assert snapshot() == (DRAFT, config, stats, last_run_at, findings)
    again = client.get(base, headers=auth).json()
    assert {k: v for k, v in again.items() if k not in ("status", "updated_at")} == {
        k: v for k, v in shown.items() if k not in ("status", "updated_at")
    }
    assert client.post(base + "/run", headers=auth).status_code == 200


def test_clause_pause_then_resume_while_queued_lets_the_queued_run_execute(
    client, factory, counted
):
    """Pinned current behaviour (review r1 F8; D-25-a, not decided here): a pause
    followed by a resume while the run is still queued leaves the job in place, and the
    worker then executes it normally -- resume puts the request in ``draft`` and the
    entry guard only skips ``paused``."""
    rid = seed_mod.sid("scout", "nairobi")
    ws = seed_mod.sid("ws")
    auth = {"Authorization": f"Bearer {live_bearer(seed_mod.sid('user'))}"}
    base = f"{API}/workspaces/{ws}/scout-requests/{rid}"
    job_id = client.post(base + "/run", headers=auth).json()["stats"]["job_id"]
    assert client.post(base + "/pause", headers=auth).json()["status"] == PAUSED
    assert client.post(base + "/resume", headers=auth).json()["status"] == DRAFT

    with offline_llm():
        assert _runner(factory).poll_once(worker_id="w-resumed") is True

    assert counted["fetch"] == 1 and counted["model"] > 0
    with factory() as s:
        job = s.get(Job, job_id)
        assert job.status == JobStatus.SUCCEEDED.value
        assert job.result_summary["outcome"] == "completed"
        assert s.get(ScoutRequest, rid).status == COMPLETED
