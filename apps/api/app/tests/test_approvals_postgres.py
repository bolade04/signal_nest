"""Phase 5B ``decision_seq`` concurrency on PostgreSQL 16 (W21 lane P5B; review rule
W21-REV-CF1-DATA, binding).

The rule under test: the subject's brief row is locked (``SELECT … FOR UPDATE``) under READ
COMMITTED **before** ``max(decision_seq)`` is read, so concurrent deciders serialize and
each reads the previous decider's committed row; a unique-key clash on
``uq_approval_decision_seq`` becomes a 409 that writes nothing. REPEATABLE READ is not
sufficient even with the lock (its snapshot predates the wait), and the backstop is what
keeps it from storing a duplicate.

Real sessions in real threads against a database of each test's own (created and dropped
here, from ``TEST_POSTGRES_URL``); the service is called exactly as the route calls it.
Every wait is bounded (``lock_timeout`` / ``statement_timeout`` on every connection and a
join bound), so a defect fails rather than hangs.

Gate: :func:`require_postgres` -- with ``TEST_POSTGRES_URL`` unset these tests FAIL in CI
(``CI=true``) and skip only on a developer machine.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

import app.main  # noqa: F401 - registers every ORM model
from app.approvals import service as svc
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.auth.dependencies import TenantContext
from app.briefs.models import RecommendationBrief
from app.core.config import get_settings
from app.core.enums import ApprovalDecision, Role
from app.core.errors import ConflictError
from app.db.models import Base
from app.db.session import build_engine
from app.organizations.models import Organization, User, Workspace
from app.tests._approvals_support import add_brief, add_evidence, add_org, add_user
from app.tests._auth2_support import pg_database, require_postgres

ORG, WS, BRIEF = "org-appg", "ws-appg", "brief-appg"
AUTHOR = "appg-author"
REVIEWERS = tuple(f"appg-reviewer-{n}" for n in range(8))
JOIN_SECONDS = 60.0
POLL_SECONDS = 30.0


@dataclass
class World:
    url: str
    engine: Engine
    factory: sessionmaker
    observer: Engine


def _engine(url: str, **kwargs) -> Engine:
    # Bounded waits on every connection: a missed release fails the test instead of
    # hanging it.
    return create_engine(
        url,
        future=True,
        pool_size=12,
        max_overflow=4,
        connect_args={"options": "-c lock_timeout=30000 -c statement_timeout=60000"},
        **kwargs,
    )


@pytest.fixture
def world() -> Iterator[World]:
    require_postgres()
    with pg_database("sn_appr") as url:
        engine = _engine(url)
        observer = _engine(url, isolation_level="AUTOCOMMIT")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with factory() as s:
            add_org(s, ORG, WS)
            author = add_user(s, AUTHOR, org_id=ORG, role=Role.MARKETER)
            for reviewer in REVIEWERS:
                add_user(s, reviewer, org_id=ORG, role=Role.REVIEWER)
            evidence = add_evidence(s, org_id=ORG, ws_id=WS, key="appg")
            add_brief(s, evidence, brief_id=BRIEF, author=author)
            s.commit()
        try:
            yield World(url=url, engine=engine, factory=factory, observer=observer)
        finally:
            engine.dispose()
            observer.dispose()


def _decide(
    w: World,
    reviewer: str,
    decision: ApprovalDecision,
    *,
    isolation: str | None = None,
    before_decide: threading.Event | None = None,
    go: threading.Event | None = None,
    hold: threading.Event | None = None,
    holding: threading.Event | None = None,
) -> tuple:
    """One decision in its own session, as the route makes it; returns its outcome.

    ``before_decide``/``go``: signal once the transaction has read (its snapshot exists),
    then wait to be released. ``holding``/``hold``: signal once the decision is flushed
    (lock held, row uncommitted), then wait before committing.
    """
    with w.factory() as s:
        if isolation is not None:
            s.connection(execution_options={"isolation_level": isolation})
        user = s.get(User, reviewer)
        brief = s.get(RecommendationBrief, BRIEF)
        ctx = TenantContext(
            user=user,
            organization=s.get(Organization, ORG),
            workspace=s.get(Workspace, WS),
            role=Role.REVIEWER,
        )
        if before_decide is not None:
            before_decide.set()
            assert go is not None and go.wait(JOIN_SECONDS)
        try:
            approval = svc.record_decision(
                s, ctx=ctx, brief=brief, decision=decision, subject_version=1
            )
        except ConflictError as exc:
            s.rollback()
            return ("conflict", exc.code)
        if holding is not None:
            holding.set()
            assert hold is not None and hold.wait(JOIN_SECONDS)
        s.commit()
        return ("ok", approval.decision_seq)


class _Runner(threading.Thread):
    def __init__(self, fn, *args, **kwargs):
        super().__init__(daemon=True)
        self._call = (fn, args, kwargs)
        self.result = None
        self.error: BaseException | None = None

    def run(self) -> None:  # pragma: no cover - exercised through join
        fn, args, kwargs = self._call
        try:
            self.result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            self.error = exc

    def outcome(self):
        self.join(JOIN_SECONDS)
        assert not self.is_alive(), "a decider did not finish"
        if self.error is not None:
            raise self.error
        return self.result


def _waiting_on_lock(w: World) -> list[str]:
    """Queries of this database's backends now blocked on a heavyweight lock."""
    with w.observer.connect() as c:
        rows = c.execute(
            text(
                "SELECT query FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        ).all()
    return [r[0] for r in rows]


def _wait_for(predicate, what: str):
    deadline = time.monotonic() + POLL_SECONDS
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")


def _stored(w: World) -> list[tuple]:
    with w.factory() as s:
        return [
            (a.decision_seq, a.decision, a.decided_by)
            for a in s.scalars(select(Approval).order_by(Approval.decision_seq))
        ]


def _audited(w: World) -> int:
    with w.factory() as s:
        return len(s.scalars(select(AuditLog).where(AuditLog.action.like("approval.%"))).all())


def test_the_session_isolation_is_read_committed_on_the_production_engine(world: World):
    """The precondition of the rule: the application's own engine does not raise isolation."""
    settings = get_settings().model_copy(update={"database_url": world.url})
    engine = build_engine(settings)
    try:
        with engine.connect() as c:
            level = c.execute(text("SHOW transaction_isolation")).scalar_one()
    finally:
        engine.dispose()
    assert level == "read committed"


def test_a_second_decider_waits_on_the_subject_lock_before_reading_the_sequence(
    world: World, monkeypatch
):
    w = world
    reads: list[str] = []
    real_next = svc._next_decision_seq

    def spy(*args, **kwargs):
        reads.append(threading.current_thread().name)
        return real_next(*args, **kwargs)

    monkeypatch.setattr(svc, "_next_decision_seq", spy)
    holding, hold = threading.Event(), threading.Event()
    first = _Runner(
        _decide, w, REVIEWERS[0], ApprovalDecision.APPROVED, holding=holding, hold=hold
    )
    first.name = "first"
    first.start()
    assert holding.wait(JOIN_SECONDS), "the first decider never flushed"
    second = _Runner(_decide, w, REVIEWERS[1], ApprovalDecision.REJECTED)
    second.name = "second"
    second.start()
    try:
        waiting = _wait_for(lambda: _waiting_on_lock(w), "the second decider to block")
        # It blocks on the subject's row lock, and it has not read the sequence yet.
        assert any("FOR UPDATE" in q and "recommendation_briefs" in q for q in waiting), waiting
        time.sleep(0.5)
        assert second.is_alive() and reads == ["first"]
    finally:
        hold.set()
    assert first.outcome() == ("ok", 1)
    assert second.outcome() == ("ok", 2)  # read the first decider's committed row
    assert reads == ["first", "second"]
    assert _stored(w) == [
        (1, "approved", REVIEWERS[0]),
        (2, "rejected", REVIEWERS[1]),
    ]
    assert _audited(w) == 2


def test_many_concurrent_deciders_get_a_gapless_unique_sequence(world: World):
    w = world
    barrier = threading.Barrier(len(REVIEWERS))

    def decide(n: int):
        barrier.wait(JOIN_SECONDS)
        decision = ApprovalDecision.APPROVED if n % 2 == 0 else ApprovalDecision.REJECTED
        return _decide(w, REVIEWERS[n], decision)

    runners = [_Runner(decide, n) for n in range(len(REVIEWERS))]
    for r in runners:
        r.start()
    outcomes = [r.outcome() for r in runners]
    assert all(o[0] == "ok" for o in outcomes), outcomes  # no conflict under the lock
    assert sorted(o[1] for o in outcomes) == list(range(1, len(REVIEWERS) + 1))
    stored = _stored(w)
    assert [s[0] for s in stored] == list(range(1, len(REVIEWERS) + 1))
    assert {s[2] for s in stored} == set(REVIEWERS)
    assert _audited(w) == len(REVIEWERS)


def test_repeatable_read_is_not_sufficient_and_the_backstop_refuses_a_duplicate(world: World):
    """A REPEATABLE READ decider whose snapshot predates the first decider's commit waits on
    the lock, then reads a stale ``max(decision_seq)``: the unique key refuses its insert,
    which is a 409 that writes nothing (no duplicate, no orphan audit row)."""
    w = world
    snapshot_taken, go = threading.Event(), threading.Event()
    late = _Runner(
        _decide,
        w,
        REVIEWERS[1],
        ApprovalDecision.REJECTED,
        isolation="REPEATABLE READ",
        before_decide=snapshot_taken,
        go=go,
    )
    late.start()
    assert snapshot_taken.wait(JOIN_SECONDS)
    # The first decider commits after the late one's snapshot was taken.
    assert _decide(w, REVIEWERS[0], ApprovalDecision.APPROVED) == ("ok", 1)
    go.set()
    assert late.outcome() == ("conflict", "approval_concurrent_decision")
    assert _stored(w) == [(1, "approved", REVIEWERS[0])]
    assert _audited(w) == 1
    # The same decider under READ COMMITTED (the application's isolation) succeeds.
    assert _decide(w, REVIEWERS[1], ApprovalDecision.REJECTED) == ("ok", 2)
