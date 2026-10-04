"""Password hashing and JWT helpers for the local auth provider."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime, timedelta
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings

ALGORITHM = "HS256"

#: The ONLY algorithm the verifier will accept. Fixed here, independent of anything the
#: token header claims (``alg=none`` and every other value are refused by the library when
#: it is not in this list). Kept as a one-element tuple so it cannot be widened by a caller.
ALLOWED_ALGORITHMS: tuple[str, ...] = (ALGORITHM,)

#: Expiry boundary parity with the previous library (python-jose 3.5.0). python-jose compared
#: ``exp`` with a whole-second clock and rejected only when ``exp < floor(now)``: a token stays
#: valid during its expiry second. PyJWT compares with a fractional clock and rejects when
#: ``exp <= now - leeway``; for a whole-second ``exp`` and a leeway of exactly one second that
#: is ``exp <= floor(now) - 1``, i.e. ``exp < floor(now)`` -- the previous rule, to the second,
#: at every fraction of the second. The leeway reaches ONLY ``exp``: :class:`_Verifier` keeps
#: it away from ``nbf`` (where it would admit a token one second early) and from ``iat``.
#: Changing this value changes every token's effective lifetime.
_EXP_LEEWAY_SECONDS = 1

#: Decode options: every verification the library offers stays ON. Nothing is made optional
#: that was previously required, and nothing is newly required (``require`` stays empty).
_DECODE_OPTIONS = {
    "verify_signature": True,
    "verify_exp": True,
    "verify_nbf": True,
    "verify_iat": True,
    "verify_aud": True,
    "verify_iss": True,
    "verify_sub": True,
    "verify_jti": True,
    "require": [],
}


class _Verifier(jwt.PyJWT):
    """PyJWT with the previous library's optional-claim rules (python-jose 3.5.0).

    The application mints only ``sub``/``sid``/``auth_epoch``/``email``/``iat``/``exp``, but a
    presented token can carry anything, and the replacement must not accept what the previous
    verifier refused. Five rules differ between the libraries and are pinned here, each with a
    test that fails if the override were bypassed (``test_jwt_library_replacement.py``). The
    comparison is the measured matrix in the evidence set (121 hand-signed cases, frozen
    clocks), not a proof over every possible token.

    * ``iat`` -- python-jose only required a numeric value (``int()`` succeeds) and never
      compared it with the clock, so a token minted on a clock ahead of the verifier's keeps
      authenticating. PyJWT's own check would refuse any future ``iat``; its ``verify_iat=False``
      would drop the numeric check as well. The override keeps exactly the numeric check.
    * ``nbf`` -- python-jose rejected ``nbf > floor(now)`` with no leeway. PyJWT applies the
      one global ``leeway`` to ``nbf`` too, which would admit a token one second before its
      not-before instant; the override restores the previous rule and ignores the leeway.
    * ``aud`` -- the application names no audience. python-jose rejected ANY present ``aud``
      claim in that configuration; PyJWT lets an empty or null one through. The override
      rejects every present ``aud``.
    * ``at_hash`` -- python-jose verified it by default and, with no access token to compare,
      rejected any present ``at_hash``; PyJWT never looks at it. The override rejects it.
    * JSON encoding -- python-jose decoded the header and payload bytes strictly as UTF-8 before
      parsing; PyJWT hands the raw bytes to ``json.loads``, which auto-detects a UTF-8 BOM,
      UTF-16 and UTF-32, so a key-signed token in one of those encodings would be newly
      accepted. :meth:`_decode_payload` and :func:`_require_utf8_json_header` keep the strict
      decode (a BOM is refused by the JSON parser, as before).

    Every override raises a :class:`jwt.PyJWTError`, which :func:`decode_access_token` turns
    into ``None`` (the caller's 401). The clock stays the library's single reading (``now``),
    so no second clock is consulted. The hooks are PyJWT's (``_validate_*``); the exact version
    is pinned by the lock, and the frozen-clock tests fail if a later version stops calling
    them.
    """

    def _validate_iat(self, payload: dict[str, Any], now: float, leeway: float) -> None:
        try:
            int(payload["iat"])
        except (ValueError, TypeError, OverflowError):
            raise jwt.InvalidIssuedAtError("Issued At claim (iat) must be an integer.") from None

    def _validate_nbf(self, payload: dict[str, Any], now: float, leeway: float) -> None:
        try:
            nbf = int(payload["nbf"])
        except (ValueError, TypeError, OverflowError):
            raise jwt.DecodeError("Not Before claim (nbf) must be an integer.") from None
        if nbf > math.floor(now):
            raise jwt.ImmatureSignatureError("The token is not yet valid (nbf)")

    def _validate_aud(
        self, payload: dict[str, Any], audience: Any, *, strict: bool = False
    ) -> None:
        if "aud" in payload:
            raise jwt.InvalidAudienceError("Invalid audience")

    def _decode_payload(self, decoded: dict[str, Any]) -> dict[str, Any]:
        return _utf8_json_object(decoded["payload"], "payload")

    def _validate_claims(
        self,
        payload: dict[str, Any],
        options: Any,
        audience: Any = None,
        issuer: Any = None,
        subject: str | None = None,
        leeway: float | timedelta = 0,
    ) -> None:
        super()._validate_claims(
            payload, options, audience=audience, issuer=issuer, subject=subject, leeway=leeway
        )
        if "at_hash" in payload:
            raise jwt.InvalidTokenError("at_hash claim present with no access token to compare")


_verifier = _Verifier()


def _utf8_json_object(raw: bytes, what: str) -> dict[str, Any]:
    """Parse ``raw`` exactly as python-jose did: strict UTF-8 text, then JSON, then an object."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise jwt.DecodeError(f"Invalid {what} string: not UTF-8") from None
    try:
        value = json.loads(text)
    except (ValueError, RecursionError) as e:
        raise jwt.DecodeError(f"Invalid {what} string: {e}") from None
    if not isinstance(value, dict):
        raise jwt.DecodeError(f"Invalid {what} string: must be a json object")
    return value


def _require_utf8_json_header(token: str) -> None:
    """Refuse a header segment that is not strict UTF-8 JSON (python-jose parity; see
    :class:`_Verifier`). Anything else about the segment -- missing parts, bad base64url, a
    non-object -- is left to the library, which refuses it with its own error."""
    segment = token.split(".", 1)[0]
    try:
        raw = jwt.utils.base64url_decode(segment)
    except Exception:  # noqa: BLE001 - the library reports malformed base64url itself
        return
    _utf8_json_object(raw, "header")


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
    outlives its session: ``exp = min(now + TTL, expires_at_cap)``. PyJWT encodes datetime
    claims as whole seconds (``timegm`` of the UTC time tuple, i.e. it floors, exactly as
    python-jose did), so the encoded ``exp == min(iat + TTL_s, floor(expires_at_cap))``.
    The app clock can only make a token expire earlier than its
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
    """Verify and decode an access token; ``None`` for anything that does not verify.

    Every library refusal -- bad signature, wrong or missing algorithm (``alg=none``
    included), malformed token, expired ``exp``, premature ``nbf``, non-numeric ``iat``/
    ``exp``/``nbf``, non-string ``sub``/``jti``, any ``aud`` or ``at_hash`` claim, a header
    or payload that is not strict UTF-8 JSON -- maps to ``None``, and the caller turns
    ``None`` into its usual 401. Refusals the previous library did not make, all disclosed in
    the evidence matrix: a malformed time claim it let escape as a Python ``TypeError``/
    ``OverflowError`` (an unhandled 500) is ``None`` here; so are an unsupported ``crit``
    header, an empty ``crit`` list, a non-string ``kid``, ``b64: false`` and a signature
    segment with non-canonical base64url trailing bits (PyJWT's RFC 7515 / 7797 header and
    base64url validation). The algorithm allow-list is :data:`ALLOWED_ALGORITHMS`; the token
    header has no say in it.
    """
    settings = get_settings()
    try:
        _require_utf8_json_header(token)
        return _verifier.decode(
            token,
            settings.secret_key,
            algorithms=list(ALLOWED_ALGORITHMS),
            options=_DECODE_OPTIONS,
            leeway=_EXP_LEEWAY_SECONDS,
        )
    except jwt.PyJWTError:
        return None
