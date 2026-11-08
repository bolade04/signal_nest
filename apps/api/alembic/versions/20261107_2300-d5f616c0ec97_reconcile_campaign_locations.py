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
  valid and every row of the campaign's own tenant. Nothing is written, row for row.
* **S1** a kept location with no correct join row: an id in the JSON added after the back-fill,
  or the location of a cross-tenant row (S8) whose membership is kept. The row is inserted.
* **S2** a join row of the campaign's own tenant whose location is not in the JSON (removed
  after the back-fill): the row is deleted.
* **S3** a JSON id naming a location of another workspace, of the same organization or another,
  or of the campaign's workspace but another organization: dropped and reported, never
  inserted.
* **S4** a JSON element naming no location (deleted or never existing), or an element that is
  not a non-empty string: dropped and reported.
* **S5** an id repeated in the JSON: kept once; each repeat is reported.
* **S6** a JSON value that cannot be read as a list (a JSON ``null``, any JSON value that is not
  an array, or, on SQLite only, text that is not JSON; PostgreSQL's ``json`` type refuses such
  text on input): **the campaign is left exactly as it is** (its JSON, and every join row of
  its own tenant) and reported, because an unreadable source cannot show that a membership is
  wrong. Its cross-tenant rows are the one exception (S8). The column is ``NOT NULL``, so SQL
  ``NULL`` cannot occur (it is still read as unreadable if it ever did), but its ``JSON`` type
  admits any JSON value: neither the model nor CF-3 makes S6 impossible.
* **S7** a deactivated location of the campaign's own workspace and organization: kept (the
  CF-3 back-fill rule: it is history, not corruption) and counted.
* **S8** a cross-tenant join row (D-5DW4-03): a row whose own ``workspace_id`` or
  ``organization_id`` is not its campaign's, or whose location belongs to another workspace or
  organization than the campaign. It is deleted and reported, **whatever the campaign's JSON
  says, S6 included**: no source can make another tenant's row correct. Where its location is
  the campaign's own and the membership is kept (the location is in the kept set, or the
  campaign is S6, whose memberships stay), a correct row is inserted in its place (S1), with a
  new id. Nothing in the schema refuses every such row: the composite keys tie the row, its
  campaign and its location to one *workspace*, but no key ties ``organization_id`` to
  anything but an existing organization, and a location row can itself name another
  organization than its workspace's. No API path writes one (every writer copies the
  campaign's organization and workspace), so it needs a direct database write.

**The exact transformation** (``reconcile_campaign_locations``), inside this revision's own
migration transaction (``env.py`` runs Alembic with ``transaction_per_migration=True``):

1. *Lock and read scope.* On PostgreSQL the revision first takes ``LOCK TABLE campaigns,
   campaign_locations IN SHARE ROW EXCLUSIVE MODE`` (a lock, not DDL): a concurrent write to
   either table waits until this transaction ends, and reads continue. It then reads, in one
   pass and not in batches, every location (id, organization, workspace, active), every
   membership row (id, organization, workspace, campaign, location) and every campaign (id,
   organization, workspace and ``location_ids`` cast to text and parsed here), at the
   session's default isolation (READ COMMITTED on PostgreSQL). On SQLite there is no lock
   statement; SQLite admits one writer at a time.
2. *The cross-tenant rows*, per campaign in id order and before its JSON is read: a join row
   is cross-tenant (S8) when its own organization (``row_foreign_organization``) or workspace
   (``row_foreign_workspace``) is not the campaign's, or when its location's organization
   (``location_foreign_organization``) or workspace (``location_foreign_workspace``) is not the
   campaign's; the first that applies, in that order, is the reason (organization before
   workspace, as in S3). Every other row of the campaign is one of its own tenant: its
   *current* rows.
3. *The kept set*: the JSON's elements in order; a repeat is dropped (S5); an element that is
   not a non-empty string (S4 ``invalid``), or names no location (S4 ``missing``), is dropped;
   a location whose workspace or organization is not the campaign's is dropped (S3
   ``foreign_workspace`` / ``foreign_organization``); every other element is kept, deactivated
   or not (S7). For an S6 campaign the kept set is its current rows' locations plus each S8
   row's location that is the campaign's own.
4. *The deletes, then the inserts.* Every S8 row is deleted by its id, and so is every current
   row whose location is not in the kept set (S2). Then every kept location without a current
   row is inserted with the campaign's organization and workspace (S1). A current row that is
   kept is never touched: not updated, not deleted and re-inserted.
5. *The mirror.* Each readable campaign whose JSON is not already the sorted kept set has it
   rewritten to the sorted kept set, the mirror 5D-W1 maintains. ``updated_at`` is not
   touched. An S6 campaign's JSON is never rewritten.
6. *The report*, on the ``alembic.runtime.migration`` logger, in the CF-3 back-fill's format
   and with ids only (never names, text or a dropped element's content): one line per dropped
   reference (``location=`` only for an existing location, ``None`` otherwise), one per
   cross-tenant row deleted (its campaign, the campaign's workspace, its location and the
   reason), one per unreadable campaign, one per category with its count and campaign ids, and
   a summary, whose ``deleted`` count is the S2 and S8 rows together. Nothing is logged when
   there are no campaigns.

Running it again changes nothing: the kept rows exist, no cross-tenant row is left, the JSON is
the sorted kept set, and nothing is dropped; only the states it leaves as they are by design
(S0, S6, S7) are reported again. A failure anywhere rolls the whole revision back with its
``alembic_version`` update on PostgreSQL (measured in
``app/tests/test_campaign_locations_repair.py``).

**Downgrade: a no-op.** The repair only makes the join table agree with the JSON the API had
written; it destroys nothing a downgrade could restore (every deleted row named a location the
customer had removed or that the tenant does not own, or was a row of another tenant, replaced
by a correct row wherever its membership is kept), and CF-3's own downgrade, one step further,
drops the join table entirely while keeping the JSON.

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
        #: S8 cross-tenant rows deleted: (campaign, workspace, location, reason)
        self.cross_tenant: list[tuple[str, str, str, str]] = []
        self.mirrored: list[str] = []  # JSON rewritten

    def changes(self) -> int:
        return len(self.inserted) + len(self.deleted) + len(self.cross_tenant) + len(self.mirrored)


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


def _cross_tenant(
    row_tenant: tuple[str, str],
    home: tuple[str, str, bool] | None,
    tenant: tuple[str, str],
) -> str | None:
    """S8: why a join row crosses its campaign's tenant, or ``None`` for a row of the campaign's
    own tenant. ``row_tenant`` and ``tenant`` are (organization, workspace) of the row and of
    its campaign; ``home`` is the location's (organization, workspace, active), ``None`` when
    the location does not exist (not a tenant question: S2 or S6 decides that row)."""
    (row_org, row_ws), (organization_id, workspace_id) = row_tenant, tenant
    if row_org != organization_id:
        return "row_foreign_organization"
    if row_ws != workspace_id:
        return "row_foreign_workspace"
    if home is not None:
        if home[0] != organization_id:
            return "location_foreign_organization"
        if home[1] != workspace_id:
            return "location_foreign_workspace"
    return None


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
    #: campaign -> location -> (row id, the row's organization, the row's workspace)
    members: dict[str, dict[str, tuple[str, str, str]]] = {}
    for row in bind.execute(
        sa.select(
            _MEMBERSHIP.c.id,
            _MEMBERSHIP.c.organization_id,
            _MEMBERSHIP.c.workspace_id,
            _MEMBERSHIP.c.campaign_id,
            _MEMBERSHIP.c.location_id,
        )
    ):
        members.setdefault(row.campaign_id, {})[row.location_id] = (
            row.id,
            row.organization_id,
            row.workspace_id,
        )
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

    def insert(campaign_id: str, organization_id: str, workspace_id: str, location_id: str):
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

    for campaign_id, organization_id, workspace_id, raw in campaigns:
        report.campaigns += 1
        tenant = (organization_id, workspace_id)
        # S8 first, whatever the JSON says: a row of another tenant is never a correct row.
        current: dict[str, str] = {}  # location -> row id: the rows of the campaign's own tenant
        replaced: list[str] = []  # the S8 rows' locations that are the campaign's own
        crossings_before = len(report.cross_tenant)
        for location_id, (row_id, row_org, row_ws) in sorted(members.get(campaign_id, {}).items()):
            home = locations.get(location_id)
            reason = _cross_tenant((row_org, row_ws), home, tenant)
            if reason is None:
                current[location_id] = row_id
                continue
            deletes.append(row_id)
            report.cross_tenant.append((campaign_id, workspace_id, location_id, reason))
            if home is not None and home[:2] == tenant:
                replaced.append(location_id)
        value, why = _read(raw)
        if value is None:
            # S6: its memberships stay, so a replaced S8 row's own location gets a correct row.
            report.unreadable.append((campaign_id, workspace_id, why))
            for location_id in replaced:
                insert(campaign_id, organization_id, workspace_id, location_id)
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
        for location_id in sorted(current):
            if location_id not in desired:
                deletes.append(current[location_id])
                report.deleted.append((campaign_id, location_id))
        for location_id in sorted(desired - set(current)):
            insert(campaign_id, organization_id, workspace_id, location_id)
        mirror = sorted(desired)
        if value != mirror:
            mirrors.append({"b_id": campaign_id, "b_ids": mirror})
            report.mirrored.append(campaign_id)
        changed = desired != set(current) or value != mirror
        crossed = len(report.cross_tenant) != crossings_before
        if not changed and not crossed and len(report.dropped) == drops_before:
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
    for campaign_id, workspace_id, location_id, reason in report.cross_tenant:
        logger.warning(
            "%s: cross-tenant row deleted campaign=%s workspace=%s location=%s reason=%s",
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
        ("S8 cross-tenant row deleted", report.cross_tenant, len(report.cross_tenant)),
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
        len(report.deleted) + len(report.cross_tenant),
        len(report.mirrored),
        len(report.dropped),
        len(report.unreadable),
    )


def upgrade() -> None:
    reconcile_campaign_locations(op.get_bind())


def downgrade() -> None:
    """A no-op (see the module docstring): the repair destroyed nothing a downgrade could
    restore, and CF-3's downgrade below drops the join table while keeping the JSON."""
