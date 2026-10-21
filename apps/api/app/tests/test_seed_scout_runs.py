"""P6-UI-009: the demo seed's scout runs are real durable runs.

The seed used to call the pipeline directly, so its four ``completed`` scout requests
had no durable job: their run history (``GET .../runs``) and background-jobs panel
(``GET .../jobs``) were empty, which read as data loss. It now queues each request's
run as "Run now" does and executes it on the worker's own path (``JobRunner``), so the
job, its lifecycle events and its result summary are written by the worker's code.

Each test seeds a fresh file-backed SQLite database through the module-level
``SessionLocal`` the seed (and its runner) use; the seed target guard is untouched and
has its own suite (``test_seed_target_guard.py``).
"""

from __future__ import annotations

import sys
import threading

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.enums import ScoutRequestStatus
from app.db import seed as seed_mod
from app.db.models import Base
from app.jobs import handlers as handlers_mod
from app.jobs.models import Job, JobEvent
from app.jobs.status import JobErrorCode, JobExecutionError, JobStatus, JobType
from app.jobs.store import job_store
from app.opportunities.models import Opportunity
from app.organizations.models import Organization, Workspace
from app.scouting_requests.models import ScoutRequest
from app.scouting_requests.run_history import get_run_history

MARKETS = ("dallas", "london", "lagos", "nairobi")


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'seed.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    original = seed_mod.SessionLocal
    seed_mod.SessionLocal = make
    try:
        yield make
    finally:
        seed_mod.SessionLocal = original
        engine.dispose()


def _row_counts(factory) -> dict[str, int]:
    with factory() as s:
        return {
            model.__tablename__: s.scalar(select(func.count()).select_from(model)) or 0
            for model in (Organization, ScoutRequest, Opportunity, Job, JobEvent)
        }


def test_each_seeded_request_has_one_succeeded_run_written_by_the_worker(factory):
    seed_mod.seed()

    with factory() as s:
        for key in MARKETS:
            req = s.get(ScoutRequest, seed_mod.sid("scout", key))
            assert req.status == ScoutRequestStatus.COMPLETED.value

            jobs = list(s.execute(select(Job).where(Job.scout_request_id == req.id)).scalars())
            assert len(jobs) == 1, key
            job = jobs[0]
            assert job.job_type == JobType.SCOUT_REQUEST_EXECUTE.value
            assert job.status == JobStatus.SUCCEEDED.value
            assert job.attempt_count == 1
            assert job.started_at is not None and job.completed_at is not None
            assert job.completed_at >= job.started_at
            # Released on completion exactly as the worker releases a lease.
            assert job.worker_id is None and job.lease_token is None
            # Every lifecycle step was recorded by the store, once.
            events = sorted(
                s.execute(select(JobEvent.event_type).where(JobEvent.job_id == job.id)).scalars()
            )
            assert events == ["claimed", "enqueued", "started", "succeeded"]

            # What the customer reads: the run history projection and the jobs list.
            history = get_run_history(
                s,
                organization_id=seed_mod.sid("org"),
                workspace_id=seed_mod.sid("ws"),
                scout_request_id=req.id,
            )
            assert history.total == 1
            run = history.items[0]
            assert run.status == JobStatus.SUCCEEDED.value
            assert run.stats is not None
            for field in ("scanned", "noise_filtered", "signals_analyzed", "opportunities"):
                assert getattr(run.stats, field) == req.stats[field], (key, field)
            assert run.stats.opportunities == 3
            listed, total = job_store.list_jobs(
                s,
                organization_id=seed_mod.sid("org"),
                workspace_id=seed_mod.sid("ws"),
                scout_request_id=req.id,
            )
            assert total == 1 and listed[0].id == job.id

    # The demo itself is unchanged: three simulated opportunities per market.
    assert _row_counts(factory)["opportunities"] == 12


def test_running_the_seed_again_adds_no_rows(factory, capsys):
    seed_mod.seed()
    first = _row_counts(factory)
    assert first == {
        "organizations": 1,
        "scout_requests": 4,
        "opportunities": 12,
        "jobs": 4,
        "job_events": 16,
    }

    seed_mod.seed()

    assert "already present" in capsys.readouterr().out
    assert _row_counts(factory) == first


def test_reset_rebuilds_one_run_per_request(factory):
    seed_mod.seed()
    seed_mod.seed(reset=True)
    counts = _row_counts(factory)
    assert counts["jobs"] == 4 and counts["job_events"] == 16
    with factory() as s:
        statuses = set(s.execute(select(Job.status)).scalars())
    assert statuses == {JobStatus.SUCCEEDED.value}


def test_leaves_its_runs_queued_when_someone_elses_job_is_waiting(factory, capsys):
    # Another tenant's unfinished job: draining the queue would run it too.
    with factory() as s:
        s.add(Organization(id="org-other", name="Other Co", slug="other-co"))
        s.flush()
        s.add(Workspace(id="ws-other", organization_id="org-other", name="Other", slug="other"))
        s.flush()
        other = job_store.enqueue(
            s,
            organization_id="org-other",
            workspace_id="ws-other",
            job_type=JobType.SCOUT_REQUEST_EXECUTE.value,
            payload={"scout_request_id": "not-a-seeded-request"},
            payload_hash="0" * 64,
        )
        s.commit()
        other_id = other.id

    seed_mod.seed()

    assert "left queued for the worker" in capsys.readouterr().out
    with factory() as s:
        untouched = s.get(Job, other_id)
        assert untouched.status == JobStatus.PENDING.value
        assert untouched.attempt_count == 0 and untouched.worker_id is None
        seeded = list(s.execute(select(Job).where(Job.id != other_id)).scalars())
        assert len(seeded) == 4
        assert {j.status for j in seeded} == {JobStatus.PENDING.value}
        requests = list(s.execute(select(ScoutRequest)).scalars())
        assert {r.status for r in requests} == {ScoutRequestStatus.QUEUED.value}


def test_a_failed_run_stops_the_seed_and_stays_recorded_on_its_job(factory, monkeypatch):
    def unavailable(db, scout_request_id, context=None):
        raise RuntimeError("fixture source unavailable")

    monkeypatch.setattr(handlers_mod, "_run", unavailable)

    with pytest.raises(seed_mod.SeedRunError, match="0 of 4 seeded scout runs succeeded"):
        seed_mod.seed()

    with factory() as s:
        jobs = list(s.execute(select(Job)).scalars())
    assert len(jobs) == 4
    # The worker's own failure handling: an unclassified error is a retryable
    # transient failure, recorded by its class name only.
    assert {j.status for j in jobs} == {JobStatus.RETRY_WAIT.value}
    assert {j.last_error_code for j in jobs} == {"transient"}
    assert {j.attempt_count for j in jobs} == {1}


# --------------------------------------------------------------------------- #
# The seed's own runner (#240 adversarial review F1-F3)
# --------------------------------------------------------------------------- #
def _foreign_workspace(factory) -> None:
    with factory() as s:
        s.add(Organization(id="org-other", name="Other Co", slug="other-co"))
        s.flush()
        s.add(Workspace(id="ws-other", organization_id="org-other", name="Other", slug="other"))
        s.commit()


def _other_worker(factory, *, name: str = "other-worker"):
    """A second worker polling the same database, as `npm run dev`'s does."""
    from app.jobs.worker import JobRunner

    runner = JobRunner(settings=get_settings(), session_factory=factory)
    stop = threading.Event()

    def loop() -> None:
        while not stop.is_set():
            if not runner.poll_once(worker_id=name):
                stop.wait(0.05)

    thread = threading.Thread(target=loop, name=name, daemon=True)
    thread.start()
    return stop, thread


def test_the_runner_refuses_a_non_sqlite_session_before_connecting(monkeypatch):
    # F1: guarded in its own right, like _reset: bound to PostgreSQL it refuses
    # before anything opens a connection.
    engine = create_engine("postgresql+psycopg://u:p@runner-guard.example.invalid:5432/db")
    attempts: list[str] = []

    @event.listens_for(engine, "do_connect")
    def _record(*_args, **_kwargs):
        attempts.append("connect")
        raise AssertionError("the guard let the runner connect")

    monkeypatch.setattr(seed_mod, "SessionLocal", sessionmaker(bind=engine, future=True))

    with pytest.raises(seed_mod.SeedTargetError):
        seed_mod._execute_seed_runs(["any-job"])
    assert attempts == []


def _enqueue_a_foreign_job_after_the_first_seeded_run(factory, monkeypatch) -> dict[str, str]:
    """Another tenant's higher-priority job appears right after the seed's first run."""
    from app.jobs.worker import JobRunner

    _foreign_workspace(factory)
    foreign: dict[str, str] = {}
    real_poll = JobRunner.poll_once

    def poll(self, *, worker_id):
        ran = real_poll(self, worker_id=worker_id)
        if ran and worker_id == seed_mod._SEED_WORKER_ID and not foreign:
            with factory() as s:
                job = job_store.enqueue(
                    s,
                    organization_id="org-other",
                    workspace_id="ws-other",
                    job_type=JobType.SCOUT_REQUEST_EXECUTE.value,
                    payload={"scout_request_id": "not-a-seeded-request"},
                    payload_hash="0" * 64,
                    priority=10,
                )
                s.commit()
                foreign["id"] = job.id
        return ran

    monkeypatch.setattr(JobRunner, "poll_once", poll)
    return foreign


def _jobs_besides(factory, job_id: str) -> list[str]:
    with factory() as s:
        return sorted(s.execute(select(Job.status).where(Job.id != job_id)).scalars())


def test_stops_claiming_when_someone_elses_job_appears_mid_run(factory, monkeypatch, capsys):
    # F2: exclusivity is re-checked before every claim. A higher-priority job of
    # another tenant enqueued after the first seeded run would otherwise be the
    # next one claimed.
    foreign = _enqueue_a_foreign_job_after_the_first_seeded_run(factory, monkeypatch)

    seed_mod.seed()

    assert "left queued for the worker" in capsys.readouterr().out
    with factory() as s:
        untouched = s.get(Job, foreign["id"])
        assert untouched.status == JobStatus.PENDING.value
        assert untouched.attempt_count == 0 and untouched.worker_id is None
    assert _jobs_besides(factory, foreign["id"]) == ["pending", "pending", "pending", "succeeded"]


def test_a_run_that_failed_outright_is_not_hidden_by_someone_elses_job(
    factory, monkeypatch, capsys
):
    # F3: the outcome is the final state of the seed's own jobs. Stopping for another
    # tenant's job must not turn a run that already failed for good into "left queued".
    def unusable(db, scout_request_id, context=None):
        raise JobExecutionError(JobErrorCode.NON_RETRYABLE, "fixture source unusable")

    monkeypatch.setattr(handlers_mod, "_run", unusable)
    foreign = _enqueue_a_foreign_job_after_the_first_seeded_run(factory, monkeypatch)

    with pytest.raises(
        seed_mod.SeedRunError, match=r"0 of 4 seeded scout runs succeeded \(1 failed, 3 pending\)"
    ):
        seed_mod.seed()

    assert "left queued for the worker" not in capsys.readouterr().out
    with factory() as s:
        untouched = s.get(Job, foreign["id"])
        assert untouched.status == JobStatus.PENDING.value and untouched.attempt_count == 0
    assert _jobs_besides(factory, foreign["id"]) == ["failed", "pending", "pending", "pending"]


def _gate_one_run_on_the_other_worker(monkeypatch, *, release: threading.Event):
    """The other worker claims a seeded run first and holds it until `release`."""
    from app.jobs.worker import JobRunner

    other_claimed = threading.Event()
    real_run = handlers_mod._run

    def gated_run(db, scout_request_id, context=None):
        if threading.current_thread().name == "other-worker":
            other_claimed.set()
            release.wait(timeout=30)
        return real_run(db, scout_request_id, context)

    monkeypatch.setattr(handlers_mod, "_run", gated_run)
    real_poll = JobRunner.poll_once
    seed_found_nothing = threading.Event()

    def poll(self, *, worker_id):
        if worker_id != seed_mod._SEED_WORKER_ID:
            return real_poll(self, worker_id=worker_id)
        other_claimed.wait(timeout=30)
        ran = real_poll(self, worker_id=worker_id)
        if not ran:
            seed_found_nothing.set()
        return ran

    monkeypatch.setattr(JobRunner, "poll_once", poll)
    return seed_found_nothing


def test_runs_another_worker_takes_count_as_seeded(factory, monkeypatch):
    # F3: with `npm run dev`'s worker polling, some seeded runs are claimed there.
    # The seed judges the final state of its own jobs, not what it ran itself.
    release = threading.Event()
    seed_found_nothing = _gate_one_run_on_the_other_worker(monkeypatch, release=release)
    # The held run finishes as soon as the seed has nothing left to claim.
    threading.Thread(
        target=lambda: (seed_found_nothing.wait(30), release.set()), daemon=True
    ).start()
    stop, thread = _other_worker(factory)
    try:
        seed_mod.seed()
    finally:
        release.set()
        stop.set()
        thread.join(timeout=30)

    with factory() as s:
        statuses = list(s.execute(select(Job.status)).scalars())
        finishers = set(
            s.execute(
                select(JobEvent.worker_id).where(JobEvent.event_type == "succeeded")
            ).scalars()
        )
    assert statuses == [JobStatus.SUCCEEDED.value] * 4
    assert finishers == {seed_mod._SEED_WORKER_ID, "other-worker"}


def test_waits_a_bounded_time_for_a_run_held_elsewhere(factory, monkeypatch):
    # F3: a run another worker still holds when the wait ends is reported as such.
    monkeypatch.setattr(seed_mod, "_SEED_RUNS_WAIT_SECONDS", 0.5)
    release = threading.Event()
    _gate_one_run_on_the_other_worker(monkeypatch, release=release)
    stop, thread = _other_worker(factory)
    try:
        with pytest.raises(
            seed_mod.SeedRunError, match=r"3 of 4 seeded scout runs succeeded \(1 running\)"
        ):
            seed_mod.seed()
    finally:
        release.set()
        stop.set()
        thread.join(timeout=30)


def test_main_reports_an_incomplete_seed_in_one_line(tmp_path, monkeypatch):
    # F3: no traceback; exit status 1 with what to do, including the dev worker.
    engine = create_engine(f"sqlite:///{tmp_path / 'main.db'}", future=True)
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
    monkeypatch.setattr(seed_mod, "engine", engine)
    monkeypatch.setattr(seed_mod, "SessionLocal", sessionmaker(bind=engine, future=True))

    def incomplete(reset=False, **_kwargs):
        raise seed_mod.SeedRunError(
            "3 of 4 seeded scout runs succeeded (1 running); each job records its outcome."
        )

    monkeypatch.setattr(seed_mod, "seed", incomplete)
    monkeypatch.setattr(sys, "argv", ["seed"])

    with pytest.raises(SystemExit) as exited:
        seed_mod.main()

    message = exited.value.code
    assert isinstance(message, str) and "\n" not in message  # printed as one line, exit 1
    assert message.startswith("Demo seed incomplete: 3 of 4 seeded scout runs succeeded")
    assert "npm run dev" in message and "npm run seed:reset" in message
    engine.dispose()
