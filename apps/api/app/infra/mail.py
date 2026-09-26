"""Transactional mail delivery for account email (password reset, verification).

One seam, three backends selected by ``Settings.effective_mail_backend``:

* ``ses`` — AWS SES v2 through boto3, authenticated by the ambient credential chain
  (the ECS API task role; there is no mail credential setting). The client is built
  lazily with bounded connect/read timeouts and a bounded standard retry policy, and
  can be injected for tests so ``boto3`` is never needed to exercise the adapter.
* ``console`` — development only: prints a clearly delimited block to stdout so a
  developer can follow a link. Never routed through logging, so it cannot reach a
  log sink.
* ``memory`` — development/test only: an in-process outbox (a process singleton via
  :func:`get_mail_sender`) that tests read directly. Nothing leaves the process.

Hardening shared by every backend:

* **Nothing sensitive leaves this module in an error or a log.** A message body
  carries a raw account token, so an :class:`EmailMessage` is never logged and its
  recipient and bodies are excluded from its repr. A failure becomes a
  :class:`MailSendError` carrying only a static ``error_class``; the provider
  exception (whose message can quote the recipient) is dropped, not chained.
* **Static telemetry.** :func:`send_mail` emits exactly one ``mail.sent`` or
  ``mail.send_failed`` event carrying only template, template version, backend,
  outcome, error class and provider message id.
* **Header safety.** Recipient and subject must be non-empty single lines; the
  From header is built with :func:`email.utils.formataddr`.
"""

from __future__ import annotations

import logging
import sys
import threading
from dataclasses import dataclass, field
from email.utils import formataddr
from typing import Any, Protocol, TextIO

from app.core.config import Settings, get_settings
from app.core.logging import get_logger, log_event

logger = get_logger(__name__)

#: SES v2 error codes meaning "slow down" - retrying later can succeed.
_THROTTLED_CODES = frozenset(
    {
        "TooManyRequestsException",
        "LimitExceededException",
        "ThrottlingException",
        "Throttling",
    }
)
#: SES v2 error codes meaning the message or the sending account was refused;
#: retrying the same request cannot succeed.
_REJECTED_CODES = frozenset(
    {
        "MessageRejected",
        "MailFromDomainNotVerifiedException",
        "AccountSuspendedException",
        "SendingPausedException",
        "BadRequestException",
        "NotFoundException",
    }
)


@dataclass(frozen=True)
class EmailMessage:
    """One rendered message. The bodies may carry a raw account token, so an
    instance is never logged or persisted; recipient and bodies are kept out of
    its repr."""

    to: str = field(repr=False)
    subject: str
    text_body: str = field(repr=False)
    html_body: str = field(repr=False)
    template: str
    template_version: str

    def __post_init__(self) -> None:
        # Recipient and subject become mail headers, where a CR/LF is header
        # injection. Templates only produce static subjects and the recipient is
        # the stored account address, so this is a last-line guard, not a filter.
        for value in (self.to, self.subject, self.template, self.template_version):
            if not value or "\r" in value or "\n" in value:
                raise ValueError("email message header fields must be non-empty single lines")


class MailSendError(Exception):
    """A message was not handed to the mail backend.

    ``error_class`` is a static short string (``timeout``, ``throttled``,
    ``rejected``, ``provider_error``, ``not_configured``, ``internal_error``) and
    the only thing this exception carries - never the provider's message, the
    recipient or any part of the body - so it is safe to log.
    """

    def __init__(self, error_class: str) -> None:
        super().__init__(error_class)
        self.error_class = error_class


class MailSender(Protocol):
    backend: str

    def send(self, message: EmailMessage) -> str | None:
        """Hand ``message`` to the backend; return the provider message id, if any."""
        ...


class MemoryMailSender:
    """In-process outbox for tests (and local development). Never delivers."""

    backend = "memory"

    def __init__(self) -> None:
        self.outbox: list[EmailMessage] = []
        self._lock = threading.Lock()

    def send(self, message: EmailMessage) -> str | None:
        # Background tasks run on a worker thread; the lock keeps a concurrent
        # clear() from racing an append.
        with self._lock:
            self.outbox.append(message)
        return None

    def clear(self) -> None:
        with self._lock:
            self.outbox.clear()


class ConsoleMailSender:
    """Development-only sender that prints each message to stdout.

    Deliberately writes to the stream rather than through logging, so a printed
    link (which carries a raw token) can never reach a structured log sink. Refuses
    to construct outside development.
    """

    backend = "console"

    def __init__(self, *, environment: str, stream: TextIO | None = None) -> None:
        if environment != "development":
            raise MailSendError("not_configured")
        self._stream = stream

    def send(self, message: EmailMessage) -> str | None:
        # Resolve stdout at send time so a replaced sys.stdout (e.g. test capture)
        # is honoured.
        stream = self._stream if self._stream is not None else sys.stdout
        block = "\n".join(
            [
                "",
                "=" * 24 + " SignalNest development mail (not delivered) " + "=" * 24,
                f"To: {message.to}",
                f"Subject: {message.subject}",
                f"Template: {message.template} v{message.template_version}",
                "-" * 92,
                message.text_body.rstrip("\n"),
                "=" * 38 + " end development mail " + "=" * 32,
                "",
            ]
        )
        print(block, file=stream, flush=True)
        return None


def classify_ses_error(exc: BaseException) -> str:
    """Map an SES/botocore failure to a static error class.

    Only the exception type and the service's error *code* are inspected; the
    error message (which can quote the recipient) is never read.
    """
    try:
        from botocore import exceptions as botocore_exceptions
    except ImportError:  # pragma: no cover - boto3 is absent only in base installs
        return "provider_error"

    if isinstance(
        exc,
        (
            botocore_exceptions.ReadTimeoutError,
            botocore_exceptions.ConnectTimeoutError,
            botocore_exceptions.EndpointConnectionError,
        ),
    ):
        return "timeout"
    if isinstance(
        exc,
        (
            botocore_exceptions.NoCredentialsError,
            botocore_exceptions.PartialCredentialsError,
            botocore_exceptions.NoRegionError,
        ),
    ):
        return "not_configured"
    if isinstance(exc, botocore_exceptions.ClientError):
        response = exc.response if isinstance(exc.response, dict) else {}
        code = (response.get("Error") or {}).get("Code")
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        if code in _THROTTLED_CODES or status == 429:
            return "throttled"
        if code in _REJECTED_CODES:
            return "rejected"
        # 5xx, InternalFailure, ServiceUnavailable, AccessDenied and any code not
        # recognised above.
        return "provider_error"
    return "provider_error"


def _build_ses_client(*, region: str, timeout_seconds: float) -> Any:
    """Build a bounded, timeout-guarded boto3 SES v2 client. Lazy import.

    boto3 ships only with the ``full`` extra; without it (or with a client that
    cannot be built) the backend is simply not configured. As in ``send``, the
    error is raised outside the except block so it carries no context.
    """
    try:
        import boto3
        from botocore.config import Config

        client = boto3.client(
            "sesv2",
            region_name=region,
            config=Config(
                connect_timeout=timeout_seconds,
                read_timeout=timeout_seconds,
                retries={"mode": "standard", "max_attempts": 3},
            ),
        )
    except Exception:
        client = None
    if client is None:
        raise MailSendError("not_configured")
    return client


class SesMailSender:
    """AWS SES v2 sender.

    Takes an injected ``client`` (tests) or builds one lazily on first send, so
    constructing the sender never imports ``boto3`` or resolves credentials.
    """

    backend = "ses"

    def __init__(
        self,
        *,
        client: Any = None,
        region: str,
        from_address: str,
        from_name: str,
        reply_to: str | None = None,
        configuration_set: str | None = None,
        timeout_seconds: float,
    ) -> None:
        self._client = client
        self._client_lock = threading.Lock()
        self._region = region
        self._from = formataddr((from_name, from_address))
        self._reply_to = reply_to
        self._configuration_set = configuration_set
        self._timeout_seconds = timeout_seconds

    def _get_client(self) -> Any:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = _build_ses_client(
                        region=self._region, timeout_seconds=self._timeout_seconds
                    )
        return self._client

    def send(self, message: EmailMessage) -> str | None:
        client = self._get_client()
        request: dict[str, Any] = {
            "FromEmailAddress": self._from,
            "Destination": {"ToAddresses": [message.to]},
            "Content": {
                "Simple": {
                    "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                    "Body": {
                        "Text": {"Data": message.text_body, "Charset": "UTF-8"},
                        "Html": {"Data": message.html_body, "Charset": "UTF-8"},
                    },
                }
            },
        }
        if self._reply_to:
            request["ReplyToAddresses"] = [self._reply_to]
        if self._configuration_set:
            request["ConfigurationSetName"] = self._configuration_set

        try:
            response = client.send_email(**request)
        except Exception as exc:
            error_class = classify_ses_error(exc)
        else:
            message_id = response.get("MessageId") if isinstance(response, dict) else None
            return message_id if isinstance(message_id, str) else None
        # Raised outside the except block so the provider exception (whose message
        # can name the recipient) is not attached as context.
        raise MailSendError(error_class)


def build_mail_sender(settings: Settings) -> MailSender:
    """Build the sender for ``settings.effective_mail_backend``.

    No resolvable backend, or ``ses`` without a sender address or region, raises
    ``MailSendError("not_configured")`` (staging/production cannot get here: their
    Settings validation already requires all of it).
    """
    backend = settings.effective_mail_backend
    if backend == "memory":
        return MemoryMailSender()
    if backend == "console":
        return ConsoleMailSender(environment=settings.environment)
    if backend == "ses":
        if not settings.mail_from_address or not settings.mail_ses_region:
            raise MailSendError("not_configured")
        return SesMailSender(
            region=settings.mail_ses_region,
            from_address=settings.mail_from_address,
            from_name=settings.mail_from_name,
            reply_to=settings.mail_reply_to_address,
            configuration_set=settings.mail_ses_configuration_set,
            timeout_seconds=settings.mail_send_timeout_seconds,
        )
    raise MailSendError("not_configured")


_sender: MailSender | None = None
_sender_lock = threading.Lock()


def get_mail_sender() -> MailSender:
    """Return the process-wide sender, built on first use from ``get_settings()``.

    Cached, so the memory backend is a process singleton whose outbox tests can
    read. A failed build is not cached; the next call retries.
    """
    global _sender
    with _sender_lock:
        if _sender is None:
            _sender = build_mail_sender(get_settings())
        return _sender


def reset_mail_sender() -> None:
    """Drop the cached sender so the next call rebuilds it from settings (tests)."""
    global _sender
    with _sender_lock:
        _sender = None


def send_mail(message: EmailMessage) -> str | None:
    """Send ``message`` through the configured backend and record one static event.

    Returns the provider message id (``None`` for local backends). Every failure is
    logged once and raised as a :class:`MailSendError` carrying only its class.
    """
    backend = "unconfigured"
    try:
        sender = get_mail_sender()
        backend = sender.backend
        message_id = sender.send(message)
    except MailSendError as exc:
        error_class = exc.error_class
    except Exception:
        # A defect in a sender must still surface as one static failure; the
        # original exception may carry message content, so it is dropped.
        error_class = "internal_error"
    else:
        log_event(
            logger,
            "mail.sent",
            outcome="success",
            template=message.template,
            template_version=message.template_version,
            backend=backend,
            provider_message_id=message_id,
        )
        return message_id
    log_event(
        logger,
        "mail.send_failed",
        level=logging.WARNING,
        outcome="failure",
        template=message.template,
        template_version=message.template_version,
        backend=backend,
        error_class=error_class,
    )
    raise MailSendError(error_class)
