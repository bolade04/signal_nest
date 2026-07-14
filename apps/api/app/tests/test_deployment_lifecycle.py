"""Batch 4 — production lifecycle, shutdown and schema-compatibility tests.

These cover the deployment-hardening behavior added in Phase 3A.4b Batch 4:

* the read-only schema-compatibility classifier and the startup gate,
* the single-actor migration ``check`` command's exit contract,
* the shared bounded/idempotent shutdown sequence (telemetry flush + resource
  close that never raises), and
* the worker's second-signal-shortens-grace drain (in-flight work abandoned
  within the forced grace, its lease left to expire and be recovered).

They use injected engines/clocks and tiny bounded waits — never multi-second real
sleeps — so they stay deterministic.
"""

from __future__ import annotations

import threading
import time

import pytest
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.db.schema import (
    SchemaNotReadyError,
    SchemaState,
    check_schema_compatibility,
    code_head_revision,
    require_startup_schema,
)


# --------------------------------------------------------------------------- #
# Schema-compatibility classifier
# --------------------------------------------------------------------------- #
def _engine_with_revision(revision: str | None):
    """An isolated in-memory SQLite engine, optionally stamped to ``revision``."""
    engine = create_engine("sqlite://")
    if revision is not None:
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)"))
            conn.execute(
                text("INSERT INTO alembic_version (version_num) VALUES (:r)"), {"r": revision}
            )
    return engine


def test_schema_uninitialized_when_no_alembic_version():
    compat = check_schema_compatibility(_engine_with_revision(None))
    assert compat.state is SchemaState.UNINITIALIZED
    assert compat.db_revision is None
    assert not compat.is_startup_safe


def test_schema_compatible_at_code_head():
    head = code_head_revision()
    compat = check_schema_compatibility(_engine_with_revision(head))
    assert compat.state is SchemaState.COMPATIBLE
    assert compat.db_revision == head
    assert compat.is_startup_safe


def test_schema_pending_when_behind_head():
    # The initial migration is a strict ancestor of the current head.
    compat = check_schema_compatibility(_engine_with_revision("9a7c614699d8"))
    assert compat.state is SchemaState.PENDING
    assert not compat.is_startup_safe


def test_schema_ahead_when_revision_unknown_to_code():
    # A revision the code has never seen => a newer schema (additive-first safe).
    compat = check_schema_compatibility(_engine_with_revision("ffffffffffff"))
    assert compat.state is SchemaState.AHEAD
    assert compat.is_startup_safe


def test_require_startup_schema_raises_on_pending():
    settings = Settings()
    with pytest.raises(SchemaNotReadyError, match="behind"):
        require_startup_schema(_engine_with_revision("9a7c614699d8"), settings=settings)


def test_require_startup_schema_raises_on_uninitialized():
    settings = Settings()
    with pytest.raises(SchemaNotReadyError, match="not initialized"):
        require_startup_schema(_engine_with_revision(None), settings=settings)


def test_require_startup_schema_passes_when_ahead():
    settings = Settings()
    compat = require_startup_schema(_engine_with_revision("ffffffffffff"), settings=settings)
    assert compat.state is SchemaState.AHEAD


# --------------------------------------------------------------------------- #
# Single-actor migration command
# --------------------------------------------------------------------------- #
def test_migrate_check_reports_compatible_and_exits_zero():
    # The repo's seeded SQLite DB is stamped at head, so ``check`` is startup-safe.
    from app.db import migrate

    assert migrate.check() == 0


def test_migrate_downgrade_requires_explicit_target():
    from app.db import migrate

    with pytest.raises(SystemExit):
        migrate.main(["downgrade", "head"])


# --------------------------------------------------------------------------- #
# Shared bounded shutdown
# --------------------------------------------------------------------------- #
def test_graceful_shutdown_is_idempotent_and_never_raises():
    from app.core import lifecycle

    settings = Settings()
    # Two consecutive calls (a second SIGTERM, or shutdown then atexit) must be safe.
    lifecycle.graceful_shutdown(settings)
    lifecycle.graceful_shutdown(settings)


def test_flush_telemetry_swallows_backend_flush_failure(monkeypatch):
    from app.core import lifecycle, metrics

    class _Boom:
        def flush(self) -> None:
            raise RuntimeError("exporter down")

    monkeypatch.setattr(metrics, "_backend", _Boom())
    # A failing flush is isolated: flush_telemetry must not propagate it.
    lifecycle.flush_telemetry(Settings())


def test_service_lifecycle_metrics_recorded_across_app_boot():
    from fastapi.testclient import TestClient

    from app.core import metrics
    from app.core.metrics import SERVICE_SHUTDOWNS_TOTAL, SERVICE_STARTUPS_TOTAL, InMemoryMetrics
    from app.main import app

    # The lifespan labels the metric with the live settings, so derive the
    # expected service/environment from Settings rather than hardcoding them
    # (CI runs with ENVIRONMENT=test, local defaults to development).
    settings = Settings()
    backend = InMemoryMetrics()
    metrics.configure_metrics(backend)
    try:
        with TestClient(app):
            pass  # entering + leaving drives lifespan startup + shutdown
    finally:
        metrics.configure_metrics(None)

    assert backend.counter_value(SERVICE_STARTUPS_TOTAL, service=settings.service_name,
                                 environment=settings.environment, outcome="ready") == 1
    assert backend.counter_value(SERVICE_SHUTDOWNS_TOTAL, service=settings.service_name,
                                 environment=settings.environment, outcome="clean") == 1


# --------------------------------------------------------------------------- #
# Worker draining — second signal shortens the grace
# --------------------------------------------------------------------------- #
def _quiet_worker():
    from app.jobs.worker import Worker

    # Constructing the worker touches no database (registration happens in run()).
    return Worker(settings=Settings())


def test_second_signal_escalates_to_forced_grace():
    worker = _quiet_worker()
    # First signal begins draining at the normal grace.
    worker.request_stop(signum=15)
    assert worker._stop.is_set()
    assert not worker._fast_stop.is_set()
    assert worker._resolve_grace() == worker._settings.worker_shutdown_grace_seconds
    # Second signal escalates to the shortened, forced grace.
    worker.request_stop(signum=15)
    assert worker._fast_stop.is_set()
    assert worker._resolve_grace() == worker._settings.worker_force_shutdown_grace_seconds


def test_forced_join_abandons_in_flight_work_within_budget():
    worker = _quiet_worker()
    # A long normal grace that we must NOT wait for, and a tiny forced grace.
    worker._settings = Settings(
        worker_shutdown_grace_seconds=30.0, worker_force_shutdown_grace_seconds=0.05
    )
    blocked = threading.Event()  # never set => the "in-flight job" runs forever
    t = threading.Thread(target=blocked.wait, name="stuck-job", daemon=True)
    t.start()
    worker._threads = [t]

    # Escalate to forced stop, then join must return within the forced budget
    # rather than the 30s normal grace; the in-flight thread is abandoned (still
    # alive), so on real process exit its daemon thread dies and its DB lease
    # expires — the next worker recovers the job.
    worker.request_stop(signum=15)
    worker.request_stop(signum=15)
    started = time.monotonic()
    worker.join()
    elapsed = time.monotonic() - started

    assert elapsed < 5.0  # nowhere near the 30s normal grace
    assert t.is_alive()  # in-flight work was abandoned, not awaited
    blocked.set()  # release the stuck thread so the test process stays clean
