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

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.enums import ScoutRequestStatus
from app.db import seed as seed_mod
from app.db.models import Base
from app.jobs import handlers as handlers_mod
from app.jobs.models import Job, JobEvent
from app.jobs.status import JobStatus, JobType
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
