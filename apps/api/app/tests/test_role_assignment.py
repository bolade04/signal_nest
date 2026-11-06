"""Phase 5B-W4 adversarial battery: role assignment and role ceilings (W21 lane ADV5AB).

Source: ``docs/project-phase-5b-approval-audit-spine.md`` §2 ("Privilege ceiling (enforced +
negative-tested)", "Separation of duties (enforced in service + tested)"), §4 exit criteria
2 and 3, and the §5 W4 row ("role-ceiling negatives").

Already proven elsewhere, and not repeated here: the full actor x current x new HTTP grid,
self-change, last-owner and owner-only grants (``test_organization_members_api.py``, #181);
the exact-set dependency (``test_exact_role_authorization.py``, #179); the org-wide grant
spanning two workspaces (``test_approvals_api.py``); two owners acting on each other on
PostgreSQL (``test_organization_invitation_database.py``). This module adds what those
leave unpinned. Each class names the mutant it kills (lane evidence ``P6-W21-ADV5AB-01``):

* **The approver ceiling holds in the service.** ``record_decision`` refuses every role but
  REVIEWER on its own, not only behind the route's ``require_approver``.
* **The privilege ceiling stands on its own.** With today's roles every above-own-rank
  attempt is already stopped by the owner rule, so the ceiling is never what refuses over
  HTTP. Widening the administering set (as a later change might) shows the ceiling itself
  refusing a grant above the actor's rank, a change to a member ranked above the actor, and
  a removal of one, while equal-or-lower grants still pass.
* **A grant stays in its organization.** For a member of two organizations, a grant in one
  changes nothing in the other, and confers no approver authority there.
* **The actor's authority is the one committed under the lock.** A membership already
  loaded in the request's session is re-read, not trusted; on PostgreSQL a demotion that
  commits while a grant waits for the organization lock is seen by that grant.

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
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.approvals import service as approvals_service
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.auth.dependencies import OrganizationContext, TenantContext
from app.briefs.models import RecommendationBrief
from app.core.config import get_settings
from app.core.enums import ApprovalDecision, Role
from app.core.errors import PermissionDeniedError
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations import members as member_service
from app.organizations.models import Organization, OrganizationMember, User, Workspace
from app.tests._approvals_support import add_brief, add_evidence, add_org, add_user
from app.tests._auth2_support import (
    active_rate_limiter,
    live_bearer,
    pg_database,
    require_postgres,
)

API = get_settings().api_prefix

ORG_A, WS_A1, WS_A2 = "org-w4ra-a", "ws-w4ra-a1", "ws-w4ra-a2"
ORG_B, WS_B = "org-w4ra-b", "ws-w4ra-b"

OWNER, ADMIN, MARKETER = "w4ra-owner", "w4ra-admin", "w4ra-marketer"
REVIEWER, COMPLIANCE, VIEWER = "w4ra-reviewer", "w4ra-compliance", "w4ra-viewer"
T_VIEWER, T_ADMIN, T_MARKETER = "w4ra-t-viewer", "w4ra-t-admin", "w4ra-t-marketer"
PROMO = "w4ra-promo"  # MARKETER in A and MARKETER in B
B_OWNER, B_MARKETER = "w4ra-b-owner", "w4ra-b-marketer"

ROLE_IN_A = {
    OWNER: Role.OWNER,
    ADMIN: Role.ADMIN,
    MARKETER: Role.MARKETER,
    REVIEWER: Role.REVIEWER,
    COMPLIANCE: Role.COMPLIANCE_REVIEWER,
    VIEWER: Role.VIEWER,
    T_VIEWER: Role.VIEWER,
    T_ADMIN: Role.ADMIN,
    T_MARKETER: Role.MARKETER,
    PROMO: Role.MARKETER,
}
BY_ROLE = {role: uid for uid, role in list(ROLE_IN_A.items())[:6]}

BRIEF_A1, BRIEF_A2, BRIEF_B = "w4ra-brief-a1", "w4ra-brief-a2", "w4ra-brief-b"

ROLE_DENIAL = "Role '{role}' is not permitted for this action."
CEILING = "member_role_ceiling"

JOIN_SECONDS = 60.0
POLL_SECONDS = 20.0


# --------------------------------------------------------------------------- #
# The world
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

    def send(self, method: str, url: str, user_id: str, body=None):
        if user_id not in self.tokens:
            self.tokens[user_id] = live_bearer(user_id)
        active_rate_limiter()._hits.clear()
        kwargs: dict = {"headers": {"Authorization": f"Bearer {self.tokens[user_id]}"}}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, url, **kwargs)

    def put_role(self, actor: str, target: str, role: str, org: str = ORG_A):
        return self.send(
            "PUT", f"{API}/organizations/{org}/members/{target}/role", actor, {"role": role}
        )

    def decide(self, user_id: str, key: str, brief_id: str):
        ev = self.ev[key]
        url = (
            f"{API}/workspaces/{ev.ws_id}/opportunities/{ev.opportunity_id}"
            f"/briefs/{brief_id}/approvals"
        )
        return self.send("POST", url, user_id, {"decision": "approved", "subject_version": 1})

    # -- witness ---------------------------------------------------------------------
    def role(self, user_id: str, org: str = ORG_A) -> str | None:
        t = OrganizationMember.__table__
        with self.witness.connect() as c:
            return c.execute(
                select(t.c.role).where(t.c.user_id == user_id, t.c.organization_id == org)
            ).scalar_one_or_none()

    def member_audit(self) -> list[tuple]:
        t = AuditLog.__table__
        with self.witness.connect() as c:
            rows = c.execute(
                select(
                    t.c.organization_id,
                    t.c.action,
                    t.c.entity_id,
                    t.c.actor_user_id,
                    t.c.previous_state,
                    t.c.new_state,
                )
                .where(t.c.action.like("organization_member.%"))
                .order_by(t.c.created_at, t.c.id)
            ).all()
        return [tuple(r) for r in rows]

    def approvals(self) -> int:
        with self.witness.connect() as c:
            return c.execute(select(func.count()).select_from(Approval.__table__)).scalar_one()


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
    users = {uid: add_user(s, uid, org_id=ORG_A, role=role) for uid, role in ROLE_IN_A.items()}
    users[B_OWNER] = add_user(s, B_OWNER, org_id=ORG_B, role=Role.OWNER)
    users[B_MARKETER] = add_user(s, B_MARKETER, org_id=ORG_B, role=Role.MARKETER)
    s.add(
        OrganizationMember(
            id="m-w4ra-promo-b", organization_id=ORG_B, user_id=PROMO, role=Role.MARKETER.value
        )
    )
    s.flush()
    ev = {
        "a1": add_evidence(s, org_id=ORG_A, ws_id=WS_A1, key="w4ra-a1"),
        "a2": add_evidence(s, org_id=ORG_A, ws_id=WS_A2, key="w4ra-a2"),
        "b": add_evidence(s, org_id=ORG_B, ws_id=WS_B, key="w4ra-b"),
    }
    add_brief(s, ev["a1"], brief_id=BRIEF_A1, author=users[MARKETER])
    add_brief(s, ev["a2"], brief_id=BRIEF_A2, author=users[MARKETER])
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
        db_file = tmp_path / "w4-roles.db"
        yield from _build("sqlite", _sqlite(db_file), _sqlite(db_file))
        return
    require_postgres()
    with pg_database("sn_w4ra") as url:  # pragma: no cover - gated on PostgreSQL
        yield from _build(
            "postgresql", _postgres(url), _postgres(url, isolation_level="AUTOCOMMIT")
        )


@pytest.fixture
def pg_world() -> Iterator[World]:
    require_postgres()
    with pg_database("sn_w4rr") as url:  # pragma: no cover - gated on PostgreSQL
        yield from _build(
            "postgresql", _postgres(url), _postgres(url, isolation_level="AUTOCOMMIT")
        )


@pytest.fixture(autouse=True)
def _dark_by_default(monkeypatch):
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", False)


@pytest.fixture
def briefs_on(monkeypatch):
    monkeypatch.setattr(get_settings(), "recommendation_briefs_enabled", True)


def _org_ctx(s, user_id: str, role: Role, org: str = ORG_A) -> OrganizationContext:
    return OrganizationContext(
        user=s.get(User, user_id), organization=s.get(Organization, org), role=role
    )


def _error(r) -> tuple:
    err = (r.json() or {}).get("error") or {}
    return r.status_code, err.get("code"), err.get("message")


# --------------------------------------------------------------------------- #
# The approver ceiling holds in the service (exit criterion 2; plan §2 "enforced in
# service + tested")
# --------------------------------------------------------------------------- #
class TestApproverCeilingInTheService:
    """Kills W4B-M01 (``record_decision``'s role re-check removed; review W21-REV-P5B r1
    RM8, which survives the pre-existing approval modules because the route gate answers
    first)."""

    @pytest.mark.parametrize("role", [r for r in Role if r is not Role.REVIEWER], ids=str)
    def test_every_role_but_reviewer_is_refused_by_the_service_itself(
        self, world: World, role: Role
    ):
        with world.factory() as s:
            workspace = s.get(Workspace, WS_A1)
            ctx = TenantContext(
                user=s.get(User, BY_ROLE[role]),
                organization=s.get(Organization, ORG_A),
                workspace=workspace,
                role=role,
            )
            brief = s.get(RecommendationBrief, BRIEF_A1)
            with pytest.raises(PermissionDeniedError) as refused:
                approvals_service.record_decision(
                    s, ctx=ctx, brief=brief, decision=ApprovalDecision.APPROVED, subject_version=1
                )
            assert (refused.value.code, refused.value.message) == (
                "permission_denied",
                ROLE_DENIAL.format(role=role.value),
            )
            s.rollback()
        assert world.approvals() == 0

    def test_positive_control_the_reviewer_decides_through_the_service(self, world: World):
        with world.factory() as s:
            ctx = TenantContext(
                user=s.get(User, REVIEWER),
                organization=s.get(Organization, ORG_A),
                workspace=s.get(Workspace, WS_A1),
                role=Role.REVIEWER,
            )
            brief = s.get(RecommendationBrief, BRIEF_A1)
            approval = approvals_service.record_decision(
                s, ctx=ctx, brief=brief, decision=ApprovalDecision.APPROVED, subject_version=1
            )
            assert approval.decided_role == "reviewer"
            s.commit()
        assert world.approvals() == 1


# --------------------------------------------------------------------------- #
# The privilege ceiling stands on its own (plan §2 "no actor may assign a role ranked
# above their own")
# --------------------------------------------------------------------------- #
class TestPrivilegeCeilingOnItsOwn:
    """Kills W4B-M07 (the ceiling's new-role half removed), W4B-M08 (its current-role half
    removed) and W4B-M09 (the removal ceiling removed): with today's two administering roles
    each is unreachable over HTTP (the owner rule answers first), so all three survive
    ``test_organization_members_api.py``. Also W4B-M15 (``>`` → ``>=``: equal rank refused).
    """

    @pytest.fixture
    def marketers_administer(self, monkeypatch):
        # A hypothetical widening of the administering set: the ceiling, not the owner rule,
        # must then be what stops a marketer (rank 2) from reaching admin (rank 3).
        monkeypatch.setattr(
            member_service,
            "ORGANIZATION_ADMIN_ROLES",
            frozenset({Role.OWNER, Role.ADMIN, Role.MARKETER}),
        )

    def _change(self, world: World, target: str, new_role: Role):
        with world.factory() as s:
            try:
                row = member_service.change_member_role(
                    s, ctx=_org_ctx(s, MARKETER, Role.MARKETER), target_user_id=target,
                    new_role=new_role,
                )  # fmt: skip
            except PermissionDeniedError as exc:
                s.rollback()
                return exc.code
            s.commit()
            return row.role.value

    def _remove(self, world: World, target: str):
        with world.factory() as s:
            try:
                member_service.remove_member(
                    s, ctx=_org_ctx(s, MARKETER, Role.MARKETER), target_user_id=target
                )
            except PermissionDeniedError as exc:
                s.rollback()
                return exc.code
            s.commit()
            return "removed"

    def test_no_grant_above_the_actors_own_rank(self, world: World, marketers_administer):
        assert self._change(world, T_VIEWER, Role.ADMIN) == CEILING
        assert world.role(T_VIEWER) == "viewer"
        assert world.member_audit() == []

    def test_no_change_to_a_member_ranked_above_the_actor(self, world: World, marketers_administer):
        for new_role in (Role.VIEWER, Role.REVIEWER, Role.MARKETER):
            assert self._change(world, T_ADMIN, new_role) == CEILING, new_role
        assert world.role(T_ADMIN) == "admin"
        assert world.member_audit() == []

    def test_no_removal_of_a_member_ranked_above_the_actor(
        self, world: World, marketers_administer
    ):
        assert self._remove(world, T_ADMIN) == CEILING
        assert world.role(T_ADMIN) == "admin"
        assert world.member_audit() == []

    def test_positive_control_grants_at_or_below_the_actors_rank_pass(
        self, world: World, marketers_administer
    ):
        assert self._change(world, T_VIEWER, Role.REVIEWER) == "reviewer"
        assert self._change(world, T_VIEWER, Role.MARKETER) == "marketer"  # equal rank
        assert self._change(world, T_MARKETER, Role.VIEWER) == "viewer"  # equal-rank target
        assert [(a[2], a[4], a[5]) for a in world.member_audit()] == [
            (T_VIEWER, {"role": "viewer"}, {"role": "reviewer"}),
            (T_VIEWER, {"role": "reviewer"}, {"role": "marketer"}),
            (T_MARKETER, {"role": "marketer"}, {"role": "viewer"}),
        ]

    def test_the_route_gate_is_independent_of_the_service_set(
        self, world: World, marketers_administer
    ):
        # The two layers are separate: the route's exact OWNER/ADMIN gate still refuses.
        r = world.put_role(MARKETER, T_VIEWER, "reviewer")
        assert _error(r) == (403, "permission_denied", ROLE_DENIAL.format(role="marketer"))
        assert world.role(T_VIEWER) == "viewer"


# --------------------------------------------------------------------------- #
# A grant stays in its organization (exit criterion 3, the org-wide grant's boundary)
# --------------------------------------------------------------------------- #
class TestGrantStaysInItsOrganization:
    """Kills W4B-M06 (the request's membership lookup ignores the organization) and
    W4B-M16 (``load_membership`` ignores the organization)."""

    def test_a_reviewer_grant_in_one_organization_changes_nothing_in_the_other(
        self, world: World, briefs_on
    ):
        grant = world.put_role(OWNER, PROMO, "reviewer")
        assert grant.status_code == 200, grant.text
        assert grant.json()["role"] == "reviewer"
        assert (world.role(PROMO, ORG_A), world.role(PROMO, ORG_B)) == ("reviewer", "marketer")
        assert world.member_audit() == [
            (ORG_A, "organization_member.role_changed", PROMO, OWNER,
             {"role": "marketer"}, {"role": "reviewer"}),
        ]  # fmt: skip
        # Approver in both workspaces of A, and not in B.
        assert world.decide(PROMO, "a1", BRIEF_A1).status_code == 201
        assert world.decide(PROMO, "a2", BRIEF_A2).status_code == 201
        refused = world.decide(PROMO, "b", BRIEF_B)
        assert _error(refused) == (403, "permission_denied", ROLE_DENIAL.format(role="marketer"))
        assert world.approvals() == 2

    def test_another_organizations_owner_cannot_reach_the_grant(self, world: World):
        # Class (b): B's owner holds no role in A.
        r = world.put_role(B_OWNER, PROMO, "reviewer", org=ORG_A)
        assert _error(r) == (403, "permission_denied", "You are not a member of this organization.")
        assert (world.role(PROMO, ORG_A), world.role(PROMO, ORG_B)) == ("marketer", "marketer")
        assert world.member_audit() == []


# --------------------------------------------------------------------------- #
# The actor's authority is the one committed under the lock
# --------------------------------------------------------------------------- #
class TestAuthorityIsReadUnderTheLock:
    """Kills W4B-M14 (``load_membership`` loses ``populate_existing``, so the membership the
    request already loaded is trusted) and, on PostgreSQL, W4B-M13 (the actor re-read moved
    ahead of the organization lock)."""

    def test_a_membership_already_in_the_session_is_re_read(self, world: World):
        with world.factory() as s:
            # What get_organization_context leaves in the request's identity map.
            stale = s.scalar(
                select(OrganizationMember).where(
                    OrganizationMember.organization_id == ORG_A,
                    OrganizationMember.user_id == ADMIN,
                )
            )
            assert stale is not None and stale.role == "admin"
            ctx = _org_ctx(s, ADMIN, Role.ADMIN)
            # The owner demotes the admin; that commits before the admin's grant runs.
            with world.witness.begin() as c:
                c.execute(
                    OrganizationMember.__table__.update()
                    .where(
                        OrganizationMember.__table__.c.organization_id == ORG_A,
                        OrganizationMember.__table__.c.user_id == ADMIN,
                    )
                    .values(role=Role.VIEWER.value)
                )
            with pytest.raises(PermissionDeniedError) as refused:
                member_service.change_member_role(
                    s, ctx=ctx, target_user_id=T_MARKETER, new_role=Role.REVIEWER
                )
            assert refused.value.message == ROLE_DENIAL.format(role="viewer")
            s.rollback()
        assert world.role(T_MARKETER) == "marketer"
        assert world.member_audit() == []

    def test_a_demotion_committed_while_a_grant_waits_on_the_lock_is_seen(self, pg_world: World):
        w = pg_world
        holding, hold = threading.Event(), threading.Event()

        def demote():
            with w.factory() as s:
                member_service.change_member_role(
                    s, ctx=_org_ctx(s, OWNER, Role.OWNER), target_user_id=ADMIN,
                    new_role=Role.VIEWER,
                )  # fmt: skip
                holding.set()
                assert hold.wait(JOIN_SECONDS)
                s.commit()
            return "demoted"

        def grant():
            with w.factory() as s:
                # The request's dependency resolved ADMIN before the demotion committed.
                s.scalar(
                    select(OrganizationMember).where(
                        OrganizationMember.organization_id == ORG_A,
                        OrganizationMember.user_id == ADMIN,
                    )
                )
                try:
                    member_service.change_member_role(
                        s, ctx=_org_ctx(s, ADMIN, Role.ADMIN), target_user_id=T_MARKETER,
                        new_role=Role.REVIEWER,
                    )  # fmt: skip
                except PermissionDeniedError as exc:
                    s.rollback()
                    return ("refused", exc.message)
                s.commit()
                return ("granted",)

        demoter = _Runner(demote)
        demoter.start()
        assert holding.wait(JOIN_SECONDS), "the demotion never flushed"
        granter = _Runner(grant)
        granter.start()
        try:
            _wait_for(lambda: _organization_lock_waiters(w), "the grant to wait on the lock")
        finally:
            hold.set()
        assert demoter.outcome() == "demoted"
        assert granter.outcome() == ("refused", ROLE_DENIAL.format(role="viewer"))
        assert (w.role(ADMIN), w.role(T_MARKETER)) == ("viewer", "marketer")
        assert [(a[2], a[5]) for a in w.member_audit()] == [(ADMIN, {"role": "viewer"})]


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
        assert not self.is_alive(), "a racer did not finish"
        if self.error is not None:
            raise self.error
        return self.result


def _organization_lock_waiters(w: World) -> list[str]:
    with w.witness.connect() as c:
        rows = c.execute(
            text(
                "SELECT query FROM pg_stat_activity "
                "WHERE datname = current_database() AND wait_event_type = 'Lock'"
            )
        ).all()
    return [r[0] for r in rows if "FOR UPDATE" in r[0] and "organizations" in r[0]]


def _wait_for(predicate, what: str):
    deadline = time.monotonic() + POLL_SECONDS
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what}")
