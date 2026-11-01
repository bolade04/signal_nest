"""Project Phase 5 CF-1 (W21): erasure of the actor snapshots, per operator decision O-6.

O-6 (``REC_compliance_records_survive_FKless_scope_columns``) as Phase 6 already encodes it
for ``audit_logs``: a compliance record **survives**, and every value naming an erased
account (or a deleted organization) is cleared. Built on the P6-PRIV-1 world
(``_privacy_support``), which predates these tables, so each test adds its own briefs and
approvals in organization A before acting:

* **Account deletion (CAROL).** Her brief and her approval are kept; their snapshots become
  ``"[erased]"`` and the foreign keys are nulled; DAVE's approval keeps DAVE's snapshot but
  loses the note that names CAROL's address. The receipt counts the anonymised rows, and the
  independent verifier (full content scan) finds nothing.
* **Organization deletion (A).** A's briefs are deleted with its workspaces; A's approvals
  are retained with every identifying column cleared and reported as retained by the
  verifier; B's approval is untouched. Before the deletion the verifier would flag a
  non-anonymised retained approval (negative control).
* **Inventory and retention.** Both tables are inventoried; approvals share the
  ``audit_logs`` retention class (no new duration setting) and its purge removes an approval
  past the cut-off and keeps a newer one and the brief (review r1 DATA-C3); the erased
  markers agree.
* **Account export** (review r1 DATA-C2). The account's export lists the briefs it authored
  and the approvals it decided — its own ids, versions, decision, reason, note and snapshot —
  and never the brief's model-derived content or another person's identity.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import insert, update

from app.approvals.models import Approval
from app.briefs.models import RecommendationBrief
from app.core.actor_snapshot import ERASED_ACTOR_SNAPSHOT, actor_snapshot
from app.core.config import Settings
from app.privacy import erasure, retention, verification
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


@pytest.fixture
def env(tmp_path) -> Iterator[PrivacyEnv]:
    with privacy_world(tmp_path / "cf1-privacy.db") as e:
        yield e


def _snapshot(user_id: str) -> str:
    return actor_snapshot(user_id=user_id, full_name=f"Name {user_id}", email=EMAIL[user_id])


def _brief(row_id: str, *, ws: str, org: str, tag: str, author: str, version: int = 1) -> dict:
    return dict(
        id=row_id, organization_id=org, workspace_id=ws, opportunity_id=f"{tag}-opp",
        intelligence_record_id=f"{tag}-intel", brief_version=version, analysis_version="1",
        scoring_version="1", prompt_name="recommendation_brief", prompt_version="1.0.0",
        model_identifier="mock", input_hash="0" * 64, is_simulated=True, created_by=author,
        created_by_snapshot=_snapshot(author), summary="s", rationale="r",
        recommended_actions=[], suggested_angles=[], risk_notes=[], evidence_refs=[],
    )


def _approval(row_id: str, *, org: str, ws: str, brief: str, decider: str, seq: int,
              note: str | None = None) -> dict:
    return dict(
        id=row_id, organization_id=org, workspace_id=ws, subject_type="recommendation_brief",
        subject_id=brief, subject_version=1, brief_id=brief, decision="approved",
        decision_seq=seq, reason_code=None, note=note, decided_by=decider,
        decided_by_snapshot=_snapshot(decider), decided_role="reviewer", is_simulated=False,
    )


def _add_phase5_rows(env: PrivacyEnv) -> None:
    """A brief by CAROL in A (ws a1), approved by CAROL and by DAVE (whose note names CAROL's
    address); a brief by BOB in B, approved by BOB. Inserted through Core, as a fixture."""
    with env.engine.begin() as conn:
        conn.execute(
            insert(RecommendationBrief.__table__),
            [
                _brief("cf1-brief-a", ws=WS_A, org=ORG_A, tag="a1", author=CAROL),
                _brief("cf1-brief-b", ws=WS_B, org=ORG_B, tag="b1", author=BOB),
            ],
        )
        conn.execute(
            insert(Approval.__table__),
            [
                _approval("cf1-appr-carol", org=ORG_A, ws=WS_A, brief="cf1-brief-a",
                          decider=CAROL, seq=1),
                _approval("cf1-appr-dave", org=ORG_A, ws=WS_A, brief="cf1-brief-a",
                          decider=DAVE, seq=2, note=f"Agreed with {EMAIL[CAROL]} on a call"),
                _approval("cf1-appr-bob", org=ORG_B, ws=WS_B, brief="cf1-brief-b",
                          decider=BOB, seq=1, note="fine"),
            ],
        )


def _by_id(env: PrivacyEnv, model) -> dict[str, dict]:
    return {r["id"]: r for r in env.rows(model)}


# --------------------------------------------------------------------------- #
# Account deletion
# --------------------------------------------------------------------------- #
def test_account_deletion_keeps_the_records_and_clears_what_names_the_account(env):
    _add_phase5_rows(env)
    with env.session() as s:
        before = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert {"recommendation_briefs", "approvals"} <= {f.table for f in before.reference_findings}
    assert {"recommendation_briefs", "approvals"} <= {f.table for f in before.content_findings}
    bob_before = _by_id(env, Approval)["cf1-appr-bob"]

    r = env.client.post(
        f"{API}/account/delete",
        headers=env.auth(CAROL),
        json={"current_password": PASSWORD, "confirmation": "DELETE"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["counts"]["anonymised"] == {
        "approvals": 2, "audit_logs": 5, "recommendation_briefs": 1,
    }
    brief = _by_id(env, RecommendationBrief)["cf1-brief-a"]
    assert brief["created_by"] is None
    assert brief["created_by_snapshot"] == ERASED_ACTOR_SNAPSHOT
    approvals = _by_id(env, Approval)
    assert set(approvals) == {"cf1-appr-carol", "cf1-appr-dave", "cf1-appr-bob"}  # all kept
    carol = approvals["cf1-appr-carol"]
    assert (carol["decided_by"], carol["decided_by_snapshot"]) == (None, ERASED_ACTOR_SNAPSHOT)
    assert (carol["decision"], carol["decided_role"], carol["brief_id"]) == (
        "approved", "reviewer", "cf1-brief-a"
    )
    dave = approvals["cf1-appr-dave"]
    assert dave["decided_by"] == DAVE and dave["decided_by_snapshot"] == _snapshot(DAVE)
    assert dave["note"] is None  # it named CAROL's address
    assert approvals["cf1-appr-bob"] == bob_before
    with env.session() as s:
        after = verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])
    assert after.verified, after.as_dict()


# --------------------------------------------------------------------------- #
# Organization deletion
# --------------------------------------------------------------------------- #
def test_organization_deletion_retains_approvals_anonymised(env):
    _add_phase5_rows(env)
    b_before = _by_id(env, Approval)["cf1-appr-bob"]

    r = env.client.post(
        f"{API}/organizations/{ORG_A}/delete",
        headers=env.auth(ALICE),
        json={"current_password": PASSWORD, "confirm_organization_name": NAME[ORG_A]},
    )

    assert r.status_code == 200, r.text
    receipt = r.json()
    assert receipt["counts"]["anonymised"]["approvals"] == 2
    assert receipt["counts"]["deleted"]["recommendation_briefs"] == 1
    assert {"table": "approvals", "column": "organization_id", "rows": 2} in receipt[
        "verification"
    ]["retained_anonymised"]
    assert receipt["verification"]["references_cleared"] is True
    assert set(_by_id(env, RecommendationBrief)) == {"cf1-brief-b"}
    approvals = _by_id(env, Approval)
    for row_id in ("cf1-appr-carol", "cf1-appr-dave"):
        row = approvals[row_id]
        assert row["organization_id"] == ORG_A and row["workspace_id"] == WS_A  # kept (O-6)
        assert (row["decided_by"], row["note"], row["brief_id"]) == (None, None, None)
        assert row["decided_by_snapshot"] == ERASED_ACTOR_SNAPSHOT
        assert (row["subject_id"], row["decision"]) == ("cf1-brief-a", "approved")
    assert approvals["cf1-appr-bob"] == b_before
    with env.session() as s:
        full = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A]
        )
    assert full.verified, full.as_dict()


def test_the_verifier_flags_a_retained_approval_that_was_not_anonymised(env):
    """Negative control: a retained approval of a deleted organization that still names its
    decider is a finding, not a pass."""
    _add_phase5_rows(env)
    with env.session() as s:
        erasure.erase_organization(s, organization_id=ORG_A)
        s.commit()
    with env.engine.begin() as conn:
        table = Approval.__table__
        conn.execute(
            update(table)
            .where(table.c.id == "cf1-appr-dave")
            .values(decided_by_snapshot=_snapshot(DAVE), note="still here")
        )
    with env.session() as s:
        report = verify_organization_erased(
            s, organization_id=ORG_A, workspace_ids=WORKSPACES[ORG_A], content=False
        )
    assert not report.verified
    flagged = {(f.table, f.column) for f in report.reference_findings}
    assert flagged == {("approvals", "decided_by_snapshot"), ("approvals", "note")}


# --------------------------------------------------------------------------- #
# Inventory, retention, markers
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# Account export (review r1 DATA-C2)
# --------------------------------------------------------------------------- #
def test_the_account_export_lists_the_accounts_briefs_and_decisions_only(env):
    _add_phase5_rows(env)

    carol = env.client.get(f"{API}/account/export", headers=env.auth(CAROL))

    assert carol.status_code == 200, carol.text
    body = carol.json()
    assert [b["id"] for b in body["briefs_authored"]] == ["cf1-brief-a"]
    brief = body["briefs_authored"][0]
    assert brief["created_by_snapshot"] == _snapshot(CAROL)
    assert brief["brief_version"] == 1 and brief["opportunity_id"] == "a1-opp"
    for content in ("summary", "rationale", "recommended_actions", "evidence_refs",
                    "input_hash", "trace_id", "llm_usage_event_id", "created_by"):
        assert content not in brief
    assert [a["id"] for a in body["approvals_decided"]] == ["cf1-appr-carol"]
    decision = body["approvals_decided"][0]
    assert (decision["decision"], decision["decided_role"]) == ("approved", "reviewer")
    assert decision["decided_by_snapshot"] == _snapshot(CAROL)
    text = json.dumps(body)
    # DAVE's decision (whose note names CAROL) and BOB's are not CAROL's records.
    for other in (ALICE, BOB, DAVE):
        assert EMAIL[other] not in text and _snapshot(other) not in text

    dave = env.client.get(f"{API}/account/export", headers=env.auth(DAVE)).json()
    assert dave["briefs_authored"] == []
    assert [a["id"] for a in dave["approvals_decided"]] == ["cf1-appr-dave"]
    alice = env.client.get(f"{API}/account/export", headers=env.auth(ALICE)).json()
    assert alice["briefs_authored"] == [] and alice["approvals_decided"] == []


# --------------------------------------------------------------------------- #
# Retention: approvals purge with the audit_logs class (review r1 DATA-C3)
# --------------------------------------------------------------------------- #
def test_the_audit_logs_class_purges_old_approvals_only_and_leaves_the_brief(env):
    _add_phase5_rows(env)
    now = datetime.now(UTC)
    table = Approval.__table__
    with env.engine.begin() as conn:
        conn.execute(
            update(table)
            .where(table.c.id == "cf1-appr-carol")
            .values(created_at=now - timedelta(days=400))
        )
    cutoff = now - timedelta(days=30)

    with env.session() as s:
        result = retention.purge_batch(s, RetentionClass.AUDIT_LOGS, cutoff, batch_size=500)
        s.commit()

    assert result.counts.deleted["approvals"] == 1
    approvals = _by_id(env, Approval)
    assert "cf1-appr-carol" not in approvals  # past the cut-off: purged
    assert {"cf1-appr-dave", "cf1-appr-bob"} <= set(approvals)  # newer: kept
    assert set(_by_id(env, RecommendationBrief)) == {"cf1-brief-a", "cf1-brief-b"}
    assert approvals["cf1-appr-dave"]["brief_id"] == "cf1-brief-a"


def test_inventory_and_retention_of_the_two_tables():
    assert TABLES["approvals"].category is Category.COMPLIANCE
    assert TABLES["approvals"].retention_class is RetentionClass.AUDIT_LOGS
    assert TABLES["recommendation_briefs"].category is Category.COLLECTED
    assert TABLES["recommendation_briefs"].retention_class is None  # dependent rows
    assert Approval in {model for model, _ in _ROOTS[RetentionClass.AUDIT_LOGS]}
    # No new retention class and no duration was decided: still seven unset settings.
    assert len(RetentionClass) == 7
    settings = Settings(_env_file=None)
    assert all(getattr(settings, c.setting_name) is None for c in RetentionClass)


def test_the_erased_markers_agree():
    assert ERASED_ACTOR_SNAPSHOT == erasure.ERASED == verification._ERASED_MARKER
