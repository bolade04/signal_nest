"""Shared harness for the 6B-4A (P6-AUTH-2) test modules. Not itself a test module.

Password reset, email verification and the credential epoch share one environment: a SQLite
file (or, with ``db_file=None``, a PostgreSQL database of the test's own, created and dropped
here) holding two organizations, a workspace and a job in each, and accounts of every class
the feature distinguishes -- active, inactive, and two whose stored addresses differ from a
neighbour's only in local-part case. As in ``test_invitation_acceptance_api.py``, only
``get_db`` is overridden, and committed state is read back through a separate ``Engine``.

Two further seams, neither of them a FastAPI dependency override:

* **Mail is selected through settings.** The cached ``Settings`` is switched to the
  ``memory`` backend and the sender cache reset, so the product's own ``get_mail_sender()``
  hands back the process-wide in-memory outbox the tests read. Restored on the way out.
* **The background revoke opens its own session.** ``account_tokens.revoke_*_token`` runs
  after the response on a fresh ``SessionLocal()``, outside the request's session. The
  production factory's ``bind`` is pointed at the test database for the environment's
  lifetime and restored afterwards, so a revoke lands where the test can see it -- and never
  on the developer's ``./signalnest.db``. (Rebinding ``SessionLocal`` is the suite's
  existing seam; see ``test_seed_target_guard.py``.)

The digests are computed here from the contract's definition, not by calling the code under
test: ``sha256("password-reset:" + raw)`` and ``sha256("email-verification:" + raw)``.

P6-AUTH-4 (session lifecycle): every access token is bound to a live ``auth_sessions`` row, so
the token helpers here open a real session through the product's own ``create_session`` /
``issue_token`` -- :func:`bearer` on an :class:`Env`, :func:`live_bearer` through whatever
``get_db`` override a test module installs. :func:`raw_token` signs arbitrary claims for the
negative tests only; :func:`app_clock_skewed` and :func:`require_postgres` are the clock-skew
and fail-closed PostgreSQL harness of the AUTH4 test plan.
"""

from __future__ import annotations

import hashlib
import os
import re
import sys
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import jwt
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, insert, select, text, update
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session, sessionmaker

from app.audit.models import AuditLog
from app.auth import service as auth_service
from app.auth.models import AuthSession, EmailVerificationToken, PasswordResetToken
from app.core.config import get_settings
from app.core.middleware import RateLimitMiddleware
from app.core.security import ALGORITHM, decode_access_token, hash_password
from app.db.models import Base
from app.db.session import SessionLocal, get_db
from app.infra import mail
from app.jobs.models import Job
from app.main import app
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
    Workspace,
)

API = get_settings().api_prefix
RESET_REQUEST = f"{API}/auth/password-reset/request"
RESET_CONFIRM = f"{API}/auth/password-reset/confirm"
VERIFY_REQUEST = f"{API}/auth/email-verification/request"
VERIFY_CONFIRM = f"{API}/auth/email-verification/confirm"
LOGIN = f"{API}/auth/login"
ME = f"{API}/auth/me"
REGISTER = f"{API}/auth/register"
INVITATION_PREVIEW = f"{API}/auth/invitations/preview"
INVITATION_REGISTER = f"{API}/auth/invitations/register"
INVITATION_ACCEPT = f"{API}/auth/invitations/accept"
LOGOUT = f"{API}/auth/logout"
LOGOUT_ALL = f"{API}/auth/logout-all"

RESET_PATH = "/reset-password"
VERIFY_PATH = "/verify-email"

OLD_PASSWORD = "correct-horse-9"
NEW_PASSWORD = "battery-staple-42"
#: One bcrypt hash shared by every seeded account: hashing is deliberately slow.
OLD_HASH = hash_password(OLD_PASSWORD)

ORG_A, ORG_B = "org-a2-a", "org-a2-b"
WS_A, WS_B = "ws-a2-a", "ws-a2-b"
JOB_A, JOB_B = "job-a2-a", "job-a2-b"

ALICE, BOB, CAROL, IVAN = "a2-alice", "a2-bob", "a2-carol", "a2-ivan"
# Local-part case variants (EmailStr lowercases only the domain): stored exactly as written.
CASEY, DANA = "a2-casey", "a2-dana"
EMAIL = {
    ALICE: "alice@example.com",  # OWNER of A
    BOB: "bob@example.com",  # OWNER of B
    CAROL: "carol@example.com",  # VIEWER of A; the usual bystander
    IVAN: "ivan@example.com",  # inactive; MARKETER of A
    CASEY: "casey@example.com",
    DANA: "Dana@example.com",
}
INACTIVE = IVAN
UNKNOWN_EMAIL = "nobody@example.com"

#: The contract's closed column sets; anything more (raw token, IP, user agent, tenant ids)
#: is a defect.
RESET_COLUMNS = {
    "id",
    "user_id",
    "token_hash",
    "expires_at",
    "used_at",
    "revoked_at",
    "created_at",
    "updated_at",
}
VERIFICATION_COLUMNS = RESET_COLUMNS | {"email_snapshot"}
RESET_OPEN_INDEX = "uq_password_reset_tokens_open"
VERIFICATION_OPEN_INDEX = "uq_email_verification_tokens_open"

#: Everything an account-token operation must never touch.
TENANT_MODELS = (Organization, OrganizationMember, OrganizationInvitation, Workspace, Job, AuditLog)
TOKEN_MODELS = (PasswordResetToken, EmailVerificationToken)

_RAW = r"([A-Za-z0-9_-]+)"


def reset_digest(raw: str) -> str:
    return hashlib.sha256(("password-reset:" + raw).encode("utf-8")).hexdigest()


def verification_digest(raw: str) -> str:
    return hashlib.sha256(("email-verification:" + raw).encode("utf-8")).hexdigest()


def plain_digest(raw: str) -> str:
    """The invitation-style digest: no purpose prefix."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def as_utc(value) -> datetime:
    if isinstance(value, str):
        value = datetime.fromisoformat(value)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def claims(access_token: str) -> dict:
    payload = decode_access_token(access_token)
    assert payload is not None, "the access token did not decode"
    return payload


def active_rate_limiter() -> RateLimitMiddleware:
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None and not isinstance(node, RateLimitMiddleware):
        node = getattr(node, "app", None)
    assert node is not None, "RateLimitMiddleware not found in app.middleware_stack"
    return node


def sqlite_file_engine(db_file: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


@contextmanager
def pg_database(prefix: str = "sn_a2") -> Iterator[str]:  # pragma: no cover - gated on PG
    """A PostgreSQL database of this test's own: created here, dropped on the way out."""
    base = make_url(os.environ["TEST_POSTGRES_URL"])
    name = f"{prefix}_{uuid.uuid4().hex[:12]}"
    admin = create_engine(base, isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            conn.execute(text(f'CREATE DATABASE "{name}"'))
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def seed(s) -> None:
    s.add(Organization(id=ORG_A, name="Acme A2", slug="acme-a2"))
    s.add(Organization(id=ORG_B, name="Bravo B2", slug="bravo-b2"))
    s.flush()
    for user_id, email in EMAIL.items():
        s.add(
            User(
                id=user_id,
                email=email,
                full_name=user_id,
                hashed_password=OLD_HASH,
                is_active=user_id != INACTIVE,
            )
        )
    s.flush()
    for org, user_id, role in (
        (ORG_A, ALICE, "owner"),
        (ORG_A, CAROL, "viewer"),
        (ORG_A, IVAN, "marketer"),
        (ORG_B, BOB, "owner"),
    ):
        s.add(OrganizationMember(organization_id=org, user_id=user_id, role=role))
    s.add(Workspace(id=WS_A, organization_id=ORG_A, name="Workspace A2", slug="ws-a2"))
    s.add(Workspace(id=WS_B, organization_id=ORG_B, name="Workspace B2", slug="ws-b2"))
    s.flush()
    for job_id, org, ws in ((JOB_A, ORG_A, WS_A), (JOB_B, ORG_B, WS_B)):
        s.add(
            Job(
                id=job_id,
                organization_id=org,
                workspace_id=ws,
                job_type="a2.noop",
                payload={},
                payload_hash="0" * 64,
            )
        )
    s.commit()


Snapshot = dict[str, tuple]


@dataclass(frozen=True)
class Witness:
    """Reads and writes committed state on a connection of its own."""

    engine: Engine

    def session(self):
        return sessionmaker(bind=self.engine, autoflush=False, future=True)()

    def db_now(self) -> datetime:
        """The database clock (SQLite: millisecond ``'now'``, as the service reads it)."""
        with self.engine.connect() as conn:
            if self.engine.dialect.name == "sqlite":
                clock = select(func.strftime("%Y-%m-%d %H:%M:%f", "now"))
            else:
                clock = select(func.clock_timestamp())
            return as_utc(conn.execute(clock).scalar())

    def rows(self, model, *where) -> list[dict]:
        table = model.__table__
        order = [table.c.created_at, table.c.id] if model in TOKEN_MODELS else [table.c.id]
        with self.engine.connect() as conn:
            result = conn.execute(select(*table.c).where(*where).order_by(*order))
            return [dict(r._mapping) for r in result]

    def snapshot(self, *models) -> Snapshot:
        """Every row of every table named, in a stable order."""
        out = {}
        for model in models or (*TENANT_MODELS, User, *TOKEN_MODELS):
            out[model.__tablename__] = tuple(tuple(r.values()) for r in self.rows(model))
        return out

    def bystanders(self, *excluded: str) -> tuple:
        """Every user row except ``excluded``: must be byte-identical across an operation."""
        return tuple(tuple(r.values()) for r in self.rows(User) if r["id"] not in excluded)

    def user(self, user_id: str) -> dict:
        [row] = self.rows(User, User.id == user_id)
        return row

    def reset_tokens(self, user_id: str | None = None) -> list[dict]:
        where = () if user_id is None else (PasswordResetToken.user_id == user_id,)
        return self.rows(PasswordResetToken, *where)

    def verification_tokens(self, user_id: str | None = None) -> list[dict]:
        where = () if user_id is None else (EmailVerificationToken.user_id == user_id,)
        return self.rows(EmailVerificationToken, *where)

    def sessions(self, user_id: str | None = None) -> list[dict]:
        """Every ``auth_sessions`` row (of ``user_id``), in id order."""
        where = () if user_id is None else (AuthSession.user_id == user_id,)
        return self.rows(AuthSession, *where)

    def session_row(self, sid: str) -> dict:
        [row] = self.rows(AuthSession, AuthSession.id == sid)
        return row

    def token(self, model, token_id: str) -> dict:
        [row] = self.rows(model, model.id == token_id)
        return row

    def open_tokens(self, model, user_id: str) -> list[dict]:
        return [
            r
            for r in self.rows(model, model.user_id == user_id)
            if r["used_at"] is None and r["revoked_at"] is None
        ]

    def count(self, model) -> int:
        with self.engine.connect() as conn:
            return conn.execute(select(func.count()).select_from(model.__table__)).scalar_one()

    def write(self, stmt) -> int:
        with self.engine.begin() as conn:
            return conn.execute(stmt).rowcount

    def backdate(self, model, user_id: str, by: timedelta) -> None:
        """Move every token of ``user_id`` back in time by ``by`` (created_at only)."""
        table = model.__table__
        with self.engine.begin() as conn:
            rows = conn.execute(
                select(table.c.id, table.c.created_at).where(table.c.user_id == user_id)
            ).all()
            for token_id, created in rows:
                conn.execute(
                    update(table)
                    .where(table.c.id == token_id)
                    .values(created_at=as_utc(created) - by)
                )

    def plant(
        self,
        model,
        *,
        user_id: str,
        digest: str,
        age: timedelta = timedelta(0),
        lifetime: timedelta = timedelta(hours=1),
        used: bool = False,
        revoked: bool = False,
        email_snapshot: str | None = None,
    ) -> str:
        """Insert a token row directly, bypassing the service; returns its id."""
        now = self.db_now()
        created = now - age
        values = {
            "id": uuid.uuid4().hex,
            "user_id": user_id,
            "token_hash": digest,
            "expires_at": created + lifetime,
            "used_at": created if used else None,
            "revoked_at": created if revoked else None,
            "created_at": created,
            "updated_at": created,
        }
        if model is EmailVerificationToken:
            values["email_snapshot"] = email_snapshot or self.user(user_id)["email"]
        with self.engine.begin() as conn:
            conn.execute(insert(model.__table__).values(**values))
        return values["id"]

    def drop_index(self, name: str) -> None:
        """Remove a partial open-token index, to reach the service's defence in depth."""
        with self.engine.begin() as conn:
            conn.execute(text(f"DROP INDEX {name}"))


@dataclass(frozen=True)
class Env:
    client: TestClient
    witness: Witness
    request_engine: Engine
    sender: object  # the process-wide MemoryMailSender

    def messages(self) -> list:
        return list(self.sender.outbox)


@contextmanager
def environment(db_file: Path | None, *, teardown_commit: bool = True) -> Iterator[Env]:
    """SQLite file at ``db_file``; or, with ``db_file=None``, a PostgreSQL database of its own."""
    if db_file is None:  # pragma: no cover - gated on live PG
        with pg_database() as url:
            # The witness bounds its lock waits so a competitor that would deadlock against
            # the request's own locks fails the test instead of hanging it.
            witness = create_engine(
                url, future=True, connect_args={"options": "-c lock_timeout=5000"}
            )
            with _built(
                create_engine(url, future=True), witness, teardown_commit=teardown_commit
            ) as env:
                yield env
        return
    with _built(
        sqlite_file_engine(db_file), sqlite_file_engine(db_file), teardown_commit=teardown_commit
    ) as env:
        yield env


@contextmanager
def _built(request_engine: Engine, witness_engine: Engine, *, teardown_commit: bool):
    Base.metadata.create_all(request_engine)
    factory = sessionmaker(
        bind=request_engine, autoflush=False, expire_on_commit=False, future=True
    )
    with factory() as s:
        seed(s)

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

    settings = get_settings()
    saved_backend = settings.mail_backend
    saved_bind = SessionLocal.kw.get("bind")
    settings.mail_backend = "memory"
    mail.reset_mail_sender()
    sender = mail.get_mail_sender()
    assert sender.backend == "memory", sender
    sender.clear()
    SessionLocal.kw["bind"] = request_engine
    app.dependency_overrides[get_db] = _override_get_db
    active_rate_limiter()._hits.clear()
    try:
        yield Env(TestClient(app), Witness(witness_engine), request_engine, sender)
    finally:
        app.dependency_overrides.clear()
        active_rate_limiter()._hits.clear()
        SessionLocal.kw["bind"] = saved_bind
        sender.clear()
        settings.mail_backend = saved_backend
        mail.reset_mail_sender()
        request_engine.dispose()
        witness_engine.dispose()


# --------------------------------------------------------------------------- #
# Requests
# --------------------------------------------------------------------------- #
def open_session(engine: Engine, user_id: str) -> tuple[str, str]:
    """Open a live session for ``user_id`` on ``engine``'s database, as a fresh sign-in would.

    The product's own ``create_session`` (database clock, fixed absolute expiry) and
    ``issue_token`` (the account's CURRENT epoch, the session's ``sid``) do the work; the row
    is committed before the token exists. Returns ``(access_token, sid)``.
    """
    with sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)() as s:
        user = s.get(User, user_id)
        assert user is not None, f"open_session: unknown user {user_id}"
        session = auth_service.create_session(s, user)
        s.commit()
        return auth_service.issue_token(user, session), session.id


def bearer(env: Env, user_id: str) -> str:
    """A token bound to a new LIVE session of ``user_id``, carrying the account's CURRENT epoch."""
    return open_session(env.request_engine, user_id)[0]


def live_bearer(user_id: str, db: Session | None = None) -> str:
    """A token bound to a new LIVE session of ``user_id``, opened through the app's ``get_db``.

    For test modules with their own database: the session row is written through the
    ``get_db`` override the module installed, so it lands in that module's database. A
    ``user_id`` with no user row gets a well-formed token with an unused ``sid`` instead --
    refused either way (the user is looked up before the session). ``db`` opens the session
    inside a caller's own (seeding) transaction instead: flushed, and committed by the caller.
    """
    if db is not None:
        user = db.get(User, user_id)
        assert user is not None, f"live_bearer: unknown user {user_id}"
        session = auth_service.create_session(db, user)
        return auth_service.issue_token(user, session)
    provider = app.dependency_overrides.get(get_db)
    assert provider is not None, "live_bearer needs the test's get_db override installed"
    generator = provider()
    db = next(generator)
    try:
        user = db.get(User, user_id)
        if user is None:
            return raw_token(sub=user_id, sid=uuid.uuid4().hex, auth_epoch=0)
        session = auth_service.create_session(db, user)
        db.commit()
        return auth_service.issue_token(user, session)
    finally:
        generator.close()


def live_auth(user_id: str) -> dict:
    """``{"Authorization": "Bearer <live_bearer(user_id)>"}``."""
    return {"Authorization": f"Bearer {live_bearer(user_id)}"}


#: Omit a claim from :func:`raw_token` (``None`` is a real JSON value there).
OMIT = object()


def raw_token(
    *, lifetime: timedelta = timedelta(minutes=30), key: str | None = None, **claims
) -> str:
    """Sign ARBITRARY claims with the app's key (TEST ONLY: the product builder has no claims
    dict). ``iat``/``exp`` default to now and now + ``lifetime``; a claim passed as
    :data:`OMIT` is left out."""
    now = datetime.now(UTC)
    payload = {"iat": now, "exp": now + lifetime, **claims}
    payload = {k: v for k, v in payload.items() if v is not OMIT}
    return jwt.encode(payload, key or get_settings().secret_key, algorithm=ALGORITHM)


# --------------------------------------------------------------------------- #
# Clock skew and PostgreSQL (the AUTH4 clock-test harness)
# --------------------------------------------------------------------------- #
SKEW_60 = timedelta(minutes=60)  # > 0 and < the 720-minute TTL


@contextmanager
def app_clock_skewed(offset: timedelta) -> Iterator[type[datetime]]:
    """Shift the APPLICATION clock by ``offset`` in every ``app.*`` module bound to the real
    ``datetime`` class (never the tests', never the JWT library's). The database clock is
    untouched."""
    real = datetime

    class Shifted(real):
        @classmethod
        def now(cls, tz=None):
            return real.now(tz) + offset

    with pytest.MonkeyPatch.context() as mp:
        for name, module in list(sys.modules.items()):
            if (
                name.startswith("app.")
                and not name.startswith("app.tests.")
                and getattr(module, "datetime", None) is real
            ):
                mp.setattr(module, "datetime", Shifted)
        yield Shifted


def require_postgres() -> str:
    """``TEST_POSTGRES_URL``; unset FAILS in CI (``CI=true``) and skips only elsewhere."""
    url = os.getenv("TEST_POSTGRES_URL")
    if not url:
        if os.getenv("CI") == "true":
            pytest.fail("AUTH4 PostgreSQL tests must run in CI (TEST_POSTGRES_URL unset)")
        pytest.skip("TEST_POSTGRES_URL not set")
    return url


def post(
    env: Env,
    url: str,
    body: object,
    *,
    token: str | None = None,
    user_id: str | None = None,
    headers: dict | None = None,
):
    assert set(app.dependency_overrides) == {get_db}, "only get_db may be overridden"
    active_rate_limiter()._hits.clear()
    sent = dict(headers or {})
    if user_id is not None:
        token = bearer(env, user_id)
    if token is not None:
        sent["Authorization"] = f"Bearer {token}"
    return env.client.post(url, json=body, headers=sent)


def get_me(env: Env, access_token: str):
    active_rate_limiter()._hits.clear()
    return env.client.get(ME, headers={"Authorization": f"Bearer {access_token}"})


def login(env: Env, email: str, password: str):
    return post(env, LOGIN, {"email": email, "password": password})


def code(r) -> tuple[int, str]:
    return r.status_code, r.json()["error"]["code"]


def error(r) -> tuple[int, str, str]:
    body = r.json()["error"]
    return r.status_code, body["code"], body["message"]


def link_token(message, path: str) -> str:
    """The raw token from the link in ``message``'s plain-text body."""
    origin = get_settings().public_web_origin
    found = re.findall(re.escape(f"{origin}{path}#token=") + _RAW, message.text_body)
    assert found, f"no {path} link in the message"
    assert len(set(found)) == 1, found
    return found[0]


def request_reset(env: Env, email: str, **kw):
    return post(env, RESET_REQUEST, {"email": email}, **kw)


def confirm_reset(env: Env, token: str, password: str = NEW_PASSWORD, **kw):
    return post(env, RESET_CONFIRM, {"token": token, "new_password": password}, **kw)


def request_verification(env: Env, user_id: str, **kw):
    return post(env, VERIFY_REQUEST, {}, user_id=user_id, **kw)


def confirm_verification(env: Env, token: str, user_id: str, **kw):
    return post(env, VERIFY_CONFIRM, {"token": token}, user_id=user_id, **kw)


def _one_new_message(env: Env, before: int):
    messages = env.messages()
    assert len(messages) == before + 1, [m.template for m in messages]
    return messages[-1]


def mint_reset(env: Env, user_id: str) -> str:
    """Request a reset for ``user_id``'s stored address; return the mailed raw token."""
    before = len(env.messages())
    r = request_reset(env, EMAIL[user_id])
    assert (r.status_code, r.content) == (204, b""), r.text
    return link_token(_one_new_message(env, before), RESET_PATH)


def mint_verification(env: Env, user_id: str) -> str:
    before = len(env.messages())
    r = request_verification(env, user_id)
    assert (r.status_code, r.content) == (204, b""), r.text
    return link_token(_one_new_message(env, before), VERIFY_PATH)


def past_cooldown(env: Env, model, user_id: str) -> None:
    """Age every token of ``user_id`` just past the per-account cooldown."""
    seconds = get_settings().auth_mail_cooldown_seconds + 1
    env.witness.backdate(model, user_id, timedelta(seconds=seconds))


def invite(env: Env, email: str, role: str = "viewer", *, inviter: str = ALICE) -> dict:
    r = post(
        env,
        f"{API}/organizations/{ORG_A}/invitations",
        {"email": email, "role": role},
        user_id=inviter,
    )
    assert r.status_code == 201, r.text
    return r.json()


@contextmanager
def failing_mail(monkeypatch, env: Env, exc: BaseException | None = None) -> Iterator[list]:
    """Make the memory sender's ``send`` raise; yields the messages it was handed."""
    handed: list = []

    def fail(self, message):
        handed.append(message)
        raise exc if exc is not None else mail.MailSendError("provider_error")

    with monkeypatch.context() as m:
        m.setattr(type(env.sender), "send", fail)
        yield handed
