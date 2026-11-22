"""media asset tables and the media_assets capability (Phase 7A CF-7A-0)

The Phase 7A M0 contracts freeze (media plan v3 §3 M0, "CF-7A-0 (contracts)"). Dark: nothing
writes the new tables until M0-A's services do, behind ``media_assets_enabled`` (default
``False``), and no route is mounted. Contract: ``docs/project-phase-7a-cf7a0-contracts.md``.

* ``media_assets``: one logical asset of one workspace (``kind``, ``state``, ``display_name``,
  ``created_by`` set null with its account), with the unique indexes ``(id, workspace_id)``
  and ``(id, workspace_id, kind)`` that the composite keys below point at.
* ``media_objects``: stored objects and upload records (``storage_key``, ``size_bytes``,
  ``content_type``, ``sha256``, ``version_id``, ``state``). ``(asset_id, workspace_id, kind)``
  is a key onto the asset, so an object's kind is its asset's and a cross-workspace object is
  unrepresentable; ``ck_media_object_kind_content_type`` is the kind/content-type partition
  (no SVG); ``ck_media_object_storage_key`` keeps the key under the row's own
  ``{organization_id}/{workspace_id}/media/`` prefix; ``storage_key`` is unique.
* ``media_provenance``: one record per stored object (``(object_id, workspace_id)`` key);
  ``parent_object_id`` and ``job_id`` are ``SET NULL``.
* ``media_rights``: the MD-4 rights record (``(asset_id, workspace_id)`` key); a customer
  attestation names its text version and instant; ``attested_by`` is set null with its account.
* Every table carries ``organization_id`` and ``workspace_id`` foreign keys, ``CASCADE``.
* ``workspace_capability_overrides``: ``ck_workspace_capability_override_capability`` admits
  ``media_assets`` (the ``PP5-CHK-001`` widening; the model's CHECK derives from
  :func:`app.capabilities.registry.persisted_values`).

CHECK literals are frozen with this revision; ``test_cf7a0_contract_migration`` compares them
with their Python authorities in both directions (``PP5-CHK-001``), because Alembic never
compares CHECK constraints.

Rollback: the downgrade **refuses before any DDL** (Core counts only) while an override names
``media_assets`` (clearing it is an operator decision) or while any media table holds a row
(the rows record objects in storage; dropping them would lose the record of what to erase).
Otherwise it drops the four tables and narrows the CHECK back to the ``53fdde95354a`` set.

Revision ID: 91a796f09399
Revises: 53fdde95354a
Create Date: 2026-11-22 14:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "91a796f09399"
down_revision: str | None = "53fdde95354a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- Frozen CHECK literals (this revision's snapshot of the Python authorities) ---------
_ASSET_KIND = "kind IN ('font', 'raster_image')"
_ASSET_STATE = "state IN ('active', 'pending', 'withdrawn')"
_DISPLAY_NAME = "length(display_name) >= 1"
_KIND_CONTENT_TYPE = (
    "(kind = 'font' AND content_type IN ('font/otf', 'font/ttf', 'font/woff')) OR "
    "(kind = 'raster_image' AND content_type IN ('image/jpeg', 'image/png', 'image/webp'))"
)
_OBJECT_STATE = "state IN ('aborted', 'promoted', 'rejected', 'stored', 'upload_pending')"
_SIZE = "size_bytes > 0"
_SHA256 = "length(sha256) = 64 AND lower(sha256) = sha256"
_STORAGE_KEY = (
    "substr(storage_key, 1, length(organization_id) + length(workspace_id) + 8) = "
    "organization_id || '/' || workspace_id || '/media/'"
)
_ORIGIN = "origin IN ('customer_upload')"
_INPUTS_HASH = "length(inputs_hash) = 64 AND lower(inputs_hash) = inputs_hash"
_BASIS = "basis IN ('customer_attestation')"
_ATTESTATION = (
    "basis <> 'customer_attestation' OR "
    "(attestation_text_version IS NOT NULL AND attested_at IS NOT NULL)"
)
_CAPABILITY_CHECK = "ck_workspace_capability_override_capability"
_CAPABILITY = "media_assets"
_CAPABILITY_IN_WIDE = (
    "capability IN ('connector_rss', 'content_export', 'creative_generation',"
    " 'media_assets', 'notifications_analytics', 'opportunity_feedback',"
    " 'recommendation_briefs', 'scout_scheduling')"
)
#: The ``53fdde95354a`` set, restored by the downgrade.
_CAPABILITY_IN_NARROW = (
    "capability IN ('connector_rss', 'content_export', 'creative_generation',"
    " 'notifications_analytics', 'opportunity_feedback', 'recommendation_briefs',"
    " 'scout_scheduling')"
)

_OVERRIDES = "workspace_capability_overrides"
_ASSETS = "media_assets"
_OBJECTS = "media_objects"
_PROVENANCE = "media_provenance"
_RIGHTS = "media_rights"
#: Created in this order, dropped in the reverse.
_TABLES = (_ASSETS, _OBJECTS, _PROVENANCE, _RIGHTS)
_SCOPE = ("organization_id", "workspace_id")
#: Plain (non-unique) indexes per table, named as the models' ``index=True`` names them.
_INDEXED = {
    _ASSETS: (*_SCOPE, "created_by"),
    _OBJECTS: (*_SCOPE, "asset_id"),
    _PROVENANCE: (*_SCOPE, "parent_object_id", "job_id"),
    _RIGHTS: (*_SCOPE, "asset_id", "attested_by"),
}
#: Unique indexes per table: (name, columns).
_UNIQUE = {
    _ASSETS: (
        ("uq_media_assets_id_workspace", ("id", "workspace_id")),
        ("uq_media_assets_id_workspace_kind", ("id", "workspace_id", "kind")),
    ),
    _OBJECTS: (
        ("uq_media_objects_id_workspace", ("id", "workspace_id")),
        ("uq_media_objects_storage_key", ("storage_key",)),
    ),
    _PROVENANCE: (("uq_media_provenance_object", ("object_id",)),),
    _RIGHTS: (),
}


def _timestamp(name: str) -> sa.Column:
    return sa.Column(
        name,
        sa.DateTime(timezone=True),
        server_default=sa.text("(CURRENT_TIMESTAMP)"),
        nullable=False,
    )


def _scope_columns() -> list[sa.Column]:
    return [
        sa.Column("organization_id", sa.String(length=32), nullable=False),
        sa.Column("workspace_id", sa.String(length=32), nullable=False),
    ]


def _scope_keys() -> list[sa.ForeignKeyConstraint]:
    return [
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE"),
    ]


def _indexes(table: str) -> None:
    for column in _INDEXED[table]:
        op.create_index(f"ix_{table}_{column}", table, [column])
    for name, columns in _UNIQUE[table]:
        op.create_index(name, table, list(columns), unique=True)


# --------------------------------------------------------------------------- #
# Upgrade
# --------------------------------------------------------------------------- #
def upgrade() -> None:
    op.create_table(
        _ASSETS,
        *_scope_columns(),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("created_by", sa.String(length=32), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(_ASSET_KIND, name="ck_media_asset_kind"),
        sa.CheckConstraint(_ASSET_STATE, name="ck_media_asset_state"),
        sa.CheckConstraint(_DISPLAY_NAME, name="ck_media_asset_display_name"),
        *_scope_keys(),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    _indexes(_ASSETS)

    op.create_table(
        _OBJECTS,
        *_scope_columns(),
        sa.Column("asset_id", sa.String(length=32), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("content_type", sa.String(length=64), nullable=False),
        sa.Column("storage_key", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("version_id", sa.String(length=255), nullable=True),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("id", sa.String(length=32), nullable=False),
        _timestamp("created_at"),
        _timestamp("updated_at"),
        sa.CheckConstraint(_KIND_CONTENT_TYPE, name="ck_media_object_kind_content_type"),
        sa.CheckConstraint(_OBJECT_STATE, name="ck_media_object_state"),
        sa.CheckConstraint(_SIZE, name="ck_media_object_size"),
        sa.CheckConstraint(_SHA256, name="ck_media_object_sha256"),
        sa.CheckConstraint(_STORAGE_KEY, name="ck_media_object_storage_key"),
        sa.ForeignKeyConstraint(
            ["asset_id", "workspace_id", "kind"],
            ["media_assets.id", "media_assets.workspace_id", "media_assets.kind"],
            name="fk_media_objects_asset",
            ondelete="CASCADE",
        ),
        *_scope_keys(),
        sa.PrimaryKeyConstraint("id"),
    )
    _indexes(_OBJECTS)

    op.create_table(
        _PROVENANCE,
        *_scope_columns(),
        sa.Column("object_id", sa.String(length=32), nullable=False),
        sa.Column("origin", sa.String(length=32), nullable=False),
        sa.Column("inputs_hash", sa.String(length=64), nullable=False),
        sa.Column("parent_object_id", sa.String(length=32), nullable=True),
        sa.Column("processor", sa.String(length=100), nullable=False),
        sa.Column("scanner", sa.String(length=100), nullable=False),
        sa.Column("job_id", sa.String(length=32), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint(_ORIGIN, name="ck_media_provenance_origin"),
        sa.CheckConstraint(_INPUTS_HASH, name="ck_media_provenance_inputs_hash"),
        sa.ForeignKeyConstraint(
            ["object_id", "workspace_id"],
            ["media_objects.id", "media_objects.workspace_id"],
            name="fk_media_provenance_object",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["parent_object_id"], ["media_objects.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["job_id"], ["jobs.id"], ondelete="SET NULL"),
        *_scope_keys(),
        sa.PrimaryKeyConstraint("id"),
    )
    _indexes(_PROVENANCE)

    op.create_table(
        _RIGHTS,
        *_scope_columns(),
        sa.Column("asset_id", sa.String(length=32), nullable=False),
        sa.Column("basis", sa.String(length=32), nullable=False),
        sa.Column("attestation_text_version", sa.String(length=32), nullable=True),
        sa.Column("attested_by", sa.String(length=32), nullable=True),
        sa.Column("attested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.String(length=32), nullable=False),
        _timestamp("created_at"),
        sa.CheckConstraint(_BASIS, name="ck_media_rights_basis"),
        sa.CheckConstraint(_ATTESTATION, name="ck_media_rights_attestation"),
        sa.ForeignKeyConstraint(
            ["asset_id", "workspace_id"],
            ["media_assets.id", "media_assets.workspace_id"],
            name="fk_media_rights_asset",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(["attested_by"], ["users.id"], ondelete="SET NULL"),
        *_scope_keys(),
        sa.PrimaryKeyConstraint("id"),
    )
    _indexes(_RIGHTS)

    # The capability CHECK admits media_assets.
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
    raise RuntimeError(f"{reason} before downgrading below 91a796f09399. Nothing was changed.")


def downgrade() -> None:
    # Refuse before any DDL (Core counts only; no SQL text is assembled from strings).
    overrides = sa.table(_OVERRIDES, sa.column("capability"))
    named = _count(overrides, overrides.c.capability == _CAPABILITY)
    if named:
        _refuse(f"{named} workspace capability override(s) name '{_CAPABILITY}'; clear them")
    for table in _TABLES:
        rows = _count(sa.table(table))
        if rows:
            _refuse(
                f"{rows} {table} row(s) exist; they record media objects in storage, which a "
                "downgrade would leave unrecorded - erase or resolve them"
            )

    with op.batch_alter_table(_OVERRIDES, schema=None) as batch_op:
        batch_op.drop_constraint(_CAPABILITY_CHECK, type_="check")
        batch_op.create_check_constraint(_CAPABILITY_CHECK, sa.text(_CAPABILITY_IN_NARROW))

    for table in reversed(_TABLES):
        for name, _columns in reversed(_UNIQUE[table]):
            op.drop_index(name, table_name=table)
        for column in reversed(_INDEXED[table]):
            op.drop_index(f"ix_{table}_{column}", table_name=table)
        op.drop_table(table)
