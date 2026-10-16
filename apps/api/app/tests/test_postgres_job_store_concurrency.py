"""P6-CI-5: the production job store's PostgreSQL concurrency paths under real contention.

``DurableJobStore`` has two PostgreSQL-only mechanisms — ``SELECT ... FOR UPDATE SKIP
LOCKED`` in :meth:`claim_one` and in :meth:`recover_expired_leases` — and one shared
one, the lease-token fence inside every worker mutation. The SQLite suite
(``test_durable_jobs``) proves the shared lifecycle; the always-run compile proof and
the two gated tests in ``test_production_adapters`` prove that the claim compiles to
and honours ``SKIP LOCKED``. This module exercises the production methods on
PostgreSQL through **independent connections and genuinely overlapping
transactions**, on a schema produced by the real Alembic chain (never
``metadata.create_all``), in a database of each test's own:

* claim exclusivity under barrier-synchronised concurrent claimers (every job claimed
  exactly once, by exactly one worker) and progress past a row another transaction
  holds locked;
* concurrent lease recovery with exactly one recovery effect and one
  ``lease_recovered`` event per expired job, and a recovery loop stepping past a row
  another transaction holds locked;
* retryable recovery (-> pending) versus exhausted attempts (-> dead-lettered,
  ``timeout``);
* stale-owner fencing after recovery and reclaim on independent connections;
* unexpired, future-scheduled and terminal jobs untouched by concurrent recovery.

Every session carries a ``statement_timeout`` and ``lock_timeout`` so a regression that
blocks on a lock fails fast and unambiguously instead of hanging; every thread join
is bounded and asserted. Gate: :func:`require_postgres` — with ``TEST_POSTGRES_URL``
unset these tests FAIL in CI and skip only on a developer machine.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.jobs.models import Job, JobEvent
from app.jobs.status import JobError, JobErrorCode, JobStatus
from app.jobs.store import DurableJobStore, JobLeaseLostError
from app.organizations.models import Organization, Workspace
from app.tests._auth2_support import pg_database, require_postgres

API_DIR = Path(__file__).resolve().parents[2]
ORG = "org-ci5-0001"
WS = "ws-ci5-00001"
T0 = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
LATER = T0 + timedelta(hours=1)
JOIN_SECONDS = 30.0
DB_TIMEOUT_MS = 5000


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


def _migrate(url: str) -> None:
    env = dict(os.environ)
    env["DATABASE_URL"] = url
    env.pop("SN_MIGRATION_MODE", None)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.fixture
def engine() -> Iterator[Engine]:
    """A migrated database of this test's own; every connection carries DB timeouts."""
    with pg_database("sn_ci5") as url:
        _migrate(url)
        eng = create_engine(url, future=True, pool_size=12, max_overflow=12)

        @event.listens_for(eng, "connect")
        def _timeouts(dbapi_conn, _record):  # pragma: no cover - trivial hook
            cur = dbapi_conn.cursor()
            cur.execute(f"SET statement_timeout = {DB_TIMEOUT_MS}")
            cur.execute(f"SET lock_timeout = {DB_TIMEOUT_MS}")
            cur.close()

        # The migrated schema enforces the jobs -> organizations/workspaces foreign keys,
        # so the tenant scope exists first (through the ORM, with its model defaults).
        with Session(eng) as tenant:
            tenant.add(Organization(id=ORG, name="CI5", slug="ci5"))
            tenant.add(Workspace(id=WS, organization_id=ORG, name="CI5 WS", slug="ci5-ws"))
            tenant.commit()
        try:
            yield eng
        finally:
            eng.dispose()


@pytest.fixture
def factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)


@pytest.fixture
def store() -> DurableJobStore:
    return DurableJobStore()


def _enqueue(store: DurableJobStore, db: Session, key: str, **overrides) -> str:
    payload = {"scout_request_id": key}
    kwargs = dict(
        organization_id=ORG,
        workspace_id=WS,
        job_type="test.ok",
        payload=payload,
        payload_hash=hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest(),
        now=T0,
    )
    kwargs.update(overrides)
    job = store.enqueue(db, **kwargs)
    db.commit()
    return job.id


def _claim_and_run(store, factory, job_id: str | None = None, *, worker: str, now=T0, lease=30):
    """Claim (the given job if it is the only due one) and start it on an own session."""
    db = factory()
    try:
        job = store.claim_one(db, worker_id=worker, lease_seconds=lease, now=now)
        assert job is not None
        if job_id is not None:
            assert job.id == job_id
        store.mark_running(db, job, worker_id=job.worker_id, lease_token=job.lease_token, now=now)
        db.commit()
        return job.id, job.worker_id, job.lease_token
    finally:
        db.close()


def _row(factory, job_id: str) -> Job:
    db = factory()
    try:
        job = db.get(Job, job_id)
        db.refresh(job)
        return job
    finally:
        db.close()


def _events(factory, job_id: str) -> list[JobEvent]:
    db = factory()
    try:
        return list(
            db.execute(
                select(JobEvent).where(JobEvent.job_id == job_id).order_by(JobEvent.created_at)
            ).scalars()
        )
    finally:
        db.close()


def _run_threads(targets: list, *, names: list[str]) -> list[BaseException]:
    """Run every target on its own thread; bounded join; collect exceptions."""
    errors: list[BaseException] = []
    lock = threading.Lock()

    def _wrap(fn):
        def run():
            try:
                fn()
            except BaseException as exc:  # noqa: BLE001 - surfaced to the test
                with lock:
                    errors.append(exc)

        return run

    threads = [
        threading.Thread(target=_wrap(t), name=n, daemon=True)
        for t, n in zip(targets, names, strict=True)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(JOIN_SECONDS)
    assert not any(t.is_alive() for t in threads), "a worker thread did not finish in time"
    return errors


# --------------------------------------------------------------------------- #
# M5 — claim exclusivity under concurrency; progress past a locked row
# --------------------------------------------------------------------------- #
def test_concurrent_claimers_each_job_is_claimed_exactly_once(store, factory):
    setup = factory()
    job_ids = [_enqueue(store, setup, f"j{i}") for i in range(6)]
    setup.close()

    workers = 8
    barrier = threading.Barrier(workers)
    claims: list[tuple[str, str, str]] = []
    lock = threading.Lock()

    def claimer(name: str):
        db = factory()
        try:
            barrier.wait(timeout=JOIN_SECONDS)
            while True:
                job = store.claim_one(db, worker_id=name, lease_seconds=30, now=T0)
                if job is None:
                    return
                with lock:
                    claims.append((job.id, job.worker_id, job.lease_token))
        finally:
            db.close()

    names = [f"w{i}" for i in range(workers)]
    errors = _run_threads([lambda n=n: claimer(n) for n in names], names=names)
    assert errors == [], errors

    claimed_ids = [c[0] for c in claims]
    assert sorted(claimed_ids) == sorted(job_ids)  # every job claimed ...
    assert len(set(claimed_ids)) == len(job_ids)  # ... exactly once
    assert len({c[2] for c in claims}) == len(job_ids)  # distinct lease tokens
    for job_id, worker, token in claims:
        row = _row(factory, job_id)
        assert (row.status, row.worker_id, row.lease_token) == (
            JobStatus.CLAIMED.value, worker, token,
        )
        claimed_events = [e for e in _events(factory, job_id) if e.event_type == "claimed"]
        assert len(claimed_events) == 1 and claimed_events[0].worker_id == worker
    # Nothing is due any more: a further claim on a fresh session is idle.
    db = factory()
    try:
        assert store.claim_one(db, worker_id="late", lease_seconds=30, now=T0) is None
    finally:
        db.close()


def test_claim_one_progresses_past_a_row_locked_by_an_overlapping_transaction(
    store, factory, engine
):
    setup = factory()
    first = _enqueue(store, setup, "first", priority=10)  # the top candidate
    second = _enqueue(store, setup, "second", priority=0)
    setup.close()

    # An overlapping transaction holds the top candidate's row lock (what another
    # claimer holds between its SELECT ... FOR UPDATE and its commit).
    holder = engine.connect()
    holder_txn = holder.begin()
    locked = holder.execute(
        text("SELECT id FROM jobs WHERE id = :id FOR UPDATE"), {"id": first}
    ).scalar_one()
    assert locked == first

    db = factory()
    try:
        claimed = store.claim_one(db, worker_id="w-b", lease_seconds=30, now=T0)
        assert claimed is not None
        assert claimed.id == second  # stepped over the locked top candidate, no wait
        assert claimed.status == JobStatus.CLAIMED.value
    finally:
        db.close()

    holder_txn.rollback()
    holder.close()
    db = factory()
    try:
        remaining = store.claim_one(db, worker_id="w-a", lease_seconds=30, now=T0)
        assert remaining is not None and remaining.id == first
        assert store.claim_one(db, worker_id="w-c", lease_seconds=30, now=T0) is None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# M6 — concurrent lease recovery: one effect and one event per job; skip a locked row
# --------------------------------------------------------------------------- #
def test_concurrent_recovery_loops_recover_each_expired_job_exactly_once(store, factory):
    setup = factory()
    job_ids = [_enqueue(store, setup, f"r{i}") for i in range(6)]
    setup.close()
    for _ in job_ids:
        _claim_and_run(store, factory, worker="crashed")  # each lease will expire at LATER
    claimed = {jid: _row(factory, jid) for jid in job_ids}
    assert all(r.status == JobStatus.RUNNING.value for r in claimed.values())

    loops = 4
    barrier = threading.Barrier(loops)
    counts: list[int] = []
    lock = threading.Lock()

    def loop():
        db = factory()
        try:
            barrier.wait(timeout=JOIN_SECONDS)
            recovered = store.recover_expired_leases(db, now=LATER)
            db.commit()
            with lock:
                counts.append(recovered)
        finally:
            db.close()

    names = [f"loop{i}" for i in range(loops)]
    errors = _run_threads([loop for _ in names], names=names)
    assert errors == [], errors
    assert sum(counts) == len(job_ids)  # one recovery effect per job across all loops
    for job_id in job_ids:
        row = _row(factory, job_id)
        assert row.status == JobStatus.PENDING.value
        assert row.worker_id is None and row.lease_token is None and row.lease_expires_at is None
        recoveries = [e for e in _events(factory, job_id) if e.event_type == "lease_recovered"]
        assert len(recoveries) == 1  # exactly one audited recovery, never two
        assert recoveries[0].previous_status == JobStatus.RUNNING.value


def test_recovery_loop_skips_a_row_locked_by_an_overlapping_transaction(
    store, factory, engine
):
    setup = factory()
    job_ids = [_enqueue(store, setup, f"k{i}") for i in range(3)]
    setup.close()
    for _ in job_ids:
        _claim_and_run(store, factory, worker="crashed")
    held = job_ids[0]

    holder = engine.connect()
    holder_txn = holder.begin()
    holder.execute(text("SELECT id FROM jobs WHERE id = :id FOR UPDATE"), {"id": held}).scalar_one()

    db = factory()
    try:
        recovered = store.recover_expired_leases(db, now=LATER)
        db.commit()
    finally:
        db.close()
    assert recovered == len(job_ids) - 1  # the locked row was skipped, not waited for
    assert _row(factory, held).status == JobStatus.RUNNING.value
    assert all(_row(factory, j).status == JobStatus.PENDING.value for j in job_ids[1:])

    holder_txn.rollback()
    holder.close()
    db = factory()
    try:
        assert store.recover_expired_leases(db, now=LATER) == 1
        db.commit()
        assert store.recover_expired_leases(db, now=LATER) == 0  # nothing left to recover
        db.commit()
    finally:
        db.close()
    assert _row(factory, held).status == JobStatus.PENDING.value
    for j in job_ids:
        assert len([e for e in _events(factory, j) if e.event_type == "lease_recovered"]) == 1


# --------------------------------------------------------------------------- #
# M7 — retryable recovery vs exhausted attempts (dead-letter)
# --------------------------------------------------------------------------- #
def test_recovery_routes_retryable_to_pending_and_exhausted_to_dead_letter(store, factory):
    setup = factory()
    retryable = _enqueue(store, setup, "retryable", max_attempts=3)
    exhausted = _enqueue(store, setup, "exhausted", max_attempts=1)
    setup.close()
    _claim_and_run(store, factory, worker="A")
    _claim_and_run(store, factory, worker="A")
    assert _row(factory, exhausted).attempt_count == 1
    assert _row(factory, retryable).attempt_count == 1

    db = factory()
    try:
        assert store.recover_expired_leases(db, now=LATER) == 2
        db.commit()
    finally:
        db.close()

    r = _row(factory, retryable)
    assert r.status == JobStatus.PENDING.value and r.available_at is not None
    assert r.attempt_count == 1 and r.last_error_code is None
    assert [e.event_type for e in _events(factory, retryable)] == [
        "enqueued", "claimed", "started", "lease_recovered",
    ]

    d = _row(factory, exhausted)
    assert d.status == JobStatus.DEAD_LETTERED.value
    assert d.last_error_code == JobErrorCode.TIMEOUT.value
    assert d.completed_at is not None and d.worker_id is None and d.lease_token is None
    dead = [e for e in _events(factory, exhausted) if e.event_type == "dead_lettered"]
    assert len(dead) == 1 and dead[0].error_code == JobErrorCode.TIMEOUT.value

    # The recovered retryable job is claimable again; the dead-lettered one is not.
    db = factory()
    try:
        again = store.claim_one(db, worker_id="B", lease_seconds=30, now=LATER)
        assert again is not None and again.id == retryable
        assert store.claim_one(db, worker_id="C", lease_seconds=30, now=LATER) is None
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# M8 — stale-owner fencing after recovery / reclaim, on independent connections
# --------------------------------------------------------------------------- #
def test_stale_owner_is_fenced_after_recovery_and_reclaim(store, factory):
    setup = factory()
    job_id = _enqueue(store, setup, "fenced")
    setup.close()

    sa = factory()
    a_job = store.claim_one(sa, worker_id="A", lease_seconds=30, now=T0)
    assert a_job is not None and a_job.id == job_id
    a_wid, a_tok = a_job.worker_id, a_job.lease_token
    store.mark_running(sa, a_job, worker_id=a_wid, lease_token=a_tok, now=T0)
    sa.commit()

    sb = factory()
    assert store.recover_expired_leases(sb, now=LATER) == 1
    sb.commit()
    b_job = store.claim_one(sb, worker_id="B", lease_seconds=30, now=LATER)
    assert b_job is not None and b_job.id == job_id
    b_wid, b_tok = b_job.worker_id, b_job.lease_token
    assert (b_wid, b_tok) != (a_wid, a_tok) and b_tok is not None
    store.mark_running(sb, b_job, worker_id=b_wid, lease_token=b_tok, now=LATER)
    sb.commit()

    # Every stale mutation A attempts on its own connection is fenced out (0 rows).
    stale_mutations = (
        lambda: store.heartbeat(
            sa, a_job, worker_id=a_wid, lease_token=a_tok, lease_seconds=30, now=LATER
        ),
        lambda: store.complete(
            sa, a_job, worker_id=a_wid, lease_token=a_tok, result_summary={"stale": True}, now=LATER
        ),
        lambda: store.fail(
            sa, a_job, worker_id=a_wid, lease_token=a_tok,
            error=JobError(JobErrorCode.TRANSIENT), base_seconds=2, max_seconds=30, now=LATER,
        ),
        lambda: store.finish_cancel(sa, a_job, worker_id=a_wid, lease_token=a_tok, now=LATER),
    )
    for attempt in stale_mutations:
        sa.rollback()
        with pytest.raises(JobLeaseLostError):
            attempt()
    sa.rollback()
    sa.close()

    assert [e.event_type for e in _events(factory, job_id)] == [
        "enqueued", "claimed", "started", "lease_recovered", "claimed", "started",
    ]
    live = _row(factory, job_id)
    assert (live.status, live.worker_id, live.lease_token) == (
        JobStatus.RUNNING.value, b_wid, b_tok,
    )

    store.heartbeat(sb, b_job, worker_id=b_wid, lease_token=b_tok, lease_seconds=30, now=LATER)
    sb.commit()
    store.complete(
        sb, b_job, worker_id=b_wid, lease_token=b_tok, result_summary={"by": "B"}, now=LATER
    )
    sb.commit()
    sb.close()
    final = _row(factory, job_id)
    assert final.status == JobStatus.SUCCEEDED.value and final.result_summary == {"by": "B"}
    assert final.attempt_count == 2 and final.worker_id is None and final.lease_token is None
    terminal = [e for e in _events(factory, job_id) if e.event_type == "succeeded"]
    assert len(terminal) == 1 and terminal[0].worker_id == "B"


def test_same_worker_id_reclaim_is_fenced_by_the_lease_token_alone(store, factory):
    """A restarted worker reuses its worker_id: only the token can fence its old self.

    The fence compares ``worker_id`` AND ``lease_token``. With a different reclaiming
    worker the id alone would already reject the stale owner, so this test reclaims
    with the SAME id: the credential captured before the crash (old token) must be
    refused for every mutation while the fresh credential (new token) is accepted.
    A negative control that drops the token predicate from the fence passes the
    different-id test and fails exactly here.
    """
    setup = factory()
    job_id = _enqueue(store, setup, "restart")
    setup.close()
    _, wid, old_tok = _claim_and_run(store, factory, job_id, worker="restarting")
    db = factory()
    assert store.recover_expired_leases(db, now=LATER) == 1
    db.commit()
    db.close()
    _, wid2, new_tok = _claim_and_run(store, factory, job_id, worker="restarting", now=LATER)
    assert wid2 == wid and new_tok != old_tok

    stale = factory()
    job = stale.get(Job, job_id)
    for attempt in (
        lambda: store.heartbeat(
            stale, job, worker_id=wid, lease_token=old_tok, lease_seconds=600, now=LATER
        ),
        lambda: store.complete(
            stale, job, worker_id=wid, lease_token=old_tok, result_summary={"old": 1}, now=LATER
        ),
        lambda: store.fail(
            stale, job, worker_id=wid, lease_token=old_tok,
            error=JobError(JobErrorCode.TRANSIENT), base_seconds=2, max_seconds=30, now=LATER,
        ),
    ):
        stale.rollback()
        with pytest.raises(JobLeaseLostError):
            attempt()
    stale.rollback()
    stale.close()

    live = _row(factory, job_id)
    assert (live.status, live.worker_id, live.lease_token) == (
        JobStatus.RUNNING.value, wid, new_tok,
    )
    # The stale 600 s heartbeat did not land: the lease is still the reclaim's 30 s one.
    assert live.lease_expires_at == LATER + timedelta(seconds=30)
    assert [e.event_type for e in _events(factory, job_id)] == [
        "enqueued", "claimed", "started", "lease_recovered", "claimed", "started",
    ]
    owner = factory()
    job = owner.get(Job, job_id)
    store.complete(
        owner, job, worker_id=wid, lease_token=new_tok, result_summary={"new": 1}, now=LATER
    )
    owner.commit()
    owner.close()
    final = _row(factory, job_id)
    assert final.status == JobStatus.SUCCEEDED.value and final.result_summary == {"new": 1}


def test_concurrent_stale_heartbeats_never_extend_a_reclaimed_lease(store, factory):
    """Many stale heartbeats race the new owner's: only the owner's lease moves."""
    setup = factory()
    job_id = _enqueue(store, setup, "hb")
    setup.close()
    _, a_wid, a_tok = _claim_and_run(store, factory, job_id, worker="A")
    db = factory()
    assert store.recover_expired_leases(db, now=LATER) == 1
    db.commit()
    db.close()
    _, b_wid, b_tok = _claim_and_run(store, factory, job_id, worker="B", now=LATER)
    b_expiry = _row(factory, job_id).lease_expires_at

    stale_losses: list[str] = []
    lock = threading.Lock()
    barrier = threading.Barrier(5)

    def stale(n: int):
        s = factory()
        try:
            job = s.get(Job, job_id)
            barrier.wait(timeout=JOIN_SECONDS)
            try:
                store.heartbeat(
                    s, job, worker_id=a_wid, lease_token=a_tok, lease_seconds=600, now=LATER
                )
                s.commit()
            except JobLeaseLostError:
                s.rollback()
                with lock:
                    stale_losses.append(f"s{n}")
        finally:
            s.close()

    def owner():
        s = factory()
        try:
            job = s.get(Job, job_id)
            barrier.wait(timeout=JOIN_SECONDS)
            store.heartbeat(
                s, job, worker_id=b_wid, lease_token=b_tok, lease_seconds=120, now=LATER
            )
            s.commit()
        finally:
            s.close()

    errors = _run_threads(
        [lambda n=n: stale(n) for n in range(4)] + [owner],
        names=[f"s{n}" for n in range(4)] + ["owner"],
    )
    assert errors == [], errors
    assert sorted(stale_losses) == ["s0", "s1", "s2", "s3"]
    final = _row(factory, job_id)
    assert final.lease_expires_at == LATER + timedelta(seconds=120)  # the owner's, not 600 s
    assert final.lease_expires_at != b_expiry
    assert (final.worker_id, final.lease_token) == (b_wid, b_tok)


# --------------------------------------------------------------------------- #
# M9 — unexpired, future-scheduled and terminal jobs are untouched by concurrent recovery
# --------------------------------------------------------------------------- #
def test_concurrent_recovery_leaves_unexpired_scheduled_and_terminal_jobs_alone(
    store, factory
):
    setup = factory()
    expired = _enqueue(store, setup, "expired")
    unexpired = _enqueue(store, setup, "unexpired")
    future = _enqueue(store, setup, "future", scheduled_for=LATER + timedelta(days=1))
    done = _enqueue(store, setup, "done")
    failed = _enqueue(store, setup, "failed")
    cancelled = _enqueue(store, setup, "cancelled")
    setup.close()

    # expired: claimed+running at T0 with a 30 s lease -> expired at LATER
    _claim_and_run(store, factory, worker="A")  # claims 'expired' (FIFO: oldest available first)
    # unexpired: claimed+running at LATER-1s with a long lease
    _claim_and_run(store, factory, worker="A", now=LATER - timedelta(seconds=1), lease=3600)
    # done / failed: run to terminal states
    db = factory()
    j = store.claim_one(db, worker_id="A", lease_seconds=30, now=T0)
    store.mark_running(db, j, worker_id=j.worker_id, lease_token=j.lease_token, now=T0)
    store.complete(
        db, j, worker_id=j.worker_id, lease_token=j.lease_token, result_summary={}, now=T0
    )
    db.commit()
    j2 = store.claim_one(db, worker_id="A", lease_seconds=30, now=T0)
    store.mark_running(db, j2, worker_id=j2.worker_id, lease_token=j2.lease_token, now=T0)
    store.fail(
        db, j2, worker_id=j2.worker_id, lease_token=j2.lease_token,
        error=JobError(JobErrorCode.VALIDATION), base_seconds=1, max_seconds=2, now=T0,
    )
    db.commit()
    j3 = db.get(Job, cancelled)
    store.request_cancel(db, j3, now=T0)
    db.commit()
    db.close()
    population = (expired, unexpired, future, done, failed, cancelled)
    before = {jid: (_row(factory, jid).status, len(_events(factory, jid))) for jid in population}
    assert [before[j][0] for j in population] == [
        JobStatus.RUNNING.value, JobStatus.RUNNING.value, JobStatus.SCHEDULED.value,
        JobStatus.SUCCEEDED.value, JobStatus.FAILED.value, JobStatus.CANCELLED.value,
    ]

    barrier = threading.Barrier(3)
    counts: list[int] = []
    lock = threading.Lock()

    def loop():
        s = factory()
        try:
            barrier.wait(timeout=JOIN_SECONDS)
            n = store.recover_expired_leases(s, now=LATER)
            s.commit()
            with lock:
                counts.append(n)
        finally:
            s.close()

    errors = _run_threads([loop, loop, loop], names=["l1", "l2", "l3"])
    assert errors == [], errors
    assert sum(counts) == 1  # only the expired job
    assert _row(factory, expired).status == JobStatus.PENDING.value
    for jid in (unexpired, future, done, failed, cancelled):
        assert (_row(factory, jid).status, len(_events(factory, jid))) == before[jid]
    live = _row(factory, unexpired)
    assert live.worker_id == "A" and live.lease_token is not None
