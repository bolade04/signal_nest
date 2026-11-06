"""Phase 5B-W4 adversarial battery: approval isolation (W21 lane ADV5AB).

Source: ``docs/project-phase-5b-approval-audit-spine.md`` §4 exit criteria 4 and 7 and the
§5 W4 row ("the whole adversarial battery ... audit and approval isolation"). The P5B
lane's ``test_approvals_api.py`` already proves the HTTP 404/403 classes, the queue's
workspace scope and the dark gate; ``test_approvals_postgres.py`` proves the lock wait,
the gapless 8-way sequence and the REPEATABLE READ backstop. This module adds only what
those leave unpinned. Each test names the mutant it kills (lane evidence
``P6-W21-ADV5AB-01``):

* **The service holds its own tenant scope.** ``record_decision`` is the single write
  path (5C and 5E will call it without the route's lookup). A brief of another workspace
  or organization handed to it is the same not-found refusal, writes nothing, and the
  refusal comes before the author and version checks, so a 403 or a 409 can never confirm
  that a foreign brief exists, is the caller's, or is at a given version.
* **Two-organization members.** A member decides with the role of the *workspace's*
  organization only.
* **A brief that vanishes under the lock** answers not-found and writes nothing.
* **PostgreSQL, READ COMMITTED (the production isolation):** the transition rule is
  evaluated under the subject lock in both race directions; a brief deleted while a decider
  waits is not-found; a decision committed before its brief is deleted keeps its exact
  binding (``brief_id`` nulled by the foreign key, O-6); one tenant's in-flight decision
  never blocks another tenant's.

The ``world`` tests run on SQLite and, when ``TEST_POSTGRES_URL`` is set, on PostgreSQL;
``require_postgres`` makes an unset URL FAIL in CI and skip only on a developer machine.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, delete, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.approvals import service as svc
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.auth.dependencies import TenantContext
from app.briefs.models import RecommendationBrief
from app.core.config import get_settings
from app.core.enums import ApprovalDecision, Role
from app.core.errors import ConflictError, NotFoundError, PermissionDeniedError
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import Organization, OrganizationMember, User, Workspace
from app.tests._approvals_support import add_brief, add_evidence, add_org, add_user
from app.tests._auth2_support import (
    active_rate_limiter,
    live_bearer,
    pg_database,
    require_postgres,
)

API = get_settings().api_prefix

ORG_A, WS_A1, WS_A2 = "org-w4ap-a", "ws-w4ap-a1", "ws-w4ap-a2"
ORG_B, WS_B = "org-w4ap-b", "ws-w4ap-b"

A_OWNER, A_MARKETER = "w4ap-a-owner", "w4ap-a-marketer"
A_REVIEWER, A_REVIEWER2 = "w4ap-a-reviewer", "w4ap-a-reviewer2"
B_MARKETER, B_REVIEWER = "w4ap-b-marketer", "w4ap-b-reviewer"
DUAL = "w4ap-dual"  # REVIEWER in organization A, MARKETER in organization B

BRIEF_A1, BRIEF_A2, BRIEF_B = "w4ap-brief-a1", "w4ap-brief-a2", "w4ap-brief-b"
BRIEF_A2_OWN = "w4ap-brief-a2-own"  # in WS_A2, written by A_REVIEWER

ROLE_DENIAL = "Role '{role}' is not permitted for this action."

JOIN_SECONDS = 60.0
POLL_SECONDS = 20.0


# --------------------------------------------------------------------------- #
# The world: one schema, one seed, on SQLite or PostgreSQL
# --------------------------------------------------------------------------- #
@dataclass
class World:
    kind: str
    engine: Engine
    witness: Engine
    factory: sessionmaker
    client: TestClient
    ev: dict
    tokens: dict = field(default_factory=dict)

    # -- HTTP ------------------------------------------------------------------------
    def send(self, method: str, url: str, user_id: str, body=None):
        if user_id not in self.tokens:
            self.tokens[user_id] = live_bearer(user_id)
        active_rate_limiter()._hits.clear()
        kwargs: dict = {"headers": {"Authorization": f"Bearer {self.tokens[user_id]}"}}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, url, **kwargs)

    def decide(self, user_id: str, key: str, brief_id: str, decision="approved", version=1):
        ev = self.ev[key]
        url = f"{API}/workspaces/{ev.ws_id}/opportunities/{ev.opportunity_id}/briefs/{brief_id}"
        body = {"decision": decision, "subject_version": version}
        return self.send("POST", f"{url}/approvals", user_id, body)

    def history(self, user_id: str, key: str, brief_id: str):
        ev = self.ev[key]
        url = f"{API}/workspaces/{ev.ws_id}/opportunities/{ev.opportunity_id}/briefs/{brief_id}"
        return self.send("GET", f"{url}/approvals", user_id)

    def queue(self, user_id: str, ws: str):
        return self.send("GET", f"{API}/workspaces/{ws}/approval-queue", user_id)

    # -- witness ---------------------------------------------------------------------
    def approvals(self) -> list[tuple]:
        t = Approval.__table__
        with self.witness.connect() as c:
            rows = c.execute(
                select(
                    t.c.subject_id,
                    t.c.subject_version,
                    t.c.decision_seq,
                    t.c.decision,
                    t.c.organization_id,
                    t.c.workspace_id,
                    t.c.decided_role,
                    t.c.brief_id,
                ).order_by(t.c.subject_id, t.c.decision_seq)
            ).all()
        return [tuple(r) for r in rows]

    def approval_audit(self) -> list[tuple]:
        t = AuditLog.__table__
        with self.witness.connect() as c:
            rows = c.execute(
                select(t.c.organization_id, t.c.workspace_id, t.c.action, t.c.entity_id)
                .where(t.c.action.like("approval.%"))
                .order_by(t.c.created_at, t.c.id)
            ).all()
        return [tuple(r) for r in rows]


def _sqlite(db_file: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _postgres(url: str, **kwargs) -> Engine:  # pragma: no cover - gated on PostgreSQL
    # Bounded waits on every connection: a missed release fails instead of hanging.
    return create_engine(
        url,
        future=True,
        pool_size=8,
        max_overflow=4,
        connect_args={"options": "-c lock_timeout=30000 -c statement_timeout=60000"},
        **kwargs,
    )


def _seed(s) -> dict:
    add_org(s, ORG_A, WS_A1, WS_A2)
    add_org(s, ORG_B, WS_B)
    users = {
        uid: add_user(s, uid, org_id=ORG_A, role=role)
        for uid, role in (
            (A_OWNER, Role.OWNER),
            (A_MARKETER, Role.MARKETER),
            (A_REVIEWER, Role.REVIEWER),
            (A_REVIEWER2, Role.REVIEWER),
            (DUAL, Role.REVIEWER),
        )
    }
    for uid, role in ((B_MARKETER, Role.MARKETER), (B_REVIEWER, Role.REVIEWER)):
        users[uid] = add_user(s, uid, org_id=ORG_B, role=role)
    s.add(
        OrganizationMember(
            id="m-w4ap-dual-b", organization_id=ORG_B, user_id=DUAL, role=Role.MARKETER.value
        )
    )
    s.flush()
    ev = {
        "a1": add_evidence(s, org_id=ORG_A, ws_id=WS_A1, key="w4ap-a1"),
        "a2": add_evidence(s, org_id=ORG_A, ws_id=WS_A2, key="w4ap-a2"),
        "a2own": add_evidence(s, org_id=ORG_A, ws_id=WS_A2, key="w4ap-a2o"),
        "b": add_evidence(s, org_id=ORG_B, ws_id=WS_B, key="w4ap-b"),
    }
    add_brief(s, ev["a1"], brief_id=BRIEF_A1, author=users[A_MARKETER])
    add_brief(s, ev["a2"], brief_id=BRIEF_A2, author=users[A_MARKETER])
    add_brief(s, ev["a2own"], brief_id=BRIEF_A2_OWN, author=users[A_REVIEWER])
    add_brief(s, ev["b"], brief_id=BRIEF_B, author=users[B_MARKETER])
    return ev


def _build(kind: str, engine: Engine, witness: Engine) -> Iterator[World]:
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        ev = _seed(s)
        s.commit()

    def _override_get_db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        yield World(kind, engine, witness, factory, TestClient(app), ev)
    finally:
        app.dependency_overrides.clear()
        active_rate_limiter()._hits.clear()
        engine.dispose()
        witness.dispose()


@pytest.fixture(params=["sqlite", "postgresql"])
def world(request, tmp_path) -> Iterator[World]:
    if request.param == "sqlite":
        db_file = tmp_path / "w4-approvals.db"
        yield from _build("sqlite", _sqlite(db_file), _sqlite(db_file))
        return
    require_postgres()
    with pg_database("sn_w4ap") as url:  # pragma: no cover - gated on PostgreSQL
        yield from _build(
            "postgresql", _postgres(url), _postgres(url, isolation_level="AUTOCOMMIT")
        )


@pytest.fixture
def pg_world() -> Iterator[World]:
    require_postgres()
    with pg_database("sn_w4ar") as url:  # pragma: no cover - gated on PostgreSQL
        yield from _build(
            "postgresql", _postgres(url), _postgres(url, isolation_level="AUTOCOMMIT")
        )


@pytest.fixture(autouse=True)
def _dark_by_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", False)


@pytest.fixture
def briefs_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)


def _ctx(s, user_id: str, ws_id: str, role: Role) -> TenantContext:
    """The context ``get_tenant_context`` resolves for ``user_id`` in ``ws_id``."""
    workspace = s.get(Workspace, ws_id)
    return TenantContext(
        user=s.get(User, user_id),
        organization=s.get(Organization, workspace.organization_id),
        workspace=workspace,
        role=role,
    )


def _error(r) -> tuple:
    err = (r.json() or {}).get("error") or {}
    return r.status_code, err.get("code"), err.get("message")


# --------------------------------------------------------------------------- #
# The service holds its own tenant scope (exit criterion 7(a), defence in depth)
# --------------------------------------------------------------------------- #
class TestTheServiceHoldsItsScope:
    """Kills W4B-M02 (the service's scope re-check removed) and W4B-M03 (the author and
    version checks moved ahead of the scope check). Both survive the pre-existing approval
    modules (``test_approvals_api``, ``test_approvals_postgres``, the cross-organization and
    E7 matrices): the route's ``get_scoped_brief`` answers first over HTTP."""

    @pytest.mark.parametrize("brief_id", [BRIEF_A2, BRIEF_B], ids=["sibling-ws", "other-org"])
    def test_a_foreign_brief_handed_to_the_service_is_not_found_and_writes_nothing(
        self, world: World, brief_id: str
    ):
        with world.factory() as s:
            ctx = _ctx(s, A_REVIEWER, WS_A1, Role.REVIEWER)
            brief = s.get(RecommendationBrief, brief_id)
            with pytest.raises(NotFoundError) as refused:
                svc.record_decision(
                    s, ctx=ctx, brief=brief, decision=ApprovalDecision.APPROVED, subject_version=1
                )
            assert (refused.value.code, refused.value.message) == ("not_found", svc.BRIEF_NOT_FOUND)
            s.rollback()
        assert world.approvals() == [] and world.approval_audit() == []

    @pytest.mark.parametrize("decision", list(ApprovalDecision))
    @pytest.mark.parametrize("version", [1, 2, 99])
    def test_the_scope_refusal_precedes_the_author_and_version_checks(
        self, world: World, decision: ApprovalDecision, version: int
    ):
        # BRIEF_A2_OWN is A_REVIEWER's own brief, at version 1, in the sibling workspace.
        # Seen from WS_A1 it must be indistinguishable from a brief that does not exist:
        # never the author's 403, never a stale-version or nothing-to-revoke 409.
        with world.factory() as s:
            ctx = _ctx(s, A_REVIEWER, WS_A1, Role.REVIEWER)
            brief = s.get(RecommendationBrief, BRIEF_A2_OWN)
            with pytest.raises(NotFoundError) as refused:
                svc.record_decision(
                    s, ctx=ctx, brief=brief, decision=decision, subject_version=version
                )
            assert refused.value.message == svc.BRIEF_NOT_FOUND
            s.rollback()
        assert world.approvals() == [] and world.approval_audit() == []

    def test_positive_controls_the_author_and_version_checks_are_live_in_scope(self, world: World):
        # The same brief in its own workspace: the author check answers (so the 404 above
        # was the scope, not a dead author check) ...
        with world.factory() as s:
            ctx = _ctx(s, A_REVIEWER, WS_A2, Role.REVIEWER)
            brief = s.get(RecommendationBrief, BRIEF_A2_OWN)
            with pytest.raises(PermissionDeniedError) as author:
                svc.record_decision(
                    s, ctx=ctx, brief=brief, decision=ApprovalDecision.APPROVED, subject_version=1
                )
            assert author.value.code == "approval_author_conflict"
            s.rollback()
        # ... and the version check answers for another reviewer.
        with world.factory() as s:
            ctx = _ctx(s, A_REVIEWER2, WS_A2, Role.REVIEWER)
            brief = s.get(RecommendationBrief, BRIEF_A2_OWN)
            with pytest.raises(ConflictError) as stale:
                svc.record_decision(
                    s, ctx=ctx, brief=brief, decision=ApprovalDecision.APPROVED, subject_version=2
                )
            assert stale.value.code == "approval_stale_version"
            s.rollback()
        assert world.approvals() == []


# --------------------------------------------------------------------------- #
# A member of two organizations decides with the workspace organization's role only
# --------------------------------------------------------------------------- #
class TestTwoOrganizationMember:
    """Kills W4B-M06 (the membership lookup in ``auth/dependencies.py`` ignores the
    organization): DUAL would then carry organization A's REVIEWER role into B."""

    def test_reviewer_in_one_organization_is_not_an_approver_in_the_other(
        self, world: World, briefs_on
    ):
        a = world.decide(DUAL, "a1", BRIEF_A1)
        assert a.status_code == 201, a.text
        assert a.json()["decided_role"] == "reviewer"
        b = world.decide(DUAL, "b", BRIEF_B)
        assert _error(b) == (403, "permission_denied", ROLE_DENIAL.format(role="marketer"))
        assert _error(world.queue(DUAL, WS_B)) == (
            403,
            "permission_denied",
            ROLE_DENIAL.format(role="marketer"),
        )
        # A marketer reads B's history, and it holds nothing of A's decision.
        history = world.history(DUAL, "b", BRIEF_B)
        assert history.status_code == 200 and history.json()["total"] == 0
        assert world.queue(DUAL, WS_A2).status_code == 200
        assert world.approvals() == [
            (BRIEF_A1, 1, 1, "approved", ORG_A, WS_A1, "reviewer", BRIEF_A1)
        ]
        assert world.approval_audit() == [(ORG_A, WS_A1, "approval.approved", BRIEF_A1)]


# --------------------------------------------------------------------------- #
# A brief that vanishes before the lock answers not-found and writes nothing
# --------------------------------------------------------------------------- #
class TestVanishedSubject:
    """Kills W4B-M05 (``_lock_subject`` treats a missing row as version 1): the insert then
    fails on the brief foreign key, a 500 instead of the non-enumerating 404."""

    def test_a_brief_deleted_after_it_was_read_is_not_found_and_nothing_is_written(
        self, world: World
    ):
        with world.factory() as s:
            ctx = _ctx(s, A_REVIEWER, WS_A1, Role.REVIEWER)
            brief = s.get(RecommendationBrief, BRIEF_A1)  # the route's scoped read
            # The brief goes (a workspace or privacy deletion is a Core DELETE) ...
            with world.witness.begin() as c:
                c.execute(
                    delete(RecommendationBrief.__table__).where(
                        RecommendationBrief.__table__.c.id == BRIEF_A1
                    )
                )
            # ... before the decision takes the subject lock.
            with pytest.raises(NotFoundError) as refused:
                svc.record_decision(
                    s, ctx=ctx, brief=brief, decision=ApprovalDecision.APPROVED, subject_version=1
                )
            assert refused.value.message == svc.BRIEF_NOT_FOUND
            s.rollback()
        assert world.approvals() == [] and world.approval_audit() == []

    def test_a_decision_outlives_its_brief_with_its_exact_binding(self, world: World, briefs_on):
        """Kills W4B-M10 (the approval's brief foreign key cascades): the compliance record
        must survive its subject (O-6), still naming the exact subject and version."""
        assert world.decide(A_REVIEWER, "a1", BRIEF_A1).status_code == 201
        with world.witness.begin() as c:
            c.execute(
                delete(RecommendationBrief.__table__).where(
                    RecommendationBrief.__table__.c.id == BRIEF_A1
                )
            )
        assert world.approvals() == [(BRIEF_A1, 1, 1, "approved", ORG_A, WS_A1, "reviewer", None)]
        assert world.approval_audit() == [(ORG_A, WS_A1, "approval.approved", BRIEF_A1)]


# --------------------------------------------------------------------------- #
# PostgreSQL races (READ COMMITTED, real sessions in real threads)
# --------------------------------------------------------------------------- #
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

    def outcome(self, seconds: float = JOIN_SECONDS):
        self.join(seconds)
        assert not self.is_alive(), "a racer did not finish"
        if self.error is not None:
            raise self.error
        return self.result


def _decide(
    w: World,
    user_id: str,
    ws_id: str,
    brief_id: str,
    decision: ApprovalDecision,
    *,
    loaded: threading.Event | None = None,
    go: threading.Event | None = None,
    holding: threading.Event | None = None,
    hold: threading.Event | None = None,
) -> tuple:
    """One decision in its own session, as the route makes it.

    ``loaded``/``go``: signal once the brief was read (the route's scoped read), then wait.
    ``holding``/``hold``: signal once the decision is flushed (subject lock held, row
    uncommitted), then wait before committing.
    """
    with w.factory() as s:
        ctx = _ctx(s, user_id, ws_id, Role.REVIEWER)
        brief = s.get(RecommendationBrief, brief_id)
        if loaded is not None:
            loaded.set()
            assert go is not None and go.wait(JOIN_SECONDS)
        try:
            approval = svc.record_decision(
                s, ctx=ctx, brief=brief, decision=decision, subject_version=1
            )
        except (ConflictError, NotFoundError) as exc:
            s.rollback()
            return ("refused", exc.code)
        if holding is not None:
            holding.set()
            assert hold is not None and hold.wait(JOIN_SECONDS)
        s.commit()
        return ("ok", approval.decision_seq, approval.decision)


def _delete_brief(w: World, brief_id: str, *, holding=None, hold=None) -> str:
    """A Core DELETE of the brief (the workspace and privacy deletion path), in its own
    transaction; optionally held open after the statement ran."""
    with w.engine.connect() as c:
        with c.begin():
            c.execute(
                delete(RecommendationBrief.__table__).where(
                    RecommendationBrief.__table__.c.id == brief_id
                )
            )
            if holding is not None:
                holding.set()
                assert hold is not None and hold.wait(JOIN_SECONDS)
    return "deleted"


def _lock_waiters(w: World) -> list[str]:
    """Queries of this database now blocked on a heavyweight lock."""
    with w.witness.connect() as c:
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


def _blocked_on_the_subject_lock(w: World) -> list[str]:
    return [q for q in _lock_waiters(w) if "FOR UPDATE" in q and "recommendation_briefs" in q]


class TestPostgresRaces:
    """READ COMMITTED on PostgreSQL. W4B-M04 (the controlling decision read before the
    subject lock) is killed in both race directions; W4B-M05 by the deleted-first race;
    W4B-M10 (the approval's brief foreign key cascades) by the decided-first race; W4B-M11
    (the subject lock taken on every brief) by the cross-tenant race."""

    def test_a_revoke_racing_a_reject_reads_the_committed_reject_under_the_lock(
        self, pg_world: World
    ):
        w = pg_world
        assert _decide(w, A_REVIEWER, WS_A1, BRIEF_A1, ApprovalDecision.APPROVED) == (
            "ok",
            1,
            "approved",
        )
        holding, hold = threading.Event(), threading.Event()
        reject = _Runner(
            _decide, w, A_REVIEWER2, WS_A1, BRIEF_A1, ApprovalDecision.REJECTED,
            holding=holding, hold=hold,
        )  # fmt: skip
        reject.start()
        assert holding.wait(JOIN_SECONDS), "the reject never flushed"
        revoke = _Runner(_decide, w, A_REVIEWER, WS_A1, BRIEF_A1, ApprovalDecision.REVOKED)
        revoke.start()
        try:
            _wait_for(lambda: _blocked_on_the_subject_lock(w), "the revoke to block")
        finally:
            hold.set()
        assert reject.outcome() == ("ok", 2, "rejected")
        # The revoke read the committed reject under the lock: nothing to revoke.
        assert revoke.outcome() == ("refused", "approval_nothing_to_revoke")
        assert [a[2:4] for a in w.approvals()] == [(1, "approved"), (2, "rejected")]
        assert len(w.approval_audit()) == 2

    def test_a_revoke_racing_an_approve_reads_the_committed_approve(self, pg_world: World):
        w = pg_world
        holding, hold = threading.Event(), threading.Event()
        approve = _Runner(
            _decide, w, A_REVIEWER2, WS_A1, BRIEF_A1, ApprovalDecision.APPROVED,
            holding=holding, hold=hold,
        )  # fmt: skip
        approve.start()
        assert holding.wait(JOIN_SECONDS), "the approve never flushed"
        revoke = _Runner(_decide, w, A_REVIEWER, WS_A1, BRIEF_A1, ApprovalDecision.REVOKED)
        revoke.start()
        try:
            _wait_for(lambda: _blocked_on_the_subject_lock(w), "the revoke to block")
        finally:
            hold.set()
        assert approve.outcome() == ("ok", 1, "approved")
        assert revoke.outcome() == ("ok", 2, "revoked")
        assert [a[2:4] for a in w.approvals()] == [(1, "approved"), (2, "revoked")]

    def test_a_brief_deleted_while_a_decider_waits_is_not_found_and_nothing_is_written(
        self, pg_world: World
    ):
        w = pg_world
        loaded, go = threading.Event(), threading.Event()
        decider = _Runner(
            _decide, w, A_REVIEWER, WS_A1, BRIEF_A1, ApprovalDecision.APPROVED,
            loaded=loaded, go=go,
        )  # fmt: skip
        decider.start()
        assert loaded.wait(JOIN_SECONDS), "the decider never read the brief"
        deleting, release = threading.Event(), threading.Event()
        deleter = _Runner(_delete_brief, w, BRIEF_A1, holding=deleting, hold=release)
        deleter.start()
        try:
            assert deleting.wait(JOIN_SECONDS), "the delete never ran"
            go.set()
            _wait_for(lambda: _blocked_on_the_subject_lock(w), "the decider to block")
        finally:
            go.set()
            release.set()
        assert deleter.outcome() == "deleted"
        assert decider.outcome() == ("refused", "not_found")
        assert w.approvals() == [] and w.approval_audit() == []

    def test_a_decision_committed_before_its_brief_is_deleted_keeps_its_exact_binding(
        self, pg_world: World
    ):
        w = pg_world
        holding, hold = threading.Event(), threading.Event()
        decider = _Runner(
            _decide, w, A_REVIEWER, WS_A1, BRIEF_A1, ApprovalDecision.APPROVED,
            holding=holding, hold=hold,
        )  # fmt: skip
        decider.start()
        assert holding.wait(JOIN_SECONDS), "the decider never flushed"
        deleter = _Runner(_delete_brief, w, BRIEF_A1)
        deleter.start()
        try:
            _wait_for(lambda: _lock_waiters(w), "the delete to wait behind the decision")
        finally:
            hold.set()
        assert decider.outcome() == ("ok", 1, "approved")
        assert deleter.outcome() == "deleted"
        # The compliance record survives its subject, still bound to the exact version.
        assert w.approvals() == [(BRIEF_A1, 1, 1, "approved", ORG_A, WS_A1, "reviewer", None)]
        assert w.approval_audit() == [(ORG_A, WS_A1, "approval.approved", BRIEF_A1)]

    @pytest.mark.parametrize(
        ("other_user", "other_ws", "other_brief"),
        [(A_REVIEWER2, WS_A2, BRIEF_A2), (B_REVIEWER, WS_B, BRIEF_B)],
        ids=["sibling-workspace", "other-organization"],
    )
    def test_one_tenants_in_flight_decision_never_blocks_another_tenants(
        self, pg_world: World, other_user: str, other_ws: str, other_brief: str
    ):
        w = pg_world
        holding, hold = threading.Event(), threading.Event()
        first = _Runner(
            _decide, w, A_REVIEWER, WS_A1, BRIEF_A1, ApprovalDecision.APPROVED,
            holding=holding, hold=hold,
        )  # fmt: skip
        first.start()
        assert holding.wait(JOIN_SECONDS), "the first decider never flushed"
        other = _Runner(_decide, w, other_user, other_ws, other_brief, ApprovalDecision.APPROVED)
        other.start()
        try:
            # The other tenant's decision completes while the first still holds its lock.
            other.join(POLL_SECONDS)
            assert not other.is_alive(), "another tenant's decision waited on this one"
            assert _blocked_on_the_subject_lock(w) == []
        finally:
            hold.set()
        assert other.outcome() == ("ok", 1, "approved")
        assert first.outcome() == ("ok", 1, "approved")
        with w.witness.connect() as c:
            assert c.execute(select(func.count()).select_from(Approval.__table__)).scalar_one() == 2
