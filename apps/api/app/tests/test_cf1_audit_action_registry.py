"""Project Phase 5 CF-1 (W21): the closed audit-action registry and the single construction seam.

* The registry is closed and exact, and every action string the code wrote before it existed
  is a member with the identical value — the move onto the registry changes no stored or
  newly written ``audit_logs.action`` (behaviour-preserving).
* The one interpolated action (``f"opportunity.{status}"``) is replaced by one member per
  ``OpportunityStatus``, with the same five strings; every ``ApprovalDecision`` has its action.
* ``record_audit`` admits only registry members and writes the member's string; anything else
  is refused before a row exists.
* Single seam (AST, production code only): ``AuditLog`` is constructed only in
  ``app/audit/service.py``; no production module inserts into ``audit_logs`` any other way; and
  no ``record_audit`` call passes a literal or interpolated string as its action.
"""

from __future__ import annotations

import ast
import dataclasses
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker

from app.audit.actions import (
    APPROVAL_DECISION_ACTIONS,
    AUDIT_ACTION_REGISTRY,
    OPPORTUNITY_STATUS_ACTIONS,
    AuditAction,
    UnknownAuditActionError,
    audit_action,
    opportunity_status_action,
)
from app.audit.models import AuditLog
from app.audit.service import record_audit
from app.core.enums import ApprovalDecision, OpportunityStatus
from app.db.models import Base

API_DIR = Path(__file__).resolve().parents[2]

#: Every action string production code wrote before the registry (counted at main 102393fa:
#: 29 ``record_audit`` call sites, 33 distinct strings once the interpolated
#: ``opportunity.{status}`` is expanded over the five statuses).
PRE_REGISTRY = frozenset(
    {
        "auth.register",
        "onboarding.completed",
        "business_profile.updated",
        "organization_member.role_changed",
        "organization_member.removed",
        "organization_invitation.created",
        "organization_invitation.accepted",
        "organization_invitation.revoked",
        "location.created",
        "location.deactivated",
        "location.reactivated",
        "geo_coverage.updated",
        "scout_request.created",
        "scout_request.updated",
        "scout_request.paused",
        "scout_request.resumed",
        "scout_request.run",
        "scout_schedule.created",
        "scout_schedule.paused",
        "scout_schedule.resumed",
        "scout_schedule.deleted",
        "scout_admission.reconciled",
        "opportunity.new",
        "opportunity.saved",
        "opportunity.monitoring",
        "opportunity.ignored",
        "opportunity.actioned",
        "opportunity_feedback.created",
        "workspace_capability_override.created",
        "workspace_capability_override.updated",
        "workspace_capability_override.cleared",
        "workspace_capability_override.rejected",
        "data_source.taken_down",
    }
)
#: Reserved at CF-1 for the 5A/5B build lanes (vocabulary only).
RESERVED = frozenset(
    {
        "auth.login_succeeded",
        "auth.login_failed",
        "approval.approved",
        "approval.rejected",
        "approval.revoked",
        "recommendation_brief.created",
    }
)


def test_the_registry_is_closed_and_exact() -> None:
    assert {a.value for a in AuditAction} == PRE_REGISTRY | RESERVED
    assert len(PRE_REGISTRY) == 33 and len(RESERVED) == 6
    assert set(AUDIT_ACTION_REGISTRY) == set(AuditAction)
    assert list(AUDIT_ACTION_REGISTRY) == list(AuditAction)  # declaration order


def test_registry_metadata_is_frozen_and_read_only() -> None:
    spec = AUDIT_ACTION_REGISTRY[AuditAction.AUTH_REGISTER]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.domain = "x"  # type: ignore[misc]
    with pytest.raises(TypeError):
        AUDIT_ACTION_REGISTRY[AuditAction.AUTH_REGISTER] = spec  # type: ignore[index]
    assert {s.introduced for s in AUDIT_ACTION_REGISTRY.values()} == {"pre-registry", "5A", "5B"}
    reserved = {
        s.action.value for s in AUDIT_ACTION_REGISTRY.values() if s.introduced != "pre-registry"
    }
    assert reserved == RESERVED
    for spec in AUDIT_ACTION_REGISTRY.values():
        assert spec.action.value.startswith(spec.domain + ".")


def test_every_opportunity_status_has_its_former_action_string() -> None:
    assert set(OPPORTUNITY_STATUS_ACTIONS) == set(OpportunityStatus)
    for status in OpportunityStatus:
        assert opportunity_status_action(status).value == f"opportunity.{status.value}"
        assert opportunity_status_action(status.value) is OPPORTUNITY_STATUS_ACTIONS[status]
    with pytest.raises(ValueError):
        opportunity_status_action("reviewed")


def test_every_approval_decision_has_an_action() -> None:
    assert set(APPROVAL_DECISION_ACTIONS) == set(ApprovalDecision)
    for decision, action in APPROVAL_DECISION_ACTIONS.items():
        assert action.value == f"approval.{decision.value}"


def test_audit_action_conversion_is_strict() -> None:
    assert audit_action("auth.register") is AuditAction.AUTH_REGISTER
    for bad in ("", "note.created", "opportunity.reviewed", "AUTH.REGISTER", "auth.register "):
        with pytest.raises(UnknownAuditActionError):
            audit_action(bad)
    assert issubclass(UnknownAuditActionError, ValueError)


# --------------------------------------------------------------------------- #
# record_audit: the seam admits only members and stores the same string
# --------------------------------------------------------------------------- #
@pytest.fixture
def session() -> Iterator[Session]:
    engine = create_engine("sqlite://", future=True)
    Base.metadata.create_all(engine)
    with sessionmaker(bind=engine, future=True)() as s:
        yield s
    engine.dispose()


def test_record_audit_stores_the_member_string(session: Session) -> None:
    log = record_audit(
        session, organization_id="org-1", action=AuditAction.SCOUT_REQUEST_RUN, workspace_id="w"
    )
    session.commit()
    stored = session.execute(select(AuditLog.__table__.c.action)).scalar_one()
    assert stored == "scout_request.run" and type(stored) is str
    assert log.action == "scout_request.run"


@pytest.mark.parametrize("bad", ["opportunity.reviewed", "note.created", "", "auth.login"])
def test_record_audit_refuses_an_unregistered_action_before_writing(
    session: Session, bad: str
) -> None:
    with pytest.raises(UnknownAuditActionError):
        record_audit(session, organization_id="org-1", action=bad)  # type: ignore[arg-type]
    assert session.scalar(select(func.count()).select_from(AuditLog)) == 0


# --------------------------------------------------------------------------- #
# The single construction seam (production code only)
# --------------------------------------------------------------------------- #
def _production_modules() -> Iterator[tuple[Path, ast.AST]]:
    for path in sorted((API_DIR / "app").rglob("*.py")):
        rel = path.relative_to(API_DIR)
        if "tests" in rel.parts:
            continue
        yield rel, ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _called_name(node: ast.Call) -> str | None:
    return getattr(node.func, "id", getattr(node.func, "attr", None))


def test_audit_log_is_constructed_only_inside_record_audit() -> None:
    sites = [
        f"{rel}:{node.lineno}"
        for rel, tree in _production_modules()
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node) == "AuditLog"
    ]
    assert len(sites) == 1 and sites[0].startswith("app/audit/service.py:"), sites


def test_no_production_module_inserts_audit_rows_another_way() -> None:
    offenders = []
    for rel, tree in _production_modules():
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and _called_name(node) == "insert"
                and any(
                    isinstance(arg, ast.Name) and arg.id == "AuditLog"
                    or isinstance(arg, ast.Attribute)
                    and isinstance(arg.value, ast.Name)
                    and arg.value.id == "AuditLog"
                    for arg in node.args
                )
            ):
                offenders.append(f"{rel}:{node.lineno}")
        text = (API_DIR / rel).read_text(encoding="utf-8")
        if "INSERT INTO audit_logs" in text.upper().replace("\n", " "):
            offenders.append(str(rel))
    assert offenders == []


def test_no_record_audit_call_passes_a_string_action() -> None:
    calls, offenders = 0, []
    for rel, tree in _production_modules():
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and _called_name(node) == "record_audit"):
                continue
            calls += 1
            action = next((k.value for k in node.keywords if k.arg == "action"), None)
            if action is None or isinstance(action, ast.Constant | ast.JoinedStr | ast.BinOp):
                offenders.append(f"{rel}:{node.lineno}")
    assert offenders == []
    # 29 call sites at main 102393fa, all moved; a lower count would mean the scan broke.
    assert calls >= 29
