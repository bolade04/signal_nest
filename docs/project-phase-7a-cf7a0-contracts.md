# Phase 7A CF-7A-0 — media asset contracts (M0)

- **What this is.** The contract freeze CF-7A-0 of the Phase 7A media programme: the data model,
  closed vocabularies, flag, settings, privacy inventory, API shapes and ownership that M0-A and
  M0-B are built against (media plan v3 §3.0 and §3 M0, "CF-7A-0 (contracts)").
- **Status: PROPOSED, for the R-CF7A-0 review (DATA and CONTROLS lines).** The plan requires that
  review "before any builder consumes it" (§3.0). Section 13 lists the decisions this document
  leaves to it. Nothing here is a ruling.
- **Dark.** The flag `media_assets_enabled` defaults to `False`; every new setting defaults to
  unset; no route is mounted; nothing writes the new tables. This document authorizes no
  activation, deployment, AWS operation, provider call or spending.
- **Where the contract lives.** The Phase 5 contract freezes (CF-1 #282, CF-2 #292, CF-3 #298)
  had no in-repository contract document: their contracts were the in-repository Phase 5 plans
  (`docs/project-phase-5*.md`) plus each lane's evidence record. The Phase 7A plan is not in the
  repository, so CF-7A-0's contract is this file.
- **Sources.** The adopted media plan v3 (evidence store
  `P6-W21-MEDIA-PLAN-01/PLAN-PHASE-7A-MEDIA-PRODUCTION-v3.md`), the operator's rulings MD-1 to
  MD-9 and D-1, and the Phase 5 plan's §6.2.0 (one atomic payload), §7.1 (review before
  consumption) and §9.2 (`PP5-CHK-001`).

## 1. Tables

Four tables, one Alembic revision `91a796f09399` on `53fdde95354a`, congruent with the models in
`apps/api/app/media/models.py` (`alembic check` is clean on SQLite and PostgreSQL). Each carries
the Phase 5 tenant scope columns `organization_id` and `workspace_id`, real foreign keys,
`ON DELETE CASCADE`, both indexed. Ids are 32-character hex strings (`UUIDPrimaryKeyMixin`).

### `media_assets` — one logical asset of one workspace

| Column | Type | Notes |
|---|---|---|
| `kind` | `String(32)` not null | closed: `MediaAssetKind` (`ck_media_asset_kind`) |
| `state` | `String(32)` not null | closed: `MediaAssetState` (`ck_media_asset_state`) |
| `display_name` | `String(200)` not null | customer text; `ck_media_asset_display_name`: at least one character |
| `created_by` | FK `users.id`, `SET NULL`, nullable, indexed | the uploading account |
| `created_at`, `updated_at` | timestamps | `TimestampMixin` |

Unique indexes `uq_media_assets_id_workspace (id, workspace_id)` and
`uq_media_assets_id_workspace_kind (id, workspace_id, kind)` are the targets of the
workspace-consistency keys below.

### `media_objects` — one stored object, or one upload record, of an asset

| Column | Type | Notes |
|---|---|---|
| `asset_id` | `String(32)` not null, indexed | with `workspace_id` and `kind`: FK `fk_media_objects_asset` onto `media_assets (id, workspace_id, kind)`, `CASCADE` |
| `kind` | `String(32)` not null | always the asset's kind (part of the key) |
| `content_type` | `String(64)` not null | `ck_media_object_kind_content_type`: the kind/content-type partition |
| `storage_key` | `String(255)` not null | unique (`uq_media_objects_storage_key`); `ck_media_object_storage_key`: under `{organization_id}/{workspace_id}/media/` |
| `size_bytes` | `BigInteger` not null | `ck_media_object_size`: `> 0`; declared size (upload record) or stored size |
| `sha256` | `String(64)` not null | `ck_media_object_sha256`: 64 characters, lower case; declared (upload record) or of the stored bytes |
| `version_id` | `String(255)` nullable | the storage version id (S3); NULL where the backend has none |
| `state` | `String(32)` not null | closed: `MediaObjectState` (`ck_media_object_state`) |
| `created_at`, `updated_at` | timestamps | |

Unique index `uq_media_objects_id_workspace (id, workspace_id)` is the target of the provenance key.

### `media_provenance` — how one stored object was made (MD-4)

| Column | Type | Notes |
|---|---|---|
| `object_id` | `String(32)` not null | with `workspace_id`: FK `fk_media_provenance_object` onto `media_objects (id, workspace_id)`, `CASCADE`; unique (`uq_media_provenance_object`: one record per object) |
| `origin` | `String(32)` not null | closed: `MediaProvenanceOrigin` (`ck_media_provenance_origin`) |
| `inputs_hash` | `String(64)` not null | `ck_media_provenance_inputs_hash`; for an upload, the sha256 of the bytes as uploaded |
| `parent_object_id` | FK `media_objects.id`, `SET NULL`, nullable, indexed | for an upload, its upload record |
| `processor` | `String(100)` not null | identifier and version of the code path that produced the bytes |
| `scanner` | `String(100)` not null | identifier of the scanner that cleared the bytes (section 12) |
| `job_id` | FK `jobs.id`, `SET NULL`, nullable, indexed | the durable job, if any |
| `created_at` | timestamp | `CreatedAtMixin`: never updated |

### `media_rights` — who asserts which right to use one asset (MD-4)

| Column | Type | Notes |
|---|---|---|
| `asset_id` | `String(32)` not null, indexed | with `workspace_id`: FK `fk_media_rights_asset` onto `media_assets (id, workspace_id)`, `CASCADE` |
| `basis` | `String(32)` not null | closed: `MediaRightsBasis` (`ck_media_rights_basis`) |
| `attestation_text_version` | `String(32)` nullable | the version of the attestation wording accepted |
| `attested_by` | FK `users.id`, `SET NULL`, nullable, indexed | the attesting account |
| `attested_at` | `DateTime(tz)` nullable | |
| `created_at` | timestamp | `CreatedAtMixin` |

`ck_media_rights_attestation`: a `customer_attestation` row names its text version and its
instant. `attested_by` is not required, because account deletion clears it (section 6).

**Distinct records (MD-4).** `media_provenance` has no rights column and `media_rights` no
provenance column. CF-7A-0's `test_cf7a0_contracts.py` pins both column sets now; M1 owns
AE-M1-3's schema test when it widens them.

**Workspace consistency.** Every reference between media rows is a composite key that includes
`workspace_id`, onto a unique index of its target that begins with `(id, workspace_id)` (for an
object, `(id, workspace_id, kind)`; the CF-3 `campaign_locations` pattern, operator decision O-8). A cross-workspace object, rights record or provenance record cannot be
stored. The one exception is `media_provenance.parent_object_id`: a composite `SET NULL` key would
also clear the row's `workspace_id`, so it is a plain key, and the writer (M0-A) must keep it
inside the workspace.

**Not append-only.** CF-1's append-only guard (`app/db/append_only.py`) and the PostgreSQL
backstop triggers pin their table sets in existing tests; adding `media_provenance` and
`media_rights` to them is not an exact-value test update, so CF-7A-0 does not (decision R-9).

## 2. Closed vocabularies and `PP5-CHK-001`

The authorities are in `apps/api/app/core/enums.py` (bare vocabularies, Phase 5 plan §6.2.1):

| Authority | Members | CHECK |
|---|---|---|
| `MediaAssetKind` | `raster_image`, `font` | `ck_media_asset_kind`; the keys of the partition |
| `MediaContentType` | `image/png`, `image/jpeg`, `image/webp`, `font/ttf`, `font/otf`, `font/woff` | the values of the partition |
| `MEDIA_CONTENT_TYPES_BY_KIND` | `raster_image` → the three image types; `font` → the three font types | `ck_media_object_kind_content_type` (polarity form) |
| `MediaAssetState` | `pending`, `active`, `withdrawn` | `ck_media_asset_state` |
| `MediaObjectState` | `upload_pending`, `promoted`, `rejected`, `aborted`, `stored` | `ck_media_object_state` |
| `MediaProvenanceOrigin` | `customer_upload` | `ck_media_provenance_origin` |
| `MediaRightsBasis` | `customer_attestation` | `ck_media_rights_basis` |
| `Capability` (`persisted_values()`) | + `media_assets` | `ck_workspace_capability_override_capability`, widened |

- **SVG is excluded** (CF-7A-0's proposal; the plan leaves "excluded or rasterised on upload"
  to this review): no kind and no content type admits it, so the database itself refuses an SVG
  object. WOFF 2, font collections, GIF, HEIF/AVIF and every other type are refused the same way.
- **Object lifecycle.** An upload record starts `upload_pending` under the upload area and ends
  `promoted` (checked, sanitised and promoted; its upload-area object deleted with every version),
  `rejected` (refused at completion; never promoted; its upload-area object deleted with every
  version) or `aborted` (abandoned or expired; any multipart upload aborted). The promoted bytes
  are a separate `stored` object under the asset area. An asset is `pending` until its object is
  promoted, then `active`; `withdrawn` takes it out of use (M1's takedown, AE-M1-5).
- **`PP5-CHK-001`.** Alembic never compares CHECK constraints (Phase 5 plan §9.2), so
  `apps/api/app/tests/test_cf7a0_contract_migration.py` compares every CHECK of the migrated
  database (built by `alembic upgrade head`, never `create_all`) with its imported Python
  authority, exactly and in both directions (`missing_from_db`, `unauthorized_in_db`). The
  kind/content-type CHECK is compared as a **partition** (kind → types), so a swapped or dropped
  limb fails even where the flat set of literals is unchanged. Discovery: every CHECK on the four
  tables must be one the module knows, and the models declare the same names. Negative controls:
  one value removed, and `'__pp5_negative_control__'` added, each fails the comparison; the
  positive control requires a non-zero count of compared vocabularies.
  `test_cf7a0_postgres.py` repeats the comparison on PostgreSQL, from `pg_get_constraintdef`.
- **Widening later.** CF-7A-1 and CF-7A-2 widen these sets (generation origins, a licence basis,
  rendition content types) only with their own widening migration under the same control.

## 3. The flag and the capability

- `Settings.media_assets_enabled: bool = False` (`core/config.py`, the "Phase 7A: media assets
  (CF-7A-0)" block).
- `Capability.MEDIA_ASSETS` (`media_assets`), label "Media Assets", bound to that flag;
  `workspace_enableable=True`, `workspace_disableable=True`, subject to the safety ceiling,
  requires workspace context, `future_activation_phase="7A"`. This is O-3's pattern for the
  Phase 5 flags, which plan §3.0 extends to the media flags ("defaults to False, fails closed, is
  capability-resolved"); whether a workspace override may enable it is decision R-10.
- `FeatureFlagsOut.media_assets_enabled` (required), the raw-flag reflection on
  `GET /system/capabilities`, with the exact-equality test updated in the same change (AE-M0-6).
- M0-A's gates must decide through `resolve_capability`, never the raw flag
  (`test_capability_override_types.py` guards the raw-flag readers).

## 4. Settings (all unset)

| Setting | Type | When set | Serves |
|---|---|---|---|
| `media_upload_max_bytes` | int | > 0 | AE-M0-1, AE-M0-10 |
| `media_allowed_types` | list of str | non-empty, no repeats, a subset of `MediaContentType` | narrows release 1's types |
| `media_signed_url_ttl_seconds` | int | > 0; clamped to `s3_signed_url_ttl_seconds` | AE-M0-8, AE-M7-3 |
| `media_image_max_pixels` | int | > 0 | M0-A0 `max_pixels`; AE-M0-9 |
| `media_image_decode_timeout_seconds` | float | finite, > 0 | M0-A0 `decode_timeout_seconds`; AE-M0-9 |
| `media_font_parse_timeout_seconds` | float | finite, > 0 | M0-A0 `font_parse_timeout_seconds` |
| `media_decoder_max_memory_bytes` | int | > 0 | M0-A0 `max_memory_bytes` (the decoder ceiling) |

- A boolean is refused for every numeric media setting (`True` would otherwise read as 1).
- `Settings.media_upload_settings_unset()` names the unset ones (`MEDIA_UPLOAD_SETTINGS`).
  **M0-A must refuse every upload while it is non-empty** (AE-M0-1: "With the setting unset,
  every upload is refused"; AE-M0-9: "With either limit absent, every image is refused").
- `Settings.media_signed_url_ceiling_seconds()` is `min(media_signed_url_ttl_seconds,
  s3_signed_url_ttl_seconds)`, or `None` while the media setting is unset; then no media URL may
  be issued. A request above the ceiling is issued at the ceiling and its audit row records the
  TTL as issued (AE-M0-8).
- **No value is chosen** (plan: "Values written `<configured …>` are settings that default to
  unset and refuse when unset. They are never invented here."). The M0-A0 lane's E8-L item 3
  needs "the ceilings CF-7A-0 proposes"; CF-7A-0 proposes none and leaves them to the operator
  or the review (decision R-8), with the reference points in section 13.
- `Settings` grows from 155 to 165 fields: the flag, these seven and the two retention durations
  (section 5). `docs/operations/aws-staging-secret-inventory.md` classifies all ten as
  non-secret configuration.

## 5. Retention classes

- `RetentionClass.MEDIA_UPLOADS` (`media_uploads`) and `RetentionClass.MEDIA_RECORDS`
  (`media_records`) in `apps/api/app/privacy/inventory.py`, with `retention_media_uploads_days`
  and `retention_media_records_days`, both `None` (D-7: "What are the O-5 durations for the media
  retention classes").
- **Production refuses to start** while either is unset, through the existing mechanism
  (`Settings._validate_retention` requires every `RetentionClass` member's duration in production)
  (AE-M0-5). Development, test and staging may leave them unset; the migration actor is exempt.
- **The sweep refuses both classes while unset**: it reports `policy_unset` and deletes nothing.
- **Binding (stated, wired by M0-B).** `MEDIA_RETENTION_BINDINGS`: `media_assets` and
  `media_objects` → `media_uploads`; `media_provenance` and `media_rights` → `media_records`. Each
  `TableEntry.retention_class` stays `None` until M0-B adds the purge roots
  (`app/privacy/retention.py`, M0-B's file) with the object erasure: the existing inventory test
  requires every class named in a `TableEntry` to have a purge root.
- `media_intermediates` and `media_approved_packages` arrive with CF-7A-1 and CF-7A-2.
- **No indefinite exception (MD-6).** No media class is exempt from the purge or from erasure.

## 6. Privacy inventory, erasure and export

- **Table entries** (`TABLES`): `media_assets`, `media_objects`, `media_provenance` (category
  `tenant_content`) and `media_rights` (category `compliance`, its own classification, MD-4).
  All four are exported with the organization (rows, every column) and have no time-based class
  until M0-B binds them (section 5). `test_privacy_inventory.py` fails if any of the four lacks a
  `TableEntry` (AE-M0-5).
- **Object storage entry** (`OBJECT_STORES["media"]`): category `tenant_content`; key layout as in
  section 8; erasure prefix `{organization_id}/`; retention class `media_uploads`; erasure must
  remove every object version and delete marker under the prefix and abort every in-progress
  multipart upload there (plan §2.6; AE-M0-4). It replaces nothing yet:
  `docs/operations/data-inventory.md` keeps "no product path writes customer data to object
  storage", which stays true until M0-A and M0-B ship, and now points to this entry; M0-B
  replaces that row.
- **Account deletion — the attester rule.** `media_rights.attested_by` and
  `media_assets.created_by` are `SET NULL` keys, and no snapshot of the account is stored. The
  existing deletion walk (`app.privacy.erasure.delete_rows`, which follows declared keys) therefore
  sets them to NULL and keeps each row: **a rights record keeps its rights content (basis, text
  version, instant) and loses the account's identity** (AE-M0-4, last bullet). This differs from
  the Phase 5 records only in that there is no snapshot to anonymise. The rule holds at this head
  by construction, and `test_cf7a0_privacy.py` shows it through the real account-deletion route
  (with the verifier then finding nothing); M0-B owns AE-M0-4's test with stored objects and
  its verification listings (decision R-5).
- **Organization deletion.** Every media row is deleted through its organization and workspace
  keys (CASCADE). Media rows are not compliance records kept after their organization (MD-6), so
  nothing is retained. The stored objects are M0-B's version-aware prefix erasure, which does not
  rely on these rows (AE-M0-4).
- **Export.** The organization export includes the four tables (generic over `TABLES`). The stored
  bytes are not in the JSON export. Whether the account export lists the account's own uploads and
  attestations is M0-B's (`app/privacy/export.py`), as the Phase 5 freezes did for authored records.
- **Verification.** `app/privacy/verification.py` reflects the live schema, so the two user keys
  are counted in its reference check without a change. `media_assets.display_name` is customer
  text that its content scan reads; a display name that names an erased person is a finding, as
  in any tenant text column.

## 7. The CSP directive set (stated; M0-B changes the files)

At N3 the SPA policy (`apps/web/vite.config.ts:25-45`, pinned by
`apps/web/scripts/assert-csp-build.mjs:76-82`) is:

```
default-src 'none'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self';
connect-src 'self' <api>; frame-src 'none'; worker-src 'none'; media-src 'none';
object-src 'none'; base-uri 'none'; form-action 'self'; manifest-src 'none'
```

The proposed media set (AE-M0-7), where `<media>` is the bucket's exact origin
(`https://` + host, no path, no wildcard), supplied at build time and validated like the API
origin, never written into a tracked file:

| Directive | At N3 | Proposed |
|---|---|---|
| `connect-src` | `'self' <api>` | `'self' <api> <media>` (the browser's presigned PUT and part PUTs) |
| `img-src` | `'self'` | `'self' <media>` (previews by signed GET) |
| `media-src` | `'none'` | `<media>` (plan text; not needed by release 1's M0 kinds, see R-4) |
| `worker-src` | `'none'` | `'none'` (unchanged) |
| `frame-src` | `'none'` | `'none'` (unchanged) |
| every other directive | as above | unchanged; no `blob:`, `data:` or wildcard is added |

- The bucket origin appears **only** in those three directives. `assert-csp-build` must pin the
  exact policy for a given media origin and fail when any other source is added; its bundle
  origin scan must admit no other host (BT-1).
- **`font-src` is absent**, so fonts fall back to `default-src 'none'`: the browser cannot load an
  uploaded font for a specimen preview. Release 1 needs none in M0; adding one is decision R-4.
- A build without a media origin (development, `LocalStorage`) keeps today's policy.
- Reading a multipart part's `ETag` from the browser needs the bucket's CORS to expose it (M0-C).

## 8. The object key layout (stated; enforced in part)

Built with `tenant_object_key(organization_id, workspace_id, …)`
(`apps/api/app/infra/storage.py:80`), every segment validated:

| Object | Key |
|---|---|
| upload record | `{organization_id}/{workspace_id}/media/uploads/{object_id}` |
| stored object | `{organization_id}/{workspace_id}/media/assets/{asset_id}/{object_id}` |

- The tenant upload prefix M0-A0's presign binding requires (at least
  `{organization}/{workspace}/{area}`) is `{organization_id}/{workspace_id}/media/uploads`.
- Every variable segment is a server-generated 32-character hex id. **No file name, extension,
  content type or customer text is ever part of a key** (keys travel in signed URLs).
- The first segment is an organization id, so a key never begins with LocalStorage's reserved
  staging segment.
- An organization's erasure empties `{organization_id}/` (every version, every delete marker,
  every in-progress multipart upload).
- **Enforced in the schema:** `ck_media_object_storage_key` (the row's own
  `{organization_id}/{workspace_id}/media/` prefix, case-sensitive on both engines) and the unique
  `storage_key`. **Not enforced:** the area by state (`uploads` for upload records, `assets/{asset_id}`
  for stored objects); a state-dependent CHECK is decision R-2.
- Constants: `MEDIA_KEY_ROOT = "media"`, `MEDIA_UPLOAD_AREA = "uploads"`,
  `MEDIA_ASSET_AREA = "assets"` (`app/media/models.py`).

## 9. API shapes for M0-A (schemas only; no route mounted)

`apps/api/app/media/schemas.py` freezes the shapes. Nothing imports it into a router, so
`openapi.json` does not contain them. M0-A builds the routes unmounted; M0-B mounts them behind
the capability.

| Route (proposed path) | Request | Response | Serves |
|---|---|---|---|
| `POST /workspaces/{workspace_id}/media/uploads` | `MediaUploadCreate` | `MediaUploadSessionOut` (201) | AE-M0-1, AE-M0-10 |
| `POST /workspaces/{workspace_id}/media/uploads/{object_id}/parts` | `MediaUploadPartsRequest` | `MediaUploadPartsOut` | AE-M0-1, AE-M0-10 |
| `POST /workspaces/{workspace_id}/media/uploads/{object_id}/complete` | `MediaUploadComplete` | `MediaAssetOut` | AE-M0-2, AE-M0-9, AE-M0-10 |
| `GET /workspaces/{workspace_id}/media/assets` | — | `MediaAssetListOut` | the asset library |
| `GET /workspaces/{workspace_id}/media/assets/{asset_id}` | — | `MediaAssetOut` | the asset library |
| `POST /workspaces/{workspace_id}/media/assets/{asset_id}/signed-url` | `MediaSignedUrlRequest` | `MediaSignedUrlOut` | AE-M0-3, AE-M0-8 |
| `GET /workspaces/{workspace_id}/media-assets-capability` | — | `MediaAssetsCapabilityOut` | AE-M0-6 |

- **Requests are strict** (`extra="forbid"`). The caller declares kind, content type (one of the
  kind's: never SVG), size (> 0, an int), sha256 (lower-case hex), display name (1–200
  characters, no control characters) and the MD-4 attestation (`text_version` and
  `accepted: true`). Tenant scope, ids, keys, state, the account and every limit are server-side.
- **Responses expose no storage detail**: no key, version id, bucket or endpoint. A presigned URL
  goes only to the caller that asked.
- **Refusals** use the existing error envelope. Until M0-B mounts them, the routes do not exist;
  once mounted, a capability-off workspace gets the capability-off response on every media route
  (AE-M0-6), and a foreign workspace's asset the uniform non-enumerating 404 with no URL minted
  and no issuance audited (AE-M0-3).
- **Contract regeneration.** The Phase 5 freezes regenerated `apps/api/openapi.json` and
  `apps/web/src/api/schema.d.ts` at freeze time; CF-7A-0 does too. The two changes are the new
  required `FeatureFlagsOut.media_assets_enabled` and the `media_assets` member of the
  `Capability` enum (the operator capability surfaces).
- **Audit actions.** Signed-GET issuance and upload outcomes need `AuditAction` members, which
  are a closed registry with pinned counts (`test_cf1_audit_action_registry.py`). CF-7A-0 adds
  none; proposed names for a freeze amendment: `media_asset.uploaded`, `media_asset.rejected`,
  `media_asset.url_issued` (decision R-11).

## 10. The freeze-owned web surface

As far as the Phase 5 freezes touched it at freeze time:

- `apps/web/src/api/queryKeys.ts`: `mediaAssets(workspaceId)`,
  `mediaAsset(workspaceId, assetId)` and `mediaAssetsCapability(workspaceId)`, every key
  embedding the workspace id; pinned by `apps/web/src/api/__tests__/media-query-keys.test.ts`.
- `apps/web/src/test/handlers.ts`: the flag in the `features` fixture and the capability in the
  operator registry fixture, mirroring the real API.
- Generated: `apps/web/src/api/schema.d.ts`.
- Not touched: `App.tsx`, `nav.ts`, `notifications.tsx`, `types.ts`, `endpoints.ts`,
  `labels.ts`, `badges.tsx`, `test/fixtures.ts`. The routes, navigation and UI are M0-B's
  (`apps/web/src/pages/media/library/`); their freeze-owned entries come through the integration
  owner in M0's integration window.

## 11. Test ownership by exact filename

Cross-authored (plan §3.0, Phase 5 plan §6.1): no lane tests its own code; W3 writes no tests;
W4 holds the whole adversarial battery and W3's UI tests. Ownership is by exact filename; every
integration checks that no new test file matches zero lists.

**CF-7A-0 (integration owner), at this head:**
- `apps/api/app/tests/test_cf7a0_contract_migration.py`
- `apps/api/app/tests/test_cf7a0_postgres.py`
- `apps/api/app/tests/test_cf7a0_contracts.py`
- `apps/api/app/tests/test_cf7a0_privacy.py`
- `apps/web/src/api/__tests__/media-query-keys.test.ts`

**Authored by the M0-B lane (testing M0-A's code):**
- `apps/api/app/tests/test_media_assets_upload_session.py` — AE-M0-1 (size refusal before presign; every upload refused while a setting is unset)
- `apps/api/app/tests/test_media_storage_multipart.py` — AE-M0-1 multipart path and AE-M0-10 part binding, through the service
- `apps/api/app/tests/test_media_assets_completion.py` — AE-M0-2, AE-M0-9, AE-M0-10 (refusal at completion, no promotion, upload-area object deleted with every version; EXIF stripped)
- `apps/api/app/tests/test_media_assets_library.py` — library list and detail shapes, paging
- `apps/api/app/tests/test_media_assets_signed_url.py` — AE-M0-8 (clamp to the ceiling; the audit row records the TTL)

**Authored by the M0-A lane (testing M0-B's code):**
- `apps/api/app/tests/test_media_erasure_objects.py` — AE-M0-4 (version-aware erasure, multipart abort, report counts, organization B untouched, the attester rule)
- `apps/api/app/tests/test_media_retention_purge.py` — AE-M0-5 (the purge refuses while unset; the bound roots; object erasure with the purge)
- `apps/api/app/tests/test_media_verification.py` — AE-M0-4 (`privacy/verification.py` checks the three listings independently)
- `apps/api/app/tests/test_media_capability_gate.py` — AE-M0-6 (every mounted media route answers capability-off)

**Authored by W4 (adversarial battery and W3's UI tests):**
- `apps/api/app/tests/test_media_isolation.py` — AE-M0-3 (API and direct query, cross-workspace and cross-organization)
- `apps/api/app/tests/test_media_upload_adversarial.py` — type confusion, polyglots, SVG, oversize and out-of-prefix parts, key escape
- `apps/api/app/tests/test_media_boundary.py` — BT-1 to BT-4 over M0's files
- `apps/web/scripts/assert-csp-build.test.mjs` — the existing file, extended for AE-M0-7 (the file is M0-B's lane's to change; the test cases are W4's)
- `apps/web/src/pages/media/library/__tests__/media-library.test.tsx` — the asset-library UI (W3's code)

**Kept by M0-A0** (not reassigned): `test_media_assets_{fonts,images,pipeline,scanning,types}.py`
and `test_storage_adapter_{compat,fakes,presign,streaming,versions}.py`. The plan's example glob
`test_media_assets_*.py` would overlap them, which is why the lists above are exact names.

**Existing tests that later lanes must extend** (each an exact-value or additive update in the
lane that changes the pinned set): `test_cross_organization_isolation.py` (every new
customer-facing route gets a cross-organization probe in the PR that mounts it, LANE-BRIEF-COMMON
§10), `test_auth2_leakage.py` and `test_e7_role_policy_matrix.py` (operation counts, recomputed
from code when routes are mounted).

## 12. The malware-scanning approach (proposed; the review decides)

Constraint (plan M0): "release 1 accepts only re-encodable images and validated fonts". M0-A0
provides the scanning seam (an interface and a deterministic test scanner); `media_provenance.scanner`
records which scanner cleared each object.

| Option | What it is | For | Against |
|---|---|---|---|
| **S-1 Transform, no engine** | No anti-malware engine. Every image is decoded and re-encoded (the stored bytes are a new encoding of the pixels; metadata and appended payloads are dropped); every font is parsed and validated; the seam is bound to a recorded no-engine scanner | no new image, dependency, egress or cost; matches the constraint; nothing unscanned is ever served as an image | fonts are stored as uploaded after validation, so bytes in tables the validator does not interpret can survive |
| **S-1b** S-1 plus font re-serialisation | fonts rebuilt from their parsed tables, keeping an allowlist of tables | closes S-1's font gap with the same toolchain | a change to M0-A0's library and its review; some fonts may lose features |
| **S-2 Self-hosted engine** | a signature-based engine in its own image, behind the seam | detects known malware signatures in fonts | its image falls under `Container build and security` (plan :362); signature updates need egress (BT-1) or a rebuilt image; memory; low value for re-encoded images |
| **S-3 Managed AWS service** | a managed malware-scanning service on the bucket | no engine in our images | an AWS operation and cost: joins the MD-9 bundle under D-9 (plan :363); asynchronous verdicts need a new object state (a widening); not exercisable in CI |

**Recommendation (CF-7A-0's, not a ruling):** S-1 for release 1, with S-1b decided by this review
for M0-A0's library before uploads are enabled, and the seam kept so S-2 or S-3 can be added
without a schema change except, for S-3, a `MediaObjectState` widening.

## 13. Decisions left to the R-CF7A-0 review

| # | Decision | CF-7A-0's proposal |
|---|---|---|
| R-1 | **SVG**: excluded or rasterised on upload | excluded; enforced by the vocabularies and the partition CHECK |
| R-2 | **The key layout** (section 8), and whether the area by state becomes a CHECK | the layout of section 8; tenant prefix enforced, area not |
| R-3 | **The malware-scanning approach** (section 12) | S-1, with S-1b decided here |
| R-4 | **The CSP set** (section 7): `media-src` now or when a media kind needs it; `font-src` for font previews | as the plan names (`connect-src`, `img-src`, `media-src`); no `font-src` |
| R-5 | **The attester erasure rule** (section 6) | `SET NULL` with no snapshot: the rights content stays, the identity goes |
| R-6 | The closed sets: asset states, object states, the content types (WOFF 2 and GIF excluded) | as in section 2 |
| R-7 | The package: `app/media/models.py` and `schemas.py` with no `app/media/__init__.py` (an implicit namespace portion), so that no M0-A0 file is touched; M0-A0's `__init__.py` makes it a regular package when it lands | as built; the alternative is a CF-owned `__init__.py` and one add/add conflict with M0-A0 |
| R-8 | Values for the M0-A0 limits (E8-L item 3 needs "the ceilings CF-7A-0 proposes") | none chosen. Reference points only: M0-A0's own tests use 1,000,000 pixels, 30 s and a 2 GiB address-space ceiling, and its Linux bomb test a 256 MiB ceiling; these are test values, not product limits |
| R-9 | Whether `media_provenance` and `media_rights` join CF-1's append-only guard and the PostgreSQL backstop | not at CF-7A-0 (it needs edits to pinned sets beyond class ruling CF7A0-1) |
| R-10 | Whether a workspace override may enable `media_assets` | yes, as for the Phase 5 flags (O-3's pattern) |
| R-11 | The audit actions for M0-A (section 9) | a freeze amendment adding `media_asset.uploaded`, `media_asset.rejected`, `media_asset.url_issued` |
| R-12 | Limitation L-1 (section 15): a media duration set before M0-B makes the sweep raise, and two existing retention tests fail on it | (a) CF-7A-0 registers both media classes in `retention.py`'s `_ROOTS` with no roots (no table is purged by them until M0-B binds roots with the object erasure; no media row can exist before M0-B mounts the routes); or (b) the media classes are registered in `RetentionClass` by M0-B with its purge, as 5E-CF declared `retention_notifications_days` before 5E-W1 registered its class (production would then not require the media durations until M0-B); or (c) a refusal status for a class with no roots, with a ruling on the two tests that assume every class purges |
| R-13 | The PostgreSQL full-chain step-down proof (`test_postgres_migration_proof.py::test_full_chain_round_trips_on_postgresql_under_the_confirmation_guard`) steps down one revision at a time from the head; a new head needs a new step, which is new assertions, not an exact-value update (class ruling CF7A0-1) | add CF-7A-0's step (the four media tables go, the CHECK narrows, the backstop stays), as #326 added 5E-CF's; the test fails at this head until then |
| R-14 | `test_workspace_capability_override_migration.py::test_the_5e_cf_downgrade_refuses_while_an_override_names_the_capability` addresses 5E-CF's revision as `"head"`; naming it (`NA_WIDENING`) is the same revision at N3, so the edit does not fail against N3's product code and is outside class ruling CF7A0-1 | name the revision, as the other 5E-CF tests now do; the test fails at this head until then |

## 14. Acceptance examples this freeze serves

| AE | What CF-7A-0 provides | Who proves it |
|---|---|---|
| AE-M0-1 | `media_upload_max_bytes` (unset); key layout; `sha256` column; multipart shapes | M0-B lane's tests of M0-A |
| AE-M0-2 | `unsupported_media_type` by vocabulary; `rejected` state; no SVG | M0-B lane; W4 |
| AE-M0-3 | composite workspace keys; non-enumerating 404 stated | W4 (`test_media_isolation.py`) |
| AE-M0-4 | `OBJECT_STORES` entry; cascades; the attester rule by `SET NULL` | M0-A lane's tests of M0-B |
| AE-M0-5 | four `TableEntry` rows; two classes; production refusal | CF-7A-0 (`test_cf7a0_privacy.py`, `test_privacy_inventory.py`); the purge: M0-A lane |
| AE-M0-6 | the flag, capability and `FeatureFlagsOut` field | CF-7A-0 (`test_cf7a0_contracts.py`, `test_api_isolation.py`); the routes: M0-A lane |
| AE-M0-7 | the directive set (section 7) | W4, on M0-B's change |
| AE-M0-8 | `media_signed_url_ceiling_seconds()`; `MediaSignedUrlOut.ttl_seconds` | M0-B lane |
| AE-M0-9 | the four M0-A0 limit settings (unset) | M0-B lane |
| AE-M0-10 | declared size/sha256/type; terminal upload states | M0-B lane; W4 |

## 15. What CF-7A-0 does not do, and its limitations

**Does not:** mount a route; add a service, job type, audit action, metric or span; touch
`app/infra/storage.py`, `app/media/__init__.py` or `app/media/assets/` (M0-A0); change
`app/privacy/{erasure,retention,verification,export}.py` (M0-B); change the CSP files (M0-B);
touch `infra/aws/**` (M0-C), `ci.yml` or any lock file; choose any limit, duration or provider;
activate anything.

**Live changes at merge** (behaviour visible without enabling anything):
1. `GET /system/capabilities` (and every `features` reflection) gains
   `media_assets_enabled: false`.
2. The operator capability surfaces list an eighth, dark capability, `media_assets`; the override
   plane accepts it (the CHECK is widened).
3. Production refuses to start until `RETENTION_MEDIA_UPLOADS_DAYS` and
   `RETENTION_MEDIA_RECORDS_DAYS` are set (it already refuses while any of the eight existing
   durations is unset).
4. The retention sweep reports two more classes, `policy_unset`.
5. The organization export index lists four more tables (empty).
6. The database gains four empty tables; a downgrade below `91a796f09399` refuses while an
   override names `media_assets` or a media table holds a row.

**Existing tests failing at this head, by design, pending rulings:** the two retention sweep tests (R-12), the PostgreSQL full-chain step-down proof (R-13) and the 5E-CF refusal test (R-14). Each has a trial edit, measured to pass, in the lane's evidence; none is committed without a ruling.

**Limitations:**
- **L-1.** `retention.py` has no purge roots for the media classes (M0-B's file). With a media
  duration set, `sweep()` raises `KeyError` at that class after the classes before it have run,
  and deletes nothing of it; a dry run raises the same way. While the durations are unset (the
  default, and the only state before D-7) the sweep reports `policy_unset`. Two existing tests
  set every duration and sweep every class
  (`test_privacy_retention.py::test_platform_classes_purge_only_rows_past_the_cutoff` and
  `::test_dry_run_counts_and_changes_nothing`), so they fail on L-1 at this head. Neither a
  `retention.py` change nor an edit of those tests is within CF-7A-0's brief; decision R-12 asks
  for the ruling.
- **L-2.** `ck_media_object_sha256` and `ck_media_provenance_inputs_hash` check length and case,
  not that every character is hexadecimal (a portable CHECK cannot); the request schema and the
  writer do.
- **L-3.** `media_provenance.parent_object_id` is not workspace-bound by a key (section 1).
- **L-4.** The tables are not append-only (R-9).
