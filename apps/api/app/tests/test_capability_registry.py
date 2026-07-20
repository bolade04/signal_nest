"""Phase 4A-C.1: unit tests for the closed capability registry.

These assert the type-safety foundation only — there is no resolver, no
precedence, no persistence and no gate here. What is covered: the closed
``Capability`` value set and its exact binding to the global feature flags, the
frozen/immutable policy metadata, deterministic enumeration, strict string
conversion, the sorted persisted-value list the migration derives its check
constraint from, and the RSS non-enableable governance policy.
"""

from __future__ import annotations

import dataclasses

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
        assert policy.future_activation_phase == "4B"
        assert policy.label and not policy.label.startswith(" ")


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
