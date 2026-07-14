"""Worker-fleet foundation tests (Phase 3A.4a).

Three layers, none touching a real external service:

* **Lifecycle** — the explicit worker status transition map (legal/illegal edges,
  terminal state) as a pure, DB-free contract.
* **Registry** — registration + self-replacement, lifecycle marks, bounded
  heartbeats (no audit rows), stale detection/sweep and operator-facing counts,
  driven by an injected clock against a throwaway SQLite database.
* **Readiness** — the worker-registry probe's policy gate: informational by
  default, blocking only when ``require_worker_fleet`` is enabled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.db.base import Base
from app.jobs.worker_registry import WorkerRegistry
from app.jobs.worker_status import (
    InvalidWorkerTransition,
    WorkerStatus,
    can_worker_transition,
    ensure_worker_transition,
    is_worker_terminal,
)

# Importing the app registers every ORM model on the shared Base.
from app.main import app  # noqa: F401


# --------------------------------------------------------------------------- #
# Lifecycle transitions (pure)
# --------------------------------------------------------------------------- #
def test_worker_transition_legal_edges() -> None:
    assert can_worker_transition(WorkerStatus.STARTING, WorkerStatus.READY)
    assert can_worker_transition(WorkerStatus.READY, WorkerStatus.BUSY)
    assert can_worker_transition(WorkerStatus.BUSY, WorkerStatus.DRAINING)
    assert can_worker_transition(WorkerStatus.STALE, WorkerStatus.READY)  # recovery
    assert can_worker_transition(WorkerStatus.DRAINING, WorkerStatus.STOPPED)


def test_worker_transition_illegal_edges() -> None:
    # A starting worker cannot jump straight to busy without becoming ready.
    assert not can_worker_transition(WorkerStatus.STARTING, WorkerStatus.BUSY)
    # Stopped is terminal — nothing revives that registration.
    assert not can_worker_transition(WorkerStatus.STOPPED, WorkerStatus.READY)
    with pytest.raises(InvalidWorkerTransition):
        ensure_worker_transition(WorkerStatus.STOPPED, WorkerStatus.READY)


def test_stopped_is_the_only_terminal_state() -> None:
    assert is_worker_terminal(WorkerStatus.STOPPED)
    for s in (WorkerStatus.STALE, WorkerStatus.FAILED, WorkerStatus.DRAINING):
        assert not is_worker_terminal(s)


# --------------------------------------------------------------------------- #
# Registry (DB-backed, injected clock)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def db(tmp_path) -> Session:
    engine = create_engine(
        f"sqlite:///{tmp_path/'workers.db'}",
        connect_args={"check_same_thread": False},
        future=True,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    s = factory()
    try:
        yield s
    finally:
        s.close()
        engine.dispose()


@pytest.fixture()
def registry() -> WorkerRegistry:
    return WorkerRegistry()


def _register(registry: WorkerRegistry, db: Session, worker_id: str, *, now=None):
    return registry.register(
        db,
        worker_id=worker_id,
        worker_type="durable-jobs",
        concurrency=4,
        supported_job_types=["run_scout_request"],
        queue_backend="inprocess",
        application_version="1.2.3",
        now=now,
    )


def test_register_initializes_to_starting(registry, db) -> None:
    row = _register(registry, db, "w-1")
    assert row.status == WorkerStatus.STARTING.value
    assert row.started_at is not None
    assert row.last_heartbeat_at is not None
    assert row.stopped_at is None


def test_register_is_self_replacing_on_restart(registry, db) -> None:
    _register(registry, db, "w-1")
    registry.mark_ready(db, "w-1")
    registry.mark_busy(db, "w-1")
    # A restart of the same worker id re-initializes the same row to STARTING.
    row = _register(registry, db, "w-1")
    assert row.status == WorkerStatus.STARTING.value
    assert registry.list_workers(db) == [row]  # no duplicate row accumulated


def test_lifecycle_marks_and_stopped_sets_stopped_at(registry, db) -> None:
    _register(registry, db, "w-1")
    registry.mark_ready(db, "w-1")
    assert registry.get(db, "w-1").status == WorkerStatus.READY.value
    registry.mark_draining(db, "w-1")
    stopped = registry.mark_stopped(db, "w-1")
    assert stopped.status == WorkerStatus.STOPPED.value
    assert stopped.stopped_at is not None


def test_heartbeat_updates_liveness_without_audit(registry, db) -> None:
    t0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
    _register(registry, db, "w-1", now=t0)
    registry.mark_ready(db, "w-1", now=t0)
    t1 = t0 + timedelta(seconds=5)
    row = registry.heartbeat(db, "w-1", now=t1)
    assert row.last_heartbeat_at == t1
    assert row.status == WorkerStatus.READY.value


def test_stale_worker_recovers_to_ready_on_heartbeat(registry, db) -> None:
    t0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
    _register(registry, db, "w-1", now=t0)
    registry.mark_ready(db, "w-1", now=t0)
    # Sweep with a threshold shorter than the elapsed gap flags it stale.
    later = t0 + timedelta(seconds=120)
    flagged = registry.sweep_stale(db, stale_after_seconds=60, now=later)
    assert flagged == 1
    assert registry.get(db, "w-1").status == WorkerStatus.STALE.value
    # A fresh heartbeat recovers it to ready.
    recovered = registry.heartbeat(db, "w-1", now=later + timedelta(seconds=1))
    assert recovered.status == WorkerStatus.READY.value


def test_sweep_excludes_stopped_and_is_idempotent(registry, db) -> None:
    t0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
    _register(registry, db, "live", now=t0)
    registry.mark_ready(db, "live", now=t0)
    _register(registry, db, "gone", now=t0)
    registry.mark_ready(db, "gone", now=t0)
    registry.mark_draining(db, "gone", now=t0)
    registry.mark_stopped(db, "gone", now=t0)
    later = t0 + timedelta(seconds=120)
    assert registry.sweep_stale(db, stale_after_seconds=60, now=later) == 1  # only 'live'
    # Re-sweeping does not re-flag the already-stale row.
    assert registry.sweep_stale(db, stale_after_seconds=60, now=later) == 0


def test_counts_reflect_fleet_state(registry, db) -> None:
    t0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
    _register(registry, db, "a", now=t0)
    registry.mark_ready(db, "a", now=t0)
    _register(registry, db, "b", now=t0)
    registry.mark_ready(db, "b", now=t0)
    registry.mark_busy(db, "b", now=t0)
    assert registry.active_count(db, stale_after_seconds=60, now=t0) == 2  # ready + busy
    # A worker overdue for a heartbeat is counted stale even before the sweep.
    later = t0 + timedelta(seconds=120)
    assert registry.stale_count(db, stale_after_seconds=60, now=later) == 2
    counts = registry.status_counts(db)
    assert counts[WorkerStatus.READY.value] == 1
    assert counts[WorkerStatus.BUSY.value] == 1


def test_heartbeat_for_unknown_worker_returns_none(registry, db) -> None:
    assert registry.heartbeat(db, "nope") is None
    assert registry.mark_ready(db, "nope") is None


# --------------------------------------------------------------------------- #
# Readiness probe policy gate
# --------------------------------------------------------------------------- #
def test_worker_probe_informational_by_default(db, monkeypatch) -> None:
    from app.core.config import Settings
    from app.system import probes

    # Point the probe's engine/session at the throwaway DB.
    monkeypatch.setattr(probes, "engine", db.get_bind())
    monkeypatch.setattr(probes, "SessionLocal", sessionmaker(bind=db.get_bind(), future=True))
    settings = Settings(_env_file=None)  # require_worker_fleet defaults False
    status, summary, _detail, _retry = probes._check_worker_registry(settings)
    assert status is probes.ProbeStatus.HEALTHY
    assert "informational" in summary


def test_worker_probe_blocks_when_fleet_required_and_absent(db, monkeypatch) -> None:
    from app.core.config import Settings
    from app.system import probes

    monkeypatch.setattr(probes, "engine", db.get_bind())
    monkeypatch.setattr(probes, "SessionLocal", sessionmaker(bind=db.get_bind(), future=True))
    settings = Settings(_env_file=None, require_worker_fleet=True)
    status, _summary, _detail, retry = probes._check_worker_registry(settings)
    assert status is probes.ProbeStatus.UNAVAILABLE
    assert retry is True


def test_worker_probe_healthy_when_required_and_active(db, monkeypatch, registry) -> None:
    from app.core.config import Settings
    from app.system import probes

    _register(registry, db, "w-1")
    registry.mark_ready(db, "w-1")
    db.commit()
    monkeypatch.setattr(probes, "engine", db.get_bind())
    monkeypatch.setattr(probes, "SessionLocal", sessionmaker(bind=db.get_bind(), future=True))
    settings = Settings(_env_file=None, require_worker_fleet=True)
    status, _summary, _detail, _retry = probes._check_worker_registry(settings)
    assert status is probes.ProbeStatus.HEALTHY


def test_worker_probe_unhealthy_when_only_worker_is_overdue_unswept(
    db, monkeypatch, registry
) -> None:
    # M1 regression: with require_worker_fleet enabled, a ready worker whose
    # heartbeat is older than the stale threshold must NOT satisfy readiness, even
    # if no sweep has flagged it stale yet. This is the real failure mode: when the
    # whole fleet dies there is no worker left to run sweep_stale, so readiness has
    # to derive liveness from heartbeat age directly. Deliberately no sweep_stale().
    from datetime import timedelta

    from app.core.config import Settings
    from app.jobs.store import utcnow
    from app.system import probes

    overdue = utcnow() - timedelta(seconds=120)
    _register(registry, db, "w-1", now=overdue)
    registry.mark_ready(db, "w-1", now=overdue)  # status ready, heartbeat still 120s old
    db.commit()
    monkeypatch.setattr(probes, "engine", db.get_bind())
    monkeypatch.setattr(probes, "SessionLocal", sessionmaker(bind=db.get_bind(), future=True))
    settings = Settings(
        _env_file=None, require_worker_fleet=True, worker_stale_after_seconds=60.0
    )
    status, _summary, _detail, retry = probes._check_worker_registry(settings)
    assert status is probes.ProbeStatus.UNAVAILABLE
    assert retry is True


def test_worker_probe_healthy_when_fresh_busy(db, monkeypatch, registry) -> None:
    # A busy worker with a fresh heartbeat is an active worker.
    from app.core.config import Settings
    from app.system import probes

    _register(registry, db, "w-1")
    registry.mark_ready(db, "w-1")
    registry.mark_busy(db, "w-1")
    db.commit()
    monkeypatch.setattr(probes, "engine", db.get_bind())
    monkeypatch.setattr(probes, "SessionLocal", sessionmaker(bind=db.get_bind(), future=True))
    settings = Settings(_env_file=None, require_worker_fleet=True)
    status, _summary, _detail, _retry = probes._check_worker_registry(settings)
    assert status is probes.ProbeStatus.HEALTHY


def test_worker_probe_unhealthy_when_only_worker_is_overdue_busy_unswept(
    db, monkeypatch, registry
) -> None:
    # Same failure mode as the ready case, for a worker stuck BUSY: an overdue
    # heartbeat must not satisfy readiness even though the status is active.
    from datetime import timedelta

    from app.core.config import Settings
    from app.jobs.store import utcnow
    from app.system import probes

    overdue = utcnow() - timedelta(seconds=120)
    _register(registry, db, "w-1", now=overdue)
    registry.mark_ready(db, "w-1", now=overdue)
    registry.mark_busy(db, "w-1", now=overdue)  # active status, heartbeat 120s old
    db.commit()
    monkeypatch.setattr(probes, "engine", db.get_bind())
    monkeypatch.setattr(probes, "SessionLocal", sessionmaker(bind=db.get_bind(), future=True))
    settings = Settings(
        _env_file=None, require_worker_fleet=True, worker_stale_after_seconds=60.0
    )
    status, _summary, _detail, retry = probes._check_worker_registry(settings)
    assert status is probes.ProbeStatus.UNAVAILABLE
    assert retry is True


def test_worker_probe_unhealthy_when_only_worker_stopped(
    db, monkeypatch, registry
) -> None:
    # A stopped worker is terminal and never counts as active, even with a
    # fresh heartbeat timestamp.
    from app.core.config import Settings
    from app.system import probes

    _register(registry, db, "w-1")
    registry.mark_ready(db, "w-1")
    registry.mark_draining(db, "w-1")
    registry.mark_stopped(db, "w-1")
    db.commit()
    monkeypatch.setattr(probes, "engine", db.get_bind())
    monkeypatch.setattr(probes, "SessionLocal", sessionmaker(bind=db.get_bind(), future=True))
    settings = Settings(_env_file=None, require_worker_fleet=True)
    status, _summary, _detail, retry = probes._check_worker_registry(settings)
    assert status is probes.ProbeStatus.UNAVAILABLE
    assert retry is True


def test_worker_probe_unhealthy_when_only_worker_explicitly_stale(
    db, monkeypatch, registry
) -> None:
    # The already-swept counterpart of the unswept regression: a worker flagged
    # STALE (status changed, heartbeat old) is not active.
    from datetime import timedelta

    from app.core.config import Settings
    from app.jobs.store import utcnow
    from app.system import probes

    overdue = utcnow() - timedelta(seconds=120)
    _register(registry, db, "w-1", now=overdue)
    registry.mark_ready(db, "w-1", now=overdue)
    registry.sweep_stale(db, stale_after_seconds=60, now=utcnow())
    assert registry.get(db, "w-1").status == WorkerStatus.STALE.value
    db.commit()
    monkeypatch.setattr(probes, "engine", db.get_bind())
    monkeypatch.setattr(probes, "SessionLocal", sessionmaker(bind=db.get_bind(), future=True))
    settings = Settings(
        _env_file=None, require_worker_fleet=True, worker_stale_after_seconds=60.0
    )
    status, _summary, _detail, retry = probes._check_worker_registry(settings)
    assert status is probes.ProbeStatus.UNAVAILABLE
    assert retry is True


def test_worker_probe_healthy_with_mixed_fresh_and_stale(
    db, monkeypatch, registry
) -> None:
    # One fresh active worker is enough for readiness even when the fleet also
    # holds a dead/overdue worker.
    from datetime import timedelta

    from app.core.config import Settings
    from app.jobs.store import utcnow
    from app.system import probes

    overdue = utcnow() - timedelta(seconds=120)
    _register(registry, db, "dead", now=overdue)
    registry.mark_ready(db, "dead", now=overdue)
    _register(registry, db, "live")  # fresh registration + heartbeat
    registry.mark_ready(db, "live")
    db.commit()
    monkeypatch.setattr(probes, "engine", db.get_bind())
    monkeypatch.setattr(probes, "SessionLocal", sessionmaker(bind=db.get_bind(), future=True))
    settings = Settings(
        _env_file=None, require_worker_fleet=True, worker_stale_after_seconds=60.0
    )
    status, _summary, _detail, _retry = probes._check_worker_registry(settings)
    assert status is probes.ProbeStatus.HEALTHY


# --------------------------------------------------------------------------- #
# Active-count freshness boundary + dialect portability (registry level)
# --------------------------------------------------------------------------- #
def test_active_count_boundary_is_exact_complement_of_stale(registry, db) -> None:
    # The active predicate (last_heartbeat_at >= cutoff) is the exact complement
    # of find_stale (< cutoff): at the exact threshold the worker is still active
    # and not stale; one microsecond past, it flips to overdue and not active.
    t0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
    _register(registry, db, "edge", now=t0)
    registry.mark_ready(db, "edge", now=t0)
    stale_after = 60
    at_cutoff = t0 + timedelta(seconds=stale_after)
    assert registry.active_count(db, stale_after_seconds=stale_after, now=at_cutoff) == 1
    assert registry.find_stale(db, stale_after_seconds=stale_after, now=at_cutoff) == []
    past = at_cutoff + timedelta(microseconds=1)
    assert registry.active_count(db, stale_after_seconds=stale_after, now=past) == 0
    assert len(registry.find_stale(db, stale_after_seconds=stale_after, now=past)) == 1


def test_active_count_query_compiles_for_postgresql(registry) -> None:
    # The fix must be dialect-portable: the freshness predicate has to compile
    # against PostgreSQL (production) as well as the SQLite test backend.
    from sqlalchemy import func, select
    from sqlalchemy.dialects import postgresql

    from app.jobs.store import utcnow

    conds = registry._fresh_active_conditions(stale_after_seconds=60, now=utcnow())
    stmt = select(func.count()).where(*conds)
    compiled = str(stmt.compile(dialect=postgresql.dialect()))
    assert "worker_registrations" in compiled


def test_duplicate_registration_cannot_forge_freshness(registry, db) -> None:
    # Residual-risk documentation for the deferred generation-token follow-up:
    # re-registering a known worker_id self-replaces the row and resets both its
    # heartbeat AND its status to STARTING. It therefore cannot make an overdue
    # worker look active without a legitimate fresh heartbeat and mark_ready — so
    # the freshness guarantee holds today without a generation token.
    t0 = datetime(2026, 7, 13, 12, 0, 0, tzinfo=UTC)
    _register(registry, db, "w-1", now=t0)
    registry.mark_ready(db, "w-1", now=t0)
    later = t0 + timedelta(seconds=120)
    assert registry.active_count(db, stale_after_seconds=60, now=later) == 0
    # Re-registration at `later` resets status to STARTING (not active).
    _register(registry, db, "w-1", now=later)
    assert registry.get(db, "w-1").status == WorkerStatus.STARTING.value
    assert registry.active_count(db, stale_after_seconds=60, now=later) == 0
    # Only a legitimate mark_ready with a fresh heartbeat makes it active.
    registry.mark_ready(db, "w-1", now=later)
    assert registry.active_count(db, stale_after_seconds=60, now=later) == 1
