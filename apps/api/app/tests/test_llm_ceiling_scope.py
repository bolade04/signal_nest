"""D-N4 item 29 (operator ruling 2026-11-12): which operations the daily ceilings constrain.

The ruling, verbatim: "creative admission counts Scout spend as specified. State accurately which
operations the ceilings constrain; do not strengthen this into an unproven platform-wide spending
guarantee." ``app.llm.quota`` states it; this module pins the statement's two halves:

* **Counted:** the cost ceilings count every task's spend of the day, Scouting included, so
  Scout spend alone can make a generation attempt refused (creative admission).
* **Constrained:** only creative generation is checked against them. A Scout-shaped call (a
  legacy task under the plain :class:`~app.llm.metering.LLMMeter`) is made and recorded while the
  same day's spend is past both cost ceilings: the ceilings are not a platform-wide spending cap.
  Statically, the ceiling checks are reached from ``app.creative.service`` only.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.core.config import get_settings
from app.creative.service import GENERATE_TASK
from app.llm import service as service_module
from app.llm.metering import LLMMeter
from app.llm.models import LLMBudgetReservation, LLMUsageEvent
from app.llm.quota import (
    DAILY_COST_GLOBAL,
    DAILY_COST_WORKSPACE,
    LLMCeilingExceededError,
    check_ceilings,
)
from app.tests._creative_support import CreativeWorld, creative_world

APP = Path(__file__).resolve().parents[1]
CEILINGS = {
    "llm_daily_cost_ceiling_usd_global": 10.0,
    "llm_daily_cost_ceiling_usd_per_workspace": 1.0,
    "llm_max_calls_per_workspace_per_day": 3,
    "llm_max_output_tokens_per_call": 400,
}
#: The names whose use decides a ceiling (``unusable_ceilings`` only reads the settings).
CEILING_CHECKS = {"CeilingMeter", "check_ceilings", "lock_for_generation", "daily_usage"}


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("ceiling-scope") / "scope.db", "cscope") as w:
        yield w


@pytest.fixture(autouse=True)
def ceilings(monkeypatch, world: CreativeWorld):
    for name, value in CEILINGS.items():
        monkeypatch.setattr(get_settings(), name, value)
    with world.witness.begin() as conn:
        conn.execute(LLMUsageEvent.__table__.delete())
        conn.execute(LLMBudgetReservation.__table__.delete())


def _scout_spend(world: CreativeWorld, ws: str, cost: float) -> None:
    with world.session() as s:
        s.add(LLMUsageEvent(
            organization_id=world.org_of(ws), workspace_id=ws, task="explain_opportunity",
            provider="anthropic", model="claude-sonnet-4-6", input_tokens=10, output_tokens=10,
            cost_usd=cost, outcome="ok", created_at=datetime.now(UTC),
        ))


def _generation_refusal(world: CreativeWorld, ws: str) -> str | None:
    with world.session() as s:
        try:
            check_ceilings(s, get_settings(), workspace_id=ws, task=GENERATE_TASK,
                           now=datetime.now(UTC), worst=Decimal(0))
        except LLMCeilingExceededError as refused:
            return refused.reason
    return None


def test_scout_spend_alone_refuses_a_generation_attempt(world: CreativeWorld):
    """Creative admission counts Scout spend: the workspace's and then every workspace's."""
    ws = world.workspace("scope ws")
    assert _generation_refusal(world, ws) is None
    _scout_spend(world, ws, 1.0)
    assert _generation_refusal(world, ws) == DAILY_COST_WORKSPACE
    other = world.workspace("scope other")
    assert _generation_refusal(world, other) is None
    _scout_spend(world, world.workspace("scope third"), 9.0)  # 10.0 across every workspace
    assert _generation_refusal(world, other) == DAILY_COST_GLOBAL


def test_a_scout_call_is_made_while_both_cost_ceilings_are_passed(world: CreativeWorld):
    """The ceilings refuse generation only: a legacy (Scout) task under the plain meter is made
    and recorded with the day's spend past both cost ceilings."""
    ws = world.workspace("scope scout")
    _scout_spend(world, ws, 12.0)  # past the workspace (1.0) and the global (10.0) ceilings
    assert _generation_refusal(world, ws) == DAILY_COST_WORKSPACE
    with world.session() as s:
        meter = LLMMeter(get_settings()).bind(
            s, organization_id=world.org_of(ws), workspace_id=ws, scout_request_id=None
        )
        service_module.llm_service.run(
            "explain_opportunity",
            {"resolved_market": "Dallas", "audience_fit": "parents",
             "pain_point_dna": "price_frustration", "safe_positioning": "Fair pricing."},
            organization_id=world.org_of(ws), workspace_id=ws, meter=meter,
        )
        meter.flush(s)
        s.commit()
    rows = world.rows(LLMUsageEvent, workspace_id=ws)
    assert sorted(r["task"] for r in rows) == ["explain_opportunity", "explain_opportunity"]
    assert {r["outcome"] for r in rows} == {"ok"}


def test_only_creative_generation_reaches_the_ceiling_checks():
    """Outside ``app.llm.quota`` itself, the names that decide a ceiling are used by
    ``app.creative.service`` alone (tests excluded)."""
    users: dict[str, set[str]] = {}
    for path in sorted(APP.rglob("*.py")):
        relative = path.relative_to(APP).as_posix()
        if relative.startswith("tests/") or relative == "llm/quota.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        names = {
            node.id if isinstance(node, ast.Name) else node.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Name | ast.Attribute)
        } | {
            alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            for alias in node.names
        }
        hit = names & CEILING_CHECKS
        if hit:
            users[relative] = hit
    assert set(users) == {"creative/service.py"}, users
