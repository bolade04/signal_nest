"""Phase 7A CF-7A-0: the media privacy contract (AE-M0-5, and the rules AE-M0-4 builds on).

* **Inventory:** the four media tables have exactly the stated ``TableEntry`` (category, export,
  no time-based class until M0-B binds one), and the stated retention bindings name real
  classes; the object-storage entry exists with its layout, prefix, class and erasure duty.
* **Retention:** ``media_uploads`` and ``media_records`` are classes with unset durations;
  production refuses to start while either is unset, staging and the migration actor do not;
  the sweep refuses both (``policy_unset``) without opening a session.
* **Account deletion** (the attester rule, stated in the contract and holding by the keys):
  through the real route, the account's media asset and rights record are kept, with
  ``created_by`` and ``attested_by`` cleared and the rights content intact; the verifier then
  finds nothing naming the account.
* **Organization deletion** removes every media row of the organization and none of another's;
  the **organization export** lists the four tables, scoped to the organization.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import UTC, datetime

import pytest
from sqlalchemy import insert

from app.core.config import Settings
from app.media.models import MediaAsset, MediaObject, MediaProvenanceRecord, MediaRightsRecord
from app.privacy import retention
from app.privacy.inventory import (
    MEDIA_RETENTION_BINDINGS,
    OBJECT_STORES,
    TABLES,
    Category,
    RetentionClass,
)
from app.privacy.retention import _ROOTS
from app.privacy.verification import verify_organization_erased, verify_subject_erased
from app.tests._platform_support import production_vector_kwargs
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    EMAIL,
    NAME,
    ORG_A,
    ORG_B,
    PASSWORD,
    WORKSPACES,
    WS_A,
    WS_B,
    PrivacyEnv,
    privacy_world,
)

MEDIA_TABLES = ("media_assets", "media_objects", "media_provenance", "media_rights")
MEDIA_CLASSES = (RetentionClass.MEDIA_UPLOADS, RetentionClass.MEDIA_RECORDS)
SHA = "d" * 64


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "cf7a0-privacy.db") as e:
        yield e


# --------------------------------------------------------------------------- #
# Inventory
# --------------------------------------------------------------------------- #
def test_the_media_tables_have_their_stated_entries() -> None:
    expected = {
        "media_assets": Category.TENANT_CONTENT,
        "media_objects": Category.TENANT_CONTENT,
        "media_provenance": Category.TENANT_CONTENT,
        "media_rights": Category.COMPLIANCE,
    }
    for table, category in expected.items():
        entry = TABLES[table]
        assert (entry.category, entry.exported, entry.excluded_columns) == (category, True, ())
        # No time-based class until M0-B binds one with its purge roots (contract §5).
        assert entry.retention_class is None, table


def test_the_retention_bindings_are_stated_and_not_yet_wired() -> None:
    assert dict(MEDIA_RETENTION_BINDINGS) == {
        "media_assets": RetentionClass.MEDIA_UPLOADS,
        "media_objects": RetentionClass.MEDIA_UPLOADS,
        "media_provenance": RetentionClass.MEDIA_RECORDS,
        "media_rights": RetentionClass.MEDIA_RECORDS,
    }
    rooted = {model.__tablename__ for roots in _ROOTS.values() for model, _ in roots}
    assert not rooted & set(MEDIA_TABLES)
    assert not set(MEDIA_CLASSES) & set(_ROOTS)
    with pytest.raises(TypeError):
        MEDIA_RETENTION_BINDINGS["media_assets"] = RetentionClass.AUDIT_LOGS  # read-only


def test_the_object_storage_entry_exists() -> None:
    (entry,) = OBJECT_STORES.values()
    assert entry.name == "media"
    assert entry.category is Category.TENANT_CONTENT
    assert entry.key_layout == (
        "{organization_id}/{workspace_id}/media/uploads/{object_id} (upload records); "
        "{organization_id}/{workspace_id}/media/assets/{asset_id}/{object_id} (stored objects)"
    )
    assert entry.erasure_prefix == "{organization_id}/"
    assert entry.retention_class is RetentionClass.MEDIA_UPLOADS
    for duty in ("every object version", "delete marker", "multipart upload"):
        assert duty in entry.erasure


# --------------------------------------------------------------------------- #
# Retention
# --------------------------------------------------------------------------- #
def test_the_media_classes_have_unset_durations() -> None:
    assert [c.value for c in RetentionClass][-2:] == ["media_uploads", "media_records"]
    settings = Settings(_env_file=None)
    for data_class in MEDIA_CLASSES:
        assert data_class.setting_name in Settings.model_fields
        assert Settings.model_fields[data_class.setting_name].default is None
        assert getattr(settings, data_class.setting_name) is None


def _production(**overrides) -> dict:
    kwargs = dict(
        _env_file=None,
        environment="production",
        app_mode="full",
        database_url="postgresql+psycopg://u:p@db.internal:5432/signalnest",
        queue_backend="redis",
        cache_backend="redis",
        redis_url="redis://cache.internal:6379/0",
        storage_backend="s3",
        s3_bucket="example-bucket",
        **production_vector_kwargs(),
        rate_limit_backend="redis",
        secret_key="a-real-production-secret-value-0123456789",
        llm_provider="anthropic",
        llm_api_key="inert-test-value",
        mail_backend="ses",
        mail_from_address="no-reply@example.com",
        mail_ses_region="us-east-1",
        public_web_origin="https://app.example.com",
    )
    kwargs.update({c.setting_name: 30 for c in RetentionClass})
    kwargs.update(overrides)
    return kwargs


@pytest.mark.parametrize("data_class", MEDIA_CLASSES)
def test_production_refuses_to_start_while_a_media_duration_is_unset(data_class) -> None:
    assert Settings(**_production()).is_production
    with pytest.raises(ValueError) as exc:
        Settings(**_production(**{data_class.setting_name: None}))
    assert f"{data_class.setting_name} must be set explicitly in production" in str(exc.value)
    unset = {c.setting_name: None for c in MEDIA_CLASSES}
    assert Settings(**_production(environment="staging", **unset)).environment == "staging"
    assert Settings(**_production(migration_mode=True, **unset)).migration_mode


def test_the_sweep_refuses_both_media_classes_while_unset() -> None:
    def no_session():
        raise AssertionError("the sweep opened a session for an unset class")

    outcomes = retention.sweep(no_session, classes=MEDIA_CLASSES, settings=Settings(_env_file=None))
    assert [(o.data_class, o.status, o.deleted) for o in outcomes] == [
        ("media_uploads", "policy_unset", {}),
        ("media_records", "policy_unset", {}),
    ]


# --------------------------------------------------------------------------- #
# Deletion and export, through the real routes
# --------------------------------------------------------------------------- #
def _media_rows(org: str, ws: str, tag: str, account: str) -> dict[str, dict]:
    asset, upload, stored = f"cf7a0-{tag}-asset", f"cf7a0-{tag}-up", f"cf7a0-{tag}-obj"
    now = datetime.now(UTC)
    base = {"organization_id": org, "workspace_id": ws}
    return {
        "asset": {**base, "id": asset, "kind": "raster_image", "state": "active",
                  "display_name": f"{tag} logo", "created_by": account},
        "upload": {**base, "id": upload, "asset_id": asset, "kind": "raster_image",
                   "content_type": "image/png", "storage_key": f"{org}/{ws}/media/uploads/{upload}",
                   "size_bytes": 10, "sha256": SHA, "state": "promoted"},
        "stored": {**base, "id": stored, "asset_id": asset, "kind": "raster_image",
                   "content_type": "image/png",
                   "storage_key": f"{org}/{ws}/media/assets/{asset}/{stored}",
                   "size_bytes": 9, "sha256": SHA, "state": "stored"},
        "provenance": {**base, "id": f"cf7a0-{tag}-prov", "object_id": stored,
                       "origin": "customer_upload", "inputs_hash": SHA,
                       "parent_object_id": upload, "processor": "sanitiser/1",
                       "scanner": "test-scanner"},
        "rights": {**base, "id": f"cf7a0-{tag}-rights", "asset_id": asset,
                   "basis": "customer_attestation", "attestation_text_version": "attest-1",
                   "attested_by": account, "attested_at": now},
    }


def _add_media(env: PrivacyEnv) -> None:
    worlds = (_media_rows(ORG_A, WS_A, "a", CAROL), _media_rows(ORG_B, WS_B, "b", BOB))
    with env.engine.begin() as conn:
        for rows in worlds:
            conn.execute(insert(MediaAsset.__table__), [rows["asset"]])
            conn.execute(insert(MediaObject.__table__), [rows["upload"], rows["stored"]])
            conn.execute(insert(MediaProvenanceRecord.__table__), [rows["provenance"]])
            conn.execute(insert(MediaRightsRecord.__table__), [rows["rights"]])


def _by_id(env: PrivacyEnv, model) -> dict[str, dict]:
    return {row["id"]: row for row in env.rows(model)}


def test_account_deletion_keeps_the_rights_content_and_loses_the_identity(env) -> None:
    _add_media(env)
    with env.session() as s:
        before = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert {"media_assets", "media_rights"} <= {f.table for f in before.reference_findings}
    others = {m: _by_id(env, m) for m in (MediaAsset, MediaRightsRecord)}

    r = env.client.post(
        f"{API}/account/delete",
        headers=env.auth(CAROL),
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )

    assert r.status_code == 200, r.text
    nulled = r.json()["counts"]["nulled"]
    assert nulled["media_assets.created_by"] == 1
    assert nulled["media_rights.attested_by"] == 1
    rights = _by_id(env, MediaRightsRecord)["cf7a0-a-rights"]
    assert rights["attested_by"] is None
    assert (rights["basis"], rights["attestation_text_version"]) == (
        "customer_attestation", "attest-1"
    )
    assert rights["attested_at"] is not None
    assert _by_id(env, MediaAsset)["cf7a0-a-asset"]["created_by"] is None
    # Organization B's rows are untouched.
    assert _by_id(env, MediaRightsRecord)["cf7a0-b-rights"] == (
        others[MediaRightsRecord]["cf7a0-b-rights"]
    )
    assert _by_id(env, MediaAsset)["cf7a0-b-asset"] == others[MediaAsset]["cf7a0-b-asset"]
    with env.session() as s:
        after = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert after.verified, after.as_dict()


def test_organization_deletion_removes_every_media_row_of_that_organization(env) -> None:
    _add_media(env)
    r = env.client.post(
        f"{API}/organizations/{ORG_A}/delete",
        headers=env.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )
    assert r.status_code == 200, r.text
    for model in (MediaAsset, MediaObject, MediaProvenanceRecord, MediaRightsRecord):
        assert {row["organization_id"] for row in env.rows(model)} == {ORG_B}, model
    with env.session() as s:
        report = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A]
        )
    assert report.verified, report.as_dict()


def test_the_organization_export_lists_the_media_tables(env) -> None:
    _add_media(env)
    r = env.client.get(f"{API}/organizations/{ORG_A}/export", headers=env.auth(ALICE))
    assert r.status_code == 200, r.text
    tables = r.json()["tables"]
    for table in MEDIA_TABLES:
        rows = tables[table]["rows"]
        assert rows and {row["organization_id"] for row in rows} == {ORG_A}, table
    assert [row["id"] for row in tables["media_rights"]["rows"]] == ["cf7a0-a-rights"]
