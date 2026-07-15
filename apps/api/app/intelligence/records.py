"""Persistence model for evidence-backed opportunity intelligence (Batch 4A).

This is the *only* SQLAlchemy surface in ``app/intelligence``; the domain models in
``models.py`` stay framework-free dataclasses. A :class:`SignalIntelligenceRecord`
is the durable, scoped, immutable, version-aware form of a Batch 3
``OpportunityCandidate``:

* **Immutable / version-aware** — a re-score under a new ``analysis_version`` /
  ``scoring_version`` writes a *new* row (no destructive overwrite, no mutable
  ``is_current`` flag). Every row is interpretable against the versions it carries.
* **Concurrency-safe idempotency** — the unique constraint on
  ``(workspace_id, normalized_signal_id, analysis_version, scoring_version,
  fingerprint)`` is the *final* guard against duplicate persistence under retry or
  concurrent workers (see ``persistence.persist_intelligence``).
* **Isolation-preserving** — typed FK scope columns
  (organization/workspace/scout-request/normalized-signal, and the later-attached
  opportunity/location) keep every row bound to exactly one tenant scope.

Facts and inference are stored in *separate* bounded JSON payloads so the Batch 3
fact-vs-inference discipline survives on disk. Types are portable across SQLite
(local) and PostgreSQL (full): ``String`` UUIDs, ``JSON`` (never ``JSONB``).
"""

from __future__ import annotations

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class SignalIntelligenceRecord(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Durable, scoped, immutable record of a signal's intelligence analysis."""

    __tablename__ = "signal_intelligence_records"
    __table_args__ = (
        # Final concurrency guard: idempotent persistence relies on the DB, not an
        # app-level check-then-insert. One row per signal per analysis identity.
        UniqueConstraint(
            "workspace_id",
            "normalized_signal_id",
            "analysis_version",
            "scoring_version",
            "fingerprint",
            name="uq_signal_intelligence_identity",
        ),
    )

    # --- Scope (isolation-preserving typed FKs) ------------------------------
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scout_request_id: Mapped[str] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    normalized_signal_id: Mapped[str] = mapped_column(
        ForeignKey("normalized_signals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Attached later, once the cluster's opportunity exists (nullable by design).
    opportunity_id: Mapped[str | None] = mapped_column(
        ForeignKey("opportunities.id", ondelete="SET NULL"), index=True
    )
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_locations.id", ondelete="SET NULL"), index=True
    )

    # --- Identity / versions -------------------------------------------------
    analysis_version: Mapped[str] = mapped_column(String(20), nullable=False)
    scoring_version: Mapped[str] = mapped_column(String(20), nullable=False)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    enricher: Mapped[str] = mapped_column(String(40), nullable=False)

    # --- Outcome (typed, queryable) ------------------------------------------
    accepted: Mapped[bool] = mapped_column(Boolean, nullable=False)
    classification: Mapped[str] = mapped_column(String(40), nullable=False)
    decision: Mapped[str | None] = mapped_column(String(40))
    rejection_reason: Mapped[str | None] = mapped_column(String(40))
    cluster_key: Mapped[str] = mapped_column(String(120), nullable=False, default="general")
    score_total: Mapped[int] = mapped_column(Integer, default=0)
    evidence_count: Mapped[int] = mapped_column(Integer, default=0)
    rationale: Mapped[str | None] = mapped_column(Text)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)

    # --- Bounded payloads (facts vs inference kept separate) -----------------
    facts: Mapped[dict] = mapped_column(JSON, default=dict)
    inference: Mapped[dict] = mapped_column(JSON, default=dict)
    relevance: Mapped[dict] = mapped_column(JSON, default=dict)
    score_components: Mapped[dict] = mapped_column(JSON, default=dict)
    provenance: Mapped[dict] = mapped_column(JSON, default=dict)
