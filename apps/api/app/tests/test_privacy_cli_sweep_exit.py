"""FU-M0B-F7: the retention sweep CLI's exit status when required deletions remain incomplete.

Authored by the test author line M0B-T (cross-authorship, as in ruling M0B-1); the builder line
wrote the fix and none of this. Written from review W7A-REV-M0B r1 F-7 (``cli.py`` exited ``0``
when the media purge kept roots), the operator's instruction for FU-M0B-F7, and the CLI's
documented exit status, against the operator surface: ``python -m app.privacy.cli sweep``
(``cli.main``), its JSON report on stdout and its exit code. The store is the local
versioned-storage fixture ``_versioned_store_support.VersionedStore`` set as the application's
store, so the sweep erases through the same path it uses in production.

On SQLite and on PostgreSQL, with synthetic media records:

* **(a) incomplete:** a media purge that keeps a root over a failed storage deletion exits ``5``.
  The report keeps its shape, says ``complete: false`` with ``kept``, and its counts are exact;
  the next sweep completes the root and exits ``0``.
* **(b) completed:** a sweep that removes every eligible root exits ``0``, with exact counts.
* **(c) precedence:** a class with no duration (``policy_unset``) together with a kept media root
  exits ``2``.
* **(d) normal skips exit ``0``:** a dry run (even over a failing store, which it never lists), a
  sweep with nothing due, and a class stopped at its batch limit (``complete: false`` with
  nothing kept; the next sweep continues it).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import fields
from datetime import UTC, datetime, timedelta

import pytest

import app.infra.storage as infra_storage
from app.core.config import get_settings
from app.privacy import cli, retention
from app.privacy.inventory import RetentionClass
from app.tests._privacy_support import BOB, CAROL, ORG_A, ORG_B, WS_A, WS_B, PrivacyEnv
from app.tests._versioned_store_support import (
    ENGINES,
    MediaRows,
    VersionedStore,
    asset_key,
    engine_world,
    media_rows,
)

#: The report's fields, as the CLI prints them (one document; one entry per class).
DOCUMENT_FIELDS = {"dry_run", "classes"}
CLASS_FIELDS = {f.name for f in fields(retention.ClassOutcome)}
OBJECT_FIELDS = {
    "store", "status", "complete", "keys", "versions", "delete_markers", "aborted_uploads",
    "failed_deletions", "failed_aborts", "failed_listings", "refused_keys",
    "remaining_versions", "remaining_delete_markers", "remaining_uploads",
}
NO_PROBLEMS = {
    "failed_deletions": 0, "failed_aborts": 0, "failed_listings": 0, "refused_keys": 0,
    "remaining_versions": 0, "remaining_delete_markers": 0, "remaining_uploads": 0,
}


@pytest.fixture(params=ENGINES)
def env(request, tmp_path, monkeypatch) -> Iterator[PrivacyEnv]:
    """The seeded world, every retention duration unset except the two media ones (test-only
    durations), default batch bounds, and the application's store left to each test."""
    settings = get_settings()
    for data_class in RetentionClass:
        monkeypatch.setattr(settings, data_class.setting_name, None)
    monkeypatch.setattr(settings, "retention_media_uploads_days", 30)
    monkeypatch.setattr(settings, "retention_media_records_days", 30)
    monkeypatch.setattr(settings, "retention_purge_batch_size", 500)
    monkeypatch.setattr(settings, "retention_purge_max_batches", 100)
    with engine_world(request.param, tmp_path) as e:
        yield e


# --------------------------------------------------------------------------- #
# The world: two eligible roots and a live asset
# --------------------------------------------------------------------------- #
OLD = datetime.now(UTC) - timedelta(days=400)
NEW = datetime.now(UTC) - timedelta(days=1)
#: C1: an old asset of A, with its old promoted upload record (1 version at the upload key) and
#: its stored object (2 versions).
C1_UP_KEY = f"{ORG_A}/{WS_A}/media/uploads/m0bt-c1-up"
C1_OBJ_KEY = asset_key(ORG_A, WS_A, "m0bt-c1", "m0bt-c1-obj")
#: C2: an old asset of B with a stored object (1 version and a delete marker on top).
C2_OBJ_KEY = asset_key(ORG_B, WS_B, "m0bt-c2", "m0bt-c2-obj")
#: C3: a live asset of A (created yesterday), its object written yesterday: never due.
C3_OBJ_KEY = asset_key(ORG_A, WS_A, "m0bt-c3", "m0bt-c3-obj")


def _world(env: PrivacyEnv, *, eligible: bool = True) -> VersionedStore:
    """Rows and objects; the store becomes the application's (``resolve_object_store``)."""
    rows, store = MediaRows(), VersionedStore()
    if eligible:
        rows.asset(ORG_A, WS_A, "m0bt-c1", state="active", at=OLD, created_by=CAROL)
        assert rows.upload(ORG_A, WS_A, "m0bt-c1-up", "m0bt-c1", state="promoted",
                           at=OLD) == C1_UP_KEY
        assert rows.stored(ORG_A, WS_A, "m0bt-c1-obj", "m0bt-c1", at=OLD,
                           parent="m0bt-c1-up") == C1_OBJ_KEY
        rows.rights_record(ORG_A, WS_A, "m0bt-c1", attested_by=CAROL, at=OLD)
        rows.asset(ORG_B, WS_B, "m0bt-c2", state="active", at=OLD, created_by=BOB)
        assert rows.stored(ORG_B, WS_B, "m0bt-c2-obj", "m0bt-c2", at=OLD) == C2_OBJ_KEY
        rows.rights_record(ORG_B, WS_B, "m0bt-c2", attested_by=BOB, at=OLD)
        store.put(C1_UP_KEY)
        store.put(C1_OBJ_KEY)
        store.put(C1_OBJ_KEY)
        store.put(C2_OBJ_KEY)
        store.delete(C2_OBJ_KEY)
    rows.asset(ORG_A, WS_A, "m0bt-c3", state="active", at=NEW, created_by=CAROL)
    assert rows.stored(ORG_A, WS_A, "m0bt-c3-obj", "m0bt-c3", at=NEW) == C3_OBJ_KEY
    rows.rights_record(ORG_A, WS_A, "m0bt-c3", attested_by=CAROL, at=NEW)
    store.put(C3_OBJ_KEY)
    rows.insert(env.engine)
    return store


def _use(monkeypatch, store: VersionedStore) -> None:
    monkeypatch.setattr(infra_storage, "storage", store)


def _run(capsys, *argv: str) -> tuple[int, dict]:
    """``python -m app.privacy.cli sweep ...`` in process: its exit code and its JSON report."""
    capsys.readouterr()  # drop anything printed before
    code = cli.main(["sweep", *argv])
    document = json.loads(capsys.readouterr().out)
    assert set(document) == DOCUMENT_FIELDS, document
    for outcome in document["classes"]:
        assert set(outcome) == CLASS_FIELDS, outcome  # the report's shape is unchanged
        if outcome["objects"] is not None:
            assert set(outcome["objects"]) == OBJECT_FIELDS, outcome["objects"]
    return code, document


def _removed(objects: dict) -> dict[str, int]:
    return {k: objects[k] for k in ("keys", "versions", "delete_markers", "aborted_uploads")}


def _problems(objects: dict) -> dict[str, int]:
    return {k: objects[k] for k in NO_PROBLEMS}


# --------------------------------------------------------------------------- #
# (a) Kept roots: required deletions remain incomplete -> 5
# --------------------------------------------------------------------------- #
def test_a_kept_media_root_over_a_failed_storage_deletion_exits_5(env, monkeypatch, capsys):
    store = _world(env)
    store.fail_deletes(C1_OBJ_KEY)  # both versions of C1's stored object refuse to go
    _use(monkeypatch, store)

    code, report = _run(capsys, "--class", "media_uploads")

    assert code == 5, (code, report)  # F-7: before the fix this kept root exited 0
    assert cli.EXIT_INCOMPLETE == 5
    assert report["dry_run"] is False
    (media,) = report["classes"]
    assert (media["data_class"], media["status"], media["retention_days"]) == (
        "media_uploads", "purged", 30
    )
    assert media["complete"] is False
    assert media["kept"] == {"media_assets": 1, "media_objects": 1}  # C1 and its upload record
    assert media["batches"] == 1  # a batch that kept roots stops the class for this run
    # C2 went, rows and objects; of C1 only the upload key's version went, under C1's own scope.
    assert media["deleted"] == {
        "media_assets": 1, "media_objects": 1, "media_provenance": 1, "media_rights": 1,
    }
    objects = media["objects"]
    assert (objects["store"], objects["status"], objects["complete"]) == (
        "media", "incomplete", False
    )
    assert _removed(objects) == {"keys": 2, "versions": 2, "delete_markers": 1,
                                 "aborted_uploads": 0}
    assert _problems(objects) == {**NO_PROBLEMS, "failed_deletions": 2, "remaining_versions": 2}
    assert store.holds(C1_OBJ_KEY) == (2, 0)
    rows = media_rows(env)
    assert {"m0bt-c1", "m0bt-c3"} == set(rows["media_assets"])
    assert {"m0bt-c1-up", "m0bt-c1-obj", "m0bt-c3-obj"} == set(rows["media_objects"])

    # The next sweep retries the kept root, completes it and exits 0.
    store.heal()
    code, report = _run(capsys, "--class", "media_uploads")

    assert code == cli.EXIT_OK == 0
    (media,) = report["classes"]
    assert (media["complete"], media["kept"]) == (True, {})
    assert media["deleted"] == {
        "media_assets": 1, "media_objects": 2, "media_provenance": 1, "media_rights": 1,
    }
    assert _removed(media["objects"]) == {"keys": 1, "versions": 2, "delete_markers": 0,
                                          "aborted_uploads": 0}
    assert _problems(media["objects"]) == NO_PROBLEMS
    assert set(media_rows(env)["media_assets"]) == {"m0bt-c3"}


# --------------------------------------------------------------------------- #
# (b) A completed sweep -> 0
# --------------------------------------------------------------------------- #
def test_a_completed_sweep_exits_0_with_exact_counts(env, monkeypatch, capsys):
    store = _world(env)
    _use(monkeypatch, store)

    code, report = _run(capsys, "--class", "media_uploads")

    assert code == cli.EXIT_OK == 0
    (media,) = report["classes"]
    assert (media["status"], media["complete"], media["kept"], media["batches"]) == (
        "purged", True, {}, 1
    )
    assert media["deleted"] == {
        "media_assets": 2, "media_objects": 3, "media_provenance": 2, "media_rights": 2,
    }
    assert (media["objects"]["status"], media["objects"]["complete"]) == ("complete", True)
    assert _removed(media["objects"]) == {"keys": 3, "versions": 4, "delete_markers": 1,
                                          "aborted_uploads": 0}
    assert _problems(media["objects"]) == NO_PROBLEMS
    assert set(media_rows(env)["media_assets"]) == {"m0bt-c3"}
    assert store.holds(C3_OBJ_KEY) == (1, 0)  # the live asset's object is untouched


# --------------------------------------------------------------------------- #
# (c) policy_unset (2) takes precedence over kept roots (5)
# --------------------------------------------------------------------------- #
def test_an_unset_class_with_a_kept_media_root_exits_2(env, monkeypatch, capsys):
    store = _world(env)
    store.fail_deletes(C1_OBJ_KEY)
    _use(monkeypatch, store)

    code, report = _run(capsys, "--class", "media_uploads", "--class", "auth_sessions")

    assert code == cli.EXIT_POLICY_UNSET == 2
    media, sessions = report["classes"]
    assert (sessions["data_class"], sessions["status"]) == ("auth_sessions", "policy_unset")
    # The media purge did run and kept C1: on its own this sweep would exit 5.
    assert (media["data_class"], media["status"], media["complete"]) == (
        "media_uploads", "purged", False
    )
    assert media["kept"] == {"media_assets": 1, "media_objects": 1}
    assert media["objects"]["failed_deletions"] == 2


# --------------------------------------------------------------------------- #
# (d) The normal skips -> 0
# --------------------------------------------------------------------------- #
def test_a_dry_run_exits_0_and_never_asks_the_store(env, monkeypatch, capsys):
    store = _world(env)
    store.fail_deletes(C1_OBJ_KEY)  # a failing store: a dry run never reaches it
    _use(monkeypatch, store)
    before_db, before_store = env.snapshot(), store.snapshot()

    code, report = _run(capsys, "--class", "media_uploads", "--dry-run")

    assert code == cli.EXIT_OK == 0
    assert report["dry_run"] is True
    (media,) = report["classes"]
    # The roots past the cut-off: the assets C1 and C2, and C1's upload record.
    assert (media["status"], media["candidates"], media["complete"], media["kept"]) == (
        "dry_run", 3, None, {}
    )
    assert (media["deleted"], media["objects"], media["batches"]) == ({}, None, 0)
    assert store.calls == []
    assert store.snapshot() == before_store
    assert env.snapshot() == before_db


def test_a_sweep_with_nothing_due_exits_0(env, monkeypatch, capsys):
    store = _world(env, eligible=False)  # only the live asset C3
    store.fail_deletes(C3_OBJ_KEY)  # never asked for: C3 is not due
    _use(monkeypatch, store)
    before_db, before_store = env.snapshot(), store.snapshot()

    code, report = _run(capsys, "--class", "media_uploads")

    assert code == cli.EXIT_OK == 0
    (media,) = report["classes"]
    assert (media["status"], media["complete"], media["kept"], media["deleted"]) == (
        "purged", True, {}, {}
    )
    assert store.mutating_calls() == []
    assert store.snapshot() == before_store
    assert env.snapshot() == before_db


def test_a_class_stopped_at_its_batch_limit_exits_0_and_the_next_sweep_continues(
    env, monkeypatch, capsys
):
    """``complete: false`` with nothing kept is the policy's normal per-sweep bound, not an
    incomplete deletion: one batch of one root per model, then the sweep stops."""
    store = _world(env)
    _use(monkeypatch, store)
    monkeypatch.setattr(get_settings(), "retention_purge_batch_size", 1)
    monkeypatch.setattr(get_settings(), "retention_purge_max_batches", 1)

    code, report = _run(capsys, "--class", "media_uploads")

    assert code == cli.EXIT_OK == 0
    (media,) = report["classes"]
    assert (media["status"], media["complete"], media["kept"], media["batches"]) == (
        "purged", False, {}, 1
    )
    # The first batch: the asset C1 (lowest id) with its upload record; C2 waits.
    assert media["deleted"] == {
        "media_assets": 1, "media_objects": 2, "media_provenance": 1, "media_rights": 1,
    }
    assert _removed(media["objects"]) == {"keys": 2, "versions": 3, "delete_markers": 0,
                                          "aborted_uploads": 0}
    assert _problems(media["objects"]) == NO_PROBLEMS
    assert set(media_rows(env)["media_assets"]) == {"m0bt-c2", "m0bt-c3"}

    # The next sweep continues where this one stopped.
    code, report = _run(capsys, "--class", "media_uploads")

    assert code == cli.EXIT_OK
    (media,) = report["classes"]
    assert (media["kept"], media["deleted"]["media_assets"]) == ({}, 1)
    assert set(media_rows(env)["media_assets"]) == {"m0bt-c3"}
