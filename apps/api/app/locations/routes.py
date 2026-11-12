from __future__ import annotations

from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from fastapi.exceptions import RequestValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit.actions import AuditAction
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
from app.db.request_tx import WriteDb
from app.db.session import get_db
from app.geography.geocoder import GeocodeFailure, GeocodingService, get_geocoding_service
from app.jurisdiction.business_address import (
    CONFIRMATION_REFUSALS,
    confirmation_applies,
    confirmation_refusal,
    locality_differs,
    location_code,
)
from app.locations import service as location_service
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.locations.schemas import (
    BUSINESS_ADDRESS_FIELDS,
    GeocodeRequest,
    GeocodeResponse,
    GeoCoverageBase,
    GeoCoverageOut,
    LocalityConfirmationIn,
    LocationBase,
    LocationOut,
    unreadable_address_fields,
)

router = APIRouter(tags=["locations"])

EDITORS = require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)


def _get_scoped(db: Session, workspace_id: str, location_id: str) -> BusinessLocation:
    location = db.get(BusinessLocation, location_id)
    if not location or location.workspace_id != workspace_id:
        raise NotFoundError("Location not found in this workspace.")
    return location


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
    db: Session = WriteDb,
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
        action=AuditAction.LOCATION_CREATED,
        entity_type="business_location",
        entity_id=location.id,
    )
    return location


@router.put("/workspaces/{workspace_id}/locations/{location_id}", response_model=LocationOut)
def update_location(
    workspace_id: str,
    location_id: str,
    body: LocationBase,
    db: Session = WriteDb,
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
    db: Session, ctx: TenantContext, location: BusinessLocation, action: AuditAction
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
    db: Session = WriteDb,
    ctx: TenantContext = Depends(EDITORS),
) -> BusinessLocation:
    """Retire a location without deleting it or any of its history (P6-UI-023).

    See :mod:`app.locations.service` for why this is a deactivation and what it stops.
    Deactivating an already-deactivated location changes nothing and answers 200.
    """
    location = _get_scoped(db, workspace_id, location_id)
    if location_service.deactivate(db, location):
        _record_lifecycle(db, ctx, location, AuditAction.LOCATION_DEACTIVATED)
    return location


@router.post(
    "/workspaces/{workspace_id}/locations/{location_id}/reactivate",
    response_model=LocationOut,
)
def reactivate_location(
    workspace_id: str,
    location_id: str,
    db: Session = WriteDb,
    ctx: TenantContext = Depends(EDITORS),
) -> BusinessLocation:
    """Return a deactivated location to service; a no-op (200) when already active."""
    location = _get_scoped(db, workspace_id, location_id)
    if location_service.reactivate(db, location):
        _record_lifecycle(db, ctx, location, AuditAction.LOCATION_REACTIVATED)
    return location


_NO_RULE = "No Scout Reach configured for this location yet."


class LocalityConfirmationRefusedError(ValidationDomainError):
    """The customer may not confirm the business town now (W21 ADDR): the location does not
    read, the business address contradicts it or is incomplete, or there is nothing to confirm.
    Nothing is recorded (422)."""

    code = "locality_confirmation_refused"


#: The confirmation's columns; the PUT never writes them, the confirmation route only them.
_CONFIRMATION_COLUMNS = (
    "locality_confirmed_locality",
    "locality_confirmed_city",
    "locality_confirmed_jurisdiction",
    "locality_confirmed_at",
    "locality_confirmed_by_user_id",
)


def _rule_of(db: Session, workspace_id: str, location_id: str) -> GeoCoverageRule | None:
    return db.scalar(
        select(GeoCoverageRule).where(
            GeoCoverageRule.workspace_id == workspace_id,
            GeoCoverageRule.location_id == location_id,
        )
    )


def _coverage_out(rule: GeoCoverageRule, location: BusinessLocation | None) -> GeoCoverageOut:
    """The stored rule, with what the Locations dialog needs to know of its business town:
    whether it matches the location's city, and whether a customer confirmation applies.
    Computed from the stored values with the readers alone; the resolver is never called here
    (register gate G0)."""
    out = GeoCoverageOut.model_validate(rule)
    city = location.city if location is not None else None
    differs = locality_differs(rule.business_address_locality, city)
    return out.model_copy(
        update={
            "locality_matches_city": None if differs is None else not differs,
            "locality_confirmation_applies": confirmation_applies(
                rule, city, location_code(location)
            ),
        }
    )


@router.get(
    "/workspaces/{workspace_id}/locations/{location_id}/geo-coverage",
    response_model=GeoCoverageOut,
)
def get_geo_coverage(
    workspace_id: str,
    location_id: str,
    db: Session = Depends(get_db),
    ctx: TenantContext = Depends(get_tenant_context),
) -> GeoCoverageOut:
    rule = _rule_of(db, workspace_id, location_id)
    if not rule:
        raise NotFoundError(_NO_RULE)
    # The rule's own location, loaded once the rule is found (the 404s are unchanged).
    location = db.get(BusinessLocation, rule.location_id) if rule.location_id else None
    if location is not None and location.workspace_id != workspace_id:
        location = None  # never another workspace's location, even for a computed field
    return _coverage_out(rule, location)


@router.put(
    "/workspaces/{workspace_id}/locations/{location_id}/geo-coverage",
    response_model=GeoCoverageOut,
)
def upsert_geo_coverage(
    workspace_id: str,
    location_id: str,
    body: GeoCoverageBase,
    db: Session = WriteDb,
    ctx: TenantContext = Depends(require_role(Role.OWNER, Role.ADMIN, Role.MARKETER)),
) -> GeoCoverageOut:
    """Store a location's Scout Reach and business address. The structured business-address
    fields are written only when the request names them (an explicit ``null`` clears one), so a
    client that does not know them never erases them; nothing is filled from the location.
    Their values must be readable (422 with a fixed message, never the value); whether they are
    complete, or agree with the location, is decided only before a jurisdiction-dependent
    action, never here."""
    location = db.get(BusinessLocation, location_id)
    if not location or location.workspace_id != workspace_id:
        raise NotFoundError("Location not found in this workspace.")
    rule = _rule_of(db, workspace_id, location_id)
    if not rule:
        rule = GeoCoverageRule(
            organization_id=ctx.organization.id,
            workspace_id=workspace_id,
            location_id=location_id,
        )
    # Readability of the values the rule WILL hold: the stored ones for fields the request does
    # not name, merged with the named ones, so two partial PUTs cannot store a state of another
    # country (review d1 C-4). Checked before anything is written; a 422 like the body's own.
    merged = {
        key: getattr(body, key) if key in body.model_fields_set else getattr(rule, key, None)
        for key in BUSINESS_ADDRESS_FIELDS
    }
    unreadable = unreadable_address_fields(
        merged["business_address_country"], merged["business_address_region"]
    )
    if unreadable:
        raise RequestValidationError(
            [
                {"type": "value_error", "loc": ("body", field), "msg": f"Value error, {message}"}
                for field, message in unreadable
            ]
        )
    for key, value in body.model_dump(exclude=set(BUSINESS_ADDRESS_FIELDS)).items():
        setattr(rule, key, value)
    for key in BUSINESS_ADDRESS_FIELDS:
        if key in body.model_fields_set:
            setattr(rule, key, getattr(body, key))
    db.add(rule)
    db.flush()
    record_audit(
        db,
        organization_id=ctx.organization.id,
        workspace_id=workspace_id,
        actor_user_id=ctx.user.id,
        action=AuditAction.GEO_COVERAGE_UPDATED,
        entity_type="geo_coverage_rule",
        entity_id=rule.id,
    )
    return _coverage_out(rule, location)


@router.put(
    "/workspaces/{workspace_id}/locations/{location_id}/geo-coverage/locality-confirmation",
    response_model=GeoCoverageOut,
)
def set_locality_confirmation(
    workspace_id: str,
    location_id: str,
    body: LocalityConfirmationIn,
    db: Session = WriteDb,
    ctx: TenantContext = Depends(EDITORS),
) -> GeoCoverageOut:
    """Confirm, or withdraw, that the saved business town or locality lies within this
    location's area (W21 ADDR). The confirmation is the customer's own statement, recorded as
    confirmed by the customer, with who made it and when.

    It binds what is saved now: the stored business town, the location's city and the
    jurisdiction both resolve to. It lapses when any of them changes. Before a
    jurisdiction-dependent action it answers only the question of the town, after every
    contradiction and completeness check, so it can never override a contradiction. Answers
    422 ``locality_confirmation_refused`` (a fixed message per cause; nothing is recorded) when
    the location's own country or required state or province does not read, when the business
    address contradicts the location or is incomplete, or when there is nothing to confirm.
    Withdrawing is always allowed. A change is audited as ``geo_coverage.updated`` naming the
    confirmation's fields that changed, never their values; a request that changes nothing
    records nothing."""
    location = _get_scoped(db, workspace_id, location_id)
    rule = _rule_of(db, workspace_id, location_id)
    if not rule:
        raise NotFoundError(_NO_RULE)
    before = {column: getattr(rule, column) for column in _CONFIRMATION_COLUMNS}
    if body.confirmed:
        refusal = confirmation_refusal(rule, location)
        if refusal is not None:
            raise LocalityConfirmationRefusedError(CONFIRMATION_REFUSALS[refusal])
        rule.locality_confirmed_locality = rule.business_address_locality
        rule.locality_confirmed_city = location.city
        rule.locality_confirmed_jurisdiction = location_code(location)
        rule.locality_confirmed_at = datetime.now(UTC)
        rule.locality_confirmed_by_user_id = ctx.user.id
    else:
        for column in _CONFIRMATION_COLUMNS:
            setattr(rule, column, None)
    changed = sorted(c for c in _CONFIRMATION_COLUMNS if getattr(rule, c) != before[c])
    if changed:
        db.add(rule)
        db.flush()
        record_audit(
            db,
            organization_id=ctx.organization.id,
            workspace_id=workspace_id,
            actor_user_id=ctx.user.id,
            action=AuditAction.GEO_COVERAGE_UPDATED,
            entity_type="geo_coverage_rule",
            entity_id=rule.id,
            context={
                "locality_confirmation": "confirmed" if body.confirmed else "withdrawn",
                "fields_changed": changed,
            },
        )
    return _coverage_out(rule, location)
