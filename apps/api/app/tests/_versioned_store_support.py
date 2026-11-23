"""A local versioned object store, and synthetic media rows, for the M0-B tests. Not a test module.

Written by the independent test author of M0-B's code (lane M0B-T, coordinator ruling M0B-1, for
the M0-A lane's test role in contract section 11). Used by ``test_media_erasure_objects.py``,
``test_media_retention_purge.py`` and ``test_media_verification.py``.

**The store** (:class:`VersionedStore`) is a test double of M0-B's storage protocol,
``app.privacy.object_erasure.VersionedObjectStore``: ``list_object_versions``,
``delete_version``, ``list_multipart_uploads`` and ``abort_multipart_upload``. It behaves as a
versioned S3 bucket does, which is why erasure must remove every version (contract sections 6
and 8; plan section 2.6; AE-M0-4):

* ``put`` adds a new version, which becomes the key's latest entry; a plain ``delete`` (no
  version id) only adds a delete marker as the latest entry and removes nothing, even on a key
  with no version;
* ``delete_version(key, version_id)`` removes exactly that version or delete marker,
  permanently, and never adds a marker; removing what is already gone is not an error;
* an in-progress multipart upload holds customer bytes without being an object; aborting it
  discards it, and aborting one that no longer exists is not an error;
* a listing of ``prefix`` returns only keys strictly under ``prefix + "/"``: never ``prefix``
  itself and never a sibling (``org-10/`` is not under ``org-1``). Versions come key by key,
  newest first, and the listing is lazy, as a paged listing is.

**Faults** (the incomplete and retry paths): a ``delete_version`` or ``abort_multipart_upload``
that raises for chosen keys; a ``delete_version`` or ``abort_multipart_upload`` that returns
without doing anything; a listing that raises, on every call or only on its n-th call, before
its first entry or after n entries, for every prefix or one; and a store that over-returns
entries outside the prefix it was asked for. :meth:`VersionedStore.heal` clears them all.

**Inspection.** Every call is recorded in ``calls``, so a test can show that nothing was asked
of the store, or that no call named a key outside a tenant's prefix. :meth:`VersionedStore.tally`
counts what a prefix holds, and :meth:`VersionedStore.snapshot` gives a comparable picture of it
(every version's bytes included), so "count before equals count after" is checked exactly.

**Rows.** :class:`MediaRows` builds synthetic media rows with the contract's key layout
(section 8): an upload record at ``{org}/{ws}/media/uploads/{object_id}``, a stored object at
``{org}/{ws}/media/assets/{asset_id}/{object_id}``. :func:`engine_world` gives the seeded privacy
world of ``_privacy_support`` on SQLite or on PostgreSQL (``TEST_POSTGRES_URL``).
"""

from __future__ import annotations

import itertools
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sqlalchemy import insert
from sqlalchemy.engine import Engine

from app.media.models import MediaAsset, MediaObject, MediaProvenanceRecord, MediaRightsRecord
from app.tests._auth2_support import require_postgres
from app.tests._privacy_support import PrivacyEnv, privacy_world

ENGINES = ("sqlite", "postgresql")


# --------------------------------------------------------------------------- #
# The store
# --------------------------------------------------------------------------- #
class StoreFault(RuntimeError):
    """What the double raises for an injected fault: it stands in for a driver error."""


@dataclass(frozen=True)
class ListedVersion:
    """One entry of a version-aware listing (an object version or a delete marker)."""

    key: str
    version_id: str
    is_latest: bool
    is_delete_marker: bool
    size: int | None


@dataclass(frozen=True)
class ListedUpload:
    """One in-progress multipart upload."""

    key: str
    upload_id: str


@dataclass(frozen=True)
class Tally:
    """What one prefix holds. ``versions`` = ``current`` + ``noncurrent``."""

    keys: int = 0
    versions: int = 0
    current: int = 0
    noncurrent: int = 0
    delete_markers: int = 0
    uploads: int = 0

    @property
    def empty(self) -> bool:
        return self == Tally()


@dataclass
class _Entry:
    version_id: str
    is_delete_marker: bool
    data: bytes | None


@dataclass
class _ListingFault:
    kind: str  # "versions" or "uploads"
    call: int | None  # the n-th call of that listing (1-based); None: every call
    after: int  # entries yielded before it raises
    prefix: str | None  # only a listing of this prefix; None: every prefix


class VersionedStore:
    """An in-memory versioned bucket with fault injection (module docstring)."""

    def __init__(self) -> None:
        self._objects: dict[str, list[_Entry]] = {}  # per key, oldest entry first
        self._uploads: dict[str, tuple[str, int]] = {}  # upload id -> (key, parts)
        self._ids = itertools.count(1)
        self._listing_calls = {"versions": 0, "uploads": 0}
        #: Every protocol call, in order: (method, key or prefix, ...).
        self.calls: list[tuple] = []
        self.heal()

    # -- faults --------------------------------------------------------------
    def heal(self) -> None:
        """Clear every injected fault (the stored state is kept)."""
        self._failing_deletes: set[str] = set()
        self._silent_deletes: set[str] = set()
        self._failing_aborts: set[str] = set()
        self._silent_aborts: set[str] = set()
        self._listing_faults: list[_ListingFault] = []
        self._over_return: tuple[str, ...] = ()

    def fail_deletes(self, *keys: str) -> None:
        """``delete_version`` of any version or marker of these keys raises."""
        self._failing_deletes.update(keys)

    def silence_deletes(self, *keys: str) -> None:
        """``delete_version`` of these keys returns without deleting anything."""
        self._silent_deletes.update(keys)

    def fail_aborts(self, *keys: str) -> None:
        """``abort_multipart_upload`` of an upload of these keys raises."""
        self._failing_aborts.update(keys)

    def silence_aborts(self, *keys: str) -> None:
        """``abort_multipart_upload`` of these keys returns without aborting."""
        self._silent_aborts.update(keys)

    def fail_listing(
        self, kind: str, *, call: int | None = None, after: int = 0, prefix: str | None = None
    ) -> None:
        """A ``kind`` listing (``"versions"`` or ``"uploads"``) raises: on its ``call``-th call
        only (counted from now on, 1-based) or on every call; after ``after`` entries; for
        ``prefix`` only or for every prefix."""
        assert kind in ("versions", "uploads"), kind
        start = self._listing_calls[kind]
        self._listing_faults.append(
            _ListingFault(kind, None if call is None else start + call, after, prefix)
        )

    def over_return(self, *literal_prefixes: str) -> None:
        """A misbehaving store: every listing also returns every entry whose key starts with one
        of these strings (no ``/`` boundary), whatever prefix it was asked for."""
        self._over_return = tuple(literal_prefixes)

    # -- writes that build a world (not part of the protocol) ----------------
    def put(self, key: str, data: bytes | None = None) -> str:
        """A new version of ``key``; it becomes the latest entry."""
        version_id = f"v{next(self._ids):06d}"
        body = data if data is not None else f"{key}#{version_id}".encode()
        self._objects.setdefault(key, []).append(_Entry(version_id, False, body))
        return version_id

    def delete(self, key: str) -> str:
        """A plain delete on a versioned bucket: a delete marker becomes the latest entry."""
        marker_id = f"m{next(self._ids):06d}"
        self._objects.setdefault(key, []).append(_Entry(marker_id, True, None))
        return marker_id

    def start_upload(self, key: str, parts: int = 2) -> str:
        """An in-progress multipart upload of ``key`` holding ``parts`` parts."""
        upload_id = f"u{next(self._ids):06d}"
        self._uploads[upload_id] = (key, parts)
        return upload_id

    # -- the protocol --------------------------------------------------------
    def list_object_versions(self, prefix: str) -> Iterator[ListedVersion]:
        self.calls.append(("list_object_versions", prefix))
        _check_prefix(prefix)
        self._listing_calls["versions"] += 1
        entries = [
            ListedVersion(key, e.version_id, i == len(held) - 1, e.is_delete_marker,
                          None if e.is_delete_marker else len(e.data or b""))
            for key, held in sorted(self._objects.items())
            if self._listed(key, prefix)
            for i, e in reversed(list(enumerate(held)))  # newest first
        ]
        return self._emit("versions", prefix, entries)

    def delete_version(self, key: str, version_id: str) -> None:
        self.calls.append(("delete_version", key, version_id))
        if key in self._failing_deletes:
            raise StoreFault("injected delete failure")
        if key in self._silent_deletes:
            return
        held = [e for e in self._objects.get(key, []) if e.version_id != version_id]
        if held:
            self._objects[key] = held
        else:
            self._objects.pop(key, None)

    def list_multipart_uploads(self, prefix: str) -> Iterator[ListedUpload]:
        self.calls.append(("list_multipart_uploads", prefix))
        _check_prefix(prefix)
        self._listing_calls["uploads"] += 1
        entries = sorted(
            (
                ListedUpload(key, upload_id)
                for upload_id, (key, _parts) in self._uploads.items()
                if self._listed(key, prefix)
            ),
            key=lambda u: (u.key, u.upload_id),
        )
        return self._emit("uploads", prefix, entries)

    def abort_multipart_upload(self, upload) -> None:
        self.calls.append(("abort_multipart_upload", upload.key, upload.upload_id))
        if upload.key in self._failing_aborts:
            raise StoreFault("injected abort failure")
        if upload.key in self._silent_aborts:
            return
        held = self._uploads.get(upload.upload_id)
        if held is not None and held[0] == upload.key:
            del self._uploads[upload.upload_id]

    # -- inspection ----------------------------------------------------------
    def tally(self, prefix: str) -> Tally:
        """What is held strictly under ``prefix + "/"`` (independent of the listing code)."""
        under = prefix + "/"
        keys = current = noncurrent = markers = 0
        for key, held in self._objects.items():
            if not key.startswith(under) or not held:
                continue
            keys += 1
            for i, e in enumerate(held):
                if e.is_delete_marker:
                    markers += 1
                elif i == len(held) - 1:
                    current += 1
                else:
                    noncurrent += 1
        uploads = sum(1 for key, _ in self._uploads.values() if key.startswith(under))
        return Tally(keys, current + noncurrent, current, noncurrent, markers, uploads)

    def holds(self, key: str) -> tuple[int, int]:
        """(versions, delete markers) held for exactly ``key``."""
        held = self._objects.get(key, [])
        markers = sum(1 for e in held if e.is_delete_marker)
        return len(held) - markers, markers

    def uploads_of(self, key: str) -> int:
        return sum(1 for k, _ in self._uploads.values() if k == key)

    def snapshot(self, *, under: str | None = None, outside: str | None = None) -> tuple:
        """Every entry (key, id, marker, bytes) and upload, sorted: all of them, only those
        strictly under ``under + "/"``, or only those not under ``outside + "/"``."""

        def keep(key: str) -> bool:
            if under is not None and not key.startswith(under + "/"):
                return False
            return outside is None or not key.startswith(outside + "/")

        versions = tuple(
            (key, e.version_id, e.is_delete_marker, e.data)
            for key, held in sorted(self._objects.items())
            if keep(key)
            for e in held
        )
        uploads = tuple(
            sorted((key, upload_id, parts) for upload_id, (key, parts) in self._uploads.items()
                   if keep(key))
        )
        return versions, uploads

    def mutating_calls(self) -> list[tuple]:
        return [c for c in self.calls if c[0] in ("delete_version", "abort_multipart_upload")]

    def keys_acted_on(self) -> set[str]:
        """Every key passed to ``delete_version`` or ``abort_multipart_upload``."""
        return {c[1] for c in self.mutating_calls()}

    # -- internals -----------------------------------------------------------
    def _listed(self, key: str, prefix: str) -> bool:
        return key.startswith(prefix + "/") or any(key.startswith(p) for p in self._over_return)

    def _emit(self, kind: str, prefix: str, entries: list) -> Iterator:
        number = self._listing_calls[kind]
        fault = next(
            (
                f
                for f in self._listing_faults
                if f.kind == kind
                and (f.call is None or f.call == number)
                and (f.prefix is None or f.prefix == prefix)
            ),
            None,
        )

        def generate():
            for i, entry in enumerate(entries):
                if fault is not None and i == fault.after:
                    raise StoreFault(f"injected {kind} listing failure")
                yield entry
            if fault is not None and fault.after >= len(entries):
                raise StoreFault(f"injected {kind} listing failure")

        return generate()


def _check_prefix(prefix: str) -> None:
    """A listing prefix is a key with no leading or trailing slash (M0-A0's listing rule)."""
    if not isinstance(prefix, str) or not prefix or prefix.startswith("/") or prefix.endswith("/"):
        raise ValueError("a listing prefix is a key without a leading or trailing slash")


# --------------------------------------------------------------------------- #
# Synthetic media rows (contract sections 1 and 8)
# --------------------------------------------------------------------------- #
SHA = "c" * 64


def upload_key(org: str, ws: str, object_id: str) -> str:
    return f"{org}/{ws}/media/uploads/{object_id}"


def asset_area(org: str, ws: str, asset_id: str) -> str:
    return f"{org}/{ws}/media/assets/{asset_id}"


def asset_key(org: str, ws: str, asset_id: str, object_id: str) -> str:
    return f"{asset_area(org, ws, asset_id)}/{object_id}"


@dataclass
class MediaRows:
    """Rows to insert, built in foreign-key order. Every id is at most 32 characters."""

    assets: list[dict] = field(default_factory=list)
    objects: list[dict] = field(default_factory=list)
    provenance: list[dict] = field(default_factory=list)
    rights: list[dict] = field(default_factory=list)

    def asset(
        self, org: str, ws: str, asset_id: str, *, state: str, at: datetime,
        created_by: str | None = None,
    ) -> str:
        self.assets.append({
            "id": _id(asset_id), "organization_id": org, "workspace_id": ws,
            "kind": "raster_image", "state": state, "display_name": f"{asset_id} logo",
            "created_by": created_by, "created_at": at, "updated_at": at,
        })
        return asset_id

    def upload(
        self, org: str, ws: str, object_id: str, asset_id: str, *, state: str, at: datetime
    ) -> str:
        """An upload record (``state`` is never ``stored``); returns its key."""
        assert state != "stored"
        key = upload_key(org, ws, object_id)
        self.objects.append(_object(org, ws, object_id, asset_id, key, state, at))
        return key

    def stored(
        self, org: str, ws: str, object_id: str, asset_id: str, *, at: datetime,
        parent: str | None = None, provenance_at: datetime | None = None,
    ) -> str:
        """A stored object with its provenance record; returns its key."""
        key = asset_key(org, ws, asset_id, object_id)
        self.objects.append(_object(org, ws, object_id, asset_id, key, "stored", at))
        self.provenance.append({
            "id": _id(f"{object_id}-pv"), "organization_id": org, "workspace_id": ws,
            "object_id": object_id, "origin": "customer_upload", "inputs_hash": SHA,
            "parent_object_id": parent, "processor": "sanitiser/1", "scanner": "no-engine/1",
            "created_at": provenance_at or at,
        })
        return key

    def rights_record(
        self, org: str, ws: str, asset_id: str, *, attested_by: str | None, at: datetime
    ) -> str:
        rights_id = _id(f"{asset_id}-rt")
        self.rights.append({
            "id": rights_id, "organization_id": org, "workspace_id": ws, "asset_id": asset_id,
            "basis": "customer_attestation", "attestation_text_version": "attest-1",
            "attested_by": attested_by, "attested_at": at, "created_at": at,
        })
        return rights_id

    def insert(self, engine: Engine) -> None:
        with engine.begin() as conn:
            for table, rows in (
                (MediaAsset.__table__, self.assets),
                (MediaObject.__table__, self.objects),
                (MediaProvenanceRecord.__table__, self.provenance),
                (MediaRightsRecord.__table__, self.rights),
            ):
                if rows:
                    conn.execute(insert(table), rows)


def _id(value: str) -> str:
    assert len(value) <= 32, value
    return value


def _object(org, ws, object_id, asset_id, key, state, at) -> dict:
    return {
        "id": _id(object_id), "organization_id": org, "workspace_id": ws,
        "asset_id": asset_id, "kind": "raster_image", "content_type": "image/png",
        "storage_key": key, "size_bytes": 10, "sha256": SHA, "state": state,
        "created_at": at, "updated_at": at,
    }


MEDIA_MODELS = (MediaAsset, MediaObject, MediaProvenanceRecord, MediaRightsRecord)


def media_rows(env: PrivacyEnv) -> dict[str, dict[str, dict]]:
    """Every media row, by table and id, read through the witness engine."""
    return {
        model.__tablename__: {row["id"]: row for row in env.rows(model)}
        for model in MEDIA_MODELS
    }


# --------------------------------------------------------------------------- #
# The world, on either engine
# --------------------------------------------------------------------------- #
@contextmanager
def engine_world(engine: str, tmp_path: Path) -> Iterator[PrivacyEnv]:
    """``_privacy_support``'s seeded world on SQLite, or on PostgreSQL (``TEST_POSTGRES_URL``:
    skipped outside CI when unset, a failure in CI)."""
    if engine == "postgresql":
        require_postgres()
        with privacy_world(None) as env:
            yield env
    else:
        assert engine == "sqlite", engine
        with privacy_world(tmp_path / "m0b-tests.db") as env:
            yield env


__all__ = [
    "ENGINES",
    "MEDIA_MODELS",
    "SHA",
    "ListedUpload",
    "ListedVersion",
    "MediaRows",
    "StoreFault",
    "Tally",
    "VersionedStore",
    "asset_area",
    "asset_key",
    "engine_world",
    "media_rows",
    "upload_key",
]
