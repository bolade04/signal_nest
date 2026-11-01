"""Project Phase 5 CF-1 (W21): the append-only models refuse ORM updates and deletes.

* ``recommendation_briefs`` and ``approvals`` (new at CF-1) and ``audit_logs`` (the 5B audit
  spine) are :func:`~app.db.append_only.append_only`: a flush that would UPDATE or DELETE one
  of their rows raises :class:`~app.db.append_only.AppendOnlyViolation`, the transaction is
  rolled back, and the stored row is unchanged.
* Exactly these three models are append-only, and the two new tables carry ``created_at``
  only — no ``updated_at`` and no ``onupdate`` (Phase 5A plan §2).
* This module covers the unit of work. Every other ORM path (ORM UPDATE/DELETE statements,
  ``Query.update/delete``, the bulk API, upserts) is covered, on SQLite and PostgreSQL, by
  ``test_cf1_append_only_bulk_paths.py``, which also pins statically that no production
  module outside ``app/privacy/`` writes these rows after insert.
* The sanctioned Core paths (privacy anonymisation, the foreign-key walk) still reach the
  rows, by design (O-10; ``app.db.append_only``).

The briefs test is the target of the CF-1 mutation check: with the guard removed from
``RecommendationBrief`` the update succeeds silently and this module fails.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, delete, select, update
from sqlalchemy.orm import Session, sessionmaker

from app.approvals.models import Approval
from app.audit.actions import AuditAction
from app.audit.models import AuditLog
from app.audit.service import record_audit
from app.briefs.models import RecommendationBrief
from app.core.actor_snapshot import actor_snapshot
from app.db.append_only import AppendOnlyViolation, is_append_only
from app.db.models import Base

SNAPSHOT = actor_snapshot(user_id="u-author", full_name="Ada Author", email="ada@example.test")


@pytest.fixture
def session() -> Iterator[Session]:
    # A private in-memory schema built from the models (foreign keys are not enforced on
    # this connection, so a row needs no parents: the subject here is the ORM guard).
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        yield s
    engine.dispose()


def _brief(**overrides) -> RecommendationBrief:
    values = dict(
        id="brief-1",
        organization_id="org-1",
        workspace_id="ws-1",
        opportunity_id="opp-1",
        intelligence_record_id="intel-1",
        brief_version=1,
        analysis_version="1",
        scoring_version="1",
        prompt_name="recommendation_brief",
        prompt_version="1.0.0",
        model_identifier="mock-model",
        input_hash="0" * 64,
        is_simulated=True,
        created_by="u-author",
        created_by_snapshot=SNAPSHOT,
        summary="A summary.",
        rationale="A rationale.",
    )
    values.update(overrides)
    return RecommendationBrief(**values)


def _approval(**overrides) -> Approval:
    values = dict(
        id="approval-1",
        organization_id="org-1",
        workspace_id="ws-1",
        subject_type="recommendation_brief",
        subject_id="brief-1",
        subject_version=1,
        brief_id="brief-1",
        decision="approved",
        decision_seq=1,
        reason_code="evidence_sufficient",
        decided_by="u-reviewer",
        decided_by_snapshot="u-reviewer Rita Reviewer <rita@example.test>",
        decided_role="reviewer",
    )
    values.update(overrides)
    return Approval(**values)


def _stored(session: Session, model, row_id: str) -> dict:
    table = model.__table__
    row = session.execute(select(*table.c).where(table.c.id == row_id)).one()
    return dict(row._mapping)


# --------------------------------------------------------------------------- #
# recommendation_briefs (the mutation-check target)
# --------------------------------------------------------------------------- #
def test_an_orm_update_of_a_brief_raises_and_changes_nothing(session: Session) -> None:
    session.add(_brief())
    session.commit()
    before = _stored(session, RecommendationBrief, "brief-1")

    brief = session.get(RecommendationBrief, "brief-1")
    brief.summary = "Rewritten in place."
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()

    assert _stored(session, RecommendationBrief, "brief-1") == before


def test_an_orm_delete_of_a_brief_raises(session: Session) -> None:
    session.add(_brief())
    session.commit()
    session.delete(session.get(RecommendationBrief, "brief-1"))
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()
    assert session.get(RecommendationBrief, "brief-1") is not None


def test_a_revision_is_a_new_row(session: Session) -> None:
    session.add(_brief())
    session.commit()
    session.add(_brief(id="brief-2", brief_version=2, parent_brief_id="brief-1"))
    session.commit()  # inserting is the only way to change a brief
    versions = session.scalars(
        select(RecommendationBrief.brief_version).order_by(RecommendationBrief.brief_version)
    ).all()
    assert versions == [1, 2]


# --------------------------------------------------------------------------- #
# approvals
# --------------------------------------------------------------------------- #
def test_an_orm_update_of_an_approval_raises_and_changes_nothing(session: Session) -> None:
    session.add(_approval())
    session.commit()
    before = _stored(session, Approval, "approval-1")

    approval = session.get(Approval, "approval-1")
    approval.decision = "rejected"
    approval.reason_code = "inaccurate"
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()

    assert _stored(session, Approval, "approval-1") == before


def test_an_orm_delete_of_an_approval_raises(session: Session) -> None:
    session.add(_approval())
    session.commit()
    session.delete(session.get(Approval, "approval-1"))
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()
    assert session.get(Approval, "approval-1") is not None


def test_a_change_of_mind_is_a_new_decision_row(session: Session) -> None:
    session.add(_approval())
    session.commit()
    session.add(
        _approval(id="approval-2", decision="revoked", decision_seq=2, reason_code="superseded")
    )
    session.commit()
    latest = session.scalars(
        select(Approval.decision).order_by(Approval.decision_seq.desc()).limit(1)
    ).one()
    assert latest == "revoked"


# --------------------------------------------------------------------------- #
# audit_logs (Phase 5B: the audit table had no ORM-update-raises test)
# --------------------------------------------------------------------------- #
def test_an_orm_update_of_an_audit_record_raises(session: Session) -> None:
    log = record_audit(
        session,
        organization_id="org-1",
        action=AuditAction.LOCATION_CREATED,
        entity_type="business_location",
        entity_id="loc-1",
    )
    session.commit()
    before = _stored(session, AuditLog, log.id)

    log.action = AuditAction.LOCATION_DEACTIVATED.value
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()

    assert _stored(session, AuditLog, log.id) == before


def test_an_orm_delete_of_an_audit_record_raises(session: Session) -> None:
    log = record_audit(session, organization_id="org-1", action=AuditAction.AUTH_REGISTER)
    session.commit()
    session.delete(log)
    with pytest.raises(AppendOnlyViolation):
        session.commit()
    session.rollback()
    assert session.get(AuditLog, log.id) is not None


# --------------------------------------------------------------------------- #
# Shape and scope of the control
# --------------------------------------------------------------------------- #
def test_exactly_the_three_append_only_models_are_guarded() -> None:
    guarded = {
        mapper.class_.__tablename__
        for mapper in Base.registry.mappers
        if is_append_only(mapper.class_)
    }
    assert guarded == {"recommendation_briefs", "approvals", "audit_logs"}


@pytest.mark.parametrize("model", [RecommendationBrief, Approval])
def test_new_append_only_tables_carry_created_at_only(model) -> None:
    columns = model.__table__.c
    assert "created_at" in columns
    assert "updated_at" not in columns
    assert all(column.onupdate is None for column in columns)


def test_the_sanctioned_core_paths_still_reach_the_rows(session: Session) -> None:
    """The guard is ORM-only by design: privacy anonymisation and the foreign-key walk use
    Core statements and must keep working (O-10 records the enforcement level)."""
    session.add(_brief())
    session.add(_approval())
    session.commit()
    table = RecommendationBrief.__table__
    session.execute(
        update(table).where(table.c.id == "brief-1").values(created_by_snapshot="[erased]")
    )
    session.execute(delete(Approval.__table__).where(Approval.__table__.c.id == "approval-1"))
    session.commit()
    assert _stored(session, RecommendationBrief, "brief-1")["created_by_snapshot"] == "[erased]"
    assert session.execute(select(Approval.__table__.c.id)).all() == []
