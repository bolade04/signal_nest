"""Persistence model for recommendation briefs (Project Phase 5A; frozen at W21 CF-1).

A :class:`RecommendationBrief` is an **immutable, versioned** recommendation derived from
one opportunity's evidence, modeled on the :class:`~app.intelligence.records.
SignalIntelligenceRecord` discipline (Phase 5A plan §2):

* **Append-only.** No update or delete path is written, and every ORM path that would
  update or delete a row raises (unit of work, ORM UPDATE/DELETE statements and
  ``Query.update/delete``, the bulk API, upserts; :func:`app.db.append_only.append_only`).
  A mapped subclass is guarded too. Not refused: Core statements and Core upserts on the
  table, ORM statements on a bare ``Connection``, raw SQL and the database's own FK actions;
  only :mod:`app.privacy` (erasure, retention) and the development-only demo reset write that
  way, pinned by a static test whose limits ``app.db.append_only`` states. The table
  carries ``created_at`` only —
  no ``updated_at``/``onupdate``. A revised brief is a new row: ``brief_version`` is
  monotonic per opportunity (``uq_recommendation_brief_version``) and ``parent_brief_id``
  points at the brief it revises.
* **Bound to its evidence.** ``intelligence_record_id`` is the exact immutable
  intelligence record the brief was derived from; ``analysis_version`` and
  ``scoring_version`` are copied from that record at capture, never caller-supplied.
* **Full LLM provenance on the row.** ``prompt_name``, ``prompt_version``,
  ``model_identifier``, ``input_hash`` (computed in the service layer), ``trace_id`` and
  ``is_simulated``. Usage and money are **not** duplicated here: they live in the Phase 6
  usage ledger, and ``llm_usage_event_id`` references the ledger row of the call that
  produced the brief (``ON DELETE SET NULL``; NULL when no metered call produced it).
* **Authorship that survives the author.** ``created_by`` (``SET NULL``) plus the non-FK
  ``created_by_snapshot`` (:mod:`app.core.actor_snapshot`), which 5B's author≠approver rule
  reads. On account erasure the snapshot is anonymised and the row kept (O-6, as Phase 6
  encodes it for ``audit_logs``; :mod:`app.privacy.erasure`).
* **Facts vs inference.** ``evidence_refs`` holds pointers into the evidence (facts);
  ``summary``, ``rationale``, ``recommended_actions``, ``suggested_angles`` and
  ``risk_notes`` are inference. Bounds are enforced at the API boundary
  (:mod:`app.briefs.schemas`).
* **No status column.** A brief's display status (draft / superseded / approved /
  rejected) is derived at read time from the approval spine and the ``parent_brief_id``
  chain; no status vocabulary is frozen (Phase 5A plan §2).

Tenancy: ``organization_id`` / ``workspace_id`` / ``opportunity_id`` /
``intelligence_record_id`` cascade, so a brief is deleted with its workspace and is a
dependent of its opportunity and evidence for retention. ``campaign_id`` is validated
against the workspace at write time (service layer); ``location_id`` and ``campaign_id``
are ``SET NULL``. Portable types only (String/Integer/Boolean/Text/JSON; no JSONB, no
native enums).
"""

from __future__ import annotations

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.actor_snapshot import ACTOR_SNAPSHOT_MAX_LENGTH
from app.db.append_only import append_only
from app.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


@append_only
class RecommendationBrief(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One immutable version of a recommendation brief for one opportunity."""

    __tablename__ = "recommendation_briefs"
    __table_args__ = (
        UniqueConstraint(
            "workspace_id",
            "opportunity_id",
            "brief_version",
            name="uq_recommendation_brief_version",
        ),
        CheckConstraint("brief_version >= 1", name="ck_recommendation_brief_version"),
    )

    # --- Scope (isolation-preserving typed FKs) ------------------------------
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    #: The exact immutable evidence record the brief was derived from.
    intelligence_record_id: Mapped[str] = mapped_column(
        ForeignKey("signal_intelligence_records.id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_locations.id", ondelete="SET NULL"), index=True
    )
    #: Validated against the brief's workspace at write time (service layer).
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )

    # --- Versioning ------------------------------------------------------------
    #: Monotonic per (workspace, opportunity), starting at 1.
    brief_version: Mapped[int] = mapped_column(Integer, nullable=False)
    #: The brief this version revises; NULL for a first version.
    parent_brief_id: Mapped[str | None] = mapped_column(
        ForeignKey("recommendation_briefs.id", ondelete="SET NULL"), index=True
    )
    #: Copied from the intelligence record at capture — never caller-supplied.
    analysis_version: Mapped[str] = mapped_column(String(20), nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # --- LLM provenance --------------------------------------------------------
    prompt_name: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    model_identifier: Mapped[str] = mapped_column(String(100), nullable=False)
    #: SHA-256 hex of the model inputs, computed in the service layer.
    input_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    trace_id: Mapped[str | None] = mapped_column(String(64))
    is_simulated: Mapped[bool] = mapped_column(Boolean, nullable=False)
    #: The Phase 6 usage-ledger row of the call that produced this brief (tokens, cost,
    #: provider, outcome live there; nothing is duplicated here).
    llm_usage_event_id: Mapped[str | None] = mapped_column(
        ForeignKey("llm_usage_events.id", ondelete="SET NULL"), index=True
    )

    # --- Authorship ------------------------------------------------------------
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    #: Immutable "<user id> <full name> <<email>>" at capture (app.core.actor_snapshot);
    #: "[erased]" once the account is erased.
    created_by_snapshot: Mapped[str] = mapped_column(
        String(ACTOR_SNAPSHOT_MAX_LENGTH), nullable=False
    )

    # --- Content (inference) ---------------------------------------------------
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    recommended_actions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    suggested_angles: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    risk_notes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    # --- Content (facts: pointers into the evidence) ----------------------------
    evidence_refs: Mapped[list] = mapped_column(JSON, nullable=False, default=list)


__all__ = ["RecommendationBrief"]
