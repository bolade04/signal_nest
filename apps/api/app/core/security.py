"""Password hashing and JWT helpers for the local auth provider."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
from jose import JWTError, jwt

from app.core.config import get_settings

ALGORITHM = "HS256"


def _prepare(password: str) -> bytes:
    # bcrypt only considers the first 72 bytes; truncate deterministically so long
    # passwords never raise (matches bcrypt's own documented limit).
    return password.encode("utf-8")[:72]


def hash_password(password: str) -> str:
    return bcrypt.hashpw(_prepare(password), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_prepare(plain), hashed.encode("utf-8"))
    except ValueError:
        return False


#: Claims only the builder sets, from server state. There is no caller-supplied claims
#: dict, so none of them can be supplied or overwritten by a caller (P6-AUTH-4).
RESERVED_CLAIMS = frozenset({"sub", "sid", "auth_epoch", "iat", "exp", "nbf", "iss", "aud", "jti"})


def create_access_token(
    *,
    subject: str,
    session_id: str,
    auth_epoch: int,
    expires_at_cap: datetime,
    email: str,
) -> str:
    """Mint an access token bound to one server-side session (P6-AUTH-4).

    The application clock is read EXACTLY ONCE: ``iat`` and ``exp`` both derive from that
    one reading, and ``exp`` is capped at the session's absolute expiry, so no token
    outlives its session: ``exp = min(now + TTL, expires_at_cap)``. python-jose encodes
    whole seconds (it floors), so the encoded ``exp == min(iat + TTL_s,
    floor(expires_at_cap))``. The app clock can only make a token expire earlier than its
    session; the database clock alone decides the session's own lifetime.

    Every claim comes from the keyword arguments; there is no generic claims dict, so the
    reserved claims (:data:`RESERVED_CLAIMS`) can never be supplied by a caller.
    """
    settings = get_settings()
    now = datetime.now(UTC)
    cap = expires_at_cap if expires_at_cap.tzinfo else expires_at_cap.replace(tzinfo=UTC)
    payload: dict[str, Any] = {
        "sub": subject,
        "sid": session_id,
        "auth_epoch": auth_epoch,
        "email": email,
        "iat": now,
        "exp": min(now + timedelta(minutes=settings.access_token_expire_minutes), cap),
    }
    return jwt.encode(payload, settings.secret_key, algorithm=ALGORITHM)


def decode_access_token(token: str) -> dict[str, Any] | None:
    settings = get_settings()
    try:
        return jwt.decode(token, settings.secret_key, algorithms=[ALGORITHM])
    except JWTError:
        return None
