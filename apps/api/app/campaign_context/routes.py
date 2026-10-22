"""Campaign Context Center endpoints.

A compact generic factory produces list/create/update/delete routes for each
workspace-scoped context entity. All routes enforce tenant scoping via
``get_tenant_context`` and attach ``organization_id``/``workspace_id``/``brand_id``
server-side; the update route (PATCH, P6-PLAT-9) changes only the fields the client sends
and never those three or the row's ``id``. Campaign and offer location lists are checked
against the workspace's own active locations on create and update (P6-PLAT-8,
:mod:`app.campaign_context.location_refs`).
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.brands.service import get_primary_brand
from app.campaign_context.location_refs import validate_location_ids
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
    AudiencePatch,
    BrandVoiceIn,
    BrandVoicePatch,
    CampaignIn,
    CampaignPatch,
    ChannelPrefIn,
    ChannelPrefPatch,
    ClaimIn,
    ClaimPatch,
    CompetitorIn,
    CompetitorPatch,
    OfferIn,
    OfferPatch,
    ProductIn,
    ProductPatch,
    SourcePrefIn,
    SourcePrefPatch,
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


def _get_scoped(db: Session, model: type, workspace_id: str, item_id: str) -> Any:
    obj = db.get(model, item_id)
    if not obj or obj.workspace_id != workspace_id:
        raise NotFoundError("Item not found in this workspace.")
    return obj


def register_context_entity(
    *,
    path: str,
    model: type,
    schema: type[BaseModel],
    patch_schema: type[BaseModel],
    tag: str,
    location_field: str | None = None,
) -> None:
    """Attach list/create/update/delete routes for a workspace-scoped context entity.

    ``location_field`` names the entity's list of location ids, if it has one; every
    write of that list goes through :func:`validate_location_ids`.
    """

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
        values = body.model_dump()
        if location_field:
            validate_location_ids(db, workspace_id, values[location_field])
        obj = model(
            organization_id=ctx.organization.id,
            workspace_id=workspace_id,
            brand_id=brand.id,
            **values,
        )
        db.add(obj)
        db.flush()
        return _serialize(obj)

    # ``from __future__ import annotations`` would stringify a ``body: schema`` hint
    # into an unresolvable ForwardRef; bind the concrete class object directly so
    # FastAPI can build request/response models and OpenAPI.
    _create.__annotations__["body"] = schema

    @router.patch(
        f"/workspaces/{{workspace_id}}/{path}/{{item_id}}",
        name=f"update_{path}",
        tags=[tag],
    )
    def _update(
        workspace_id: str,
        item_id: str,
        body,  # annotation bound below, as for ``_create``
        db: Session = Depends(get_db),
        ctx: TenantContext = Depends(EDITORS),
    ) -> dict:
        obj = _get_scoped(db, model, workspace_id, item_id)
        changes = body.model_dump(exclude_unset=True)
        if location_field and location_field in changes:
            stored = getattr(obj, location_field)
            validate_location_ids(
                db,
                workspace_id,
                changes[location_field],
                already_stored=stored if isinstance(stored, list) else (),
            )
        for key, value in changes.items():
            setattr(obj, key, value)
        db.add(obj)
        db.flush()
        return _serialize(obj)

    _update.__annotations__["body"] = patch_schema

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
        db.delete(_get_scoped(db, model, workspace_id, item_id))


# (path, model, create schema, patch schema, tag, location-id list field)
_ENTITIES: list[tuple[str, type, type[BaseModel], type[BaseModel], str, str | None]] = [
    ("products", ProductProfile, ProductIn, ProductPatch, "products", None),
    ("audiences", AudienceProfile, AudienceIn, AudiencePatch, "audiences", None),
    ("competitors", CompetitorProfile, CompetitorIn, CompetitorPatch, "competitors", None),
    ("brand-voice", BrandVoiceProfile, BrandVoiceIn, BrandVoicePatch, "brand-voice", None),
    ("offers", OfferCalendarEntry, OfferIn, OfferPatch, "offers", "eligible_location_ids"),
    ("claims", ClaimsLibraryEntry, ClaimIn, ClaimPatch, "claims", None),
    ("source-preferences", SourcePreference, SourcePrefIn, SourcePrefPatch, "sources", None),
    ("channel-preferences", ChannelPreference, ChannelPrefIn, ChannelPrefPatch, "channels", None),
    ("campaigns", Campaign, CampaignIn, CampaignPatch, "campaigns", "location_ids"),
]

for _path, _model, _schema, _patch, _tag, _location_field in _ENTITIES:
    register_context_entity(
        path=_path,
        model=_model,
        schema=_schema,
        patch_schema=_patch,
        tag=_tag,
        location_field=_location_field,
    )
