"""Campaign Context Center models: products, audiences, competitors, brand voice,
offers, claims library, source/channel preferences, campaigns.

**Project Phase 5D campaign hardening (W21 CF-3).** ``campaigns.mode`` and
``campaigns.status`` are closed vocabularies (:class:`~app.core.enums.CampaignMode`,
:class:`~app.core.enums.CampaignStatus`), validated by the create/update schemas and by the
portable CHECKs ``ck_campaign_mode`` / ``ck_campaign_status`` (the CF-3 migration enumerated
and normalised every stored value first). Campaign location membership is the
:class:`CampaignLocation` join table (operator decision O-8,
``REC_campaign_locations_join_table_FKs_backfill``): real foreign keys, and two composite
keys that tie the campaign and the location to the **same workspace** as the row, so a
cross-workspace membership is unrepresentable in the database. The JSON ``location_ids``
array is retained for one release; the CF-3 migration back-filled the join table from it
(valid ids only, the rest dropped and reported). Keeping the two in step on writes, and
reading the join table, is the 5D build lane's (5D-W1); until it lands the API still writes
only the JSON array.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.core.enums import CampaignMode, CampaignStatus
from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


def _in_list(column: str, values) -> str:
    rendered = ", ".join(f"'{v}'" for v in sorted(str(v) for v in values))
    return f"{column} IN ({rendered})"


def campaign_mode_sql() -> str:
    return _in_list("mode", (m.value for m in CampaignMode))


def campaign_status_sql() -> str:
    return _in_list("status", (s.value for s in CampaignStatus))


class _WorkspaceScoped(UUIDPrimaryKeyMixin, TimestampMixin):
    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    brand_id: Mapped[str] = mapped_column(
        ForeignKey("brands.id", ondelete="CASCADE"), index=True, nullable=False
    )


class ProductProfile(Base, _WorkspaceScoped):
    __tablename__ = "product_profiles"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str | None] = mapped_column(Text)
    audience: Mapped[str | None] = mapped_column(Text)
    pain_points: Mapped[list] = mapped_column(JSON, default=list)
    use_cases: Mapped[list] = mapped_column(JSON, default=list)
    competitors: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    exclusion_rules: Mapped[list] = mapped_column(JSON, default=list)
    relevance_weight: Mapped[float] = mapped_column(Float, default=1.0)
    claims_rules: Mapped[dict] = mapped_column(JSON, default=dict)


class AudienceProfile(Base, _WorkspaceScoped):
    __tablename__ = "audience_profiles"

    label: Mapped[str] = mapped_column(String(200), nullable=False)  # specific, not "consumers"
    description: Mapped[str | None] = mapped_column(Text)
    demographics: Mapped[dict] = mapped_column(JSON, default=dict)
    motivations: Mapped[list] = mapped_column(JSON, default=list)
    objections: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)


class CompetitorProfile(Base, _WorkspaceScoped):
    __tablename__ = "competitor_profiles"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    website: Mapped[str | None] = mapped_column(String(500))
    known_weaknesses: Mapped[list] = mapped_column(JSON, default=list)
    notes: Mapped[str | None] = mapped_column(Text)


class BrandVoiceProfile(Base, _WorkspaceScoped):
    __tablename__ = "brand_voice_profiles"

    tone: Mapped[list] = mapped_column(JSON, default=list)
    personality: Mapped[list] = mapped_column(JSON, default=list)
    do_use: Mapped[list] = mapped_column(JSON, default=list)
    avoid: Mapped[list] = mapped_column(JSON, default=list)
    example_copy: Mapped[str | None] = mapped_column(Text)


class OfferCalendarEntry(Base, _WorkspaceScoped):
    __tablename__ = "offer_calendar"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    product_service: Mapped[str | None] = mapped_column(String(200))
    discount_amount: Mapped[float | None] = mapped_column(Float)
    percentage_discount: Mapped[float | None] = mapped_column(Float)
    sale_price: Mapped[float | None] = mapped_column(Float)
    regular_price: Mapped[float | None] = mapped_column(Float)
    promo_code: Mapped[str | None] = mapped_column(String(80))
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    eligible_location_ids: Mapped[list] = mapped_column(JSON, default=list)
    terms: Mapped[str | None] = mapped_column(Text)
    required_disclaimer: Mapped[str | None] = mapped_column(Text)
    cta: Mapped[str | None] = mapped_column(String(200))
    landing_url: Mapped[str | None] = mapped_column(String(500))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ClaimsLibraryEntry(Base, _WorkspaceScoped):
    __tablename__ = "claims_library"

    text: Mapped[str] = mapped_column(Text, nullable=False)
    # kind: approved | restricted | blocked | required_disclaimer
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    category: Mapped[str | None] = mapped_column(String(80))  # health, financial, comparative...
    risk_level: Mapped[str] = mapped_column(String(20), default="low")
    notes: Mapped[str | None] = mapped_column(Text)
    #: Soft retirement (Project Phase 5C, W21 CF-2): the claims library is a protected
    #: safety control once generation reads it, so an entry is never hard-deleted through
    #: the API; ``DELETE`` sets this, and a retired entry is no longer listed or applied.
    retired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: Jurisdiction-conditioned claim rules (Project Phase 5D, W21 CF-3): the jurisdiction
    #: codes (``app.jurisdiction.codes``) this entry applies in. NULL or an empty list means
    #: every jurisdiction - the meaning of every entry written before the column existed.
    #: The claims engine reads it from 5D on; nothing reads it yet.
    jurisdiction_codes: Mapped[list | None] = mapped_column(JSON)


class SourcePreference(Base, _WorkspaceScoped):
    __tablename__ = "source_preferences"

    source_type: Mapped[str] = mapped_column(String(60), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    config: Mapped[dict] = mapped_column(JSON, default=dict)


class ChannelPreference(Base, _WorkspaceScoped):
    __tablename__ = "channel_preferences"

    channel: Mapped[str] = mapped_column(String(60), nullable=False)  # facebook, instagram...
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    weekly_volume: Mapped[int | None] = mapped_column(Integer)


class Campaign(Base, _WorkspaceScoped):
    __tablename__ = "campaigns"
    __table_args__ = (
        CheckConstraint(campaign_mode_sql(), name="ck_campaign_mode"),
        CheckConstraint(campaign_status_sql(), name="ck_campaign_status"),
        # The target of the join table's workspace-consistency key (CF-3). ``id`` alone is
        # already unique; the pair is what the composite foreign key references.
        Index("uq_campaigns_id_workspace", "id", "workspace_id", unique=True),
    )

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    goal: Mapped[str | None] = mapped_column(Text)
    mode: Mapped[str] = mapped_column(String(40), default=CampaignMode.PER_LOCATION.value)
    #: The pre-O-8 membership list, retained for one release (O-8). It is not read-only yet:
    #: until 5D-W1 moves membership writes to :class:`CampaignLocation`, the API still
    #: writes this list (validated, P6-PLAT-8) and nothing writes the join table.
    location_ids: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(40), default=CampaignStatus.ACTIVE.value)


class CampaignLocation(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One location's membership of one campaign (Project Phase 5D, O-8; W21 CF-3).

    ``organization_id`` / ``workspace_id`` are the row's explicit tenant scope. The two
    composite foreign keys ``(campaign_id, workspace_id)`` and ``(location_id,
    workspace_id)`` reference ``(id, workspace_id)`` on ``campaigns`` and
    ``business_locations``: the database itself refuses a membership whose campaign or
    location belongs to another workspace (the workspace-consistency constraint). Every key
    cascades, so a membership goes with its campaign, its location, its workspace and its
    organization. One row per (campaign, location). A membership row is inserted or deleted,
    never updated (no ``updated_at``); it is not append-only, because removing a location
    from a campaign deletes its row.
    """

    __tablename__ = "campaign_locations"
    __table_args__ = (
        UniqueConstraint("campaign_id", "location_id", name="uq_campaign_location"),
        ForeignKeyConstraint(
            ["campaign_id", "workspace_id"],
            ["campaigns.id", "campaigns.workspace_id"],
            ondelete="CASCADE",
            name="fk_campaign_locations_campaign_workspace",
        ),
        ForeignKeyConstraint(
            ["location_id", "workspace_id"],
            ["business_locations.id", "business_locations.workspace_id"],
            ondelete="CASCADE",
            name="fk_campaign_locations_location_workspace",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    campaign_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    location_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
