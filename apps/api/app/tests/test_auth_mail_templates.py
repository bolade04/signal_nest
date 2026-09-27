"""6B-4A: account-email templates (password reset, email verification).

Each builder takes only the recipient (the stored address) and the raw token -- there is no
parameter through which a name, organization or workspace could reach a message -- and every
message carries a static subject, plain-text and HTML bodies, a call to action plus the plain
URL as a fallback, the expiry from settings, a "didn't request this" disclaimer, a "never
share this link" warning and a support line (configured, or a static generic one).

Links are ``public_web_origin`` + ``/reset-password#token=`` or ``/verify-email#token=``:
the token rides in the fragment. The end-to-end test gives the account, its organization and
its workspace names full of markup and CR/LF and shows none of it reaches the mail.
"""

from __future__ import annotations

import html
import inspect
from collections.abc import Iterator

import pytest
from sqlalchemy import update

from app.core.config import get_settings
from app.infra import mail_templates as templates
from app.organizations.models import Organization, User, Workspace
from app.tests._auth2_support import (
    ALICE,
    ORG_A,
    WS_A,
    Env,
    environment,
    mint_reset,
    mint_verification,
)

ORIGIN = "https://app.signalnest.test"
RAW = "Zm9vYmFyLXRva2VuLXZhbHVlLTAxMjM0NTY3ODlhYmNkZWY"
BUILDERS = {
    "password_reset": (templates.password_reset_message, "/reset-password"),
    "email_verification": (templates.email_verification_message, "/verify-email"),
}


@pytest.fixture(autouse=True)
def _origin(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_web_origin", ORIGIN)
    monkeypatch.setattr(get_settings(), "mail_support_contact", None)


def _text(message) -> str:
    return message.text_body


def _visible_html(message) -> str:
    return html.unescape(message.html_body)


@pytest.mark.parametrize("name", list(BUILDERS))
class TestEachTemplate:
    def test_only_the_recipient_and_the_token_can_be_supplied(self, name: str):
        builder, _ = BUILDERS[name]
        params = inspect.signature(builder).parameters
        assert list(params) == ["to", "raw_token"]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params.values())

    def test_identity_and_link(self, name: str):
        builder, path = BUILDERS[name]
        message = builder(to="alice@example.com", raw_token=RAW)
        link = f"{ORIGIN}{path}#token={RAW}"
        assert (message.to, message.template) == ("alice@example.com", name)
        assert isinstance(message.template_version, str) and message.template_version
        assert link in message.text_body
        # A call to action and the plain URL as a fallback.
        assert f'href="{link}"' in message.html_body
        assert message.html_body.count(link) >= 2
        for body in (message.subject, message.text_body, message.html_body):
            assert "?token" not in body and "&token" not in body

    def test_the_subject_is_static(self, name: str):
        builder, _ = BUILDERS[name]
        a = builder(to="alice@example.com", raw_token=RAW)
        b = builder(to="Someone.Else+x@example.org", raw_token="other-token-value")
        assert a.subject == b.subject
        assert RAW not in a.subject and "alice" not in a.subject
        assert "\r" not in a.subject and "\n" not in a.subject

    def test_the_copy_warns_and_disclaims(self, name: str):
        builder, _ = BUILDERS[name]
        message = builder(to="alice@example.com", raw_token=RAW)
        for body in (_text(message), _visible_html(message)):
            lowered = body.lower()
            assert "if you didn't request this" in lowered
            assert "never share this link" in lowered
            if name == "password_reset":
                assert "your password has not changed" in lowered

    def test_the_support_line_comes_from_settings(self, name: str, monkeypatch):
        builder, _ = BUILDERS[name]
        generic = builder(to="alice@example.com", raw_token=RAW)
        assert generic == builder(to="alice@example.com", raw_token=RAW)  # deterministic
        assert "None" not in generic.text_body
        monkeypatch.setattr(get_settings(), "mail_support_contact", "help@signalnest.test")
        configured = builder(to="alice@example.com", raw_token=RAW)
        assert "help@signalnest.test" in configured.text_body
        assert "help@signalnest.test" in _visible_html(configured)
        assert "help@signalnest.test" not in generic.text_body

    def test_the_recipient_is_escaped_wherever_html_shows_it(self, name: str):
        builder, _ = BUILDERS[name]
        to = "o'neil&co@example.com"
        message = builder(to=to, raw_token=RAW)
        assert to not in message.html_body  # never raw; escaped if shown at all
        if to in message.text_body:
            assert html.escape(to, quote=True) in message.html_body


class TestExpiry:
    def test_reset_states_its_lifetime_in_minutes(self, monkeypatch):
        assert "60 minutes" in _text(templates.password_reset_message(to="a@x.io", raw_token=RAW))
        monkeypatch.setattr(get_settings(), "password_reset_token_ttl_minutes", 30)
        message = templates.password_reset_message(to="a@x.io", raw_token=RAW)
        assert "30 minutes" in _text(message) and "30 minutes" in _visible_html(message)

    def test_verification_states_its_lifetime_in_hours(self, monkeypatch):
        message = templates.email_verification_message(to="a@x.io", raw_token=RAW)
        assert "48 hours" in _text(message)
        monkeypatch.setattr(get_settings(), "email_verification_token_ttl_hours", 24)
        message = templates.email_verification_message(to="a@x.io", raw_token=RAW)
        assert "24 hours" in _text(message) and "24 hours" in _visible_html(message)


def test_the_two_templates_are_distinct():
    reset = templates.password_reset_message(to="a@x.io", raw_token=RAW)
    verify = templates.email_verification_message(to="a@x.io", raw_token=RAW)
    assert reset.subject != verify.subject and reset.template != verify.template


def test_links_are_built_from_the_configured_origin():
    assert templates.build_reset_link(RAW) == f"{ORIGIN}/reset-password#token={RAW}"
    assert templates.build_verification_link(RAW) == f"{ORIGIN}/verify-email#token={RAW}"


# --------------------------------------------------------------------------- #
HOSTILE = {
    "full_name": '<script>alert("n")</script>\r\nBcc: victim@evil.example',
    "org": "<b>Evil Org</b>\r\nX-Injected: 1",
    "workspace": '<img src=x onerror="alert(1)">\nReply-To: evil@evil.example',
}
MARKERS = ("<script", "alert(", "Bcc:", "X-Injected", "<img", "onerror", "Evil Org", "evil.example")


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "templates.db") as e:
        yield e


def test_account_and_tenant_names_never_reach_a_message(env: Env):
    for stmt in (
        update(User).where(User.id == ALICE).values(full_name=HOSTILE["full_name"]),
        update(Organization).where(Organization.id == ORG_A).values(name=HOSTILE["org"]),
        update(Workspace).where(Workspace.id == WS_A).values(name=HOSTILE["workspace"]),
    ):
        assert env.witness.write(stmt) == 1
    mint_reset(env, ALICE)
    mint_verification(env, ALICE)
    messages = env.messages()
    assert [m.template for m in messages] == ["password_reset", "email_verification"]
    for message in messages:
        for part in (message.subject, message.text_body, message.html_body, message.to):
            for marker in MARKERS:
                assert marker not in part, (message.template, marker)
        assert "\r" not in message.subject and "\n" not in message.subject
