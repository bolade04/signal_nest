"""P6-PRIV-1 / E10: the independent erasure verifier finds what it must (SQLite).

The verifier is the proof path for E10, so these tests are its positive controls: after a
correct deletion, one remaining copy of the subject is planted at a time — a foreign-key
reference, a reference with no foreign key, a free-text copy, a JSON copy, a copy in other
letter case — and each must turn the report from ``verified`` into a named finding. For an
organization, a retained audit record that still carries an identifying value is a finding,
while correctly anonymised ones are reported as retained and pass.

The verifier reflects the live schema: a table the ORM does not know (created here with raw
DDL) is checked too.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import insert, text, update

from app.audit.models import AuditLog
from app.feedback.models import OpportunityFeedback
from app.jobs.models import Job
from app.privacy.erasure import delete_account, erase_invitee_email, erase_organization
from app.privacy.verification import (
    verify_organization_erased,
    verify_subject_erased,
)
from app.tests._privacy_support import (
    CAROL,
    EMAIL,
    INVITEE,
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
    with privacy_world(tmp_path / "verify.db") as e:
        yield e


@pytest.fixture
def carol_deleted(env: PrivacyEnv) -> PrivacyEnv:
    from app.organizations.models import User

    with env.session() as s:
        delete_account(s, user=s.get(User, CAROL), current_password=PASSWORD)
        s.commit()
    return env


def _account_report(env: PrivacyEnv):
    with env.session() as s:
        return verify_subject_erased(s, user_id=CAROL, email=EMAIL[CAROL])


def _findings(report) -> set[tuple[str, str]]:
    return {(f.table, f.column) for f in (*report.reference_findings, *report.content_findings)}


def test_a_correct_deletion_verifies(carol_deleted: PrivacyEnv):
    report = _account_report(carol_deleted)
    assert report.verified, report.as_dict()
    assert report.tables_checked == 37
    assert report.email_sha256 and EMAIL[CAROL] not in str(report.as_dict())


@pytest.mark.parametrize(
    ("plant", "finding"),
    [
        # A foreign-key column still pointing at the deleted account (a SET NULL that never
        # ran; foreign-key enforcement is switched off on this connection to plant it).
        (
            lambda conn: (
                conn.exec_driver_sql("PRAGMA foreign_keys=OFF"),
                conn.execute(
                    update(OpportunityFeedback.__table__)
                    .where(OpportunityFeedback.id == "b1-feedback")
                    .values(submitted_by_user_id=CAROL)
                ),
            ),
            ("opportunity_feedback", "submitted_by_user_id"),
        ),
        # The id copied into a column with no foreign key.
        (
            lambda conn: conn.execute(
                update(AuditLog.__table__)
                .where(AuditLog.id == "prv-audit-0")
                .values(entity_id=CAROL)
            ),
            ("audit_logs", "entity_id"),
        ),
        # Free text, other letter case.
        (
            lambda conn: conn.execute(
                update(Job.__table__)
                .where(Job.id == "b1-job")
                .values(last_error_summary=f"failed for {EMAIL[CAROL].upper()}")
            ),
            ("jobs", "last_error_summary"),
        ),
        # A JSON payload.
        (
            lambda conn: conn.execute(
                update(Job.__table__)
                .where(Job.id == "a1-job")
                .values(result_summary={"by": CAROL})
            ),
            ("jobs", "result_summary"),
        ),
        # A column NAMED like a user reference, with no foreign key.
        (
            lambda conn: (
                conn.execute(text("CREATE TABLE side_notes (id VARCHAR PRIMARY KEY, "
                                  "author_user_id VARCHAR)")),
                conn.execute(text("INSERT INTO side_notes VALUES ('n1', :u)"), {"u": CAROL}),
            ),
            ("side_notes", "author_user_id"),
        ),
    ],
)
def test_each_planted_copy_is_found(carol_deleted: PrivacyEnv, plant, finding):
    with carol_deleted.engine.begin() as conn:
        plant(conn)
    report = _account_report(carol_deleted)
    assert not report.verified
    assert finding in _findings(report), report.as_dict()


def test_the_email_alone_is_checked_for_an_invitee(env: PrivacyEnv):
    with env.session() as s:
        assert not verify_subject_erased(s, email=INVITEE).verified
        counts = erase_invitee_email(s, email=INVITEE)
        s.commit()
        assert counts.deleted["organization_invitations"] == 1
        assert counts.anonymised["audit_logs"] == 1
        report = verify_subject_erased(s, email=INVITEE)
    assert report.verified, report.as_dict()
    assert report.subject_type == "email"


def test_an_invitee_email_held_by_an_account_is_refused(env: PrivacyEnv):
    from app.core.errors import ConflictError

    with env.session() as s, pytest.raises(ConflictError) as exc:
        erase_invitee_email(s, email=EMAIL[CAROL].upper())
    assert exc.value.code == "erasure_account_exists"


def test_a_subject_is_required():
    with pytest.raises(ValueError):
        verify_subject_erased(object())


# --------------------------------------------------------------------------- #
# Organizations
# --------------------------------------------------------------------------- #
@pytest.fixture
def a_deleted(env: PrivacyEnv) -> PrivacyEnv:
    with env.session() as s:
        erase_organization(s, organization_id=ORG_A)
        s.commit()
    return env


def _org_report(env: PrivacyEnv, workspaces=WORKSPACES[ORG_A]):
    with env.session() as s:
        return verify_organization_erased(s, organization_id=ORG_A, workspace_ids=workspaces)


def test_a_deleted_organization_verifies_with_its_audit_retained(a_deleted: PrivacyEnv):
    report = _org_report(a_deleted)
    assert report.verified, report.as_dict()
    assert [(f.table, f.rows) for f in report.retained_anonymised] == [("audit_logs", 6)]


@pytest.mark.parametrize(
    ("values", "column"),
    [
        ({"actor_user_id": CAROL}, "actor_user_id"),
        ({"reason": "kept by mistake"}, "reason"),
        ({"new_state": {"email": "x"}}, "new_state"),
        ({"context": {"request": "r1"}}, "context"),
    ],
)
def test_a_retained_audit_record_with_an_identifying_value_is_a_finding(
    a_deleted: PrivacyEnv, values, column
):
    with a_deleted.engine.begin() as conn:
        conn.execute(
            update(AuditLog.__table__).where(AuditLog.id == "prv-audit-1").values(**values)
        )
    report = _org_report(a_deleted)
    assert not report.verified
    assert ("audit_logs", column) in _findings(report)


def test_a_workspace_reference_left_behind_is_found(a_deleted: PrivacyEnv):
    with a_deleted.engine.begin() as conn:
        conn.execute(
            insert(Job.__table__).values(
                id="planted-job", organization_id=ORG_B, workspace_id=WS_B, job_type="x",
                payload={"workspace": WS_A}, payload_hash="0" * 64, status="pending",
                contract_version="1", priority=0, attempt_count=0, max_attempts=1,
            )
        )
    report = _org_report(a_deleted)
    assert ("jobs", "payload") in _findings(report)


def test_before_deletion_the_organization_is_found_everywhere(env: PrivacyEnv):
    report = _org_report(env)
    assert not report.verified
    tables = {f.table for f in report.reference_findings}
    assert {"organizations", "workspaces", "raw_signals", "opportunity_scores",
            "signal_clusters", "job_events"} <= tables
