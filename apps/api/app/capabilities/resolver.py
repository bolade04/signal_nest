"""Centralized, deny-biased capability resolver (Phase 4A-C.2).

This module is the *single* place the *effective* state of one
``(capability, workspace)`` pair is decided. It computes that state under a
strict, deny-biased precedence — safety ceiling → workspace override → global
configuration → secure default — and returns both the boolean answer and the
*deciding rule*, so an operator surface can always explain *why* a capability
resolved as it did.

It mirrors the Phase 4A-B "one predicate is the single source of truth" shape of
:mod:`app.jobs.stuck` (``is_job_stuck``): a **pure** decision function
(:func:`decide_capability`) that is fully deterministic and DB-free is the
primary unit-test surface, and a thin I/O wrapper (:func:`resolve_capability`)
performs at most one indexed override lookup before delegating to it.

Deny-biased posture (Phase 4A-C.2 plan §8.9, §8.14, §8.18):

* **Absence = disabled.** A missing, malformed, unknown, or tenant-mismatched
  override is treated as "no override", never as "enabled".
* **Ceiling is absolute.** An unknown/unregistered capability is ceiling-blocked
  first and can never be raised by an override or a global flag. So is
  ``scout_scheduling`` while its **environment ceiling** is engaged
  (``scout_scheduling_ceiling_lifted=False``, the shipped default; FU-267-C1, the
  operator's D-25-c ruling, option (b)): the scheduling kill switch outranks every
  workspace override and the global flag. Lifting it restores rules 2-4 unchanged.
* **Overrides only narrow, unless enable is permitted.** A ``workspace_disableable``
  capability always honors an ``enabled=False`` override; an ``enabled=True``
  override is honored only for a ``workspace_enableable`` capability (RSS is not),
  otherwise it falls to the secure default rather than enabling.
* **No error is swallowed into a silent enable.** There is no broad
  ``except``-to-disabled; the safety comes from the precedence *shape*. A failed
  lookup aborts (a 5xx for a future caller) rather than defaulting to enabled.

**Consumption, as of P6-CAP-1** (this paragraph described 4A-C, when the resolver
shipped unconsumed; that has not been true since 4B-A). Every registered capability's
enforcement now goes through this resolver, each via one per-capability helper that the
enforcement point and its reflection share:

* ``opportunity_feedback`` — LIVE since 4B-A (:mod:`app.feedback.routes`). An honored
  enable override makes that workspace's feedback endpoints serve while
  ``opportunity_feedback_enabled`` stays ``False``.
* ``scout_scheduling`` — LIVE since P6-CAP-1
  (:func:`app.scouting_requests.schedules.resolve_scheduling_capability`): schedule
  mutations, tick seeding and the tick's own gate. Since FU-267-C1 it is
  ceiling-blocked until the environment lifts the scheduling ceiling; once lifted, an
  honored enable override makes that workspace's schedules run while
  ``scout_scheduling_enabled`` stays ``False``.
* ``connector_rss`` — LIVE since P6-CAP-1
  (:func:`app.scouting_requests.connectors._rss_enabled_for`): live connector
  selection. Not ``workspace_enableable``, so only the global flag can enable it; a
  disable override (or the ceiling) turns it off for a workspace.

With all three global flags ``False``, no override row and the scheduling ceiling
engaged (the shipped configuration), every capability resolves **disabled**:
``scout_scheduling`` via the safety-ceiling rule, the other two via the
global-configuration rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import (
    CAPABILITY_REGISTRY,
    Capability,
    get_policy,
)
from app.core.config import Settings

#: Capabilities with an **environment ceiling**, each bound to the
#: :class:`~app.core.config.Settings` attribute that *lifts* it (FU-267-C1, D-25-c
#: option (b)). While that attribute is not ``True`` — it defaults to ``False`` — rule 1
#: blocks the capability in that environment, above every workspace override and the
#: global flag. Read-only, like the registry. Only scheduling has one.
_ENVIRONMENT_CEILING_LIFT_ATTRS: MappingProxyType[Capability, str] = MappingProxyType(
    {Capability.SCOUT_SCHEDULING: "scout_scheduling_ceiling_lifted"}
)


class DecisionSource(StrEnum):
    """The single precedence rule that decided a resolution (bounded, operator-safe).

    The four values are exactly the four precedence outcomes of §8.9, in order:
    the safety ceiling, an honored per-workspace override, the bound global flag,
    and the deny-biased secure default.
    """

    SAFETY_CEILING = "safety_ceiling"
    WORKSPACE_OVERRIDE = "workspace_override"
    GLOBAL_CONFIGURATION = "global_configuration"
    SECURE_DEFAULT = "secure_default"


@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    """Immutable, secret-free result of resolving one ``(capability, workspace)``.

    Carries only bounded primitives (enums, booleans, the workspace id) so the
    value is safe to render on an operator surface and safe to log. It never
    carries the override ``reason`` note, actor id, timestamps, URLs, payloads,
    tokens, or raw error text.

    Attributes:
        capability: The resolved :class:`Capability`.
        workspace_id: The workspace scope the resolution was computed for.
        effective_enabled: The single answer a future gate consumes.
        decided_by: The precedence rule that decided ``effective_enabled``.
        global_flag: The bound global ``*_enabled`` flag value at resolution time.
        has_override: Whether an honored per-workspace override was present.
        override_value: The honored override's boolean; ``None`` iff
            ``has_override`` is ``False``.
        environment_ceiling_lifted: For a capability with an environment ceiling
            (``scout_scheduling``), whether the environment had lifted it at
            resolution time (``scout_scheduling_ceiling_lifted``); ``False`` means the
            ceiling was engaged and decided (``decided_by=safety_ceiling``). ``None``
            for a capability that has no environment ceiling.
    """

    capability: Capability
    workspace_id: str
    effective_enabled: bool
    decided_by: DecisionSource
    global_flag: bool
    has_override: bool
    override_value: bool | None
    environment_ceiling_lifted: bool | None


def _environment_ceiling_lifted(settings: Settings, capability: Capability) -> bool | None:
    """Read ``capability``'s environment-ceiling setting, or ``None`` if it has none.

    Deny-biased: only a real ``True`` lifts the ceiling; ``False`` (the default) keeps
    it engaged.
    """
    attr = _ENVIRONMENT_CEILING_LIFT_ATTRS.get(capability)
    if attr is None:
        return None
    return getattr(settings, attr) is True


def _ceiling_blocks(capability: Capability, *, environment_ceiling_lifted: bool | None) -> bool:
    """Whether the safety ceiling (rule 1) hard-blocks ``capability``.

    Two inputs block, and nothing below rule 1 can raise either:

    * an unknown/unregistered capability — any value absent from
      :data:`CAPABILITY_REGISTRY`, which can only reach here via a future/forged enum
      member since the public entry point accepts a typed :class:`Capability`;
    * an engaged **environment ceiling** (FU-267-C1, D-25-c option (b)): a capability
      bound in :data:`_ENVIRONMENT_CEILING_LIFT_ATTRS` (``scout_scheduling``) is
      blocked unless ``environment_ceiling_lifted`` is ``True``. ``False`` and ``None``
      (not supplied) both block — deny-biased.

    The other registered capabilities are *eligible* to be ceiling-blocked
    (``subject_to_safety_ceiling=True``) but have no environment input; for them
    ``environment_ceiling_lifted`` is ignored.
    """
    if capability not in CAPABILITY_REGISTRY:
        return True
    if capability in _ENVIRONMENT_CEILING_LIFT_ATTRS:
        return environment_ceiling_lifted is not True
    return False


def decide_capability(
    *,
    capability: Capability,
    workspace_id: str,
    global_flag: bool,
    override_value: bool | None,
    environment_ceiling_lifted: bool | None = None,
) -> CapabilityResolution:
    """Pure, DB-free precedence decision (plan §8.9, §8.13); mirrors ``is_job_stuck``.

    Inputs are already-fetched primitives: the typed ``capability``, the bound
    ``global_flag``, ``override_value`` (already tenant-validated by the
    caller; ``None`` means "no honored override") and ``environment_ceiling_lifted``
    (the capability's environment-ceiling setting; omitted, a capability that has an
    environment ceiling is ceiling-blocked — deny-biased). Applies §8.9 rules 1→4 in
    order and constructs the immutable result. Fully deterministic — the primary
    unit-test surface, exercisable without a database.
    """
    # Reported as the setting's value for a capability with an environment ceiling
    # (a missing value reads as "not lifted"), ``None`` for one without.
    ceiling_lifted = (
        environment_ceiling_lifted is True
        if capability in _ENVIRONMENT_CEILING_LIFT_ATTRS
        else None
    )

    # Rule 1 — safety ceiling (absolute). An unknown/unregistered capability, or one
    # whose environment ceiling is engaged, can never be raised by any override or flag.
    if _ceiling_blocks(capability, environment_ceiling_lifted=environment_ceiling_lifted):
        return CapabilityResolution(
            capability=capability,
            workspace_id=workspace_id,
            effective_enabled=False,
            decided_by=DecisionSource.SAFETY_CEILING,
            global_flag=global_flag,
            has_override=False,
            override_value=None,
            environment_ceiling_lifted=ceiling_lifted,
        )

    policy = get_policy(capability)

    # Rule 2 — an honored per-workspace override. A disable override is honored for
    # any disableable capability; an enable override is honored only for an
    # enableable capability. An un-honorable override (e.g. an enable on RSS, which
    # is not workspace-enableable) is deny-biased: it does not enable — it falls
    # through to the secure default rather than to the global flag.
    if override_value is not None:
        if override_value and policy.workspace_enableable:
            return CapabilityResolution(
                capability=capability,
                workspace_id=workspace_id,
                effective_enabled=True,
                decided_by=DecisionSource.WORKSPACE_OVERRIDE,
                global_flag=global_flag,
                has_override=True,
                override_value=True,
                environment_ceiling_lifted=ceiling_lifted,
            )
        if not override_value and policy.workspace_disableable:
            return CapabilityResolution(
                capability=capability,
                workspace_id=workspace_id,
                effective_enabled=False,
                decided_by=DecisionSource.WORKSPACE_OVERRIDE,
                global_flag=global_flag,
                has_override=True,
                override_value=False,
                environment_ceiling_lifted=ceiling_lifted,
            )
        # Override present but not honorable for this capability's policy → secure
        # default (disabled). Deny-biased: an un-honorable enable never enables.
        return CapabilityResolution(
            capability=capability,
            workspace_id=workspace_id,
            effective_enabled=False,
            decided_by=DecisionSource.SECURE_DEFAULT,
            global_flag=global_flag,
            has_override=False,
            override_value=None,
            environment_ceiling_lifted=ceiling_lifted,
        )

    # Rule 3 — global configuration. With no honored override, the bound global
    # flag decides (either value).
    return CapabilityResolution(
        capability=capability,
        workspace_id=workspace_id,
        effective_enabled=global_flag,
        decided_by=DecisionSource.GLOBAL_CONFIGURATION,
        global_flag=global_flag,
        has_override=False,
        override_value=None,
        environment_ceiling_lifted=ceiling_lifted,
    )


def _load_override(
    session: Session,
    *,
    capability: Capability,
    organization_id: str,
    workspace_id: str,
) -> bool | None:
    """Load the single tenant-validated override boolean for a scope, or ``None``.

    Issues exactly one indexed query for the ``(workspace_id, capability)`` row
    (the unique constraint guarantees at most one). Returns ``None`` — treated as
    "no honored override" — when no row exists *or* when the stored
    ``organization_id`` does not match the passed scope (deny-biased tenant
    validation, plan §8.11: a cross-tenant/inconsistent row can never enable).
    """
    row = session.scalar(
        select(WorkspaceCapabilityOverride).where(
            WorkspaceCapabilityOverride.workspace_id == workspace_id,
            WorkspaceCapabilityOverride.capability == capability.value,
        )
    )
    if row is None:
        return None
    if row.organization_id != organization_id:
        return None
    return bool(row.enabled)


def resolve_capability(
    *,
    session: Session,
    settings: Settings,
    capability: Capability,
    organization_id: str,
    workspace_id: str,
) -> CapabilityResolution:
    """Resolve the effective state of ``capability`` for ``workspace_id`` (plan §8.7).

    The thin I/O wrapper around :func:`decide_capability`: short-circuits the
    safety ceiling for an unknown/forged capability, otherwise reads the bound
    global flag from ``settings`` (via the registry's ``global_flag_attr``, never
    a hardcoded flag name) and the capability's environment-ceiling setting (via
    :data:`_ENVIRONMENT_CEILING_LIFT_ATTRS`). An engaged environment ceiling decides
    without an override lookup — the kill switch never depends on override storage.
    Otherwise it loads + tenant-validates the single override row and delegates the
    branching to the pure decision function.

    Keyword-only at this safety-critical boundary. Never raises for a governance
    outcome (absence/error → disabled by precedence shape, not by exception); may
    raise only for a genuine programmer error (a non-:class:`Capability` argument
    reaching the ceiling short-circuit as an unregistered value is handled, not
    raised). Performs no write, opens no session of its own, and toggles no flag.
    """
    # Short-circuit an unknown/forged capability before touching settings or the
    # DB: it is ceiling-blocked regardless.
    if capability not in CAPABILITY_REGISTRY:
        return decide_capability(
            capability=capability,
            workspace_id=workspace_id,
            global_flag=False,
            override_value=None,
        )

    policy = get_policy(capability)
    global_flag = bool(getattr(settings, policy.global_flag_attr))
    environment_ceiling_lifted = _environment_ceiling_lifted(settings, capability)
    if _ceiling_blocks(capability, environment_ceiling_lifted=environment_ceiling_lifted):
        # The environment ceiling outranks any override, so no row is read.
        return decide_capability(
            capability=capability,
            workspace_id=workspace_id,
            global_flag=global_flag,
            override_value=None,
            environment_ceiling_lifted=environment_ceiling_lifted,
        )
    override_value = _load_override(
        session,
        capability=capability,
        organization_id=organization_id,
        workspace_id=workspace_id,
    )
    return decide_capability(
        capability=capability,
        workspace_id=workspace_id,
        global_flag=global_flag,
        override_value=override_value,
        environment_ceiling_lifted=environment_ceiling_lifted,
    )


__all__ = [
    "CapabilityResolution",
    "DecisionSource",
    "decide_capability",
    "resolve_capability",
]
