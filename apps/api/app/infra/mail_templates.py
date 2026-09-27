"""Versioned account-email templates (password reset, email verification).

Registry maps template name -> (name, version, subject, text, html), mirroring
:mod:`app.llm.prompts`. Rendering uses stdlib :class:`string.Template` with every
HTML substitution passed through :func:`html.escape`.

Content rules:

* **No user-controlled strings.** A template sees only server-derived values: the
  account link, the expiry statement computed from settings, and the support line
  from settings. No name, organization or workspace text can reach a message, so
  a hostile account field can never phish through a SignalNest-branded email.
* **Links come only from ``Settings.public_web_origin``** (validated at startup),
  never from a request's Host/Origin/Referer headers. The raw token rides in the
  URL *fragment*, which browsers neither send to a server nor put in a Referer.
* **Static subjects**, so a subject can never carry a header-injection payload.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from string import Template
from urllib.parse import quote

from app.core.config import get_settings
from app.infra.mail import EmailMessage

RESET_PASSWORD_PATH = "/reset-password"
VERIFY_EMAIL_PATH = "/verify-email"

#: Support copy used when ``mail_support_contact`` is not configured (FD-2: no real
#: support contact is chosen in this repository).
GENERIC_SUPPORT_LINE = "If you need help, contact SignalNest support."

_STYLE_BODY = (
    "margin:0;padding:24px;background:#f6f7f9;color:#1f2933;"
    "font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,"
    "sans-serif;font-size:15px;line-height:1.5"
)
_STYLE_CARD = "max-width:560px;margin:0 auto;background:#ffffff;border-radius:8px;padding:32px"
_STYLE_BUTTON = (
    "display:inline-block;padding:12px 20px;background:#1d4ed8;color:#ffffff;"
    "text-decoration:none;border-radius:6px;font-weight:600"
)
_STYLE_MUTED = "color:#52606d;font-size:13px"


@dataclass(frozen=True)
class MailTemplate:
    name: str
    version: str
    subject: str
    text: str
    html: str


def _html_document(*, heading: str, intro: str, cta: str, ignore: str) -> str:
    """Assemble the shared HTML layout for one template (static text only)."""
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        "<title>${subject}</title>\n"
        "</head>\n"
        f'<body style="{_STYLE_BODY}">\n'
        f'<div style="{_STYLE_CARD}">\n'
        f'<h1 style="font-size:20px;margin:0 0 16px">{heading}</h1>\n'
        f"<p>{intro}</p>\n"
        f'<p style="margin:24px 0"><a href="${{link}}" style="{_STYLE_BUTTON}">{cta}</a></p>\n'
        "<p>If the button doesn't work, copy and paste this link into your browser:<br>\n"
        '<a href="${link}" style="word-break:break-all">${link}</a></p>\n'
        "<p>${expiry}</p>\n"
        f"<p>{ignore}</p>\n"
        "<p><strong>Never share this link with anyone.</strong> "
        "SignalNest will never ask you for it.</p>\n"
        f'<p style="{_STYLE_MUTED}">${{support}}</p>\n'
        "</div>\n"
        "</body>\n"
        "</html>\n"
    )


_RESET_IGNORE = (
    "If you didn't request this, you can ignore this email; your password has not changed."
)
_VERIFY_IGNORE = (
    "If you didn't request this, you can ignore this email; "
    "your account and email address have not changed."
)

TEMPLATES: dict[str, MailTemplate] = {
    "password_reset": MailTemplate(
        name="password_reset",
        version="1",
        subject="Reset your SignalNest password",
        text=(
            "Reset your SignalNest password\n"
            "\n"
            "We received a request to reset the password for your SignalNest account. "
            "To choose a new password, open this link:\n"
            "\n"
            "${link}\n"
            "\n"
            "${expiry}\n"
            "\n"
            f"{_RESET_IGNORE}\n"
            "\n"
            "Never share this link with anyone. SignalNest will never ask you for it.\n"
            "\n"
            "${support}\n"
        ),
        html=_html_document(
            heading="Reset your SignalNest password",
            intro=(
                "We received a request to reset the password for your SignalNest "
                "account. To choose a new password, use the button below."
            ),
            cta="Reset password",
            ignore=_RESET_IGNORE,
        ),
    ),
    "email_verification": MailTemplate(
        name="email_verification",
        version="1",
        subject="Verify your SignalNest email address",
        text=(
            "Verify your SignalNest email address\n"
            "\n"
            "Confirm that this email address belongs to your SignalNest account by "
            "opening this link. You will be asked to sign in to that account if you "
            "are not already signed in:\n"
            "\n"
            "${link}\n"
            "\n"
            "${expiry}\n"
            "\n"
            f"{_VERIFY_IGNORE}\n"
            "\n"
            "Never share this link with anyone. SignalNest will never ask you for it.\n"
            "\n"
            "${support}\n"
        ),
        html=_html_document(
            heading="Verify your SignalNest email address",
            intro=(
                "Confirm that this email address belongs to your SignalNest account. "
                "You will be asked to sign in to that account if you are not already "
                "signed in."
            ),
            cta="Verify email address",
            ignore=_VERIFY_IGNORE,
        ),
    ),
}


def get_template(name: str) -> MailTemplate:
    if name not in TEMPLATES:
        raise KeyError(f"No mail template registered for '{name}'")
    return TEMPLATES[name]


def _account_link(path: str, raw_token: str) -> str:
    if not raw_token:
        raise ValueError("an account link requires a token")
    origin = get_settings().public_web_origin
    # Tokens come from secrets.token_urlsafe, so quoting is a no-op for them; it is
    # kept so no other character can ever change the link's structure.
    return f"{origin}{path}#token={quote(raw_token, safe='-_')}"


def build_reset_link(raw_token: str) -> str:
    return _account_link(RESET_PASSWORD_PATH, raw_token)


def build_verification_link(raw_token: str) -> str:
    return _account_link(VERIFY_EMAIL_PATH, raw_token)


def _plural(value: int, unit: str) -> str:
    return f"{value} {unit}" if value == 1 else f"{value} {unit}s"


def _support_line() -> str:
    contact = get_settings().mail_support_contact
    return f"Need help? Contact {contact}." if contact else GENERIC_SUPPORT_LINE


def _render(template: MailTemplate, *, to: str, link: str, lifetime: str) -> EmailMessage:
    # The complete substitution set. Every value is server-derived; nothing an
    # account holder typed (name, organization, workspace) is available here.
    values = {
        "subject": template.subject,
        "link": link,
        "expiry": f"This link expires in {lifetime} and can be used only once.",
        "support": _support_line(),
    }
    escaped = {key: html.escape(value, quote=True) for key, value in values.items()}
    return EmailMessage(
        to=to,
        subject=template.subject,
        text_body=Template(template.text).substitute(values),
        html_body=Template(template.html).substitute(escaped),
        template=template.name,
        template_version=template.version,
    )


def password_reset_message(*, to: str, raw_token: str) -> EmailMessage:
    ttl = get_settings().password_reset_token_ttl_minutes
    return _render(
        get_template("password_reset"),
        to=to,
        link=build_reset_link(raw_token),
        lifetime=_plural(ttl, "minute"),
    )


def email_verification_message(*, to: str, raw_token: str) -> EmailMessage:
    ttl = get_settings().email_verification_token_ttl_hours
    return _render(
        get_template("email_verification"),
        to=to,
        link=build_verification_link(raw_token),
        lifetime=_plural(ttl, "hour"),
    )
