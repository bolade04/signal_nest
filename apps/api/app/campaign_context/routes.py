"""Campaign Context Center endpoints.

A compact generic factory produces list/create/update/delete routes for each
workspace-scoped context entity. All routes enforce tenant scoping via
``get_tenant_context`` and attach ``organization_id``/``workspace_id``/``brand_id``
server-side; the update route (PATCH, P6-PLAT-9) changes only the fields the client sends
and never those three or the row's ``id``. An offer's location list is checked against the
workspace's own active locations on create and update (P6-PLAT-8,
:mod:`app.campaign_context.location_refs`).

**Campaigns are not served by the factory** (Project Phase 5D, 5D-W1): they have a dedicated
router (:mod:`app.campaign_context.campaigns`, included below at the same paths) with a
read-one route, a projected answer and ``campaign_locations`` membership.

**The claims library is a protected safety control** (Project Phase 5C plan §2, W21 CF-2):
5C's output-side review blocks a generated draft on the workspace's ``blocked`` claims, so a
marketer whose draft is blocked must not be able to remove the claim that blocks it. For
``/claims`` only:

* create, update and delete are gated by ``require_exact_roles(OWNER, ADMIN,
  COMPLIANCE_REVIEWER)`` — **marketers lose write access to these routes** (a disclosed,
  live behaviour change; every other collection keeps the ``OWNER``/``ADMIN``/``MARKETER``
  editors);
* ``DELETE`` soft-retires the entry (``retired_at``) instead of deleting it; a retired entry
  is no longer listed, updated or retired again (404, as a missing one);
* every create, update and retirement writes an audit record
  (``claims_library_entry.created`` / ``.updated`` / ``.retired``) naming the changed field
  names in ``context`` and carrying the entry's ``text``, ``kind`` and ``jurisdiction_codes``
  (:func:`app.claims.library.claim_audit_values`; operator ruling D-N4 item 40, 2026-11-12:
  "record the previous and new claim text and kind under the applicable access, retention and
  erasure controls. Free-form claim text must not automatically be classified as containing no
  personal data."): a create records them as ``new_state``; an update as ``previous_state``
  (before the change) and ``new_state`` (after it), on every update whichever fields changed;
  a retirement as ``previous_state`` (``new_state`` empty: the entry no longer applies). The
  ``jurisdiction_codes`` are a coordinator addition beyond the ruling's minimum (DN4-A ruling
  A-R-3: they decide where an entry applies since 5D-W1; codes only). The other fields' values
  are not recorded. The claim text may name a person, so it is treated as personal data under
  the ``audit_logs`` controls (``docs/operations/data-inventory.md``).

**Onboarding is held to the same rules (coordinator ruling R1, closed by the 5C build, W21
lane P5C).** ``POST /workspaces/{workspace_id}/onboarding`` (:mod:`app.brands.service`) is the
library's only other writer: its ``claims`` are refused for a non-steward (403
``permission_denied``), validated as a ``/claims`` create body (``ClaimIn``; 422), accepted
only on the workspace's first onboarding (409 ``onboarding_claims_closed`` afterwards), and
audited per entry in this module's ``claims_library_entry.created`` shape
(:func:`app.claims.library.audit_claim_created`). A marketer can therefore neither add, edit
nor retire a claim by any route.
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
from app.campaign_context.campaigns import router as campaigns_router
from app.campaign_context.location_refs import validate_location_ids
from app.campaign_context.models import (
    AudienceProfile,
    BrandVoiceProfile,
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
from app.claims.library import CLAIMS_STEWARD_ROLES, claim_audit_values
from app.core.enums import Role
from app.core.errors import NotFoundError, ValidationDomainError
from app.core.pagination import MAX_PAGE_SIZE, PAGINATION_RESPONSES, paginate
from app.db.clock import database_now
from app.db.request_tx import WriteDb
from app.db.session import get_db

router = APIRouter(tags=["campaign-context"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)
#: The claims library's stewards (W21 CF-2): exact roles, no rank inheritance. The set lives
#: in :mod:`app.claims.library`, the one authority the onboarding claims input shares (R1).
CLAIMS_STEWARDS = require_exact_roles(*sorted(CLAIMS_STEWARD_ROLES))


@dataclass(frozen=True)
class ProtectedCollection:
    """A collection whose writes are a safety control: exact-role writers, soft retirement
    (the model has ``retired_at``) and audited mutations. ``values`` gives what each audit
    record carries of the entry (its ``previous_state`` / ``new_state``)."""

    writers: Callable[..., TenantContext]
    entity_type: str
    created: AuditAction
    updated: AuditAction
    retired: AuditAction
    values: Callable[[Any], dict]


CLAIMS_PROTECTION = ProtectedCollection(
    writers=CLAIMS_STEWARDS,
    entity_type="claims_library_entry",
    created=AuditAction.CLAIMS_LIBRARY_ENTRY_CREATED,
    updated=AuditAction.CLAIMS_LIBRARY_ENTRY_UPDATED,
    retired=AuditAction.CLAIMS_LIBRARY_ENTRY_RETIRED,
    values=claim_audit_values,
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
    *,
    previous: dict | None = None,
    new: dict | None = None,
) -> None:
    """One audit record of a protected mutation: the changed field names in ``context``, and
    the entry's audited values before (``previous``) and after (``new``) it (D-N4 item 40)."""
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=obj.workspace_id,
        actor_user_id=ctx.user.id,
        action=action,
        entity_type=protection.entity_type,
        entity_id=obj.id,
        previous_state=previous,
        new_state=new,
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
        db: Session = WriteDb,
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
            _audit(
                db, ctx, protection, protection.created, obj, list(values),
                new=protection.values(obj),
            )
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
        db: Session = WriteDb,
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
        # The audited values as they stand before the change (D-N4 item 40).
        before = protection.values(obj) if protection else None
        for key, value in changes.items():
            setattr(obj, key, value)
        db.add(obj)
        db.flush()
        if protection and changes:
            _audit(
                db, ctx, protection, protection.updated, obj, list(changes),
                previous=before, new=protection.values(obj),
            )
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
        db: Session = WriteDb,
        ctx: TenantContext = Depends(writers),
    ) -> None:
        obj = _get_scoped(db, model, workspace_id, item_id)
        if not protection:
            db.delete(obj)
            return
        retired = protection.values(obj)  # what stops applying (D-N4 item 40)
        obj.retired_at = database_now(db)  # soft retirement: never a hard delete
        db.add(obj)
        db.flush()
        _audit(
            db, ctx, protection, protection.retired, obj, ["retired_at"], previous=retired
        )


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

# The ninth collection, campaigns, at the same paths (Project Phase 5D, 5D-W1).
router.include_router(campaigns_router)
