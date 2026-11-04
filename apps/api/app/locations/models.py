"""Business location + geo coverage models (multi-location, global + local overrides).

**Project Phase 5D jurisdiction axis (W21 CF-3; this module joined the CF-3 payload, plan
§6.2.0).** ``business_locations.jurisdiction_code`` and ``geo_coverage_rules.jurisdiction_code``
are additive, nullable normalised codes (:mod:`app.jurisdiction.codes`) beside the free-text
``country`` / ``state`` fields, which are kept. ``NULL`` means "no resolution was ever
attempted or captured"; the resolver's ``UNRESOLVED`` is never stored (the CHECK refuses it).
Nothing writes or reads either column yet: the resolver and its consumers are lane 5D-W2's.
``uq_business_locations_id_workspace`` is the target of the campaign-membership table's
workspace-consistency key (:class:`app.campaign_context.models.CampaignLocation`).
"""

from __future__ import annotations

from sqlalchemy import Boolean, CheckConstraint, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from app.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin
from app.jurisdiction.codes import JURISDICTION_CODE_MAX_LENGTH, jurisdiction_code_check_sql


class BusinessLocation(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "business_locations"
    __table_args__ = (
        CheckConstraint(
            jurisdiction_code_check_sql(nullable=True),
            name="ck_business_location_jurisdiction_code",
        ),
        Index("uq_business_locations_id_workspace", "id", "workspace_id", unique=True),
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

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    address: Mapped[str | None] = mapped_column(String(400))
    city: Mapped[str | None] = mapped_column(String(160))
    state_province: Mapped[str | None] = mapped_column(String(160))
    country: Mapped[str | None] = mapped_column(String(120))
    postal_code: Mapped[str | None] = mapped_column(String(40))
    latitude: Mapped[float | None] = mapped_column(Float)
    longitude: Mapped[float | None] = mapped_column(Float)
    timezone: Mapped[str | None] = mapped_column(String(60))
    currency: Mapped[str | None] = mapped_column(String(10))
    languages: Mapped[list] = mapped_column(JSON, default=list)

    local_phone: Mapped[str | None] = mapped_column(String(60))
    local_website_page: Mapped[str | None] = mapped_column(String(500))
    local_social_links: Mapped[dict] = mapped_column(JSON, default=dict)
    google_business_profile: Mapped[str | None] = mapped_column(String(500))
    local_competitors: Mapped[list] = mapped_column(JSON, default=list)
    local_notes: Mapped[str | None] = mapped_column(Text)

    # Per-location override map: keys that override global brand settings.
    overrides: Mapped[dict] = mapped_column(JSON, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    #: Normalised jurisdiction of this location (Project Phase 5D, W21 CF-3); NULL until a
    #: resolution is captured.
    jurisdiction_code: Mapped[str | None] = mapped_column(String(JURISDICTION_CODE_MAX_LENGTH))


class GeoCoverageRule(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Scout Reach configuration for a location (radius / markets / exclusions)."""

    __tablename__ = "geo_coverage_rules"
    __table_args__ = (
        CheckConstraint(
            jurisdiction_code_check_sql(nullable=True),
            name="ck_geo_coverage_rule_jurisdiction_code",
        ),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    location_id: Mapped[str | None] = mapped_column(
        ForeignKey("business_locations.id", ondelete="CASCADE"), index=True
    )

    coverage_type: Mapped[str] = mapped_column(String(40), nullable=False, default="radius")
    business_address: Mapped[str | None] = mapped_column(String(400))
    center_latitude: Mapped[float | None] = mapped_column(Float)
    center_longitude: Mapped[float | None] = mapped_column(Float)
    radius_miles: Mapped[int | None] = mapped_column(Integer)  # 1..200
    country: Mapped[str | None] = mapped_column(String(120))
    state: Mapped[str | None] = mapped_column(String(160))
    included_markets: Mapped[list] = mapped_column(JSON, default=list)
    excluded_markets: Mapped[list] = mapped_column(JSON, default=list)
    online_global: Mapped[bool] = mapped_column(Boolean, default=False)
    #: Normalised jurisdiction this rule is scoped to (Project Phase 5D, W21 CF-3); NULL
    #: until captured.
    jurisdiction_code: Mapped[str | None] = mapped_column(String(JURISDICTION_CODE_MAX_LENGTH))
