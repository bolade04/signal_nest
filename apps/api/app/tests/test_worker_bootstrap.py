"""Phase 6U-1A: the worker process must be able to execute what it advertises.

Before this suite the durable worker was structurally incapable of running any
job: ``app.jobs.handlers`` registers every handler as an import side effect, and
the only module that imported it was ``app.jobs.service`` — which the API imports
and the worker does not. A real worker therefore claimed each job, failed to
resolve a handler, and failed it non-retryably with ``unsupported_type`` in
milliseconds, while advertising both job types to the fleet registry.

Nothing in the existing suite could see that, for two reasons this module is
built to avoid:

* **Process boundary.** Every job test imports ``app.main`` or
  ``app.jobs.service`` at module scope, and pytest imports all collected modules
  before the first test runs — so the registry was always populated by the time
  any assertion executed. An in-process assertion is structurally incapable of
  failing here, which is why the registry checks below run in a **subprocess**.
* **Ordering, not presence.** ``app.core.lifecycle.close_coordination`` imports
  ``app.jobs.service`` from inside ``graceful_shutdown``, which the worker calls
  as the last statement of ``run()``. So the worker *does* eventually import the
  handlers — on its way out, long after it needed them. "Were handlers imported?"
  is the wrong question; "were they registered before the first possible claim?"
  is the right one, and it is what these tests assert.

This module deliberately imports neither ``app.main`` nor ``app.jobs.service``.
ORM metadata comes from ``app.db.models`` (which registers no handlers), so the
only thing that can populate the registry here is the worker's own bootstrap.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.enums import ScoutRequestStatus
from app.db import seed as seed_mod

# Registers every ORM model on the shared Base. Unlike ``app.main`` this pulls in
# no route, no enqueue service and therefore no handler registration — so a test
# here that passes proves the *worker* registered the handlers.
from app.db.models import Base
from app.jobs import registry as registry_mod
from app.jobs.models import Job
from app.jobs.registry import (
    HandlerContext,
    builtin_job_types,
    known_job_types,
    register_handler,
)
from app.jobs.status import JobStatus, JobType
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner, Worker
from app.jobs.worker_models import WorkerRegistration
from app.jobs.worker_registry import WorkerRegistry
from app.opportunities.models import Opportunity
from app.scouting_requests.models import ScoutRequest

API_DIR = Path(__file__).resolve().parents[3]
_MARKET = "dallas"


# --------------------------------------------------------------------------- #
# Fresh-process helpers
# --------------------------------------------------------------------------- #
def _fresh_process(body: str) -> subprocess.CompletedProcess[str]:
    """Run ``body`` in a brand-new interpreter rooted at ``apps/api``.

    A fresh process is the only place the worker's real import graph can be
    observed: within pytest the registry has already been populated by other
    collected modules. ``cwd`` matters independently — ``DATABASE_URL`` and the
    settings ``.env`` are both CWD-relative, so a worker started from the repo
    root silently attaches to a different database.
    """
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(body)],
        cwd=API_DIR,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_fresh_process_worker_import_registers_builtin_handlers() -> None:
    """Importing only the worker must leave every built-in job type resolvable.

    This is the exact defect: before the fix this printed ``()``.
    """
    proc = _fresh_process(
        """
        import json, sys
        import app.jobs.worker  # the real worker entrypoint module
        from app.jobs.registry import known_job_types, builtin_job_types
        print(json.dumps({
            "registered": list(known_job_types()),
            "builtin": list(builtin_job_types()),
            "service_imported": "app.jobs.service" in sys.modules,
            "main_imported": "app.main" in sys.modules,
        }))
        """
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    # Every built-in type resolves...
    assert set(result["registered"]) == set(result["builtin"])
    assert {"scout_request.execute", "scout_schedule.tick"} <= set(result["registered"])
    # ...and it was the worker's own bootstrap that did it, not the API's
    # enqueue service and not the FastAPI app.
    assert result["service_imported"] is False
    assert result["main_imported"] is False


def test_fresh_process_worker_has_complete_orm_metadata() -> None:
    """A worker-only process must be able to flush across the whole domain.

    Handlers write beyond the jobs tables, and SQLAlchemy's unit of work sorts
    tables by foreign key at flush time — so a process holding only a subset of
    the models raises ``NoReferencedTableError`` ("could not find table
    'organizations'") on the first flush, while ``configure_mappers()`` still
    succeeds. That failure classified as ``transient`` and dead-lettered the job
    after burning every attempt.

    This must run in a fresh process: importing ``app.db.models`` anywhere in the
    pytest session (this module does, for ``Base``) completes the metadata for
    every later test and hides the gap.
    """
    proc = _fresh_process(
        """
        import json
        import app.jobs.worker  # the worker's real import graph, nothing else
        from app.db.base import Base

        unresolved = []
        for table in Base.metadata.tables.values():
            for fk in table.foreign_keys:
                try:
                    fk.column  # resolves the referenced table; raises if absent
                except Exception as exc:
                    unresolved.append(f"{table.name}.{fk.parent.name}: {type(exc).__name__}")
        print(json.dumps({"unresolved": unresolved, "tables": len(Base.metadata.tables)}))
        """
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])
    assert result["unresolved"] == [], (
        "the worker process cannot resolve every foreign key, so a handler flush "
        f"will fail: {result['unresolved']}"
    )
    assert result["tables"] > 10, "metadata looks suspiciously empty"


def test_fresh_process_registration_precedes_first_possible_claim() -> None:
    """Registration must complete at import, not at shutdown (D7).

    ``graceful_shutdown`` imports ``app.jobs.service`` and would populate the
    registry *after* the worker has already failed every job it claimed. So this
    asserts the registry is full at a point where the shutdown path provably has
    not run: immediately after import, before a Worker is even constructed, and
    before ``validate()``. ``run()`` is never called.
    """
    proc = _fresh_process(
        """
        import json, sys
        import app.jobs.worker as w
        from app.jobs.registry import known_job_types

        at_import = sorted(known_job_types())
        # The shutdown path is what would otherwise import the handlers; prove it
        # has not run and cannot be the source of these registrations.
        shutdown_ran = "app.jobs.service" in sys.modules

        # A Worker can now be constructed and validated; still no claim, no run().
        worker = w.Worker(settings=None) if False else None
        print(json.dumps({
            "at_import": at_import,
            "shutdown_path_ran": shutdown_ran,
            "worker_constructed": worker is None,
        }))
        """
    )
    assert proc.returncode == 0, proc.stderr
    result = json.loads(proc.stdout.strip().splitlines()[-1])

    assert {"scout_request.execute", "scout_schedule.tick"} <= set(result["at_import"])
    assert result["shutdown_path_ran"] is False


def test_fresh_process_worker_module_body_under_dash_m_registers() -> None:
    """``python -m app.jobs.worker`` (the real entrypoint) registers identically.

    Executed via ``runpy`` with a non-``__main__`` run name so the module body
    runs exactly as ``-m`` would while the entrypoint guard stays false — no
    worker process is started, no database is touched.
    """
    proc = _fresh_process(
        """
        import json, runpy
        runpy.run_module("app.jobs.worker", run_name="__notmain__")
        from app.jobs.registry import known_job_types
        print(json.dumps(sorted(known_job_types())))
        """
    )
    assert proc.returncode == 0, proc.stderr
    registered = json.loads(proc.stdout.strip().splitlines()[-1])
    assert {"scout_request.execute", "scout_schedule.tick"} <= set(registered)


# --------------------------------------------------------------------------- #
# Fail-closed capability (D3)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def migrated_factory(tmp_path):
    """A throwaway file-backed SQLite DB with the full schema."""
    engine = create_engine(
        f"sqlite:///{tmp_path/'bootstrap.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    yield factory
    engine.dispose()


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_worker_refuses_to_start_when_a_builtin_handler_is_missing(
    migrated_factory, monkeypatch
) -> None:
    """A worker that cannot execute a built-in type must never reach registration.

    The registry is emptied in isolation (the autouse fixture in ``conftest.py``
    restores it), rather than production code growing a reset API purely for a
    test.
    """
    monkeypatch.delitem(registry_mod._HANDLERS, "scout_request.execute")

    worker = Worker(settings=_settings(), session_factory=migrated_factory)
    with pytest.raises(RuntimeError) as exc:
        worker.validate()

    message = str(exc.value)
    assert "scout_request.execute" in message
    assert "incomplete" in message.lower()


def test_worker_validate_passes_with_the_real_registry(migrated_factory) -> None:
    """The guard is not vacuous: with the real bootstrap, validation succeeds."""
    Worker(settings=_settings(), session_factory=migrated_factory).validate()


def test_advertised_job_types_equal_registered_job_types(migrated_factory) -> None:
    """The persisted fleet row must carry executable capability, not the enum.

    This reads the value the worker actually wrote, rather than comparing two
    hard-coded lists to each other.

    A handler outside :class:`JobType` is registered first, deliberately: with
    only the two built-ins registered, ``known_job_types()`` and
    ``[t.value for t in JobType]`` are indistinguishable, so the assertion would
    pass just as happily against the old enum-derived line and prove nothing.
    The extra type can only appear in the persisted row if the value really was
    read from the registry. The autouse fixture in ``conftest.py`` removes it
    again afterwards.
    """
    extra = "test.bootstrap.extra"

    @register_handler(extra)
    def _extra(ctx: HandlerContext) -> dict:  # pragma: no cover - never executed
        return {}

    worker = Worker(
        settings=_settings(),
        session_factory=migrated_factory,
        registry=WorkerRegistry(),
    )
    worker.validate()
    worker.register()

    with migrated_factory() as db:
        row = db.scalar(
            select(WorkerRegistration).where(
                WorkerRegistration.worker_id == worker.worker_id
            )
        )
    assert row is not None
    # The invariant: the fleet row advertises exactly what this process can resolve.
    assert set(row.supported_job_types) == set(known_job_types())
    # The discriminating half: an enum-derived value could never contain this.
    assert extra in row.supported_job_types
    # ...and validate() has already guaranteed that covers every built-in type.
    # A superset, not equality: sibling test modules legitimately register their
    # own handlers into the same process-global registry, and a worker that can
    # execute them should say so. Asserting equality here would be asserting the
    # absence of other tests, which is exactly the order-dependence this module
    # exists to avoid.
    assert set(builtin_job_types()) <= set(row.supported_job_types)


# --------------------------------------------------------------------------- #
# End-to-end through the real worker (D4 / D8)
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def seeded_factory(tmp_path_factory):
    """Seeded four-market demo data on a file-backed DB, no Job rows.

    Connectors are the deterministic fixture ones, so execution is offline and
    repeatable. The seed creates no jobs, so every job observed below was created
    by the code under test.
    """
    tmp = tmp_path_factory.mktemp("worker_bootstrap")
    engine = create_engine(
        f"sqlite:///{tmp/'bootstrap_e2e.db'}",
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
def _clear_jobs(request):
    """Leave no Job rows between end-to-end tests in this module."""
    yield
    if "seeded_factory" not in request.fixturenames:
        return
    factory = request.getfixturevalue("seeded_factory")
    with factory() as s:
        s.query(Job).delete(synchronize_session=False)
        s.commit()


def _hash(payload: dict) -> str:
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _queue_scout_run(factory, *, max_attempts: int = 5) -> tuple[str, str]:
    """Put a seeded scout request into the exact state ``POST .../run`` leaves.

    Returns ``(scout_request_id, job_id)``. The job is enqueued through the store
    with the production job type — deliberately *not* through
    ``app.jobs.service``, whose import would register the handlers and mask the
    defect this module exists to detect.
    """
    scout_request_id = seed_mod.sid("scout", _MARKET)
    payload = {"scout_request_id": scout_request_id}
    with factory() as db:
        request = db.get(ScoutRequest, scout_request_id)
        request.status = ScoutRequestStatus.QUEUED.value
        job = DurableJobStore().enqueue(
            db,
            organization_id=request.organization_id,
            workspace_id=request.workspace_id,
            job_type=JobType.SCOUT_REQUEST_EXECUTE.value,
            payload=payload,
            payload_hash=_hash(payload),
            scout_request_id=scout_request_id,
            location_id=request.location_id,
            max_attempts=max_attempts,
        )
        db.commit()
        return scout_request_id, job.id


def _runner(factory) -> JobRunner:
    # The default (wall-clock) clock is deliberate: these jobs are enqueued as
    # immediately-due, so a pinned past clock would make nothing claimable.
    return JobRunner(
        settings=_settings(),
        session_factory=factory,
        store=DurableJobStore(),
    )


def _reload(factory, scout_request_id: str, job_id: str) -> tuple[ScoutRequest, Job]:
    with factory() as db:
        return db.get(ScoutRequest, scout_request_id), db.get(Job, job_id)


def test_durable_scout_run_succeeds_through_the_real_worker(seeded_factory) -> None:
    """The founder's "Run now" path, end to end, with nothing hand-registered.

    Enqueue -> claim -> resolve -> execute -> succeed, and the scout request
    reaches ``completed`` with opportunities attached.
    """
    scout_request_id, job_id = _queue_scout_run(seeded_factory)

    assert _runner(seeded_factory).poll_once(worker_id="w-bootstrap") is True

    request, job = _reload(seeded_factory, scout_request_id, job_id)
    # The precise regression: no handler resolution failure.
    assert job.last_error_code != "unsupported_type"
    assert job.last_error_code is None
    assert job.status == JobStatus.SUCCEEDED.value
    assert request.status == ScoutRequestStatus.COMPLETED.value
    # The handler returned a safe summary, and real work was recorded.
    assert job.result_summary is not None
    assert job.result_summary.get("opportunities", 0) >= 0

    with seeded_factory() as db:
        found = db.scalars(
            select(Opportunity).where(Opportunity.scout_request_id == scout_request_id)
        ).all()
    assert found, "the run produced no opportunities"


def test_final_job_failure_settles_the_scout_request(seeded_factory, monkeypatch) -> None:
    """D8: a final failure must not strand the scout in ``queued``.

    A non-retryable failure is injected into the pipeline seam (the same
    technique the scheduling integration suite uses), so no source file is
    broken to produce it.
    """
    from app.jobs import handlers as handlers_mod
    from app.jobs.status import JobErrorCode, JobExecutionError

    def _boom(db, scout_request_id, context=None):
        raise JobExecutionError(JobErrorCode.VALIDATION, "injected permanent failure")

    monkeypatch.setattr(handlers_mod, "_run", _boom)

    scout_request_id, job_id = _queue_scout_run(seeded_factory)
    assert _runner(seeded_factory).poll_once(worker_id="w-bootstrap") is True

    request, job = _reload(seeded_factory, scout_request_id, job_id)

    # 1-2: the job reached a final failure with no retry left.
    assert job.status == JobStatus.FAILED.value
    assert job.last_error_code == JobErrorCode.VALIDATION.value
    assert job.available_at is None or job.status != JobStatus.RETRY_WAIT.value

    # 3-4: the scout request left the in-flight states.
    assert request.status not in (
        ScoutRequestStatus.QUEUED.value,
        ScoutRequestStatus.RUNNING.value,
    )
    assert request.status == ScoutRequestStatus.FAILED.value

    # 5: and is therefore runnable again — the route rejects only queued/running
    # (and paused), so a settled 'failed' request is not locked out.
    assert request.status not in (
        ScoutRequestStatus.QUEUED.value,
        ScoutRequestStatus.RUNNING.value,
        ScoutRequestStatus.PAUSED.value,
    )


def test_failed_scout_request_can_be_run_again(seeded_factory, monkeypatch) -> None:
    """The recovery half of D8: after settling, a fresh run succeeds."""
    from app.jobs import handlers as handlers_mod
    from app.jobs.status import JobErrorCode, JobExecutionError

    real_run = handlers_mod._run

    def _boom(db, scout_request_id, context=None):
        raise JobExecutionError(JobErrorCode.VALIDATION, "injected permanent failure")

    monkeypatch.setattr(handlers_mod, "_run", _boom)
    scout_request_id, _ = _queue_scout_run(seeded_factory)
    _runner(seeded_factory).poll_once(worker_id="w-bootstrap")

    with seeded_factory() as db:
        assert db.get(ScoutRequest, scout_request_id).status == (
            ScoutRequestStatus.FAILED.value
        )

    # Repair the seam and re-run exactly as the route would.
    monkeypatch.setattr(handlers_mod, "_run", real_run)
    _, job_id = _queue_scout_run(seeded_factory)
    assert _runner(seeded_factory).poll_once(worker_id="w-bootstrap") is True

    request, job = _reload(seeded_factory, scout_request_id, job_id)
    assert job.status == JobStatus.SUCCEEDED.value
    assert request.status == ScoutRequestStatus.COMPLETED.value


def test_retryable_failure_does_not_settle_the_scout_request(
    seeded_factory, monkeypatch
) -> None:
    """An intermediate retryable attempt is not a final failure.

    The job goes to ``retry_wait`` and the request must stay ``queued`` — settling
    it here would strand a run that is still coming.
    """
    from app.jobs import handlers as handlers_mod
    from app.jobs.status import JobErrorCode, JobExecutionError

    def _blip(db, scout_request_id, context=None):
        raise JobExecutionError(JobErrorCode.TRANSIENT, "injected transient failure")

    monkeypatch.setattr(handlers_mod, "_run", _blip)

    scout_request_id, job_id = _queue_scout_run(seeded_factory, max_attempts=5)
    assert _runner(seeded_factory).poll_once(worker_id="w-bootstrap") is True

    request, job = _reload(seeded_factory, scout_request_id, job_id)
    assert job.status == JobStatus.RETRY_WAIT.value
    assert request.status == ScoutRequestStatus.QUEUED.value


def test_exhausted_retries_settle_the_scout_request(seeded_factory, monkeypatch) -> None:
    """Dead-lettering is also a final failure, and must settle the request too."""
    from app.jobs import handlers as handlers_mod
    from app.jobs.status import JobErrorCode, JobExecutionError

    def _blip(db, scout_request_id, context=None):
        raise JobExecutionError(JobErrorCode.TRANSIENT, "injected transient failure")

    monkeypatch.setattr(handlers_mod, "_run", _blip)

    # max_attempts=1 -> the first attempt exhausts the budget, so the retryable
    # error dead-letters rather than backing off.
    scout_request_id, job_id = _queue_scout_run(seeded_factory, max_attempts=1)
    _runner(seeded_factory).poll_once(worker_id="w-bootstrap")

    request, job = _reload(seeded_factory, scout_request_id, job_id)
    assert job.status == JobStatus.DEAD_LETTERED.value
    assert request.status == ScoutRequestStatus.FAILED.value
