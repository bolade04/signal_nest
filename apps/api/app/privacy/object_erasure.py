"""Object-storage erasure for Phase 7A media (M0-B; media plan v3 §2.6, MD-6; AE-M0-4).

MD-6: "Include object storage in privacy inventory and erasure before customer uploads ship."
The media bucket is versioned, so a plain delete only adds a delete marker and leaves every
version. Erasure therefore removes **every object version and every delete marker** in its
scope and **aborts every in-progress multipart upload** there, whose parts hold customer
bytes without being objects (``OBJECT_STORES["media"]`` in :mod:`app.privacy.inventory`).

**The store interface.** M0-B needs four operations, which the storage adapter of CF-7A-0
does not have and M0-A0 adds (``app/infra/storage.py`` at ``5e5fa350``, held):
``list_object_versions``, ``delete_version``, ``list_multipart_uploads`` and
``abort_multipart_upload``. :class:`VersionedObjectStore` names exactly those, with M0-A0's
method names, parameters and return types, so M0-A0's ``LocalStorage`` and ``S3Storage``
satisfy it without change. :class:`ObjectVersion` and :class:`InProgressUpload` are structural
equivalents of M0-A0's dataclasses of the same names (they read the same attributes and
nothing else); no M0-A0 code is copied here. ``delete_all_versions`` and ``purge_prefix`` are
not used: they raise after a partial batch without saying what they removed, and this module
counts every deletion itself.

**Scope.** An erasure touches only what its :class:`ObjectScope` owns: every key strictly under
one of its prefixes, and every version of each of its exact keys. Prefixes and keys are built
from server-side ids, each validated as one key segment, and an entry a store returns outside
the scope is never deleted. A listing covers ``prefix + "/"`` only, so ``org-1`` never reaches
``org-10/…``.

**Completeness is measured, not assumed.** After the deletions and aborts, the same listings run
again; the scope is ``complete`` only when no call raised and the fresh listings find nothing
left in it. A delete that returns without deleting (M0-A0's ``LocalStorage.delete_version``
ignores a version id other than ``"null"``) is therefore still caught. A report is never
complete over a failed or skipped deletion.

**Safe to retry.** Each run lists what is there now and, except in the cases named at the end of
this paragraph, counts only what it removed, so a second run after a partial failure removes the
rest and counts nothing twice. Removal is confirmed by the same fresh listing, not assumed from a
call: an entry this run asked to delete or abort counts as removed when a fresh listing that ran
to its end no longer shows it (a delete that returned without deleting is not counted; one that
was applied and then raised is counted, and its error still makes the run incomplete). Where the
fresh listing over an entry failed, the run is incomplete and only a call that returned counts.
An object (a key) is counted by the run that leaves no version and no delete marker of it, so the
counts summed over runs are what was removed (AE-M0-4), except where a run's fresh listing over
an entry failed while that entry's call was applied and then raised (it is never counted), or
returned without removing it (it is counted by that run and again by the run that removes it),
or while something was written under the prefix (its key can be counted by that run and again
by the next); every such run is incomplete.

**No store.** :func:`resolve_object_store` returns the application's store
(``app.infra.storage.storage``, read at call time) only when it has the four operations. At
CF-7A-0 it has none of them, so the object step reports ``unavailable``: never complete. No
product path can have written a media object with that adapter (M0-A's uploads need M0-A0's
operations, and no media route is mounted), which is why organization deletion may proceed
with that report while the retention purge keeps every root it cannot erase
(:mod:`app.privacy.erasure`, :mod:`app.privacy.retention`).

Nothing here touches the database, logs a key or a bucket, or commits anything.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from logging import ERROR, INFO, WARNING
from typing import Any, Protocol, runtime_checkable

from app.core.errors import ObjectStorageUnavailableError
from app.core.logging import get_logger, log_event

logger = get_logger("signalnest.privacy.objects")

#: The ``OBJECT_STORES`` entry this module erases (:mod:`app.privacy.inventory`).
MEDIA_STORE = "media"


# --------------------------------------------------------------------------- #
# The protocol (M0-A0's names and signatures at 5e5fa350; structural, not copied)
# --------------------------------------------------------------------------- #
class ObjectVersion(Protocol):
    """One entry of a version-aware listing: an object version or a delete marker.

    Structural equivalent of M0-A0's ``app.infra.storage.ObjectVersion``."""

    @property
    def key(self) -> str: ...
    @property
    def version_id(self) -> str: ...
    @property
    def is_latest(self) -> bool: ...
    @property
    def is_delete_marker(self) -> bool: ...
    @property
    def size(self) -> int | None: ...


class InProgressUpload(Protocol):
    """An in-progress multipart upload. Structural equivalent of M0-A0's
    ``app.infra.storage.InProgressUpload``."""

    @property
    def key(self) -> str: ...
    @property
    def upload_id(self) -> str: ...


@runtime_checkable
class VersionedObjectStore(Protocol):
    """The four storage operations M0-B's erasure, purge and verification need.

    The same names, parameters and return types as M0-A0's ``Storage`` protocol. M0-A0's
    ``abort_multipart_upload`` accepts ``MultipartUpload | InProgressUpload``; M0-B passes back
    only uploads the same store listed, so the narrower parameter here is satisfied by it."""

    def list_object_versions(self, prefix: str) -> Iterator[ObjectVersion]: ...
    def delete_version(self, key: str, version_id: str) -> None: ...
    def list_multipart_uploads(self, prefix: str) -> Iterator[InProgressUpload]: ...
    def abort_multipart_upload(self, upload: InProgressUpload) -> None: ...


def supports_object_erasure(store: object) -> bool:
    """Whether ``store`` has the four operations (a run-time structural check)."""
    return store is not None and isinstance(store, VersionedObjectStore)


def resolve_object_store() -> VersionedObjectStore | None:
    """The application's store when it can erase versions, else ``None``.

    Reads ``app.infra.storage.storage`` at call time (imported here, not at module import, so
    importing the privacy code builds no store)."""
    from app.infra import storage as storage_module

    store = storage_module.storage
    return store if supports_object_erasure(store) else None


# --------------------------------------------------------------------------- #
# Scopes
# --------------------------------------------------------------------------- #
class InvalidObjectScope(ValueError):
    """A prefix or key that is not inside the tenant prefix it must be inside."""


def require_segment(value: str, *, label: str) -> str:
    """``value`` if it is exactly one safe key segment (an id), else raise."""
    if (
        not isinstance(value, str)
        or value in ("", ".", "..")
        or "/" in value
        or "\\" in value
        or "\x00" in value
    ):
        raise InvalidObjectScope(f"{label} is not a single key segment")
    return value


def organization_prefix(organization_id: str) -> str:
    """The listing prefix of an organization's objects: its id. A listing covers
    ``"{organization_id}/"``, the inventory's erasure prefix."""
    return require_segment(organization_id, label="organization id")


def _unsafe(key: str) -> bool:
    """A key with an empty, ``.`` or ``..`` segment, a backslash or a NUL: M0-B never asks a store
    to act on one (a filesystem store could resolve it elsewhere); its scope stays incomplete."""
    return (
        not isinstance(key, str)
        or "\\" in key
        or "\x00" in key
        or any(s in ("", ".", "..") for s in key.split("/"))
    )


def _parent(key: str) -> str:
    parent, _, _ = key.rpartition("/")
    return parent


@dataclass(frozen=True)
class ObjectScope:
    """What one erasure owns: every key strictly under each prefix, and each exact key."""

    prefixes: tuple[str, ...] = ()
    keys: tuple[str, ...] = ()

    def listing_prefixes(self) -> frozenset[str]:
        """The prefixes to list: the areas, and the parent of each exact key."""
        return frozenset(self.prefixes) | {_parent(k) for k in self.keys}

    def owns(self, key: str) -> bool:
        return key in self.keys or any(key.startswith(p + "/") for p in self.prefixes)


def tenant_scope(tenant_prefix: str, *, prefixes: Iterable[str] = (), keys: Iterable[str] = ()):
    """An :class:`ObjectScope` whose every prefix and key is strictly under ``tenant_prefix``.

    Raises :class:`InvalidObjectScope` otherwise, before any storage call: a root whose stored
    key left its own tenant prefix is never erased by key."""
    prefixes, keys = tuple(sorted(set(prefixes))), tuple(sorted(set(keys)))
    base = tenant_prefix + "/"
    for value in (*prefixes, *keys):
        if not isinstance(value, str) or not value.startswith(base):
            raise InvalidObjectScope("an object key is outside its tenant prefix")
        segments = value.split("/")
        if any(s in ("", ".", "..") or "\\" in s or "\x00" in s for s in segments):
            raise InvalidObjectScope("an object key has an unsafe segment")
    return ObjectScope(prefixes=prefixes, keys=keys)


# --------------------------------------------------------------------------- #
# The report
# --------------------------------------------------------------------------- #
class ObjectErasureStatus(StrEnum):
    #: No call failed, nothing was refused, and the fresh listings found nothing left.
    COMPLETE = "complete"
    #: Something failed or is left: the caller keeps its retry handle (the rows, or the
    #: organization) and reports it.
    INCOMPLETE = "incomplete"
    #: The store cannot list versions (the adapter before M0-A0): nothing was listed or erased.
    UNAVAILABLE = "unavailable"


@dataclass
class ObjectErasureReport:
    """What one run removed from one scope (or from many, merged), and whether it is complete.

    Counts are of this run only, confirmed by its fresh listing, so a retry counts again only
    what a failed fresh listing hid from the run before it, and the counts summed over runs are
    what was removed except in the cases the module docstring names."""

    status: ObjectErasureStatus = ObjectErasureStatus.COMPLETE
    #: Object keys this run left with no version and no delete marker, of which it removed at
    #: least one entry: an object is counted by the run that removes its last entry, never again
    #: (except in the cases the module docstring names).
    keys: int = 0
    #: Object versions removed, current and noncurrent.
    versions: int = 0
    delete_markers: int = 0
    aborted_uploads: int = 0
    failed_deletions: int = 0
    failed_aborts: int = 0
    failed_listings: int = 0
    #: Entries M0-B refused to act on: a root whose stored key is outside its tenant prefix, or a
    #: listed key with an unsafe segment. Never touched; the scope stays incomplete.
    refused_keys: int = 0
    #: What the fresh listings after the deletions still found in the scope.
    remaining_versions: int = 0
    remaining_delete_markers: int = 0
    remaining_uploads: int = 0

    @property
    def complete(self) -> bool:
        return self.status is ObjectErasureStatus.COMPLETE

    @property
    def problems(self) -> int:
        return (
            self.failed_deletions + self.failed_aborts + self.failed_listings + self.refused_keys
            + self.remaining_versions + self.remaining_delete_markers + self.remaining_uploads
        )

    def settle(self) -> ObjectErasureReport:
        """Set ``status`` from the counts (``unavailable`` stays)."""
        if self.status is not ObjectErasureStatus.UNAVAILABLE:
            self.status = (
                ObjectErasureStatus.INCOMPLETE if self.problems else ObjectErasureStatus.COMPLETE
            )
        return self

    def merge(self, other: ObjectErasureReport) -> None:
        """Add ``other``'s counts; the merged status is the worst of the two."""
        for name in _COUNTS:
            setattr(self, name, getattr(self, name) + getattr(other, name))
        order = (
            ObjectErasureStatus.COMPLETE,
            ObjectErasureStatus.INCOMPLETE,
            ObjectErasureStatus.UNAVAILABLE,
        )
        self.status = max(self.status, other.status, key=order.index)

    def as_dict(self) -> dict[str, Any]:
        return {
            "store": MEDIA_STORE,
            "status": self.status.value,
            "complete": self.complete,
            **{name: getattr(self, name) for name in _COUNTS},
        }


_COUNTS = (
    "keys",
    "versions",
    "delete_markers",
    "aborted_uploads",
    "failed_deletions",
    "failed_aborts",
    "failed_listings",
    "refused_keys",
    "remaining_versions",
    "remaining_delete_markers",
    "remaining_uploads",
)


def unavailable_report() -> ObjectErasureReport:
    return ObjectErasureReport(status=ObjectErasureStatus.UNAVAILABLE)


class ObjectErasureIncompleteError(ObjectStorageUnavailableError):
    """An erasure's object step did not complete, so its database step did not run.

    503, retryable: the caller's transaction is rolled back and a retry erases what is left.
    ``report`` says what this attempt removed and what failed (counts only, never a key)."""

    code = "object_erasure_incomplete"
    default_message = (
        "Stored files could not all be erased, so nothing was deleted. Try again later."
    )

    def __init__(self, report: ObjectErasureReport) -> None:
        super().__init__()
        self.report = report


# --------------------------------------------------------------------------- #
# The engine
# --------------------------------------------------------------------------- #
def _drain(iterator_factory) -> tuple[list, bool]:
    """Everything a listing yields before it ends or raises; ``ok`` is False if it raised."""
    items: list = []
    try:
        for item in iterator_factory():
            items.append(item)
    except Exception:  # a driver error, possibly mid-page: the listing failed
        return items, False
    return items, True


def _covered(prefixes: Iterable[str], key: str) -> bool:
    """Whether ``key`` is under one of ``prefixes`` (literal ``prefix/``, as in S3)."""
    return any(key.startswith(prefix + "/") for prefix in prefixes)


@dataclass
class _Listing:
    versions: dict[tuple[str, str], Any] = field(default_factory=dict)
    uploads: dict[tuple[str, str], Any] = field(default_factory=dict)
    #: The prefixes whose version listing or upload listing raised.
    failed: set[str] = field(default_factory=set)
    #: The prefixes whose version listing, and those whose upload listing, ran to its end: an
    #: entry under one of them that the listing does not show is not in the store.
    full_versions: set[str] = field(default_factory=set)
    full_uploads: set[str] = field(default_factory=set)


def _list(store: VersionedObjectStore, prefixes: Iterable[str]) -> _Listing:
    out = _Listing()
    for prefix in sorted(set(prefixes)):
        versions, ok_versions = _drain(lambda p=prefix: store.list_object_versions(p))
        uploads, ok_uploads = _drain(lambda p=prefix: store.list_multipart_uploads(p))
        if ok_versions:
            out.full_versions.add(prefix)
        if ok_uploads:
            out.full_uploads.add(prefix)
        if not (ok_versions and ok_uploads):
            out.failed.add(prefix)
        for v in versions:
            out.versions.setdefault((v.key, v.version_id), v)
        for u in uploads:
            out.uploads.setdefault((u.key, u.upload_id), u)
    return out


def erase_scopes(
    store: VersionedObjectStore, scopes: Mapping[str, ObjectScope]
) -> dict[str, ObjectErasureReport]:
    """Erase every scope; one report each. Scopes must not overlap (each entry has one owner).

    For each listing prefix the scopes need: list every version and delete marker and every
    in-progress upload; delete each version and marker a scope owns (``delete_version``) and
    abort each upload it owns; then list again and record what is left. A scope whose listing
    failed, or one of whose calls raised, or that still has anything left, is ``incomplete``.
    What it removed is counted against that fresh listing (module docstring)."""
    reports = {name: ObjectErasureReport() for name in scopes}
    exact: dict[str, str] = {}
    areas: dict[str, str] = {}
    for name, scope in scopes.items():
        for key in scope.keys:
            if exact.setdefault(key, name) != name:
                raise ValueError("two erasure scopes own the same key")
        for prefix in scope.prefixes:
            if areas.setdefault(prefix, name) != name:
                raise ValueError("two erasure scopes own the same prefix")
    prefixes = {p for scope in scopes.values() for p in scope.listing_prefixes()}

    def owner_of(key: str) -> str | None:
        """The scope that owns ``key`` exactly, else the one with an area above it (keys are
        literal strings, as in S3: a key is under a prefix when it starts with ``prefix/``)."""
        if key in exact:
            return exact[key]
        parent = _parent(key)
        while parent:
            if parent in areas:
                return areas[parent]
            parent = _parent(parent)
        return None

    def note_failed_listings(listing: _Listing) -> None:
        for name, scope in scopes.items():
            reports[name].failed_listings += len(scope.listing_prefixes() & listing.failed)

    before = _list(store, prefixes)
    note_failed_listings(before)
    # Each entry this run asks the store to remove: its scope, whether it is a delete marker, and
    # whether the call returned. Whether it is gone is decided by the fresh listing below.
    asked: dict[tuple[str, str], tuple[str, bool, bool]] = {}
    asked_uploads: dict[tuple[str, str], tuple[str, bool]] = {}
    for (key, version_id), version in sorted(before.versions.items()):
        name = owner_of(key)
        if name is None:
            continue  # not ours: never deleted, whatever the store returned
        if _unsafe(key):
            reports[name].refused_keys += 1  # ours, but never passed to the store: incomplete
            continue
        returned = True
        try:
            store.delete_version(key, version_id)
        except Exception:
            reports[name].failed_deletions += 1
            returned = False
        asked[(key, version_id)] = (name, bool(version.is_delete_marker), returned)
    for (key, upload_id), upload in sorted(before.uploads.items()):
        name = owner_of(key)
        if name is None:
            continue
        if _unsafe(key):
            reports[name].refused_keys += 1
            continue
        returned = True
        try:
            store.abort_multipart_upload(upload)
        except Exception:
            reports[name].failed_aborts += 1
            returned = False
        asked_uploads[(key, upload_id)] = (name, returned)

    after = _list(store, prefixes)
    note_failed_listings(after)
    for (key, _version_id), version in after.versions.items():
        name = owner_of(key)
        if name is None:
            continue
        if version.is_delete_marker:
            reports[name].remaining_delete_markers += 1
        else:
            reports[name].remaining_versions += 1
    for (key, _upload_id) in after.uploads:
        name = owner_of(key)
        if name is not None:
            reports[name].remaining_uploads += 1

    # Removed: asked for, and not shown by the fresh listing. Where no fresh listing over the
    # entry ran to its end (the run is then incomplete), only a call that returned counts.
    touched: dict[str, set[str]] = {name: set() for name in scopes}
    for (key, version_id), (name, marker, returned) in asked.items():
        if (key, version_id) in after.versions:
            continue  # still there: a failed call, or a call that returned without deleting
        if not (returned or _covered(after.full_versions, key)):
            continue  # its call raised and nothing shows whether it was applied
        if marker:
            reports[name].delete_markers += 1
        else:
            reports[name].versions += 1
        touched[name].add(key)
    for (key, upload_id), (name, returned) in asked_uploads.items():
        if (key, upload_id) in after.uploads:
            continue
        if returned or _covered(after.full_uploads, key):
            reports[name].aborted_uploads += 1

    # An object (key) counts in the run that leaves no version and no delete marker of it: a
    # fresh listing that ran to its end shows none, or, where that listing failed, this run saw
    # every entry of the key in a full first listing and every call on them returned.
    left = {key for key, _version_id in after.versions}
    raised = {key for (key, _v), (_name, _marker, returned) in asked.items() if not returned}
    for name, report in reports.items():
        report.keys = sum(
            1
            for key in touched[name] - left
            if _covered(after.full_versions, key)
            or (_covered(before.full_versions, key) and key not in raised)
        )
        report.settle()
    return reports


def erase_organization_objects(
    organization_id: str, *, object_store: object | None = None
) -> ObjectErasureReport:
    """Erase every object version, delete marker and in-progress multipart upload strictly
    under ``{organization_id}/`` (AE-M0-4). ``object_store=None`` resolves the application's
    store; a store without the four operations gives an ``unavailable`` report."""
    prefix = organization_prefix(organization_id)
    store = resolve_object_store() if object_store is None else object_store
    if not supports_object_erasure(store):
        report = unavailable_report()
    else:
        report = erase_scopes(store, {"organization": ObjectScope(prefixes=(prefix,))})[
            "organization"
        ]
    log_event(
        logger,
        "privacy.objects.organization_erased",
        level=_LEVELS[report.status],
        outcome=_OUTCOMES[report.status],
        organization_id=organization_id,
        **{k: v for k, v in report.as_dict().items() if k != "store"},
    )
    return report


_LEVELS = {
    ObjectErasureStatus.COMPLETE: INFO,
    ObjectErasureStatus.UNAVAILABLE: WARNING,  # the object step did not run
    ObjectErasureStatus.INCOMPLETE: ERROR,
}
_OUTCOMES = {
    ObjectErasureStatus.COMPLETE: "success",
    ObjectErasureStatus.UNAVAILABLE: "skipped",
    ObjectErasureStatus.INCOMPLETE: "failure",
}


__all__ = [
    "MEDIA_STORE",
    "InProgressUpload",
    "InvalidObjectScope",
    "ObjectErasureIncompleteError",
    "ObjectErasureReport",
    "ObjectErasureStatus",
    "ObjectScope",
    "ObjectVersion",
    "VersionedObjectStore",
    "erase_organization_objects",
    "erase_scopes",
    "organization_prefix",
    "require_segment",
    "resolve_object_store",
    "supports_object_erasure",
    "tenant_scope",
    "unavailable_report",
]
