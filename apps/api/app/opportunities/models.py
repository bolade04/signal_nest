"""Opportunity models: opportunities, score breakdowns, validation evidence, sightings."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.enums import OpportunityStatus
from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

#: The finding-identity rule's version prefix (W16 customer repairs, PROPOSED): a key is
#: ``f2:`` + SHA-256 over the sorted identities of the card's member evidence items, each
#: including the market it was collected for (contract §J.1). ``f1`` (the rule without the
#: market) was never released.
FINDING_KEY_PREFIX = "f2:"


class Opportunity(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "opportunities"
    __table_args__ = (
        # W16 customer repairs (PROPOSED): one current card per finding in a Scout. A card
        # created before the finding identity existed has no key (NULL) and never conflicts.
        Index(
            "uq_opportunities_finding",
            "organization_id",
            "workspace_id",
            "scout_request_id",
            "finding_key",
            unique=True,
            postgresql_where=text("finding_key IS NOT NULL"),
            sqlite_where=text("finding_key IS NOT NULL"),
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    brand_id: Mapped[str] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scout_request_id: Mapped[str] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_locations.id", ondelete="SET NULL"), index=True
    )
    campaign_id: Mapped[str | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL"), index=True
    )
    cluster_id: Mapped[str | None] = mapped_column(
        ForeignKey("signal_clusters.id", ondelete="SET NULL"), index=True
    )

    title: Mapped[str] = mapped_column(String(300), nullable=False)
    classification: Mapped[str] = mapped_column(String(40), nullable=False)
    decision: Mapped[str] = mapped_column(String(40), nullable=False)

    opportunity_score: Mapped[int] = mapped_column(Integer, default=0)
    confidence_score: Mapped[int] = mapped_column(Integer, default=0)
    confidence_level: Mapped[str] = mapped_column(String(20), default="low")
    priority_score: Mapped[int] = mapped_column(Integer, default=0)
    relevance_score: Mapped[int] = mapped_column(Integer, default=0)
    risk_level: Mapped[str] = mapped_column(String(20), default="low")

    resolved_market: Mapped[str | None] = mapped_column(String(200))
    inside_scout_area: Mapped[bool] = mapped_column(Boolean, default=True)

    # Explanation engine output (observed / inference / action are separated).
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    who_cares: Mapped[str | None] = mapped_column(Text)
    observed_evidence: Mapped[list] = mapped_column(JSON, default=list)
    ai_inference: Mapped[str | None] = mapped_column(Text)
    recommended_action: Mapped[str | None] = mapped_column(Text)
    suggested_angles: Mapped[list] = mapped_column(JSON, default=list)
    risk_note: Mapped[str | None] = mapped_column(Text)
    claims_warnings: Mapped[list] = mapped_column(JSON, default=list)
    audience_fit: Mapped[str | None] = mapped_column(String(300))
    urgency: Mapped[str | None] = mapped_column(String(40))
    commercial_value: Mapped[str | None] = mapped_column(String(40))
    source_summary: Mapped[list] = mapped_column(JSON, default=list)
    # ExplanationSource value (P6-LLM-5); NULL on rows created before the marker.
    explanation_source: Mapped[str | None] = mapped_column(String(32))

    status: Mapped[str] = mapped_column(String(40), default=OpportunityStatus.NEW.value)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    #: The finding's identity within its Scout (W16 customer repairs, PROPOSED): ``f2:`` +
    #: SHA-256 over the sorted ``(source_type, source identity, content_hash, market)`` of its
    #: member evidence items, computed from their stored rows (``app.opportunities.identity``). NULL
    #: on a card created before the identity existed and not (yet) adopted by a later run.
    finding_key: Mapped[str | None] = mapped_column(String(72))


class OpportunityScore(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Explainable per-factor score breakdown (opportunity + confidence)."""

    __tablename__ = "opportunity_scores"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)  # opportunity | confidence
    breakdown: Mapped[dict] = mapped_column(JSON, default=dict)  # factor -> {weight, value, points}
    total: Mapped[int] = mapped_column(Integer, default=0)


class ValidationEvidence(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "validation_evidence"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False)
    weight: Mapped[float] = mapped_column(Float, default=0.0)
    source_url: Mapped[str | None] = mapped_column(String(1000))


class OpportunityObservation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One sighting of a finding by one run (W16 customer repairs, PROPOSED). INSERT-only.

    ``first_seen``: the run created the card. ``seen_again``: a later run produced the same
    finding, so it added this row instead of another card. ``cluster_id`` is that run's own
    cluster, whose normalized signals (and their intelligence records) are the run's members.
    Ids, a kind and instants only - no content.
    """

    __tablename__ = "opportunity_observations"
    __table_args__ = (
        CheckConstraint(
            "kind IN ('first_seen', 'seen_again')", name="ck_opportunity_observation_kind"
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scout_request_id: Mapped[str] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    opportunity_id: Mapped[str] = mapped_column(
        ForeignKey("opportunities.id", ondelete="CASCADE"), index=True, nullable=False
    )
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    cluster_id: Mapped[str | None] = mapped_column(
        ForeignKey("signal_clusters.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
