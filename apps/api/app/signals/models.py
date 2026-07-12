"""Signal models: raw signals, normalized signals, location evidence, clusters."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class _RequestScoped(UUIDPrimaryKeyMixin, TimestampMixin):
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scout_request_id: Mapped[str] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )


class RawSignal(Base, _RequestScoped):
    """As-ingested payload from a connector (kept for traceability)."""

    __tablename__ = "raw_signals"

    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    author: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language: Mapped[str | None] = mapped_column(String(12))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    raw_metadata: Mapped[dict] = mapped_column(JSON, default=dict)
    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)


class NormalizedSignal(Base, _RequestScoped):
    """Canonical signal after normalization, classification and scoring."""

    __tablename__ = "normalized_signals"

    raw_signal_id: Mapped[str] = mapped_column(
        ForeignKey("raw_signals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    cluster_id: Mapped[str | None] = mapped_column(
        ForeignKey("signal_clusters.id", ondelete="SET NULL"), index=True
    )

    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    source_url: Mapped[str | None] = mapped_column(String(1000))
    author: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    language: Mapped[str | None] = mapped_column(String(12))
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    embedding: Mapped[list] = mapped_column(JSON, default=list)

    signal_type: Mapped[str | None] = mapped_column(String(60))
    pain_point_dna: Mapped[str | None] = mapped_column(String(60))
    sentiment: Mapped[str | None] = mapped_column(String(20))

    # Noise pre-analysis gate 0..100.
    pre_analysis_score: Mapped[int] = mapped_column(Integer, default=0)
    is_noise: Mapped[bool] = mapped_column(Boolean, default=False)
    noise_reasons: Mapped[list] = mapped_column(JSON, default=list)
    is_duplicate: Mapped[bool] = mapped_column(Boolean, default=False)

    is_simulated: Mapped[bool] = mapped_column(Boolean, default=True)
    ingest_metadata: Mapped[dict] = mapped_column(JSON, default=dict)


class SignalLocationEvidence(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "signal_location_evidence"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    normalized_signal_id: Mapped[str] = mapped_column(
        ForeignKey("normalized_signals.id", ondelete="CASCADE"), index=True, nullable=False
    )
    resolved_market: Mapped[str | None] = mapped_column(String(200))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    evidence: Mapped[list] = mapped_column(JSON, default=list)
    inside_scout_area: Mapped[bool] = mapped_column(Boolean, default=False)


class SignalCluster(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "signal_clusters"

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    scout_request_id: Mapped[str] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="CASCADE"), index=True, nullable=False
    )
    label: Mapped[str] = mapped_column(String(300), nullable=False)
    centroid: Mapped[list] = mapped_column(JSON, default=list)
    size: Mapped[int] = mapped_column(Integer, default=0)
