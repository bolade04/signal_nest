"""Location lifecycle: deactivate and reactivate (P6-UI-023).

A location is never hard-deleted. Opportunities, scout requests, schedules, durable jobs
and intelligence records reference ``business_locations.id`` with ``ON DELETE SET NULL``
and geo-coverage with ``ON DELETE CASCADE``, so a delete would silently detach a market's
history from its location (its opportunities would stop being that market's) and drop its
Scout Reach. Deactivation keeps every row and every link; it only stops NEW work:

* no scout request may be created for a deactivated location, and none of its requests
  may be run or have a schedule created or resumed (``422``);
* deactivation itself is refused (``409``) while the location still has an enabled
  schedule or a queued/running scout, so nothing keeps running for a retired location.

Reactivation lifts all of this. Reads are unchanged: the location and its history stay
listed and readable, with ``is_active`` telling the client which state it is in.
"""

from __future__ import annotations

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from app.core.enums import ScoutRequestStatus
from app.core.errors import ConflictError, ValidationDomainError
from app.locations.models import BusinessLocation
from app.scouting_requests.models import ScoutRequest, ScoutSchedule

LOCATION_DEACTIVATED = "This location is deactivated. Reactivate it to scout it again."
HAS_ENABLED_SCHEDULE = "Pause this location's scheduled scouts before deactivating it."
HAS_WORK_IN_FLIGHT = (
    "Wait for this location's queued or running scouts to finish before deactivating it."
)


def ensure_location_active(db: Session, location_id: str | None) -> None:
    """Refuse new scouting work for a deactivated location (no-op without a location)."""
    if location_id is None:
        return
    location = db.get(BusinessLocation, location_id)
    if location is not None and not location.is_active:
        raise ValidationDomainError(LOCATION_DEACTIVATED)


def deactivate(db: Session, location: BusinessLocation) -> bool:
    """Deactivate ``location``; ``False`` when it already was (nothing changes)."""
    if not location.is_active:
        return False
    schedule_enabled = db.scalar(
        select(
            exists().where(
                ScoutSchedule.workspace_id == location.workspace_id,
                ScoutSchedule.enabled.is_(True),
                ScoutSchedule.scout_request_id == ScoutRequest.id,
                ScoutRequest.location_id == location.id,
            )
        )
    )
    if schedule_enabled:
        raise ConflictError(HAS_ENABLED_SCHEDULE)
    in_flight = db.scalar(
        select(
            exists().where(
                ScoutRequest.workspace_id == location.workspace_id,
                ScoutRequest.location_id == location.id,
                ScoutRequest.status.in_(
                    (ScoutRequestStatus.QUEUED.value, ScoutRequestStatus.RUNNING.value)
                ),
            )
        )
    )
    if in_flight:
        raise ConflictError(HAS_WORK_IN_FLIGHT)
    location.is_active = False
    db.add(location)
    db.flush()
    return True


def reactivate(db: Session, location: BusinessLocation) -> bool:
    """Reactivate ``location``; ``False`` when it was already active."""
    if location.is_active:
        return False
    location.is_active = True
    db.add(location)
    db.flush()
    return True
