"""Phase 7A CF-7A-0: the contracts other than the migration.

* **Nothing activates.** ``media_assets_enabled`` defaults ``False`` and is reflected, required,
  in ``FeatureFlagsOut``; the capability is bound to it; **no route serves a media surface**
  (M0-B mounts M0-A's routes later) and no media schema reaches ``openapi.json``.
* **The vocabularies** are exactly the proposed members, and the kind/content-type map is a
  partition with no SVG.
* **The settings** default to unset, refuse a value that is not usable when set (booleans,
  zero, negatives, non-finite numbers, unknown or repeated types), name the unset ones, and
  clamp the signed-URL lifetime to the S3 ceiling; the secret inventory names every new one.
* **The models** carry the tenant scope keys (CASCADE), the account keys set null, no account
  snapshot, and keep provenance and rights apart (MD-4).
* **The API shapes** (schemas only) are strict: a declared type must be one of its kind's, the
  MD-4 attestation must be accepted, and no storage detail is part of a response.
"""

from __future__ import annotations

import ast
import math
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.capabilities.registry import Capability, get_policy, persisted_values
from app.core.config import MEDIA_UPLOAD_SETTINGS, Settings
from app.core.enums import (
    MEDIA_CONTENT_TYPES_BY_KIND,
    MediaAssetKind,
    MediaAssetState,
    MediaContentType,
    MediaObjectState,
    MediaProvenanceOrigin,
    MediaRightsBasis,
)
from app.main import app
from app.media import schemas
from app.media.models import (
    MEDIA_ASSET_AREA,
    MEDIA_KEY_ROOT,
    MEDIA_UPLOAD_AREA,
    MediaAsset,
    MediaObject,
    MediaProvenanceRecord,
    MediaRightsRecord,
)

API_DIR = Path(__file__).resolve().parents[2]
REPO_ROOT = API_DIR.parents[1]
SECRET_INVENTORY = REPO_ROOT / "docs" / "operations" / "aws-staging-secret-inventory.md"
MODELS = (MediaAsset, MediaObject, MediaProvenanceRecord, MediaRightsRecord)
#: Every setting CF-7A-0 adds (the flag, the upload settings and the two durations).
NEW_SETTINGS = (
    "media_assets_enabled",
    *MEDIA_UPLOAD_SETTINGS,
    "retention_media_uploads_days",
    "retention_media_records_days",
)


# --------------------------------------------------------------------------- #
# Nothing activates
# --------------------------------------------------------------------------- #
def test_the_flag_ships_dark_and_is_reflected() -> None:
    """The CF-7A-0 flag-default mutation-check target (AE-M0-6)."""
    assert Settings(_env_file=None).media_assets_enabled is False
    assert Settings.model_fields["media_assets_enabled"].default is False
    assert app.openapi()["components"]["schemas"]["FeatureFlagsOut"]["required"].count(
        "media_assets_enabled"
    ) == 1


def test_the_capability_is_bound_to_the_flag_and_persisted() -> None:
    policy = get_policy(Capability.MEDIA_ASSETS)
    assert Capability.MEDIA_ASSETS.value == "media_assets"
    assert policy.label == "Media Assets"
    assert policy.global_flag_attr == "media_assets_enabled"
    assert policy.workspace_enableable and policy.workspace_disableable
    assert policy.subject_to_safety_ceiling and policy.requires_workspace_context
    assert policy.future_activation_phase == "7A"
    assert "media_assets" in persisted_values()


def test_no_route_serves_a_media_surface() -> None:
    """CF-7A-0 mounts nothing: no path names media, and no media schema is published."""
    spec = app.openapi()
    for path in spec["paths"]:
        segments = path.lower().split("/")
        assert not any(seg == "media" or seg.startswith("media-") for seg in segments), path
    published = set(spec["components"]["schemas"])
    assert not published & set(schemas.__all__)


def test_the_router_mounts_no_media_module() -> None:
    """The mounting point (``app/api/router.py``) imports nothing from ``app.media``: M0-A may
    build its routes unmounted on these schemas; M0-B mounts them here, behind the flag."""
    tree = ast.parse((API_DIR / "app" / "api" / "router.py").read_text(encoding="utf-8"))
    imported = [
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    ] + [a.name for node in ast.walk(tree) if isinstance(node, ast.Import) for a in node.names]
    assert imported, "the router imports its route modules"
    assert not [m for m in imported if m == "app.media" or m.startswith("app.media.")]


# --------------------------------------------------------------------------- #
# Vocabularies
# --------------------------------------------------------------------------- #
def test_the_vocabularies_are_exact() -> None:
    assert [k.value for k in MediaAssetKind] == ["raster_image", "font"]
    assert [t.value for t in MediaContentType] == [
        "image/png", "image/jpeg", "image/webp", "font/ttf", "font/otf", "font/woff"
    ]
    assert [s.value for s in MediaAssetState] == ["pending", "active", "withdrawn"]
    assert [s.value for s in MediaObjectState] == [
        "upload_pending", "promoted", "rejected", "aborted", "stored"
    ]
    assert [o.value for o in MediaProvenanceOrigin] == ["customer_upload"]
    assert [b.value for b in MediaRightsBasis] == ["customer_attestation"]


def test_the_content_types_are_partitioned_by_kind_and_exclude_svg() -> None:
    by_kind = MEDIA_CONTENT_TYPES_BY_KIND.items()
    assert {k.value: sorted(t.value for t in v) for k, v in by_kind} == {
        "raster_image": ["image/jpeg", "image/png", "image/webp"],
        "font": ["font/otf", "font/ttf", "font/woff"],
    }
    with pytest.raises(TypeError):
        MEDIA_CONTENT_TYPES_BY_KIND[MediaAssetKind.FONT] = frozenset()  # read-only
    for value in (*MediaContentType, *MediaAssetKind):
        assert "svg" not in value.value


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
def test_every_new_setting_is_declared_and_unset_or_off_by_default() -> None:
    settings = Settings(_env_file=None)
    assert settings.media_assets_enabled is False
    for name in NEW_SETTINGS[1:]:
        assert Settings.model_fields[name].default is None, name
        assert getattr(settings, name) is None, name
    assert settings.media_upload_settings_unset() == MEDIA_UPLOAD_SETTINGS
    assert settings.media_signed_url_ceiling_seconds() is None


def test_the_secret_inventory_classifies_every_new_setting() -> None:
    text = SECRET_INVENTORY.read_text(encoding="utf-8")
    for name in NEW_SETTINGS:
        assert f"`{name}`" in text, name
        assert f"`{name.upper()}`" in text, name


_VALID = {
    "media_upload_max_bytes": 1,
    "media_allowed_types": ["image/png"],
    "media_signed_url_ttl_seconds": 60,
    "media_image_max_pixels": 1,
    "media_image_decode_timeout_seconds": 0.5,
    "media_font_parse_timeout_seconds": 0.5,
    "media_decoder_max_memory_bytes": 1,
}


def test_every_upload_setting_is_named_until_it_is_set() -> None:
    assert set(_VALID) == set(MEDIA_UPLOAD_SETTINGS)
    configured = Settings(_env_file=None, **_VALID)
    assert configured.media_upload_settings_unset() == ()
    for name in MEDIA_UPLOAD_SETTINGS:
        partial = {k: v for k, v in _VALID.items() if k != name}
        assert Settings(_env_file=None, **partial).media_upload_settings_unset() == (name,)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        *[(n, bad) for n in ("media_upload_max_bytes", "media_signed_url_ttl_seconds",
                             "media_image_max_pixels", "media_decoder_max_memory_bytes")
          for bad in (0, -1, True)],
        *[(n, bad) for n in ("media_image_decode_timeout_seconds",
                             "media_font_parse_timeout_seconds")
          for bad in (0, -0.5, math.nan, math.inf, True)],
        ("media_allowed_types", []),
        ("media_allowed_types", ["image/png", "image/png"]),
        ("media_allowed_types", ["image/svg+xml"]),
        ("media_allowed_types", ["font/woff2"]),
        ("media_upload_max_bytes", 1.5),
    ],
)
def test_an_unusable_media_setting_is_refused(name: str, value) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{name: value})


def test_allowed_types_may_narrow_release_one() -> None:
    every = [t.value for t in MediaContentType]
    assert Settings(_env_file=None, media_allowed_types=every).media_allowed_types == every
    assert Settings(_env_file=None, media_allowed_types=["font/ttf"]).media_allowed_types == [
        "font/ttf"
    ]


@pytest.mark.parametrize(
    ("media_ttl", "s3_ttl", "ceiling"),
    [(60, 900, 60), (900, 900, 900), (5000, 900, 900), (5000, 300, 300)],
)
def test_the_signed_url_ceiling_is_clamped_to_the_s3_ceiling(media_ttl, s3_ttl, ceiling) -> None:
    settings = Settings(
        _env_file=None, media_signed_url_ttl_seconds=media_ttl, s3_signed_url_ttl_seconds=s3_ttl
    )
    assert settings.media_signed_url_ceiling_seconds() == ceiling


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
def _fk_rules(model) -> dict[tuple[str, ...], tuple[str, str]]:
    out = {}
    for constraint in model.__table__.foreign_key_constraints:
        cols = tuple(c.name for c in constraint.columns)
        target = constraint.elements[0].column.table.name
        out[cols] = (target, constraint.ondelete)
    return out


def test_every_table_is_tenant_scoped_with_cascading_keys() -> None:
    for model in MODELS:
        rules = _fk_rules(model)
        assert rules[("organization_id",)] == ("organizations", "CASCADE"), model
        assert rules[("workspace_id",)] == ("workspaces", "CASCADE"), model
        for column in ("organization_id", "workspace_id"):
            assert model.__table__.c[column].nullable is False
            assert model.__table__.c[column].index is True


def test_the_workspace_consistency_keys_and_the_account_keys() -> None:
    assert _fk_rules(MediaObject)[("asset_id", "workspace_id", "kind")] == (
        "media_assets", "CASCADE"
    )
    assert _fk_rules(MediaProvenanceRecord)[("object_id", "workspace_id")] == (
        "media_objects", "CASCADE"
    )
    assert _fk_rules(MediaRightsRecord)[("asset_id", "workspace_id")] == (
        "media_assets", "CASCADE"
    )
    for model, column, target in (
        (MediaAsset, "created_by", "users"),
        (MediaRightsRecord, "attested_by", "users"),
        (MediaProvenanceRecord, "parent_object_id", "media_objects"),
        (MediaProvenanceRecord, "job_id", "jobs"),
    ):
        assert _fk_rules(model)[(column,)] == (target, "SET NULL"), (model, column)
        assert model.__table__.c[column].nullable is True


def test_no_media_table_stores_an_account_snapshot() -> None:
    for model in MODELS:
        names = set(model.__table__.c.keys())
        assert not {n for n in names if "snapshot" in n or "email" in n or "name" == n}, model


def test_provenance_and_rights_records_stay_distinct() -> None:
    shared = {"id", "organization_id", "workspace_id", "created_at"}
    provenance = set(MediaProvenanceRecord.__table__.c.keys()) - shared
    rights = set(MediaRightsRecord.__table__.c.keys()) - shared
    assert provenance == {
        "object_id", "origin", "inputs_hash", "parent_object_id", "processor", "scanner",
        "job_id",
    }
    assert rights == {
        "asset_id", "basis", "attestation_text_version", "attested_by", "attested_at"
    }


def test_the_key_layout_constants() -> None:
    assert (MEDIA_KEY_ROOT, MEDIA_UPLOAD_AREA, MEDIA_ASSET_AREA) == ("media", "uploads", "assets")


def test_the_models_module_depends_only_on_the_contract_layer() -> None:
    tree = ast.parse((API_DIR / "app" / "media" / "models.py").read_text(encoding="utf-8"))
    imported = {
        node.module for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith("app.")
    }
    assert imported == {"app.core.enums", "app.db.base"}


# --------------------------------------------------------------------------- #
# API shapes (schemas only)
# --------------------------------------------------------------------------- #
_UPLOAD = {
    "kind": "raster_image",
    "content_type": "image/png",
    "size_bytes": 1024,
    "sha256": "c" * 64,
    "display_name": "Logo",
    "attestation": {"text_version": "media-attest-1", "accepted": True},
}


def test_a_valid_upload_request_is_accepted() -> None:
    body = schemas.MediaUploadCreate.model_validate(_UPLOAD)
    assert body.kind is MediaAssetKind.RASTER_IMAGE
    font = {**_UPLOAD, "kind": "font", "content_type": "font/woff"}
    assert schemas.MediaUploadCreate.model_validate(font).content_type is MediaContentType.FONT_WOFF


@pytest.mark.parametrize(
    "change",
    [
        {"content_type": "image/svg+xml"},
        {"kind": "svg"},
        {"content_type": "font/ttf"},  # a font type for a raster image
        {"kind": "font"},  # an image type for a font
        {"size_bytes": 0},
        {"size_bytes": True},
        {"size_bytes": "1024"},
        {"sha256": "C" * 64},
        {"sha256": "c" * 63},
        {"display_name": ""},
        {"display_name": "x" * 201},
        {"display_name": "line\nbreak"},
        {"attestation": {"text_version": "v1", "accepted": False}},
        {"attestation": {"text_version": "v 1", "accepted": True}},
        {"attestation": None},
        {"storage_key": "o/w/media/assets/a/b"},
        {"workspace_id": "w"},
    ],
)
def test_an_upload_request_is_refused(change: dict) -> None:
    with pytest.raises(ValidationError):
        schemas.MediaUploadCreate.model_validate({**_UPLOAD, **change})


def test_the_multipart_and_signed_url_requests_are_strict() -> None:
    assert schemas.MediaUploadPartsRequest(part_numbers=[1, 2]).part_numbers == [1, 2]
    for bad in ([], [0], [1, 1], [10001], [True]):
        with pytest.raises(ValidationError):
            schemas.MediaUploadPartsRequest(part_numbers=bad)
    assert schemas.MediaUploadComplete().parts is None
    with pytest.raises(ValidationError):
        schemas.MediaUploadComplete(parts=[])
    assert schemas.MediaSignedUrlRequest().ttl_seconds is None
    for bad in (0, -1, True, 1.5):
        with pytest.raises(ValidationError):
            schemas.MediaSignedUrlRequest(ttl_seconds=bad)


def test_no_response_shape_exposes_a_storage_detail() -> None:
    forbidden = {"storage_key", "version_id", "bucket", "endpoint", "key"}
    for name in schemas.__all__:
        model = getattr(schemas, name)
        if isinstance(model, type) and name.endswith("Out"):
            assert not forbidden & set(model.model_fields), name
    assert set(schemas.MediaAssetsCapabilityOut.model_fields) == {"enabled"}
    issued = schemas.MediaSignedUrlOut(
        url="https://example.invalid/x", ttl_seconds=60, expires_at=datetime.now(UTC)
    )
    assert issued.ttl_seconds == 60
