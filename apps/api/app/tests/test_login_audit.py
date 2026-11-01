"""Phase 5B login auditing (W21 lane P5B): ``auth.login_succeeded`` / ``auth.login_failed``.

    POST /api/v1/auth/login

Exit criterion 6 (``docs/project-phase-5b-approval-audit-spine.md`` §4): login success and
failure are audited. What this module pins:

* a success writes one organization-level row per organization of the account, committed in
  the same transaction as the new session (a failure to open the session leaves neither);
* a refusal answers the unchanged 401 and writes one row per organization of the account the
  address belongs to, committed although the request fails; an address that belongs to no
  account writes nothing (there is no organization to record against), and its answer is
  the same as a wrong password's;
* no audit value carries the address, the password, a token or a session id;
* the rows are readable through the typed audit read API.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.auth import service as auth_service
from app.auth.models import AuthSession
from app.core.config import get_settings
from app.core.enums import Role
from app.core.security import hash_password
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import OrganizationMember, User
from app.tests._approvals_support import add_org
from app.tests._auth2_support import active_rate_limiter

API = get_settings().api_prefix
LOGIN = f"{API}/auth/login"
ORG_A, WS_A = "org-la-a", "ws-la-a"
ORG_B, WS_B = "org-la-b", "ws-la-b"
PASSWORD = "la-correct-horse-42"
WRONG = "la-wrong-battery-17"

MULTI, SOLO, NONE, INACTIVE = "la-multi", "la-solo", "la-none", "la-inactive"
MEMBERSHIPS = {
    MULTI: ((ORG_A, Role.OWNER), (ORG_B, Role.VIEWER)),
    SOLO: ((ORG_A, Role.MARKETER),),
    NONE: (),
    INACTIVE: ((ORG_A, Role.ADMIN),),
}


def email(user_id: str) -> str:
    return f"{user_id}@example.com"


@dataclass
class Env:
    client: TestClient
    witness: Engine

    def login(self, address: str, password: str, client: TestClient | None = None):
        active_rate_limiter()._hits.clear()
        return (client or self.client).post(LOGIN, json={"email": address, "password": password})

    def rows(self, action: str | None = None) -> list[dict]:
        t = AuditLog.__table__
        stmt = select(*t.c).order_by(t.c.organization_id)
        if action is not None:
            stmt = stmt.where(t.c.action == action)
        with self.witness.connect() as c:
            return [dict(r._mapping) for r in c.execute(stmt).all()]

    def sessions(self, user_id: str) -> int:
        t = AuthSession.__table__
        with self.witness.connect() as c:
            return c.execute(
                select(func.count()).select_from(t).where(t.c.user_id == user_id)
            ).scalar_one()


def _engine(db_file) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


@pytest.fixture(scope="module")
def hashed() -> str:
    return hash_password(PASSWORD)


@pytest.fixture
def env(tmp_path, hashed) -> Iterator[Env]:
    engine, witness = _engine(tmp_path / "login.db"), _engine(tmp_path / "login.db")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    with factory() as s:
        add_org(s, ORG_A, WS_A)
        add_org(s, ORG_B, WS_B)
        for user_id, memberships in MEMBERSHIPS.items():
            s.add(
                User(
                    id=user_id,
                    email=email(user_id),
                    full_name=f"Name {user_id}",
                    hashed_password=hashed,
                    is_active=user_id != INACTIVE,
                )
            )
            s.flush()
            for org_id, role in memberships:
                s.add(
                    OrganizationMember(
                        id=f"m-{user_id}-{org_id[-1]}",
                        organization_id=org_id,
                        user_id=user_id,
                        role=role.value,
                    )
                )
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
        yield Env(TestClient(app), witness)
    finally:
        app.dependency_overrides.clear()
        active_rate_limiter()._hits.clear()
        engine.dispose()
        witness.dispose()


def _shape(row: dict) -> tuple:
    return (
        row["organization_id"],
        row["workspace_id"],
        row["actor_user_id"],
        row["entity_type"],
        row["entity_id"],
        row["reason"],
        row["previous_state"],
        row["new_state"],
        row["context"],
    )


class TestSuccess:
    def test_one_row_per_organization_committed_with_the_session(self, env: Env):
        r = env.login(email(MULTI), PASSWORD)
        assert r.status_code == 200, r.text
        rows = env.rows("auth.login_succeeded")
        assert [_shape(row) for row in rows] == [
            (org, None, MULTI, "user", MULTI, None, None, None, {"method": "password"})
            for org in (ORG_A, ORG_B)
        ]
        assert env.sessions(MULTI) == 1
        assert env.rows("auth.login_failed") == []

    def test_an_account_with_no_organization_signs_in_with_no_row(self, env: Env):
        assert env.login(email(NONE), PASSWORD).status_code == 200
        assert env.rows() == [] and env.sessions(NONE) == 1

    def test_no_session_without_its_audit_rows_and_vice_versa(self, env: Env, monkeypatch):
        def refuse(*_a, **_k):
            raise RuntimeError("session store unavailable")

        monkeypatch.setattr(auth_service, "create_session", refuse)
        client = TestClient(app, raise_server_exceptions=False)
        r = env.login(email(SOLO), PASSWORD, client=client)
        assert r.status_code == 500
        assert env.rows() == [] and env.sessions(SOLO) == 0


class TestRefusal:
    def test_a_wrong_password_is_audited_and_the_answer_is_unchanged(self, env: Env):
        r = env.login(email(MULTI), WRONG)
        assert r.status_code == 401
        assert (r.json()["error"]["code"], r.json()["error"]["message"]) == (
            "unauthorized",
            "Invalid email or password.",
        )
        rows = env.rows("auth.login_failed")
        assert [_shape(row) for row in rows] == [
            (org, None, None, "user", MULTI, None, None, None, {"failure": "invalid_credentials"})
            for org in (ORG_A, ORG_B)
        ]
        # Committed although the request failed; no session was opened.
        assert env.sessions(MULTI) == 0
        assert env.rows("auth.login_succeeded") == []

    def test_an_inactive_account_is_audited_with_its_reason(self, env: Env):
        right = env.login(email(INACTIVE), PASSWORD)
        assert right.status_code == 401
        assert right.json()["error"]["message"] == "This account is inactive."
        wrong = env.login(email(INACTIVE), WRONG)
        assert wrong.json()["error"]["message"] == "Invalid email or password."
        assert [row["context"] for row in env.rows("auth.login_failed")] == [
            {"failure": "inactive_account"},
            {"failure": "invalid_credentials"},
        ]
        assert {row["organization_id"] for row in env.rows()} == {ORG_A}

    def test_an_unknown_address_writes_nothing_and_answers_like_a_wrong_password(self, env: Env):
        unknown = env.login("nobody@example.com", WRONG)
        known = env.login(email(SOLO), WRONG)
        assert unknown.status_code == known.status_code == 401
        # The same envelope apart from the per-request id.
        strip = lambda r: {k: v for k, v in r.json()["error"].items() if k != "request_id"}  # noqa: E731
        assert strip(unknown) == strip(known)
        assert [row["entity_id"] for row in env.rows()] == [SOLO]

    def test_repeated_refusals_each_leave_a_row(self, env: Env):
        for _ in range(3):
            assert env.login(email(SOLO), WRONG).status_code == 401
        assert len(env.rows("auth.login_failed")) == 3


class TestNoSecrets:
    def test_no_audit_value_carries_an_address_a_password_a_token_or_a_session(self, env: Env):
        ok = env.login(email(MULTI), PASSWORD)
        token = ok.json()["access_token"]
        env.login(email(MULTI), WRONG)
        env.login(email(INACTIVE), PASSWORD)
        env.login("nobody@example.com", WRONG)
        dump = json.dumps(env.rows(), default=str)
        with env.witness.connect() as c:
            session_ids = [r[0] for r in c.execute(select(AuthSession.__table__.c.id)).all()]
        for secret in (PASSWORD, WRONG, token, "@example.com", *session_ids):
            assert secret not in dump, secret


class TestReadable:
    def test_the_rows_are_organization_level_and_readable_by_the_owner(self, env: Env):
        token = env.login(email(MULTI), PASSWORD).json()["access_token"]
        env.login(email(SOLO), WRONG)
        active_rate_limiter()._hits.clear()
        r = env.client.get(
            f"{API}/workspaces/{WS_A}/audit-logs",
            params={"entity_type": "user"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert r.status_code == 200, r.text
        seen = {(row["action"], row["entity_id"], row["workspace_id"]) for row in r.json()}
        assert seen == {
            ("auth.login_succeeded", MULTI, None),
            ("auth.login_failed", SOLO, None),
        }
