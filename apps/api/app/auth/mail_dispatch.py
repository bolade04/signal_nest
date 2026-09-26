"""Post-response delivery of account-token mail (password reset, email verification).

The routes commit the token first and schedule these as FastAPI background tasks, so a
message is only ever sent for a committed token and the response never waits on, or
reveals, delivery. A background task has no caller to report to: every failure, whether
building the message or sending it, is caught here, the unsent token is revoked in a
fresh session, and one static event is logged carrying only the error class — never the
address, token, link or message body. Nothing is re-raised.

Residual (AUTH2-C3): revoke-on-failure only covers a send that runs and fails. If the
process dies after the route commits but before the task runs, the unsent token stays
valid until it expires or a later request supersedes it. This is an availability
residual, accepted by design: there is deliberately no durable outbox, because one
would have to persist the raw token.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.auth import account_tokens
from app.auth.account_tokens import IssuedToken
from app.core.logging import get_logger, log_event
from app.infra import mail_templates
from app.infra.mail import EmailMessage, MailSendError, send_mail

logger = get_logger(__name__)


def send_password_reset_email(issued: IssuedToken) -> None:
    _deliver(
        lambda: mail_templates.password_reset_message(
            to=issued.recipient, raw_token=issued.raw_token
        ),
        token_id=issued.token_id,
        revoke=account_tokens.revoke_password_reset_token,
        failure_event="security.password_reset.mail_failed",
    )


def send_email_verification_email(issued: IssuedToken) -> None:
    _deliver(
        lambda: mail_templates.email_verification_message(
            to=issued.recipient, raw_token=issued.raw_token
        ),
        token_id=issued.token_id,
        revoke=account_tokens.revoke_email_verification_token,
        failure_event="security.email_verification.mail_failed",
    )


def _deliver(
    build: Callable[[], EmailMessage],
    *,
    token_id: str,
    revoke: Callable[[str], None],
    failure_event: str,
) -> None:
    try:
        send_mail(build())
    except Exception as exc:
        # An unsent token must not stay usable. revoke_* uses its own session and never
        # raises; the event is static and carries the error class only.
        revoke(token_id)
        log_event(
            logger,
            failure_event,
            level=logging.WARNING,
            outcome="failure",
            error_class=_error_class(exc),
        )


def _error_class(exc: Exception) -> str:
    # MailSendError carries a static class; anything else is named by its type alone,
    # never its message, which could quote the address or the link.
    if isinstance(exc, MailSendError):
        return exc.error_class
    return type(exc).__name__
