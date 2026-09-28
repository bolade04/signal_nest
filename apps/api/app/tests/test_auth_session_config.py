"""P6-AUTH-4 (FD-AUTH4-C1): the session-lifetime settings are validated at construction.

``session_absolute_lifetime_minutes`` bounds every session (at most 720 minutes, the
12-hour ceiling) and ``access_token_expire_minutes`` may not exceed it, so no configuration
lets a token outlive the session it belongs to. Both are whole minutes; anything else is
refused when ``Settings`` is built, never at first use.
"""

from __future__ import annotations

import math

import pytest
from pydantic import ValidationError

from app.core.config import Settings, get_settings
from app.tests._auth2_support import ALICE, EMAIL, OLD_PASSWORD, as_utc, claims, environment, login

SETTINGS = ("session_absolute_lifetime_minutes", "access_token_expire_minutes")


@pytest.fixture(autouse=True)
def _no_ambient_values(monkeypatch):
    for name in SETTINGS:
        monkeypatch.delenv(name.upper(), raising=False)


def _build(**values) -> Settings:
    return Settings(_env_file=None, **values)


def _refused(**values) -> str:
    with pytest.raises(ValidationError) as caught:
        _build(**values)
    return str(caught.value)


class TestBounds:
    def test_the_defaults_are_720_and_720(self):  # C-01
        s = _build()
        assert (s.session_absolute_lifetime_minutes, s.access_token_expire_minutes) == (720, 720)

    @pytest.mark.parametrize("name", SETTINGS)
    def test_zero_is_refused(self, name):  # C-02
        assert f"{name} must be > 0" in _refused(**{name: 0})

    @pytest.mark.parametrize("name", SETTINGS)
    def test_a_negative_value_is_refused(self, name):  # C-03
        assert f"{name} must be > 0" in _refused(**{name: -1})

    def test_a_lifetime_over_720_is_refused(self):  # C-04
        message = _refused(session_absolute_lifetime_minutes=721)
        assert "session_absolute_lifetime_minutes must be > 0 and <= 720" in message

    def test_a_ttl_longer_than_the_session_is_refused(self):  # C-05
        message = _refused(access_token_expire_minutes=720, session_absolute_lifetime_minutes=600)
        assert (
            "access_token_expire_minutes must be > 0 and <= session_absolute_lifetime_minutes"
            in message
        )

    @pytest.mark.parametrize("raw", ["", "12h", "7.5", "abc"])
    @pytest.mark.parametrize("name", SETTINGS)
    def test_a_non_integer_is_refused_at_construction(self, monkeypatch, name, raw):  # C-07
        monkeypatch.setenv(name.upper(), raw)
        with pytest.raises(ValidationError):
            _build()


class TestShorterTtl:
    def test_a_60_minute_ttl_inside_a_720_minute_session(self, tmp_path, monkeypatch):  # C-06
        s = _build(access_token_expire_minutes=60, session_absolute_lifetime_minutes=720)
        assert (s.access_token_expire_minutes, s.session_absolute_lifetime_minutes) == (60, 720)
        monkeypatch.setattr(get_settings(), "access_token_expire_minutes", 60)
        with environment(tmp_path / "config.db") as env:
            r = login(env, EMAIL[ALICE], OLD_PASSWORD)
            assert r.status_code == 200, r.text
            token = claims(r.json()["access_token"])
            expires_at = env.witness.session_row(token["sid"])["expires_at"]
            deadline = math.floor(as_utc(expires_at).timestamp())
            assert token["exp"] == min(token["iat"] + 3600, deadline) == token["iat"] + 3600
