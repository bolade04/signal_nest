"""AE-M0-4: ``privacy/verification.py`` checks the three listings independently (M0-B's code).

Authored by the M0-A lane's test role (contract section 11), here the independent test author
M0B-T (coordinator ruling M0B-1); the M0-B builder wrote none of it. Written from plan v3's
AE-M0-4 ("a version-aware listing of A's prefix returns no objects, no noncurrent versions and
no delete markers, and a listing of in-progress multipart uploads under A's prefix returns none
... ``privacy/verification.py`` checks the same three listings independently") and the
contract (sections 6 and 8), against M0-B's public surface:
``verify_organization_objects_erased(store, organization_id=...)`` and its
``ObjectListingCheck`` (``objects``, ``noncurrent_versions``, ``delete_markers``,
``in_progress_uploads``, ``failed_listings``, ``prefix``, ``verified``). The store is the local
versioned-storage fixture ``_versioned_store_support.VersionedStore``.

What these tests show:

* An empty prefix verifies, with all three listings run and every count zero.
* A leftover object, noncurrent version, delete marker or in-progress upload is a finding, each
  counted in its own listing; so is a key that only a delete marker hides.
* A listing that fails is named and never counted as empty, and the others are still counted:
  a failing listing cannot make an unerased prefix look erased.
* Only entries strictly under ``{organization_id}/`` count, whatever the store returns.
* The verifier only reads, and it agrees with the store's own picture after a partial erasure
  and after the retry; after organization deletion through the real route (SQLite and
  PostgreSQL) A verifies and B does not.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

import app.infra.storage as infra_storage
from app.privacy.object_erasure import erase_organization_objects
from app.privacy.verification import ObjectListingCheck, verify_organization_objects_erased
from app.tests._auth2_support import active_rate_limiter
from app.tests._privacy_support import (
    ALICE,
    API,
    NAME,
    ORG_A,
    ORG_B,
    PASSWORD,
    WS_A,
    WS_A2,
    WS_B,
    PrivacyEnv,
)
from app.tests._versioned_store_support import (
    ENGINES,
    VersionedStore,
    asset_key,
    engine_world,
    upload_key,
)

KA = asset_key(ORG_A, WS_A, "m0bt-va1", "m0bt-va1-obj")
KA2 = asset_key(ORG_A, WS_A2, "m0bt-va2", "m0bt-va2-obj")
UA = upload_key(ORG_A, WS_A, "m0bt-va-up")
KB = asset_key(ORG_B, WS_B, "m0bt-vb1", "m0bt-vb1-obj")
SIBLING = f"{ORG_A}0"


def _counts(check: ObjectListingCheck) -> tuple:
    return (check.objects, check.noncurrent_versions, check.delete_markers,
            check.in_progress_uploads)


def _b_only() -> VersionedStore:
    """B holds two versions of an object under a delete marker, and an in-progress upload."""
    store = VersionedStore()
    store.put(KB)
    store.put(KB)
    store.delete(KB)
    store.start_upload(upload_key(ORG_B, WS_B, "m0bt-vb-up"))
    return store


# --------------------------------------------------------------------------- #
# The three listings
# --------------------------------------------------------------------------- #
def test_an_empty_prefix_verifies_with_all_three_listings_run_and_zero():
    store = _b_only()

    check = verify_organization_objects_erased(store, organization_id=ORG_A)

    assert check.verified
    assert _counts(check) == (0, 0, 0, 0)
    assert check.failed_listings == []
    assert (check.store, check.prefix) == ("media", f"{ORG_A}/")
    assert check.generated_at
    as_dict = check.as_dict()
    assert as_dict["verified"] is True and as_dict["prefix"] == f"{ORG_A}/"
    listed = {(c[0], c[1]) for c in store.calls}
    assert listed == {("list_object_versions", ORG_A), ("list_multipart_uploads", ORG_A)}


def _object(store):  # a current object
    store.put(KA)


def _object_with_history(store):  # a current object over a noncurrent version
    store.put(KA)
    store.put(KA)


def _hidden_by_a_marker(store):  # a "deleted" object: its version survives under a marker
    store.put(KA)
    store.delete(KA)


def _marker_only(store):  # a delete marker with nothing under it
    store.delete(KA)


def _upload(store):  # an in-progress multipart upload: parts, but no object
    store.start_upload(UA)


def _everything(store):
    _object_with_history(store)  # 1 current, 1 noncurrent
    store.put(KA2)
    store.delete(KA2)  # 1 noncurrent, 1 marker
    store.start_upload(UA)
    store.start_upload(KA2)


LEFTOVERS = [
    pytest.param(_object, (1, 0, 0, 0), id="current_object"),
    pytest.param(_object_with_history, (1, 1, 0, 0), id="noncurrent_version"),
    pytest.param(_hidden_by_a_marker, (0, 1, 1, 0), id="version_under_a_delete_marker"),
    pytest.param(_marker_only, (0, 0, 1, 0), id="delete_marker"),
    pytest.param(_upload, (0, 0, 0, 1), id="in_progress_upload"),
    pytest.param(_everything, (1, 2, 1, 2), id="all_kinds"),
]


@pytest.mark.parametrize(("leave", "expected"), LEFTOVERS)
def test_each_kind_of_leftover_is_a_finding_counted_in_its_own_listing(leave, expected):
    store = _b_only()
    leave(store)
    held = store.tally(ORG_A)
    assert (held.current, held.noncurrent, held.delete_markers, held.uploads) == expected

    check = verify_organization_objects_erased(store, organization_id=ORG_A)

    assert not check.verified
    assert check.as_dict()["verified"] is False
    assert _counts(check) == expected
    assert check.failed_listings == []


FAILING = [
    # The first version listing (objects) fails before its first entry.
    pytest.param(dict(kind="versions", call=1), "objects", id="objects_listing"),
    # The second version listing (noncurrent versions and delete markers) fails.
    pytest.param(dict(kind="versions", call=2), "versions", id="versions_listing"),
    pytest.param(dict(kind="uploads", call=1), "uploads", id="uploads_listing"),
    # A listing that fails on a later page, after some entries, is still a failure.
    pytest.param(dict(kind="versions", call=1, after=1), "objects", id="objects_listing_midway"),
    pytest.param(dict(kind="versions", call=2, after=1), "versions",
                 id="versions_listing_midway"),
    pytest.param(dict(kind="uploads", call=1, after=1), "uploads", id="uploads_listing_midway"),
]


@pytest.mark.parametrize("empty", [True, False], ids=["empty_prefix", "leftovers"])
@pytest.mark.parametrize(("fault", "named"), FAILING)
def test_a_failing_listing_is_named_never_counted_as_empty_and_the_others_still_count(
    fault, named, empty
):
    store = _b_only()
    if not empty:
        _everything(store)
    expected = (0, 0, 0, 0) if empty else (1, 2, 1, 2)
    fault = dict(fault)  # the parameter is shared between the two ``empty`` cases
    store.fail_listing(fault.pop("kind"), **fault)

    check = verify_organization_objects_erased(store, organization_id=ORG_A)

    assert not check.verified  # even an empty prefix is not shown empty by a failed listing
    assert check.failed_listings == [named]
    field = {"objects": ("objects",), "versions": ("noncurrent_versions", "delete_markers"),
             "uploads": ("in_progress_uploads",)}[named]
    for name in field:
        assert getattr(check, name) is None, name
    others = {"objects": 0, "noncurrent_versions": 1, "delete_markers": 2,
              "in_progress_uploads": 3}
    for name, i in others.items():
        if name not in field:
            assert getattr(check, name) == expected[i], name


def test_only_entries_strictly_under_the_organizations_prefix_count():
    """B's entries, a sibling prefix that literally starts with A's id, and a key equal to A's id
    are not A's, even when the store over-returns them for A's listings."""
    store = _b_only()
    store.put(f"{SIBLING}/{WS_A}/media/assets/x/y")
    store.start_upload(f"{SIBLING}/{WS_A}/media/uploads/z")
    store.put(ORG_A)
    store.over_return(ORG_A, ORG_B)

    check = verify_organization_objects_erased(store, organization_id=ORG_A)

    assert check.verified, check.as_dict()
    assert _counts(check) == (0, 0, 0, 0)
    sibling = verify_organization_objects_erased(store, organization_id=SIBLING)
    assert not sibling.verified and _counts(sibling) == (1, 0, 0, 1)


def test_a_store_that_cannot_list_is_a_failed_check_never_a_verified_one():
    for store in (object(), _VersionsOnly(), _UploadsOnly()):
        check = verify_organization_objects_erased(store, organization_id=ORG_A)
        assert not check.verified
        assert check.failed_listings == ["store"]
        assert _counts(check) == (None, None, None, None)


class _VersionsOnly:
    def list_object_versions(self, prefix):
        return iter(())


class _UploadsOnly:
    def list_multipart_uploads(self, prefix):
        return iter(())


@pytest.mark.parametrize("bad", ["", ".", "..", "a/b", f"{ORG_A}/", "a\\b", "a\x00b"])
def test_an_organization_id_that_is_not_one_key_segment_is_refused(bad):
    store = _b_only()
    with pytest.raises(ValueError):
        verify_organization_objects_erased(store, organization_id=bad)
    assert store.calls == []


def test_the_verifier_only_reads():
    store = _b_only()
    _everything(store)
    before = store.snapshot()

    verify_organization_objects_erased(store, organization_id=ORG_A)
    verify_organization_objects_erased(store, organization_id=ORG_B)

    assert store.snapshot() == before
    assert store.mutating_calls() == []
    assert {c[0] for c in store.calls} == {"list_object_versions", "list_multipart_uploads"}


# --------------------------------------------------------------------------- #
# Agreement with the store after an erasure, and after deletion through the route
# --------------------------------------------------------------------------- #
def test_the_verifier_finds_exactly_what_a_partial_erasure_left_and_nothing_after_the_retry():
    store = _b_only()
    _everything(store)  # KA: 2 versions; KA2: a version under a marker; 2 uploads
    store.fail_deletes(KA)
    store.fail_aborts(UA)

    report = erase_organization_objects(ORG_A, object_store=store)

    assert not report.complete
    check = verify_organization_objects_erased(store, organization_id=ORG_A)
    held = store.tally(ORG_A)
    assert (held.current, held.noncurrent, held.delete_markers, held.uploads) == (1, 1, 0, 1)
    assert not check.verified and _counts(check) == (1, 1, 0, 1)

    store.heal()
    assert erase_organization_objects(ORG_A, object_store=store).complete
    after = verify_organization_objects_erased(store, organization_id=ORG_A)
    assert after.verified and _counts(after) == (0, 0, 0, 0)


@pytest.fixture(params=ENGINES)
def env(request, tmp_path) -> Iterator[PrivacyEnv]:
    with engine_world(request.param, tmp_path) as e:
        yield e


def test_after_organization_deletion_through_the_route_a_verifies_and_b_does_not(
    env, monkeypatch
):
    store = _b_only()
    _everything(store)
    monkeypatch.setattr(infra_storage, "storage", store)
    assert not verify_organization_objects_erased(store, organization_id=ORG_A).verified

    active_rate_limiter()._hits.clear()
    r = env.client.post(
        f"{API}/organizations/{ORG_A}/delete",
        headers=env.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )

    assert r.status_code == 200, r.text
    check_a = verify_organization_objects_erased(store, organization_id=ORG_A)
    assert check_a.verified, check_a.as_dict()
    check_b = verify_organization_objects_erased(store, organization_id=ORG_B)
    assert not check_b.verified
    assert _counts(check_b) == (0, 2, 1, 1)  # B's two versions under its marker, its upload
