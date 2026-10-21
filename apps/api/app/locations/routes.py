from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.service import record_audit
from app.auth.dependencies import TenantContext, get_tenant_context, require_role
from app.brands.service import get_primary_brand
from app.core.enums import Role
from app.core.errors import (
    CapabilityUnavailableError,
    NotFoundError,
    SignalNestError,
    ValidationDomainError,
)
from app.db.session import get_db
from app.geography.geocoder import GeocodeFailure, GeocodingService, get_geocoding_service
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


class GeocodeRateLimitedError(SignalNestError):
    status_code = 429
    code = "geocode_rate_limited"


#: Every address lookup that ends without a place leaves the customer a way on: the
#: location can be saved with its city, state or province and country typed in,
#: which is what scouting matches on (P6-DATA-6); coordinates are optional.
_MANUAL_ENTRY = (
    "You can still save this location: enter its city, state or province and country, "
    "which is what scouting uses."
)
_GEOCODE_FAILURES: dict[str, tuple[type[SignalNestError], str, str]] = {
    "invalid_query": (
        ValidationDomainError,
        "geocode_invalid_query",
        "Enter an address or place name of 2 to 200 characters.",
    ),
    "no_match": (
        ValidationDomainError,
        "location_unresolvable",
        "We couldn't find that place. Check the spelling, or add the city and country "
        f'(for example "London, Ontario, Canada"). {_MANUAL_ENTRY}',
    ),
    "ambiguous": (
        ValidationDomainError,
        "location_ambiguous",
        "That matches more than one place. Add the state or province and the country "
        f'(for example "Portland, Oregon, United States"). {_MANUAL_ENTRY}',
    ),
    "not_configured": (
        CapabilityUnavailableError,
        "geocoder_not_configured",
        f"Address lookup is not available in this environment. {_MANUAL_ENTRY}",
    ),
    "rate_limited": (
        GeocodeRateLimitedError,
        "geocode_rate_limited",
        f"Too many address lookups. Wait a minute and try again. {_MANUAL_ENTRY}",
    ),
}
_GEOCODER_UNAVAILABLE = (
    CapabilityUnavailableError,
    "geocoder_unavailable",
    f"Address lookup is temporarily unavailable. Try again shortly. {_MANUAL_ENTRY}",
)


@router.post("/geocode", response_model=GeocodeResponse)
def geocode_address(
    body: GeocodeRequest,
    ctx: TenantContext = Depends(get_tenant_context),
    service: GeocodingService = Depends(get_geocoding_service),
) -> GeocodeResponse:
    """Scout Reach helper — resolve an address or place to coordinates and a
    normalised city / region / country. A lookup that finds no single place answers
    with an error whose message says how to continue (P6-DATA-3)."""
    try:
        result = service.geocode(
            body.query, organization_id=ctx.organization.id, workspace_id=ctx.workspace.id
        )
    except GeocodeFailure as failure:
        error_class, code, message = _GEOCODE_FAILURES.get(failure.reason, _GEOCODER_UNAVAILABLE)
        raise error_class(message, code=code) from None
    return GeocodeResponse(**result.to_dict())


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
