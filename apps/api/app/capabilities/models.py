"""Persistence model for per-workspace capability overrides (Phase 4A-C.1).

A :class:`WorkspaceCapabilityOverride` row records an operator's durable *intent*
to enable or disable exactly one governed capability for exactly one workspace. It
is the storage foundation for governed activation — a future Phase 4B activation
can be scoped to a single workspace instead of flipping a global switch for every
tenant at once.

Foundation-only semantics (4A-C.1):

* **Intent, not activation.** Persisting a row (even ``enabled=True``) never flips
  a global flag and, in this batch, is consumed by nothing: no resolver reads it
  and no live feature gate consults it. The three global capability flags remain
  ``False`` and the subsystem ships fully dark.
* **Closed vocabulary.** ``capability`` stores a registry ``.value``; a portable
  ``CheckConstraint`` derived from :func:`app.capabilities.registry.persisted_values`
  restricts the column to the closed registry set, so the *storable* set can never
  drift from the *governable* set.
* **One override per (workspace, capability).** A unique constraint enables
  idempotent upsert semantics for the later override service (4A-C.3): setting an
  override is insert-or-update in place, never a duplicate row.
* **Isolation-preserving.** Typed FK scope columns bind every row to one tenant;
  retention follows the workspace/organization deletion lifecycle (``CASCADE``).
  Actor attribution is ``SET NULL`` so deleting a user forgets *who* set the
  override without destroying the override itself. Organization/workspace
  consistency is a service-layer concern (deferred to 4A-C.3): the workspaces
  table carries no ``(id, organization_id)`` unique key, so a composite tenant FK
  is not available here — this mirrors the ``opportunity_feedback`` precedent of
  independent scope FKs.

Types are portable across SQLite (local) and PostgreSQL (full): ``String`` UUIDs,
``Boolean``, a ``String`` capability code (never a native enum), ``Text`` for the
optional operator note, and a portable ``CheckConstraint``.
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.capabilities.registry import persisted_values
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _capability_in_registry_sql() -> str:
    """Render the closed capability set as a portable ``IN`` guard.

    Derived from the single registry source so the storable set is exactly the
    governable set. Deterministic (sorted) so the migration renders identically
    on every regeneration.
    """
    values = ", ".join(f"'{value}'" for value in persisted_values())
    return f"capability IN ({values})"


class WorkspaceCapabilityOverride(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One operator-recorded override of a capability for a single workspace."""

    __tablename__ = "workspace_capability_overrides"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "capability",
            name="uq_workspace_capability_override",
        ),
        CheckConstraint(
            _capability_in_registry_sql(),
            name="ck_workspace_capability_override_capability",
        ),
    )

    # --- Scope (isolation-preserving typed FKs) ------------------------------
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # --- Governed intent -----------------------------------------------------
    #: The governed capability's registry ``.value``; DB-restricted to the closed
    #: registry set by the check constraint above.
    capability: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The override value: True = intent to enable, False = intent to force off.
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False)

    # --- Attribution / provenance --------------------------------------------
    # SET NULL (not CASCADE) so deleting a user forgets who set the override
    # without destroying the override itself (mirrors feedback attribution).
    set_by_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: Optional, non-secret operator note explaining the override.
    reason: Mapped[str | None] = mapped_column(Text)
