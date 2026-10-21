"""Closed registry of governable capabilities (Phase 4A-C.1).

This module is the *single source of truth* for the only capabilities that may
ever be governed by a per-workspace override, each bound to its global feature
flag on :class:`app.core.config.Settings` and carrying frozen governance policy
metadata.

Design invariants:

* **Closed allow-list.** :class:`Capability` is a small, explicit ``StrEnum``.
  A name that is not a member is *not* governable — a later resolver treats it as
  a safety-ceiling deny, and the persistence layer refuses to store it.
* **Immutable.** The registry mapping is exposed through a read-only
  :class:`~types.MappingProxyType`, and every policy is a frozen dataclass, so no
  caller can mutate governance metadata at runtime.
* **Single derivation point.** Both the override model's ``CheckConstraint`` and
  (in a later batch) the resolver derive their capability set from
  :func:`persisted_values` / :func:`iter_capabilities`, so the *storable* set and
  the *resolvable* set can never drift.
* **Portable persisted form.** Each capability persists as its ``StrEnum`` value
  (a plain ``String`` column, repo convention — no native PostgreSQL enum).

This module defines the registry and its metadata only; no precedence is executed
here. (When 4A-C.1 shipped nothing consumed these policies; since then the resolver,
the operator console and — since P6-CAP-1 — every capability's enforcement point do.)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


class Capability(StrEnum):
    """The closed set of capabilities that may be governed per workspace.

    The value of each member is the stable string persisted in the
    ``workspace_capability_overrides.capability`` column and (later) resolved by
    the capability resolver. Values are bound to their global flag by the
    registry below.
    """

    OPPORTUNITY_FEEDBACK = "opportunity_feedback"
    SCOUT_SCHEDULING = "scout_scheduling"
    CONNECTOR_RSS = "connector_rss"


@dataclass(frozen=True, slots=True)
class CapabilityPolicy:
    """Immutable governance metadata for a single capability.

    Attributes:
        capability: The :class:`Capability` this policy describes.
        label: Human-safe, non-secret display label for operator surfaces.
        global_flag_attr: The :class:`app.core.config.Settings` attribute name of
            the global master flag this capability is bound to.
        workspace_enableable: Whether an operator override may record intent to
            *enable* this capability for a single workspace. ``False`` means the
            capability is only ever activated by a global decision (e.g. the RSS
            connector, whose activation is a global connector-policy/legal call,
            not a per-workspace toggle).
        workspace_disableable: Whether an operator override may force this
            capability *off* for a single workspace even when the global flag is
            on. Deny-biased: every capability is disableable.
        subject_to_safety_ceiling: Whether a platform safety ceiling may force
            this capability off regardless of override or global flag.
        requires_workspace_context: Whether resolving this capability is only
            meaningful within a concrete workspace scope.
        future_activation_phase: The phase whose plan record names the first real
            activation of this capability, or ``None`` when no plan record does.
            Descriptive only — it gates nothing — but it is rendered verbatim by the
            operator console ("First activation planned in Phase …"), so it must
            match a governing plan document, never a guess (P6-CAP-3).
    """

    capability: Capability
    label: str
    global_flag_attr: str
    workspace_enableable: bool
    workspace_disableable: bool
    subject_to_safety_ceiling: bool
    requires_workspace_context: bool
    future_activation_phase: str | None


# Declaration order here is the canonical, deterministic enumeration order used by
# every public accessor below.
#
# ``future_activation_phase`` provenance (P6-CAP-3). Every entry once read "4B", which
# only the feedback entry's records support:
# * ``docs/phase-4b-plan.md`` is titled "Opportunity Feedback Internal Canary
#   Activation"; its 4B-B canary was never executed and now needs its own separate
#   authorization (``docs/project-phase-6-plan.md`` §10.3), but Phase 4B remains the
#   phase whose plan names that activation.
# * The same plan's §15 lists "scheduling activation" and "RSS activation" among the
#   non-goals of all of Phase 4B, and no later plan record schedules either (RSS is
#   blocked on a connector-policy/legal decision). Both are therefore ``None``.
# ``test_capability_registry.py`` cross-checks these values against that plan.
_POLICIES: tuple[CapabilityPolicy, ...] = (
    CapabilityPolicy(
        capability=Capability.OPPORTUNITY_FEEDBACK,
        label="Opportunity Feedback",
        global_flag_attr="opportunity_feedback_enabled",
        workspace_enableable=True,
        workspace_disableable=True,
        subject_to_safety_ceiling=True,
        requires_workspace_context=True,
        future_activation_phase="4B",
    ),
    CapabilityPolicy(
        capability=Capability.SCOUT_SCHEDULING,
        label="Scout Scheduling",
        global_flag_attr="scout_scheduling_enabled",
        workspace_enableable=True,
        workspace_disableable=True,
        subject_to_safety_ceiling=True,
        requires_workspace_context=True,
        future_activation_phase=None,
    ),
    CapabilityPolicy(
        capability=Capability.CONNECTOR_RSS,
        label="RSS Connector",
        global_flag_attr="connector_rss_enabled",
        # RSS activation is a global connector-policy/legal decision (service-layer
        # selection, not a per-workspace route guard), so it is never enabled by a
        # per-workspace override — only globally. It remains disableable per
        # workspace for deny-biased safety.
        workspace_enableable=False,
        workspace_disableable=True,
        subject_to_safety_ceiling=True,
        requires_workspace_context=True,
        future_activation_phase=None,
    ),
)

# Read-only registry keyed by capability. Exposed as a MappingProxyType so callers
# cannot mutate governance metadata at runtime.
CAPABILITY_REGISTRY: MappingProxyType[Capability, CapabilityPolicy] = MappingProxyType(
    {policy.capability: policy for policy in _POLICIES}
)


class UnknownCapabilityError(ValueError):
    """Raised when a string does not name a member of the closed registry."""


def iter_capabilities() -> tuple[Capability, ...]:
    """Return all governable capabilities in canonical declaration order."""

    return tuple(policy.capability for policy in _POLICIES)


def get_policy(capability: Capability) -> CapabilityPolicy:
    """Return the frozen governance policy for a known capability."""

    return CAPABILITY_REGISTRY[capability]


def capability_from_value(value: str) -> Capability:
    """Strictly convert a persisted/string value to a :class:`Capability`.

    Raises :class:`UnknownCapabilityError` for any value that is not a member of
    the closed registry — the storable set never widens by accident.
    """

    try:
        return Capability(value)
    except ValueError as exc:
        raise UnknownCapabilityError(value) from exc


def is_known_capability(value: str) -> bool:
    """Return whether ``value`` names a member of the closed registry."""

    return value in Capability._value2member_map_


def persisted_values() -> tuple[str, ...]:
    """Return the sorted tuple of persisted capability values.

    Sorted for a deterministic, stable rendering wherever the closed set is
    materialized (notably the migration's ``CheckConstraint`` ``IN (...)`` list),
    so regeneration never produces a spurious diff.
    """

    return tuple(sorted(c.value for c in iter_capabilities()))
