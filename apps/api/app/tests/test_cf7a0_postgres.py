"""Phase 7A CF-7A-0: the migration ``91a796f09399`` on PostgreSQL.

``test_cf7a0_contract_migration`` proves the revision on SQLite; this module proves, on a
PostgreSQL database of its own (created and dropped here), what a dialect can change:

* ``upgrade head`` reaches ``91a796f09399`` and ``alembic check`` reports no drift;
* **``PP5-CHK-001`` on PostgreSQL:** every CHECK on the four media tables, read back with
  ``pg_get_constraintdef`` (PostgreSQL rewrites ``IN`` lists as ``= ANY (ARRAY[…])`` and a
  one-member list as ``=``), equals its Python authority in both directions; the
  kind/content-type CHECK equals the partition; the capability CHECK equals
  ``persisted_values()``; and discovery finds no CHECK this module does not know;
* the CHECKs and the composite workspace-consistency keys refuse what they must, the storage
  key prefix is case-sensitive, and an organization's deletion cascades every media row;
* the downgrade **refuses before any DDL** while a media table holds a row; once it is clear,
  one step down leaves no media table and the narrowed CHECK, and ``upgrade`` restores the head
  with no drift.

Gate: :func:`require_postgres` — with ``TEST_POSTGRES_URL`` unset these tests FAIL in CI and
skip only on a developer machine.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError

from app.capabilities.registry import persisted_values
from app.core.enums import (
    MEDIA_CONTENT_TYPES_BY_KIND,
    MediaAssetKind,
    MediaAssetState,
    MediaObjectState,
    MediaProvenanceOrigin,
    MediaRightsBasis,
)
from app.tests._auth2_support import pg_database, require_postgres
from app.tests.test_postgres_migration_proof import (
    _alembic,
    _confirmation,
    _run,
    _tables,
    _version,
)

CF7A0 = "91a796f09399"
PREV = "53fdde95354a"
MEDIA_TABLES = {"media_assets", "media_objects", "media_provenance", "media_rights"}
EXPECTED_CHECKS = {
    "media_assets": {
        "ck_media_asset_kind",
        "ck_media_asset_state",
        "ck_media_asset_display_name",
    },
    "media_objects": {
        "ck_media_object_kind_content_type",
        "ck_media_object_state",
        "ck_media_object_size",
        "ck_media_object_sha256",
        "ck_media_object_storage_key",
    },
    "media_provenance": {"ck_media_provenance_origin", "ck_media_provenance_inputs_hash"},
    "media_rights": {"ck_media_rights_basis", "ck_media_rights_attestation"},
}
VOCABULARIES = (
    ("media_assets", "ck_media_asset_kind", "kind", MediaAssetKind),
    ("media_assets", "ck_media_asset_state", "state", MediaAssetState),
    ("media_objects", "ck_media_object_state", "state", MediaObjectState),
    ("media_provenance", "ck_media_provenance_origin", "origin", MediaProvenanceOrigin),
    ("media_rights", "ck_media_rights_basis", "basis", MediaRightsBasis),
)
SHA = "b" * 64


@pytest.fixture(autouse=True)
def _postgres_required() -> None:
    require_postgres()


@pytest.fixture
def pg_url() -> Iterator[str]:
    with pg_database("sn_cf7a0") as url:
        yield url


@pytest.fixture
def migrated(pg_url: str) -> str:
    _alembic(pg_url, "upgrade", "head")
    return pg_url


def _checks(url: str, table: str) -> dict[str, str]:
    query = sa.text(
        "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
        "WHERE conrelid = CAST(:table AS regclass) AND contype = 'c'"
    )
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            return dict(conn.execute(query, {"table": table}).all())
    finally:
        engine.dispose()


def _literals(definition: str, column: str) -> set[str]:
    """The literal set of a one-column vocabulary CHECK as PostgreSQL renders it."""
    assert f"(({column})::text" in definition, definition
    assert definition.count("::text =") == 1, definition  # one comparison: no other limb
    return set(re.findall(r"'([^']*)'::(?:character varying|text)", definition))


def _partition(definition: str) -> dict[str, frozenset[str]]:
    limbs = re.findall(
        r"\(\(kind\)::text = '([^']+)'::text\) AND \(\(content_type\)::text = ANY "
        r"\(\(ARRAY\[([^\]]*)\]\)",
        definition,
    )
    assert len(limbs) == definition.count("(kind)::text ="), definition
    partition = {k: frozenset(re.findall(r"'([^']*)'::character varying", v)) for k, v in limbs}
    assert len(partition) == len(limbs), "a kind appears in two limbs"
    return partition


def _insert(url: str, table: str, row: dict) -> None:
    clause = sa.table(table, *(sa.column(c) for c in row))
    engine = create_engine(url)
    try:
        with engine.begin() as conn:
            conn.execute(clause.insert(), [row])
    finally:
        engine.dispose()


def _count(url: str, table: str) -> int:
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            query = sa.select(sa.func.count()).select_from(sa.table(table))
            return conn.execute(query).scalar_one()
    finally:
        engine.dispose()


def _world(url: str) -> None:
    _insert(url, "organizations", {"id": "o1", "name": "O", "slug": "o"})
    for ws in ("w1", "w2"):
        _insert(url, "workspaces", {
            "id": ws, "organization_id": "o1", "name": ws, "slug": ws,
            "onboarding_completed": True,
        })
    _insert(url, "users", {
        "id": "u1", "email": "u1@example.test", "full_name": "U", "hashed_password": "x",
        "is_active": True,
    })


def _asset(url: str, row_id: str, **over) -> None:
    row = {"id": row_id, "organization_id": "o1", "workspace_id": "w1", "kind": "raster_image",
           "state": "active", "display_name": "logo.png", "created_by": "u1"}
    _insert(url, "media_assets", {**row, **over})


def _object(url: str, row_id: str, **over) -> None:
    row = {"id": row_id, "organization_id": "o1", "workspace_id": "w1", "asset_id": "a1",
           "kind": "raster_image", "content_type": "image/png",
           "storage_key": f"o1/w1/media/assets/a1/{row_id}", "size_bytes": 10, "sha256": SHA,
           "state": "stored"}
    _insert(url, "media_objects", {**row, **over})


def test_upgrade_reaches_the_head_with_no_drift(migrated: str) -> None:
    assert _version(migrated) == [CF7A0]
    assert MEDIA_TABLES <= _tables(migrated)
    result = _run(["check"], migrated)
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_check_equals_its_authority_on_postgresql(migrated: str) -> None:
    for table, names in EXPECTED_CHECKS.items():
        assert set(_checks(migrated, table)) == names, table  # discovery
    compared = 0
    for table, name, column, authority in VOCABULARIES:
        in_db = _literals(_checks(migrated, table)[name], column)
        assert in_db == {m.value for m in authority}, name
        compared += 1
    assert compared > 0
    partition = _partition(_checks(migrated, "media_objects")["ck_media_object_kind_content_type"])
    assert partition == {
        k.value: frozenset(t.value for t in v) for k, v in MEDIA_CONTENT_TYPES_BY_KIND.items()
    }
    overrides = _checks(migrated, "workspace_capability_overrides")
    capability = _literals(overrides["ck_workspace_capability_override_capability"], "capability")
    assert capability == set(persisted_values())
    assert "media_assets" in capability


def test_the_checks_and_keys_refuse_on_postgresql(migrated: str) -> None:
    _world(migrated)
    _asset(migrated, "a1")
    _object(migrated, "ob1")
    for row_id, over in (
        ("svg", {"content_type": "image/svg+xml"}),
        ("font-type", {"content_type": "font/ttf"}),
        ("kind-mismatch", {"kind": "font", "content_type": "font/ttf"}),
        ("upper-sha", {"sha256": "B" * 64}),
        ("case-folded-key", {"storage_key": "o1/w1/MEDIA/assets/a1/x"}),
        ("other-ws-key", {"storage_key": "o1/w2/media/assets/a1/x"}),
        ("cross-workspace", {"workspace_id": "w2", "storage_key": "o1/w2/media/assets/a1/x"}),
        ("duplicate-key", {"storage_key": "o1/w1/media/assets/a1/ob1"}),
        ("empty", {"size_bytes": 0}),
    ):
        with pytest.raises(IntegrityError):
            _object(migrated, row_id, **over)
    with pytest.raises(IntegrityError):
        _insert(migrated, "media_rights", {
            "id": "r-no-version", "organization_id": "o1", "workspace_id": "w1",
            "asset_id": "a1", "basis": "customer_attestation",
            "attestation_text_version": None, "attested_at": None,
        })
    _insert(migrated, "workspace_capability_overrides", {
        "id": "ov1", "organization_id": "o1", "workspace_id": "w1",
        "capability": "media_assets", "enabled": False,
    })


def test_the_organization_takes_every_media_row_with_it(migrated: str) -> None:
    _world(migrated)
    _asset(migrated, "a1")
    _object(migrated, "ob1")
    _insert(migrated, "media_provenance", {
        "id": "p1", "organization_id": "o1", "workspace_id": "w1", "object_id": "ob1",
        "origin": "customer_upload", "inputs_hash": SHA, "processor": "s/1", "scanner": "t",
    })
    _insert(migrated, "media_rights", {
        "id": "r1", "organization_id": "o1", "workspace_id": "w1", "asset_id": "a1",
        "basis": "customer_attestation", "attestation_text_version": "v1",
        "attested_by": "u1", "attested_at": datetime.now(UTC),
    })
    engine = create_engine(migrated)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM users WHERE id = 'u1'"))
            rights = conn.execute(
                sa.text("SELECT attested_by, attestation_text_version FROM media_rights")
            ).all()
            assert [tuple(r) for r in rights] == [(None, "v1")]
            conn.execute(sa.text("DELETE FROM organizations WHERE id = 'o1'"))
    finally:
        engine.dispose()
    for table in MEDIA_TABLES:
        assert _count(migrated, table) == 0, table


def test_downgrade_refuses_before_ddl_then_steps_down_and_up(migrated: str) -> None:
    _world(migrated)
    _asset(migrated, "a1")
    token = _confirmation(migrated, "-1")
    refused = _run(["-x", f"confirm={token}", "downgrade", "-1"], migrated)
    assert refused.returncode != 0
    assert "1 media_assets row(s) exist" in refused.stdout + refused.stderr
    assert _version(migrated) == [CF7A0]
    assert MEDIA_TABLES <= _tables(migrated)

    engine = create_engine(migrated)
    try:
        with engine.begin() as conn:
            conn.execute(sa.text("DELETE FROM media_assets"))
    finally:
        engine.dispose()
    token = _confirmation(migrated, "-1")
    _alembic(migrated, "-x", f"confirm={token}", "downgrade", "-1")
    assert _version(migrated) == [PREV]
    assert not MEDIA_TABLES & _tables(migrated)
    narrowed = _literals(
        _checks(migrated, "workspace_capability_overrides")[
            "ck_workspace_capability_override_capability"
        ],
        "capability",
    )
    assert narrowed == set(persisted_values()) - {"media_assets"}

    _alembic(migrated, "upgrade", "head")
    assert _version(migrated) == [CF7A0]
    result = _run(["check"], migrated)
    assert result.returncode == 0, result.stdout + result.stderr
