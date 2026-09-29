"""6B-4A: nothing secret reaches a log, stdout, an error, or the API contract.

**Logs.** Every record from every logger is captured -- a handler on the root logger at
DEBUG, and the same handler attached to every logger that does not propagate (uvicorn's are
the usual ones) -- while all four operations run through their success AND failure paths:
issued, unknown, inactive and cooling-down reset requests; a completed, a replayed, an
unknown and a refused-password confirm; issued, cooling-down and already-verified
verification requests; a wrong-account, an unknown and a completed verification confirm;
and two background sends that fail, one with a ``MailSendError`` and one with an arbitrary
exception whose message quotes the whole email; the two P6-AUTH-4 session revocations
(a logout, the same token's refused repeat, and a logout-all); and the P6-UI-017 password
change (a wrong current password, an unchanged password, a too-short new password, and a
success). Each record is rendered three
ways (its message, its full ``__dict__``, and the production JSON formatter's output with any
traceback) and must contain no raw token, no digest, no link, no email address, no
password, no password hash (before or after a change), no mail body, no access token and
no session id. Positive controls prove the
capture really saw the request log, every security event, the mail events and both failure
events -- and that the failed tokens were revoked (AUTH2-C3).

**422 responses (P6-UI-017, U17-S03, FD-U17-5 = A).** A request that fails validation gets
back ``type``/``loc``/``msg`` (and only the schema's own bounds in ``ctx``) -- never the
submitted value. Distinct synthetic markers stand in for passwords, tokens and secret-looking
extras on register, invitation register, reset confirm, login and the password change: out
of bounds passwords, a missing field (for which Pydantic's ``input`` is the WHOLE body), an
unknown field, malformed JSON and request-derived context. Each case first shows, on the
product's own schema model, that Pydantic itself would echo the marker -- so the redaction,
not the case, is what keeps it out -- and no marker reaches the response, its headers, a log
record or a stream. The password change's own refusals (wrong current password, unchanged
password) echo no password and no hash either.

**OpenAPI.** The four account-token operations take the token only in a JSON body that
forbids unknown fields, and the password change takes both passwords the same way; no
operation anywhere has a token in its path, query or headers; the two public operations
have no parameters at all (no bearer dependency), the two verification operations and the
password change only the ``authorization`` header; each answers 204 with no body. 106
operations (105 before P6-UI-017 added the password change).
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Literal

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.auth.models import EmailVerificationToken, PasswordResetToken
from app.auth.schemas import (
    ChangePasswordRequest,
    InvitationRegisterRequest,
    LoginRequest,
    PasswordResetConfirmRequest,
    RegisterRequest,
)
from app.core.errors import register_exception_handlers
from app.core.logging import JsonFormatter
from app.infra import mail
from app.locations.schemas import GeoCoverageBase
from app.main import app
from app.organizations.models import User
from app.tests._auth2_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    CASEY,
    DANA,
    EMAIL,
    INACTIVE,
    INVITATION_REGISTER,
    LOGIN,
    LOGOUT,
    LOGOUT_ALL,
    NEW_PASSWORD,
    OLD_HASH,
    OLD_PASSWORD,
    REGISTER,
    RESET_CONFIRM,
    RESET_PATH,
    RESET_REQUEST,
    UNKNOWN_EMAIL,
    VERIFY_CONFIRM,
    VERIFY_PATH,
    VERIFY_REQUEST,
    Env,
    active_rate_limiter,
    bearer,
    confirm_reset,
    confirm_verification,
    environment,
    failing_mail,
    link_token,
    mint_reset,
    mint_verification,
    open_session,
    plain_digest,
    post,
    request_reset,
    request_verification,
    reset_digest,
    verification_digest,
)

WEAK_PASSWORD = "shorty7"
CHANGE_PASSWORD = f"{API}/auth/password/change"
#: The password change's own synthetic credentials (P6-UI-017).
WRONG_CURRENT_PASSWORD = "not-the-current-password-3"
CHANGED_PASSWORD = "changed-password-58"


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "leakage.db") as e:
        yield e


class _Everything(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


#: Loggers pushed to DEBUG for the capture (restored afterwards). SQLAlchemy is left at its
#: own WARN default: statement echo is off in every environment, and turning it on here would
#: log bound parameters the application never logs.
_NOISY = ("signalnest", "app", "uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "httpx")


@contextmanager
def capture_all_logs() -> Iterator[list[logging.LogRecord]]:
    handler = _Everything()
    root = logging.getLogger()
    saved_root = root.level
    saved = {name: logging.getLogger(name).level for name in _NOISY}
    detached = [
        lg
        for lg in logging.root.manager.loggerDict.values()
        if isinstance(lg, logging.Logger) and not lg.propagate
    ]
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    for name in _NOISY:
        logging.getLogger(name).setLevel(logging.DEBUG)
    for lg in detached:
        lg.addHandler(handler)
    try:
        yield handler.records
    finally:
        root.removeHandler(handler)
        root.setLevel(saved_root)
        for name, level in saved.items():
            logging.getLogger(name).setLevel(level)
        for lg in detached:
            lg.removeHandler(handler)


_FORMATTER = JsonFormatter(service="signalnest-api", environment="production")


def _rendered(rec: logging.LogRecord) -> str:
    parts = [rec.getMessage(), json.dumps(rec.__dict__, default=str), _FORMATTER.format(rec)]
    if rec.exc_info:
        parts.append(logging.Formatter().formatException(rec.exc_info))
    return "\n".join(parts)


def _secrets(env: Env, raws: list[str], messages: list) -> list[str]:
    out = list(raws)
    for raw in raws:
        out += [reset_digest(raw), verification_digest(raw), plain_digest(raw)]
    out += [r["token_hash"] for r in env.witness.reset_tokens()]
    out += [r["token_hash"] for r in env.witness.verification_tokens()]
    out += ["#token=", RESET_PATH + "#", VERIFY_PATH + "#"]
    out += [*EMAIL.values(), UNKNOWN_EMAIL, "alice@", "bob@", "casey@", "Dana@", "nobody@"]
    out += [OLD_PASSWORD, NEW_PASSWORD, WEAK_PASSWORD, "second-new-password"]
    out += [WRONG_CURRENT_PASSWORD, CHANGED_PASSWORD]
    # Every bcrypt hash: the seeded one (each account's "before") and every account's stored
    # hash now -- CAROL's after her password change, ALICE's after her reset.
    out += [OLD_HASH, *(r["hashed_password"] for r in env.witness.rows(User))]
    for m in messages:
        out += [m.text_body, m.html_body]
        out += [line for line in m.text_body.splitlines() if "http" in line]
    return [s for s in out if s]


def _run_every_path(env: Env, monkeypatch) -> tuple[list[str], list]:
    """Drive all four operations through success and failure paths; return raw tokens."""
    raws: list[str] = []
    handed: list = []
    # Reset requests: issued, unknown, inactive, cooldown.
    alice_reset = mint_reset(env, ALICE)
    raws.append(alice_reset)
    request_reset(env, UNKNOWN_EMAIL)
    request_reset(env, EMAIL[INACTIVE])
    request_reset(env, EMAIL[ALICE])
    # Reset confirms: refused password (schema), unknown, completed, replayed.
    carol_reset = mint_reset(env, CAROL)
    raws.append(carol_reset)
    post(env, RESET_CONFIRM, {"token": carol_reset, "new_password": WEAK_PASSWORD})
    confirm_reset(env, "Z" * 43)
    raws.append("Z" * 43)
    assert confirm_reset(env, alice_reset).status_code == 204
    confirm_reset(env, alice_reset, "second-new-password")
    # Verification requests: issued, cooldown, already verified (ALICE after her reset).
    bob_verification = mint_verification(env, BOB)
    raws.append(bob_verification)
    request_verification(env, BOB)
    assert request_verification(env, ALICE).status_code == 409
    # Verification confirms: wrong account, unknown, completed.
    confirm_verification(env, bob_verification, CAROL)
    confirm_verification(env, "Y" * 43, BOB)
    raws.append("Y" * 43)
    assert confirm_verification(env, bob_verification, BOB).status_code == 204
    # Session revocation (P6-AUTH-4): a logout, its refused repeat, a logout-all. The access
    # tokens and session ids join the secrets.
    ended, remaining = open_session(env.request_engine, BOB), open_session(env.request_engine, BOB)
    assert post(env, LOGOUT, None, token=ended[0]).status_code == 204
    assert post(env, LOGOUT, None, token=ended[0]).status_code == 401
    assert post(env, LOGOUT_ALL, None, token=remaining[0]).status_code == 204
    raws += [*ended, *remaining]
    # Password change (P6-UI-017), on one session: a wrong current password, an unchanged
    # password and a too-short new one (each 422, the session still live), then a success.
    changing = open_session(env.request_engine, CAROL)
    for body, refused in (
        (
            {"current_password": WRONG_CURRENT_PASSWORD, "new_password": CHANGED_PASSWORD},
            "current_password_incorrect",
        ),
        ({"current_password": OLD_PASSWORD, "new_password": OLD_PASSWORD}, "password_unchanged"),
        ({"current_password": OLD_PASSWORD, "new_password": WEAK_PASSWORD}, "validation_error"),
    ):
        r = post(env, CHANGE_PASSWORD, body, token=changing[0])
        assert (r.status_code, r.json()["error"]["code"]) == (422, refused), r.text
    body = {"current_password": OLD_PASSWORD, "new_password": CHANGED_PASSWORD}
    assert post(env, CHANGE_PASSWORD, body, token=changing[0]).status_code == 204
    assert env.witness.user(CAROL)["hashed_password"] != OLD_HASH  # a real "after" hash
    raws += [*changing]
    # Background sends that fail: a provider error, and a defect quoting the whole message.
    with failing_mail(monkeypatch, env) as provider_failure:
        request_reset(env, EMAIL[CASEY])
    quoting = RuntimeError("boom")

    def quote_everything(self, message):
        handed.append(message)
        raise RuntimeError(f"{message.to} {message.subject} {message.text_body}") from quoting

    with monkeypatch.context() as m:
        m.setattr(type(env.sender), "send", quote_everything)
        request_verification(env, DANA)
    for message in (*provider_failure, *handed):
        raws.append(link_token(message, RESET_PATH if "reset" in message.template else VERIFY_PATH))
    return raws, [*env.messages(), *provider_failure, *handed]


def test_no_secret_in_any_log_record_or_stream(env: Env, monkeypatch, capsys):  # T-30
    with capture_all_logs() as records:
        raws, messages = _run_every_path(env, monkeypatch)
    out = capsys.readouterr()
    names = [r.getMessage() for r in records]
    # Positive controls: the capture saw the app's own logs, and every security event.
    assert any(r.name == "signalnest.request" for r in records), sorted(set(names))
    for event_name in (
        "security.password_reset.requested",
        "security.password_reset.completed",
        "security.email_verification.requested",
        "security.email_verification.completed",
        "mail.sent",
        "mail.send_failed",
        "security.password_reset.mail_failed",
        "security.email_verification.mail_failed",
        "security.session.revoked",
        "security.session.revoked_all",
        "security.password.changed",
    ):
        assert event_name in names, (event_name, sorted(set(names)))
    # AUTH2-C3: each token whose send failed was revoked.
    for model, user_id in ((PasswordResetToken, CASEY), (EmailVerificationToken, DANA)):
        [row] = env.witness.rows(model, model.user_id == user_id)
        assert row["revoked_at"] is not None and row["used_at"] is None
    secrets = _secrets(env, raws, messages)
    assert len(raws) >= 7 and messages
    for rec in records:
        rendered = _rendered(rec)
        for secret in secrets:
            assert secret not in rendered, (rec.name, rec.getMessage(), secret[:24])
    for stream in (out.out, out.err):
        for secret in secrets:
            assert secret not in stream, secret[:24]


def test_security_events_carry_only_static_fields(env: Env, monkeypatch):  # T-30
    with capture_all_logs() as records:
        _run_every_path(env, monkeypatch)
    allowed = {"outcome", "user_id", "error_class", "duration_ms"}
    for rec in records:
        if not re.match(r"^security\.", rec.getMessage()):
            continue
        fields = getattr(rec, "extra_fields", {}) or {}
        assert set(fields) <= allowed, (rec.getMessage(), set(fields) - allowed)
        if "user_id" in fields:
            assert fields["user_id"] in EMAIL  # an id, never an address


def test_the_memory_backend_prints_nothing(env: Env, capsys):
    mint_reset(env, ALICE)
    mint_verification(env, BOB)
    out = capsys.readouterr()
    assert out.out == "" and out.err == ""
    assert mail.get_mail_sender().backend == "memory"


# --------------------------------------------------------------------------- #
# 422 responses never echo the request (P6-UI-017 U17-S03; FD-U17-5 = A)
# --------------------------------------------------------------------------- #
#: Distinct synthetic markers, one set per test. The 7-character ones fail the 8-character
#: minimum; the rest pass every bound, so only the intended field fails. None contains a
#: character JSON escapes, so a verbatim echo would show in the raw body.
S03A_PASSWORD = "Zq!S3a~"
S03B_PASSWORD, S03B_TOKEN = "Zq!S3b~", "u17-s03b-reset-token-Hk2vQ9mWx4"
S03C_PASSWORD, S03C_TOKEN = "Zq!S3c~", "u17-s03c-invite-token-Lp7rT3nYz8"
S03D_PASSWORD, S03D_TOKEN = "u17-s03d-password-Wm5bJ2", "u17-s03d-token-Qe4sN8cVd1"
S03E_SECRET, S03E_TOKEN = "u17-s03e-sk_live-Rf6gU1hXa3", "u17-s03e-token-Ty8kP0dLs5"
S03F_PASSWORD = "Zq!S3f~"
S03G_PASSWORD, S03G_TOKEN = "u17-s03g-password-Cn3wE7", "u17-s03g-token-Bv5xM2qRj6"
S03H_COVERAGE, S03H_KIND = "u17-s03h-cover-Ga4", "u17-s03h-kind-Hs9"
S03H_MODE, S03H_TAG = "u17-s03h-mode-Pw7", "u17-s03h-tag-Jd2"
S03I_PASSWORD, S03I_LOGIN = "Zq!S3i~", "u17-s03i-login-password-Kf1"
S03I_TOKEN, S03I_SECRET = "u17-s03i-token-Nu6yA3", "u17-s03i-secret-Vo0zB8"

#: The only keys a public validation-error item may carry.
_ITEM_KEYS = {"type", "loc", "msg", "ctx"}


def _echoes(model: type[BaseModel], body: dict) -> str:
    """Pydantic's own unredacted error list for ``body``: the positive control."""
    with pytest.raises(ValidationError) as caught:
        model.model_validate(body)
    return json.dumps(caught.value.errors(include_url=False), default=str)


def _redacted(r, *markers: str) -> list[dict]:
    """Assert the unchanged 422 envelope, usable items, and no marker anywhere in the
    response (raw body, re-serialized body, headers); return ``details``."""
    assert r.status_code == 422, r.text
    payload = r.json()
    rendered = "\n".join([r.text, json.dumps(payload), json.dumps(dict(r.headers))])
    for marker in markers:
        assert marker not in rendered, marker
    assert set(payload) == {"error"}, payload
    error = payload["error"]
    assert set(error) == {"code", "message", "request_id", "details"}, error
    assert (error["code"], error["message"]) == ("validation_error", "Request validation failed")
    details = error["details"]
    assert isinstance(details, list) and details, details
    for item in details:
        assert set(item) <= _ITEM_KEYS and {"type", "loc", "msg"} <= set(item), item
        assert isinstance(item["loc"], list) and item["loc"][0] == "body", item
        assert isinstance(item["msg"], str) and item["msg"], item
    return details


def test_register_short_password_is_not_echoed(env: Env):  # U17-S03a
    body = {
        "email": "s03a@example.com",
        "full_name": "S03a",
        "password": S03A_PASSWORD,
        "organization_name": "S03a Org",
    }
    assert S03A_PASSWORD in _echoes(RegisterRequest, body)
    details = _redacted(post(env, REGISTER, body), S03A_PASSWORD)
    assert [d["loc"] for d in details] == [["body", "password"]]


def test_reset_confirm_short_password_echoes_neither_password_nor_token(env: Env):  # U17-S03b
    body = {"token": S03B_TOKEN, "new_password": S03B_PASSWORD}
    assert S03B_PASSWORD in _echoes(PasswordResetConfirmRequest, body)
    details = _redacted(post(env, RESET_CONFIRM, body), S03B_PASSWORD, S03B_TOKEN)
    assert [d["loc"] for d in details] == [["body", "new_password"]]


def test_invitation_register_short_password_is_not_echoed(env: Env):  # U17-S03c
    body = {"token": S03C_TOKEN, "full_name": "S03c", "password": S03C_PASSWORD}
    assert S03C_PASSWORD in _echoes(InvitationRegisterRequest, body)
    details = _redacted(post(env, INVITATION_REGISTER, body), S03C_PASSWORD, S03C_TOKEN)
    assert [d["loc"] for d in details] == [["body", "password"]]


#: For a ``missing`` field Pydantic's ``input`` is the WHOLE body, every credential in it.
MISSING_FIELD = {
    "login-without-email": (LOGIN, LoginRequest, {"password": S03D_PASSWORD}, "email"),
    "register-without-organization": (
        REGISTER,
        RegisterRequest,
        {"email": "s03d@example.com", "full_name": "S03d", "password": S03D_PASSWORD},
        "organization_name",
    ),
    "reset-confirm-without-password": (
        RESET_CONFIRM,
        PasswordResetConfirmRequest,
        {"token": S03D_TOKEN},
        "new_password",
    ),
    "reset-confirm-without-token": (
        RESET_CONFIRM,
        PasswordResetConfirmRequest,
        {"new_password": S03D_PASSWORD},
        "token",
    ),
    "invitation-register-without-name": (
        INVITATION_REGISTER,
        InvitationRegisterRequest,
        {"token": S03D_TOKEN, "password": S03D_PASSWORD},
        "full_name",
    ),
}


@pytest.mark.parametrize("case", list(MISSING_FIELD))
def test_a_missing_field_never_returns_the_body(env: Env, case: str):  # U17-S03d
    url, model, body, missing = MISSING_FIELD[case]
    markers = [v for v in body.values() if v in (S03D_PASSWORD, S03D_TOKEN)]
    unredacted = _echoes(model, body)
    assert markers and all(m in unredacted for m in markers), unredacted
    details = _redacted(post(env, url, body), *markers)
    [item] = [d for d in details if d["loc"] == ["body", missing]]
    assert (item["type"], item["msg"]) == ("missing", "Field required")


def test_an_unknown_field_value_is_not_echoed(env: Env):  # U17-S03e
    body = {"token": S03E_TOKEN, "new_password": NEW_PASSWORD, "client_secret": S03E_SECRET}
    assert S03E_SECRET in _echoes(PasswordResetConfirmRequest, body)
    details = _redacted(post(env, RESET_CONFIRM, body), S03E_SECRET, S03E_TOKEN, NEW_PASSWORD)
    # ``loc`` names the field -- the caller's own key -- so the client can say which one;
    # its value never comes back.
    assert details == [
        {
            "type": "extra_forbidden",
            "loc": ["body", "client_secret"],
            "msg": "Extra inputs are not permitted",
        }
    ]


def test_field_errors_stay_usable(env: Env):  # U17-S03f
    """Redaction removes the value, not the help. The web client (``humanizeFieldErrors``)
    reads exactly ``loc`` and ``msg``; the schema's own bound stays in ``ctx``."""
    body = {"token": S03B_TOKEN, "new_password": S03F_PASSWORD}
    assert _redacted(post(env, RESET_CONFIRM, body), S03F_PASSWORD) == [
        {
            "type": "string_too_short",
            "loc": ["body", "new_password"],
            "msg": "String should have at least 8 characters",
            "ctx": {"min_length": 8},
        }
    ]
    too_long = "L" * 129
    body = {"token": S03B_TOKEN, "new_password": too_long}
    assert _redacted(post(env, RESET_CONFIRM, body), too_long) == [
        {
            "type": "string_too_long",
            "loc": ["body", "new_password"],
            "msg": "String should have at most 128 characters",
            "ctx": {"max_length": 128},
        }
    ]
    # Several failures: one item per field, in schema order, each still labelled.
    body = {"email": "not-an-email", "full_name": "", "password": S03F_PASSWORD}
    details = _redacted(post(env, REGISTER, body), S03F_PASSWORD)
    assert [d["loc"] for d in details] == [
        ["body", "email"],
        ["body", "full_name"],
        ["body", "password"],
        ["body", "organization_name"],
    ]


def test_malformed_json_is_not_echoed(env: Env):  # U17-S03g
    active_rate_limiter()._hits.clear()
    doc = f'{{"token": "{S03G_TOKEN}", "new_password": "{S03G_PASSWORD}",'
    r = env.client.post(
        RESET_CONFIRM, content=doc.encode(), headers={"content-type": "application/json"}
    )
    details = _redacted(r, S03G_TOKEN, S03G_PASSWORD)
    assert [(d["type"], "ctx" in d) for d in details] == [("json_invalid", False)]


class _Bounded(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: str = Field(pattern=r"^(alpha|beta)$")
    mode: Literal["fast", "slow"]
    count: int = Field(ge=1, le=5)
    tags: list[str] = Field(max_length=2)


def test_request_derived_context_is_dropped():  # U17-S03h
    """``ctx`` keeps the schema's bounds only. Through the product's own handler on a
    stand-alone app: a ``field_validator``'s exception (formerly an unserializable object
    in ``ctx`` -- a 500) and a list's ``actual_length`` are dropped; the bounds stay."""
    probe = FastAPI()
    register_exception_handlers(probe)

    @probe.post("/coverage")
    def _coverage(body: GeoCoverageBase) -> dict:
        return {}

    @probe.post("/bounded")
    def _bounded(body: _Bounded) -> dict:
        return {}

    client = TestClient(probe, raise_server_exceptions=False)
    body = {"coverage_type": S03H_COVERAGE}
    assert S03H_COVERAGE in _echoes(GeoCoverageBase, body)
    details = _redacted(client.post("/coverage", json=body), S03H_COVERAGE)
    assert [(d["type"], d["loc"], "ctx" in d) for d in details] == [
        ("value_error", ["body", "coverage_type"], False)
    ]
    body = {"kind": S03H_KIND, "mode": S03H_MODE, "count": 99, "tags": ["a", "b", S03H_TAG]}
    unredacted = _echoes(_Bounded, body)
    for echoed in (S03H_KIND, S03H_MODE, S03H_TAG, "actual_length"):
        assert echoed in unredacted, echoed
    details = _redacted(client.post("/bounded", json=body), S03H_KIND, S03H_MODE, S03H_TAG)
    assert {tuple(d["loc"]): d.get("ctx") for d in details} == {
        ("body", "kind"): {"pattern": "^(alpha|beta)$"},
        ("body", "mode"): {"expected": "'fast' or 'slow'"},
        ("body", "count"): {"le": 5},
        ("body", "tags"): {"max_length": 2},
    }


def test_redacted_values_are_not_logged(env: Env, capsys):  # U17-S03i
    bodies = [
        (REGISTER, {"email": "s03i@example.com", "full_name": "S", "password": S03I_PASSWORD}),
        (LOGIN, {"password": S03I_LOGIN}),
        (RESET_CONFIRM, {"token": S03I_TOKEN}),
        (RESET_CONFIRM, {"token": S03I_TOKEN, "new_password": S03I_LOGIN, "x": S03I_SECRET}),
    ]
    with capture_all_logs() as records:
        statuses = [post(env, url, body).status_code for url, body in bodies]
    out = capsys.readouterr()
    assert statuses == [422] * len(bodies)
    # Positive control: the capture saw every one of those 422s in the request log.
    seen = [
        (getattr(r, "extra_fields", {}) or {}).get("status_code")
        for r in records
        if r.name == "signalnest.request"
    ]
    assert seen.count(422) == len(bodies), seen
    markers = (S03I_PASSWORD, S03I_LOGIN, S03I_TOKEN, S03I_SECRET)
    for rec in records:
        rendered = _rendered(rec)
        for marker in markers:
            assert marker not in rendered, (rec.name, rec.getMessage(), marker)
    for stream in (out.out, out.err):
        for marker in markers:
            assert marker not in stream, marker


# --- The password change's own 422s (P6-UI-017) ------------------------------------------
#: Distinct markers again. The route needs a live bearer: authentication runs before body
#: validation, so each case below is a signed-in CAROL, whose password must stay unchanged.
S03J_CURRENT = ("u17-s03j-current-" + "c" * 128)[:129]  # one over the 128 maximum
S03J_NEW = "u17-s03j-new-password-Xy4"
S03K_CURRENT, S03K_NEW = "u17-s03k-current-Rt5", "Zq!S3k~"
S03L_CURRENT, S03L_NEW = "u17-s03l-current-Ub8", "u17-s03l-new-password-Wc3"
S03M_CURRENT, S03M_NEW = "u17-s03m-current-Ha2", "u17-s03m-new-password-Ke7"
S03M_CONFIRM = "u17-s03m-confirm-Zx6"
S03N_CURRENT, S03N_NEW = "u17-s03n-current-Mo4", "u17-s03n-new-password-Fi1"
S03O_WRONG, S03O_NEW = "u17-s03o-wrong-current-Pq1", "u17-s03o-new-password-Ld9"


def _change(env: Env, body: dict):
    return post(env, CHANGE_PASSWORD, body, user_id=CAROL)


def test_change_password_invalid_current_password_is_not_echoed(env: Env):  # U17-S03j
    body = {"current_password": S03J_CURRENT, "new_password": S03J_NEW}
    assert len(S03J_CURRENT) == 129 and S03J_CURRENT in _echoes(ChangePasswordRequest, body)
    # The prefix too: a truncated echo must not slip past a whole-value search.
    details = _redacted(_change(env, body), S03J_CURRENT, S03J_CURRENT[:24], S03J_NEW)
    assert details == [
        {
            "type": "string_too_long",
            "loc": ["body", "current_password"],
            "msg": "String should have at most 128 characters",
            "ctx": {"max_length": 128},
        }
    ]
    assert env.witness.user(CAROL)["hashed_password"] == OLD_HASH


def test_change_password_invalid_new_password_is_not_echoed(env: Env):  # U17-S03k
    body = {"current_password": S03K_CURRENT, "new_password": S03K_NEW}
    assert S03K_NEW in _echoes(ChangePasswordRequest, body)
    details = _redacted(_change(env, body), S03K_NEW, S03K_CURRENT)
    assert details == [
        {
            "type": "string_too_short",
            "loc": ["body", "new_password"],
            "msg": "String should have at least 8 characters",
            "ctx": {"min_length": 8},
        }
    ]
    assert env.witness.user(CAROL)["hashed_password"] == OLD_HASH


CHANGE_MISSING = {
    "without-new-password": ({"current_password": S03L_CURRENT}, "new_password"),
    "without-current-password": ({"new_password": S03L_NEW}, "current_password"),
}


@pytest.mark.parametrize("case", list(CHANGE_MISSING))
def test_change_password_missing_field_never_returns_the_body(env: Env, case: str):  # U17-S03l
    body, missing = CHANGE_MISSING[case]
    markers = list(body.values())
    unredacted = _echoes(ChangePasswordRequest, body)
    assert all(m in unredacted for m in markers), unredacted  # ``input`` is the whole body
    assert _redacted(_change(env, body), *markers) == [
        {"type": "missing", "loc": ["body", missing], "msg": "Field required"}
    ]
    assert env.witness.user(CAROL)["hashed_password"] == OLD_HASH


def test_change_password_extra_field_value_is_not_echoed(env: Env):  # U17-S03m
    """``confirm_new_password`` is the dialog's own field and is never sent; sent anyway,
    it is an unknown field whose value does not come back."""
    body = {
        "current_password": S03M_CURRENT,
        "new_password": S03M_NEW,
        "confirm_new_password": S03M_CONFIRM,
    }
    assert S03M_CONFIRM in _echoes(ChangePasswordRequest, body)
    details = _redacted(_change(env, body), S03M_CONFIRM, S03M_CURRENT, S03M_NEW)
    assert details == [
        {
            "type": "extra_forbidden",
            "loc": ["body", "confirm_new_password"],
            "msg": "Extra inputs are not permitted",
        }
    ]
    assert env.witness.user(CAROL)["hashed_password"] == OLD_HASH


def test_change_password_malformed_json_is_not_echoed(env: Env):  # U17-S03n
    """No Pydantic model runs on a document that does not parse, so the positive instance
    is the parser's own exception: it carries the whole document, which FastAPI keeps as
    ``RequestValidationError.body`` -- the handler must not serialize it."""
    doc = f'{{"current_password": "{S03N_CURRENT}", "new_password": "{S03N_NEW}"'
    with pytest.raises(json.JSONDecodeError) as caught:
        json.loads(doc)
    assert S03N_CURRENT in caught.value.doc and S03N_NEW in caught.value.doc
    token = bearer(env, CAROL)
    active_rate_limiter()._hits.clear()
    r = env.client.post(
        CHANGE_PASSWORD,
        content=doc.encode(),
        headers={"content-type": "application/json", "Authorization": f"Bearer {token}"},
    )
    details = _redacted(r, S03N_CURRENT, S03N_NEW)
    assert [(d["type"], "ctx" in d) for d in details] == [("json_invalid", False)]
    assert env.witness.user(CAROL)["hashed_password"] == OLD_HASH


def test_change_password_refusals_echo_no_password_and_no_hash(env: Env):  # U17-S03o
    """The route's own 422s are static domain errors: code and message only, no details."""
    token = bearer(env, CAROL)
    wrong = post(
        env,
        CHANGE_PASSWORD,
        {"current_password": S03O_WRONG, "new_password": S03O_NEW},
        token=token,
    )
    same = post(
        env,
        CHANGE_PASSWORD,
        {"current_password": OLD_PASSWORD, "new_password": OLD_PASSWORD},
        token=token,
    )
    for r, code, message, markers in (
        (
            wrong,
            "current_password_incorrect",
            "The current password is incorrect.",
            (S03O_WRONG, S03O_NEW, OLD_PASSWORD, OLD_HASH),
        ),
        (
            same,
            "password_unchanged",
            "Choose a password different from your current one.",
            (OLD_PASSWORD, OLD_HASH),
        ),
    ):
        assert r.status_code == 422, r.text
        rendered = "\n".join([r.text, json.dumps(dict(r.headers))])
        for marker in markers:
            assert marker not in rendered, marker
        error = r.json()["error"]
        assert error == {"code": code, "message": message, "request_id": error["request_id"]}
    assert env.witness.user(CAROL)["hashed_password"] == OLD_HASH


# --------------------------------------------------------------------------- #
OPERATIONS = {
    RESET_REQUEST: ("PasswordResetRequest", {"email"}, {"email"}),
    RESET_CONFIRM: (
        "PasswordResetConfirmRequest",
        {"token", "new_password"},
        {"token", "new_password"},
    ),
    VERIFY_REQUEST: ("EmailVerificationRequest", set(), set()),
    VERIFY_CONFIRM: ("EmailVerificationConfirmRequest", {"token"}, {"token"}),
    CHANGE_PASSWORD: (
        "ChangePasswordRequest",
        {"current_password", "new_password"},
        {"current_password", "new_password"},
    ),
}
AUTHENTICATED = {VERIFY_REQUEST, VERIFY_CONFIRM, CHANGE_PASSWORD}


@pytest.fixture(scope="module")
def doc() -> dict:
    return app.openapi()


class TestOpenApiContract:
    def test_operation_count(self, doc: dict):  # T-31
        count = sum(
            1
            for item in doc["paths"].values()
            for method in item
            if method in {"get", "post", "put", "delete", "patch"}
        )
        # 105 -> 106 (P6-UI-017): POST /auth/password/change, pinned below in OPERATIONS.
        assert count == 106

    def test_no_token_in_any_path_query_or_header_anywhere(self, doc: dict):
        for path, item in doc["paths"].items():
            assert "token" not in path.lower(), path
            for op in item.values():
                for param in op.get("parameters", []):
                    assert "token" not in param["name"].lower(), (path, param["name"])

    @pytest.mark.parametrize("path", list(OPERATIONS))
    def test_each_operation_takes_a_closed_body_and_answers_204(self, doc: dict, path: str):
        op = doc["paths"][path]["post"]
        params = op.get("parameters", [])
        if path in AUTHENTICATED:
            assert [(p["name"].lower(), p["in"]) for p in params] == [("authorization", "header")]
        else:
            assert params == [], "a public operation must take no header, path or query input"
        name, properties, required = OPERATIONS[path]
        ref = op["requestBody"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.rsplit("/", 1)[-1] == name
        schema = doc["components"]["schemas"][name]
        assert schema.get("additionalProperties") is False
        assert set(schema.get("properties", {})) == properties
        assert set(schema.get("required", [])) == required
        assert set(op["responses"]) - {"204", "422"} == set(), op["responses"]
        assert "content" not in op["responses"]["204"]

    def test_bounds_are_published(self, doc: dict):
        schemas = doc["components"]["schemas"]
        for name in ("PasswordResetConfirmRequest", "EmailVerificationConfirmRequest"):
            token = schemas[name]["properties"]["token"]
            assert (token["minLength"], token["maxLength"]) == (1, 128)
        password = schemas["PasswordResetConfirmRequest"]["properties"]["new_password"]
        assert (password["minLength"], password["maxLength"]) == (8, 128)
        change = schemas["ChangePasswordRequest"]["properties"]
        for field, bounds in (("current_password", (1, 128)), ("new_password", (8, 128))):
            assert (change[field]["minLength"], change[field]["maxLength"]) == bounds, field
        assert schemas["PasswordResetRequest"]["properties"]["email"]["format"] == "email"

    def test_user_out_reports_verification_status_only(self, doc: dict):
        user = doc["components"]["schemas"]["UserOut"]["properties"]
        assert set(user) == {"id", "email", "full_name", "is_operator", "email_verified"}
        assert user["email_verified"]["type"] == "boolean"
        for schema in doc["components"]["schemas"].values():
            props = set(schema.get("properties", {}))
            assert not props & {"email_verified_at", "auth_epoch", "token_hash"}, props
