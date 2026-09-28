"""6B-4A: nothing secret reaches a log, stdout, an error, or the API contract.

**Logs.** Every record from every logger is captured -- a handler on the root logger at
DEBUG, and the same handler attached to every logger that does not propagate (uvicorn's are
the usual ones) -- while all four operations run through their success AND failure paths:
issued, unknown, inactive and cooling-down reset requests; a completed, a replayed, an
unknown and a refused-password confirm; issued, cooling-down and already-verified
verification requests; a wrong-account, an unknown and a completed verification confirm;
and two background sends that fail, one with a ``MailSendError`` and one with an arbitrary
exception whose message quotes the whole email; and the two P6-AUTH-4 session revocations
(a logout, the same token's refused repeat, and a logout-all). Each record is rendered three
ways (its message, its full ``__dict__``, and the production JSON formatter's output with any
traceback) and must contain no raw token, no digest, no link, no email address, no
password, no mail body, no access token and no session id. Positive controls prove the
capture really saw the request log, every security event, the mail events and both failure
events -- and that the failed tokens were revoked (AUTH2-C3).

**OpenAPI.** The four operations take the token only in a JSON body that forbids unknown
fields; no operation anywhere has a token in its path, query or headers; the two public
operations have no parameters at all (no bearer dependency), the two verification
operations only the ``authorization`` header; each answers 204 with no body. 105 operations.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from app.auth.models import EmailVerificationToken, PasswordResetToken
from app.core.logging import JsonFormatter
from app.infra import mail
from app.main import app
from app.tests._auth2_support import (
    ALICE,
    BOB,
    CAROL,
    CASEY,
    DANA,
    EMAIL,
    INACTIVE,
    LOGOUT,
    LOGOUT_ALL,
    NEW_PASSWORD,
    OLD_PASSWORD,
    RESET_CONFIRM,
    RESET_PATH,
    RESET_REQUEST,
    UNKNOWN_EMAIL,
    VERIFY_CONFIRM,
    VERIFY_PATH,
    VERIFY_REQUEST,
    Env,
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
OPERATIONS = {
    RESET_REQUEST: ("PasswordResetRequest", {"email"}, {"email"}),
    RESET_CONFIRM: (
        "PasswordResetConfirmRequest",
        {"token", "new_password"},
        {"token", "new_password"},
    ),
    VERIFY_REQUEST: ("EmailVerificationRequest", set(), set()),
    VERIFY_CONFIRM: ("EmailVerificationConfirmRequest", {"token"}, {"token"}),
}
AUTHENTICATED = {VERIFY_REQUEST, VERIFY_CONFIRM}


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
        assert count == 105

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
        assert schemas["PasswordResetRequest"]["properties"]["email"]["format"] == "email"

    def test_user_out_reports_verification_status_only(self, doc: dict):
        user = doc["components"]["schemas"]["UserOut"]["properties"]
        assert set(user) == {"id", "email", "full_name", "is_operator", "email_verified"}
        assert user["email_verified"]["type"] == "boolean"
        for schema in doc["components"]["schemas"].values():
            props = set(schema.get("properties", {}))
            assert not props & {"email_verified_at", "auth_epoch", "token_hash"}, props
