"""reconcile campaign_locations to each campaign's location_ids (Project Phase 5D, 5D-W1, I-CF3-2)

The one-time campaign-location data repair (5D-W1 blocked portion B-6, integration item
I-CF3-2), under the operator's limited exception to CF-3's migration freeze (decision D-1 (a),
2026-11-07; lane amendment D-1). **Data only**: no DDL, no constraint, index, model or enum
change. It touches exactly two things: the ``campaign_locations`` rows and each campaign's
``location_ids`` JSON mirror.

**Authoritative source of campaign-location membership.**

* *Before this revision:* the ``campaigns.location_ids`` JSON array. Between the CF-3 back-fill
  (``c3f8e1a6d2b9``) and 5D-W1, the API wrote only that array (the generic factory's create and
  PATCH at ``c7c4d277``: ``campaign_context/routes.py`` ``_create`` / ``_update`` set the list
  and never wrote the join table), so it records the customer's latest intent. The join table
  holds what the back-fill copied, which every later edit left behind.
* *After this revision:* the ``campaign_locations`` join table (operator decision O-8; Project
  Phase 5D plan ``docs/project-phase-5d-multi-location-jurisdiction.md`` §2 lines 36-44: "new
  ``campaign_locations`` join table with real FKs + backfill from the JSON array, which is
  retained read-only for one release"; CF-3's ``app/campaign_context/models.py`` lines 8-16 and
  182-185: "keeping the two in step on writes, and reading the join table, is the 5D build
  lane's (5D-W1)"). 5D-W1 reads only the join table and rewrites the JSON as its read-only,
  sorted mirror (``app/campaign_context/campaigns.py``). (The plan's §2 lines 78-100 are its
  nullability decision for jurisdiction columns; they do not govern membership.)

**The inconsistent states repaired** (each campaign is classified by its JSON value and its
current join rows; one campaign can be in several):

* **S0** consistent: the JSON is the sorted list of exactly the campaign's join rows, every id
  valid. Nothing is written, row for row.
* **S1** an id in the JSON with no join row (added after the back-fill): the row is inserted.
* **S2** a join row whose location is not in the JSON (removed after the back-fill): the row
  is deleted.
* **S3** a JSON id naming a location of another workspace, of the same organization or another,
  or of the campaign's workspace but another organization: dropped and reported, never
  inserted.
* **S4** a JSON element naming no location (deleted or never existing), or an element that is
  not a non-empty string: dropped and reported.
* **S5** an id repeated in the JSON: kept once; each repeat is reported.
* **S6** a JSON value that cannot be read as a list (a JSON ``null``, any JSON value that is not
  an array, or, on SQLite only, text that is not JSON; PostgreSQL's ``json`` type refuses such
  text on input): **the campaign is left exactly as it is** (its join rows and its JSON) and
  reported, because an unreadable source cannot show that a membership is wrong. The column is
  ``NOT NULL``, so SQL ``NULL`` cannot occur (it is still read as unreadable if it ever did),
  but its ``JSON`` type admits any JSON value: neither the model nor CF-3 makes S6 impossible.
* **S7** a deactivated location of the campaign's own workspace and organization: kept (the
  CF-3 back-fill rule: it is history, not corruption) and counted.

**The exact transformation** (``reconcile_campaign_locations``), inside this revision's own
migration transaction (``env.py`` runs Alembic with ``transaction_per_migration=True``):

1. *Lock and read scope.* On PostgreSQL the revision first takes ``LOCK TABLE campaigns,
   campaign_locations IN SHARE ROW EXCLUSIVE MODE`` (a lock, not DDL): a concurrent write to
   either table waits until this transaction ends, and reads continue. It then reads, in one
   pass and not in batches, every location (id, organization, workspace, active), every
   membership row (id, campaign, location) and every campaign (id, organization, workspace and
   ``location_ids`` cast to text and parsed here), at the session's default isolation (READ
   COMMITTED on PostgreSQL). On SQLite there is no lock statement; SQLite admits one writer at
   a time.
2. *The kept set*, per campaign in id order: the JSON's elements in order; a repeat is
   dropped (S5); an element that is not a non-empty string (S4 ``invalid``), or names no
   location (S4 ``missing``), is dropped; a location whose workspace or organization is not the
   campaign's is dropped (S3 ``foreign_workspace`` / ``foreign_organization``); every other
   element is kept, deactivated or not (S7).
3. *The deletes, then the inserts.* Every join row of the campaign whose location is not in
   the kept set is deleted by its id (S2). Then every kept location without a row is inserted
   with the campaign's organization and workspace (S1). A row that is kept is never touched:
   not updated, not deleted and re-inserted.
4. *The mirror.* Each campaign whose JSON is not already the sorted kept set has it rewritten
   to the sorted kept set, the mirror 5D-W1 maintains. ``updated_at`` is not touched.
5. *The report*, on the ``alembic.runtime.migration`` logger, in the CF-3 back-fill's format
   and with ids only (never names, text or a dropped element's content): one line per dropped
   reference (``location=`` only for an existing location, ``None`` otherwise), one per
   unreadable campaign, one per category with its count and campaign ids, and a summary.
   Nothing is logged when there are no campaigns.

Running it again changes nothing: the kept rows exist, the JSON is the sorted kept set, and
nothing is dropped; only the states it leaves as they are by design (S0, S6, S7) are reported
again. A failure anywhere rolls the whole revision back with its ``alembic_version`` update on
PostgreSQL (measured in ``app/tests/test_campaign_locations_repair.py``).

**Downgrade: a no-op.** The repair only makes the join table agree with the JSON the API had
written; it destroys nothing a downgrade could restore (every deleted row named a location the
customer had removed or that the tenant does not own), and CF-3's own downgrade, one step
further, drops the join table entirely while keeping the JSON.

Revision ID: d5f616c0ec97
Revises: c3f8e1a6d2b9
Create Date: 2026-11-07 23:00:00.000000
"""

from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d5f616c0ec97"
down_revision: str | None = "c3f8e1a6d2b9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

_PREFIX = "5D-W1 campaign_locations repair"
#: Rows deleted per statement (a bound-parameter list; well under SQLite's limit).
_CHUNK = 500
#: PostgreSQL only: concurrent writers of either table wait for this transaction; reads go on.
_LOCK = sa.text("LOCK TABLE campaigns, campaign_locations IN SHARE ROW EXCLUSIVE MODE")

_CAMPAIGNS = sa.table(
    "campaigns",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("workspace_id"),
    sa.column("location_ids", sa.JSON),
)
_LOCATIONS = sa.table(
    "business_locations",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("workspace_id"),
    sa.column("is_active"),
)
_MEMBERSHIP = sa.table(
    "campaign_locations",
    sa.column("id"),
    sa.column("organization_id"),
    sa.column("workspace_id"),
    sa.column("campaign_id"),
    sa.column("location_id"),
)


class RepairReport:
    """What one run found and did. Ids only. (A plain class: Alembic loads a revision file
    without registering it in ``sys.modules``, which ``dataclasses`` would need here.)"""

    def __init__(self) -> None:
        self.campaigns = 0
        self.consistent: list[str] = []  # S0
        self.inserted: list[tuple[str, str]] = []  # S1 (campaign, location)
        self.deleted: list[tuple[str, str]] = []  # S2 (campaign, location)
        #: S3 / S4 / S5: (campaign, workspace, location or None, reason)
        self.dropped: list[tuple[str, str, str | None, str]] = []
        self.unreadable: list[tuple[str, str, str]] = []  # S6 (campaign, workspace, why)
        self.deactivated_kept: list[tuple[str, str]] = []  # S7 (campaign, location)
        self.mirrored: list[str] = []  # JSON rewritten

    def changes(self) -> int:
        return len(self.inserted) + len(self.deleted) + len(self.mirrored)


def _read(raw: str | None) -> tuple[list | None, str]:
    """``(list, "")`` or ``(None, reason)``. The value is parsed here, so unparseable text is a
    reported state rather than a failed read."""
    if raw is None:
        return None, "null"
    try:
        value = json.loads(raw)
    except ValueError:
        return None, "unparseable"
    if value is None:
        return None, "null"
    if not isinstance(value, list):
        return None, "not_a_list"
    return value, ""


def reconcile_campaign_locations(bind) -> RepairReport:
    """Make every campaign's ``campaign_locations`` rows and JSON mirror agree with its
    validated ``location_ids`` (see the module docstring). Core only; every value is bound."""
    if bind.dialect.name == "postgresql":
        bind.execute(_LOCK)
    report = RepairReport()
    locations = {
        row.id: (row.organization_id, row.workspace_id, bool(row.is_active))
        for row in bind.execute(
            sa.select(
                _LOCATIONS.c.id,
                _LOCATIONS.c.organization_id,
                _LOCATIONS.c.workspace_id,
                _LOCATIONS.c.is_active,
            )
        )
    }
    members: dict[str, dict[str, str]] = {}
    for row in bind.execute(
        sa.select(_MEMBERSHIP.c.id, _MEMBERSHIP.c.campaign_id, _MEMBERSHIP.c.location_id)
    ):
        members.setdefault(row.campaign_id, {})[row.location_id] = row.id
    campaigns = bind.execute(
        sa.select(
            _CAMPAIGNS.c.id,
            _CAMPAIGNS.c.organization_id,
            _CAMPAIGNS.c.workspace_id,
            sa.cast(_CAMPAIGNS.c.location_ids, sa.Text).label("raw"),
        ).order_by(_CAMPAIGNS.c.id)
    ).all()

    deletes: list[str] = []
    inserts: list[dict[str, str]] = []
    mirrors: list[dict[str, object]] = []
    for campaign_id, organization_id, workspace_id, raw in campaigns:
        report.campaigns += 1
        value, why = _read(raw)
        if value is None:
            report.unreadable.append((campaign_id, workspace_id, why))
            continue
        kept: list[str] = []
        seen: set[str] = set()
        drops_before = len(report.dropped)
        for element in value:
            if not isinstance(element, str) or not element:
                report.dropped.append((campaign_id, workspace_id, None, "invalid"))
                continue
            if element in seen:
                report.dropped.append((campaign_id, workspace_id, None, "duplicate"))
                continue
            seen.add(element)
            home = locations.get(element)
            if home is None:
                report.dropped.append((campaign_id, workspace_id, None, "missing"))
                continue
            home_org, home_ws, active = home
            if home_ws != workspace_id or home_org != organization_id:
                reason = (
                    "foreign_workspace"
                    if home_ws != workspace_id and home_org == organization_id
                    else "foreign_organization"
                )
                report.dropped.append((campaign_id, workspace_id, element, reason))
                continue
            if not active:
                report.deactivated_kept.append((campaign_id, element))
            kept.append(element)
        desired = set(kept)
        current = members.get(campaign_id, {})
        for location_id in sorted(current):
            if location_id not in desired:
                deletes.append(current[location_id])
                report.deleted.append((campaign_id, location_id))
        for location_id in sorted(desired - set(current)):
            inserts.append(
                {
                    "id": uuid.uuid4().hex,
                    "organization_id": organization_id,
                    "workspace_id": workspace_id,
                    "campaign_id": campaign_id,
                    "location_id": location_id,
                }
            )
            report.inserted.append((campaign_id, location_id))
        mirror = sorted(desired)
        if value != mirror:
            mirrors.append({"b_id": campaign_id, "b_ids": mirror})
            report.mirrored.append(campaign_id)
        changed = desired != set(current) or value != mirror
        if not changed and len(report.dropped) == drops_before:
            report.consistent.append(campaign_id)

    for start in range(0, len(deletes), _CHUNK):
        bind.execute(
            _MEMBERSHIP.delete().where(_MEMBERSHIP.c.id.in_(deletes[start : start + _CHUNK]))
        )
    if inserts:
        bind.execute(_MEMBERSHIP.insert(), inserts)
    if mirrors:
        bind.execute(
            _CAMPAIGNS.update()
            .where(_CAMPAIGNS.c.id == sa.bindparam("b_id"))
            .values(location_ids=sa.bindparam("b_ids", type_=sa.JSON)),
            mirrors,
        )
    _log(report)
    return report


def _ids(pairs) -> str:
    return ",".join(sorted({p[0] if isinstance(p, tuple) else p for p in pairs}))


def _log(report: RepairReport) -> None:
    if not report.campaigns:
        return
    for campaign_id, workspace_id, location_id, reason in report.dropped:
        logger.warning(
            "%s: dropped campaign=%s workspace=%s location=%s reason=%s",
            _PREFIX,
            campaign_id,
            workspace_id,
            location_id,
            reason,
        )
    for campaign_id, workspace_id, why in report.unreadable:
        logger.warning(
            "%s: unreadable campaign=%s workspace=%s reason=%s memberships kept",
            _PREFIX,
            campaign_id,
            workspace_id,
            why,
        )
    by_reason: dict[str, list] = {}
    for item in report.dropped:
        by_reason.setdefault(item[3], []).append(item)
    categories = (
        ("S0 consistent", report.consistent, len(report.consistent)),
        ("S1 inserted", report.inserted, len(report.inserted)),
        ("S2 deleted", report.deleted, len(report.deleted)),
        (
            "S3 foreign",
            by_reason.get("foreign_workspace", []) + by_reason.get("foreign_organization", []),
            len(by_reason.get("foreign_workspace", []))
            + len(by_reason.get("foreign_organization", [])),
        ),
        (
            "S4 missing or invalid",
            by_reason.get("missing", []) + by_reason.get("invalid", []),
            len(by_reason.get("missing", [])) + len(by_reason.get("invalid", [])),
        ),
        ("S5 duplicate", by_reason.get("duplicate", []), len(by_reason.get("duplicate", []))),
        ("S6 unreadable", report.unreadable, len(report.unreadable)),
        ("S7 deactivated kept", report.deactivated_kept, len(report.deactivated_kept)),
        ("mirror rewritten", report.mirrored, len(report.mirrored)),
    )
    for name, items, count in categories:
        logger.info("%s: %s: %d campaigns=[%s]", _PREFIX, name, count, _ids(items))
    logger.info(
        "%s: %d campaign(s) read, %d membership(s) inserted, %d deleted, %d mirror(s) "
        "rewritten, %d reference(s) dropped, %d unreadable",
        _PREFIX,
        report.campaigns,
        len(report.inserted),
        len(report.deleted),
        len(report.mirrored),
        len(report.dropped),
        len(report.unreadable),
    )


def upgrade() -> None:
    reconcile_campaign_locations(op.get_bind())


def downgrade() -> None:
    """A no-op (see the module docstring): the repair destroyed nothing a downgrade could
    restore, and CF-3's downgrade below drops the join table while keeping the JSON."""
