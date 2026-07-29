"""Worker database-exception containment tests (Phase 4 Gate 4F).

Three worker paths can surface a raw driver exception to a default hook or a
traceback: startup validation, registration-retry exhaustion, and the polling
loop. These tests pin the safe shapes — fixed events / fixed messages carrying
only the exception *class name*, causes suppressed with ``from None`` — while
proving the success and retry behaviors are preserved.

``caplog`` is deliberately not used where ``configure_logging()`` runs (it
replaces the root handler list and evicts pytest's capture handler); emission
tests capture the real formatter's stream output exactly once per test.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import traceback

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from alembic import command
from app.core.config import Settings, get_settings
from app.core.errors import WorkerRegistrationFailedError
from app.core.logging import configure_logging
from app.db.schema import alembic_config
from app.jobs.worker import Worker

_SENTINEL_HOST = "db-sentinel-host.internal"
_SENTINEL_PASS = "sn-sentinel-p4ssw0rd"


@pytest.fixture(autouse=True)
def _restore_root_logging():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield
    root.handlers = handlers
    root.level = level


def _boom_factory():
    """A session factory bound to an engine that always fails with a DSN."""

    def _boom():
        raise sqlite3.OperationalError(
            f"connection to server at {_SENTINEL_HOST}, port 5432 failed "
            f"(postgresql://svc:{_SENTINEL_PASS}@{_SENTINEL_HOST}:5432/appdb)"
        )

    return sessionmaker(bind=create_engine("sqlite://", creator=_boom))


@pytest.fixture()
def migrated_factory(tmp_path, monkeypatch):
    """A real migrated SQLite session factory (real alembic upgrade)."""
    url = f"sqlite:///{tmp_path / 'worker_containment.db'}"
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    command.upgrade(alembic_config(), "head")
    get_settings.cache_clear()
    engine = create_engine(url)
    yield sessionmaker(bind=engine)
    engine.dispose()


def _json_events(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


# --------------------------------------------------------------------------- #
# validate(): startup schema probe
# --------------------------------------------------------------------------- #
def test_validate_failure_is_contained() -> None:
    w = Worker(settings=Settings(), session_factory=_boom_factory())
    with pytest.raises(RuntimeError) as ei:
        w.validate()
    exc = ei.value

    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True
    # Positive: safe classification present in the message and in the rendered
    # traceback exactly as the default excepthook would print it.
    assert "error_class=OperationalError" in str(exc)
    rendered = "".join(traceback.format_exception(exc))
    assert _SENTINEL_HOST not in rendered
    assert _SENTINEL_PASS not in rendered
    assert "postgresql://" not in rendered


def test_validate_success_preserved(migrated_factory) -> None:
    w = Worker(settings=Settings(), session_factory=migrated_factory)
    w.validate()  # must not raise against a real migrated schema


# --------------------------------------------------------------------------- #
# register(): retry then safe exhaustion
# --------------------------------------------------------------------------- #
class _FailingRegistry:
    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    def register(self, db, **kwargs):
        self.calls += 1
        if self.calls <= self.fail_times:
            raise sqlite3.OperationalError(
                f"registration write failed (postgresql://svc:{_SENTINEL_PASS}@"
                f"{_SENTINEL_HOST}:5432/appdb)"
            )

        class _Row:
            generation_token = "tok-test"

        return _Row()


def _retry_settings() -> Settings:
    return Settings(
        worker_registration_retry_limit=2,
        worker_registration_retry_delay_seconds=0,
    )


def test_register_retries_then_succeeds(migrated_factory) -> None:
    registry = _FailingRegistry(fail_times=2)
    w = Worker(
        settings=_retry_settings(),
        session_factory=migrated_factory,
        registry=registry,
    )
    w.register()  # third attempt succeeds
    assert registry.calls == 3
    assert w._generation_token == "tok-test"


def test_register_exhaustion_is_contained(migrated_factory, capsys) -> None:
    registry = _FailingRegistry(fail_times=99)
    w = Worker(
        settings=_retry_settings(),
        session_factory=migrated_factory,
        registry=registry,
    )
    configure_logging("INFO", log_format="json")

    with pytest.raises(WorkerRegistrationFailedError) as ei:
        w.register()

    cap = capsys.readouterr()  # single capture; both streams asserted
    exc = ei.value
    assert registry.calls == 3  # retry budget honored: limit + 1
    assert exc.__cause__ is None
    assert exc.__suppress_context__ is True

    # Positive: the fixed exhaustion event with the class name actually emitted.
    events = _json_events(cap.out)
    exhausted = [e for e in events if e.get("event") == "worker.registration.exhausted"]
    assert len(exhausted) == 1
    assert exhausted[0]["error_class"] == "OperationalError"
    assert exhausted[0]["attempts"] == 3

    # Negative: no DSN fragment, no chained traceback, on any surface.
    rendered = "".join(traceback.format_exception(exc))
    blob = cap.out + cap.err + rendered
    assert _SENTINEL_HOST not in blob
    assert _SENTINEL_PASS not in blob
    assert "Traceback" not in cap.out and "Traceback" not in cap.err


# --------------------------------------------------------------------------- #
# _loop(): polling-loop exception safety
# --------------------------------------------------------------------------- #
def test_poll_loop_failure_logs_safe_event_and_survives(
    migrated_factory, capsys, monkeypatch
) -> None:
    w = Worker(
        settings=Settings(worker_poll_interval_seconds=0.001),
        session_factory=migrated_factory,
    )
    calls = {"n": 0}

    def _boom_poll(*, worker_id):
        calls["n"] += 1
        if calls["n"] >= 3:
            w._stop.set()  # bounded run: three failing polls, then stop
        raise sqlite3.OperationalError(
            f"poll failed (postgresql://svc:{_SENTINEL_PASS}@{_SENTINEL_HOST}/appdb)"
        )

    monkeypatch.setattr(w._runner, "poll_once", _boom_poll)
    configure_logging("INFO", log_format="json")

    w._loop()  # must not raise: repeated failures never kill the loop

    cap = capsys.readouterr()  # single capture; both streams asserted
    assert calls["n"] == 3

    # Positive: one fixed event per failure, class name only.
    events = _json_events(cap.out)
    errors = [e for e in events if e.get("event") == "worker.poll_error"]
    assert len(errors) == 3
    assert all(e["error_class"] == "OperationalError" for e in errors)
    assert all("message" not in e or e["message"] == "worker.poll_error" for e in errors)

    # Negative: no traceback, no DSN fragment, on either stream.
    blob = cap.out + cap.err
    assert "Traceback" not in blob
    assert _SENTINEL_HOST not in blob
    assert _SENTINEL_PASS not in blob


def test_poll_loop_does_not_swallow_base_exceptions(migrated_factory, monkeypatch) -> None:
    # Deliberately `except Exception`, never BaseException: shutdown signals
    # (KeyboardInterrupt) must still propagate out of the loop.
    w = Worker(
        settings=Settings(worker_poll_interval_seconds=0.001),
        session_factory=migrated_factory,
    )

    def _interrupt(*, worker_id):
        raise KeyboardInterrupt

    monkeypatch.setattr(w._runner, "poll_once", _interrupt)
    with pytest.raises(KeyboardInterrupt):
        w._loop()
