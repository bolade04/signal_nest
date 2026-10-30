"""The fenced terminal transaction's lock order (D-25 recovery contract §9.1, PROPOSED).

A defect in the previously reviewed candidate (430f7f2f), found by lane W15's probe L1: the
worker's fenced failure transaction updated the request row (its fenced writers, e.g. the
refusal-at-bound settlement of review r1 F1) BEFORE the job row (the token-fenced ``fail``),
while every admission check locks job -> request. A stale attempt's admission check and the
fenced transaction then deadlock (``40P01``); depending on which side PostgreSQL aborts, the
uncaught error lands in the worker's fenced transaction. The fix locks the job row first, so
the single order job -> request -> ledger -> period -> reservation holds; no end state changes.

This module uses only names that exist at 430f7f2f, so it runs unchanged against it (where it
reproduces the deadlock) and against the fix (where the stale check simply waits).

The run's SUCCESS commit had the same inversion (recovery contract §10.1, review r1 F1/C1):
``RunAdmission.finish`` updated the request in the run's transaction before the worker's
``complete`` updated the job. ``test_rev_r8_...`` is the independent reviewer's probe R8 (lane
W15-REV-RECOVERY, ``scripts/test_rev_recovery_r1b_postgres.py``, sha256 22fb4afc...395a),
adopted with credit. Two adaptations, no assertion changed: its helpers are inlined; and the
wait for the worker's position is "it reached ``complete`` OR it waits on the job row lock",
because with the fix the run blocks on the stale check's lock before reaching ``complete``.

Gate: ``require_postgres`` (fails, never skips, under ``CI=true``).
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import text

from app.core.config import get_settings
from app.jobs import pipeline
from app.jobs.store import DurableJobStore
from app.llm.mock import MockLLMProvider
from app.llm.service import LLMService
from app.tests import test_scout_pause_postgres as harness
from app.tests._auth2_support import require_postgres
from app.tests._llm_offline import offline_settings
from app.tests.test_scout_pause_postgres import JOIN_SECONDS, Sources, _end_state, _start_worker

template_url = harness.template_url
world = harness.world


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


def _lock_waiter(w, exclude: int) -> bool:
    """A backend of this database (not ``exclude``) waits on a lock in a FOR NO KEY UPDATE."""
    with w.observer.connect() as c:
        return bool(
            c.execute(
                text(
                    "select count(*) from pg_stat_activity where wait_event_type = 'Lock'"
                    " and pid <> :p and datname = current_database()"
                    " and query ilike '%for no key update%'"
                ),
                {"p": exclude},
            ).scalar_one()
        )


def test_a_stale_admission_check_never_deadlocks_the_fenced_failure(world, monkeypatch):
    """A run refused at the in-process bound settles through its fenced writer and the fenced
    ``fail``. A stale attempt's admission check holds the job row FOR SHARE, then asks for the
    request row FOR SHARE once the fenced transaction is under way. Before the fix the two
    deadlock; after it the check waits for the fenced transaction, and the run still ends
    ``failed`` with the request ``failed`` and the refusal's stats (mutant L1's killer)."""
    w = world
    monkeypatch.setattr(get_settings(), "vector_inprocess_max_signals_per_run", 2)
    sources = Sources(gate=True, count=5)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    monkeypatch.setattr(
        pipeline,
        "llm_service",
        LLMService(
            MockLLMProvider(model="mock-1", seed=offline_settings().llm_mock_seed),
            settings=offline_settings(),
        ),
    )
    at_fail, stale_waiting = threading.Event(), threading.Event()
    real_fail = DurableJobStore.fail

    def held_fail(self, db, job, **kw):
        at_fail.set()
        stale_waiting.wait(10)  # the stale check queues on the request row first
        return real_fail(self, db, job, **kw)

    monkeypatch.setattr(DurableJobStore, "fail", held_fail)
    worker = _start_worker(w)
    assert sources.started.wait(JOIN_SECONDS)  # claimed, running, parked in its fetch
    stale = w.engine.connect()
    stale.begin()
    stale_pid = stale.execute(text("select pg_backend_pid()")).scalar_one()
    stale.execute(text("select status from jobs where id = :j for share"), {"j": w.job_id}).all()
    sources.release.set()
    deadline = time.monotonic() + JOIN_SECONDS
    while not (at_fail.is_set() or _lock_waiter(w, stale_pid)):
        assert time.monotonic() < deadline, "the fenced transaction never started"
        time.sleep(0.02)
    errors: list[str] = []

    def second_half() -> None:
        try:
            stale.execute(
                text("select status from scout_requests where id = :r for share"), {"r": w.rid}
            ).all()
            stale.commit()
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(type(getattr(exc, "orig", exc)).__name__)
            stale.rollback()

    t = threading.Thread(target=second_half, daemon=True)
    t.start()
    time.sleep(0.3)
    stale_waiting.set()
    t.join(30)
    stale.close()
    worker.join_ok()
    assert errors == []  # no DeadlockDetected (40P01) on either side
    end = _end_state(w)
    assert (end.job_status, end.status) == ("failed", "failed")
    assert end.stats.get("refused")


# --------------------------------------------------------------------------- #
# The success commit (contract §10.1): the reviewer's R8, adopted
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("deadlock_timeout_ms", "gap"),
    [(1000, 0.3), (100, 0.05)],
    ids=["commit_victim", "stale_victim"],
)
def test_rev_r8_a_stale_admission_check_against_the_success_commit(
    world, monkeypatch, deadlock_timeout_ms, gap
):
    """Review r1 F1 (reviewer's probe R8). A stale admission check holds the job row FOR SHARE
    and then asks for the request row FOR SHARE while the run's success commit is under way.
    ``commit_victim``: the stale side waits longer than the run's deadlock timeout, so before the
    fix the SUCCESS commit was rolled back (job ``retry_wait``, request ``running``).
    ``stale_victim``: the stale side detects the cycle first. With the job row locked first, the
    stale check simply waits and the run completes (mutant M1's killer)."""
    from app.jobs.models import Job

    w = world
    with w.factory() as s:  # the reviewer's ``rec._manual_prior(w, "completed")``
        job = s.get(Job, w.job_id)
        job.payload = {**job.payload, "prior_status": "completed"}
        s.commit()
    sources = Sources(gate=True)
    monkeypatch.setattr(pipeline, "fetch_signals", sources)
    monkeypatch.setattr(  # the reviewer's ``rec._mock_llm(monkeypatch)``
        pipeline,
        "llm_service",
        LLMService(
            MockLLMProvider(model="mock-1", seed=offline_settings().llm_mock_seed),
            settings=offline_settings(),
        ),
    )
    at_complete, stale_waiting = threading.Event(), threading.Event()
    real_complete = DurableJobStore.complete

    def held_complete(self, db, job, **kw):
        at_complete.set()  # ``finish`` has already run in this transaction
        stale_waiting.wait(10)
        return real_complete(self, db, job, **kw)

    monkeypatch.setattr(DurableJobStore, "complete", held_complete)
    worker = _start_worker(w)
    assert sources.started.wait(JOIN_SECONDS)
    stale = w.engine.connect()
    stale.begin()
    stale_pid = stale.execute(text("select pg_backend_pid()")).scalar_one()
    stale.execute(text(f"set local deadlock_timeout = '{deadlock_timeout_ms}ms'"))
    stale.execute(text("select status from jobs where id = :j for share"), {"j": w.job_id}).all()
    sources.release.set()
    # Adapted wait (see the module docstring): the run reached ``complete`` (before the fix) or
    # waits on the job row lock its terminal write now takes first (after it).
    deadline = time.monotonic() + JOIN_SECONDS
    while not (at_complete.is_set() or _lock_waiter(w, stale_pid)):
        assert time.monotonic() < deadline, "the success commit never started"
        time.sleep(0.02)
    errors: list[str] = []

    def second_half() -> None:
        try:
            stale.execute(
                text("select status from scout_requests where id = :r for share"), {"r": w.rid}
            ).all()
            stale.commit()
        except Exception as exc:  # noqa: BLE001 - recorded and asserted below
            errors.append(type(getattr(exc, "orig", exc)).__name__)
            stale.rollback()

    t = threading.Thread(target=second_half, daemon=True)
    t.start()
    time.sleep(gap)  # the stale check now waits on the request row (before the fix)
    stale_waiting.set()  # ``complete`` now asks for the job row the stale check holds FOR SHARE
    t.join(30)
    stale.close()
    worker_error = None
    try:
        worker.join_ok()
    except Exception as exc:  # noqa: BLE001 - recorded and asserted below
        worker_error = type(getattr(exc, "orig", exc)).__name__
    end = _end_state(w)
    with w.factory() as s:
        job = s.execute(
            text(
                "select status, last_error_code, last_error_summary, attempt_count"
                " from jobs where id=:j"
            ),
            {"j": w.job_id},
        ).first()
    print(
        f"REVR8 stale_deadlock_timeout={deadlock_timeout_ms}ms gap={gap}s stale_errors={errors}"
        f" worker_error={worker_error} job={tuple(job)} request={end.status}"
    )
    assert errors == [] and worker_error is None, (errors, worker_error, tuple(job), end.status)
    # No end state changes: the run completed, the request is completed.
    assert (job[0], end.status) == ("succeeded", "completed"), (tuple(job), end.status)
