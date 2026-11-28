"""AE-M0-5: the media retention purge, with its stored objects (M0-B's code).

Authored by the M0-A lane's test role (contract section 11), here the independent test author
M0B-T (coordinator ruling M0B-1); the M0-B builder wrote none of it. Written from the CF-7A-0
contract (sections 5, 11 and 13: R-12 as re-specified by ruling CF7A0-3, DATA C2), plan v3's
AE-M0-5 and the operator's instruction of 2026-11-22 ("Show that unset retention leaves data
intact, a configured test policy removes only eligible objects and versions ... A failed storage
deletion must remain visible as incomplete"), against M0-B's public surface:
``app.privacy.retention.sweep`` and ``purge_batch`` with ``object_store=``, ``_ROOTS``, and the
``ClassOutcome`` fields ``status``, ``complete``, ``deleted``, ``kept`` and ``objects``. The
store is the local versioned-storage fixture ``_versioned_store_support.VersionedStore``.

Every test runs on SQLite and on PostgreSQL, with synthetic records:

* **The gate** (``test_the_media_classes_have_real_roots``, exactly as contract section 11
  words it): it fails while ``_ROOTS[MEDIA_UPLOADS]`` lacks ``media_assets`` and
  ``media_objects``, or while a ``media_uploads`` purge leaves a stored object, any version or a
  delete marker; and it fails if a ``media_records`` purge with ``retention_media_records_days``
  shorter than ``retention_media_uploads_days`` removes the rights record of a ``pending`` or
  ``active`` asset or the provenance record of an existing object. ``_ROOTS[MEDIA_RECORDS]`` is
  not required to be non-empty.
* **Unset retention leaves data intact:** with any media duration unset the purge refuses, asks
  nothing of the store, and no row, object, version, delete marker or upload changes.
* **A configured test policy removes only what is eligible:** with test-only durations, the
  eligible assets and upload records go with every version, delete marker and in-progress upload
  of their keys; every other row and object is unchanged, including a live asset's old stored
  object, its provenance and its rights; the counts are exact; a second sweep finds nothing.
* **A failed storage deletion stays visibly incomplete:** its root is kept with all its rows,
  the class reports ``complete: false`` with ``kept`` and an incomplete ``objects`` report, and
  the next sweep completes it, counting only what was left.
* **A root whose stored key left its own tenant is refused and kept** (fix pass FXM0B-1, review
  F-5, RM02): it is never erased by key, the class is incomplete with ``refused_keys`` 1, and
  nothing is asked of the store or changed in the database.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from app.core.config import Settings
from app.privacy import retention
from app.privacy.inventory import RetentionClass
from app.privacy.retention import _ROOTS
from app.tests._privacy_support import (
    BOB,
    CAROL,
    ORG_A,
    ORG_B,
    WS_A,
    WS_A2,
    WS_B,
    PrivacyEnv,
)
from app.tests._versioned_store_support import (
    ENGINES,
    MediaRows,
    VersionedStore,
    asset_area,
    asset_key,
    engine_world,
    media_rows,
    upload_key,
)

UPLOADS, RECORDS = RetentionClass.MEDIA_UPLOADS, RetentionClass.MEDIA_RECORDS
MEDIA = (UPLOADS, RECORDS)


def _settings(uploads: int | None, records: int | None, **more) -> Settings:
    return Settings(
        _env_file=None,
        retention_media_uploads_days=uploads,
        retention_media_records_days=records,
        **more,
    )


#: Test-only durations (D-7 leaves the real ones undecided).
POLICY = _settings(30, 30)


@pytest.fixture(params=ENGINES)
def env(request, tmp_path) -> Iterator[PrivacyEnv]:
    with engine_world(request.param, tmp_path) as e:
        yield e


# --------------------------------------------------------------------------- #
# The world: eligible roots, a live asset with old parts, and objects no root owns
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class World:
    store: VersionedStore
    #: Every key the eligible roots own (exact keys and areas' contents).
    eligible_keys: tuple[str, ...]
    #: Keys of the eligible roots' areas that have no row.
    eligible_areas: tuple[str, ...]
    #: The rows that must go (table -> ids).
    eligible_rows: dict[str, set[str]]


def _world(env: PrivacyEnv) -> World:
    """Rows and objects, counted by hand below.

    Eligible under a 30-day ``media_uploads`` policy (``created_at`` 400 days ago):

    * E1, an old active asset in A's first workspace: its promoted upload record (one version and
      a delete marker left at the upload key), its stored object (three versions), the object's
      provenance and the asset's rights; plus, in the asset's area, a rendition with no row (a
      version and a marker) and an in-progress multipart upload with no row.
    * E2, an old pending asset in A's second workspace: its pending upload record, whose
      multipart upload is still in progress, and its rights.
    * E3, an old active asset of organization B: its stored object (two versions), provenance
      and rights.
    * L1-OLDUP, an old rejected upload record of the live asset L1 (a version and a marker).

    Not eligible: L1 itself (created yesterday) with its recent promoted upload record, its
    **old** stored object (a stored object is never a root of its own) with that object's old
    provenance, and its old rights record; L2, a recent pending asset with its in-progress
    upload; and two keys no row names, in A's asset area and B's upload area.
    """
    old = datetime.now(UTC) - timedelta(days=400)
    new = datetime.now(UTC) - timedelta(days=1)
    rows, store = MediaRows(), VersionedStore()

    rows.asset(ORG_A, WS_A, "m0bt-e1", state="active", at=old, created_by=CAROL)
    e1_up = rows.upload(ORG_A, WS_A, "m0bt-e1-up", "m0bt-e1", state="promoted", at=old)
    e1_obj = rows.stored(ORG_A, WS_A, "m0bt-e1-obj", "m0bt-e1", at=old, parent="m0bt-e1-up")
    rows.rights_record(ORG_A, WS_A, "m0bt-e1", attested_by=CAROL, at=old)
    store.put(e1_up)
    store.delete(e1_up)
    for _ in range(3):
        store.put(e1_obj)
    e1_stray = asset_key(ORG_A, WS_A, "m0bt-e1", "m0bt-e1-rendition")
    store.put(e1_stray)
    store.delete(e1_stray)
    e1_next = asset_key(ORG_A, WS_A, "m0bt-e1", "m0bt-e1-next")
    store.start_upload(e1_next)

    rows.asset(ORG_A, WS_A2, "m0bt-e2", state="pending", at=old, created_by=CAROL)
    e2_up = rows.upload(ORG_A, WS_A2, "m0bt-e2-up", "m0bt-e2", state="upload_pending", at=old)
    rows.rights_record(ORG_A, WS_A2, "m0bt-e2", attested_by=CAROL, at=old)
    store.start_upload(e2_up)

    rows.asset(ORG_B, WS_B, "m0bt-e3", state="active", at=old, created_by=BOB)
    e3_obj = rows.stored(ORG_B, WS_B, "m0bt-e3-obj", "m0bt-e3", at=old)
    rows.rights_record(ORG_B, WS_B, "m0bt-e3", attested_by=BOB, at=old)
    store.put(e3_obj)
    store.put(e3_obj)

    rows.asset(ORG_A, WS_A, "m0bt-l1", state="active", at=new, created_by=CAROL)
    l1_oldup = rows.upload(ORG_A, WS_A, "m0bt-l1-oldup", "m0bt-l1", state="rejected", at=old)
    l1_up = rows.upload(ORG_A, WS_A, "m0bt-l1-up", "m0bt-l1", state="promoted", at=new)
    l1_obj = rows.stored(ORG_A, WS_A, "m0bt-l1-obj", "m0bt-l1", at=old, parent="m0bt-l1-up")
    rows.rights_record(ORG_A, WS_A, "m0bt-l1", attested_by=CAROL, at=old)
    store.put(l1_oldup)
    store.delete(l1_oldup)
    store.put(l1_up)
    store.put(l1_obj)
    store.put(l1_obj)

    rows.asset(ORG_A, WS_A, "m0bt-l2", state="pending", at=new, created_by=CAROL)
    l2_up = rows.upload(ORG_A, WS_A, "m0bt-l2-up", "m0bt-l2", state="upload_pending", at=new)
    rows.rights_record(ORG_A, WS_A, "m0bt-l2", attested_by=CAROL, at=new)
    store.start_upload(l2_up)

    store.put(asset_key(ORG_A, WS_A, "m0bt-gone", "m0bt-gone-obj"))  # no row: no root's
    stray_b = upload_key(ORG_B, WS_B, "m0bt-b-stray")
    store.put(stray_b)
    store.delete(stray_b)

    rows.insert(env.engine)
    return World(
        store=store,
        eligible_keys=(e1_up, e1_obj, e1_stray, e1_next, e2_up, e3_obj, l1_oldup),
        eligible_areas=(
            asset_area(ORG_A, WS_A, "m0bt-e1"),
            asset_area(ORG_A, WS_A2, "m0bt-e2"),
            asset_area(ORG_B, WS_B, "m0bt-e3"),
        ),
        eligible_rows={
            "media_assets": {"m0bt-e1", "m0bt-e2", "m0bt-e3"},
            "media_objects": {
                "m0bt-e1-up", "m0bt-e1-obj", "m0bt-e2-up", "m0bt-e3-obj", "m0bt-l1-oldup",
            },
            "media_provenance": {"m0bt-e1-obj-pv", "m0bt-e3-obj-pv"},
            "media_rights": {"m0bt-e1-rt", "m0bt-e2-rt", "m0bt-e3-rt"},
        },
    )


#: The roots a 30-day policy selects: the three assets and the three old upload records.
ROOTS = 6
#: What the eligible roots hold, counted by hand: 5 keys (E1's upload key, object and rendition,
#: E3's object, L1-OLDUP); 8 versions (1 + 3 + 1 + 2 + 1); 3 delete markers; 2 in-progress
#: uploads (E1's next, E2's upload).
ELIGIBLE = {"keys": 5, "versions": 8, "delete_markers": 3, "aborted_uploads": 2}
DELETED = {"media_assets": 3, "media_objects": 5, "media_provenance": 2, "media_rights": 3}
NO_FAILURES = {
    "failed_deletions": 0, "failed_aborts": 0, "failed_listings": 0, "refused_keys": 0,
    "remaining_versions": 0, "remaining_delete_markers": 0, "remaining_uploads": 0,
}


def _removed(objects: dict | None) -> dict[str, int]:
    assert objects is not None
    return {k: objects[k] for k in ELIGIBLE}


def _failures(objects: dict) -> dict[str, int]:
    return {k: objects[k] for k in NO_FAILURES}


def _ineligible_store(world: World) -> tuple:
    """The store with every eligible key removed from the picture: what must not change."""
    store = world.store
    versions, uploads = store.snapshot()
    owned = set(world.eligible_keys)

    def eligible(key: str) -> bool:
        return key in owned or any(key.startswith(area + "/") for area in world.eligible_areas)

    return (
        tuple(v for v in versions if not eligible(v[0])),
        tuple(u for u in uploads if not eligible(u[0])),
    )


def _ineligible_rows(env: PrivacyEnv, world: World) -> dict[str, dict]:
    return {
        table: {i: r for i, r in rows.items() if i not in world.eligible_rows[table]}
        for table, rows in media_rows(env).items()
    }


def _eligible_left(world: World) -> list[tuple[str, tuple[int, int], int]]:
    store = world.store
    left = [(k, store.holds(k), store.uploads_of(k)) for k in world.eligible_keys]
    left = [entry for entry in left if entry[1] != (0, 0) or entry[2]]
    for area in world.eligible_areas:
        if not store.tally(area).empty:
            left.append((area, (store.tally(area).versions, store.tally(area).delete_markers),
                         store.tally(area).uploads))
    return left


# --------------------------------------------------------------------------- #
# The gate (contract section 11; ruling CF7A0-2, R-12, re-specified by CF7A0-3, DATA C2)
# --------------------------------------------------------------------------- #
def test_the_media_classes_have_real_roots(env):
    # 1. ``_ROOTS[MEDIA_UPLOADS]`` has ``media_assets`` and ``media_objects``.
    rooted = {model.__tablename__ for model, _predicate in _ROOTS[UPLOADS]}
    assert {"media_assets", "media_objects"} <= rooted, rooted

    # 2. A ``media_records`` purge with ``retention_media_records_days`` shorter than
    #    ``retention_media_uploads_days`` removes neither the rights record of a pending or active
    #    asset nor the provenance record of an existing object (nothing here is past the uploads
    #    duration; every record is far past the records duration).
    world = _world(env)
    before_rows, before_store = media_rows(env), world.store.snapshot()
    shorter = _settings(36500, 1)
    for classes in ((RECORDS,), MEDIA):
        outcomes = retention.sweep(
            env.session, classes=classes, settings=shorter, object_store=world.store
        )
        assert all(o.status == "purged" for o in outcomes), outcomes
        after = media_rows(env)
        assert after["media_rights"] == before_rows["media_rights"]
        assert after["media_provenance"] == before_rows["media_provenance"]
        assert after == before_rows
        assert world.store.snapshot() == before_store
    states = {r["state"] for r in before_rows["media_assets"].values()}
    assert {"pending", "active"} <= states

    # 3. A ``media_uploads`` purge leaves no stored object, no version and no delete marker of
    #    what it purges (the stored objects with noncurrent versions, the keys with markers).
    (outcome,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=POLICY, object_store=world.store
    )
    assert (outcome.status, outcome.complete) == ("purged", True), outcome
    assert _eligible_left(world) == []
    after = media_rows(env)
    for table, ids in world.eligible_rows.items():
        assert not ids & set(after[table]), table
    assert not {i for i, r in after["media_objects"].items() if r["asset_id"] in
                world.eligible_rows["media_assets"]}


# --------------------------------------------------------------------------- #
# Unset retention leaves data intact (AE-M0-5)
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "durations",
    [(None, None), (30, None), (None, 30)],
    ids=["both_unset", "records_unset", "uploads_unset"],
)
def test_with_any_media_duration_unset_the_purge_refuses_and_nothing_changes(env, durations):
    world = _world(env)
    settings = _settings(*durations)
    before_db, before_store = env.snapshot(), world.store.snapshot()

    for dry_run in (True, False):
        outcomes = retention.sweep(
            env.session, classes=MEDIA, settings=settings, dry_run=dry_run,
            object_store=world.store,
        )
        assert [(o.data_class, o.status, o.deleted, o.kept, o.objects) for o in outcomes] == [
            ("media_uploads", "policy_unset", {}, {}, None),
            ("media_records", "policy_unset", {}, {}, None),
        ]
    # The full sweep (every class) refuses the media classes the same way.
    swept = retention.sweep(env.session, settings=settings, object_store=world.store)
    media = [(o.data_class, o.status) for o in swept if o.data_class in {c.value for c in MEDIA}]
    assert media == [("media_uploads", "policy_unset"), ("media_records", "policy_unset")]

    assert world.store.calls == []  # the purge refused to run: nothing was asked of the store
    assert world.store.snapshot() == before_store
    assert env.snapshot() == before_db  # no row of any table changed


# --------------------------------------------------------------------------- #
# A configured test policy removes only eligible objects and versions
# --------------------------------------------------------------------------- #
def test_a_configured_policy_removes_only_eligible_assets_objects_and_versions(env):
    world = _world(env)
    before_rows = _ineligible_rows(env, world)
    before_store = _ineligible_store(world)
    everything = world.store.snapshot()

    (dry,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=POLICY, dry_run=True, object_store=world.store
    )
    assert (dry.status, dry.candidates, dry.deleted) == ("dry_run", ROOTS, {})
    assert world.store.calls == [] and world.store.snapshot() == everything

    uploads, records = retention.sweep(
        env.session, classes=MEDIA, settings=POLICY, object_store=world.store
    )

    assert (uploads.status, uploads.complete, uploads.kept) == ("purged", True, {})
    assert uploads.deleted == DELETED
    assert uploads.objects["status"] == "complete" and uploads.objects["complete"] is True
    assert _removed(uploads.objects) == ELIGIBLE
    assert _failures(uploads.objects) == NO_FAILURES
    assert (records.status, records.complete, records.deleted) == ("purged", True, {})
    # Every eligible key is empty: no object, version, delete marker or upload is left ...
    assert _eligible_left(world) == []
    # ... and everything else is exactly as it was: rows, objects, versions, markers, uploads.
    assert _ineligible_rows(env, world) == before_rows
    assert _ineligible_store(world) == before_store
    left = media_rows(env)
    assert set(left["media_assets"]) == {"m0bt-l1", "m0bt-l2"}
    assert left["media_objects"]["m0bt-l1-obj"]["state"] == "stored"  # a live asset's old object
    assert set(left["media_provenance"]) == {"m0bt-l1-obj-pv"}
    assert set(left["media_rights"]) == {"m0bt-l1-rt", "m0bt-l2-rt"}

    # A second sweep selects nothing and counts nothing.
    settled = world.store.snapshot()
    (again,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=POLICY, object_store=world.store
    )
    assert (again.status, again.complete, again.deleted, again.kept) == ("purged", True, {}, {})
    assert _removed(again.objects) == dict.fromkeys(ELIGIBLE, 0)
    assert world.store.snapshot() == settled


def test_a_purge_in_small_batches_removes_the_same_and_counts_it_once(env):
    world = _world(env)
    before_rows = _ineligible_rows(env, world)
    before_store = _ineligible_store(world)
    small = _settings(30, 30, retention_purge_batch_size=1)

    (outcome,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=small, object_store=world.store
    )

    assert (outcome.status, outcome.complete, outcome.kept) == ("purged", True, {})
    assert outcome.batches > 1
    assert outcome.deleted == DELETED
    assert _removed(outcome.objects) == ELIGIBLE
    assert _eligible_left(world) == []
    assert _ineligible_rows(env, world) == before_rows
    assert _ineligible_store(world) == before_store


# --------------------------------------------------------------------------- #
# A failed storage deletion stays visible as incomplete; the next sweep completes it
# --------------------------------------------------------------------------- #
E1_OBJ = asset_key(ORG_A, WS_A, "m0bt-e1", "m0bt-e1-obj")
E1_NEXT = asset_key(ORG_A, WS_A, "m0bt-e1", "m0bt-e1-next")
E1_AREA = asset_area(ORG_A, WS_A, "m0bt-e1")
E1_ROWS = {
    "media_assets": {"m0bt-e1"},
    "media_objects": {"m0bt-e1-up", "m0bt-e1-obj"},
    "media_provenance": {"m0bt-e1-obj-pv"},
    "media_rights": {"m0bt-e1-rt"},
}
#: E1's roots kept: the asset, and its upload record (itself a root, which goes with its asset).
E1_KEPT = {"media_assets": 1, "media_objects": 1}


@dataclass(frozen=True)
class Fault:
    name: str
    inject: object  # (store) -> None
    #: What the first and the second sweep removed (keys, versions, markers, aborted uploads).
    first: dict[str, int]
    second: dict[str, int]
    counter: str  # the ``objects`` field that is non-zero after the first sweep


def _counts(keys, versions, markers, aborted) -> dict[str, int]:
    return dict(zip(ELIGIBLE, (keys, versions, markers, aborted), strict=True))


FAULTS = (
    # E1's stored object refuses to go (all three versions).
    Fault("delete_raises", lambda s: s.fail_deletes(E1_OBJ),
          _counts(4, 5, 3, 2), _counts(1, 3, 0, 0), "failed_deletions"),
    # E1's in-progress upload refuses to be aborted.
    Fault("abort_raises", lambda s: s.fail_aborts(E1_NEXT),
          _counts(5, 8, 3, 1), _counts(0, 0, 0, 1), "failed_aborts"),
    # The version listing of E1's area fails on every call: its object and rendition stay.
    Fault("listing_fails", lambda s: s.fail_listing("versions", prefix=E1_AREA),
          _counts(3, 4, 2, 2), _counts(2, 4, 1, 0), "failed_listings"),
)


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f.name)
def test_a_failed_storage_deletion_keeps_its_root_visibly_incomplete_until_the_next_sweep(
    env, fault: Fault
):
    world = _world(env)
    e1_before = {t: {i: r for i, r in rows.items() if i in E1_ROWS[t]}
                 for t, rows in media_rows(env).items()}
    before_rows = _ineligible_rows(env, world)
    before_store = _ineligible_store(world)
    fault.inject(world.store)

    (first,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=POLICY, object_store=world.store
    )

    # Visible as incomplete: not complete, the root kept and counted, the objects incomplete.
    assert (first.status, first.complete, first.kept) == ("purged", False, E1_KEPT), first
    assert first.objects["status"] == "incomplete" and first.objects["complete"] is False
    assert first.objects[fault.counter] >= 1, first.objects
    assert _removed(first.objects) == fault.first, first.objects
    # E1 keeps every row, unchanged: the rows are the handle the next sweep retries from.
    rows = media_rows(env)
    assert {t: {i: r for i, r in rows[t].items() if i in E1_ROWS[t]} for t in rows} == e1_before
    # The other eligible roots are purged; nothing ineligible changed.
    for table, ids in world.eligible_rows.items():
        assert not (ids - E1_ROWS[table]) & set(rows[table]), table
    assert _ineligible_rows(env, world) == before_rows
    assert _ineligible_store(world) == before_store
    assert _eligible_left(world) and all(
        key == E1_AREA or key.startswith(E1_AREA + "/") for key, _, _ in _eligible_left(world)
    )

    world.store.heal()
    (second,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=POLICY, object_store=world.store
    )

    assert (second.status, second.complete, second.kept) == ("purged", True, {}), second
    assert second.objects["status"] == "complete"
    assert _failures(second.objects) == NO_FAILURES
    assert _removed(second.objects) == fault.second  # only what was left
    assert {k: fault.first[k] + fault.second[k] for k in ELIGIBLE} == ELIGIBLE
    assert {k: first.deleted.get(k, 0) + second.deleted.get(k, 0) for k in DELETED} == DELETED
    assert _eligible_left(world) == []
    rows = media_rows(env)
    assert not any(ids & set(rows[t]) for t, ids in E1_ROWS.items())
    assert _ineligible_rows(env, world) == before_rows
    assert _ineligible_store(world) == before_store


def test_a_deletion_that_silently_does_nothing_keeps_its_root_until_the_next_sweep(env):
    """Completeness is measured, not assumed: a delete that returns without deleting leaves the
    object, so the root is kept and the class is incomplete."""
    world = _world(env)
    world.store.silence_deletes(E1_OBJ)

    (first,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=POLICY, object_store=world.store
    )

    assert (first.complete, first.kept) == (False, E1_KEPT)
    assert first.objects["status"] == "incomplete"
    assert first.objects["remaining_versions"] == 3
    assert world.store.holds(E1_OBJ) == (3, 0)
    assert "m0bt-e1" in media_rows(env)["media_assets"]

    world.store.heal()
    (second,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=POLICY, object_store=world.store
    )

    assert (second.complete, second.kept) == (True, {})
    assert _eligible_left(world) == []
    assert "m0bt-e1" not in media_rows(env)["media_assets"]


def test_a_store_that_cannot_erase_versions_keeps_every_root(env):
    """No version-aware store (the adapter before M0-A0): no root may lose its rows, since its
    objects cannot be shown gone."""
    world = _world(env)
    before_db, before_store = env.snapshot(), world.store.snapshot()

    (outcome,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=POLICY, object_store=object()
    )

    assert (outcome.status, outcome.complete, outcome.deleted) == ("purged", False, {})
    assert outcome.kept == {"media_assets": 3, "media_objects": 3}
    assert outcome.objects["status"] == "unavailable" and outcome.objects["complete"] is False
    assert env.snapshot() == before_db
    assert world.store.snapshot() == before_store


def test_a_root_whose_stored_key_is_outside_its_tenant_is_refused_and_kept(env):
    """Review F-5, RM02. Contract section 8: every key of a tenant's media is under
    ``{organization_id}/{workspace_id}/media/``. An old asset of (A, WS_A) whose stored object's
    row names organization B (the schema allows it: its key is under ``{B}/{WS_A}/media/``) is a
    root whose key left its tenant: it is refused, never erased by key, and kept with its rows."""
    old = datetime.now(UTC) - timedelta(days=400)
    rows, store = MediaRows(), VersionedStore()
    rows.asset(ORG_A, WS_A, "m0bt-x1", state="active", at=old, created_by=CAROL)
    outside = rows.stored(ORG_B, WS_A, "m0bt-x1-obj", "m0bt-x1", at=old)
    rows.rights_record(ORG_A, WS_A, "m0bt-x1", attested_by=CAROL, at=old)
    rows.insert(env.engine)
    assert outside.startswith(f"{ORG_B}/{WS_A}/media/")
    store.put(outside)
    store.put(outside)
    store.put(asset_key(ORG_A, WS_A, "m0bt-x1", "m0bt-x1-rendition"))  # in the asset's own area
    before_db, before_store = env.snapshot(), store.snapshot()

    (outcome,) = retention.sweep(
        env.session, classes=(UPLOADS,), settings=POLICY, object_store=store
    )

    assert (outcome.status, outcome.complete, outcome.deleted, outcome.kept) == (
        "purged", False, {}, {"media_assets": 1}
    ), outcome
    assert outcome.objects["status"] == "incomplete" and outcome.objects["complete"] is False
    assert outcome.objects["refused_keys"] == 1, outcome.objects
    assert store.mutating_calls() == []
    assert store.snapshot() == before_store
    assert env.snapshot() == before_db  # the asset, its object, provenance and rights are kept
