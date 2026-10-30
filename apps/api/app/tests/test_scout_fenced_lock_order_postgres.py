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
