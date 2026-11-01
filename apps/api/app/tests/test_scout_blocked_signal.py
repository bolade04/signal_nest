"""FU-267-A1 (PROPOSED): the blocked-Scout signal where no PostgreSQL is needed.

* On SQLite the admission ledger is not kept, so the signal is ABSENT - no gauge is set and the
  operator overview reports ``ledger_kept: false`` with nulls - never a zero, even when an
  eligible row exists (rows are inserted directly here, as a run never writes them on SQLite).
* With metrics off (the no-op backend) a sweep reads nothing at all.
* The gauges carry no label, pass ``validate_metric`` and read 0 / 0 for no blocked Scout.
* A failed read is logged by class and never raises; a failing signal never stops the worker's
  registry heartbeat.

The PostgreSQL values (0, 1, several across two organizations; reconciliation; healthy
operations; cost; the heartbeat schedule) are in ``test_scout_blocked_signal_postgres.py``.
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.core.metrics import (
    SCOUTS_BLOCKED,
    SCOUTS_BLOCKED_OLDEST_AGE_SECONDS,
    InMemoryMetrics,
    MetricError,
    configure_metrics,
    validate_metric,
)
from app.db.models import Base
from app.jobs import worker as worker_mod
from app.jobs.blocked_signal import record_blocked_scouts, set_blocked_gauges
from app.jobs.reconcile import blocked
from app.jobs.worker import Worker
from app.scouting_requests.execution import BlockedScouts, blocked_scouts, ledger_kept
from app.system.internal_observability_routes import internal_overview
from app.tests import test_scout_pause_promise as promise
from app.tests.test_scout_recovery import _adm, _req

factory = promise.factory
s = promise.s
flag_on = promise.flag_on


@pytest.fixture
def metrics():
    backend = InMemoryMetrics()
    configure_metrics(backend)
    try:
        yield backend
    finally:
        configure_metrics(None)


def test_on_sqlite_the_signal_is_absent_never_zero(factory, s, metrics):
    """An eligible row exists (inserted directly) and the CLI lists it, yet the signal is
    absent: SQLite keeps no ledger, so its count would be a claim about a bound that does not
    exist there."""
    row = _adm(s, _req(s), end_kind="unknown")
    s.commit()
    try:
        assert [i["admission_id"] for i in blocked(s)["blocked"]] == [row.id]
        assert ledger_kept(s) is False
        assert blocked_scouts(s) is None
        assert record_blocked_scouts(factory) is None
        assert metrics.gauge_value(SCOUTS_BLOCKED) is None
        assert metrics.gauge_value(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS) is None
        assert internal_overview(db=s, _operator=None).scouts.model_dump() == {
            "ledger_kept": False,
            "blocked_count": None,
            "oldest_blocked_since": None,
            "oldest_blocked_age_seconds": None,
        }
    finally:
        s.delete(row)
        s.commit()


def test_with_metrics_off_a_sweep_reads_nothing():
    configure_metrics(None)  # the no-op default

    def no_session():
        raise AssertionError("a sweep with metrics off must not open a session")

    assert record_blocked_scouts(no_session) is None


def test_the_gauges_carry_no_label_and_read_zero_for_none(metrics):
    for name in (SCOUTS_BLOCKED, SCOUTS_BLOCKED_OLDEST_AGE_SECONDS):
        validate_metric(name, {})
        for forbidden in ("organization_id", "workspace_id", "scout_request_id", "admission_id"):
            with pytest.raises(MetricError):
                validate_metric(name, {forbidden: "x"})
    now = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)
    set_blocked_gauges(metrics, BlockedScouts(count=0, oldest_since=None, as_of=now))
    assert metrics.gauge_value(SCOUTS_BLOCKED) == 0.0
    assert metrics.gauge_value(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS) == 0.0
    blocked_since = now - timedelta(minutes=90)
    set_blocked_gauges(metrics, BlockedScouts(count=3, oldest_since=blocked_since, as_of=now))
    assert metrics.gauge_value(SCOUTS_BLOCKED) == 3.0
    assert metrics.gauge_value(SCOUTS_BLOCKED_OLDEST_AGE_SECONDS) == 5400.0
    assert set(metrics._gauges) == {(SCOUTS_BLOCKED, ()), (SCOUTS_BLOCKED_OLDEST_AGE_SECONDS, ())}
    # A clock skew can never report a negative age.
    later = BlockedScouts(count=1, oldest_since=now + timedelta(seconds=5), as_of=now)
    assert later.oldest_age_seconds == 0.0


class _Dialect:
    name = "postgresql"


class _Bind:
    dialect = _Dialect()


class _FailingSession:
    """A PostgreSQL-looking session whose every statement fails (a lost connection)."""

    def __init__(self):
        self.closed = False

    def get_bind(self):
        return _Bind()

    def execute(self, *_args, **_kwargs):
        raise ConnectionError("server closed the connection unexpectedly")

    def rollback(self):
        raise ConnectionError("no connection to roll back")

    def close(self):
        self.closed = True


def test_a_failed_read_is_logged_by_class_and_never_raises(metrics, caplog):
    session = _FailingSession()
    with caplog.at_level(logging.WARNING, logger="signalnest.jobs.blocked_signal"):
        assert record_blocked_scouts(lambda: session) is None
    assert session.closed is True
    assert metrics.gauge_value(SCOUTS_BLOCKED) is None  # no stale or invented value
    messages = [r.getMessage() for r in caplog.records]
    assert any("worker.blocked_scouts_sweep_failed" in m for m in messages), messages
    assert not any("server closed the connection" in m for m in messages)


def test_a_failing_signal_never_stops_the_registry_heartbeat(tmp_path, metrics, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'hb.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    make = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    calls: list[float] = []
    enough = threading.Event()

    def explode(_factory):
        calls.append(time.monotonic())
        if len(calls) >= 3:
            enough.set()
        raise RuntimeError("signal defect")

    monkeypatch.setattr(worker_mod, "record_blocked_scouts", explode)
    settings = get_settings().model_copy(update={"worker_heartbeat_seconds": 0.02})
    worker = Worker(settings=settings, session_factory=make)
    stop, thread = worker._start_registry_heartbeat()
    try:
        assert enough.wait(20.0), "the heartbeat stopped after a failing signal"
        assert thread.is_alive()
    finally:
        stop.set()
        thread.join(timeout=20.0)
    engine.dispose()
