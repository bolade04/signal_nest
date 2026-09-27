"""6B-4A: the mail transport seam and its settings (FD-1, FD-2, FD-8).

``app.infra.mail`` sends one ``EmailMessage`` through the backend chosen by settings:

* ``memory`` -- a process-wide outbox the tests read (development and test only);
* ``console`` -- a clearly marked block on stdout, never through logging (development only);
* ``ses`` -- AWS SES v2 via boto3 on the task role (no SMTP, no stored secret).

The SES adapter is exercised against a REAL ``sesv2`` client with botocore's ``Stubber`` or
an event hook raising the transport exception -- every socket ``connect`` is refused for the
duration, so a stub that failed to intercept would fail the test rather than reach AWS.
Provider failures surface only as ``MailSendError`` with a static ``error_class``; nothing of
the message or the provider's own text is carried. ``send_mail`` logs exactly the allowed
fields and re-raises.

Settings: staging/production (outside migration mode) require ``mail_backend="ses"``, an
https origin-only ``public_web_origin`` that is not the localhost default, a valid
``mail_from_address`` and ``mail_ses_region``; ``console`` is development-only and ``memory``
development/test-only; origin shape, lifetimes, cooldown, cap and timeout are bounded; the
From name admits no CR/LF.
"""

from __future__ import annotations

import json
import logging
import socket
from email.utils import formataddr

import boto3
import pytest
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import (
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)
from botocore.stub import Stubber
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.infra import mail
from app.infra.mail import (
    ConsoleMailSender,
    EmailMessage,
    MailSendError,
    MemoryMailSender,
    SesMailSender,
    send_mail,
)

REGION = "us-east-1"
ENDPOINT = f"https://email.{REGION}.amazonaws.com"
FROM = "no-reply@example.com"  # email_validator refuses special-use TLDs (.test)
STATIC_CLASSES = {
    "timeout",
    "throttled",
    "rejected",
    "provider_error",
    "not_configured",
    "internal_error",
}
ALLOWED_LOG_FIELDS = {
    "template",
    "template_version",
    "backend",
    "outcome",
    "error_class",
    "provider_message_id",
}

MESSAGE = EmailMessage(
    to="alice@example.com",
    subject="Reset your SignalNest password",
    text_body="Plain body with https://app.signalnest.test/reset-password#token=SECRET-RAW",
    html_body='<p><a href="https://app.signalnest.test/reset-password#token=SECRET-RAW">x</a></p>',
    template="password_reset",
    template_version="1",
)
#: Nothing of the message may appear in an error or a log record.
MESSAGE_PARTS = (MESSAGE.to, MESSAGE.subject, MESSAGE.text_body, MESSAGE.html_body, "SECRET-RAW")


@pytest.fixture
def no_network(monkeypatch):
    def refuse(*_args, **_kwargs):
        raise AssertionError("a socket connect was attempted")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


@pytest.fixture
def memory_backend(monkeypatch):
    monkeypatch.setattr(get_settings(), "mail_backend", "memory")
    mail.reset_mail_sender()
    sender = mail.get_mail_sender()
    sender.clear()
    yield sender
    sender.clear()
    mail.reset_mail_sender()


def _client():
    # Unsigned: the Stubber and the before-send hook intercept before any request is signed,
    # so the client needs no credentials.
    return boto3.client(
        "sesv2",
        region_name=REGION,
        config=Config(signature_version=UNSIGNED, retries={"mode": "standard", "max_attempts": 1}),
    )


def _ses(client, **overrides) -> SesMailSender:
    kwargs = {
        "client": client,
        "region": REGION,
        "from_address": FROM,
        "from_name": "SignalNest",
        "reply_to": None,
        "configuration_set": None,
        "timeout_seconds": 5.0,
    }
    kwargs.update(overrides)
    return SesMailSender(**kwargs)


def _expected_request(**extra) -> dict:
    params = {
        "FromEmailAddress": formataddr(("SignalNest", FROM)),
        "Destination": {"ToAddresses": [MESSAGE.to]},
        "Content": {
            "Simple": {
                "Subject": {"Data": MESSAGE.subject, "Charset": "UTF-8"},
                "Body": {
                    "Text": {"Data": MESSAGE.text_body, "Charset": "UTF-8"},
                    "Html": {"Data": MESSAGE.html_body, "Charset": "UTF-8"},
                },
            }
        },
    }
    params.update(extra)
    return params


def _assert_carries_nothing(exc: MailSendError) -> None:
    rendered = repr(exc) + str(exc) + json.dumps(exc.__dict__, default=str) + repr(exc.args)
    for part in (*MESSAGE_PARTS, "provider said"):
        assert part not in rendered, part
    assert exc.error_class in STATIC_CLASSES


# --------------------------------------------------------------------------- #
class TestMemoryAndConsole:
    def test_memory_sender_records_and_clears(self):
        sender = MemoryMailSender()
        assert sender.backend == "memory"
        sender.send(MESSAGE)
        assert sender.outbox == [MESSAGE]
        sender.clear()
        assert sender.outbox == []

    def test_the_memory_backend_is_one_process_wide_outbox(self, memory_backend):
        assert mail.get_mail_sender() is memory_backend
        send_mail(MESSAGE)
        assert mail.get_mail_sender().outbox == [MESSAGE]

    def test_console_writes_a_marked_block_to_stdout_and_nothing_to_logging(self, capsys, caplog):
        caplog.set_level(logging.DEBUG)
        sender = ConsoleMailSender(environment="development")
        assert sender.backend == "console"
        sender.send(MESSAGE)
        out = capsys.readouterr().out
        assert MESSAGE.to in out and MESSAGE.subject in out and "SECRET-RAW" in out
        for rec in caplog.records:
            assert "SECRET-RAW" not in rec.getMessage() + json.dumps(rec.__dict__, default=str)

    @pytest.mark.parametrize("environment", ["test", "staging", "production"])
    def test_console_refuses_to_exist_outside_development(self, environment: str):
        with pytest.raises(MailSendError) as caught:
            ConsoleMailSender(environment=environment)
        assert caught.value.error_class == "not_configured"


# --------------------------------------------------------------------------- #
class TestSesAdapter:
    def test_success_sends_exactly_the_contracted_request(self, no_network):
        client = _client()
        with Stubber(client) as stub:
            stub.add_response("send_email", {"MessageId": "mid-1"}, _expected_request())
            assert _ses(client).send(MESSAGE) == "mid-1"
            stub.assert_no_pending_responses()

    def test_reply_to_and_configuration_set_are_passed_when_set(self, no_network):
        client = _client()
        expected = _expected_request(
            ReplyToAddresses=["support@example.com"], ConfigurationSetName="auth-mail"
        )
        with Stubber(client) as stub:
            stub.add_response("send_email", {"MessageId": "mid-2"}, expected)
            sender = _ses(client, reply_to="support@example.com", configuration_set="auth-mail")
            assert sender.send(MESSAGE) == "mid-2"
            stub.assert_no_pending_responses()

    @pytest.mark.parametrize(
        ("error_code", "status", "expected"),
        [
            ("TooManyRequestsException", 429, "throttled"),
            ("MessageRejected", 400, "rejected"),
            ("InternalFailure", 500, "provider_error"),
            ("SomethingNobodyModelled", 400, "provider_error"),
        ],
    )
    def test_provider_errors_map_to_a_static_class(
        self, no_network, error_code: str, status: int, expected: str
    ):
        client = _client()
        with Stubber(client) as stub:
            stub.add_client_error(
                "send_email",
                service_error_code=error_code,
                service_message=f"provider said: {MESSAGE.to} {MESSAGE.subject}",
                http_status_code=status,
            )
            with pytest.raises(MailSendError) as caught:
                _ses(client).send(MESSAGE)
        assert caught.value.error_class == expected
        _assert_carries_nothing(caught.value)
        # The provider exception (its message quotes the recipient) is not chained.
        assert caught.value.__cause__ is None and caught.value.__context__ is None

    def test_every_modelled_send_email_error_maps_to_a_static_class(self, no_network):
        client = _client()
        operation = client.meta.service_model.operation_model("SendEmail")
        codes = [shape.name for shape in operation.error_shapes]
        assert codes, "the sesv2 model lists no SendEmail errors"
        for error_code in codes:
            with Stubber(client) as stub:
                stub.add_client_error(
                    "send_email",
                    service_error_code=error_code,
                    service_message=f"provider said: {MESSAGE.to}",
                    http_status_code=400,
                )
                with pytest.raises(MailSendError) as caught:
                    _ses(client).send(MESSAGE)
            _assert_carries_nothing(caught.value)

    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (ReadTimeoutError(endpoint_url=ENDPOINT), {"timeout"}),
            (ConnectTimeoutError(endpoint_url=ENDPOINT), {"timeout"}),
            (EndpointConnectionError(endpoint_url=ENDPOINT), STATIC_CLASSES),
        ],
        ids=["read-timeout", "connect-timeout", "endpoint-connection"],
    )
    def test_transport_failures_map_to_a_static_class(self, no_network, exc, expected):
        client = _client()

        def raise_transport_error(**_kwargs):
            raise exc

        client.meta.events.register("before-send.sesv2.SendEmail", raise_transport_error)
        with pytest.raises(MailSendError) as caught:
            _ses(client).send(MESSAGE)
        assert caught.value.error_class in expected
        _assert_carries_nothing(caught.value)

    def test_the_client_is_built_lazily_with_bounded_timeouts_and_retries(self, monkeypatch):
        built: list[tuple] = []

        class Recorded:
            def send_email(self, **_params):
                return {"MessageId": "mid-lazy"}

        def fake_client(service, **kwargs):
            built.append((service, kwargs))
            return Recorded()

        monkeypatch.setattr(boto3, "client", fake_client)
        sender = _ses(None, timeout_seconds=7.5)
        assert built == [], "the client must not be built before the first send"
        assert sender.send(MESSAGE) == "mid-lazy"
        [(service, kwargs)] = built
        assert (service, kwargs["region_name"]) == ("sesv2", REGION)
        config = kwargs["config"]
        assert (config.connect_timeout, config.read_timeout) == (7.5, 7.5)
        assert config.retries == {"mode": "standard", "max_attempts": 3}
        # No credentials are passed: the task role's ambient chain supplies them (FD-1).
        assert not {"aws_access_key_id", "aws_secret_access_key", "aws_session_token"} & set(kwargs)

    def test_mail_send_error_carries_only_its_class(self):
        exc = MailSendError("timeout")
        assert exc.error_class == "timeout"
        _assert_carries_nothing(exc)


# --------------------------------------------------------------------------- #
class TestSendMail:
    def test_success_logs_only_the_allowed_fields(self, memory_backend, caplog):
        caplog.set_level(logging.DEBUG)
        send_mail(MESSAGE)
        [rec] = [r for r in caplog.records if r.getMessage() == "mail.sent"]
        fields = rec.extra_fields
        assert set(fields) <= ALLOWED_LOG_FIELDS, set(fields) - ALLOWED_LOG_FIELDS
        assert (fields["template"], fields["template_version"], fields["backend"]) == (
            "password_reset",
            "1",
            "memory",
        )
        for r in caplog.records:
            rendered = r.getMessage() + json.dumps(r.__dict__, default=str)
            for part in MESSAGE_PARTS:
                assert part not in rendered, (r.name, part)

    def test_an_unexpected_sender_defect_is_one_static_failure(
        self, memory_backend, caplog, monkeypatch
    ):
        """An arbitrary exception -- here one quoting the whole message -- is dropped."""
        caplog.set_level(logging.DEBUG)

        def defective(self, message):
            raise RuntimeError(f"{message.to} {message.subject} {message.text_body}")

        monkeypatch.setattr(type(memory_backend), "send", defective)
        with pytest.raises(MailSendError) as caught:
            send_mail(MESSAGE)
        _assert_carries_nothing(caught.value)
        assert caught.value.__cause__ is None and caught.value.__context__ is None
        [rec] = [r for r in caplog.records if r.getMessage() == "mail.send_failed"]
        assert set(rec.extra_fields) <= ALLOWED_LOG_FIELDS
        for r in caplog.records:
            rendered = r.getMessage() + json.dumps(r.__dict__, default=str)
            for part in MESSAGE_PARTS:
                assert part not in rendered, (r.name, part)

    def test_failure_logs_the_class_and_re_raises(self, memory_backend, caplog, monkeypatch):
        caplog.set_level(logging.DEBUG)

        def throttled(self, message):
            raise MailSendError("throttled")

        monkeypatch.setattr(type(memory_backend), "send", throttled)
        with pytest.raises(MailSendError) as caught:
            send_mail(MESSAGE)
        assert caught.value.error_class == "throttled"
        [rec] = [r for r in caplog.records if r.getMessage() == "mail.send_failed"]
        assert set(rec.extra_fields) <= ALLOWED_LOG_FIELDS
        assert rec.extra_fields["error_class"] == "throttled"
        for r in caplog.records:
            rendered = r.getMessage() + json.dumps(r.__dict__, default=str)
            for part in MESSAGE_PARTS:
                assert part not in rendered, (r.name, part)


# --------------------------------------------------------------------------- #
class TestBackendSelection:
    @pytest.mark.parametrize(
        ("environment", "backend", "expected"),
        [
            ("development", None, "console"),
            ("test", None, "memory"),
            ("development", "memory", "memory"),
            ("test", "memory", "memory"),
        ],
    )
    def test_the_sender_follows_settings(self, monkeypatch, environment, backend, expected):
        settings = get_settings()
        monkeypatch.setattr(settings, "environment", environment)
        monkeypatch.setattr(settings, "mail_backend", backend)
        mail.reset_mail_sender()
        try:
            assert settings.effective_mail_backend == expected
            assert mail.get_mail_sender().backend == expected
        finally:
            mail.reset_mail_sender()

    def test_ses_is_selected_without_contacting_aws(self, monkeypatch, no_network):
        # Should a client be built eagerly, keep the credential chain off the network too.
        monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
        monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
        settings = get_settings()
        for name, value in {
            "mail_backend": "ses",
            "mail_ses_region": REGION,
            "mail_from_address": FROM,
        }.items():
            monkeypatch.setattr(settings, name, value)
        mail.reset_mail_sender()
        try:
            sender = mail.get_mail_sender()
            assert isinstance(sender, SesMailSender) and sender.backend == "ses"
        finally:
            mail.reset_mail_sender()


# --------------------------------------------------------------------------- #
_MAIL_UNSET = {
    "mail_backend": None,
    "public_web_origin": "http://localhost:5173",
    "mail_from_address": None,
    "mail_reply_to_address": None,
    "mail_support_contact": None,
    "mail_ses_region": None,
    "mail_ses_configuration_set": None,
}
_PRODUCTION_STACK = {
    "app_mode": "full",
    "database_url": "postgresql+psycopg://u:p@db.invalid:5432/d",
    "queue_backend": "redis",
    "cache_backend": "redis",
    "redis_url": "redis://unused.invalid:6379/0",
    "storage_backend": "s3",
    "s3_bucket": "unused",
    "vector_backend": "pgvector",
}
_VALID_LIVE_MAIL = {
    "mail_backend": "ses",
    "public_web_origin": "https://app.signalnest.test",
    "mail_from_address": FROM,
    "mail_ses_region": REGION,
}


def _settings(environment: str, **overrides) -> Settings:
    """Mail fields always passed explicitly, so ambient MAIL_* variables cannot decide."""
    kwargs: dict = {"_env_file": None, "environment": environment, **_MAIL_UNSET}
    if environment in ("staging", "production"):
        kwargs |= {"secret_key": "x" * 64, "llm_provider": "openai", "llm_api_key": "k" * 20}
        kwargs |= _VALID_LIVE_MAIL
    if environment == "production":
        kwargs |= _PRODUCTION_STACK
    kwargs.update(overrides)
    return Settings(**kwargs)


def _rejected(environment: str, needle: str, **overrides) -> None:
    with pytest.raises(ValidationError) as caught:
        _settings(environment, **overrides)
    assert needle in str(caught.value), str(caught.value)


LIVE = ["staging", "production"]


class TestMailSettings:
    def test_defaults_match_the_founder_decisions(self):
        s = Settings(_env_file=None)
        assert (
            s.password_reset_token_ttl_minutes,
            s.email_verification_token_ttl_hours,
            s.auth_mail_cooldown_seconds,
            s.auth_mail_daily_cap,
        ) == (60, 48, 120, 5)
        assert (s.mail_from_name, s.mail_send_timeout_seconds) == ("SignalNest", 10.0)
        assert Settings.model_fields["mail_backend"].default is None
        assert Settings.model_fields["public_web_origin"].default == "http://localhost:5173"
        for name in ("mail_from_address", "mail_support_contact", "mail_ses_region"):
            assert Settings.model_fields[name].default is None, name  # FD-2: no real values

    @pytest.mark.parametrize("environment", LIVE)
    def test_a_complete_live_configuration_is_accepted(self, environment: str):
        s = _settings(environment)
        assert s.effective_mail_backend == "ses"

    @pytest.mark.parametrize("environment", LIVE)
    @pytest.mark.parametrize("backend", ["memory", "console", None])
    def test_live_environments_require_ses(self, environment: str, backend):
        _rejected(environment, "mail_backend", mail_backend=backend)

    @pytest.mark.parametrize("environment", LIVE)
    @pytest.mark.parametrize(
        "origin",
        ["http://localhost:5173", "http://app.signalnest.test"],
        ids=["localhost-default", "plain-http"],
    )
    def test_live_environments_require_an_https_origin(self, environment: str, origin: str):
        _rejected(environment, "public_web_origin", public_web_origin=origin)

    @pytest.mark.parametrize("environment", LIVE)
    @pytest.mark.parametrize("address", [None, "not-an-address"], ids=["missing", "invalid"])
    def test_live_environments_require_a_from_address(self, environment: str, address):
        _rejected(environment, "mail_from_address", mail_from_address=address)

    @pytest.mark.parametrize("environment", LIVE)
    def test_live_environments_require_a_region(self, environment: str):
        _rejected(environment, "mail_ses_region", mail_ses_region=None)

    def test_migration_mode_needs_no_mail_configuration(self):
        s = Settings(
            _env_file=None,
            environment="staging",
            migration_mode=True,
            database_url="postgresql+psycopg://u:p@db.invalid:5432/d",
            **_MAIL_UNSET,
        )
        assert s.migration_mode is True

    def test_console_is_development_only(self):
        assert _settings("development", mail_backend="console").effective_mail_backend == (
            "console"
        )
        _rejected("test", "mail_backend", mail_backend="console")

    @pytest.mark.parametrize("environment", ["development", "test"])
    def test_memory_is_allowed_in_development_and_test(self, environment: str):
        assert _settings(environment, mail_backend="memory").effective_mail_backend == "memory"

    @pytest.mark.parametrize(
        ("environment", "expected"), [("development", "console"), ("test", "memory")]
    )
    def test_the_unset_backend_resolves_by_environment(self, environment: str, expected: str):
        assert _settings(environment).effective_mail_backend == expected

    @pytest.mark.parametrize(
        "origin",
        [
            "https://user:pass@app.signalnest.test",
            "https://app.signalnest.test/app",
            "https://app.signalnest.test?next=x",
            "https://app.signalnest.test#frag",
            "ftp://app.signalnest.test",
            "javascript:alert(1)",
            "app.signalnest.test",
            "https://",
        ],
        ids=["userinfo", "path", "query", "fragment", "ftp", "javascript", "no-scheme", "no-host"],
    )
    def test_the_origin_must_be_a_bare_origin(self, origin: str):
        _rejected("development", "public_web_origin", public_web_origin=origin)

    @pytest.mark.parametrize(
        "origin",
        [
            "http://localhost:5173",
            "https://app.signalnest.test",
            "https://app.signalnest.test:8443",
        ],
    )
    def test_bare_origins_are_accepted(self, origin: str):
        assert _settings("development", public_web_origin=origin).public_web_origin == origin

    @pytest.mark.parametrize(
        "field",
        [
            "password_reset_token_ttl_minutes",
            "email_verification_token_ttl_hours",
            "auth_mail_cooldown_seconds",
            "auth_mail_daily_cap",
        ],
    )
    def test_lifetimes_cooldown_and_cap_are_at_least_one(self, field: str):
        _rejected("development", field, **{field: 0})
        assert getattr(_settings("development", **{field: 1}), field) == 1

    @pytest.mark.parametrize("timeout", [0, -1.0])
    def test_the_send_timeout_is_positive(self, timeout: float):
        _rejected("development", "mail_send_timeout_seconds", mail_send_timeout_seconds=timeout)

    @pytest.mark.parametrize(
        "name", ["SignalNest\r\nBcc: x@evil.example", "SignalNest\nX: y", "Signal\rNest"]
    )
    def test_the_from_name_admits_no_line_breaks(self, name: str):
        _rejected("development", "mail_from_name", mail_from_name=name)
