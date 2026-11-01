"""Campaign Context Center endpoints.

A compact generic factory produces list/create/update/delete routes for each
workspace-scoped context entity. All routes enforce tenant scoping via
``get_tenant_context`` and attach ``organization_id``/``workspace_id``/``brand_id``
server-side; the update route (PATCH, P6-PLAT-9) changes only the fields the client sends
and never those three or the row's ``id``. Campaign and offer location lists are checked
against the workspace's own active locations on create and update (P6-PLAT-8,
:mod:`app.campaign_context.location_refs`).

**The claims library is a protected safety control** (Project Phase 5C plan §2, W21 CF-2):
5C's output-side review blocks a generated draft on the workspace's ``blocked`` claims, so a
marketer whose draft is blocked must not be able to remove the claim that blocks it. For
``/claims`` only:

* create, update and delete are gated by ``require_exact_roles(OWNER, ADMIN,
  COMPLIANCE_REVIEWER)`` — **marketers lose claims write access** (a disclosed, live
  behaviour change; every other collection keeps the ``OWNER``/``ADMIN``/``MARKETER``
  editors);
* ``DELETE`` soft-retires the entry (``retired_at``) instead of deleting it; a retired entry
  is no longer listed, updated or retired again (404, as a missing one);
* every create, update and retirement writes an audit record
  (``claims_library_entry.created`` / ``.updated`` / ``.retired``) naming the changed field
  names, never their values.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Query, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
from app.audit.service import record_audit
from app.auth.dependencies import (
    TenantContext,
    get_tenant_context,
    require_exact_roles,
    require_role,
)
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
from app.core.pagination import MAX_PAGE_SIZE, PAGINATION_RESPONSES, paginate
from app.db.clock import database_now
from app.db.session import get_db

router = APIRouter(tags=["campaign-context"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)
#: The claims library's stewards (W21 CF-2): exact roles, no rank inheritance.
CLAIMS_STEWARD_ROLES: frozenset[Role] = frozenset(
    {Role.OWNER, Role.ADMIN, Role.COMPLIANCE_REVIEWER}
)
CLAIMS_STEWARDS = require_exact_roles(*sorted(CLAIMS_STEWARD_ROLES))


@dataclass(frozen=True)
class ProtectedCollection:
    """A collection whose writes are a safety control: exact-role writers, soft retirement
    (the model has ``retired_at``) and audited mutations."""

    writers: Callable[..., TenantContext]
    entity_type: str
    created: AuditAction
    updated: AuditAction
    retired: AuditAction


CLAIMS_PROTECTION = ProtectedCollection(
    writers=CLAIMS_STEWARDS,
    entity_type="claims_library_entry",
    created=AuditAction.CLAIMS_LIBRARY_ENTRY_CREATED,
    updated=AuditAction.CLAIMS_LIBRARY_ENTRY_UPDATED,
    retired=AuditAction.CLAIMS_LIBRARY_ENTRY_RETIRED,
)


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
    # A retired (soft-deleted) entry of a protected collection is gone for every route.
    if getattr(obj, "retired_at", None) is not None:
        raise NotFoundError("Item not found in this workspace.")
    return obj


def _audit(
    db: Session,
    ctx: TenantContext,
    protection: ProtectedCollection,
    action: AuditAction,
    obj: Any,
    fields: list[str],
) -> None:
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=obj.workspace_id,
        actor_user_id=ctx.user.id,
        action=action,
        entity_type=protection.entity_type,
        entity_id=obj.id,
        context={"fields": sorted(fields)},
    )


def register_context_entity(
    *,
    path: str,
    model: type,
    schema: type[BaseModel],
    patch_schema: type[BaseModel],
    tag: str,
    location_field: str | None = None,
    protection: ProtectedCollection | None = None,
) -> None:
    """Attach list/create/update/delete routes for a workspace-scoped context entity.

    ``location_field`` names the entity's list of location ids, if it has one; every
    write of that list goes through :func:`validate_location_ids`. ``protection`` makes the
    collection a protected control (exact-role writers, soft retirement, audit).
    """
    writers = protection.writers if protection else EDITORS

    @router.get(
        f"/workspaces/{{workspace_id}}/{path}",
        name=f"list_{path}",
        tags=[tag],
        responses=PAGINATION_RESPONSES,
    )
    def _list(
        workspace_id: str,
        response: Response,
        # No limit by default, as before P6-PLAT-12; a client that pages sends one.
        limit: int | None = Query(default=None, ge=1, le=MAX_PAGE_SIZE),
        cursor: str | None = Query(default=None, max_length=1024),
        db: Session = Depends(get_db),
        ctx: TenantContext = Depends(get_tenant_context),
    ) -> list[dict]:
        query = select(model).where(model.workspace_id == workspace_id)
        if protection:
            query = query.where(model.retired_at.is_(None))
        page = paginate(
            db,
            query,
            order=(model.created_at, model.id),
            descending=False,
            scope=f"context:{path}",
            workspace_id=workspace_id,
            filters={},
            cursor=cursor,
            limit=limit,
        )
        page.apply_headers(response)
        return [_serialize(r) for r in page.rows]

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
        ctx: TenantContext = Depends(writers),
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
        if protection:
            _audit(db, ctx, protection, protection.created, obj, list(values))
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
        ctx: TenantContext = Depends(writers),
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
        if protection and changes:
            _audit(db, ctx, protection, protection.updated, obj, list(changes))
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
        ctx: TenantContext = Depends(writers),
    ) -> None:
        obj = _get_scoped(db, model, workspace_id, item_id)
        if not protection:
            db.delete(obj)
            return
        obj.retired_at = database_now(db)  # soft retirement: never a hard delete
        db.add(obj)
        db.flush()
        _audit(db, ctx, protection, protection.retired, obj, ["retired_at"])


#: Collections that are protected controls (W21 CF-2): only the claims library.
_PROTECTED: dict[str, ProtectedCollection] = {"claims": CLAIMS_PROTECTION}

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
        protection=_PROTECTED.get(_path),
    )
