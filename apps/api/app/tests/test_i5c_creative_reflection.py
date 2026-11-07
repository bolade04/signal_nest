"""I-5C (W21 INT5AB): the creative-capability reflection tells the UI whether a generate request
can be accepted (5C-W4 battery item O-5).

Before I-5C, ``GET /workspaces/{ws}/creative-capability`` answered ``{"enabled": true}`` while a
generation ceiling was unset, so the drafts UI offered Generate and the route answered 503
``creative_generation_unconfigured``. The reflection now also carries
``generation_configured``. It uses the generate route's own check (``unusable_ceilings``),
and is false whenever ``enabled`` is, so a dark workspace learns nothing about the deployment.

Each case below proves agreement on both sides: what the reflection says for every role, and
what the generate route then does. A daily ceiling being reached (429) is not reflected.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from app.core.config import CREATIVE_GENERATION_CEILINGS, get_settings
from app.tests._creative_support import ALL_ROLES, CEILINGS, CreativeWorld, creative_world

#: Values the generate route's ``unusable_ceilings`` refuses (set on the live settings object,
#: past ``Settings`` validation, as the 5C-W4 battery does).
UNUSABLE = (None, 0, -1, float("nan"), float("inf"), True)


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("i5c-reflection") / "api.db", "i5crefl") as w:
        yield w


@pytest.fixture(scope="module")
def enabled_brief(world: CreativeWorld) -> tuple[str, str]:
    ws = world.workspace("i5c enabled")  # creative_generation on by override; flag stays off
    return ws, world.seed_brief(ws)  # an approved brief


@pytest.fixture
def every_ceiling(monkeypatch) -> None:
    for name, value in CEILINGS.items():
        monkeypatch.setattr(get_settings(), name, value)


def _reflection(world: CreativeWorld, ws: str, role: str) -> dict:
    r = world.send("GET", f"/workspaces/{ws}/creative-capability", role)
    assert r.status_code == 200, r.text
    return r.json()


def test_the_ceiling_set_is_the_four_5c_ceilings():
    assert set(CREATIVE_GENERATION_CEILINGS) == set(CEILINGS)


@pytest.mark.parametrize("value", UNUSABLE, ids=repr)
@pytest.mark.parametrize("name", sorted(CEILINGS))
def test_one_unusable_ceiling_is_reported_and_the_route_refuses(
    world, enabled_brief, every_ceiling, monkeypatch, name, value
):
    ws, brief_id = enabled_brief
    monkeypatch.setattr(get_settings(), name, value)
    for role in ALL_ROLES:
        assert _reflection(world, ws, role) == {"enabled": True, "generation_configured": False}
    r = world.generate(brief_id, "marketer", ws=ws, channel="email")
    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "creative_generation_unconfigured"


def test_every_ceiling_usable_is_reported_and_the_route_accepts(
    world, enabled_brief, every_ceiling
):
    ws, brief_id = enabled_brief
    for role in ALL_ROLES:
        assert _reflection(world, ws, role) == {"enabled": True, "generation_configured": True}
    r = world.generate(brief_id, "marketer", ws=ws, channel="email")
    assert r.status_code == 202, r.text
    world.drain()


def test_dark_reports_neither_whatever_the_ceilings(world, every_ceiling, monkeypatch):
    assert get_settings().creative_generation_enabled is False
    ws = world.workspace("i5c dark", enabled=False)
    for unset in (None, *CEILINGS):  # every ceiling usable, then each one unset
        if unset is not None:
            monkeypatch.setattr(get_settings(), unset, None)
        for role in ALL_ROLES:
            assert _reflection(world, ws, role) == {
                "enabled": False,
                "generation_configured": False,
            }
