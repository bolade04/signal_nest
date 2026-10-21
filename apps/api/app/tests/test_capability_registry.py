"""Phase 4A-C.1: unit tests for the closed capability registry.

These assert the type-safety foundation only — there is no resolver, no
precedence, no persistence and no gate here. What is covered: the closed
``Capability`` value set and its exact binding to the global feature flags, the
frozen/immutable policy metadata, deterministic enumeration, strict string
conversion, the sorted persisted-value list the migration derives its check
constraint from, the RSS non-enableable governance policy, and (P6-CAP-3) that each
``future_activation_phase`` agrees with the plan record it claims.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.capabilities.registry import (
    CAPABILITY_REGISTRY,
    Capability,
    CapabilityPolicy,
    UnknownCapabilityError,
    capability_from_value,
    get_policy,
    is_known_capability,
    iter_capabilities,
    persisted_values,
)
from app.core.config import get_settings


def test_capability_value_set_is_closed_and_exact() -> None:
    assert {c.value for c in Capability} == {
        "opportunity_feedback",
        "scout_scheduling",
        "connector_rss",
    }


def test_registry_covers_every_capability_exactly_once() -> None:
    assert set(CAPABILITY_REGISTRY) == set(Capability)
    assert len(CAPABILITY_REGISTRY) == len(Capability)


def test_iter_capabilities_is_deterministic_declaration_order() -> None:
    assert iter_capabilities() == (
        Capability.OPPORTUNITY_FEEDBACK,
        Capability.SCOUT_SCHEDULING,
        Capability.CONNECTOR_RSS,
    )
    # Stable across repeated calls.
    assert iter_capabilities() == iter_capabilities()


def test_each_policy_binds_a_real_settings_flag_that_is_dark() -> None:
    settings = get_settings()
    for capability in iter_capabilities():
        policy = get_policy(capability)
        assert hasattr(settings, policy.global_flag_attr), policy.global_flag_attr
        # Foundation batch ships dark: every bound global flag is False.
        assert getattr(settings, policy.global_flag_attr) is False


def test_global_flag_bindings_are_exact() -> None:
    assert get_policy(Capability.OPPORTUNITY_FEEDBACK).global_flag_attr == (
        "opportunity_feedback_enabled"
    )
    assert get_policy(Capability.SCOUT_SCHEDULING).global_flag_attr == (
        "scout_scheduling_enabled"
    )
    assert get_policy(Capability.CONNECTOR_RSS).global_flag_attr == (
        "connector_rss_enabled"
    )


def test_policies_are_frozen_and_immutable() -> None:
    policy = get_policy(Capability.OPPORTUNITY_FEEDBACK)
    assert dataclasses.is_dataclass(policy)
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.workspace_enableable = True  # type: ignore[misc]
    with pytest.raises(dataclasses.FrozenInstanceError):
        policy.label = "mutated"  # type: ignore[misc]


def test_registry_mapping_is_read_only() -> None:
    with pytest.raises(TypeError):
        CAPABILITY_REGISTRY[Capability.CONNECTOR_RSS] = CapabilityPolicy(  # type: ignore[index]
            capability=Capability.CONNECTOR_RSS,
            label="x",
            global_flag_attr="connector_rss_enabled",
            workspace_enableable=True,
            workspace_disableable=True,
            subject_to_safety_ceiling=True,
            requires_workspace_context=True,
            future_activation_phase="4B",
        )


def test_rss_is_not_workspace_enableable_but_is_disableable() -> None:
    # RSS activation is a global connector-policy/legal decision, never a
    # per-workspace enable; it stays disableable for deny-biased safety.
    rss = get_policy(Capability.CONNECTOR_RSS)
    assert rss.workspace_enableable is False
    assert rss.workspace_disableable is True


def test_route_guarded_capabilities_are_workspace_enableable() -> None:
    for capability in (Capability.OPPORTUNITY_FEEDBACK, Capability.SCOUT_SCHEDULING):
        policy = get_policy(capability)
        assert policy.workspace_enableable is True
        assert policy.workspace_disableable is True


def test_every_capability_is_ceiling_subject_and_workspace_scoped() -> None:
    for capability in iter_capabilities():
        policy = get_policy(capability)
        assert policy.subject_to_safety_ceiling is True
        assert policy.requires_workspace_context is True
        assert policy.label and not policy.label.startswith(" ")


# --------------------------------------------------------------------------- #
# P6-CAP-3 — future_activation_phase must agree with the plan record it names
#
# Every entry once read "4B" and a blanket ``== "4B"`` assertion enforced it, so the
# test suite pinned a value the Phase 4B plan contradicts for two of the three
# capabilities. These tests check the values against that plan instead of against a
# copy of themselves.
# --------------------------------------------------------------------------- #
_REPO_ROOT = Path(__file__).resolve().parents[4]

#: Phase -> the plan document that governs it. A capability may claim a phase only if
#: that phase's plan is listed here and names the capability in its title.
_PHASE_PLANS = {"4B": _REPO_ROOT / "docs" / "phase-4b-plan.md"}

#: The Phase 4B plan's §15 non-goal that excludes each capability's activation from
#: all of Phase 4B. Such a capability may never claim "4B".
_EXCLUDED_FROM_4B = {
    Capability.SCOUT_SCHEDULING: "scheduling activation",
    Capability.CONNECTOR_RSS: "RSS activation",
}


def _section(text: str, heading: str) -> str:
    """The body of the ``## <heading>`` section, up to the next ``## `` heading."""
    lines = text.splitlines()
    start = lines.index(heading) + 1
    end = next(
        (i for i in range(start, len(lines)) if lines[i].startswith("## ")), len(lines)
    )
    return "\n".join(lines[start:end])


def test_future_activation_phase_values_are_the_recorded_ones() -> None:
    assert {c: get_policy(c).future_activation_phase for c in iter_capabilities()} == {
        Capability.OPPORTUNITY_FEEDBACK: "4B",
        Capability.SCOUT_SCHEDULING: None,
        Capability.CONNECTOR_RSS: None,
    }


def test_a_claimed_phase_is_one_whose_plan_names_the_capability() -> None:
    for capability in iter_capabilities():
        policy = get_policy(capability)
        phase = policy.future_activation_phase
        if phase is None:
            continue
        assert phase in _PHASE_PLANS, (
            f"{capability.value} claims Phase {phase!r}, which has no governing plan "
            "record registered in _PHASE_PLANS; add the record or clear the claim"
        )
        title = _PHASE_PLANS[phase].read_text(encoding="utf-8").splitlines()[0]
        assert policy.label in title, (
            f"{capability.value} claims Phase {phase}, but that plan's title "
            f"({title!r}) does not name {policy.label!r}"
        )


def test_no_capability_claims_phase_4b_when_that_plan_excludes_it() -> None:
    plan = _PHASE_PLANS["4B"].read_text(encoding="utf-8")
    non_goals = _section(plan, "## 15. Non-goals")
    for capability, non_goal in _EXCLUDED_FROM_4B.items():
        # The record still excludes it — if the plan is ever amended, this fails and
        # the registry value must be revisited rather than silently kept.
        assert any(
            line.startswith(f"- {non_goal}") for line in non_goals.splitlines()
        ), non_goal
        assert get_policy(capability).future_activation_phase != "4B", capability.value


def test_capability_from_value_is_strict() -> None:
    assert capability_from_value("connector_rss") is Capability.CONNECTOR_RSS
    with pytest.raises(UnknownCapabilityError):
        capability_from_value("not_a_capability")
    with pytest.raises(UnknownCapabilityError):
        capability_from_value("")
    # UnknownCapabilityError is a ValueError for ergonomic handling.
    assert issubclass(UnknownCapabilityError, ValueError)


def test_is_known_capability_reflects_closed_set() -> None:
    assert is_known_capability("scout_scheduling") is True
    assert is_known_capability("connector_rss") is True
    assert is_known_capability("bogus") is False
    assert is_known_capability("Scout_Scheduling") is False  # case-sensitive


def test_persisted_values_are_sorted_and_match_enum() -> None:
    values = persisted_values()
    assert values == ("connector_rss", "opportunity_feedback", "scout_scheduling")
    assert list(values) == sorted(values)
    assert set(values) == {c.value for c in Capability}
