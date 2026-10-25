"""LLM usage ledger (P6-LLM-4): one row per provider attempt, per workspace.

Holds counts and money only - never prompt text, scraped content or model output -
so it carries no customer content into retention scope beyond the workspace link.
Rows cascade with their workspace and organization.
"""

from __future__ import annotations

from sqlalchemy import Float, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class LLMUsageEvent(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "llm_usage_events"
    __table_args__ = (Index("ix_llm_usage_events_workspace_created", "workspace_id", "created_at"),)

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    scout_request_id: Mapped[str | None] = mapped_column(
        ForeignKey("scout_requests.id", ondelete="SET NULL"), index=True
    )
    #: The durable job whose attempt made the call (NULL off the worker path). A run's
    #: call cap counts this job's rows only.
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )
    task: Mapped[str] = mapped_column(String(64), nullable=False)
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    #: ``ok`` or the normalized error class name of a failed attempt.
    outcome: Mapped[str] = mapped_column(String(64), nullable=False)
