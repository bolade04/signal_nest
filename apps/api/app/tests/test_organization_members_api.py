"""6B-3A: organization members -- list, change role, remove (P6-AUTH-1).

    GET    /api/v1/organizations/{organization_id}/members              any member
    PUT    /api/v1/organizations/{organization_id}/members/{user_id}/role  exact OWNER/ADMIN
    DELETE /api/v1/organizations/{organization_id}/members/{user_id}       exact OWNER/ADMIN

The rules (Phase-5B design authority, as implemented in ``app.organizations.members``):
only OWNER/ADMIN manage members; nobody changes or removes themselves; only an OWNER
grants, changes or removes OWNER; nobody assigns or manages above their own rank; the last
OWNER can be neither demoted nor removed. Removal deletes the membership row only.

What runs for real: only ``get_db`` is overridden; committed state is read through a
separate ``Engine``. Every matrix cell starts from a known committed state written by the
witness, sends one request, and checks the status and code, the committed role, the audit
trail and the OWNER count.

The two matrices are written out as literal grids, not computed from the rules: a rule the
implementation got wrong cannot also be wrong in the expectation.

Reachability, disclosed. Three service codes are defence in depth and cannot be produced
through any consistent state: ``member_role_ceiling`` (an actor is OWNER or ADMIN, OWNER
targets and grants are caught first by ``member_owner_required``, and nothing else outranks
an ADMIN), ``organization_last_owner`` (an OWNER acting on another OWNER means two OWNERs
exist, and nobody may act on themselves), and the post-write owner backstop. The invariants
they protect -- no grant above the actor, at least one OWNER always -- are asserted after
every cell instead. The PostgreSQL concurrency module covers the race the lock serializes.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select, update
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.auth.dependencies import OrganizationContext
from app.core.config import get_settings
from app.core.enums import Role
from app.core.errors import PermissionDeniedError
from app.core.middleware import RateLimitMiddleware
from app.core.security import create_access_token
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations import members as member_service
from app.organizations.models import Organization, OrganizationMember, User, Workspace

API = get_settings().api_prefix
ORG_A, ORG_B, WS_A = "org-mem-a", "org-mem-b", "ws-mem-a"
TARGET = "mem-target"  # member of A (role set per cell) and of B (viewer)
B_ONLY = "mem-b-only"  # member of B only
OUTSIDER, OPERATOR = "mem-outsider", "mem-operator"
ALL_ROLES = (
    Role.OWNER,
    Role.ADMIN,
    Role.MARKETER,
    Role.REVIEWER,
    Role.COMPLIANCE_REVIEWER,
    Role.VIEWER,
)
NON_ADMINS = (Role.MARKETER, Role.REVIEWER, Role.COMPLIANCE_REVIEWER, Role.VIEWER)
MEMBER_FIELDS = {"user_id", "email", "full_name", "role", "created_at"}
NOT_A_MEMBER = "You are not a member of this organization."
T0 = datetime(2026, 9, 1, tzinfo=UTC)


def _actor(role: Role) -> str:
    return f"mem-a-{role.value[:3]}"


def _email(user_id: str) -> str:
    return f"{user_id}@example.com"


def _role_denial(role: Role | str) -> str:
    return f"Role '{Role(role).value}' is not permitted for this action."


def _active_rate_limiter() -> RateLimitMiddleware:
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None and not isinstance(node, RateLimitMiddleware):
        node = getattr(node, "app", None)
    assert node is not None
    return node


def _sqlite_file_engine(db_file: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _seed(s) -> None:
    s.add(Organization(id=ORG_A, name="Members A", slug="members-a"))
    s.add(Organization(id=ORG_B, name="Members B", slug="members-b"))
    s.flush()
    s.add(Workspace(id=WS_A, organization_id=ORG_A, name="WS", slug="ws"))
    users = [_actor(r) for r in ALL_ROLES] + [TARGET, B_ONLY, OUTSIDER, OPERATOR]
    for user_id in users:
        s.add(
            User(
                id=user_id,
                email=_email(user_id),
                full_name=user_id.title(),
                hashed_password="x",
                is_operator=user_id == OPERATOR,
            )
        )
    s.flush()
    # Distinct, ordered membership times so the list order is a checkable property.
    rows = [(ORG_A, _actor(r), r.value) for r in ALL_ROLES] + [
        (ORG_A, TARGET, "marketer"),
        (ORG_B, TARGET, "viewer"),
        (ORG_B, B_ONLY, "owner"),
    ]
    for i, (org, user_id, role) in enumerate(rows):
        s.add(
            OrganizationMember(
                id=f"m-{i:02d}",
                organization_id=org,
                user_id=user_id,
                role=role,
                created_at=T0 + timedelta(minutes=i),
            )
        )
    s.commit()


@dataclass(frozen=True)
class Witness:
    engine: Engine

    def session(self):
        return sessionmaker(bind=self.engine, autoflush=False, future=True)()

    def role(self, user_id: str, org: str = ORG_A) -> str | None:
        with self.session() as s:
            return s.scalar(
                select(OrganizationMember.role).where(
                    OrganizationMember.user_id == user_id, OrganizationMember.organization_id == org
                )
            )

    def owners(self, org: str = ORG_A) -> int:
        with self.session() as s:
            return s.scalar(
                select(func.count())
                .select_from(OrganizationMember)
                .where(
                    OrganizationMember.organization_id == org, OrganizationMember.role == "owner"
                )
            )

    def memberships(self) -> set[tuple[str, str, str]]:
        with self.session() as s:
            return set(
                s.execute(
                    select(
                        OrganizationMember.user_id,
                        OrganizationMember.organization_id,
                        OrganizationMember.role,
                    )
                )
            )

    def audit(self) -> list[dict]:
        with self.session() as s:
            return [
                {c.key: getattr(r, c.key) for c in AuditLog.__table__.c}
                for r in s.scalars(select(AuditLog).order_by(AuditLog.created_at, AuditLog.id))
            ]

    def user_exists(self, user_id: str) -> bool:
        with self.session() as s:
            return s.get(User, user_id) is not None

    def set_role(self, user_id: str, role: Role | str, org: str = ORG_A) -> None:
        with self.engine.begin() as conn:
            n = conn.execute(
                update(OrganizationMember)
                .where(
                    OrganizationMember.user_id == user_id, OrganizationMember.organization_id == org
                )
                .values(role=Role(role).value)
            ).rowcount
        assert n == 1, (user_id, org)

    def ensure_member(self, user_id: str, role: Role | str, org: str = ORG_A) -> None:
        if self.role(user_id, org) is None:
            with self.engine.begin() as conn:
                conn.execute(
                    OrganizationMember.__table__.insert().values(
                        id=f"m-re-{user_id[-6:]}",
                        organization_id=org,
                        user_id=user_id,
                        role=Role(role).value,
                        created_at=T0,
                        updated_at=T0,
                    )
                )
        else:
            self.set_role(user_id, role, org)

    def clear_audit(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(AuditLog.__table__.delete())


@dataclass(frozen=True)
class Env:
    client: TestClient
    witness: Witness
    request_factory: sessionmaker
    request_engine: Engine


@contextmanager
def _environment(db_file: Path, *, teardown_commit: bool = True) -> Iterator[Env]:
    request_engine = _sqlite_file_engine(db_file)
    witness_engine = _sqlite_file_engine(db_file)
    Base.metadata.create_all(request_engine)
    factory = sessionmaker(
        bind=request_engine, autoflush=False, expire_on_commit=False, future=True
    )
    with factory() as s:
        _seed(s)

    def _override_get_db():
        s = factory()
        try:
            yield s
            if teardown_commit:
                s.commit()
            else:
                s.rollback()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    _active_rate_limiter()._hits.clear()
    try:
        yield Env(TestClient(app), Witness(witness_engine), factory, request_engine)
    finally:
        app.dependency_overrides.clear()
        _active_rate_limiter()._hits.clear()
        request_engine.dispose()
        witness_engine.dispose()


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with _environment(tmp_path / "members.db") as e:
        yield e


@pytest.fixture
def withheld_env(tmp_path) -> Iterator[Env]:
    with _environment(tmp_path / "members-withheld.db", teardown_commit=False) as e:
        yield e


def _send(env: Env, method: str, url: str, headers: dict[str, str], body: object = None):
    assert set(app.dependency_overrides) == {get_db}, "only get_db may be overridden"
    _active_rate_limiter()._hits.clear()
    if body is None:
        return env.client.request(method, url, headers=headers)
    return env.client.request(method, url, headers=headers, json=body)


def _bearer(user_id: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user_id)}"}


def _members_url(org: str = ORG_A) -> str:
    return f"{API}/organizations/{org}/members"


def _put_role(
    env: Env, actor: str, target: str, role: str, org: str = ORG_A, extra: dict | None = None
):
    return _send(
        env,
        "PUT",
        f"{_members_url(org)}/{target}/role",
        _bearer(actor),
        {"role": role, **(extra or {})},
    )


def _remove(env: Env, actor: str, target: str, org: str = ORG_A):
    return _send(env, "DELETE", f"{_members_url(org)}/{target}", _bearer(actor))


def _outcome(r) -> int | str:
    """200/204, or the error code; a role denial is ``ROLE`` (its message is checked apart)."""
    if r.status_code in (200, 204):
        return r.status_code
    err = r.json()["error"]
    if err["code"] == "permission_denied" and err["message"].startswith("Role '"):
        return "ROLE"
    return err["code"]


# --------------------------------------------------------------------------- #
# The role-change matrix (actor x target's current role x new role). Literal grids.
# Rows: target's current role. Columns: new role, in ALL_ROLES order
# (owner, admin, marketer, reviewer, compliance_reviewer, viewer).
# --------------------------------------------------------------------------- #
OR = "member_owner_required"
OK = 200
CHANGE_GRID = {
    Role.OWNER: {
        Role.OWNER: (OK, OK, OK, OK, OK, OK),
        Role.ADMIN: (OK, OK, OK, OK, OK, OK),
        Role.MARKETER: (OK, OK, OK, OK, OK, OK),
        Role.REVIEWER: (OK, OK, OK, OK, OK, OK),
        Role.COMPLIANCE_REVIEWER: (OK, OK, OK, OK, OK, OK),
        Role.VIEWER: (OK, OK, OK, OK, OK, OK),
    },
    Role.ADMIN: {
        Role.OWNER: (OR, OR, OR, OR, OR, OR),
        Role.ADMIN: (OR, OK, OK, OK, OK, OK),
        Role.MARKETER: (OR, OK, OK, OK, OK, OK),
        Role.REVIEWER: (OR, OK, OK, OK, OK, OK),
        Role.COMPLIANCE_REVIEWER: (OR, OK, OK, OK, OK, OK),
        Role.VIEWER: (OR, OK, OK, OK, OK, OK),
    },
    **{actor: {current: ("ROLE",) * 6 for current in ALL_ROLES} for actor in NON_ADMINS},
}
# Removal (actor x target's current role), same column order for the target's role.
REMOVE_GRID = {
    Role.OWNER: (204, 204, 204, 204, 204, 204),
    Role.ADMIN: (OR, 204, 204, 204, 204, 204),
    **{actor: ("ROLE",) * 6 for actor in NON_ADMINS},
}


class TestRoleChangeMatrix:
    @pytest.mark.parametrize("actor", ALL_ROLES, ids=lambda r: r.value)
    def test_actor_row(self, env: Env, actor: Role):
        w = env.witness
        observed, expected = {}, {}
        for current in ALL_ROLES:
            for new, want in zip(ALL_ROLES, CHANGE_GRID[actor][current], strict=True):
                w.set_role(TARGET, current)
                w.clear_audit()
                r = _put_role(env, _actor(actor), TARGET, new.value)
                got = _outcome(r)
                observed[(current, new)], expected[(current, new)] = got, want
                if got == OK:
                    body = r.json()
                    assert set(body) == MEMBER_FIELDS
                    assert (body["user_id"], body["email"], body["role"]) == (
                        TARGET,
                        _email(TARGET),
                        new.value,
                    )
                    assert w.role(TARGET) == new.value
                    audit = w.audit()
                    if current is new:
                        assert audit == [], "a same-role change is a no-op and is not audited"
                    else:
                        [row] = audit
                        assert (
                            row["action"],
                            row["entity_type"],
                            row["entity_id"],
                            row["actor_user_id"],
                            row["organization_id"],
                            row["workspace_id"],
                            row["previous_state"],
                            row["new_state"],
                        ) == (
                            "organization_member.role_changed",
                            "organization_member",
                            TARGET,
                            _actor(actor),
                            ORG_A,
                            None,
                            {"role": current.value},
                            {"role": new.value},
                        )
                else:
                    if got == "ROLE":
                        assert r.json()["error"]["message"] == _role_denial(actor)
                    assert r.status_code == 403
                    assert w.role(TARGET) == current.value
                    assert w.audit() == []
                assert w.owners() >= 1
                # TARGET's other organization is never touched.
                assert w.role(TARGET, ORG_B) == "viewer"
        assert observed == expected

    @pytest.mark.parametrize("actor", ALL_ROLES, ids=lambda r: r.value)
    def test_nobody_changes_their_own_role(self, env: Env, actor: Role):
        for new in ALL_ROLES:
            r = _put_role(env, _actor(actor), _actor(actor), new.value)
            if actor in (Role.OWNER, Role.ADMIN):
                assert (r.status_code, r.json()["error"]["code"]) == (
                    403,
                    "member_self_change_forbidden",
                )
            else:
                assert r.json()["error"]["message"] == _role_denial(actor)
            assert env.witness.role(_actor(actor)) == actor.value
        assert env.witness.audit() == []

    def test_unknown_target_is_404(self, env: Env):
        r = _put_role(env, _actor(Role.OWNER), "mem-nobody", "viewer")
        assert (r.status_code, r.json()["error"]["code"]) == (404, "member_not_found")

    @pytest.mark.parametrize("role", ["superuser", "OWNER", "", "member"])
    def test_role_outside_the_vocabulary_is_422(self, env: Env, role: str):
        r = _put_role(env, _actor(Role.OWNER), TARGET, role)
        assert (r.status_code, r.json()["error"]["code"]) == (422, "validation_error")
        assert env.witness.role(TARGET) == "marketer"


class TestOwnerLifecycle:
    def test_the_sole_owner_cannot_be_demoted_or_removed_by_anyone(self, env: Env):
        w = env.witness
        assert w.owners() == 1
        owner = _actor(Role.OWNER)
        attempts = [
            _put_role(env, owner, owner, "admin"),
            _remove(env, owner, owner),
            _put_role(env, _actor(Role.ADMIN), owner, "admin"),
            _remove(env, _actor(Role.ADMIN), owner),
        ]
        assert [(r.status_code, r.json()["error"]["code"]) for r in attempts] == [
            (403, "member_self_change_forbidden"),
            (403, "member_self_removal_forbidden"),
            (403, "member_owner_required"),
            (403, "member_owner_required"),
        ]
        assert w.role(owner) == "owner" and w.owners() == 1

    def test_owner_is_granted_only_through_the_role_endpoint_by_an_owner(self, env: Env):
        w = env.witness
        w.set_role(TARGET, Role.ADMIN)
        # ADMIN cannot grant OWNER.
        r = _put_role(env, _actor(Role.ADMIN), TARGET, "owner")
        assert (r.status_code, r.json()["error"]["code"]) == (403, "member_owner_required")
        # OWNER can.
        assert _put_role(env, _actor(Role.OWNER), TARGET, "owner").status_code == 200
        assert w.owners() == 2
        # Now two owners: the new owner may demote the original one ...
        assert _put_role(env, TARGET, _actor(Role.OWNER), "admin").status_code == 200
        assert w.owners() == 1
        # ... who, as an ADMIN, can no longer touch the remaining owner.
        r = _put_role(env, _actor(Role.OWNER), TARGET, "admin")
        assert (r.status_code, r.json()["error"]["code"]) == (403, "member_owner_required")
        assert w.role(TARGET) == "owner"

    def test_an_owner_removes_another_owner_while_two_exist(self, env: Env):
        env.witness.set_role(TARGET, Role.OWNER)
        assert _remove(env, _actor(Role.OWNER), TARGET).status_code == 204
        assert env.witness.owners() == 1


class TestRemovalMatrix:
    @pytest.mark.parametrize("actor", ALL_ROLES, ids=lambda r: r.value)
    def test_actor_row(self, env: Env, actor: Role):
        w = env.witness
        observed, expected = {}, {}
        for current, want in zip(ALL_ROLES, REMOVE_GRID[actor], strict=True):
            w.ensure_member(TARGET, current)
            w.clear_audit()
            r = _remove(env, _actor(actor), TARGET)
            got = _outcome(r)
            observed[current], expected[current] = got, want
            if got == 204:
                assert r.content == b""
                assert w.role(TARGET) is None
                assert w.user_exists(TARGET), "removal must never delete the user"
                [row] = w.audit()
                assert (
                    row["action"],
                    row["entity_type"],
                    row["entity_id"],
                    row["actor_user_id"],
                    row["workspace_id"],
                    row["previous_state"],
                    row["new_state"],
                ) == (
                    "organization_member.removed",
                    "organization_member",
                    TARGET,
                    _actor(actor),
                    None,
                    {"role": current.value},
                    None,
                )
            else:
                assert r.status_code == 403
                assert w.role(TARGET) == current.value and w.audit() == []
            assert w.role(TARGET, ORG_B) == "viewer"
            assert w.owners() >= 1
        assert observed == expected

    def test_removing_a_non_member_is_404(self, env: Env):
        r = _remove(env, _actor(Role.OWNER), B_ONLY)
        assert (r.status_code, r.json()["error"]["code"]) == (404, "member_not_found")
        assert env.witness.role(B_ONLY, ORG_B) == "owner"


# --------------------------------------------------------------------------- #
class TestList:
    @pytest.mark.parametrize("role", ALL_ROLES, ids=lambda r: r.value)
    def test_every_member_lists_every_member(self, env: Env, role: Role):
        r = _send(env, "GET", _members_url(), _bearer(_actor(role)))
        assert r.status_code == 200, r.text
        rows = r.json()
        expected_ids = [_actor(x) for x in ALL_ROLES] + [TARGET]  # seeded in this order
        assert [row["user_id"] for row in rows] == expected_ids
        for row in rows:
            assert set(row) == MEMBER_FIELDS
            assert row["email"] == _email(row["user_id"])
            assert row["full_name"] == row["user_id"].title()
        assert {row["user_id"]: row["role"] for row in rows} == {
            **{_actor(x): x.value for x in ALL_ROLES},
            TARGET: "marketer",
        }
        assert "is_operator" not in r.text and "hashed_password" not in r.text

    @pytest.mark.parametrize(
        ("user_id", "org"),
        [(OUTSIDER, ORG_A), (OPERATOR, ORG_A), (_actor(Role.OWNER), ORG_B), (B_ONLY, ORG_A)],
        ids=["outsider", "operator", "owner-of-a-in-b", "owner-of-b-in-a"],
    )
    def test_non_members_are_403(self, env: Env, user_id: str, org: str):
        r = _send(env, "GET", _members_url(org), _bearer(user_id))
        assert (r.status_code, r.json()["error"]["message"]) == (403, NOT_A_MEMBER)

    def test_anonymous_is_401(self, env: Env):
        assert env.client.get(_members_url()).status_code == 401

    def test_order_is_membership_time_then_user_id(self, env: Env):
        with env.request_factory() as s:
            rows = member_service.list_members(s, organization_id=ORG_A)
        keys = [(row.created_at, row.user_id) for row in rows]
        assert keys == sorted(keys)


# --------------------------------------------------------------------------- #
class TestTakesEffectOnTheNextRequest:
    """One bearer header, minted once; the committed membership decides each request."""

    def _probe(self, env: Env, headers) -> tuple[int, int, int]:
        members = _send(env, "GET", _members_url(), headers)
        listing = _send(env, "GET", f"{API}/workspaces/{WS_A}/locations", headers)
        gated = _send(env, "POST", f"{API}/workspaces/{WS_A}/locations", headers, [])
        return members.status_code, listing.status_code, gated.status_code

    def test_removal_revokes_org_and_workspace_access_with_the_same_token(self, env: Env):
        headers = _bearer(TARGET)
        assert self._probe(env, headers) == (200, 200, 422)  # 422: the role gate passed
        assert _remove(env, _actor(Role.OWNER), TARGET).status_code == 204
        after = [
            _send(env, "GET", _members_url(), headers),
            _send(env, "GET", f"{API}/workspaces/{WS_A}/locations", headers),
            _send(env, "POST", f"{API}/workspaces/{WS_A}/locations", headers, []),
        ]
        assert [(r.status_code, r.json()["error"]["message"]) for r in after] == [
            (403, NOT_A_MEMBER)
        ] * 3
        me = _send(env, "GET", f"{API}/auth/me", headers)
        assert me.status_code == 200  # the account still exists and authenticates
        assert [(m["organization_id"], m["role"]) for m in me.json()["memberships"]] == [
            (ORG_B, "viewer")
        ]

    def test_demotion_takes_effect_with_the_same_token(self, env: Env):
        headers = _bearer(TARGET)
        assert self._probe(env, headers) == (200, 200, 422)
        assert _put_role(env, _actor(Role.OWNER), TARGET, "viewer").status_code == 200
        members, listing, gated = self._probe(env, headers)
        assert (members, listing, gated) == (200, 200, 403)

    def test_a_demoted_admin_loses_member_management_at_once(self, env: Env):
        admin = _bearer(_actor(Role.ADMIN))
        assert (
            _send(
                env, "PUT", f"{_members_url()}/{TARGET}/role", admin, {"role": "viewer"}
            ).status_code
            == 200
        )
        env.witness.set_role(_actor(Role.ADMIN), Role.MARKETER)
        r = _send(env, "PUT", f"{_members_url()}/{TARGET}/role", admin, {"role": "reviewer"})
        assert (r.status_code, r.json()["error"]["message"]) == (403, _role_denial(Role.MARKETER))
        assert env.witness.role(TARGET) == "viewer"

    def test_service_rereads_the_actor_under_the_lock(self, env: Env):
        """A context resolved as ADMIN is not trusted: the service re-reads the membership."""
        env.witness.set_role(_actor(Role.ADMIN), Role.VIEWER)
        with env.request_factory() as s:
            stale = OrganizationContext(
                user=s.get(User, _actor(Role.ADMIN)),
                organization=s.get(Organization, ORG_A),
                role=Role.ADMIN,
            )
            with pytest.raises(PermissionDeniedError) as change:
                member_service.change_member_role(
                    s, ctx=stale, target_user_id=TARGET, new_role=Role.VIEWER
                )
            with pytest.raises(PermissionDeniedError) as remove:
                member_service.remove_member(s, ctx=stale, target_user_id=TARGET)
            s.rollback()
        assert change.value.message == remove.value.message == _role_denial(Role.VIEWER)
        assert env.witness.role(TARGET) == "marketer"


# --------------------------------------------------------------------------- #
class TestLockPrecedesTheAuthorityReread:
    """G2: the organization row is locked BEFORE the actor's authority is re-read.

    SQLite renders no ``FOR UPDATE``, so the SQL text cannot show the lock here. The ORM
    statement can: every statement the request's sessions execute is recorded in order
    (``do_orm_execute``), rendered for PostgreSQL, and classified -- the locking select on
    ``organizations``, and membership reads by whose row they read. The write itself is
    recorded at the cursor. The PostgreSQL race in ``test_organization_invitation_database``
    shows the same lock serializing two real transactions.
    """

    def _trace(self, env: Env, send) -> tuple[object, list]:
        actor = _actor(Role.OWNER)
        events: list = []

        def on_orm(state):
            if not state.is_select:
                return
            mapped = {m.class_ for m in state.all_mappers}
            if Organization in mapped:
                sql = str(state.statement.compile(dialect=postgresql.dialect()))
                if "FOR UPDATE" in sql:
                    events.append("lock")
            elif OrganizationMember in mapped:
                # Membership reads filter on literal ids, so they render with their values.
                sql = str(
                    state.statement.compile(
                        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
                    )
                )
                if "count(" not in sql:
                    who = tuple(u for u in (actor, TARGET) if f"'{u}'" in sql)
                    events.append(("member_read", who))

        def on_cursor(conn, cursor, statement, parameters, context, executemany):
            verb = statement.lstrip().split(None, 1)[0].upper()
            if verb in ("UPDATE", "DELETE") and "organization_members" in statement:
                events.append(verb.lower())

        event.listen(env.request_factory, "do_orm_execute", on_orm)
        event.listen(env.request_engine, "before_cursor_execute", on_cursor)
        try:
            return send(), events
        finally:
            event.remove(env.request_factory, "do_orm_execute", on_orm)
            event.remove(env.request_engine, "before_cursor_execute", on_cursor)

    def _assert_order(self, events: list, write: str) -> None:
        actor = _actor(Role.OWNER)
        assert events.count("lock") == 1, events
        lock = events.index("lock")
        actor_reads = [i for i, e in enumerate(events) if e == ("member_read", (actor,))]
        target_reads = [i for i, e in enumerate(events) if e == ("member_read", (TARGET,))]
        # The request's dependency read the actor before the lock (authorization of the route);
        # the service re-reads it AFTER the lock, and only then reads the target and writes.
        assert actor_reads and actor_reads[0] < lock, events
        assert any(i > lock for i in actor_reads), events
        assert target_reads and min(target_reads) > lock, events
        assert events.index(write) > lock, events

    def test_role_change(self, env: Env):
        r, events = self._trace(env, lambda: _put_role(env, _actor(Role.OWNER), TARGET, "viewer"))
        assert r.status_code == 200, r.text
        self._assert_order(events, "update")

    def test_removal(self, env: Env):
        r, events = self._trace(env, lambda: _remove(env, _actor(Role.OWNER), TARGET))
        assert r.status_code == 204, r.text
        self._assert_order(events, "delete")

    def test_the_spy_sees_a_lock_it_is_shown(self, env: Env):
        """Positive control: an explicit lock through the request session is recorded as one."""
        with env.request_factory() as s:
            events: list = []

            def on_orm(state):
                sql = str(state.statement.compile(dialect=postgresql.dialect()))
                events.append("FOR UPDATE" in sql)

            event.listen(env.request_factory, "do_orm_execute", on_orm)
            try:
                member_service.lock_organization(s, ORG_A)
                s.execute(select(Organization.id).where(Organization.id == ORG_A))
            finally:
                event.remove(env.request_factory, "do_orm_execute", on_orm)
        assert events == [True, False]


# --------------------------------------------------------------------------- #
class TestCrossOrganization:
    def test_owner_of_a_cannot_manage_b(self, env: Env):
        a_owner = _actor(Role.OWNER)
        r1 = _put_role(env, a_owner, B_ONLY, "viewer", org=ORG_B)
        r2 = _remove(env, a_owner, B_ONLY, org=ORG_B)
        assert [(r.status_code, r.json()["error"]["message"]) for r in (r1, r2)] == [
            (403, NOT_A_MEMBER)
        ] * 2
        assert env.witness.role(B_ONLY, ORG_B) == "owner"

    def test_a_member_of_b_is_not_found_through_a(self, env: Env):
        a_owner = _actor(Role.OWNER)
        r1 = _put_role(env, a_owner, B_ONLY, "viewer")
        r2 = _remove(env, a_owner, B_ONLY)
        assert [(r.status_code, r.json()["error"]["code"]) for r in (r1, r2)] == [
            (404, "member_not_found")
        ] * 2
        assert env.witness.memberships() >= {(B_ONLY, ORG_B, "owner"), (TARGET, ORG_B, "viewer")}

    def test_removing_from_a_leaves_b_membership(self, env: Env):
        assert _remove(env, _actor(Role.OWNER), TARGET).status_code == 204
        assert env.witness.role(TARGET, ORG_B) == "viewer"

    @pytest.mark.parametrize("extra", ["organization_id", "user_id", "email"])
    def test_body_identity_fields_are_rejected(self, env: Env, extra: str):
        r = _put_role(env, _actor(Role.OWNER), TARGET, "reviewer", extra={extra: ORG_B})
        assert (r.status_code, r.json()["error"]["code"]) == (422, "validation_error")
        assert env.witness.role(TARGET) == "marketer"
        assert env.witness.role(TARGET, ORG_B) == "viewer"


# --------------------------------------------------------------------------- #
class TestExplicitCommit:
    def test_role_change_is_committed_before_the_response(self, withheld_env: Env):
        assert _put_role(withheld_env, _actor(Role.OWNER), TARGET, "reviewer").status_code == 200
        assert withheld_env.witness.role(TARGET) == "reviewer"

    def test_removal_is_committed_before_the_response(self, withheld_env: Env):
        assert _remove(withheld_env, _actor(Role.ADMIN), TARGET).status_code == 204
        assert withheld_env.witness.role(TARGET) is None
