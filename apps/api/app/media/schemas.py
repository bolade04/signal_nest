"""Request/response shapes of M0-A's media routes (Phase 7A; frozen at CF-7A-0).

**Schemas only.** CF-7A-0 mounts no route, so none of these models is in ``openapi.json``. M0-A
builds the services and routes on them, unmounted; M0-B mounts them behind
``media_assets_enabled`` (media plan v3 M0). The routes they belong to, with the refusals and
the acceptance examples they serve, are in ``docs/project-phase-7a-cf7a0-contracts.md`` §9:

* ``POST /workspaces/{workspace_id}/media/uploads``: :class:`MediaUploadCreate` →
  :class:`MediaUploadSessionOut` (an asset in ``pending``, an upload record in
  ``upload_pending``, and a presigned PUT or a multipart session);
* ``POST /workspaces/{workspace_id}/media/uploads/{object_id}/parts``:
  :class:`MediaUploadPartsRequest` → :class:`MediaUploadPartsOut` (multipart part presigns);
* ``POST /workspaces/{workspace_id}/media/uploads/{object_id}/complete``:
  :class:`MediaUploadComplete` → :class:`MediaAssetOut` (checked, sanitised and promoted, or
  refused and never promoted);
* ``GET /workspaces/{workspace_id}/media/assets`` → :class:`MediaAssetListOut`, and
  ``GET /workspaces/{workspace_id}/media/assets/{asset_id}`` → :class:`MediaAssetOut`;
* ``POST /workspaces/{workspace_id}/media/assets/{asset_id}/signed-url``:
  :class:`MediaSignedUrlRequest` → :class:`MediaSignedUrlOut` (one audited issuance);
* ``GET /workspaces/{workspace_id}/media-assets-capability`` → :class:`MediaAssetsCapabilityOut`.

Boundary decisions encoded here (the Phase 5 schema precedent):

* **Strict requests** (``extra="forbid"``). Tenant scope, ids, object keys, state, the
  uploading account and every limit are server-side; a caller never supplies a key.
* **The declared facts are bound**: kind, content type (one of the kind's, so never SVG),
  size and sha256. M0-A refuses an upload whose stored object differs from them (AE-M0-10).
* **The MD-4 attestation is required** with every upload: its text version and an explicit
  ``accepted: true``. It becomes the asset's ``media_rights`` record.
* **Safe responses.** No object key, version id, bucket or storage detail is returned; a
  presigned URL is returned only to the caller that asked for it.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator

from app.core.enums import (
    MEDIA_CONTENT_TYPES_BY_KIND,
    MediaAssetKind,
    MediaAssetState,
    MediaContentType,
)

#: Bounds frozen at CF-7A-0. None of them is an upload limit: those are settings, unset.
MEDIA_SCHEMA_BOUNDS = {
    "display_name": 200,
    "attestation_text_version": 32,
    "etag": 128,
    #: S3's ceiling on parts in one multipart upload.
    "parts": 10000,
}

_Id = Annotated[str, Field(min_length=1, max_length=32)]
_Sha256 = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
_PartNumber = Annotated[StrictInt, Field(ge=1, le=MEDIA_SCHEMA_BOUNDS["parts"])]


def _no_control_characters(value: str) -> str:
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("must not contain control characters")
    return value


class MediaAttestationIn(BaseModel):
    """The uploading customer's MD-4 attestation (rights and releases), per asset."""

    model_config = ConfigDict(extra="forbid")

    #: The version of the attestation wording the customer was shown.
    text_version: str = Field(
        min_length=1,
        max_length=MEDIA_SCHEMA_BOUNDS["attestation_text_version"],
        pattern=r"^[A-Za-z0-9._-]+$",
    )
    #: Must be ``true``: an upload without an accepted attestation is refused (422).
    accepted: Literal[True]


class MediaUploadCreate(BaseModel):
    """Strict request body that opens one upload session for one new asset."""

    model_config = ConfigDict(extra="forbid")

    kind: MediaAssetKind
    content_type: MediaContentType
    #: Declared size in bytes. Above ``media_upload_max_bytes`` it is refused before any
    #: presign is issued; with that setting unset every upload is refused (AE-M0-1).
    size_bytes: StrictInt = Field(gt=0)
    #: Declared lower-case hex sha256 of the bytes to be uploaded.
    sha256: _Sha256
    display_name: str = Field(min_length=1, max_length=MEDIA_SCHEMA_BOUNDS["display_name"])
    attestation: MediaAttestationIn

    @model_validator(mode="after")
    def _type_matches_kind(self) -> MediaUploadCreate:
        _no_control_characters(self.display_name)
        if self.content_type not in MEDIA_CONTENT_TYPES_BY_KIND[self.kind]:
            raise ValueError("content_type is not a type of this kind")
        return self


class MediaUploadPartOut(BaseModel):
    """One presigned multipart part: bound to the upload's exact key (AE-M0-10)."""

    part_number: int
    url: str
    #: Headers the client must send with this part.
    headers: dict[str, str]


class MediaUploadSessionOut(BaseModel):
    """An open upload session."""

    asset_id: str
    #: The upload record (``media_objects``, state ``upload_pending``).
    object_id: str
    #: ``single``: one presigned PUT (bound to the exact key, the declared content type and
    #: the declared length). ``multipart``: parts presigned on request.
    mode: Literal["single", "multipart"]
    #: The presigned PUT for ``single``; None for ``multipart``.
    url: str | None
    #: Headers the client must send with the PUT (``single``); empty for ``multipart``.
    headers: dict[str, str]
    #: For ``multipart``: the size of every part but the last, and the number of parts.
    part_size_bytes: int | None
    part_count: int | None
    expires_at: datetime


class MediaUploadPartsRequest(BaseModel):
    """Which parts of a multipart upload to presign."""

    model_config = ConfigDict(extra="forbid")

    part_numbers: list[_PartNumber] = Field(
        min_length=1, max_length=MEDIA_SCHEMA_BOUNDS["parts"]
    )

    @model_validator(mode="after")
    def _unique(self) -> MediaUploadPartsRequest:
        if len(set(self.part_numbers)) != len(self.part_numbers):
            raise ValueError("part_numbers must not repeat a part")
        return self


class MediaUploadPartsOut(BaseModel):
    parts: list[MediaUploadPartOut]
    expires_at: datetime


class MediaUploadedPart(BaseModel):
    model_config = ConfigDict(extra="forbid")

    part_number: _PartNumber
    etag: str = Field(min_length=1, max_length=MEDIA_SCHEMA_BOUNDS["etag"])


class MediaUploadComplete(BaseModel):
    """Completes an upload. ``parts`` is required for a multipart upload and absent for a
    single PUT (the service refuses either mismatch)."""

    model_config = ConfigDict(extra="forbid")

    parts: list[MediaUploadedPart] | None = Field(
        default=None, min_length=1, max_length=MEDIA_SCHEMA_BOUNDS["parts"]
    )


class MediaAssetOut(BaseModel):
    """One asset of the workspace's library. The stored object's facts are None until an
    object is promoted (state ``pending``)."""

    id: str
    kind: MediaAssetKind
    state: MediaAssetState
    display_name: str
    content_type: MediaContentType | None
    size_bytes: int | None
    sha256: str | None
    created_at: datetime
    updated_at: datetime


class MediaAssetListOut(BaseModel):
    """One page of the library, newest first; ``next_cursor`` is opaque."""

    items: list[MediaAssetOut]
    next_cursor: str | None


class MediaSignedUrlRequest(BaseModel):
    """A signed GET for an asset's stored object. ``ttl_seconds`` above the ceiling
    (``Settings.media_signed_url_ceiling_seconds``) is issued at the ceiling (AE-M0-8)."""

    model_config = ConfigDict(extra="forbid")

    ttl_seconds: StrictInt | None = Field(default=None, gt=0)


class MediaSignedUrlOut(BaseModel):
    url: str
    #: The lifetime as issued (and as recorded in the issuance's audit row).
    ttl_seconds: int
    expires_at: datetime


class MediaAssetsCapabilityOut(BaseModel):
    """Customer-visible media availability for one workspace.

    Exactly one field (the ``RecommendationBriefCapabilityOut`` precedent): no governance
    metadata reaches a customer."""

    enabled: bool


__all__ = [
    "MEDIA_SCHEMA_BOUNDS",
    "MediaAssetListOut",
    "MediaAssetOut",
    "MediaAssetsCapabilityOut",
    "MediaAttestationIn",
    "MediaSignedUrlOut",
    "MediaSignedUrlRequest",
    "MediaUploadComplete",
    "MediaUploadCreate",
    "MediaUploadPartOut",
    "MediaUploadPartsOut",
    "MediaUploadPartsRequest",
    "MediaUploadSessionOut",
    "MediaUploadedPart",
]
