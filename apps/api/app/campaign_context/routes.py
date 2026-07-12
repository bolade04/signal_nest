"""Campaign Context Center endpoints.

A compact generic factory produces list/create/delete routes for each workspace-scoped
context entity. All routes enforce tenant scoping via ``get_tenant_context`` and attach
``organization_id``/``workspace_id``/``brand_id`` server-side.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.brands.service import get_primary_brand
from app.campaign_context.models import (
    AudienceProfile,
    BrandVoiceProfile,
    Campaign,
    ChannelPreference,
    ClaimsLibraryEntry,
    CompetitorProfile,
    OfferCalendarEntry,
    ProductProfile,
    SourcePreference,
)
from app.campaign_context.schemas import (
    AudienceIn,
    BrandVoiceIn,
    CampaignIn,
    ChannelPrefIn,
    ClaimIn,
    CompetitorIn,
    OfferIn,
    ProductIn,
    SourcePrefIn,
)
from app.core.enums import Role
from app.core.errors import NotFoundError, ValidationDomainError
from app.db.session import get_db

router = APIRouter(tags=["campaign-context"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)


def _serialize(obj: Any) -> dict:
    data = {}
    for col in obj.__table__.columns:
        value = getattr(obj, col.name)
        data[col.name] = value
    return data


def register_context_entity(
    *,
    path: str,
    model: type,
    schema: type[BaseModel],
    tag: str,
) -> None:
    """Attach list/create/delete routes for a workspace-scoped context entity."""

    @router.get(f"/workspaces/{{workspace_id}}/{path}", name=f"list_{path}", tags=[tag])
    def _list(
        workspace_id: str,
        db: Session = Depends(get_db),
        ctx: TenantContext = Depends(get_tenant_context),
    ) -> list[dict]:
        rows = db.execute(
            select(model).where(model.workspace_id == workspace_id).order_by(model.created_at)
        ).scalars()
        return [_serialize(r) for r in rows]

    @router.post(
        f"/workspaces/{{workspace_id}}/{path}",
        status_code=201,
        name=f"create_{path}",
        tags=[tag],
    )
    def _create(
        workspace_id: str,
        body,  # annotation bound below to the concrete schema class
        db: Session = Depends(get_db),
        ctx: TenantContext = Depends(EDITORS),
    ) -> dict:
        brand = get_primary_brand(db, workspace_id)
        if not brand:
            raise ValidationDomainError("Complete onboarding before adding context.")
        obj = model(
            organization_id=ctx.organization.id,
            workspace_id=workspace_id,
            brand_id=brand.id,
            **body.model_dump(),
        )
        db.add(obj)
        db.flush()
        return _serialize(obj)

    # ``from __future__ import annotations`` would stringify a ``body: schema`` hint
    # into an unresolvable ForwardRef; bind the concrete class object directly so
    # FastAPI can build request/response models and OpenAPI.
    _create.__annotations__["body"] = schema

    @router.delete(
        f"/workspaces/{{workspace_id}}/{path}/{{item_id}}",
        status_code=204,
        name=f"delete_{path}",
        tags=[tag],
    )
    def _delete(
        workspace_id: str,
        item_id: str,
        db: Session = Depends(get_db),
        ctx: TenantContext = Depends(EDITORS),
    ) -> None:
        obj = db.get(model, item_id)
        if not obj or obj.workspace_id != workspace_id:
            raise NotFoundError("Item not found in this workspace.")
        db.delete(obj)


_ENTITIES: list[tuple[str, type, type[BaseModel], str]] = [
    ("products", ProductProfile, ProductIn, "products"),
    ("audiences", AudienceProfile, AudienceIn, "audiences"),
    ("competitors", CompetitorProfile, CompetitorIn, "competitors"),
    ("brand-voice", BrandVoiceProfile, BrandVoiceIn, "brand-voice"),
    ("offers", OfferCalendarEntry, OfferIn, "offers"),
    ("claims", ClaimsLibraryEntry, ClaimIn, "claims"),
    ("source-preferences", SourcePreference, SourcePrefIn, "sources"),
    ("channel-preferences", ChannelPreference, ChannelPrefIn, "channels"),
    ("campaigns", Campaign, CampaignIn, "campaigns"),
]

for _path, _model, _schema, _tag in _ENTITIES:
    register_context_entity(path=_path, model=_model, schema=_schema, tag=_tag)
