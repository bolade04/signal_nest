"""Phase 7A CF-7A-0: the contracts-freeze migration ``91a796f09399`` on SQLite.

Through the real Alembic CLI on a throwaway SQLite database (as CI validates migrations):

* ``91a796f09399`` follows ``53fdde95354a`` and is the single head; ``upgrade head`` then
  ``alembic check`` reports no drift between the media models and the migration (Phase 5 plan
  §6.2.0, one atomic payload);
* **``PP5-CHK-001``: every CHECK of the migrated database equals its Python authority, in both
  directions.** Alembic never compares CHECK constraints (Phase 5 plan §9.2), so this is the only
  gate on them. Each vocabulary CHECK is read from the migrated database (never ``create_all``)
  and compared, as a set, with its imported authority (``missing_from_db`` and
  ``unauthorized_in_db``); the kind/content-type CHECK is compared as a **partition** (kind →
  types), so a swapped or dropped limb fails even where the flat literals agree; the capability
  CHECK equals ``persisted_values()`` and admits ``media_assets``; the shape CHECKs equal the
  models' text. Discovery: every CHECK on the four media tables is one this module knows, and
  the models declare the same names. Negative controls A (a value removed) and B
  (``'__pp5_negative_control__'`` added) and the swapped-limb control each fail the comparison;
  the positive control requires a non-zero count of compared vocabularies;
* the CHECKs, the workspace-consistency keys and the cascades hold on insert and delete, with
  SQLite's foreign keys on (as the application's engine sets them);
* the downgrade **refuses before any DDL** while an override names ``media_assets`` or while any
  media table holds a row; otherwise one step down gives the schema of a database migrated
  straight to ``53fdde95354a``, and ``upgrade`` restores the head with no drift.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.capabilities.registry import persisted_values
from app.core.enums import (
    MEDIA_CONTENT_TYPES_BY_KIND,
    MediaAssetKind,
    MediaAssetState,
    MediaContentType,
    MediaObjectState,
    MediaProvenanceOrigin,
    MediaRightsBasis,
)
from app.media.models import (
    MediaAsset,
    MediaObject,
    MediaProvenanceRecord,
    MediaRightsRecord,
)

API_DIR = Path(__file__).resolve().parents[2]
#: This revision (Phase 7A CF-7A-0).
CF7A0 = "91a796f09399"
#: The revision below it: 5E-CF's capability-CHECK widening (``notifications_analytics``).
PREV = "53fdde95354a"
OVERRIDES = "workspace_capability_overrides"
MODELS = (MediaAsset, MediaObject, MediaProvenanceRecord, MediaRightsRecord)
MEDIA_TABLES = tuple(m.__tablename__ for m in MODELS)

#: Every CHECK the media tables carry at this revision.
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
#: The vocabulary CHECKs: (table, CHECK, column, authority).
VOCABULARIES = (
    ("media_assets", "ck_media_asset_kind", "kind", MediaAssetKind),
    ("media_assets", "ck_media_asset_state", "state", MediaAssetState),
    ("media_objects", "ck_media_object_state", "state", MediaObjectState),
    ("media_provenance", "ck_media_provenance_origin", "origin", MediaProvenanceOrigin),
    ("media_rights", "ck_media_rights_basis", "basis", MediaRightsBasis),
)
PARTITION = ("media_objects", "ck_media_object_kind_content_type")
#: The CHECKs that are not vocabularies: compared as exact text with the model's.
SHAPES = {
    ("media_assets", "ck_media_asset_display_name"),
    ("media_objects", "ck_media_object_size"),
    ("media_objects", "ck_media_object_sha256"),
    ("media_objects", "ck_media_object_storage_key"),
    ("media_provenance", "ck_media_provenance_inputs_hash"),
    ("media_rights", "ck_media_rights_attestation"),
}
NEGATIVE = "__pp5_negative_control__"


# --------------------------------------------------------------------------- #
# Alembic plumbing (as the sibling migration tests do)
# --------------------------------------------------------------------------- #
def _confirmation(db_path: Path, target: str) -> list[str]:
    from alembic.config import Config
    from alembic.runtime.migration import MigrationContext
    from alembic.script import ScriptDirectory

    from app.db.downgrade_guard import (
        chain_identity,
        confirmation_token,
        live_identity,
        resolve_downgrade,
    )

    url = f"sqlite:///{db_path}"
    engine = create_engine(url)
    try:
        with engine.connect() as conn:
            heads = MigrationContext.configure(conn).get_current_heads()
            identity = live_identity(conn, url)
    finally:
        engine.dispose()
    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    resolved, chain = resolve_downgrade(script, heads[0], target)
    token = confirmation_token(
        database_url=url,
        live_identity=identity,
        source_revision=heads[0],
        requested_target=target,
        resolved_destination=resolved,
        chain_identity=chain_identity(script, chain),
    )
    return ["-x", f"confirm={token}"]


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    confirm = _confirmation(db_path, args[1]) if args[0] == "downgrade" else []
    return subprocess.run(
        [sys.executable, "-m", "alembic", *confirm, *args],
        cwd=API_DIR,
        env=env,
        capture_output=True,
        text=True,
    )


def _sql(db_path: Path, statement: str, params: tuple = (), *, keys: bool = True) -> list[tuple]:
    """One statement on its own connection; foreign keys on unless ``keys=False``."""
    con = sqlite3.connect(db_path)
    try:
        con.execute(f"PRAGMA foreign_keys = {'ON' if keys else 'OFF'}")
        rows = con.execute(statement, params).fetchall()
        con.commit()
        return rows
    finally:
        con.close()


def _checks(db_path: Path, table: str) -> dict[str, str]:
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        return {c["name"]: c["sqltext"] for c in inspect(engine).get_check_constraints(table)}
    finally:
        engine.dispose()


def _norm(sqltext: str) -> str:
    return " ".join(str(sqltext).split())


def _in_list(sqltext: str, column: str) -> set[str]:
    match = re.fullmatch(rf"\s*{column} IN \(([^)]*)\)\s*", sqltext)
    assert match, sqltext
    return set(re.findall(r"'([^']*)'", match.group(1)))


def _partition(sqltext: str) -> dict[str, frozenset[str]]:
    """kind → content types, read from the CHECK text. Every limb is accounted for."""
    limbs = re.findall(r"\(kind = '([^']+)' AND content_type IN \(([^)]*)\)\)", sqltext)
    assert len(limbs) == _norm(sqltext).count(" OR ") + 1, sqltext
    partition = {k: frozenset(re.findall(r"'([^']*)'", types)) for k, types in limbs}
    assert len(partition) == len(limbs), "a kind appears in two limbs"
    return partition


def _authority_partition() -> dict[str, frozenset[str]]:
    return {
        kind.value: frozenset(t.value for t in types)
        for kind, types in MEDIA_CONTENT_TYPES_BY_KIND.items()
    }


def _compare(in_db: set[str], authority: set[str]) -> tuple[set[str], set[str]]:
    """``(missing_from_db, unauthorized_in_db)``; both must be empty."""
    return authority - in_db, in_db - authority


def _model_check(table: str, name: str) -> str:
    model = next(m for m in MODELS if m.__tablename__ == table)
    (check,) = [
        c for c in model.__table__.constraints
        if c.__class__.__name__ == "CheckConstraint" and c.name == name
    ]
    return str(check.sqltext)


@pytest.fixture()
def migrated(tmp_path) -> Path:
    db_path = tmp_path / "cf7a0.db"
    result = _alembic(db_path, "upgrade", "head")
    assert result.returncode == 0, result.stderr
    return db_path


# --------------------------------------------------------------------------- #
# Head and drift
# --------------------------------------------------------------------------- #
def test_the_head_is_single_and_follows_the_capability_widening() -> None:
    from alembic.config import Config
    from alembic.script import ScriptDirectory

    script = ScriptDirectory.from_config(Config(str(API_DIR / "alembic.ini")))
    assert [r.revision for r in script.get_revisions("heads")] == [CF7A0]
    assert script.get_revision(CF7A0).down_revision == PREV


def test_upgrade_creates_the_four_tables_and_leaves_no_model_drift(migrated: Path) -> None:
    assert set(MEDIA_TABLES) <= {
        r[0] for r in _sql(migrated, "SELECT name FROM sqlite_master WHERE type='table'")
    }
    result = _alembic(migrated, "check")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "No new upgrade operations detected" in result.stdout + result.stderr


# --------------------------------------------------------------------------- #
# PP5-CHK-001: the CHECKs against their Python authorities
# --------------------------------------------------------------------------- #
def test_every_check_is_known(migrated: Path) -> None:
    """Discovery: a CHECK the database carries but this module does not know fails here."""
    for table, names in EXPECTED_CHECKS.items():
        assert set(_checks(migrated, table)) == names, table
    for model in MODELS:
        declared = {
            c.name for c in model.__table__.constraints
            if c.__class__.__name__ == "CheckConstraint"
        }
        assert declared == EXPECTED_CHECKS[model.__tablename__]
    # Every known CHECK is compared below: as a vocabulary, the partition or a shape.
    compared = {(t, n) for t, n, _c, _a in VOCABULARIES} | {PARTITION} | SHAPES
    assert compared == {(t, n) for t, names in EXPECTED_CHECKS.items() for n in names}


def test_every_vocabulary_check_equals_its_authority(migrated: Path) -> None:
    compared = 0
    for table, name, column, authority in VOCABULARIES:
        in_db = _in_list(_checks(migrated, table)[name], column)
        assert _compare(in_db, {m.value for m in authority}) == (set(), set()), name
        compared += 1
    assert compared == len(VOCABULARIES) > 0  # the positive control: not an empty registry


def test_the_kind_content_type_check_equals_the_partition(migrated: Path) -> None:
    table, name = PARTITION
    in_db = _partition(_checks(migrated, table)[name])
    assert in_db == _authority_partition()
    # The authority itself partitions the content types: disjoint, complete, every kind.
    sets = list(MEDIA_CONTENT_TYPES_BY_KIND.values())
    assert set(MEDIA_CONTENT_TYPES_BY_KIND) == set(MediaAssetKind)
    assert sum(len(s) for s in sets) == len(MediaContentType)
    assert frozenset().union(*sets) == frozenset(MediaContentType)
    # SVG is excluded (CF-7A-0's proposal R-1): no kind and no content type admits it.
    assert not any("svg" in v for types in in_db.values() for v in types)
    assert not any("svg" in k for k in in_db)


def test_the_capability_check_equals_the_registry_and_admits_media_assets(
    migrated: Path,
) -> None:
    sqltext = _checks(migrated, OVERRIDES)["ck_workspace_capability_override_capability"]
    in_db = _in_list(sqltext, "capability")
    assert _compare(in_db, set(persisted_values())) == (set(), set())
    assert "media_assets" in in_db


def test_the_shape_checks_equal_the_models(migrated: Path) -> None:
    for table, name in sorted(SHAPES):
        assert _norm(_checks(migrated, table)[name]) == _norm(_model_check(table, name)), name


def test_every_check_of_the_models_equals_the_migrated_one(migrated: Path) -> None:
    """The model side of ``PP5-CHK-001`` (review W7A-REV-CF7A0-DATA r1, C1): every CHECK the models
    declare, the vocabularies and the partition as well as the shapes, has the migrated database's
    exact text. Fixtures built by ``create_all`` use the models' CHECKs, and ``alembic check``
    never compares CHECKs, so a model that drifted from the migration would otherwise go unseen.
    CF-3 closed the same gap (``test_cf3_contract_migration.py``,
    ``test_the_vocabulary_checks_of_the_models_equal_the_migrated_ones``)."""
    compared = 0
    for table, names in EXPECTED_CHECKS.items():
        in_db = _checks(migrated, table)
        for name in sorted(names):
            assert _norm(in_db[name]) == _norm(_model_check(table, name)), (table, name)
            compared += 1
    assert compared == sum(len(names) for names in EXPECTED_CHECKS.values()) == 12


def test_the_comparison_fails_both_negative_controls_and_a_swapped_limb() -> None:
    authority = {m.value for m in MediaObjectState}
    removed = authority - {MediaObjectState.ABORTED.value}
    assert _compare(removed, authority) == ({"aborted"}, set())  # control A
    added = authority | {NEGATIVE}
    assert _compare(added, authority) == (set(), {NEGATIVE})  # control B
    # A swapped limb keeps the flat set of literals and still fails the partition comparison.
    partition = _authority_partition()
    swapped = dict(partition)
    swapped["font"] = (partition["font"] - {"font/woff"}) | {"image/webp"}
    swapped["raster_image"] = (partition["raster_image"] - {"image/webp"}) | {"font/woff"}
    assert frozenset().union(*swapped.values()) == frozenset().union(*partition.values())
    assert swapped != partition


# --------------------------------------------------------------------------- #
# The CHECKs, keys and cascades hold (foreign keys on)
# --------------------------------------------------------------------------- #
ORG, WS, WS2, USER = "o1", "w1", "w2", "u1"
SHA = "a" * 64


def _world(db: Path) -> None:
    _sql(db, "INSERT INTO organizations (id, name, slug) VALUES (?, 'O', 'o')", (ORG,))
    for ws in (WS, WS2):
        _sql(
            db,
            "INSERT INTO workspaces (id, organization_id, name, slug, onboarding_completed)"
            " VALUES (?, ?, ?, ?, 1)",
            (ws, ORG, ws, ws),
        )
    _sql(
        db,
        "INSERT INTO users (id, email, full_name, hashed_password, is_active)"
        " VALUES (?, 'u1@example.test', 'U', 'x', 1)",
        (USER,),
    )


_ASSET = (
    "INSERT INTO media_assets (id, organization_id, workspace_id, kind, state, display_name,"
    " created_by) VALUES (?, ?, ?, ?, ?, ?, ?)"
)
_OBJECT = (
    "INSERT INTO media_objects (id, organization_id, workspace_id, asset_id, kind, content_type,"
    " storage_key, size_bytes, sha256, version_id, state) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,"
    " NULL, ?)"
)
_PROVENANCE = (
    "INSERT INTO media_provenance (id, organization_id, workspace_id, object_id, origin,"
    " inputs_hash, parent_object_id, processor, scanner, job_id)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, 'sanitiser/1', 'test-scanner', NULL)"
)
_RIGHTS = (
    "INSERT INTO media_rights (id, organization_id, workspace_id, asset_id, basis,"
    " attestation_text_version, attested_by, attested_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def _asset(db, row_id, *, ws=WS, kind="raster_image", state="active", name="logo.png",
           keys=True):
    _sql(db, _ASSET, (row_id, ORG, ws, kind, state, name, USER), keys=keys)


def _object(db, row_id, *, asset="a1", ws=WS, kind="raster_image", ctype="image/png",
            key=None, size=10, sha=SHA, state="stored", keys=True):
    key = key if key is not None else f"{ORG}/{ws}/media/assets/{asset}/{row_id}"
    _sql(db, _OBJECT, (row_id, ORG, ws, asset, kind, ctype, key, size, sha, state), keys=keys)


def _provenance(db, row_id, *, obj="ob1", ws=WS, origin="customer_upload", inputs=SHA,
                parent=None, keys=True):
    _sql(db, _PROVENANCE, (row_id, ORG, ws, obj, origin, inputs, parent), keys=keys)


def _rights(db, row_id, *, asset="a1", ws=WS, basis="customer_attestation", version="v1",
            by=USER, at="2026-11-22 14:00:00", keys=True):
    _sql(db, _RIGHTS, (row_id, ORG, ws, asset, basis, version, by, at), keys=keys)


def _refused(call, *args, **kwargs) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        call(*args, **kwargs)


def test_the_asset_checks_hold(migrated: Path) -> None:
    _world(migrated)
    _asset(migrated, "a1")
    _asset(migrated, "a2", kind="font", state="pending", name="Brand Sans")
    _refused(_asset, migrated, "bad-kind", kind="svg")
    _refused(_asset, migrated, "bad-kind2", kind="image")
    _refused(_asset, migrated, "bad-state", state="deleted")
    _refused(_asset, migrated, "bad-name", name="")
    _refused(_asset, migrated, "bad-ws", ws="no-such-workspace")


def test_the_object_checks_and_the_partition_hold(migrated: Path) -> None:
    _world(migrated)
    _asset(migrated, "a1")
    _asset(migrated, "f1", kind="font")
    _object(migrated, "ob1")
    _object(migrated, "ob2", ctype="image/jpeg")
    _object(migrated, "ob3", asset="f1", kind="font", ctype="font/woff")
    _object(
        migrated, "up1", state="upload_pending", key=f"{ORG}/{WS}/media/uploads/up1", size=5
    )
    for row_id, kwargs in (
        ("svg", {"ctype": "image/svg+xml"}),
        ("woff2", {"asset": "f1", "kind": "font", "ctype": "font/woff2"}),
        ("gif", {"ctype": "image/gif"}),
        ("font-type-under-image", {"ctype": "font/ttf"}),
        ("image-type-under-font", {"asset": "f1", "kind": "font", "ctype": "image/png"}),
        ("kind-not-the-assets", {"asset": "a1", "kind": "font", "ctype": "font/ttf"}),
        ("bad-state", {"state": "deleted"}),
        ("empty", {"size": 0}),
        ("negative", {"size": -1}),
        ("upper-sha", {"sha": "A" * 64}),
        ("short-sha", {"sha": "a" * 63}),
        ("other-org-key", {"key": f"o2/{WS}/media/assets/a1/x"}),
        ("other-ws-key", {"key": f"{ORG}/{WS2}/media/assets/a1/x"}),
        ("not-media-key", {"key": f"{ORG}/{WS}/exports/x"}),
        ("case-folded-key", {"key": f"{ORG}/{WS}/MEDIA/assets/a1/x"}),
        ("duplicate-key", {"key": f"{ORG}/{WS}/media/assets/a1/ob1"}),
        ("cross-workspace", {"ws": WS2}),
        ("no-such-asset", {"asset": "nope"}),
    ):
        _refused(_object, migrated, row_id, **kwargs)
    assert {r[0] for r in _sql(migrated, "SELECT id FROM media_objects")} == {
        "ob1", "ob2", "ob3", "up1"
    }


def test_the_provenance_and_rights_checks_hold(migrated: Path) -> None:
    _world(migrated)
    _asset(migrated, "a1")
    _object(migrated, "up1", state="promoted", key=f"{ORG}/{WS}/media/uploads/up1")
    _object(migrated, "ob1")
    _provenance(migrated, "p1", parent="up1")
    _refused(_provenance, migrated, "p-dup")  # one record per object
    _object(migrated, "ob2")
    _refused(_provenance, migrated, "p-origin", obj="ob2", origin="generated")
    _refused(_provenance, migrated, "p-hash", obj="ob2", inputs="X" * 64)
    _refused(_provenance, migrated, "p-cross", obj="ob2", ws=WS2)
    _rights(migrated, "r1")
    _refused(_rights, migrated, "r-basis", basis="licence")
    _refused(_rights, migrated, "r-no-version", version=None)
    _refused(_rights, migrated, "r-no-instant", at=None)
    _refused(_rights, migrated, "r-cross", ws=WS2)
    _rights(migrated, "r2", by=None)  # the attester may be gone (account deletion)


def test_the_capability_check_holds_on_insert(migrated: Path) -> None:
    _world(migrated)
    insert = (
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES (?, ?, ?, ?, 0)"
    )
    _sql(migrated, insert, ("ov-1", ORG, WS, "media_assets"))
    _refused(_sql, migrated, insert, ("ov-2", ORG, WS, "media"))


def test_the_keys_cascade_and_set_null(migrated: Path) -> None:
    _world(migrated)
    _asset(migrated, "a1")
    _object(migrated, "up1", state="promoted", key=f"{ORG}/{WS}/media/uploads/up1")
    _object(migrated, "ob1")
    _provenance(migrated, "p1", parent="up1")
    _rights(migrated, "r1")

    # The parent object goes: the provenance record stays, with a NULL parent.
    _sql(migrated, "DELETE FROM media_objects WHERE id = 'up1'")
    assert _sql(migrated, "SELECT id, parent_object_id FROM media_provenance") == [("p1", None)]
    # The account goes: the asset and the rights record stay, without the account.
    _sql(migrated, "DELETE FROM users WHERE id = ?", (USER,))
    assert _sql(migrated, "SELECT created_by FROM media_assets") == [(None,)]
    assert _sql(
        migrated, "SELECT basis, attestation_text_version, attested_by FROM media_rights"
    ) == [("customer_attestation", "v1", None)]
    # The asset goes: its objects, their provenance and its rights go with it.
    _sql(migrated, "DELETE FROM media_assets WHERE id = 'a1'")
    for table in ("media_objects", "media_provenance", "media_rights"):
        assert _sql(migrated, f"SELECT count(*) FROM {table}") == [(0,)], table


def test_the_organization_takes_every_media_row_with_it(migrated: Path) -> None:
    _world(migrated)
    _asset(migrated, "a1")
    _object(migrated, "ob1")
    _provenance(migrated, "p1")
    _rights(migrated, "r1")
    _sql(migrated, "DELETE FROM organizations WHERE id = ?", (ORG,))
    for table in MEDIA_TABLES:
        assert _sql(migrated, f"SELECT count(*) FROM {table}") == [(0,)], table


# --------------------------------------------------------------------------- #
# Downgrade and re-upgrade
# --------------------------------------------------------------------------- #
def _version(db_path: Path) -> list[str]:
    return [r[0] for r in _sql(db_path, "SELECT version_num FROM alembic_version")]


def _tables(db_path: Path) -> set[str]:
    return {r[0] for r in _sql(db_path, "SELECT name FROM sqlite_master WHERE type='table'")}


def _schema(db_path: Path) -> dict[str, tuple]:
    """Every table's columns, indexes, unique constraints, keys and CHECKs, as reflected."""
    engine = create_engine(f"sqlite:///{db_path}")
    try:
        inspector = inspect(engine)
        out = {}
        for table in sorted(inspector.get_table_names()):
            columns = inspector.get_columns(table)
            indexes = inspector.get_indexes(table)
            uniques = inspector.get_unique_constraints(table)
            keys = inspector.get_foreign_keys(table)
            checks = inspector.get_check_constraints(table)
            out[table] = (
                tuple((c["name"], str(c["type"]), c["nullable"], c["default"]) for c in columns),
                tuple(sorted(
                    (i["name"], tuple(i["column_names"]), bool(i["unique"])) for i in indexes
                )),
                tuple(sorted(str(sorted(u.items())) for u in uniques)),
                tuple(sorted(str(sorted(f.items())) for f in keys)),
                tuple(sorted((c["name"], _norm(c["sqltext"])) for c in checks)),
            )
        return out
    finally:
        engine.dispose()


def _refuses(db: Path, message: str) -> None:
    tables, checks = _tables(db), _checks(db, OVERRIDES)
    refused = _alembic(db, "downgrade", "-1")
    assert refused.returncode != 0
    assert message in refused.stdout + refused.stderr
    assert "Nothing was changed" in refused.stdout + refused.stderr
    # Nothing changed: the same revision, tables and capability CHECK.
    assert _version(db) == [CF7A0]
    assert _tables(db) == tables
    assert _checks(db, OVERRIDES) == checks


def test_downgrade_refuses_while_an_override_names_media_assets(migrated: Path) -> None:
    _world(migrated)
    _sql(
        migrated,
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES ('ov-media', ?, ?, 'media_assets', 0)",
        (ORG, WS),
    )
    _refuses(migrated, "name 'media_assets'; clear them")


@pytest.mark.parametrize("table", MEDIA_TABLES)
def test_downgrade_refuses_while_a_media_table_holds_a_row(migrated: Path, table: str) -> None:
    # One row in this table only (keys off, so it needs no parents; the CHECKs still hold).
    if table == "media_assets":
        _asset(migrated, "a1", keys=False)
    elif table == "media_objects":
        _object(migrated, "ob1", keys=False)
    elif table == "media_provenance":
        _provenance(migrated, "p1", keys=False)
    else:
        _rights(migrated, "r1", keys=False)
    _refuses(migrated, f"1 {table} row(s) exist")
    assert _sql(migrated, f"SELECT count(*) FROM {table}") == [(1,)]


def test_one_step_down_gives_the_previous_schema_and_up_restores_it(
    migrated: Path, tmp_path: Path
) -> None:
    straight = tmp_path / "straight-to-prev.db"
    assert _alembic(straight, "upgrade", PREV).returncode == 0
    expected = _schema(straight)
    with_head = _schema(migrated)

    _world(migrated)
    _sql(
        migrated,
        f"INSERT INTO {OVERRIDES} (id, organization_id, workspace_id, capability, enabled)"
        " VALUES ('ov-feedback', ?, ?, 'opportunity_feedback', 1)",
        (ORG, WS),
    )
    down = _alembic(migrated, "downgrade", "-1")
    assert down.returncode == 0, down.stdout + down.stderr
    assert _version(migrated) == [PREV]
    assert not set(MEDIA_TABLES) & _tables(migrated)
    assert _schema(migrated) == expected
    assert "media_assets" not in _in_list(
        _checks(migrated, OVERRIDES)["ck_workspace_capability_override_capability"],
        "capability",
    )
    assert _sql(migrated, f"SELECT id FROM {OVERRIDES}") == [("ov-feedback",)]  # data kept

    up = _alembic(migrated, "upgrade", "head")
    assert up.returncode == 0, up.stderr
    assert _version(migrated) == [CF7A0]
    assert _schema(migrated) == with_head
    assert _alembic(migrated, "check").returncode == 0
