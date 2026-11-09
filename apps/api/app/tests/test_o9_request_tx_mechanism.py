"""O9 (FU-267-F7): the ``commit_before_response`` mechanism, on a FastAPI app of its own.

``app.db.request_tx.WriteDb`` is the real helper; ``get_db`` is the real dependency, replaced
only through ``dependency_overrides`` by a copy of production's body over a SQLite file
(:func:`mirror_get_db`). A separate ``sqlite3`` connection is the committed-state witness.
Only the first two properties below, the ordering and a failed commit, are paired with a
control route that still uses ``Depends(get_db)``, which shows the defect the helper removes
and so shows that those assertions can fail; the others have no ``get_db`` control:

* ordering: the helper commits before ``http.response.start``, and a read made when the
  final body is sent sees the write (the control commits after the body: the read misses);
* a failed commit answers 500 with nothing persisted (the control answers 200);
* a route that raises after a flush keeps its 4xx, and nothing is committed;
* an explicit commit in the route followed by the helper's is harmless;
* the helper commits the session of a ``dependency_overrides[get_db]`` override, the same
  session every other ``get_db`` dependency of the request receives;
* a background task sees the committed write;
* the helper is function-scoped over the request-scoped ``get_db``, never catches, and FastAPI
  refuses it under a request-scoped yield dependency.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import textwrap
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException
from fastapi.exceptions import DependencyScopeError
from fastapi.testclient import TestClient
from sqlalchemy import String, create_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from app.db import request_tx
from app.db.request_tx import WriteDb, commit_before_response
from app.db.session import get_db
from app.tests._write_commit_support import (
    COMMIT,
    FINAL_BODY,
    RESPONSE_START,
    CommitSwitch,
    SendRecorder,
    commit_precedes_response,
    failing_commit_factory,
    mirror_get_db,
    record_commits,
)


class _Base(DeclarativeBase):
    pass


class _Item(_Base):
    __tablename__ = "o9_item"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(50))


class _World:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.engine = create_engine(
            f"sqlite:///{path}", connect_args={"check_same_thread": False}, future=True
        )
        _Base.metadata.create_all(self.engine)
        self.switch = CommitSwitch()
        self.factory = failing_commit_factory(self.engine, self.switch)
        self.events: list[str] = []
        self.sessions: list[int] = []
        self.background_reads: list[int] = []
        self.app = _build_app(self)
        self.recorder = SendRecorder(self.app, self.events, at_final_body=self.committed_rows)
        self.client = TestClient(self.recorder, raise_server_exceptions=False)

    def committed_rows(self) -> int:
        """What a second client reads now: rows committed to the file."""
        with sqlite3.connect(self.path) as conn:
            return conn.execute("SELECT count(*) FROM o9_item").fetchone()[0]


def _build_app(world: _World) -> FastAPI:
    application = FastAPI()

    def opened() -> Iterator[Session]:
        """The production-mirroring override, also logging which sessions it opened."""
        for db in mirror_get_db(world.factory)():
            world.sessions.append(id(db))
            yield db

    application.dependency_overrides[get_db] = opened

    def tenant(db: Session = Depends(get_db)) -> int:
        """Stands in for ``get_tenant_context``: another request-scoped ``get_db`` user."""
        return id(db)

    @application.post("/helper")
    def helper(db: Session = WriteDb, tenant_session: int = Depends(tenant)) -> dict:
        db.add(_Item(name="helper"))
        db.flush()
        return {"route_session": id(db), "tenant_session": tenant_session}

    @application.post("/control")
    def control(db: Session = Depends(get_db)) -> dict:
        db.add(_Item(name="control"))
        db.flush()
        return {"ok": True}

    @application.post("/helper-refuses")
    def helper_refuses(db: Session = WriteDb) -> dict:
        db.add(_Item(name="refused"))
        db.flush()
        raise HTTPException(status_code=409, detail="refused after a flush")

    @application.post("/helper-explicit")
    def helper_explicit(db: Session = WriteDb) -> dict:
        db.add(_Item(name="explicit"))
        db.commit()
        return {"ok": True}

    @application.post("/helper-background")
    def helper_background(background_tasks: BackgroundTasks, db: Session = WriteDb) -> dict:
        db.add(_Item(name="background"))
        db.flush()
        background_tasks.add_task(lambda: world.background_reads.append(world.committed_rows()))
        return {"ok": True}

    return application


@pytest.fixture
def world(tmp_path) -> Iterator[_World]:
    w = _World(tmp_path / "o9_mechanism.db")
    stop = record_commits(w.switch, w.events)
    try:
        yield w
    finally:
        stop()
        w.engine.dispose()


# --------------------------------------------------------------------------- #
# Ordering (T3) and the read-after-write window (T6)
# --------------------------------------------------------------------------- #
def test_the_helper_commits_before_the_response_starts(world):
    reply = world.client.post("/helper")

    assert reply.status_code == 200
    assert commit_precedes_response(world.events), world.events
    # A read sent once the client has the complete 200 sees the write.
    assert world.recorder.seen_at_final_body == [1]


def test_control_get_db_alone_commits_after_the_body_is_sent(world):
    """The defect the helper removes (AUDIT proof 1a): the commit follows the final body."""
    reply = world.client.post("/control")

    assert reply.status_code == 200
    assert world.events == [RESPONSE_START, FINAL_BODY, COMMIT]
    assert world.recorder.seen_at_final_body == [0]
    assert world.committed_rows() == 1


# --------------------------------------------------------------------------- #
# A failed commit (T1)
# --------------------------------------------------------------------------- #
def test_a_failed_commit_answers_500_and_persists_nothing(world):
    world.switch.armed = True
    reply = world.client.post("/helper")

    assert reply.status_code == 500
    assert world.switch.failures == 1
    assert world.committed_rows() == 0


def test_control_a_failed_teardown_commit_still_answers_200(world):
    """The defect (AUDIT proof 1b): the client keeps a 200 for a write that was lost."""
    world.switch.armed = True
    reply = world.client.post("/control")

    assert reply.status_code == 200
    assert world.switch.failures == 1
    assert world.committed_rows() == 0


# --------------------------------------------------------------------------- #
# A refusal raised after a flush, and a route's own commit
# --------------------------------------------------------------------------- #
def test_a_refusal_raised_after_a_flush_keeps_its_status_and_commits_nothing(world):
    reply = world.client.post("/helper-refuses")

    assert reply.status_code == 409
    assert COMMIT not in world.events
    assert world.committed_rows() == 0


def test_an_explicit_commit_before_the_helpers_is_harmless(world):
    reply = world.client.post("/helper-explicit")

    assert reply.status_code == 200
    assert world.events.count(COMMIT) >= 2
    assert commit_precedes_response(world.events)
    assert world.committed_rows() == 1


# --------------------------------------------------------------------------- #
# One session, from the get_db override
# --------------------------------------------------------------------------- #
def test_the_helper_commits_the_get_db_override_session_shared_by_the_request(world):
    reply = world.client.post("/helper")

    body = reply.json()
    assert reply.status_code == 200
    assert world.sessions == [body["route_session"]]  # the override's one session...
    assert body["tenant_session"] == body["route_session"]  # ...shared with the other user
    assert world.committed_rows() == 1


def test_a_background_task_sees_the_committed_write(world):
    reply = world.client.post("/helper-background")

    assert reply.status_code == 200
    assert world.background_reads == [1]


# --------------------------------------------------------------------------- #
# The helper's shape
# --------------------------------------------------------------------------- #
def test_write_db_is_the_helper_with_function_scope_over_request_scoped_get_db():
    assert WriteDb.dependency is commit_before_response
    assert WriteDb.scope == "function"
    inner = inspect.signature(commit_before_response).parameters["db"].default
    assert inner.dependency is get_db
    assert inner.scope is None  # get_db stays request-scoped: one session per request


def test_the_helper_never_catches_an_exception():
    """A ``try`` in the helper could commit or swallow a refused write; the rule is none."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(request_tx.commit_before_response)))
    assert not [node for node in ast.walk(tree) if isinstance(node, ast.Try | ast.TryStar)]


def test_a_request_scoped_yield_dependency_may_not_depend_on_the_helper():
    application = FastAPI()

    def request_scoped(db: Session = WriteDb) -> Iterator[Session]:
        yield db

    with pytest.raises(DependencyScopeError):

        @application.post("/refused")
        def refused(db: Session = Depends(request_scoped)) -> dict:
            return {}
