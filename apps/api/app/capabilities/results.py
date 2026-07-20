"""Typed result models for the governed capability override service (Phase 4A-C.3).

Immutable, ``slots=True`` result types the override service returns and a future
route batch (4A-C.4) serializes (plan §8.14). Defining them now — as pure types,
ahead of the service logic (4A-C.3.2+) — keeps the later read/set/clear and route
batches thin.

This batch (4A-C.3.1) ships **types only**: nothing constructs these yet.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability


@dataclass(frozen=True, slots=True)
class OverrideMutation:
    """Immutable, secret-free summary of one ``set``/``clear`` mutation (plan §8.14).

    Carries only bounded primitives so the value is safe to render on an operator
    surface and safe to log. It never carries the override ``reason`` note, the
    actor id, or timestamps — those live on the ORM row and the audit log, not this
    summary.

    Attributes:
        capability: The mutated :class:`Capability`.
        workspace_id: The workspace scope the mutation applied to.
        created: Whether a new override row was inserted (``False`` for an in-place
            update, an idempotent no-op, or a clear).
        changed: Whether the stored state actually changed (``False`` for an
            idempotent no-op ``set`` or a ``clear`` of an absent override).
        enabled: The resulting stored override value, or ``None`` after a clear
            (no override remains).
        override_id: The surviving override row's id, or ``None`` after a clear.
    """

    capability: Capability
    workspace_id: str
    created: bool
    changed: bool
    enabled: bool | None
    override_id: str | None


@dataclass(frozen=True, slots=True)
class OverridePage:
    """Immutable page of override rows for a single workspace (plan §8.14, §8.8).

    The list read-back a future route serializes. Scoped strictly to one workspace;
    ordering and clamping are the service's responsibility.

    Attributes:
        items: The page of override rows (newest first).
        total: Total matching rows across all pages.
        limit: The clamped page size that produced ``items``.
        offset: The clamped offset that produced ``items``.
    """

    items: tuple[WorkspaceCapabilityOverride, ...]
    total: int
    limit: int
    offset: int


__all__ = [
    "OverrideMutation",
    "OverridePage",
]
