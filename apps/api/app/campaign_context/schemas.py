from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, create_model, field_validator

from app.core.enums import CampaignMode, CampaignStatus
from app.jurisdiction.codes import is_jurisdiction_code

#: Upper bound on a campaign's ``location_ids`` / an offer's ``eligible_location_ids``.
#: Every listed id is checked against the workspace's locations in one query (P6-PLAT-8),
#: so the list is bounded rather than left to grow without limit.
MAX_LOCATION_IDS = 500


def _unique_location_ids(value: list[str]) -> list[str]:
    if len(set(value)) != len(value):
        raise ValueError("must not list the same location twice")
    return value


class ProductIn(BaseModel):
    name: str = Field(min_length=1)
    description: str | None = None
    audience: str | None = None
    pain_points: list[str] = Field(default_factory=list)
    use_cases: list[str] = Field(default_factory=list)
    competitors: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)
    exclusion_rules: list[str] = Field(default_factory=list)
    relevance_weight: float = 1.0
    claims_rules: dict = Field(default_factory=dict)


class AudienceIn(BaseModel):
    label: str = Field(min_length=1)
    description: str | None = None
    demographics: dict = Field(default_factory=dict)
    motivations: list[str] = Field(default_factory=list)
    objections: list[str] = Field(default_factory=list)
    keywords: list[str] = Field(default_factory=list)


class CompetitorIn(BaseModel):
    name: str = Field(min_length=1)
    website: str | None = None
    known_weaknesses: list[str] = Field(default_factory=list)
    notes: str | None = None


class BrandVoiceIn(BaseModel):
    tone: list[str] = Field(default_factory=list)
    personality: list[str] = Field(default_factory=list)
    do_use: list[str] = Field(default_factory=list)
    avoid: list[str] = Field(default_factory=list)
    example_copy: str | None = None


class OfferIn(BaseModel):
    name: str = Field(min_length=1)
    product_service: str | None = None
    discount_amount: float | None = None
    percentage_discount: float | None = None
    sale_price: float | None = None
    regular_price: float | None = None
    promo_code: str | None = None
    start_date: datetime | None = None
    end_date: datetime | None = None
    eligible_location_ids: list[str] = Field(default_factory=list, max_length=MAX_LOCATION_IDS)
    terms: str | None = None
    required_disclaimer: str | None = None
    cta: str | None = None
    landing_url: str | None = None
    is_active: bool = True

    @field_validator("eligible_location_ids")
    @classmethod
    def _unique_eligible(cls, v: list[str]) -> list[str]:
        return _unique_location_ids(v)


class ClaimIn(BaseModel):
    text: str = Field(min_length=1)
    kind: str = "approved"  # approved | restricted | blocked | required_disclaimer
    category: str | None = None
    risk_level: str = "low"
    notes: str | None = None
    #: Jurisdiction-conditioned claim rules (Project Phase 5D §2, 5D-W1): the jurisdictions the
    #: entry applies in; empty (the default) means every jurisdiction, the meaning of every
    #: entry written before the field existed. Each value must be a jurisdiction code in the
    #: frozen grammar (:mod:`app.jurisdiction.codes`), else 422. Published as a plain string
    #: list; the grammar is enforced by the validator below. The claims review reads it
    #: through :func:`app.claims.engine.active_entries`.
    jurisdiction_codes: list[str] = Field(
        default_factory=list,
        description=(
            "Jurisdiction codes the entry applies in (ISO 3166-1 alpha-2, optionally '-' and "
            "an ISO 3166-2 subdivision part: GB, US-TX). Empty means every jurisdiction."
        ),
    )

    @field_validator("jurisdiction_codes")
    @classmethod
    def _jurisdiction_codes(cls, v: list[str]) -> list[str]:
        if not all(is_jurisdiction_code(code) for code in v):
            raise ValueError(
                "must list jurisdiction codes: an upper-case ISO 3166-1 alpha-2 country code, "
                "optionally '-' and a subdivision part of 1-3 letters or digits (GB, US-TX)"
            )
        return v


class SourcePrefIn(BaseModel):
    source_type: str
    enabled: bool = True
    config: dict = Field(default_factory=dict)


class ChannelPrefIn(BaseModel):
    channel: str
    enabled: bool = True
    weekly_volume: int | None = None


class CampaignIn(BaseModel):
    name: str = Field(min_length=1)
    goal: str | None = None
    #: Closed vocabularies since Project Phase 5D campaign hardening (W21 CF-3): the CF-3
    #: migration installed ``ck_campaign_mode`` / ``ck_campaign_status``, so a value outside
    #: them is refused here (422) rather than by the database. A disclosed, live narrowing:
    #: the create and update routes accepted any string before.
    mode: CampaignMode = CampaignMode.PER_LOCATION
    location_ids: list[str] = Field(default_factory=list, max_length=MAX_LOCATION_IDS)
    status: CampaignStatus = CampaignStatus.ACTIVE

    @field_validator("location_ids")
    @classmethod
    def _unique_locations(cls, v: list[str]) -> list[str]:
        return _unique_location_ids(v)


class CampaignOut(BaseModel):
    """A campaign as the dedicated campaigns router answers it (Project Phase 5D, 5D-W1).

    Projected: the row's tenant columns ``organization_id`` and ``workspace_id`` are not
    echoed (Phase 5D plan §2; exit criterion 2; live change 8(c)). ``location_ids`` is the
    campaign's membership as the ``campaign_locations`` join table holds it, in location-id
    order; the request's order is not kept (a membership is a set).
    """

    id: str
    brand_id: str
    name: str
    goal: str | None
    mode: CampaignMode
    location_ids: list[str]
    status: CampaignStatus
    created_at: datetime
    updated_at: datetime


# --------------------------------------------------------------------------- #
# Partial-update (PATCH) bodies -- P6-PLAT-9
# --------------------------------------------------------------------------- #
def _drop_defaults(schema: dict[str, Any]) -> None:
    # An omitted PATCH field means "leave unchanged", never "set to null"; publishing
    # ``default: null`` would say the opposite.
    for prop in schema.get("properties", {}).values():
        prop.pop("default", None)


class _PatchConfig(BaseModel):
    model_config = ConfigDict(json_schema_extra=_drop_defaults)


def _patch_model(create_schema: type[BaseModel], name: str) -> type[BaseModel]:
    """The PATCH body for ``create_schema``: the same fields, none required.

    Each field keeps the create schema's type and constraints, and the subclass keeps its
    validators, so a value PATCH accepts is exactly a value POST accepts. Only what the
    client sends is applied (``model_dump(exclude_unset=True)``). The omitted-field
    default is never validated, so a field whose type is not nullable (``name: str``)
    still rejects an explicit ``null``, while a nullable one (``goal: str | None``)
    accepts ``null`` to clear it. Lists and objects are replaced whole, not merged.
    """
    fields: dict[str, Any] = {}
    for field_name, info in create_schema.model_fields.items():
        annotation = (
            Annotated[(info.annotation, *info.metadata)] if info.metadata else info.annotation
        )
        fields[field_name] = (annotation, Field(default=None, description=info.description))
    return create_model(name, __base__=(create_schema, _PatchConfig), __module__=__name__, **fields)


ProductPatch = _patch_model(ProductIn, "ProductPatch")
AudiencePatch = _patch_model(AudienceIn, "AudiencePatch")
CompetitorPatch = _patch_model(CompetitorIn, "CompetitorPatch")
BrandVoicePatch = _patch_model(BrandVoiceIn, "BrandVoicePatch")
OfferPatch = _patch_model(OfferIn, "OfferPatch")
ClaimPatch = _patch_model(ClaimIn, "ClaimPatch")
SourcePrefPatch = _patch_model(SourcePrefIn, "SourcePrefPatch")
ChannelPrefPatch = _patch_model(ChannelPrefIn, "ChannelPrefPatch")
CampaignPatch = _patch_model(CampaignIn, "CampaignPatch")
