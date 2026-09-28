"""Auth models: sign-in sessions (P6-AUTH-4), password reset and email verification.

**Sessions.** ``auth_sessions`` holds one row per sign-in. Every access token carries its
session's id as the ``sid`` claim, and every authenticated request requires that row to
belong to the token's user, be unrevoked and be unexpired on the database clock. The
row is created only by a fresh sign-in (login, register, invitation register); its
``expires_at`` is fixed then and never moved, so no re-issued token outlives it.
``revoked_at`` set means revoked (``POST /auth/logout`` for one session, ``POST
/auth/logout-all`` for every session of the account). There is no device, IP address,
user agent or refresh column.

**Account tokens.** Password reset and email verification.

Each row is one single-use token minted for one user. Only the SHA-256 hex digest of
the token is stored, prefixed by its purpose before hashing so a digest from one
table can never match in the other; the raw token is handed to the mailer once and
never persisted. There is no status column: ``revoked_at`` set means revoked, else
``used_at`` set means used, else ``expires_at`` at or before the database clock
means expired, else open.

Neither table records an IP address, user agent, organization or workspace: the
tokens belong to the account, not to a tenant.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, func, text
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: Predicate selecting the tokens still open for use, shared by both tables' partial
#: unique indexes on both dialects. Expiry is deliberately outside it: an index
#: predicate cannot read the clock, and the service revokes the open row before it
#: inserts the next.
_OPEN_TOKEN_PREDICATE = "used_at IS NULL AND revoked_at IS NULL"


class AuthSession(Base, UUIDPrimaryKeyMixin):
    """One sign-in: the server-side record every access token of that sign-in is bound to.

    The row's ``id`` is the tokens' ``sid`` claim. ``created_at`` and ``revoked_at`` are
    stamped from the database clock (``app.db.clock.database_now``); ``expires_at`` is
    ``created_at`` plus ``session_absolute_lifetime_minutes``, fixed at creation and never
    updated. There is deliberately no ``TimestampMixin``: no ``updated_at`` and no
    application-clock default can reach a session row. Expired and revoked rows are
    inert history.
    """

    __tablename__ = "auth_sessions"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class PasswordResetToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A one-time token that lets its holder set a new password for one account."""

    __tablename__ = "password_reset_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_password_reset_tokens_token_hash"),
        # At most one open reset token per user. Used and revoked rows fall outside
        # the predicate, so history is kept.
        Index(
            "uq_password_reset_tokens_open",
            "user_id",
            unique=True,
            postgresql_where=text(_OPEN_TOKEN_PREDICATE),
            sqlite_where=text(_OPEN_TOKEN_PREDICATE),
        ),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: SHA-256 hex digest of ``"password-reset:" + token``. Never logged or returned.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailVerificationToken(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A one-time token proving its holder can read mail sent to one stored address."""

    __tablename__ = "email_verification_tokens"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_email_verification_tokens_token_hash"),
        # At most one open verification token per user, as for password resets.
        Index(
            "uq_email_verification_tokens_open",
            "user_id",
            unique=True,
            postgresql_where=text(_OPEN_TOKEN_PREDICATE),
            sqlite_where=text(_OPEN_TOKEN_PREDICATE),
        ),
    )

    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: SHA-256 hex digest of ``"email-verification:" + token``. Never logged or returned.
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The address the token was mailed to. The token verifies this address only: once
    #: the account's stored address differs, the token no longer verifies anything.
    email_snapshot: Mapped[str] = mapped_column(String(320), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = ["AuthSession", "EmailVerificationToken", "PasswordResetToken"]
