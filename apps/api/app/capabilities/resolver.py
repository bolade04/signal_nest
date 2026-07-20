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
  first and can never be raised by an override or a global flag.
* **Overrides only narrow, unless enable is permitted.** A ``workspace_disableable``
  capability always honors an ``enabled=False`` override; an ``enabled=True``
  override is honored only for a ``workspace_enableable`` capability (RSS is not),
  otherwise it falls to the secure default rather than enabling.
* **No error is swallowed into a silent enable.** There is no broad
  ``except``-to-disabled; the safety comes from the precedence *shape*. A failed
  lookup aborts (a 5xx for a future caller) rather than defaulting to enabled.

This batch ships the resolver **unconsumed**: no feedback, scheduling, or RSS
gate imports or calls it, no global flag is flipped, and the override table stays
empty — so every capability in every workspace resolves **disabled** via the
global-configuration rule. Every capability remains dark.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import (
    CAPABILITY_REGISTRY,
    Capability,
    get_policy,
)
from app.core.config import Settings


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
    """

    capability: Capability
    workspace_id: str
    effective_enabled: bool
    decided_by: DecisionSource
    global_flag: bool
    has_override: bool
    override_value: bool | None


def _ceiling_blocks(capability: Capability) -> bool:
    """Whether the safety ceiling (rule 1) hard-blocks ``capability``.

    In this batch the ceiling blocks only an unknown/unregistered capability —
    any value absent from :data:`CAPABILITY_REGISTRY`, which can only reach here
    via a future/forged enum member since the public entry point accepts a typed
    :class:`Capability`. Every registered capability is *eligible* to be
    ceiling-blocked (``subject_to_safety_ceiling=True``) but none is
    environment-blocked by default yet; the rule-1 slot is reserved so such a
    signal can be added later without reordering precedence (plan §8.10, §8.28 Q2).
    """
    return capability not in CAPABILITY_REGISTRY


def decide_capability(
    *,
    capability: Capability,
    workspace_id: str,
    global_flag: bool,
    override_value: bool | None,
) -> CapabilityResolution:
    """Pure, DB-free precedence decision (plan §8.9, §8.13); mirrors ``is_job_stuck``.

    Inputs are already-fetched primitives: the typed ``capability``, the bound
    ``global_flag``, and ``override_value`` (already tenant-validated by the
    caller; ``None`` means "no honored override"). Applies §8.9 rules 1→4 in
    order and constructs the immutable result. Fully deterministic — the primary
    unit-test surface, exercisable without a database.
    """
    # Rule 1 — safety ceiling (absolute). An unknown/unregistered capability can
    # never be raised by any override or flag.
    if _ceiling_blocks(capability):
        return CapabilityResolution(
            capability=capability,
            workspace_id=workspace_id,
            effective_enabled=False,
            decided_by=DecisionSource.SAFETY_CEILING,
            global_flag=global_flag,
            has_override=False,
            override_value=None,
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
    a hardcoded flag name), loads + tenant-validates the single override row, and
    delegates the branching to the pure decision function.

    Keyword-only at this safety-critical boundary. Never raises for a governance
    outcome (absence/error → disabled by precedence shape, not by exception); may
    raise only for a genuine programmer error (a non-:class:`Capability` argument
    reaching the ceiling short-circuit as an unregistered value is handled, not
    raised). Performs no write, opens no session of its own, and toggles no flag.
    """
    # Short-circuit an unknown/forged capability before touching settings or the
    # DB: it is ceiling-blocked regardless.
    if _ceiling_blocks(capability):
        return decide_capability(
            capability=capability,
            workspace_id=workspace_id,
            global_flag=False,
            override_value=None,
        )

    policy = get_policy(capability)
    global_flag = bool(getattr(settings, policy.global_flag_attr))
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
    )


__all__ = [
    "CapabilityResolution",
    "DecisionSource",
    "decide_capability",
    "resolve_capability",
]
