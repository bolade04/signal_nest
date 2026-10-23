from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.brands.service import get_primary_brand
from app.core.enums import Role
from app.core.errors import NotFoundError, ValidationDomainError
from app.db.session import get_db
from app.geography.geocoder import geocode
from app.locations import service as location_service
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.locations.schemas import (
    GeocodeRequest,
    GeocodeResponse,
    GeoCoverageBase,
    GeoCoverageOut,
    LocationBase,
    LocationOut,
)

router = APIRouter(tags=["locations"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)


def _get_scoped(db: Session, workspace_id: str, location_id: str) -> BusinessLocation:
    location = db.get(BusinessLocation, location_id)
    if not location or location.workspace_id != workspace_id:
        raise NotFoundError("Location not found in this workspace.")
    return location


@router.post("/geocode", response_model=GeocodeResponse)
def geocode_address(
    body: GeocodeRequest,
    ctx: TenantContext = Depends(get_tenant_context),
) -> GeocodeResponse:
    """Scout Reach helper — geocode an address so a radius can be applied."""
    result = geocode(body.query)
    if not result:
        raise NotFoundError(
            "Could not geocode that address in offline mode. Try a major city name "
            "(e.g. Dallas, London, Lagos, Nairobi)."
        )
    return GeocodeResponse(**result.__dict__)


@router.get("/workspaces/{workspace_id}/locations", response_model=list[LocationOut])
def list_locations(
    workspace_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> list[BusinessLocation]:
    rows = db.execute(
        select(BusinessLocation)
        .where(BusinessLocation.workspace_id == workspace_id)
        .order_by(BusinessLocation.created_at)
    ).scalars()
    return list(rows)


@router.post("/workspaces/{workspace_id}/locations", response_model=LocationOut, status_code=201)
def create_location(
    workspace_id: str,
    body: LocationBase,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)),
) -> BusinessLocation:
    brand = get_primary_brand(db, workspace_id)
    if not brand:
        raise ValidationDomainError("Complete onboarding before adding locations.")
    location = BusinessLocation(
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        brand_id=brand.id,
        **body.model_dump(),
    )
    db.add(location)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="location.created",
        entity_type="business_location",
        entity_id=location.id,
    )
    return location


@router.put("/workspaces/{workspace_id}/locations/{location_id}", response_model=LocationOut)
def update_location(
    workspace_id: str,
    location_id: str,
    body: LocationBase,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)),
) -> BusinessLocation:
    location = db.get(BusinessLocation, location_id)
    if not location or location.workspace_id != workspace_id:
        raise NotFoundError("Location not found in this workspace.")
    for key, value in body.model_dump().items():
        setattr(location, key, value)
    db.add(location)
    db.flush()
    return location


def _record_lifecycle(
    db: Session, ctx: TenantContext, location: BusinessLocation, action: str
) -> None:
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=location.workspace_id,
        actor_user_id=ctx.user.id,
        action=action,
        entity_type="business_location",
        entity_id=location.id,
    )


@router.post(
    "/workspaces/{workspace_id}/locations/{location_id}/deactivate",
    response_model=LocationOut,
)
def deactivate_location(
    workspace_id: str,
    location_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> BusinessLocation:
    """Retire a location without deleting it or any of its history (P6-UI-023).

    See :mod:`app.locations.service` for why this is a deactivation and what it stops.
    Deactivating an already-deactivated location changes nothing and answers 200.
    """
    location = _get_scoped(db, workspace_id, location_id)
    if location_service.deactivate(db, location):
        _record_lifecycle(db, ctx, location, "location.deactivated")
    return location


@router.post(
    "/workspaces/{workspace_id}/locations/{location_id}/reactivate",
    response_model=LocationOut,
)
def reactivate_location(
    workspace_id: str,
    location_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(EDITORS),
) -> BusinessLocation:
    """Return a deactivated location to service; a no-op (200) when already active."""
    location = _get_scoped(db, workspace_id, location_id)
    if location_service.reactivate(db, location):
        _record_lifecycle(db, ctx, location, "location.reactivated")
    return location


@router.get(
    "/workspaces/{workspace_id}/locations/{location_id}/geo-coverage",
    response_model=GeoCoverageOut,
)
def get_geo_coverage(
    workspace_id: str,
    location_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> GeoCoverageRule:
    rule = db.scalar(
        select(GeoCoverageRule).where(
            GeoCoverageRule.workspace_id == workspace_id,
            GeoCoverageRule.location_id == location_id,
        )
    )
    if not rule:
        raise NotFoundError("No Scout Reach configured for this location yet.")
    return rule


@router.put(
    "/workspaces/{workspace_id}/locations/{location_id}/geo-coverage",
    response_model=GeoCoverageOut,
)
def upsert_geo_coverage(
    workspace_id: str,
    location_id: str,
    body: GeoCoverageBase,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)),
) -> GeoCoverageRule:
    location = db.get(BusinessLocation, location_id)
    if not location or location.workspace_id != workspace_id:
        raise NotFoundError("Location not found in this workspace.")
    rule = db.scalar(
        select(GeoCoverageRule).where(
            GeoCoverageRule.workspace_id == workspace_id,
            GeoCoverageRule.location_id == location_id,
        )
    )
    if not rule:
        rule = GeoCoverageRule(
            organization_id=ctx.organization.id,
            workspace_id=workspace_id,
            location_id=location_id,
        )
    for key, value in body.model_dump().items():
        setattr(rule, key, value)
    db.add(rule)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action="geo_coverage.updated",
        entity_type="geo_coverage_rule",
        entity_id=rule.id,
    )
    return rule
