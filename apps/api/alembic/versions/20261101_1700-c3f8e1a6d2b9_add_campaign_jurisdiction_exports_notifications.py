"""campaign hardening, jurisdiction axis, exports, notifications (Project Phase 5 CF-3)

The consolidated Phase 5D + 5E contracts freeze (W21 CF-3). Dark: nothing writes the new tables
or columns until the 5D/5E build lanes add their services, and the one new capability flag
(``content_export_enabled``) defaults ``False``.

**5D campaign hardening.**

* ``campaigns.mode`` / ``campaigns.status`` become closed vocabularies (``ck_campaign_mode``,
  ``ck_campaign_status``). Every stored value is enumerated and normalised **before** the
  CHECKs are installed (case, surrounding space, ``-`` and inner spaces as ``_``); a value that
  still names no member is rewritten - ``mode`` to ``per_location`` (the column default),
  ``status`` to ``paused`` (never promoted to ``active``) - and each rewrite is reported in the
  migration output. The original free-string value is not kept (the downgrade cannot restore
  it).
* ``campaign_locations`` (operator decision O-8, ``REC_campaign_locations_join_table_FKs_backfill``):
  real foreign keys plus two composite keys, ``(campaign_id, workspace_id)`` and
  ``(location_id, workspace_id)``, onto new unique indexes ``(id, workspace_id)`` of
  ``campaigns`` and ``business_locations`` - the workspace-consistency constraint: a
  cross-workspace membership is unrepresentable. **Back-filled fail-closed** from
  ``campaigns.location_ids`` (which is kept): every id is checked against the campaign's own
  workspace; only ids of a location of that workspace are migrated (a deactivated one too: it
  is history, not corruption, as ``app.campaign_context.location_refs`` classifies it); an id
  naming no location or a location of another workspace, and a value that is not a list of
  ids, is **dropped and reported** in the migration output, never silently and never
  cross-tenant. The back-fill inserts only missing pairs, so running it again changes nothing.

**5D jurisdiction axis.** Nullable ``jurisdiction_code`` (``String(6)``) on
``business_locations``, ``geo_coverage_rules``, ``recommendation_briefs``, ``creative_drafts`` and
``content_reviews``, each with a portable shape CHECK (``app.jurisdiction.codes``) that refuses
the resolver's ``UNRESOLVED`` sentinel; ``claims_library.jurisdiction_codes`` (JSON; NULL or
empty = every jurisdiction).

**5E.** ``exports`` (append-only in the ORM; FK-less scope per O-6; ``jurisdiction_code`` NOT NULL
with no ``IS NULL`` limb - native from birth); ``notifications`` and ``notification_preferences``
(organization / workspace / user foreign keys, all CASCADE; closed ``event_code``); the
retention marker ``deleted_at`` on ``recommendation_briefs``, ``creative_drafts`` and
``content_reviews`` (and on ``notifications``); the capability CHECK admits ``content_export``.

CHECK literals are frozen with this revision; ``test_cf3_contract_migration`` compares them with
their Python authorities in both directions.

Rollback: the downgrade **refuses before any DDL** (Core counts only) while an override names
``content_export``, while ``exports`` holds a row (compliance records, O-6), while a brief,
draft or review holds a captured jurisdiction (a snapshot cannot be re-captured faithfully),
while any row carries ``deleted_at`` (dropping it would resurrect content marked for purge),
or while a claims-library entry is jurisdiction-restricted (dropping the restriction would
widen the entry to every jurisdiction). Otherwise it reverses every step; the
``campaign_locations`` rows are dropped (the kept ``location_ids`` arrays still hold every
migrated id) and the campaign mode/status normalisation is not undone.

Revision ID: c3f8e1a6d2b9
Revises: a7d2e9f4c1b8
Create Date: 2026-11-01 17:00:00.000000
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c3f8e1a6d2b9"
down_revision: str | None = "a7d2e9f4c1b8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

logger = logging.getLogger("alembic.runtime.migration")

# --- Frozen vocabularies (this revision's snapshot of the Python authorities) ----------
_MODES = ("grouped", "per_location", "recommend", "same_for_all")
_STATUSES = ("active", "archived", "completed", "draft", "paused")
_MODE_FALLBACK = "per_location"
_STATUS_FALLBACK = "paused"
_MODE_CHECK = "mode IN ('grouped', 'per_location', 'recommend', 'same_for_all')"
_STATUS_CHECK = "status IN ('active', 'archived', 'completed', 'draft', 'paused')"
_JURISDICTION_SHAPE = (
    "upper(jurisdiction_code) = jurisdiction_code AND (length(jurisdiction_code) = 2 OR "
    "(length(jurisdiction_code) BETWEEN 4 AND 6 AND substr(jurisdiction_code, 3, 1) = '-'))"
)
_JURISDICTION_NULLABLE = (
    "jurisdiction_code IS NULL OR (upper(jurisdiction_code) = jurisdiction_code AND "
    "(length(jurisdiction_code) = 2 OR (length(jurisdiction_code) BETWEEN 4 AND 6 AND "
    "substr(jurisdiction_code, 3, 1) = '-')))"
)
_FORMAT_CHECK = "format IN ('json', 'text/markdown', 'text/plain')"
_EVENT_CHECK = (
    "event_code IN ('approval.decided', 'brief.created', 'draft.generated',"
    " 'export.completed', 'review.blocked')"
)
_CAPABILITY_CHECK = "ck_workspace_capability_override_capability"
_CAPABILITY_IN_WIDE = (
    "capability IN ('connector_rss', 'content_export', 'creative_generation',"
    " 'opportunity_feedback', 'recommendation_briefs', 'scout_scheduling')"
)
_CAPABILITY_IN_NARROW = (
    "capability IN ('connector_rss', 'creative_generation', 'opportunity_feedback',"
    " 'recommendation_briefs', 'scout_scheduling')"
)

_OVERRIDES = "workspace_capability_overrides"
_MEMBERSHIP = "campaign_locations"
_EXPORTS = "exports"
_NOTIFICATIONS = "notifications"
_PREFERENCES = "notification_preferences"
#: Existing tables gaining a jurisdiction column, with their CHECK names.
_LOCATED = (
    ("business_locations", "ck_business_location_jurisdiction_code"),
    ("geo_coverage_rules", "ck_geo_coverage_rule_jurisdiction_code"),
)
#: Existing Phase 5 content tables gaining a jurisdiction snapshot and ``deleted_at``.
_SNAPSHOTTED = (
    ("recommendation_briefs", "ck_recommendation_brief_jurisdiction_code"),
    ("creative_drafts", "ck_creative_draft_jurisdiction_code"),
    ("content_reviews", "ck_content_review_jurisdiction_code"),
)
_MEMBERSHIP_INDEXED = ("organization_id", "workspace_id", "campaign_id", "location_id")
_EXPORT_INDEXED = ("organization_id", "workspace_id", "draft_id", "approval_id", "exported_by")
_SCOPE_INDEXED = ("organization_id", "workspace_id", "user_id")


def _created_at() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(timezone=True),
        server_default=sa.text("(CURRENT_TIMESTAMP)"),
        nullable=False,
    )


# --------------------------------------------------------------------------- #
# Data steps (Core only; every value is a bound parameter)
# --------------------------------------------------------------------------- #
def _normalised(value: object, members: tuple[str, ...]) -> str | None:
    if not isinstance(value, str):
        return None
    key = "_".join(value.strip().lower().replace("-", " ").split())
    return key if key in members else None


def normalise_campaign_vocabularies(bind) -> list[tuple[str, str, object, str]]:
    """Rewrite every campaign mode/status that is not a member; report each rewrite.

    Returns ``(campaign id, column, stored value, new value)`` per rewritten value.
    """
    campaigns = sa.table("campaigns", sa.column("id"), sa.column("mode"), sa.column("status"))
    rewrites: list[tuple[str, str, object, str]] = []
    rows = bind.execute(
        sa.select(campaigns.c.id, campaigns.c.mode, campaigns.c.status).order_by(campaigns.c.id)
    ).all()
    for campaign_id, mode, status in rows:
        values: dict[str, str] = {}
        for column, stored, members, fallback in (
            ("mode", mode, _MODES, _MODE_FALLBACK),
            ("status", status, _STATUSES, _STATUS_FALLBACK),
        ):
            new = _normalised(stored, members) or fallback
            if new != stored:
                values[column] = new
                rewrites.append((campaign_id, column, stored, new))
        if values:
            bind.execute(campaigns.update().where(campaigns.c.id == campaign_id).values(values))
    for campaign_id, column, stored, new in rewrites:
        shown = repr(stored)[:60]
        logger.warning(
            "CF-3 campaign %s: campaign=%s stored=%s rewritten_to=%s",
            column,
            campaign_id,
            shown,
            new,
        )
    logger.info("CF-3 campaign vocabularies: %d value(s) rewritten", len(rewrites))
    return rewrites


def backfill_campaign_locations(bind) -> tuple[int, list[tuple[str, str, object, str]]]:
    """Fill ``campaign_locations`` from ``campaigns.location_ids``, fail-closed, idempotent.

    Returns ``(rows inserted, dropped)``, ``dropped`` being ``(campaign id, workspace id,
    stored location id or None, reason)`` with reason ``malformed`` (the stored value is not a
    list of ids), ``missing`` (no such location) or ``foreign_workspace`` (a location of
    another workspace). Only missing pairs are inserted.
    """
    campaigns = sa.table(
        "campaigns",
        sa.column("id"),
        sa.column("organization_id"),
        sa.column("workspace_id"),
        sa.column("location_ids", sa.JSON),
    )
    locations = sa.table("business_locations", sa.column("id"), sa.column("workspace_id"))
    membership = sa.table(
        _MEMBERSHIP,
        sa.column("id"),
        sa.column("organization_id"),
        sa.column("workspace_id"),
        sa.column("campaign_id"),
        sa.column("location_id"),
    )
    where = dict(bind.execute(sa.select(locations.c.id, locations.c.workspace_id)).all())
    present = {
        (row.campaign_id, row.location_id)
        for row in bind.execute(sa.select(membership.c.campaign_id, membership.c.location_id))
    }
    inserts: list[dict[str, str]] = []
    dropped: list[tuple[str, str, object, str]] = []
    rows = bind.execute(
        sa.select(
            campaigns.c.id,
            campaigns.c.organization_id,
            campaigns.c.workspace_id,
            campaigns.c.location_ids,
        ).order_by(campaigns.c.id)
    ).all()
    for campaign_id, organization_id, workspace_id, value in rows:
        if value is None or value == []:
            continue
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            dropped.append((campaign_id, workspace_id, None, "malformed"))
            continue
        for location_id in dict.fromkeys(value):
            home = where.get(location_id)
            if home is None:
                dropped.append((campaign_id, workspace_id, location_id, "missing"))
            elif home != workspace_id:
                dropped.append((campaign_id, workspace_id, location_id, "foreign_workspace"))
            elif (campaign_id, location_id) not in present:
                present.add((campaign_id, location_id))
                inserts.append(
                    {
                        "id": uuid.uuid4().hex,
                        "organization_id": organization_id,
                        "workspace_id": workspace_id,
                        "campaign_id": campaign_id,
                        "location_id": location_id,
                    }
                )
    if inserts:
        bind.execute(membership.insert(), inserts)
    for campaign_id, workspace_id, location_id, reason in dropped:
        logger.warning(
            "CF-3 campaign_locations back-fill: dropped campaign=%s workspace=%s location=%s "
            "reason=%s",
            campaign_id,
            workspace_id,
            location_id,
            reason,
        )
    logger.info(
        "CF-3 campaign_locations back-fill: %d membership(s) inserted, %d reference(s) dropped",
        len(inserts),
        len(dropped),
    )
    return len(inserts), dropped


# --------------------------------------------------------------------------- #
# Upgrade
# --------------------------------------------------------------------------- #
def upgrade() -> None:
    bind = op.get_bind()

    # 5D campaign hardening: enumerate and normalise first, so the CHECKs cannot strand data.
    normalise_campaign_vocabularies(bind)
    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.create_check_constraint("ck_campaign_mode", sa.text(_MODE_CHECK))
        batch_op.create_check_constraint("ck_campaign_status", sa.text(_STATUS_CHECK))
    op.create_index(
        "uq_campaigns_id_workspace", "campaigns", ["id", "workspace_id"], unique=True
    )

    # 5D jurisdiction axis on the location tables.
    for table, check in _LOCATED:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("jurisdiction_code", sa.String(length=6), nullable=True)
            )
            batch_op.create_check_constraint(check, sa.text(_JURISDICTION_NULLABLE))
    op.create_index(
        "uq_business_locations_id_workspace",
        "business_locations",
        ["id", "workspace_id"],
        unique=True,
    )

    # O-8: the campaign membership join table, then its fail-closed back-fill.
    op.create_table(
        _MEMBERSHIP,
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("campaign_id", sa.String(length=32), nullable=False),
        sa.Column("location_id", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        _created_at(),
        sa.ForeignKeyConstraint(
            ["campaign_id", "workspace_id"],
            ["campaigns.id", "campaigns.workspace_id"],
            name="fk_campaign_locations_campaign_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["location_id", "workspace_id"],
            ["business_locations.id", "business_locations.workspace_id"],
            name="fk_campaign_locations_location_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("campaign_id", "location_id", name="uq_campaign_location"),
    )
    for column in _MEMBERSHIP_INDEXED:
        op.create_index(f"ix_{_MEMBERSHIP}_{column}", _MEMBERSHIP, [column])
    backfill_campaign_locations(bind)

    # 5D propagation snapshots and the 5E retention marker on the Phase 5 content tables.
    for table, check in _SNAPSHOTTED:
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.add_column(
                sa.Column("jurisdiction_code", sa.String(length=6), nullable=True)
            )
            batch_op.add_column(
                sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True)
            )
            batch_op.create_check_constraint(check, sa.text(_JURISDICTION_NULLABLE))

    # 5D jurisdiction-conditioned claim rules (NULL or empty = every jurisdiction).
    with op.batch_alter_table("claims_library", schema=None) as batch_op:
        batch_op.add_column(sa.Column("jurisdiction_codes", sa.JSON(), nullable=True))

    # 5E audited manual export: born jurisdiction-aware.
    op.create_table(
        _EXPORTS,
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("draft_id", sa.String(length=32), nullable=True),
        sa.Column("draft_version", sa.Integer(), nullable=False),
        sa.Column("approval_id", sa.String(length=32), nullable=True),
        sa.Column("format", sa.String(length=32), nullable=False),
        sa.Column("jurisdiction_code", sa.String(length=6), nullable=False),
        sa.Column("exported_by", sa.String(length=32), nullable=True),
        sa.Column("exported_by_snapshot", sa.String(length=600), nullable=False),
        sa.Column("is_simulated", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        _created_at(),
        sa.CheckConstraint("draft_version >= 1", name="ck_export_draft_version"),
        sa.CheckConstraint(_FORMAT_CHECK, name="ck_export_format"),
        sa.CheckConstraint(_JURISDICTION_SHAPE, name="ck_export_jurisdiction_code"),
        sa.ForeignKeyConstraint(["approval_id"], ["approvals.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["draft_id"], ["creative_drafts.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["exported_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in _EXPORT_INDEXED:
        op.create_index(f"ix_{_EXPORTS}_{column}", _EXPORTS, [column])

    # 5E in-app notifications and per-user preferences.
    op.create_table(
        _NOTIFICATIONS,
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("event_code", sa.String(length=32), nullable=False),
        sa.Column("subject_id", sa.String(length=32), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        _created_at(),
        sa.CheckConstraint(_EVENT_CHECK, name="ck_notification_event_code"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    for column in _SCOPE_INDEXED:
        op.create_index(f"ix_{_NOTIFICATIONS}_{column}", _NOTIFICATIONS, [column])
    op.create_index(
        "ix_notifications_feed", _NOTIFICATIONS, ["workspace_id", "user_id", "created_at"]
    )

    op.create_table(
        _PREFERENCES,
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
        sa.Column("user_id", sa.String(length=32), nullable=False),
        sa.Column("event_code", sa.String(length=32), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        _created_at(),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
        ),
        sa.CheckConstraint(_EVENT_CHECK, name="ck_notification_preference_event_code"),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "workspace_id", "user_id", "event_code", name="uq_notification_preference"
        ),
    )
    for column in _SCOPE_INDEXED:
        op.create_index(f"ix_{_PREFERENCES}_{column}", _PREFERENCES, [column])

    # The capability CHECK admits content_export.
    with op.batch_alter_table(_OVERRIDES, schema=None) as batch_op:
        batch_op.drop_constraint(_CAPABILITY_CHECK, type_="check")
        batch_op.create_check_constraint(_CAPABILITY_CHECK, sa.text(_CAPABILITY_IN_WIDE))


# --------------------------------------------------------------------------- #
# Downgrade
# --------------------------------------------------------------------------- #
def _count(table: sa.TableClause, *where) -> int:
    query = sa.select(sa.func.count()).select_from(table)
    if where:
        query = query.where(*where)
    return op.get_bind().execute(query).scalar_one()


def _refuse(reason: str) -> None:
    raise RuntimeError(f"{reason} before downgrading below c3f8e1a6d2b9. Nothing was changed.")


def downgrade() -> None:
    # Refuse before any DDL (Core counts only; no SQL text is assembled from strings).
    overrides = sa.table(_OVERRIDES, sa.column("capability"))
    named = _count(overrides, overrides.c.capability == "content_export")
    if named:
        _refuse(f"{named} workspace capability override(s) name 'content_export'; clear them")
    exports = _count(sa.table(_EXPORTS))
    if exports:
        _refuse(
            f"{exports} export record(s) exist; exports are compliance records (O-6) and are "
            "not dropped by a downgrade - resolve them"
        )
    for table, _check in _SNAPSHOTTED:
        snap = sa.table(table, sa.column("jurisdiction_code"))
        captured = _count(snap, snap.c.jurisdiction_code.is_not(None))
        if captured:
            _refuse(
                f"{captured} {table} row(s) hold a captured jurisdiction snapshot, which a "
                "downgrade would lose and cannot re-capture; resolve them"
            )
    for table in (*(t for t, _c in _SNAPSHOTTED), _NOTIFICATIONS):
        marked_table = sa.table(table, sa.column("deleted_at"))
        marked = _count(marked_table, marked_table.c.deleted_at.is_not(None))
        if marked:
            _refuse(
                f"{marked} {table} row(s) are marked deleted_at; dropping the marker would "
                "resurrect them - purge or clear them"
            )
    claims = sa.table("claims_library", sa.column("jurisdiction_codes", sa.JSON))
    restricted = _count(
        claims,
        claims.c.jurisdiction_codes.is_not(None),
        sa.cast(claims.c.jurisdiction_codes, sa.Text).not_in(["[]", "null"]),
    )
    if restricted:
        _refuse(
            f"{restricted} claims-library entr(y/ies) are jurisdiction-restricted; dropping "
            "jurisdiction_codes would apply them in every jurisdiction - resolve them"
        )

    with op.batch_alter_table(_OVERRIDES, schema=None) as batch_op:
        batch_op.drop_constraint(_CAPABILITY_CHECK, type_="check")
        batch_op.create_check_constraint(_CAPABILITY_CHECK, sa.text(_CAPABILITY_IN_NARROW))

    for column in reversed(_SCOPE_INDEXED):
        op.drop_index(f"ix_{_PREFERENCES}_{column}", table_name=_PREFERENCES)
    op.drop_table(_PREFERENCES)
    op.drop_index("ix_notifications_feed", table_name=_NOTIFICATIONS)
    for column in reversed(_SCOPE_INDEXED):
        op.drop_index(f"ix_{_NOTIFICATIONS}_{column}", table_name=_NOTIFICATIONS)
    op.drop_table(_NOTIFICATIONS)
    for column in reversed(_EXPORT_INDEXED):
        op.drop_index(f"ix_{_EXPORTS}_{column}", table_name=_EXPORTS)
    op.drop_table(_EXPORTS)

    with op.batch_alter_table("claims_library", schema=None) as batch_op:
        batch_op.drop_column("jurisdiction_codes")

    for table, check in reversed(_SNAPSHOTTED):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(check, type_="check")
            batch_op.drop_column("deleted_at")
            batch_op.drop_column("jurisdiction_code")

    for column in reversed(_MEMBERSHIP_INDEXED):
        op.drop_index(f"ix_{_MEMBERSHIP}_{column}", table_name=_MEMBERSHIP)
    op.drop_table(_MEMBERSHIP)

    op.drop_index("uq_business_locations_id_workspace", table_name="business_locations")
    for table, check in reversed(_LOCATED):
        with op.batch_alter_table(table, schema=None) as batch_op:
            batch_op.drop_constraint(check, type_="check")
            batch_op.drop_column("jurisdiction_code")

    op.drop_index("uq_campaigns_id_workspace", table_name="campaigns")
    with op.batch_alter_table("campaigns", schema=None) as batch_op:
        batch_op.drop_constraint("ck_campaign_status", type_="check")
        batch_op.drop_constraint("ck_campaign_mode", type_="check")
