"""Persistence models for media assets (Phase 7A M0; frozen at CF-7A-0, media plan v3).

Four tables, each carrying the Phase 5 tenant scope columns (``organization_id`` and
``workspace_id``, real foreign keys, ``CASCADE``). **Dark:** nothing writes or reads them until
M0-A's services do, behind ``media_assets_enabled`` (default ``False``); no route is mounted.
The contract, with the decisions left to the CF-7A-0 review, is
``docs/project-phase-7a-cf7a0-contracts.md``.

* :class:`MediaAsset` (``media_assets``): one logical asset of one workspace: its closed
  ``kind`` and ``state``, a customer-supplied ``display_name`` and the uploading account
  (``created_by``, set null when that account is deleted).
* :class:`MediaObject` (``media_objects``): one stored object or upload record of an asset:
  ``storage_key``, ``size_bytes``, ``content_type``, ``sha256``, the storage ``version_id``
  and the closed ``state``. Its ``kind`` is its asset's: the composite key
  ``(asset_id, workspace_id, kind)`` points at the asset, and
  ``ck_media_object_kind_content_type`` (the partition :data:`MEDIA_CONTENT_TYPES_BY_KIND`)
  refuses a content type of another kind, and every type release 1 does not accept (SVG
  among them). ``ck_media_object_storage_key`` keeps the key under the row's own
  ``{organization_id}/{workspace_id}/media/`` prefix.
* :class:`MediaProvenanceRecord` (``media_provenance``): how one stored object was made
  (MD-4: "Keep provenance and rights records distinct"): origin, inputs hash, parent object,
  the processing and scanning code paths, and the job. One per object.
* :class:`MediaRightsRecord` (``media_rights``): who asserts which right to use one asset:
  in release 1's M0, the uploading customer's attestation (text version, account and time).
  ``attested_by`` is set null when that account is deleted; the rights content is kept. No
  snapshot of the account is stored, so nothing else in the row names it.

**Workspace consistency.** Every reference between media rows is a composite key that
includes ``workspace_id``, onto a unique ``(id, workspace_id)`` index of its target (the CF-3
``campaign_locations`` pattern, operator decision O-8): a cross-workspace reference is
unrepresentable. The one exception is ``media_provenance.parent_object_id``, a nullable
``SET NULL`` key (a composite ``SET NULL`` would also clear the row's ``workspace_id``); its
workspace is the writer's to check.

**Organization deletion** removes every row through the cascades (MD-6: media is always
erased, never kept as a compliance record). Stored objects are removed by M0-B's
prefix-based, version-aware erasure, which does not depend on these rows.

Not append-only at CF-7A-0: CF-1's guard (``app.db.append_only``) and the PostgreSQL backstop
pin their table sets; whether ``media_provenance`` and ``media_rights`` join them is left to
the CF-7A-0 review.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.enums import (
    MEDIA_CONTENT_TYPES_BY_KIND,
    MediaAssetKind,
    MediaAssetState,
    MediaObjectState,
    MediaProvenanceOrigin,
    MediaRightsBasis,
)
from app.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin

#: The first key segment below the tenant prefix for every media object:
#: ``{organization_id}/{workspace_id}/media/…`` (``app.infra.storage.tenant_object_key``).
MEDIA_KEY_ROOT = "media"
#: The upload area: ``{organization_id}/{workspace_id}/media/uploads/{object_id}``.
MEDIA_UPLOAD_AREA = "uploads"
#: The asset area: ``{organization_id}/{workspace_id}/media/assets/{asset_id}/{object_id}``.
MEDIA_ASSET_AREA = "assets"


def _in_list(column: str, values) -> str:
    rendered = ", ".join(f"'{v}'" for v in sorted(str(v) for v in values))
    return f"{column} IN ({rendered})"


def asset_kind_sql() -> str:
    return _in_list("kind", (k.value for k in MediaAssetKind))


def asset_state_sql() -> str:
    return _in_list("state", (s.value for s in MediaAssetState))


def object_state_sql() -> str:
    return _in_list("state", (s.value for s in MediaObjectState))


def kind_content_type_sql() -> str:
    """The partition CHECK: each kind with exactly its own content types."""
    limbs = [
        f"(kind = '{kind.value}' AND {_in_list('content_type', (t.value for t in types))})"
        for kind, types in sorted(MEDIA_CONTENT_TYPES_BY_KIND.items(), key=lambda kv: kv[0].value)
    ]
    return " OR ".join(limbs)


def provenance_origin_sql() -> str:
    return _in_list("origin", (o.value for o in MediaProvenanceOrigin))


def rights_basis_sql() -> str:
    return _in_list("basis", (b.value for b in MediaRightsBasis))


def sha256_shape_sql(column: str) -> str:
    """64 characters in lower case (portable: SQLite and PostgreSQL). It does not refuse a
    non-hex letter; the writer validates the digest."""
    return f"length({column}) = 64 AND lower({column}) = {column}"


#: The key is under the row's own tenant media prefix, ``{organization_id}/{workspace_id}/media/``
#: (case-sensitive on both engines; ``LIKE`` is not, on SQLite).
STORAGE_KEY_SQL = (
    "substr(storage_key, 1, length(organization_id) + length(workspace_id) + 8) = "
    "organization_id || '/' || workspace_id || '/media/'"
)
#: A customer attestation names its wording's version and its instant (the account may later
#: be erased, so ``attested_by`` is not required).
ATTESTATION_SQL = (
    "basis <> 'customer_attestation' OR "
    "(attestation_text_version IS NOT NULL AND attested_at IS NOT NULL)"
)


class MediaAsset(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One logical media asset of one workspace."""

    __tablename__ = "media_assets"
    __table_args__ = (
        CheckConstraint(asset_kind_sql(), name="ck_media_asset_kind"),
        CheckConstraint(asset_state_sql(), name="ck_media_asset_state"),
        CheckConstraint("length(display_name) >= 1", name="ck_media_asset_display_name"),
        # Targets of the workspace-consistency keys of objects and rights records.
        Index("uq_media_assets_id_workspace", "id", "workspace_id", unique=True),
        Index("uq_media_assets_id_workspace_kind", "id", "workspace_id", "kind", unique=True),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Customer-supplied label (for example the uploaded file's name). Tenant content.
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    #: The uploading account; set null when it is deleted (the asset is kept).
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )


class MediaObject(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One stored object, or one upload record, of a media asset."""

    __tablename__ = "media_objects"
    __table_args__ = (
        ForeignKeyConstraint(
            ["asset_id", "workspace_id", "kind"],
            ["media_assets.id", "media_assets.workspace_id", "media_assets.kind"],
            name="fk_media_objects_asset",
            ondelete="CASCADE",
        ),
        CheckConstraint(kind_content_type_sql(), name="ck_media_object_kind_content_type"),
        CheckConstraint(object_state_sql(), name="ck_media_object_state"),
        CheckConstraint("size_bytes > 0", name="ck_media_object_size"),
        CheckConstraint(sha256_shape_sql("sha256"), name="ck_media_object_sha256"),
        CheckConstraint(STORAGE_KEY_SQL, name="ck_media_object_storage_key"),
        Index("uq_media_objects_id_workspace", "id", "workspace_id", unique=True),
        Index("uq_media_objects_storage_key", "storage_key", unique=True),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    asset_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    #: Always the asset's kind (it is part of the key onto the asset).
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    content_type: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The full object key, under ``{organization_id}/{workspace_id}/media/``.
    storage_key: Mapped[str] = mapped_column(String(255), nullable=False)
    #: For an upload record, the declared size; for a stored object, its stored size.
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: Lower-case hex sha256: declared (upload record) or of the stored bytes (stored object).
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The storage version id of the stored bytes (S3); NULL where the backend has none.
    version_id: Mapped[str | None] = mapped_column(String(255))
    state: Mapped[str] = mapped_column(String(32), nullable=False)


class MediaProvenanceRecord(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """How one stored media object was made. No rights field (MD-4)."""

    __tablename__ = "media_provenance"
    __table_args__ = (
        ForeignKeyConstraint(
            ["object_id", "workspace_id"],
            ["media_objects.id", "media_objects.workspace_id"],
            name="fk_media_provenance_object",
            ondelete="CASCADE",
        ),
        CheckConstraint(provenance_origin_sql(), name="ck_media_provenance_origin"),
        CheckConstraint(sha256_shape_sql("inputs_hash"), name="ck_media_provenance_inputs_hash"),
        Index("uq_media_provenance_object", "object_id", unique=True),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    object_id: Mapped[str] = mapped_column(String(32), nullable=False)
    origin: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Lower-case hex sha256 of the inputs: for an upload, of the bytes as uploaded.
    inputs_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    #: The object it was made from (for an upload, its upload record), if still present.
    parent_object_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_objects.id", ondelete="SET NULL"), index=True
    )
    #: Identifier and version of the code path that produced the bytes (e.g. the sanitiser).
    processor: Mapped[str] = mapped_column(String(100), nullable=False)
    #: Identifier of the scanner that cleared the bytes (the M0-A0 scanning seam).
    scanner: Mapped[str] = mapped_column(String(100), nullable=False)
    #: The durable job that made it, if any; set null when that job is purged.
    job_id: Mapped[str | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), index=True
    )


class MediaRightsRecord(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Who asserts which right to use one media asset. No provenance field (MD-4)."""

    __tablename__ = "media_rights"
    __table_args__ = (
        ForeignKeyConstraint(
            ["asset_id", "workspace_id"],
            ["media_assets.id", "media_assets.workspace_id"],
            name="fk_media_rights_asset",
            ondelete="CASCADE",
        ),
        CheckConstraint(rights_basis_sql(), name="ck_media_rights_basis"),
        CheckConstraint(ATTESTATION_SQL, name="ck_media_rights_attestation"),
    )

    organization_id: Mapped[str] = mapped_column(
        ForeignKey("organizations.id", ondelete="CASCADE"), index=True, nullable=False
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True, nullable=False
    )
    asset_id: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    basis: Mapped[str] = mapped_column(String(32), nullable=False)
    #: The version of the attestation wording the customer accepted.
    attestation_text_version: Mapped[str | None] = mapped_column(String(32))
    #: The attesting account; set null when it is deleted (the rights content is kept).
    attested_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), index=True
    )
    attested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "ATTESTATION_SQL",
    "MEDIA_ASSET_AREA",
    "MEDIA_KEY_ROOT",
    "MEDIA_UPLOAD_AREA",
    "STORAGE_KEY_SQL",
    "MediaAsset",
    "MediaObject",
    "MediaProvenanceRecord",
    "MediaRightsRecord",
    "asset_kind_sql",
    "asset_state_sql",
    "kind_content_type_sql",
    "object_state_sql",
    "provenance_origin_sql",
    "rights_basis_sql",
    "sha256_shape_sql",
]
