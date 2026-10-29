"""The replay demo seed never runs the pipeline against anything but a local SQLite bind.

``app.connectors.replay.seed_replay`` (#255, P6-CI-11) is the one non-test caller of
``app.jobs.pipeline._run`` besides the durable handler, and it passes no meter: the run
gets the bare ``LLMMeter(get_settings())`` fallback, which has no session factory. On
SQLite that is the same-session path the budget design pins; on PostgreSQL it would
silently bypass the shared reservation ledger. These tests prove the seed refuses a
PostgreSQL bind - through its own factory or a rebound ``SessionLocal`` - before the
pipeline runs, before the corpus is read and without opening a connection
(review r7 MINOR-1 of PR #269; adopted from the reviewer's probe).
"""

from __future__ import annotations

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.connectors import replay as replay_mod
from app.core.config import get_settings
from app.db import seed as seed_mod
from app.db import session as session_mod
from app.jobs import pipeline as pipeline_mod
from app.llm.metering import LLMMeter
from app.tests._auth2_support import require_postgres

#: Built, never connected: port 1 on loopback, no password.
NEVER_CONNECTED = "postgresql+psycopg://probe@127.0.0.1:1/never_connected"


class RunReached(Exception):
    pass


@pytest.fixture
def run_calls(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    def spy(*args, **kwargs):
        calls.append(args)
        raise RunReached("pipeline._run was reached")

    monkeypatch.setattr(pipeline_mod, "_run", spy)
    return calls


def _connects(engine) -> list[int]:
    seen = [0]

    @event.listens_for(engine, "connect")
    def _on_connect(*_args):  # pragma: no cover - must never fire
        seen[0] += 1

    return seen


def test_a_postgresql_factory_is_refused_before_the_run_and_before_connecting(
    monkeypatch, run_calls
):
    engine = create_engine(NEVER_CONNECTED)
    connects = _connects(engine)
    monkeypatch.setattr(replay_mod, "load_corpus", lambda *_a, **_k: pytest.fail("corpus read"))
    with pytest.raises(seed_mod.SeedTargetError):
        replay_mod.seed_replay(session_factory=sessionmaker(bind=engine))
    assert run_calls == [] and connects == [0]


def test_a_sessionlocal_rebound_to_postgresql_is_refused(monkeypatch, run_calls):
    engine = create_engine(NEVER_CONNECTED)
    connects = _connects(engine)
    monkeypatch.setattr(session_mod, "SessionLocal", sessionmaker(bind=engine))
    with pytest.raises(seed_mod.SeedTargetError):
        replay_mod.seed_replay()
    assert run_calls == [] and connects == [0]


def test_a_real_postgresql_cluster_is_refused_without_connecting(run_calls):
    engine = create_engine(require_postgres())
    connects = _connects(engine)
    try:
        with pytest.raises(seed_mod.SeedTargetError, match="bound backend=postgresql"):
            replay_mod.seed_replay(session_factory=sessionmaker(bind=engine))
        assert run_calls == [] and connects == [0]
    finally:
        engine.dispose()


def test_on_sqlite_the_meter_the_seed_gets_is_the_same_session_path(tmp_path):
    """What ``_run(db, request.id)`` builds for the seed: the bare fallback bound to a
    SQLite session takes the same-session path, as designed."""
    import app.db.models  # noqa: F401
    from app.db.base import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'seed.db'}")
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine)() as db:
        meter = LLMMeter(get_settings()).bind(
            db, organization_id="o", workspace_id="w", scout_request_id=None
        )
        assert meter.shared_ledger is False
    engine.dispose()
