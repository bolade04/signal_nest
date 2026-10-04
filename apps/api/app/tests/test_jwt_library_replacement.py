"""P6 JWT library replacement (python-jose 3.5.0 -> PyJWT 2.15.1): parity and rejection tests.

Every token here is minted with a SYNTHETIC key under a per-test settings override and, where
the structure matters, by hand (HMAC over exact header/payload bytes); no real key, production
token or user data is involved. The clock the verifier reads is FROZEN per case (fractional
seconds included), so no outcome depends on the wall clock.

What is pinned, and against what:

* the previous library's optional-claim rules, measured on the installed python-jose 3.5.0
  with these same tokens (evidence set P6-AUTH-pyjwt, raw/compat/matrix): a token is accepted
  during its ``exp`` second and refused from the next second on; ``nbf`` is honoured to the
  second with NO leeway; ``iat`` only has to be numeric and is never compared with the clock;
  any present ``aud`` or ``at_hash`` claim is refused (no audience / access token is
  configured); non-string ``sub``/``jti`` are refused (python-jose and PyJWT share this);
* that each :class:`app.core.security._Verifier` override is live: the plain library call is
  shown to decide the other way on the same token (a positive instance for every rule);
* intentional hardenings, recorded as such, not as parity: a malformed time claim the previous
  library let escape as a Python ``TypeError``/``OverflowError`` (an unhandled 500) is a plain
  refusal (401) here; an unsupported ``crit`` header, an empty ``crit`` list and a non-string
  ``kid`` -- accepted by python-jose -- are refused by PyJWT's RFC 7515 header validation;
* signing is HS256 and verification accepts HS256 only; wrong key, altered payload or
  signature, malformed input, ``alg=none`` and every other algorithm map to ``None`` (the
  caller's 401), never to an exception;
* tokens minted by the previous library (python-jose 3.5.0, same synthetic key) still decode.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
from calendar import timegm
from datetime import UTC, datetime, timedelta

import jwt
import jwt.api_jwt as api_jwt
import pytest

from app.core import security
from app.core.config import get_settings

SYNTHETIC_KEY = "synthetic-test-key-for-jwt-replacement-tests-not-a-secret-0123456789"
OTHER_KEY = "another-synthetic-key-that-must-not-verify-anything-9876543210"
#: A fixed boundary instant (2027-01-15T08:00:00Z); every clock below is frozen relative to it.
T = 1_800_000_000
BASE = {"sub": "user-1", "sid": "a" * 32, "auth_epoch": 3, "email": "u@example.test"}
HS256 = {"alg": "HS256", "typ": "JWT"}


@pytest.fixture
def synthetic_key(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", SYNTHETIC_KEY)
    return SYNTHETIC_KEY


class _RealInstances(type):
    """``isinstance(x, Frozen)`` holds for every real datetime, so the library's encode-side
    ``isinstance(claim, datetime)`` conversion keeps working while the clock is patched."""

    def __instancecheck__(cls, inst):
        return isinstance(inst, datetime)


class Frozen(datetime, metaclass=_RealInstances):
    """The verifier's clock, pinned to one instant with its fraction of a second."""

    _now = float(T)

    @classmethod
    def now(cls, tz=None):
        d = datetime.fromtimestamp(cls._now, tz=UTC)
        return d if tz is not None else d.replace(tzinfo=None)


@pytest.fixture
def clock(monkeypatch):
    """Freeze the verifier's clock (PyJWT reads ``datetime`` from ``jwt.api_jwt``); returns a
    setter taking an epoch instant, fractional seconds allowed."""
    monkeypatch.setattr(api_jwt, "datetime", Frozen)

    def at(epoch: float) -> None:
        Frozen._now = float(epoch)

    at(T)
    return at


def _b64(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).decode().rstrip("=")


def _enc(obj) -> str:
    return _b64(json.dumps(obj, separators=(",", ":")).encode())


def _mint(key: str = SYNTHETIC_KEY, *, header=HS256, digest=hashlib.sha256, payload=None, **claims):
    """Hand-signed token: HMAC over exact bytes, so structure cases are byte-controlled.

    ``payload`` (a ready base64url segment) overrides the claims entirely."""
    p = payload if payload is not None else _enc({**BASE, **claims})
    h = _enc(header)
    sig = hmac.new(key.encode(), f"{h}.{p}".encode(), digest).digest()
    return f"{h}.{p}.{_b64(sig)}"


def _decodes(token: str) -> bool:
    return security.decode_access_token(token) is not None


def _plain(token: str, **kw):
    """The bare library call (no override), same key and allow-list; True = accepted."""
    try:
        jwt.decode(token, SYNTHETIC_KEY, algorithms=["HS256"], **kw)
    except jwt.PyJWTError:
        return False
    return True


# --- the producer -----------------------------------------------------------------------


def test_create_access_token_signs_hs256_and_keeps_claim_types(synthetic_key):
    cap = datetime.now(UTC) + timedelta(hours=1)
    token = security.create_access_token(
        subject="user-1", session_id="s" * 32, auth_epoch=7, expires_at_cap=cap, email="u@x.test"
    )
    header = json.loads(base64.urlsafe_b64decode(token.split(".")[0] + "=="))
    assert header["alg"] == "HS256" and header["typ"] == "JWT"
    claims = security.decode_access_token(token)
    assert claims is not None
    assert claims["sub"] == "user-1" and type(claims["sub"]) is str
    assert claims["sid"] == "s" * 32 and type(claims["sid"]) is str
    assert claims["auth_epoch"] == 7 and type(claims["auth_epoch"]) is int
    assert type(claims["iat"]) is int and type(claims["exp"]) is int
    assert claims["exp"] <= timegm(cap.utctimetuple())  # never outlives the session cap
    assert claims["exp"] - claims["iat"] <= get_settings().access_token_expire_minutes * 60
    assert set(claims) == {"sub", "sid", "auth_epoch", "email", "iat", "exp"}


def test_allow_list_is_fixed_to_hs256():
    assert security.ALGORITHM == "HS256"
    assert security.ALLOWED_ALGORITHMS == ("HS256",)


def test_every_library_verification_stays_on():
    opts = security._DECODE_OPTIONS
    assert all(opts[k] for k in opts if k.startswith("verify_"))
    assert opts["require"] == []
    assert security._EXP_LEEWAY_SECONDS == 1


def test_overrides_bind_to_the_pinned_library_hooks():
    """Each override replaces a hook PyJWT 2.15.1 defines, with the same parameter list; the
    frozen-clock tests below fail outright if a later PyJWT stopped calling them."""
    for name in ("_validate_iat", "_validate_nbf", "_validate_aud", "_validate_claims"):
        assert name in vars(jwt.PyJWT), name
        assert name in vars(security._Verifier), name
        ours = inspect.signature(getattr(security._Verifier, name)).parameters
        theirs = inspect.signature(getattr(jwt.PyJWT, name)).parameters
        assert [(p.name, p.kind) for p in ours.values()] == [
            (p.name, p.kind) for p in theirs.values()
        ], name


# --- exp: accepted during the expiry second, refused from the next second on --------------


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [(-1, True), (-0.5, True), (-0.001, True), (0, True), (0.001, True), (0.5, True),
     (0.999, True), (1, False), (1.5, False), (2, False)],
)
def test_exp_boundary_to_the_fraction_of_a_second(synthetic_key, clock, offset, accepted):
    clock(T + offset)
    assert _decodes(_mint(exp=T)) is accepted


def test_exp_leeway_is_what_restores_the_previous_boundary(synthetic_key, clock):
    # positive instance: with the library's default (no leeway) the expiry second is refused
    clock(T + 0.5)
    assert _plain(_mint(exp=T)) is False
    assert _plain(_mint(exp=T), leeway=1) is True
    assert _decodes(_mint(exp=T))


# --- nbf: honoured to the second with NO leeway (the exp leeway must not reach it) ---------


@pytest.mark.parametrize(
    ("offset", "accepted"),
    [(-1, False), (-0.5, False), (-0.001, False), (0, True), (0.001, True), (0.5, True),
     (0.999, True), (1, True), (1.5, True), (2, True)],
)
def test_nbf_boundary_to_the_fraction_of_a_second(synthetic_key, clock, offset, accepted):
    clock(T + offset)
    assert _decodes(_mint(nbf=T, exp=T + 3600)) is accepted


def test_nbf_override_is_live_the_global_leeway_would_admit_a_token_early(synthetic_key, clock):
    clock(T - 1)
    token = _mint(nbf=T, exp=T + 3600)
    assert _plain(token, leeway=1) is True  # the first delivery's shape: one second early
    assert _plain(token) is False
    assert _decodes(token) is False


# --- iat: numeric, never compared with the clock -------------------------------------------


@pytest.mark.parametrize("offset", [-3600, -1, -0.5, 0, 0.5, 1])
def test_iat_is_never_compared_with_the_clock(synthetic_key, clock, offset):
    clock(T + offset)  # negative offset = iat in the verifier's future
    assert _decodes(_mint(iat=T, exp=T + 3600))


def test_iat_override_is_live_the_library_would_refuse_a_future_iat(synthetic_key, clock):
    clock(T - 60)
    token = _mint(iat=T, exp=T + 3600)
    assert _plain(token) is False
    assert _decodes(token)


_ABSENT = object()
_INF = float("inf")  # serialises as the non-standard JSON token Infinity


@pytest.mark.parametrize(
    ("value", "accepted"),
    [(_ABSENT, True), (T, True), (T + 0.5, True), (str(T), True), (f" {T} ", True),
     (True, True), (10**30, True), (-5, True),
     (f"{T}.5", False), (None, False), ("abc", False), ("", False), ([], False), ({}, False),
     (_INF, False)],
    ids=["absent", "int", "float", "numeric-string", "spaced-numeric-string", "bool", "huge-int",
         "negative", "numeric-string-float", "null", "alpha", "empty", "list", "object",
         "infinity"],
)
def test_iat_value_forms(synthetic_key, clock, value, accepted):
    token = _mint(exp=T + 3600) if value is _ABSENT else _mint(iat=value, exp=T + 3600)
    assert _decodes(token) is accepted


def test_iat_numeric_check_is_live_verify_iat_off_would_accept_anything(synthetic_key, clock):
    token = _mint(iat="abc", exp=T + 3600)
    assert _plain(token, options={"verify_iat": False}) is True  # the first delivery's shape
    assert _decodes(token) is False


# --- exp / nbf value forms ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "accepted"),
    [(_ABSENT, True), (str(T + 3600), True), (T + 0.5, True),
     (None, False), ("abc", False), ([], False), (True, False), (_INF, False)],
    ids=["absent", "numeric-string", "float", "null", "alpha", "list", "bool", "infinity"],
)
def test_exp_value_forms(synthetic_key, clock, value, accepted):
    payload = _enc({k: v for k, v in BASE.items()}) if value is _ABSENT else None
    token = _mint(payload=payload) if value is _ABSENT else _mint(exp=value)
    assert _decodes(token) is accepted  # bool -> exp == 1, long expired


@pytest.mark.parametrize(
    ("value", "accepted"),
    [(_ABSENT, True), (T - 0.5, True), (True, True),
     (str(T + 3600), False), (None, False), ("abc", False), ([], False), (_INF, False)],
    ids=["absent", "float-past", "bool", "numeric-string-future", "null", "alpha", "list",
         "infinity"],
)
def test_nbf_value_forms(synthetic_key, clock, value, accepted):
    token = _mint(exp=T + 3600) if value is _ABSENT else _mint(nbf=value, exp=T + 3600)
    assert _decodes(token) is accepted


# --- aud / at_hash: any present claim is refused (no audience, no access token configured) -


@pytest.mark.parametrize(
    "value", [None, "", [], "api", ["api"], ["a", "b"], 0, 1, False, [1], {}],
    ids=["null", "empty-string", "empty-list", "string", "list-one", "list-two", "zero", "one",
         "false", "list-non-string", "object"],
)
def test_any_present_aud_is_refused(synthetic_key, clock, value):
    assert _decodes(_mint(aud=value, exp=T + 3600)) is False


def test_absent_aud_is_accepted(synthetic_key, clock):
    assert _decodes(_mint(exp=T + 3600))


def test_aud_override_is_live_the_library_lets_an_empty_aud_through(synthetic_key, clock):
    for value in (None, "", [], 0, False, {}):
        assert _plain(_mint(aud=value, exp=T + 3600)) is True, value


@pytest.mark.parametrize("value", ["x" * 22, None, ""], ids=["string", "null", "empty"])
def test_any_present_at_hash_is_refused(synthetic_key, clock, value):
    token = _mint(at_hash=value, exp=T + 3600)
    assert _plain(token) is True  # the library never looks at at_hash
    assert _decodes(token) is False


# --- sub / jti / iss ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "accepted"),
    [("user-1", True), ("", True), (_ABSENT, True),
     (12345, False), (None, False), (True, False), (["user-1"], False)],
    ids=["string", "empty-string", "absent", "int", "null", "bool", "list"],
)
def test_sub_must_be_a_string_when_present(synthetic_key, clock, value, accepted):
    """Shared with the previous library (python-jose checked ``sub`` by default too); the
    caller refuses an absent or unknown subject afterwards."""
    if value is _ABSENT:
        claims = {k: v for k, v in BASE.items() if k != "sub"} | {"exp": T + 3600}
        token = _mint(payload=_enc(claims))
    else:
        token = _mint(sub=value, exp=T + 3600)
    assert _decodes(token) is accepted


@pytest.mark.parametrize(
    ("value", "accepted"), [("jti-1", True), (5, False), (None, False)], ids=["str", "int", "null"]
)
def test_jti_must_be_a_string_when_present(synthetic_key, clock, value, accepted):
    assert _decodes(_mint(jti=value, exp=T + 3600)) is accepted


@pytest.mark.parametrize("value", ["issuer-1", 5, None], ids=["str", "int", "null"])
def test_iss_is_not_checked_without_a_configured_issuer(synthetic_key, clock, value):
    assert _decodes(_mint(iss=value, exp=T + 3600))


# --- structure, signature, algorithm ----------------------------------------------------------


def _good():
    return _mint(exp=T + 3600)


def _split(token):
    return token.split(".")


NONE_HEADER = {"alg": "none", "typ": "JWT"}


def _reheaded(header: dict) -> str:
    """A valid token's payload and HS256 signature under a replaced header."""
    _, p, s = _split(_good())
    return f"{_enc(header)}.{p}.{s}"


def _altered_payload() -> str:
    h, _, s = _split(_good())
    return ".".join([h, _enc({**BASE, "exp": T + 3600, "auth_epoch": 99}), s])


REJECTED = {
    "wrong-key": lambda: _mint(OTHER_KEY, exp=T + 3600),
    "HS384-signed": lambda: _mint(header={"alg": "HS384", "typ": "JWT"}, digest=hashlib.sha384,
                                  exp=T + 3600),
    "HS512-signed": lambda: _mint(header={"alg": "HS512", "typ": "JWT"}, digest=hashlib.sha512,
                                  exp=T + 3600),
    "alg-none-unsigned": lambda: ".".join([_enc(NONE_HEADER), _split(_good())[1], ""]),
    "alg-none-over-hs256-sig": lambda: _reheaded(NONE_HEADER),
    "RS256-header-over-hmac-sig": lambda: _reheaded({"alg": "RS256", "typ": "JWT"}),
    "signature-altered": lambda: _good()[:-1] + ("A" if _good()[-1] != "A" else "B"),
    "payload-altered": _altered_payload,
    "garbage": lambda: "not-a-jwt",
    "empty": lambda: "",
    "two-segments": lambda: "a.b",
    "four-segments": lambda: _good() + ".extra",
    "payload-json-array": lambda: _mint(payload=_enc([1, 2])),
    "payload-json-string": lambda: _mint(payload=_enc("x")),
    "payload-not-json": lambda: _mint(payload=_b64(b"{not json")),
    "payload-deep-nesting-5000": lambda: _mint(payload=_b64(("[" * 5000 + "]" * 5000).encode())),
    # header hardening (PyJWT RFC 7515 validation; python-jose accepted these -- disclosed)
    "crit-unsupported": lambda: _mint(header={**HS256, "crit": ["zzz"], "zzz": 1}, exp=T + 3600),
    "crit-empty-list": lambda: _mint(header={**HS256, "crit": []}, exp=T + 3600),
    "kid-not-a-string": lambda: _mint(header={**HS256, "kid": 7}, exp=T + 3600),
}


@pytest.mark.parametrize("name", sorted(REJECTED))
def test_rejections_map_to_none_not_exceptions(synthetic_key, clock, name):
    assert security.decode_access_token(REJECTED[name]()) is None


@pytest.mark.parametrize(
    "header", [{"alg": "HS256"}, {"alg": "HS256", "typ": "foo"}, {**HS256, "kid": "k1"}],
    ids=["no-typ", "typ-foo", "kid-string"],
)
def test_header_forms_the_previous_library_accepted_still_decode(synthetic_key, clock, header):
    assert _decodes(_mint(header=header, exp=T + 3600))


def test_empty_claims_object_decodes_and_is_refused_by_the_caller_not_the_verifier(
    synthetic_key, clock
):
    assert security.decode_access_token(_mint(payload=_enc({}))) == {}


# --- cross-library compatibility: tokens minted by python-jose 3.5.0 ---------------------------

#: Minted OUT OF BAND with python-jose 3.5.0 (the previous dependency) using SYNTHETIC_KEY,
#: recorded verbatim; exp is far in the future (2099-01-01T00:00:00Z = 4070908800) so the
#: fixtures stay valid for any run. These are the exact bytes a baseline deployment would
#: have issued (modulo the key), and they must keep decoding under the replacement.
BASELINE_JOSE_TOKENS = {
    "far-future": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyLTEiLCJzaWQiOiJhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYSIsImF1dGhfZXBvY2giOjMsImVtYWlsIjoidUBleGFtcGxlLnRlc3QiLCJleHAiOjQwNzA5MDg4MDB9.oicB_fmSMwdMxJ6vHRNNxM_sqxQc1APQypYyhNKBP0w",  # noqa: E501
    "far-future-with-iat": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiJ1c2VyLTEiLCJzaWQiOiJhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYWFhYSIsImF1dGhfZXBvY2giOjMsImVtYWlsIjoidUBleGFtcGxlLnRlc3QiLCJleHAiOjQwNzA5MDg4MDAsImlhdCI6MTcwMDAwMDAwMH0.4cUOBDy-N3_OMsVkmgrTe8GUZdQ-1FvRUsslJmA-FN0",  # noqa: E501
}


@pytest.mark.parametrize("name", sorted(BASELINE_JOSE_TOKENS))
def test_tokens_minted_by_the_previous_library_still_decode(synthetic_key, name):
    claims = security.decode_access_token(BASELINE_JOSE_TOKENS[name])
    assert claims is not None
    assert claims["sub"] == "user-1" and claims["auth_epoch"] == 3 and claims["exp"] == 4070908800


def test_baseline_token_with_wrong_key_is_still_rejected(monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "secret_key", OTHER_KEY)
    assert security.decode_access_token(BASELINE_JOSE_TOKENS["far-future"]) is None
