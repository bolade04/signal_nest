"""Campaign/offer -> location references (P6-PLAT-8).

A campaign's ``location_ids`` and an offer's ``eligible_location_ids`` are JSON lists with
no foreign key. Integrity is enforced where they are written: :func:`validate_location_ids`
runs on every create and partial update of the two collections and admits only locations
of the request's own workspace that exist and are active. A relational join table (Phase
5D's decision O-8) is the later hardening; it is not built here, so no migration rewrites
or drops a stored list.

Rows written before this check may already hold an id that fails it. They are left as they
are and reported instead, read-only, by :func:`find_location_reference_findings` and by the
command ``python -m app.campaign_context.location_refs``, which prints a JSON report to
stdout and exits:

* ``0``  no integrity violation (references to deactivated locations of the row's own
  workspace are listed but are not violations: a location deactivated after it was
  attached is history, not corruption)
* ``50`` at least one integrity violation was found and reported
* ``51`` configuration or connection failure
* ``52`` unexpected failure

As in :mod:`app.db.revision_status`, the command never prints an exception message or a
traceback (a driver error can embed the DSN), and it only reads.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Collection, Iterable
from dataclasses import asdict, dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from app.campaign_context.models import Campaign, OfferCalendarEntry
from app.core.errors import NotFoundError, ValidationDomainError
from app.locations.models import BusinessLocation

#: The two reference lists: (collection path, model, JSON column).
REFERENCE_COLUMNS: tuple[tuple[str, type, str], ...] = (
    ("campaigns", Campaign, "location_ids"),
    ("offers", OfferCalendarEntry, "eligible_location_ids"),
)

LOCATION_NOT_FOUND = "Location not found in this workspace."
LOCATION_DEACTIVATED = "A deactivated location cannot be added."


def validate_location_ids(
    db: Session,
    workspace_id: str,
    location_ids: Iterable[str],
    *,
    already_stored: Collection[str] = (),
) -> None:
    """Admit ``location_ids`` only if every id is an active location of ``workspace_id``.

    An id that names no location, or a location of any other workspace -- in this
    organization or another -- answers the same ``404`` as every other body identifier
    in the API, so the response never reveals whether the id exists elsewhere. A
    deactivated location of this workspace answers ``422``, unless it is already stored on
    the row being updated: a reference that predates the deactivation may be kept, never
    newly added. ``already_stored`` never admits an id outside the workspace.
    """
    wanted = list(location_ids)
    if not wanted:
        return
    found = {
        loc.id: loc.is_active
        for loc in db.execute(
            select(BusinessLocation).where(
                BusinessLocation.workspace_id == workspace_id,
                BusinessLocation.id.in_(set(wanted)),
            )
        ).scalars()
    }
    if any(location_id not in found for location_id in wanted):
        raise NotFoundError(LOCATION_NOT_FOUND)
    kept = set(already_stored)
    if any(not found[i] and i not in kept for i in wanted):
        raise ValidationDomainError(LOCATION_DEACTIVATED)


# --------------------------------------------------------------------------- #
# Read-only detection of rows written before the check
# --------------------------------------------------------------------------- #
#: Integrity violations. ``inactive`` is reported separately and is not one of them.
VIOLATION_KINDS = ("malformed", "missing", "foreign_workspace")


@dataclass(frozen=True)
class LocationReferenceFinding:
    collection: str  # "campaigns" | "offers"
    row_id: str
    workspace_id: str
    location_id: str | None  # None when the stored value is not a list of strings
    kind: str  # one of VIOLATION_KINDS, or "inactive"


def find_location_reference_findings(db: Session) -> list[LocationReferenceFinding]:
    """Every stored reference that the write-time check would not admit today. Reads only."""
    locations = {
        loc_id: (ws_id, active)
        for loc_id, ws_id, active in db.execute(
            select(BusinessLocation.id, BusinessLocation.workspace_id, BusinessLocation.is_active)
        )
    }
    findings: list[LocationReferenceFinding] = []
    for collection, model, column in REFERENCE_COLUMNS:
        rows = db.execute(
            select(model.id, model.workspace_id, getattr(model, column)).order_by(model.id)
        )
        for row_id, workspace_id, value in rows:
            if value is None:
                continue
            if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
                findings.append(
                    LocationReferenceFinding(collection, row_id, workspace_id, None, "malformed")
                )
                continue
            for location_id in value:
                where = locations.get(location_id)
                if where is None:
                    kind = "missing"
                elif where[0] != workspace_id:
                    kind = "foreign_workspace"
                elif not where[1]:
                    kind = "inactive"
                else:
                    continue
                findings.append(
                    LocationReferenceFinding(collection, row_id, workspace_id, location_id, kind)
                )
    return findings


def build_report(db: Session) -> dict:
    findings = find_location_reference_findings(db)
    violations = [asdict(f) for f in findings if f.kind in VIOLATION_KINDS]
    inactive = [asdict(f) for f in findings if f.kind == "inactive"]
    return {
        "rows_checked": {
            collection: db.scalar(select(func.count()).select_from(model))
            for collection, model, _column in REFERENCE_COLUMNS
        },
        "violation_count": len(violations),
        "violations": violations,
        "inactive_reference_count": len(inactive),
        "inactive_references": inactive,
    }


EXIT_OK = 0
EXIT_VIOLATIONS = 50
EXIT_CONFIG_OR_CONNECT_FAILURE = 51
EXIT_UNEXPECTED = 52


def _fail(token: str, code: int) -> int:
    print(f"location-refs: {token}", file=sys.stderr)
    return code


def _run(argv: list[str]) -> int:
    if argv:
        return _fail("unexpected-arguments", EXIT_CONFIG_OR_CONNECT_FAILURE)
    try:
        from app.core.config import get_settings
        from app.db.utc_session import create_utc_engine

        # A UTC session, as the app's (app.db.utc_session); a refused one is a
        # connection-failure below.
        engine = create_utc_engine(
            get_settings().database_url, poolclass=NullPool, hide_parameters=True
        )
    except Exception:
        return _fail("configuration-failure", EXIT_CONFIG_OR_CONNECT_FAILURE)
    try:
        try:
            conn = engine.connect()
        except Exception:
            return _fail("connection-failure", EXIT_CONFIG_OR_CONNECT_FAILURE)
        try:
            with Session(bind=conn) as db:
                report = build_report(db)
                db.rollback()
        except Exception:
            return _fail("unexpected-failure", EXIT_UNEXPECTED)
        finally:
            conn.close()
    finally:
        engine.dispose()
    print(json.dumps(report, indent=2, sort_keys=True))
    return EXIT_VIOLATIONS if report["violation_count"] else EXIT_OK


def main(argv: list[str] | None = None) -> int:
    """Entry point. Never lets any exception reach the default excepthook."""
    try:
        return _run(sys.argv[1:] if argv is None else argv)
    except BaseException:  # noqa: BLE001 - fixed classification, never a traceback
        try:
            print("location-refs: unexpected-failure", file=sys.stderr)
        except BaseException:  # pragma: no cover - stderr itself unusable
            pass
        return EXIT_UNEXPECTED


if __name__ == "__main__":  # pragma: no cover - process entrypoint
    sys.exit(main())
