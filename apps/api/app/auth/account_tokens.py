"""Account tokens: password reset and email verification.

Both flows mint a one-time token for one account, mail it, and later consume it. The
raw token is ``secrets.token_urlsafe(32)`` (256 bits). It is handed back once, in an
:class:`IssuedToken` for the mailer, and never stored: each table keeps only the
SHA-256 hex digest of the token prefixed by its purpose (``"password-reset:"`` or
``"email-verification:"``), so a token minted for one purpose can never be presented
for the other. The token, its digest, the link and the address are never logged or
placed in an error, and every error message here is static.

Issuing. The account's user row is locked (``SELECT … FOR UPDATE``, a no-op on SQLite,
whose single writer serializes instead) and every check runs under the lock. A request
is silently suppressed — ``None``, nothing minted, nothing revoked — for an unknown or
inactive account, inside the per-user cooldown (any token of that purpose created
within ``auth_mail_cooldown_seconds``) or at the per-user daily cap
(``auth_mail_daily_cap`` tokens of that purpose created in the trailing 24 hours); the
cooldown and cap count each purpose separately. Otherwise the account's open tokens of
that purpose are revoked and one new token is inserted, so at most one token per
purpose is ever open, which a partial unique index backs on both dialects. Should a
concurrent request still win that index, the insert is rolled back to its savepoint
and the request is suppressed too; the winner's token stands.

Consuming. A token is claimed with a conditional ``UPDATE`` whose ``WHERE`` re-states
the open predicate (unused, unrevoked, unexpired), and the claim only counts when
exactly one row changed, so a replayed, superseded or expired token can never be
spent twice. A completed reset, in the same transaction, sets the new password,
increments ``users.auth_epoch`` as a SQL expression (every access token issued before
the reset stops authenticating) and marks the stored address verified if it was not
already; a completed verification marks the address verified. Both revoke whatever
tokens of their purpose remain open. Every token-state failure of a reset is one 404
(``password_reset_invalid``); a verification token belonging to another account is a
403 (``email_verification_wrong_account``) that spends and changes nothing.

Time is the database's clock, never the application host's: each operation reads
:func:`~app.organizations.invitations.database_now` once and uses that one reading for
every predicate and every timestamp it writes (``created_at``, ``expires_at``,
``used_at``, ``revoked_at``, ``updated_at``), so the decision and the write agree.

Conventions mirror the house service style: explicit ``db: Session`` first positional,
keyword-only domain args, caller-owns-transaction (the service flushes but never
commits; routes commit before answering and before scheduling mail) and
``SignalNestError`` subclasses. The exceptions are :func:`revoke_password_reset_token`
and :func:`revoke_email_verification_token`, which run after the request is over and
so open and commit a session of their own. A ``begin_nested`` savepoint is only ever
opened after the transaction's first write: under pysqlite an outermost savepoint
commits on release, which would break caller-owns-transaction.

Residuals. Delivery, and revoking a token whose send failed, belong to
``app.auth.mail_dispatch``; the AUTH2-C3 availability residual (a process that dies
between commit and send leaves an unsent token valid until it expires or is
superseded) is recorded there and does not arise in this module. The reset request
looks the address up by exact match on the stored ``users.email`` — no case folding or
normalization (F2, unchanged) — so an address typed with different case than at
registration finds no account and silently sends nothing. The request's response is
identical for every account class, but its timing is not equalized: an active account
runs a few more statements than an unknown one.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.models import EmailVerificationToken, PasswordResetToken
from app.core.config import get_settings
from app.core.errors import (
    ConflictError,
    NotFoundError,
    PermissionDeniedError,
    ValidationDomainError,
)
from app.core.logging import get_logger, log_event
from app.core.security import hash_password
from app.db.session import SessionLocal
from app.organizations.invitations import MAX_TOKEN_LENGTH, database_now
from app.organizations.members import as_utc
from app.organizations.models import User

logger = get_logger("signalnest.auth.account_tokens")

#: New-password bounds; the same as registration's.
MIN_PASSWORD_LENGTH = 8
MAX_PASSWORD_LENGTH = 128

_PASSWORD_RESET_PURPOSE = "password-reset"
_EMAIL_VERIFICATION_PURPOSE = "email-verification"

#: The trailing window the daily cap counts over.
_DAILY_CAP_WINDOW = timedelta(hours=24)

_AccountToken = type[PasswordResetToken] | type[EmailVerificationToken]


@dataclass(frozen=True, slots=True)
class IssuedToken:
    """A freshly minted token, for the mailer only.

    ``raw_token`` is transient: it must never be logged or stored, and it (like the
    address) is kept out of ``repr``. ``recipient`` is the account's stored address.
    """

    token_id: str
    raw_token: str = field(repr=False)
    recipient: str = field(repr=False)
    expires_at: datetime
    user_id: str


# --------------------------------------------------------------------------- #
# Digests and errors
# --------------------------------------------------------------------------- #
def _digest(purpose: str, raw: str) -> str:
    return hashlib.sha256(f"{purpose}:{raw}".encode()).hexdigest()


def hash_password_reset_token(raw: str) -> str:
    """SHA-256 hex digest of ``"password-reset:" + raw``."""
    return _digest(_PASSWORD_RESET_PURPOSE, raw)


def hash_email_verification_token(raw: str) -> str:
    """SHA-256 hex digest of ``"email-verification:" + raw``."""
    return _digest(_EMAIL_VERIFICATION_PURPOSE, raw)


def _reset_invalid() -> NotFoundError:
    return NotFoundError(
        "This password reset link is invalid or has expired.", code="password_reset_invalid"
    )


def _verification_invalid() -> NotFoundError:
    return NotFoundError(
        "This verification link is invalid or has expired.", code="email_verification_invalid"
    )


def _already_verified() -> ConflictError:
    return ConflictError("This email address is already verified.", code="email_already_verified")


# --------------------------------------------------------------------------- #
# Internal helpers
# --------------------------------------------------------------------------- #
def _is_well_formed(token: object) -> bool:
    """Whether ``token`` could be an issued token at all; checked before hashing.

    Issued tokens are 43 ASCII characters. Anything empty, longer than
    ``MAX_TOKEN_LENGTH`` or non-ASCII cannot be one.
    """
    return isinstance(token, str) and 0 < len(token) <= MAX_TOKEN_LENGTH and token.isascii()


def _require_valid_password(new_password: object) -> None:
    if not (
        isinstance(new_password, str)
        and MIN_PASSWORD_LENGTH <= len(new_password) <= MAX_PASSWORD_LENGTH
    ):
        raise ValidationDomainError("The new password must be 8 to 128 characters long.")


def _user_lock_select(*criteria):
    """The ``SELECT … FOR UPDATE`` that serializes token mutations for one account.

    Factored out so a test can compile it against the PostgreSQL dialect and prove it
    emits ``FOR UPDATE`` without a live database.
    """
    return select(User).where(*criteria).with_for_update()


def _lock_user(db: Session, *criteria) -> User | None:
    """Lock and read fresh the user row matching ``criteria``, if any.

    ``populate_existing`` overwrites an instance already in the session (for example
    the request's authenticated user), so every decision is made on the row as it is
    under the lock.
    """
    return db.execute(
        _user_lock_select(*criteria).execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _open_predicate(model: _AccountToken, now: datetime) -> tuple:
    return (model.used_at.is_(None), model.revoked_at.is_(None), model.expires_at > now)


def _is_open(row: PasswordResetToken | EmailVerificationToken, *, now: datetime) -> bool:
    return row.used_at is None and row.revoked_at is None and as_utc(row.expires_at) > now


def _find(
    db: Session, model: _AccountToken, token_hash: str
) -> PasswordResetToken | EmailVerificationToken | None:
    return db.execute(
        select(model)
        .where(model.token_hash == token_hash)
        .execution_options(populate_existing=True)
    ).scalar_one_or_none()


def _throttled(db: Session, model: _AccountToken, *, user_id: str, now: datetime) -> bool:
    """Whether minting now would break the account's cooldown or daily cap."""
    settings = get_settings()
    latest = db.scalar(select(func.max(model.created_at)).where(model.user_id == user_id))
    cooldown = timedelta(seconds=settings.auth_mail_cooldown_seconds)
    if latest is not None and as_utc(latest) > now - cooldown:
        return True
    issued_in_window = db.scalar(
        select(func.count())
        .select_from(model)
        .where(model.user_id == user_id, model.created_at > now - _DAILY_CAP_WINDOW)
    )
    return issued_in_window >= settings.auth_mail_daily_cap


def _revoke_open(db: Session, model: _AccountToken, *, user_id: str, now: datetime) -> None:
    """Revoke every unused, unrevoked token of ``model`` held by ``user_id``."""
    db.execute(
        update(model)
        .where(model.user_id == user_id, model.used_at.is_(None), model.revoked_at.is_(None))
        .values(revoked_at=now, updated_at=now),
        execution_options={"synchronize_session": False},
    )


def _claim(db: Session, model: _AccountToken, token_id: str, *criteria, now: datetime) -> bool:
    """Spend one token iff it is still open at ``now``; ``True`` when this call spent it.

    The open predicate lives inside the ``UPDATE``'s ``WHERE`` — never a read-then-write
    in Python — so the check and the mutation are one atomic compare-and-set.
    """
    result = db.execute(
        update(model)
        .where(model.id == token_id, *_open_predicate(model, now), *criteria)
        .values(used_at=now, updated_at=now),
        execution_options={"synchronize_session": False},
    )
    return result.rowcount == 1


def _issue(
    db: Session,
    model: _AccountToken,
    *,
    user: User,
    purpose: str,
    ttl: timedelta,
    now: datetime,
    **columns: str,
) -> IssuedToken | None:
    """Mint one token of ``model`` for the locked ``user``, or ``None`` when suppressed."""
    if _throttled(db, model, user_id=user.id, now=now):
        return None
    # Always executed, even when nothing is open: on pysqlite this UPDATE is the
    # transaction's first write, so the savepoint below is genuinely nested.
    _revoke_open(db, model, user_id=user.id, now=now)

    raw_token = secrets.token_urlsafe(32)
    expires_at = now + ttl
    row = model(
        user_id=user.id,
        token_hash=_digest(purpose, raw_token),
        expires_at=expires_at,
        created_at=now,
        updated_at=now,
        **columns,
    )
    try:
        with db.begin_nested():
            db.add(row)
            db.flush()
    except IntegrityError:
        # A concurrent request won the open-token index: its token stands and this one
        # is abandoned with the savepoint. Not re-raised, since the driver error carries
        # the digest; and suppressed like every other refusal, so a request for an
        # existing account can never answer differently from one for an unknown address.
        return None
    return IssuedToken(
        token_id=row.id,
        raw_token=raw_token,
        recipient=user.email,
        expires_at=expires_at,
        user_id=user.id,
    )


def _revoke_in_own_session(model: _AccountToken, token_id: str, *, failure_event: str) -> None:
    try:
        with SessionLocal() as db:
            now = database_now(db)
            db.execute(
                update(model)
                .where(model.id == token_id, model.used_at.is_(None), model.revoked_at.is_(None))
                .values(revoked_at=now, updated_at=now),
                execution_options={"synchronize_session": False},
            )
            db.commit()
    except Exception as exc:
        # Static event, class name only: a driver error can carry the DSN or the SQL.
        log_event(
            logger,
            failure_event,
            level=logging.ERROR,
            outcome="failure",
            error_class=type(exc).__name__,
        )


# --------------------------------------------------------------------------- #
# Password reset
# --------------------------------------------------------------------------- #
def request_password_reset(db: Session, *, email: str) -> IssuedToken | None:
    """Mint a password-reset token for the active account at ``email``, if allowed.

    Never raises for any account class: an unknown or inactive address, the cooldown
    and the daily cap return ``None`` with nothing minted or revoked, and a lost race
    returns ``None`` with nothing minted. Otherwise the account's open reset tokens are
    revoked and the new one is returned. Flushes but never commits.
    """
    now = database_now(db)
    user = _lock_user(db, User.email == email)
    if user is None or not user.is_active:
        return None
    return _issue(
        db,
        PasswordResetToken,
        user=user,
        purpose=_PASSWORD_RESET_PURPOSE,
        ttl=timedelta(minutes=get_settings().password_reset_token_ttl_minutes),
        now=now,
    )


def confirm_password_reset(db: Session, *, token: str, new_password: str) -> None:
    """Spend a reset token and set ``new_password`` on its account.

    The password bounds are checked first, before any lookup, so a rejected password
    never spends a token. In one transaction the token is claimed, the password is
    replaced, ``auth_epoch`` is incremented and the stored address is marked verified
    (if it was not already), and the account's remaining open reset tokens are revoked.
    No session is created. Every token-state failure — unknown, expired, used, revoked,
    superseded, inactive account, lost race — is 404 ``password_reset_invalid``.
    Flushes but never commits.
    """
    _require_valid_password(new_password)
    if not _is_well_formed(token):
        raise _reset_invalid()
    now = database_now(db)
    row = _find(db, PasswordResetToken, hash_password_reset_token(token))
    if row is None or not _is_open(row, now=now):
        raise _reset_invalid()
    # Hashed before the lock, so the lock is never held across bcrypt; only a token that
    # exists and is still open gets this far, so an unknown token costs no hashing.
    hashed_password = hash_password(new_password)
    user = _lock_user(db, User.id == row.user_id)
    if user is None or not user.is_active:
        raise _reset_invalid()
    if not _claim(db, PasswordResetToken, row.id, now=now):
        raise _reset_invalid()

    db.execute(
        update(User)
        .where(User.id == user.id)
        .values(
            hashed_password=hashed_password,
            auth_epoch=User.auth_epoch + 1,
            email_verified_at=func.coalesce(User.email_verified_at, now),
            updated_at=now,
        ),
        execution_options={"synchronize_session": False},
    )
    _revoke_open(db, PasswordResetToken, user_id=user.id, now=now)
    db.expire(user, ["hashed_password", "auth_epoch", "email_verified_at", "updated_at"])
    db.flush()


def revoke_password_reset_token(token_id: str) -> None:
    """Revoke one reset token if it is still open, in a session of its own. Never raises.

    For the mail dispatcher, which runs after the request's session is gone: it commits
    here, and a failure is logged as a static event and swallowed.
    """
    _revoke_in_own_session(
        PasswordResetToken, token_id, failure_event="security.password_reset.revoke_failed"
    )


# --------------------------------------------------------------------------- #
# Email verification
# --------------------------------------------------------------------------- #
def request_email_verification(db: Session, *, user: User) -> IssuedToken | None:
    """Mint a verification token for ``user``'s stored address, if allowed.

    409 ``email_already_verified`` when the address is already verified. The cooldown
    and the daily cap return ``None`` with nothing minted or revoked, and a lost race
    returns ``None`` with nothing minted. Otherwise the account's open verification
    tokens are revoked and the new one, bound to the current address, is returned.
    Flushes but never commits.
    """
    now = database_now(db)
    locked = _lock_user(db, User.id == user.id)
    if locked is None or not locked.is_active:
        return None
    if locked.email_verified_at is not None:
        raise _already_verified()
    return _issue(
        db,
        EmailVerificationToken,
        user=locked,
        purpose=_EMAIL_VERIFICATION_PURPOSE,
        ttl=timedelta(hours=get_settings().email_verification_token_ttl_hours),
        now=now,
        email_snapshot=locked.email,
    )


def confirm_email_verification(db: Session, *, user: User, token: str) -> None:
    """Spend a verification token and mark ``user``'s stored address verified.

    The token must be ``user``'s own: another account's token is 403
    ``email_verification_wrong_account``, decided before any lock or write, so nothing
    is spent or changed. An unknown, expired, used or revoked token, or one minted for
    an address the account no longer has, is 404 ``email_verification_invalid``. The
    account's remaining open verification tokens are revoked. Flushes but never commits.
    """
    if not _is_well_formed(token):
        raise _verification_invalid()
    now = database_now(db)
    row = _find(db, EmailVerificationToken, hash_email_verification_token(token))
    if row is None:
        raise _verification_invalid()
    if row.user_id != user.id:
        raise PermissionDeniedError(
            "This verification link was issued to a different account.",
            code="email_verification_wrong_account",
        )
    locked = _lock_user(db, User.id == user.id)
    if (
        locked is None
        or not locked.is_active
        or not _is_open(row, now=now)
        or row.email_snapshot != locked.email
    ):
        raise _verification_invalid()
    if not _claim(
        db,
        EmailVerificationToken,
        row.id,
        EmailVerificationToken.email_snapshot == locked.email,
        now=now,
    ):
        raise _verification_invalid()

    db.execute(
        update(User)
        .where(User.id == locked.id)
        .values(email_verified_at=func.coalesce(User.email_verified_at, now), updated_at=now),
        execution_options={"synchronize_session": False},
    )
    _revoke_open(db, EmailVerificationToken, user_id=locked.id, now=now)
    db.expire(locked, ["email_verified_at", "updated_at"])
    db.flush()


def revoke_email_verification_token(token_id: str) -> None:
    """Revoke one verification token if it is still open, in a session of its own.

    Never raises; see :func:`revoke_password_reset_token`.
    """
    _revoke_in_own_session(
        EmailVerificationToken,
        token_id,
        failure_event="security.email_verification.revoke_failed",
    )


__all__ = [
    "MAX_PASSWORD_LENGTH",
    "MIN_PASSWORD_LENGTH",
    "IssuedToken",
    "confirm_email_verification",
    "confirm_password_reset",
    "hash_email_verification_token",
    "hash_password_reset_token",
    "request_email_verification",
    "request_password_reset",
    "revoke_email_verification_token",
    "revoke_password_reset_token",
]
