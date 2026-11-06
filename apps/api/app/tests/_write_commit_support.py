"""Shared tooling for the O9 commit-before-response tests (FU-267-F7). Not a test module.

* :class:`FailingCommitSession`: ``commit()`` raises ``OperationalError`` while its factory's
  :class:`CommitSwitch` is armed. ``commit()`` itself is overridden, not ``before_commit``:
  that event also fires when a SAVEPOINT is released (``begin_nested()``), which is not a
  commit of the request's transaction.
* :func:`mirror_get_db`: a ``get_db`` override that mirrors production (commit after the
  yield, rollback on an exception, close).
* :func:`never_commits`: a ``get_db`` override whose teardown never commits (the #267
  pattern, ``test_scout_pause_cooperative.py``). A write that is still durable under it was
  committed before the response, by the route or by ``commit_before_response``.
* :class:`SendRecorder`: an outermost ASGI wrapper that logs ``http.response.start`` and the
  final body message in one ordered event list, and runs a witness read at the instant the
  final body message is sent: what a second client reads once the first has its response.
* :func:`record_commits`: appends ``"commit"`` to the same event list each time a
  :class:`FailingCommitSession` commits. Recorded from ``Session.commit()`` itself, not from
  ``after_commit``: under SQLAlchemy 2.1 ``after_commit`` also fires when a SAVEPOINT is
  released, which the override-set service does before the request transaction commits.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.engine import Engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

RESPONSE_START = "http.response.start"
FINAL_BODY = "http.response.body(final)"
COMMIT = "commit"


@dataclass
class CommitSwitch:
    """While ``armed``, every ``commit()`` of a :class:`FailingCommitSession` raises.

    ``log``, when set, receives ``"commit"`` after each successful ``commit()``.
    """

    armed: bool = False
    failures: int = 0
    log: list[str] | None = None


class FailingCommitSession(Session):
    def commit(self) -> None:
        switch: CommitSwitch | None = self.info.get("o9_commit_switch")
        if switch is not None and switch.armed:
            switch.failures += 1
            raise OperationalError("COMMIT", None, RuntimeError("O9: injected commit failure"))
        super().commit()
        if switch is not None and switch.log is not None:
            switch.log.append(COMMIT)


def failing_commit_factory(engine: Engine, switch: CommitSwitch) -> sessionmaker:
    """Production's session options (``app/db/session.py``) with a commit that can be failed."""
    return sessionmaker(
        bind=engine,
        class_=FailingCommitSession,
        autoflush=False,
        expire_on_commit=False,
        future=True,
        info={"o9_commit_switch": switch},
    )


def mirror_get_db(factory: sessionmaker) -> Callable[[], Iterator[Session]]:
    """A ``get_db`` override with production's body (``app/db/session.py``)."""

    def _get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    return _get_db


def never_commits(factory: sessionmaker) -> Callable[[], Iterator[Session]]:
    """A ``get_db`` override whose teardown rolls back instead of committing."""

    def _get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.rollback()
            db.close()

    return _get_db


def record_commits(switch: CommitSwitch, events: list[str]) -> Callable[[], None]:
    """Log ``"commit"`` into ``events`` after every ``commit()`` of the switch's sessions.

    Returns the function that stops the logging.
    """
    switch.log = events

    def _stop() -> None:
        switch.log = None

    return _stop


@dataclass
class SendRecorder:
    """Outermost ASGI wrapper around ``app``; see the module docstring."""

    app: Any
    events: list[str] = field(default_factory=list)
    at_final_body: Callable[[], Any] | None = None
    seen_at_final_body: list[Any] = field(default_factory=list)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def recording_send(message) -> None:
            if message["type"] == "http.response.start":
                self.events.append(RESPONSE_START)
            elif message["type"] == "http.response.body" and not message.get("more_body"):
                if self.at_final_body is not None:
                    self.seen_at_final_body.append(self.at_final_body())
                self.events.append(FINAL_BODY)
            await send(message)

        await self.app(scope, receive, recording_send)


def commit_precedes_response(events: list[str]) -> bool:
    """True when a commit was logged before ``http.response.start``."""
    return RESPONSE_START in events and COMMIT in events[: events.index(RESPONSE_START)]
