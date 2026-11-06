"""Project Phase 5 CF-3 (W21): privacy of the 5D/5E tables, per operator decision O-6.

Built on the P6-PRIV-1 world (``_privacy_support``) with the CF-1/CF-2 helpers. Each test adds,
in organization A, CAROL's brief and draft, DAVE's approval of the draft, **CAROL's export** of
it and DAVE's notifications and CAROL's (with one preference of hers), and in B, BOB's brief,
draft, approval and export. The world's campaigns already list their own location, so each
workspace also gets one ``campaign_locations`` row.

* **Account deletion (CAROL).** Her export is kept; ``exported_by_snapshot`` becomes
  ``"[erased]"`` and ``exported_by`` is nulled; her notifications and preference are deleted
  with her account (DAVE's are kept); the receipt counts the anonymised export and the
  independent verifier finds nothing (before the deletion it finds both).
* **Organization deletion (A).** A's exports are **retained** (O-6, FK-less scope), with the
  exporter cleared and ``draft_id`` nulled when the draft goes; A's notifications, preferences
  and campaign memberships are deleted; B is untouched; the verifier passes, and reports a
  retained export whose identity was not cleared (negative control).
* **Account export.** CAROL's export lists her exports, notifications and preferences — codes,
  ids, versions, instants and her own snapshot; never the exported text, and never another
  account's rows.
* **Organization export.** ``exports`` and ``campaign_locations`` are in it; the per-user
  ``notifications`` and ``notification_preferences`` are not.
* **Retention.** Exports are purged with the ``audit_logs`` class, and only past the cut-off;
  no retention class or duration is added.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, update

from app.approvals.models import Approval
from app.briefs.models import RecommendationBrief
from app.campaign_context.models import CampaignLocation
from app.core.actor_snapshot import ERASED_ACTOR_SNAPSHOT
from app.core.config import Settings
from app.creative.models import CreativeDraft
from app.exports.models import Export
from app.notifications.models import Notification, NotificationPreference
from app.privacy import retention
from app.privacy.inventory import TABLES, Category, RetentionClass
from app.privacy.retention import _ROOTS
from app.privacy.verification import verify_organization_erased, verify_subject_erased
from app.tests._privacy_support import (
    ALICE,
    API,
    BOB,
    CAROL,
    DAVE,
    EMAIL,
    NAME,
    ORG_A,
    ORG_B,
    PASSWORD,
    WORKSPACES,
    WS_A,
    WS_B,
    PrivacyEnv,
    privacy_world,
)
from app.tests.test_cf1_phase5_privacy import _brief, _by_id, _snapshot
from app.tests.test_cf2_creative_privacy import DRAFT_TEXT, _draft


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "cf3-privacy.db") as e:
        yield e


def _approval(row_id: str, *, org: str, ws: str, draft: str, decider: str) -> dict:
    return dict(
        id=row_id, organization_id=org, workspace_id=ws, subject_type="creative_draft",
        subject_id=draft, subject_version=1, draft_id=draft, decision="approved",
        decision_seq=1, decided_by=decider, decided_by_snapshot=_snapshot(decider),
        decided_role="reviewer", is_simulated=False,
    )


def _export(row_id: str, *, org: str, ws: str, draft: str, approval: str, exporter: str) -> dict:
    return dict(
        id=row_id, organization_id=org, workspace_id=ws, draft_id=draft, draft_version=1,
        approval_id=approval, format="text/markdown", jurisdiction_code="US-TX",
        exported_by=exporter, exported_by_snapshot=_snapshot(exporter), is_simulated=True,
    )


def _notification(row_id: str, *, user: str, subject: str, code: str) -> dict:
    return dict(
        id=row_id, organization_id=ORG_A, workspace_id=WS_A, user_id=user, event_code=code,
        subject_id=subject,
    )


def _add_rows(env: PrivacyEnv) -> None:
    with env.engine.begin() as conn:
        conn.execute(
            insert(RecommendationBrief.__table__),
            [
                _brief("cf3-brief-a", ws=WS_A, org=ORG_A, tag="a1", author=CAROL),
                _brief("cf3-brief-b", ws=WS_B, org=ORG_B, tag="b1", author=BOB),
            ],
        )
        conn.execute(
            insert(CreativeDraft.__table__),
            [
                _draft("cf3-draft-a", org=ORG_A, ws=WS_A, brief="cf3-brief-a", tag="a1",
                       author=CAROL),
                _draft("cf3-draft-b", org=ORG_B, ws=WS_B, brief="cf3-brief-b", tag="b1",
                       author=BOB),
            ],
        )
        conn.execute(
            insert(Approval.__table__),
            [
                _approval("cf3-appr-a", org=ORG_A, ws=WS_A, draft="cf3-draft-a", decider=DAVE),
                _approval("cf3-appr-b", org=ORG_B, ws=WS_B, draft="cf3-draft-b", decider=BOB),
            ],
        )
        conn.execute(
            insert(Export.__table__),
            [
                _export("cf3-export-a", org=ORG_A, ws=WS_A, draft="cf3-draft-a",
                        approval="cf3-appr-a", exporter=CAROL),
                _export("cf3-export-b", org=ORG_B, ws=WS_B, draft="cf3-draft-b",
                        approval="cf3-appr-b", exporter=BOB),
            ],
        )
        conn.execute(
            insert(Notification.__table__),
            [
                _notification("cf3-note-carol", user=CAROL, subject="cf3-appr-a",
                              code="approval.decided"),
                _notification("cf3-note-dave", user=DAVE, subject="cf3-export-a",
                              code="export.completed"),
            ],
        )
        conn.execute(
            insert(NotificationPreference.__table__).values(
                id="cf3-pref-carol", organization_id=ORG_A, workspace_id=WS_A, user_id=CAROL,
                event_code="draft.generated", enabled=False,
            )
        )
        conn.execute(
            insert(CampaignLocation.__table__),
            [
                dict(id=f"cf3-member-{tag}", organization_id=org, workspace_id=ws,
                     campaign_id=f"{tag}-campaign", location_id=f"{tag}-loc")
                for tag, org, ws in (("a1", ORG_A, WS_A), ("b1", ORG_B, WS_B))
            ],
        )


# --------------------------------------------------------------------------- #
# Account deletion
# --------------------------------------------------------------------------- #
def test_account_deletion_keeps_the_export_and_removes_the_accounts_feed(env):
    _add_rows(env)
    with env.session() as s:
        before = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert {"exports", "notifications", "notification_preferences"} <= {
        f.table for f in before.reference_findings
    }
    assert "exports" in {f.table for f in before.content_findings}
    bob_export = _by_id(env, Export)["cf3-export-b"]

    r = env.client.post(
        f"{API}/account/delete",
        headers=env.auth(CAROL),
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )

    assert r.status_code == 200, r.text
    counts = r.json()["counts"]
    assert counts["anonymised"]["exports"] == 1
    assert counts["deleted"]["notifications"] == 1
    assert counts["deleted"]["notification_preferences"] == 1
    export = _by_id(env, Export)["cf3-export-a"]
    assert (export["exported_by"], export["exported_by_snapshot"]) == (
        None, ERASED_ACTOR_SNAPSHOT
    )
    # The compliance record itself is kept (O-6): what was exported, how, where.
    assert (export["draft_id"], export["approval_id"], export["jurisdiction_code"]) == (
        "cf3-draft-a", "cf3-appr-a", "US-TX"
    )
    assert set(_by_id(env, Notification)) == {"cf3-note-dave"}
    assert _by_id(env, NotificationPreference) == {}
    assert _by_id(env, Export)["cf3-export-b"] == bob_export
    with env.session() as s:
        after = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert after.verified, after.as_dict()


# --------------------------------------------------------------------------- #
# Organization deletion
# --------------------------------------------------------------------------- #
def test_organization_deletion_retains_exports_anonymised_and_deletes_the_rest(env):
    _add_rows(env)
    bob_export = _by_id(env, Export)["cf3-export-b"]

    r = env.client.post(
        f"{API}/organizations/{ORG_A}/delete",
        headers=env.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )

    assert r.status_code == 200, r.text
    receipt = r.json()["counts"]
    assert receipt["anonymised"]["exports"] == 1
    assert receipt["deleted"]["notifications"] == 2
    assert receipt["deleted"]["notification_preferences"] == 1
    # A has two workspaces, each with one campaign membership in this world.
    assert receipt["deleted"]["campaign_locations"] >= 1
    export = _by_id(env, Export)["cf3-export-a"]  # a compliance record: retained
    assert (export["exported_by"], export["draft_id"]) == (None, None)
    assert export["exported_by_snapshot"] == ERASED_ACTOR_SNAPSHOT
    assert (export["organization_id"], export["approval_id"], export["format"]) == (
        ORG_A, "cf3-appr-a", "text/markdown"
    )
    assert _by_id(env, Export)["cf3-export-b"] == bob_export
    assert set(_by_id(env, Notification)) == set()
    assert {r["organization_id"] for r in env.rows(CampaignLocation)} == {ORG_B}
    with env.session() as s:
        full = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A]
        )
    assert full.verified, full.as_dict()
    assert ("exports", "organization_id", 1) in {
        (f.table, f.column, f.rows) for f in full.retained_anonymised
    }


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("exported_by_snapshot", "leftover snapshot"),
        ("exported_by", BOB),  # an existing account, so the foreign key holds
        ("draft_id", "cf3-draft-b"),  # an existing draft, so the foreign key holds
    ],
)
def test_the_verifier_reports_a_retained_export_that_still_identifies(env, column, value):
    """Negative controls for the O-6 exception: retained is not the same as anonymised, for
    every identifying column of a retained export."""
    _add_rows(env)
    r = env.client.post(
        f"{API}/organizations/{ORG_A}/delete",
        headers=env.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )
    assert r.status_code == 200, r.text
    table = Export.__table__
    with env.engine.begin() as conn:
        conn.execute(update(table).where(table.c.id == "cf3-export-a").values({column: value}))
    with env.session() as s:
        report = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A], content=False
        )
    assert not report.verified
    assert ("exports", column) in {(f.table, f.column) for f in report.reference_findings}


# --------------------------------------------------------------------------- #
# Exports (portability)
# --------------------------------------------------------------------------- #
def test_the_account_export_lists_the_accounts_own_exports_and_feed(env):
    _add_rows(env)

    carol = env.client.get(f"{API}/account/export", headers=env.auth(CAROL))

    assert carol.status_code == 200, carol.text
    body = carol.json()
    assert [e["id"] for e in body["exports_performed"]] == ["cf3-export-a"]
    export = body["exports_performed"][0]
    assert set(export) == {
        "id", "organization_id", "workspace_id", "draft_id", "draft_version", "approval_id",
        "format", "jurisdiction_code", "exported_by_snapshot", "created_at",
    }
    assert export["exported_by_snapshot"] == _snapshot(CAROL)
    assert [n["id"] for n in body["notifications_received"]] == ["cf3-note-carol"]
    assert set(body["notifications_received"][0]) == {
        "id", "organization_id", "workspace_id", "event_code", "subject_id", "read_at",
        "deleted_at", "created_at",
    }
    assert [p["id"] for p in body["notification_preferences"]] == ["cf3-pref-carol"]
    assert body["notification_preferences"][0]["enabled"] is False
    blob = json.dumps(body)
    assert DRAFT_TEXT not in blob  # the exported text is the draft's, not the account's
    for other in (BOB, DAVE):
        assert _snapshot(other) not in blob
    dave = env.client.get(f"{API}/account/export", headers=env.auth(DAVE)).json()
    assert dave["exports_performed"] == []
    assert [n["id"] for n in dave["notifications_received"]] == ["cf3-note-dave"]
    assert dave["notification_preferences"] == []
    bob = env.client.get(f"{API}/account/export", headers=env.auth(BOB)).json()
    assert [e["id"] for e in bob["exports_performed"]] == ["cf3-export-b"]
    assert bob["notifications_received"] == []


def test_the_organization_export_carries_exports_and_memberships_not_member_feeds(env):
    _add_rows(env)

    r = env.client.get(f"{API}/organizations/{ORG_A}/export", headers=env.auth(ALICE))

    assert r.status_code == 200, r.text
    body = r.json()
    tables = body["tables"]
    assert [row["id"] for row in tables["exports"]["rows"]] == ["cf3-export-a"]
    assert {row["organization_id"] for row in tables["campaign_locations"]["rows"]} == {ORG_A}
    assert "notifications" not in tables and "notification_preferences" not in tables
    for name in ("notifications", "notification_preferences"):
        page = env.client.get(
            f"{API}/organizations/{ORG_A}/export/{name}", headers=env.auth(ALICE)
        )
        assert page.status_code == 404, page.text


# --------------------------------------------------------------------------- #
# Retention and inventory
# --------------------------------------------------------------------------- #
def test_the_audit_logs_class_purges_old_exports_only(env):
    _add_rows(env)
    now = datetime.now(UTC)
    table = Export.__table__
    with env.engine.begin() as conn:
        conn.execute(
            update(table)
            .where(table.c.id == "cf3-export-a")
            .values(created_at=now - timedelta(days=400))
        )

    with env.session() as s:
        result = retention.purge_batch(
            s, RetentionClass.AUDIT_LOGS, now - timedelta(days=30), batch_size=500
        )
        s.commit()

    assert result.counts.deleted["exports"] == 1
    assert set(_by_id(env, Export)) == {"cf3-export-b"}  # newer: kept
    assert {"cf3-appr-a", "cf3-appr-b"} <= set(_by_id(env, Approval))  # not old: kept
    assert {"cf3-draft-a", "cf3-draft-b"} <= set(_by_id(env, CreativeDraft))


def test_inventory_of_the_cf3_tables():
    assert TABLES["exports"].category is Category.COMPLIANCE
    assert TABLES["exports"].exported is True
    assert TABLES["exports"].retention_class is RetentionClass.AUDIT_LOGS
    assert Export in {model for model, _ in _ROOTS[RetentionClass.AUDIT_LOGS]}
    for name in ("notifications", "notification_preferences"):
        assert TABLES[name].category is Category.ACCOUNT
        assert TABLES[name].exported is False
        assert TABLES[name].retention_class is None  # undecided (O-5): no class, no duration
    assert TABLES["campaign_locations"].category is Category.TENANT_CONTENT
    assert TABLES["campaign_locations"].retention_class is None
    # No new retention class and no duration was decided: still seven unset settings.
    assert len(RetentionClass) == 7
    settings = Settings(_env_file=None)
    assert all(getattr(settings, c.setting_name) is None for c in RetentionClass)
