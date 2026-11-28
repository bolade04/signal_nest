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

Fix pass FXM0B-1 (review W7A-REV-M0B r1; contract section 6 "Fix pass FXM0B-1"), added by the
same test author:

* The counts are confirmed by the fresh listing (F-3 = D-M0BT-1, F-4 = D-M0BT-2). A delete that
  returns without deleting is not counted; one that is applied and then raises (a lost response)
  is counted once and still leaves the run incomplete; an object is counted by the run that
  removes its last entry. The two tests that pinned D-M0BT-1 and D-M0BT-2 as strict xfails are
  ordinary tests now, and the fault table's summed counts are exact in every case.
* An account deletion makes every refusal before any object erasure (F-1): refused with a 409
  because a later organization it would delete has an active job, it has made no storage call
  and changed nothing, in any organization.
* A failed listing, of versions or of uploads, alone stops an organization's erasure before any
  database write (F-5, RM04); a delete marker left behind keeps the report incomplete (RM03).

Fix pass FXM0B-2 (review re-bind r1, N-3), added by the same test author:

* No refusal is made again after a storage call: in an account deletion, every active-job
  refusal of the organizations it deletes comes before the first store call, once each (MX3).
* Where the fresh version listing failed, a version whose delete raised is not counted and its
  key is not counted as an object removed (MX7); a failed listing is never taken for one that ran
  to its end, so the versions whose deletes raised are not counted (MX8). The retry counts them,
  and the sums are exact.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

import app.infra.storage as infra_storage
import app.privacy.erasure as privacy_erasure
from app.jobs.models import Job
from app.organizations.models import Organization, OrganizationMember, Workspace
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
    StoreFault,
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


def test_the_apply_then_raise_fault_removes_the_entry_and_then_raises():
    """A lost response: the store did the work, the caller sees an error."""
    store = VersionedStore()
    v1, v2 = store.put("o/w/k"), store.put("o/w/k")
    store.start_upload("o/w/u")
    store.apply_then_fail_deletes("o/w/k")
    store.apply_then_fail_aborts("o/w/u")
    with pytest.raises(StoreFault):
        store.delete_version("o/w/k", v2)
    assert store.holds("o/w/k") == (1, 0)  # v2 is gone, v1 is left
    (upload,) = store.list_multipart_uploads("o")
    with pytest.raises(StoreFault):
        store.abort_multipart_upload(upload)
    assert list(store.list_multipart_uploads("o")) == []
    store.heal()
    store.delete_version("o/w/k", v1)  # healed: no error
    assert store.holds("o/w/k") == (0, 0)


def test_the_per_entry_fault_fails_exactly_that_version_without_removing_it():
    store = VersionedStore()
    v1, v2 = store.put("o/w/k"), store.put("o/w/k")
    store.fail_delete_versions(("o/w/k", v2))
    with pytest.raises(StoreFault):
        store.delete_version("o/w/k", v2)
    assert store.holds("o/w/k") == (2, 0)  # not applied
    store.delete_version("o/w/k", v1)  # the key's other entry deletes normally
    assert store.holds("o/w/k") == (1, 0)
    store.heal()
    store.delete_version("o/w/k", v2)  # healed: no error
    assert store.holds("o/w/k") == (0, 0)


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


FAULTS = (
    Fault("delete_raises", lambda s: s.fail_deletes(K5), (4, 5, 2, 2), "failed_deletions"),
    Fault("abort_raises", lambda s: s.fail_aborts(U2), (5, 7, 3, 1), "failed_aborts"),
    # The version listing fails before its first entry, on every call: nothing is known, so
    # nothing may be reported complete. The uploads are still listed and aborted.
    Fault("versions_listing_fails", lambda s: s.fail_listing("versions"), (0, 0, 0, 2),
          "failed_listings"),
    # The first version listing fails after 4 entries (a later page): those are removed, and K5
    # is split between the runs (D-M0BT-2's case).
    Fault("versions_listing_fails_midway", lambda s: s.fail_listing("versions", call=1, after=4),
          None, "failed_listings"),
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
    # Each removal is counted once over both runs: every object (key), version, delete marker and
    # upload, even when one key's entries are split between the runs (FXM0B-1, F-4).
    assert _summed(first, second) == A_REMOVED, (first.as_dict(), second.as_dict())
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


def test_a_deletion_that_silently_does_nothing_is_not_counted_as_removed():
    """D-M0BT-1 (review F-3, fixed in FXM0B-1). The report counts what was removed (AE-M0-4;
    contract section 6: "a delete that returned without deleting is not counted"). K1's three
    versions survive the first run, so it removed 4 keys' worth (4 versions, 3 markers, 2
    uploads), and the retry counts K1's three once."""
    store = _store()
    store.silence_deletes(K1)

    first = erase_organization_objects(ORG_A, object_store=store)
    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert _removed(first) == (4, 4, 3, 2), first.as_dict()
    assert _summed(first, second) == A_REMOVED


def test_an_object_erased_over_two_runs_is_counted_as_removed_once():
    """D-M0BT-2 (review F-4, fixed in FXM0B-1). AE-M0-4: the report counts the objects removed.
    The first listing fails after K1's three versions and K5's delete marker: K5 is not removed
    by that run (its two versions remain), so the first run removed one object (K1) and the
    retry four (K2, K3, K4, K5): five in all, as A held. Contract section 6: "`keys` counts an
    object key in the run that leaves no version and no delete marker of it"."""
    store = _store()
    store.fail_listing("versions", call=1, after=4)

    first = erase_organization_objects(ORG_A, object_store=store)
    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert store.holds(K5) == (0, 0) and second.complete
    assert _summed(first, second) == A_REMOVED, (first.as_dict(), second.as_dict())


@dataclass(frozen=True)
class LostResponse:
    name: str
    inject: object  # (store) -> None
    counter: str  # the report field that counts the calls that raised
    calls: int  # how many calls raised in the first run


LOST_RESPONSES = (
    # Each of K1's three version deletes is applied, then raises.
    LostResponse("version_deletes", lambda s: s.apply_then_fail_deletes(K1), "failed_deletions", 3),
    # K4's only entry, a delete marker: its delete is applied, then raises.
    LostResponse("marker_delete", lambda s: s.apply_then_fail_deletes(K4), "failed_deletions", 1),
    # U2's in-progress upload is aborted, then the call raises.
    LostResponse("upload_abort", lambda s: s.apply_then_fail_aborts(U2), "failed_aborts", 1),
)


@pytest.mark.parametrize("fault", LOST_RESPONSES, ids=lambda f: f.name)
def test_a_removal_applied_before_its_call_raised_is_counted_once_and_the_retry_counts_nothing(
    fault: LostResponse,
):
    """Review F-3, the mirror of D-M0BT-1 (contract section 6, FXM0B-1: a delete "that was
    applied and then raised is counted once, and still makes the run incomplete"). The fresh
    listing shows the entry gone, so the first run counts it; the failed call keeps that run
    incomplete; the retry finds nothing and counts nothing; the sum is what A held."""
    store = _store()
    before = _outside_a(store)
    fault.inject(store)

    first = erase_organization_objects(ORG_A, object_store=store)

    assert first.status is ObjectErasureStatus.INCOMPLETE and not first.complete, first.as_dict()
    assert getattr(first, fault.counter) == fault.calls, first.as_dict()
    assert store.tally(ORG_A).empty  # every removal was applied
    assert (first.remaining_versions, first.remaining_delete_markers,
            first.remaining_uploads) == (0, 0, 0)
    assert _removed(first) == A_REMOVED, first.as_dict()  # each entry counted once, by this run

    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert second.complete and _failures(second) == NO_FAILURES, second.as_dict()
    assert _removed(second) == (0, 0, 0, 0), second.as_dict()
    assert _summed(first, second) == A_REMOVED
    assert _outside_a(store) == before
    assert verify_organization_objects_erased(store, organization_id=ORG_A).verified


def test_a_delete_marker_whose_deletion_silently_does_nothing_leaves_the_report_incomplete():
    """Review F-5, RM03: a delete marker left after the deletions is counted as remaining, so the
    report is incomplete; it is neither counted as removed nor its key as an object removed."""
    store = _store()
    store.silence_deletes(K4)  # K4 holds only a delete marker

    first = erase_organization_objects(ORG_A, object_store=store)

    assert first.status is ObjectErasureStatus.INCOMPLETE and not first.complete, first.as_dict()
    assert (first.remaining_delete_markers, first.remaining_versions,
            first.remaining_uploads) == (1, 0, 0), first.as_dict()
    assert store.holds(K4) == (0, 1)
    assert _removed(first) == (4, 7, 2, 2), first.as_dict()
    assert not verify_organization_objects_erased(store, organization_id=ORG_A).verified

    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert second.complete and _removed(second) == (1, 0, 1, 0), second.as_dict()
    assert _summed(first, second) == A_REMOVED
    assert store.tally(ORG_A).empty


def _newest_version(store: VersionedStore, key: str) -> str:
    """The version id of ``key``'s latest entry (read without a protocol call)."""
    versions, _uploads = store.snapshot()
    return [version_id for k, version_id, _marker, _data in versions if k == key][-1]


def test_where_the_fresh_listing_failed_a_key_with_a_raised_delete_is_not_counted_as_removed():
    """Review re-bind N-3, MX7 (contract section 6, FXM0B-1: where the fresh listing over an
    entry failed, "only a call that returned counts", and ``keys`` counts an object in the run
    that leaves nothing of it). K1's newest version refuses to go and the fresh version listing
    fails: run 1 counts K1's two other versions but neither that version nor K1 as an object;
    the retry removes it and counts K1 and that version once."""
    store = _store()
    store.fail_delete_versions((K1, _newest_version(store, K1)))
    store.fail_listing("versions", call=2)  # the fresh listing after the deletions

    first = erase_organization_objects(ORG_A, object_store=store)

    assert first.status is ObjectErasureStatus.INCOMPLETE and not first.complete, first.as_dict()
    assert (first.failed_deletions, first.failed_listings) == (1, 1), first.as_dict()
    assert store.holds(K1) == (1, 0)
    assert _removed(first) == (4, 6, 3, 2), first.as_dict()  # K1 not counted as an object

    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert second.complete and _removed(second) == (1, 1, 0, 0), second.as_dict()
    assert _summed(first, second) == A_REMOVED
    assert store.tally(ORG_A).empty


def test_a_failed_fresh_listing_never_counts_the_versions_whose_deletes_raised():
    """Review re-bind N-3, MX8: a failed version listing is not one that ran to its end, so it
    proves nothing gone. Every delete of K1 raises and the fresh version listing fails: run 1
    counts everything but K1, the retry counts K1's object and three versions, and the sum is
    what A held."""
    store = _store()
    store.fail_deletes(K1)
    store.fail_listing("versions", call=2)  # the fresh listing after the deletions

    first = erase_organization_objects(ORG_A, object_store=store)

    assert first.status is ObjectErasureStatus.INCOMPLETE and not first.complete, first.as_dict()
    assert (first.failed_deletions, first.failed_listings) == (3, 1), first.as_dict()
    assert _removed(first) == (4, 4, 3, 2), first.as_dict()  # none of K1's three versions

    store.heal()
    second = erase_organization_objects(ORG_A, object_store=store)

    assert second.complete and _removed(second) == (1, 3, 0, 0), second.as_dict()
    assert _summed(first, second) == A_REMOVED
    assert store.tally(ORG_A).empty


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


#: Review F-5, RM04: a listing that fails on every call (the missing-permission case of F-2),
#: of versions or of uploads, with no deletion failing.
LISTINGS = ("versions", "uploads")


@pytest.mark.parametrize("listing", LISTINGS)
def test_a_failing_listing_alone_makes_erase_organization_raise_before_any_database_write(
    env, listing
):
    _rows(env)
    store = _store()
    store.fail_listing(listing)
    before_db = env.snapshot()

    with env.session() as s:
        with pytest.raises(ObjectErasureIncompleteError) as exc:
            erase_organization(s, organization_id=ORG_A, object_store=store)
        s.flush()
        s.commit()  # even a commit after the refusal writes nothing

    report = exc.value.report
    assert exc.value.status_code == 503 and exc.value.retryable
    assert report.status is ObjectErasureStatus.INCOMPLETE and report.failed_listings >= 1
    assert (report.failed_deletions, report.failed_aborts) == (0, 0), report.as_dict()
    assert env.snapshot() == before_db


@pytest.mark.parametrize("listing", LISTINGS)
def test_a_failing_listing_alone_answers_503_through_the_route_and_writes_nothing(
    env, monkeypatch, listing
):
    _rows(env)
    store = _store()
    store.fail_listing(listing)
    monkeypatch.setattr(infra_storage, "storage", store)
    headers = env.auth(ALICE)
    before_db = env.snapshot()
    before_outside = _outside_a(store)

    r = _delete_org(env, headers)

    assert r.status_code == 503, r.text
    assert r.json()["error"]["code"] == "object_erasure_incomplete"
    assert env.snapshot() == before_db
    assert _outside_a(store) == before_outside

    store.heal()
    retry = _delete_org(env, headers)

    assert retry.status_code == 200, retry.text
    assert store.tally(ORG_A).empty
    assert _outside_a(store) == before_outside
    assert verify_organization_objects_erased(store, organization_id=ORG_A).verified


# --------------------------------------------------------------------------- #
# Account deletion makes every refusal before any object erasure (review F-1, FXM0B-1)
# --------------------------------------------------------------------------- #
#: A second organization of which BOB is the only member, after B in id order.
ORG_D, WS_D = "prv-org-d", "prv-ws-d"
KD1 = asset_key(ORG_D, WS_D, "m0bt-ed1", "m0bt-ed1-obj")
KD2 = upload_key(ORG_D, WS_D, "m0bt-ed1-up")
#: The job states in which a worker holds (or is meant to hold) a lease on a job.
ACTIVE_JOB_STATES = ("claimed", "running", "cancel_requested")


def _organization_d(
    env: PrivacyEnv, store: VersionedStore, *, job_status: str | None
) -> None:
    """D: BOB its only member (as of B), a job of ``job_status`` (none if ``None``), and stored
    objects: a key with a noncurrent version and a delete marker, and an in-progress upload."""
    assert ORG_B < ORG_D
    with env.session() as s:
        s.add(Organization(id=ORG_D, name="Delta Privacy", slug=ORG_D))
        s.flush()
        s.add(Workspace(id=WS_D, organization_id=ORG_D, name=WS_D, slug=WS_D))
        s.add(OrganizationMember(organization_id=ORG_D, user_id=BOB, role="owner"))
        s.flush()
        if job_status is not None:
            s.add(
                Job(
                    id="m0bt-d-job", organization_id=ORG_D, workspace_id=WS_D,
                    job_type="scout_request.execute", payload={}, payload_hash="0" * 64,
                    status=job_status,
                )
            )
        s.commit()
    store.put(KD1)
    store.put(KD1)
    store.delete(KD1)
    store.start_upload(KD2)


def _delete_account(env: PrivacyEnv, headers: dict) -> object:
    active_rate_limiter()._hits.clear()
    return env.client.post(
        f"{API}/account/delete",
        headers=headers,
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )


def _outside(store: VersionedStore, *organizations: str) -> tuple:
    """Every entry and upload not under any of ``organizations``' prefixes."""
    versions, uploads = store.snapshot()
    under = tuple(org + "/" for org in organizations)
    return (tuple(v for v in versions if not v[0].startswith(under)),
            tuple(u for u in uploads if not u[0].startswith(under)))


@pytest.mark.parametrize("job_status", ACTIVE_JOB_STATES)
def test_an_account_deletion_refused_for_a_later_organizations_job_touches_no_stored_object(
    env, monkeypatch, job_status
):
    """BOB is the only member of B and of D (after B in id order); D has an active job. The
    deletion answers 409 ``organization_jobs_active``, and B's objects, erased first in id order
    before the fix, are untouched: no storage call was made, no row changed."""
    _rows(env)
    store = _store()
    _organization_d(env, store, job_status=job_status)
    monkeypatch.setattr(infra_storage, "storage", store)
    headers = env.auth(BOB)
    everything = store.snapshot()
    b_and_d = (store.tally(ORG_B), store.tally(ORG_D))
    before_db = env.snapshot()

    r = _delete_account(env, headers)

    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "organization_jobs_active"
    assert store.mutating_calls() == []
    assert store.calls == []  # contract section 6: "makes no storage call and changes nothing"
    assert store.snapshot() == everything  # B's and D's objects, versions and uploads included
    assert (store.tally(ORG_B), store.tally(ORG_D)) == b_and_d
    assert not b_and_d[0].empty and not b_and_d[1].empty
    assert env.snapshot() == before_db


def test_with_the_later_organizations_job_finished_the_account_deletion_erases_both(
    env, monkeypatch
):
    _rows(env)
    store = _store()
    _organization_d(env, store, job_status="succeeded")
    monkeypatch.setattr(infra_storage, "storage", store)
    headers = env.auth(BOB)
    others = _outside(store, ORG_B, ORG_D)

    r = _delete_account(env, headers)

    assert r.status_code == 200, r.text
    assert [o["organization_id"] for o in r.json()["deleted_organizations"]] == [ORG_B, ORG_D]
    for org in (ORG_B, ORG_D):
        assert store.tally(org).empty, org
        check = verify_organization_objects_erased(store, organization_id=org)
        assert check.verified, check.as_dict()
        assert env.count(Organization, Organization.id == org) == 0
    assert _outside(store, ORG_B, ORG_D) == others  # A, C and the sibling untouched
    assert all(key.startswith((ORG_B + "/", ORG_D + "/")) for key in store.keys_acted_on())


class _LoggedStore:
    """The four protocol calls of a store, each logged as ``("store", method)`` in a shared log
    before it is passed on."""

    def __init__(self, store: VersionedStore, log: list[tuple[str, str]]) -> None:
        self._store, self._log = store, log

    def list_object_versions(self, prefix):
        self._log.append(("store", "list_object_versions"))
        return self._store.list_object_versions(prefix)

    def delete_version(self, key, version_id):
        self._log.append(("store", "delete_version"))
        return self._store.delete_version(key, version_id)

    def list_multipart_uploads(self, prefix):
        self._log.append(("store", "list_multipart_uploads"))
        return self._store.list_multipart_uploads(prefix)

    def abort_multipart_upload(self, upload):
        self._log.append(("store", "abort_multipart_upload"))
        return self._store.abort_multipart_upload(upload)


def test_an_account_deletion_makes_every_refusal_before_its_first_store_call(env, monkeypatch):
    """Review re-bind N-3, MX3 (contract section 6, FXM0B-1: every organization the deletion
    deletes passes its own refusal "before the first organization's object step", and "no
    refusal after a storage call"). BOB, the only member of B and D, has no active job: the
    deletion answers 200, and one log of the active-job refusals and the store's protocol calls
    shows each refusal, of B and of D once each, before the first store call."""
    _rows(env)
    store = _store()
    _organization_d(env, store, job_status=None)
    log: list[tuple[str, str]] = []
    original = privacy_erasure._require_no_active_jobs

    def recording(db, organization_id):
        log.append(("refusal", organization_id))
        return original(db, organization_id)

    monkeypatch.setattr(privacy_erasure, "_require_no_active_jobs", recording)
    monkeypatch.setattr(infra_storage, "storage", _LoggedStore(store, log))
    headers = env.auth(BOB)

    r = _delete_account(env, headers)

    assert r.status_code == 200, r.text
    assert [o["organization_id"] for o in r.json()["deleted_organizations"]] == [ORG_B, ORG_D]
    refusals = [i for i, (kind, _what) in enumerate(log) if kind == "refusal"]
    calls = [i for i, (kind, _what) in enumerate(log) if kind == "store"]
    assert calls, log  # the object steps ran through the logged store
    assert refusals and max(refusals) < min(calls), log
    assert sorted(log[i][1] for i in refusals) == [ORG_B, ORG_D], log  # once each
    assert store.tally(ORG_B).empty and store.tally(ORG_D).empty


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
