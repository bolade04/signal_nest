"""AE-M0-4: organization erasure empties the media store, every version included (M0-B's code).

Authored by the M0-A lane's test role (contract section 11), here the independent test author
M0B-T (coordinator ruling M0B-1); the M0-B builder wrote none of it. Written from the CF-7A-0
contract (sections 6 and 8), plan v3's AE-M0-4 and the operator's instruction of 2026-11-22
("erasure is tenant-scoped and safe to retry. A failed storage deletion must remain visible as
incomplete"), against M0-B's public surface: ``app.privacy.object_erasure``,
``app.privacy.erasure`` (``object_store=``) and the deletion routes. The store is the local
versioned-storage fixture ``_versioned_store_support.VersionedStore`` (S3 semantics).

What these tests show, with synthetic records, on SQLite and on PostgreSQL where rows are
involved:

* After organization A's erasure, a version-aware listing of ``{A}/`` finds no object, no
  noncurrent version and no delete marker, and no in-progress multipart upload is left: each was
  aborted. The report counts exactly the keys, versions, delete markers and aborted uploads A
  held.
* Organizations B and C, a sibling prefix that merely starts with A's id, and a key equal to A's
  id are untouched (count before equals count after, bytes included); no call names any of them,
  even when the store over-returns.
* A failed deletion, abort or listing is never reported complete. Through the route it is a 503
  ``object_erasure_incomplete`` that writes nothing in the database; the next run completes it,
  and the runs together count each removal exactly once.
* The attester rule (R-5): after an account deletion, the rights record keeps its content and
  loses the identity; the organizations the account does not delete keep their objects.

Two tests are marked ``xfail(strict=True)``. Each pins a defect in the report's counts that the
test author reported to the coordinator and did not fix (no product code is changed here):
D-M0BT-1, a ``delete_version`` that returns without deleting is counted as removed; D-M0BT-2, a
key whose entries are removed over two runs is counted as a removed object by both. A fix turns
each into an unexpected pass, which fails, so the marker cannot outlive the defect.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import app.infra.storage as infra_storage
from app.privacy.erasure import erase_organization
from app.privacy.object_erasure import (
    ObjectErasureIncompleteError,
    ObjectErasureReport,
    ObjectErasureStatus,
    VersionedObjectStore,
    erase_organization_objects,
    resolve_object_store,
)
from app.privacy.verification import verify_organization_objects_erased, verify_subject_erased
from app.tests._auth2_support import active_rate_limiter
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    EMAIL,
    NAME,
    ORG_A,
    ORG_B,
    ORG_C,
    PASSWORD,
    WS_A,
    WS_A2,
    WS_B,
    WS_C,
    PrivacyEnv,
)
from app.tests._versioned_store_support import (
    ENGINES,
    MediaRows,
    Tally,
    VersionedStore,
    asset_key,
    engine_world,
    media_rows,
    upload_key,
)

#: A prefix that literally starts with A's id but is another organization's.
SIBLING = f"{ORG_A}0"

# Organization A's holdings: two workspaces, noncurrent versions, delete markers (one key holds
# only a marker), a key with no row, and two in-progress multipart uploads (one with no row).
K1 = asset_key(ORG_A, WS_A, "m0bt-ea1", "m0bt-ea1-obj")  # 3 versions
K2 = upload_key(ORG_A, WS_A, "m0bt-ea1-up")  # 1 version + 1 delete marker
K3 = asset_key(ORG_A, WS_A2, "m0bt-ea2", "m0bt-ea2-obj")  # 1 version
K4 = upload_key(ORG_A, WS_A2, "m0bt-ea2-up")  # 1 delete marker only
K5 = asset_key(ORG_A, WS_A, "m0bt-ea1", "m0bt-ea1-stray")  # 2 versions + 1 marker; no row
U1 = upload_key(ORG_A, WS_A, "m0bt-ea3-up")  # in-progress upload; no row
U2 = asset_key(ORG_A, WS_A2, "m0bt-ea2", "m0bt-ea2-next")  # in-progress upload

#: What A holds, counted by hand: 5 keys; 7 versions (2 current, 5 noncurrent); 3 delete
#: markers; 2 in-progress uploads.
A_HOLDS = Tally(keys=5, versions=7, current=2, noncurrent=5, delete_markers=3, uploads=2)

KB1 = asset_key(ORG_B, WS_B, "m0bt-eb1", "m0bt-eb1-obj")
KB2 = upload_key(ORG_B, WS_B, "m0bt-eb1-up")
KC1 = asset_key(ORG_C, WS_C, "m0bt-ec1", "m0bt-ec1-obj")
KS1 = f"{SIBLING}/{WS_A}/media/assets/m0bt-es1/m0bt-es1-obj"


def _removed(report: ObjectErasureReport) -> tuple[int, int, int, int]:
    return (report.keys, report.versions, report.delete_markers, report.aborted_uploads)


def _failures(report: ObjectErasureReport) -> tuple[int, ...]:
    return (
        report.failed_deletions, report.failed_aborts, report.failed_listings,
        report.refused_keys, report.remaining_versions, report.remaining_delete_markers,
        report.remaining_uploads,
    )


A_REMOVED = (A_HOLDS.keys, A_HOLDS.versions, A_HOLDS.delete_markers, A_HOLDS.uploads)


def _summed(*reports: ObjectErasureReport) -> tuple[int, int, int, int]:
    """What several runs removed together."""
    return tuple(sum(column) for column in zip(*map(_removed, reports), strict=True))

NO_FAILURES = (0, 0, 0, 0, 0, 0, 0)


def _store() -> VersionedStore:
    """The store as the world holds it: A as above, B, C, a sibling and a key equal to A's id."""
    store = VersionedStore()
    for _ in range(3):
        store.put(K1)
    store.put(K2)
    store.delete(K2)
    store.put(K3)
    store.delete(K4)
    store.put(K5)
    store.put(K5)
    store.delete(K5)
    store.start_upload(U1)
    store.start_upload(U2)
    # Organization B: a noncurrent version and a marker on top, and an in-progress upload.
    store.put(KB1)
    store.put(KB1)
    store.delete(KB1)
    store.start_upload(KB2)
    # Organization C, the sibling prefix, and a key that is exactly A's id (not under "A/").
    store.put(KC1)
    store.put(KS1)
    store.start_upload(f"{SIBLING}/{WS_A}/media/uploads/m0bt-es1-up")
    store.put(ORG_A)
    assert store.tally(ORG_A) == A_HOLDS
    return store


def _outside_a(store: VersionedStore) -> tuple:
    return store.snapshot(outside=ORG_A)


def _only_a_was_named(store: VersionedStore) -> None:
    """No mutating call named a key outside ``{A}/``, and no listing asked for a wider prefix."""
    assert all(key.startswith(ORG_A + "/") for key in store.keys_acted_on()), store.calls
    for call in store.calls:
        if call[0].startswith("list_"):
            assert call[1] == ORG_A or call[1].startswith(ORG_A + "/"), call


# --------------------------------------------------------------------------- #
# The fixture itself: a versioned bucket, so a test cannot pass against a weaker store
# --------------------------------------------------------------------------- #
def test_the_local_store_behaves_as_a_versioned_bucket():
    store = VersionedStore()
    assert isinstance(store, VersionedObjectStore)  # M0-B's protocol, checked at run time
    v1, v2 = store.put("o/w/k"), store.put("o/w/k")
    marker = store.delete("o/w/k")  # a plain delete only adds a marker on top
    assert store.holds("o/w/k") == (2, 1)
    listed = [(e.version_id, e.is_latest, e.is_delete_marker)
              for e in store.list_object_versions("o")]
    assert listed == [(marker, True, True), (v2, False, False), (v1, False, False)]
    store.delete_version("o/w/k", marker)  # exactly that entry, permanently, no new marker
    assert store.holds("o/w/k") == (2, 0)
    assert [e.is_latest for e in store.list_object_versions("o")] == [True, False]
    store.delete_version("o/w/k", marker)  # already gone: not an error
    store.put("o0/w/k")
    store.put("o")
    assert {e.key for e in store.list_object_versions("o")} == {"o/w/k"}  # strictly under "o/"
    store.start_upload("o/w/u")
    (upload,) = store.list_multipart_uploads("o")
    store.abort_multipart_upload(upload)
    store.abort_multipart_upload(upload)  # already aborted: not an error
    assert list(store.list_multipart_uploads("o")) == []
    with pytest.raises(ValueError):
        store.list_object_versions("o/")


# --------------------------------------------------------------------------- #
# The erasure itself (no database)
# --------------------------------------------------------------------------- #
def test_erasing_organization_a_leaves_no_object_version_marker_or_upload_under_its_prefix():
    store = _store()

    report = erase_organization_objects(ORG_A, object_store=store)

    assert store.tally(ORG_A).empty, store.snapshot(under=ORG_A)
    assert report.status is ObjectErasureStatus.COMPLETE
    assert report.complete
    _only_a_was_named(store)


def test_the_report_counts_exactly_what_organization_a_held():
    store = _store()

    report = erase_organization_objects(ORG_A, object_store=store)

    assert _removed(report) == A_REMOVED
    assert _failures(report) == NO_FAILURES
    as_dict = report.as_dict()
    assert (as_dict["status"], as_dict["complete"]) == ("complete", True)
    assert (as_dict["keys"], as_dict["versions"], as_dict["delete_markers"],
            as_dict["aborted_uploads"]) == A_REMOVED
    # A second run finds nothing, removes nothing and is complete: erasure is idempotent.
    again = erase_organization_objects(ORG_A, object_store=store)
    assert again.complete and _removed(again) == (0, 0, 0, 0)


def test_organizations_b_c_and_a_sibling_prefix_are_untouched():
    store = _store()
    before = _outside_a(store)
    tallies = {org: store.tally(org) for org in (ORG_B, ORG_C, SIBLING)}

    erase_organization_objects(ORG_A, object_store=store)

    assert _outside_a(store) == before  # every version, marker and upload, bytes included
    assert {org: store.tally(org) for org in (ORG_B, ORG_C, SIBLING)} == tallies
    assert store.holds(ORG_A) == (1, 0)  # a key equal to A's id is not under "A/"
    _only_a_was_named(store)


def test_a_store_that_over_returns_never_leads_to_another_tenants_deletion():
    """A misbehaving store also returns B's, C's, the sibling's and the bare key's entries for a
    listing of A. None of them is A's: none is deleted or aborted, none is counted."""
    store = _store()
    before = _outside_a(store)
    store.over_return(ORG_B, ORG_C, ORG_A)  # ORG_A literally: the sibling and the bare key too

    report = erase_organization_objects(ORG_A, object_store=store)

    assert _outside_a(store) == before
    assert store.tally(ORG_A).empty
    assert report.complete and _removed(report) == A_REMOVED
    assert all(key.startswith(ORG_A + "/") for key in store.keys_acted_on())


def test_a_key_with_an_unsafe_segment_under_a_is_never_handed_to_the_store():
    """A key literally under ``{A}/`` whose segments climb into B (a filesystem store would
    resolve it there) is A's to account for, but the erasure must not pass it to the store: the
    report stays incomplete, B is untouched, and the verifier still sees the key."""
    store = _store()
    climbing = f"{ORG_A}/{WS_A}/media/assets/../../../{ORG_B}/{WS_B}/media/assets/x/y"
    store.put(climbing)
    before = _outside_a(store)

    report = erase_organization_objects(ORG_A, object_store=store)

    assert not report.complete
    assert report.status is ObjectErasureStatus.INCOMPLETE
    assert report.refused_keys >= 1
    assert climbing not in store.keys_acted_on()
    assert store.holds(climbing) == (1, 0)
    assert _outside_a(store) == before
    check = verify_organization_objects_erased(store, organization_id=ORG_A)
    assert not check.verified and check.objects == 1


# --------------------------------------------------------------------------- #
# Failures stay visible, and a retry completes them without double counting
# --------------------------------------------------------------------------- #
def test_a_failed_deletion_is_incomplete_and_the_retry_completes_with_exact_counts():
    store = _store()
    store.fail_deletes(K1)  # every version of K1 refuses to go

    first = erase_organization_objects(ORG_A, object_store=store)

    assert first.status is ObjectErasureStatus.INCOMPLETE and not first.complete
    assert first.as_dict()["complete"] is False
    assert first.failed_deletions >= 1
    assert first.remaining_versions == 3  # the fresh listing still finds K1's three versions
    assert (first.remaining_delete_markers, first.remaining_uploads) == (0, 0)
    assert _removed(first) == (4, 4, 3, 2)  # everything but K1
    assert store.tally(ORG_A) == Tally(keys=1, versions=3, current=1, noncurrent=2)

    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert second.complete and _failures(second) == NO_FAILURES
    assert _removed(second) == (1, 3, 0, 0)  # only what was left
    assert _summed(first, second) == A_REMOVED
    assert store.tally(ORG_A).empty


@dataclass(frozen=True)
class Fault:
    name: str
    inject: object  # (store) -> None
    #: What the first run removed (keys, versions, markers, aborted uploads).
    first: tuple[int, int, int, int] | None
    counter: str  # the report field that must be non-zero after the first run
    #: The first run removes some, not all, of one key's entries (D-M0BT-2's case).
    splits_a_key: bool = False


FAULTS = (
    Fault("delete_raises", lambda s: s.fail_deletes(K5), (4, 5, 2, 2), "failed_deletions"),
    Fault("abort_raises", lambda s: s.fail_aborts(U2), (5, 7, 3, 1), "failed_aborts"),
    # The version listing fails before its first entry, on every call: nothing is known, so
    # nothing may be reported complete. The uploads are still listed and aborted.
    Fault("versions_listing_fails", lambda s: s.fail_listing("versions"), (0, 0, 0, 2),
          "failed_listings"),
    # The first version listing fails after 4 entries (a later page): those are removed.
    Fault("versions_listing_fails_midway", lambda s: s.fail_listing("versions", call=1, after=4),
          None, "failed_listings", splits_a_key=True),
    Fault("uploads_listing_fails", lambda s: s.fail_listing("uploads"), (5, 7, 3, 0),
          "failed_listings"),
    # Everything is deleted, but the fresh listing that proves it fails.
    Fault("fresh_listing_fails", lambda s: s.fail_listing("versions", call=2), A_REMOVED,
          "failed_listings"),
)


@pytest.mark.parametrize("fault", FAULTS, ids=lambda f: f.name)
def test_no_failed_call_is_ever_reported_complete_and_the_retry_completes_it(fault: Fault):
    store = _store()
    before = _outside_a(store)
    fault.inject(store)

    first = erase_organization_objects(ORG_A, object_store=store)

    assert first.status is ObjectErasureStatus.INCOMPLETE, first.as_dict()
    assert not first.complete
    assert getattr(first, fault.counter) >= 1, first.as_dict()
    if fault.first is not None:
        assert _removed(first) == fault.first, first.as_dict()

    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert second.complete, second.as_dict()
    assert store.tally(ORG_A).empty
    # Each removal is counted once over both runs: every version, delete marker and upload, and
    # every object (key) unless one key's entries were split between the runs (D-M0BT-2).
    assert _summed(first, second)[1:] == A_REMOVED[1:]
    if not fault.splits_a_key:
        assert _summed(first, second) == A_REMOVED
    assert _outside_a(store) == before
    assert verify_organization_objects_erased(store, organization_id=ORG_A).verified


def test_a_deletion_that_silently_does_nothing_is_never_reported_complete():
    """Completeness is measured by a fresh listing, not assumed from a call that returned."""
    store = _store()
    store.silence_deletes(K1)

    first = erase_organization_objects(ORG_A, object_store=store)

    assert first.status is ObjectErasureStatus.INCOMPLETE and not first.complete
    assert first.remaining_versions == 3
    assert store.holds(K1) == (3, 0)

    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert second.complete
    assert store.tally(ORG_A).empty


@pytest.mark.xfail(
    strict=True,
    reason=(
        "D-M0BT-1 (reported, not fixed by the test author): erase_scopes counts a version as "
        "removed when delete_version returns, so a delete that silently does nothing is counted "
        "as removed and counted again by the retry that really removes it"
    ),
)
def test_a_deletion_that_silently_does_nothing_is_not_counted_as_removed():
    """The report counts what was removed (AE-M0-4; STORAGE-INTERFACE section 3.2: "versions:
    object versions this run removed"). K1's three versions survive the first run, so it removed
    4 keys' worth (4 versions, 3 markers, 2 uploads), and the retry counts K1's three once."""
    store = _store()
    store.silence_deletes(K1)

    first = erase_organization_objects(ORG_A, object_store=store)
    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert _removed(first) == (4, 4, 3, 2), first.as_dict()
    assert _summed(first, second) == A_REMOVED


@pytest.mark.xfail(
    strict=True,
    reason=(
        "D-M0BT-2 (reported, not fixed by the test author): ObjectErasureReport.keys counts every "
        "key a run removed at least one entry of, so a key whose entries are removed over two "
        "runs is counted as a removed object by both"
    ),
)
def test_an_object_erased_over_two_runs_is_counted_as_removed_once():
    """AE-M0-4: the report counts the objects removed. The first listing fails after K1's three
    versions and K5's delete marker: K5 is not removed by that run (its two versions remain), so
    the first run removed one object (K1) and the retry four (K2, K3, K4, K5): five in all, as A
    held. STORAGE-INTERFACE section 3.2: "the sum over runs is what was removed"."""
    store = _store()
    store.fail_listing("versions", call=1, after=4)

    first = erase_organization_objects(ORG_A, object_store=store)
    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert store.holds(K5) == (0, 0) and second.complete
    assert _summed(first, second) == A_REMOVED, (first.as_dict(), second.as_dict())


def test_a_store_without_the_version_operations_is_unavailable_and_never_complete(monkeypatch):
    report = erase_organization_objects(ORG_A, object_store=object())
    assert report.status is ObjectErasureStatus.UNAVAILABLE
    assert not report.complete and report.as_dict()["complete"] is False
    assert _removed(report) == (0, 0, 0, 0)

    # The application's store is read at call time: a versioned store is used as it is.
    store = _store()
    monkeypatch.setattr(infra_storage, "storage", store)
    assert resolve_object_store() is store
    assert erase_organization_objects(ORG_A).complete
    assert store.tally(ORG_A).empty


# --------------------------------------------------------------------------- #
# Through the service and the routes, on SQLite and PostgreSQL
# --------------------------------------------------------------------------- #
@pytest.fixture(params=ENGINES)
def env(request, tmp_path) -> Iterator[PrivacyEnv]:
    with engine_world(request.param, tmp_path) as e:
        yield e


def _rows(env: PrivacyEnv) -> None:
    """A's and B's media rows, matching the keys the store holds (contract section 8)."""
    now = datetime.now(UTC) - timedelta(days=2)
    rows = MediaRows()
    rows.asset(ORG_A, WS_A, "m0bt-ea1", state="active", at=now, created_by=CAROL)
    rows.upload(ORG_A, WS_A, "m0bt-ea1-up", "m0bt-ea1", state="promoted", at=now)
    rows.stored(ORG_A, WS_A, "m0bt-ea1-obj", "m0bt-ea1", at=now, parent="m0bt-ea1-up")
    rows.rights_record(ORG_A, WS_A, "m0bt-ea1", attested_by=CAROL, at=now)
    rows.asset(ORG_A, WS_A2, "m0bt-ea2", state="active", at=now, created_by=ALICE)
    rows.upload(ORG_A, WS_A2, "m0bt-ea2-up", "m0bt-ea2", state="promoted", at=now)
    rows.stored(ORG_A, WS_A2, "m0bt-ea2-obj", "m0bt-ea2", at=now, parent="m0bt-ea2-up")
    rows.rights_record(ORG_A, WS_A2, "m0bt-ea2", attested_by=CAROL, at=now)
    rows.asset(ORG_B, WS_B, "m0bt-eb1", state="active", at=now, created_by=BOB)
    rows.upload(ORG_B, WS_B, "m0bt-eb1-up", "m0bt-eb1", state="upload_pending", at=now)
    rows.stored(ORG_B, WS_B, "m0bt-eb1-obj", "m0bt-eb1", at=now)
    rows.rights_record(ORG_B, WS_B, "m0bt-eb1", attested_by=BOB, at=now)
    rows.insert(env.engine)


def _of(table_rows: dict[str, dict], org: str) -> dict[str, dict]:
    return {i: r for i, r in table_rows.items() if r["organization_id"] == org}


def _delete_org(env: PrivacyEnv, headers: dict) -> object:
    active_rate_limiter()._hits.clear()
    return env.client.post(
        f"{API}/organizations/{ORG_A}/delete",
        headers=headers,
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )


def test_deleting_organization_a_through_the_route_erases_its_objects_and_no_one_elses(
    env, monkeypatch
):
    _rows(env)
    store = _store()
    monkeypatch.setattr(infra_storage, "storage", store)
    before_store = _outside_a(store)
    before_rows = media_rows(env)

    r = _delete_org(env, env.auth(ALICE))

    assert r.status_code == 200, r.text
    assert store.tally(ORG_A).empty
    assert _outside_a(store) == before_store
    _only_a_was_named(store)
    after_rows = media_rows(env)
    for table, rows in after_rows.items():
        assert not _of(rows, ORG_A), table
        assert _of(rows, ORG_B) == _of(before_rows[table], ORG_B), table
    check = verify_organization_objects_erased(store, organization_id=ORG_A)
    assert check.verified, check.as_dict()


def test_erase_organization_reports_exactly_the_objects_it_removed(env):
    _rows(env)
    store = _store()

    with env.session() as s:
        result = erase_organization(s, organization_id=ORG_A, object_store=store)
        s.commit()

    assert result.objects is not None and result.objects.complete
    assert _removed(result.objects) == A_REMOVED
    assert _failures(result.objects) == NO_FAILURES
    assert result.counts.deleted["media_assets"] == 2
    assert store.tally(ORG_A).empty


def test_an_incomplete_object_step_answers_503_writes_nothing_and_a_retry_completes(
    env, monkeypatch
):
    _rows(env)
    store = _store()
    store.fail_deletes(K1)
    monkeypatch.setattr(infra_storage, "storage", store)
    headers = env.auth(ALICE)
    before_db = env.snapshot()
    before_outside = _outside_a(store)

    r = _delete_org(env, headers)

    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "object_erasure_incomplete"
    assert env.snapshot() == before_db  # not one row written: the organization is the retry handle
    assert store.tally(ORG_A) == Tally(keys=1, versions=3, current=1, noncurrent=2)
    assert _outside_a(store) == before_outside

    store.heal()
    retry = _delete_org(env, headers)

    assert retry.status_code == 200, retry.text
    assert store.tally(ORG_A).empty
    assert _outside_a(store) == before_outside
    assert not _of(media_rows(env)["media_assets"], ORG_A)


def test_erase_organization_raises_with_its_report_before_any_database_write(env):
    _rows(env)
    store = _store()
    store.fail_deletes(K1)
    before_db = env.snapshot()

    with env.session() as s:
        with pytest.raises(ObjectErasureIncompleteError) as exc:
            erase_organization(s, organization_id=ORG_A, object_store=store)
        s.flush()
        s.commit()  # even a commit after the refusal writes nothing

    assert exc.value.status_code == 503 and exc.value.retryable
    assert exc.value.report.status is ObjectErasureStatus.INCOMPLETE
    assert exc.value.report.remaining_versions == 3
    assert env.snapshot() == before_db


# --------------------------------------------------------------------------- #
# The attester rule (contract section 6, R-5), with stored objects
# --------------------------------------------------------------------------- #
def test_account_deletion_keeps_the_rights_content_drops_the_attester_and_erases_only_its_orgs(
    env, monkeypatch
):
    _rows(env)
    store = _store()
    monkeypatch.setattr(infra_storage, "storage", store)
    everything = store.snapshot()
    before = media_rows(env)

    # CAROL is a member of A, not its only one: A survives her deletion, and so do its objects.
    active_rate_limiter()._hits.clear()
    r = env.client.post(
        f"{API}/account/delete",
        headers=env.auth(CAROL),
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["counts"]["nulled"]["media_rights.attested_by"] == 2
    after = media_rows(env)
    for rights_id in ("m0bt-ea1-rt", "m0bt-ea2-rt"):
        kept, was = after["media_rights"][rights_id], before["media_rights"][rights_id]
        assert kept["attested_by"] is None
        assert {k: v for k, v in kept.items() if k != "attested_by"} == {
            k: v for k, v in was.items() if k != "attested_by"
        }  # basis, text version and instant kept
    assert after["media_assets"]["m0bt-ea1"]["created_by"] is None
    assert after["media_assets"]["m0bt-ea2"]["created_by"] == ALICE
    assert after["media_objects"] == before["media_objects"]
    assert after["media_provenance"] == before["media_provenance"]
    assert store.snapshot() == everything  # CAROL's deletion erases no organization's objects
    assert store.mutating_calls() == []
    with env.session() as s:
        assert verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL]).verified

    # BOB is B's only member: his deletion deletes B, and B's objects with it; A's and C's stay.
    a_and_c = store.snapshot(outside=ORG_B)
    active_rate_limiter()._hits.clear()
    r = env.client.post(
        f"{API}/account/delete",
        headers=env.auth(BOB),
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )

    assert r.status_code == 200, r.text
    assert [o["organization_id"] for o in r.json()["deleted_organizations"]] == [ORG_B]
    assert store.tally(ORG_B).empty
    assert verify_organization_objects_erased(store, organization_id=ORG_B).verified
    assert store.snapshot(outside=ORG_B) == a_and_c
    assert all(key.startswith(ORG_B + "/") for key in store.keys_acted_on())
