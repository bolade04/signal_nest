"""Shared seeding for the Phase 5B approval test modules (W21 lane P5B). Not a test module.

The 5A brief service is not on this lane's base, so every brief here is a direct model
insert (lane brief: "create brief rows in tests by direct model inserts"), on a complete
evidence chain -- brand, scout request, raw and normalized signal, opportunity and the
immutable intelligence record the brief is bound to -- so SQLite with foreign keys on and
PostgreSQL accept every row exactly as the 5A service will write it. Every id fits its
``String(32)`` column.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.brands.models import Brand
from app.briefs.models import RecommendationBrief
from app.core.actor_snapshot import actor_snapshot
from app.core.enums import Role
from app.intelligence.records import SignalIntelligenceRecord
from app.opportunities.models import Opportunity
from app.organizations.models import Organization, OrganizationMember, User, Workspace
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, RawSignal

_ID_MAX = 32


def fit(value: str) -> str:
    """``value`` unless it cannot fit a ``String(32)`` id column (PostgreSQL would refuse)."""
    if len(value) > _ID_MAX:
        raise AssertionError(f"fixture id {value!r} is {len(value)} characters (max {_ID_MAX})")
    return value


def add_org(s: Session, org_id: str, *workspace_ids: str) -> None:
    s.add(Organization(id=fit(org_id), name=f"Org {org_id}", slug=fit(f"s-{org_id}")))
    s.flush()
    for ws_id in workspace_ids:
        s.add(
            Workspace(
                id=fit(ws_id), organization_id=org_id, name=f"WS {ws_id}", slug=fit(f"s-{ws_id}")
            )
        )
    s.flush()


def add_user(s: Session, user_id: str, *, org_id: str | None = None, role: Role | None = None):
    user = User(
        id=fit(user_id),
        email=f"{user_id}@example.test",
        full_name=f"Name {user_id}",
        hashed_password="x",
        is_active=True,
    )
    s.add(user)
    s.flush()
    if org_id is not None:
        assert role is not None
        s.add(
            OrganizationMember(
                id=fit(f"m-{user_id}"), organization_id=org_id, user_id=user.id, role=role.value
            )
        )
        s.flush()
    return user


@dataclass(frozen=True)
class Evidence:
    org_id: str
    ws_id: str
    opportunity_id: str
    record_id: str


def add_evidence(s: Session, *, org_id: str, ws_id: str, key: str) -> Evidence:
    """One opportunity and its immutable intelligence record in ``ws_id``."""
    brand = f"br-{key}"
    if s.get(Brand, brand) is None:
        s.add(Brand(id=fit(brand), organization_id=org_id, workspace_id=ws_id, name=f"B {key}"))
        s.flush()
    request = fit(f"sr-{key}")
    s.add(
        ScoutRequest(
            id=request,
            organization_id=org_id,
            workspace_id=ws_id,
            brand_id=brand,
            name=f"scout {key}",
        )
    )
    s.flush()
    s.add(
        RawSignal(
            id=fit(f"raw-{key}"),
            organization_id=org_id,
            workspace_id=ws_id,
            scout_request_id=request,
            source_type="rss_news",
            content="c",
        )
    )
    s.flush()
    s.add(
        NormalizedSignal(
            id=fit(f"ns-{key}"),
            organization_id=org_id,
            workspace_id=ws_id,
            scout_request_id=request,
            raw_signal_id=f"raw-{key}",
            source_type="rss_news",
            excerpt="e",
            content_hash=f"h-{key}",
        )
    )
    s.flush()
    opportunity = fit(f"opp-{key}")
    s.add(
        Opportunity(
            id=opportunity,
            organization_id=org_id,
            workspace_id=ws_id,
            brand_id=brand,
            scout_request_id=request,
            title=f"Opp {key}",
            classification="emerging",
            decision="monitor",
            opportunity_score=50,
        )
    )
    s.flush()
    record = fit(f"sir-{key}")
    s.add(
        SignalIntelligenceRecord(
            id=record,
            organization_id=org_id,
            workspace_id=ws_id,
            scout_request_id=request,
            normalized_signal_id=f"ns-{key}",
            opportunity_id=opportunity,
            analysis_version="3c",
            scoring_version="3c.1",
            fingerprint=f"fp-{key}",
            enricher="deterministic",
            accepted=True,
            classification="emerging",
        )
    )
    s.flush()
    return Evidence(org_id=org_id, ws_id=ws_id, opportunity_id=opportunity, record_id=record)


def add_brief(
    s: Session,
    evidence: Evidence,
    *,
    brief_id: str,
    author: User | None,
    version: int = 1,
    parent_id: str | None = None,
    snapshot: str | None = None,
    is_simulated: bool = True,
    created_by: str | None = "author",
) -> RecommendationBrief:
    """One immutable brief version, as the 5A service will write it.

    ``author`` sets ``created_by`` and the snapshot; ``snapshot`` overrides the snapshot and
    ``created_by=None`` models an author whose foreign key was nulled.
    """
    if snapshot is None:
        snapshot = (
            "[erased]"
            if author is None
            else actor_snapshot(user_id=author.id, full_name=author.full_name, email=author.email)
        )
    brief = RecommendationBrief(
        id=fit(brief_id),
        organization_id=evidence.org_id,
        workspace_id=evidence.ws_id,
        opportunity_id=evidence.opportunity_id,
        intelligence_record_id=evidence.record_id,
        brief_version=version,
        parent_brief_id=parent_id,
        analysis_version="3c",
        scoring_version="3c.1",
        prompt_name="recommendation_brief",
        prompt_version="1.0.0",
        model_identifier="mock-model",
        input_hash="0" * 64,
        trace_id=None,
        is_simulated=is_simulated,
        created_by=None if (author is None or created_by is None) else author.id,
        created_by_snapshot=snapshot,
        summary=f"Summary of {brief_id}.",
        rationale=f"Rationale of {brief_id}.",
        recommended_actions=["Act on it."],
        suggested_angles=["An angle."],
        risk_notes=[],
        evidence_refs=[evidence.record_id],
    )
    s.add(brief)
    s.flush()
    return brief


__all__ = ["Evidence", "add_brief", "add_evidence", "add_org", "add_user", "fit"]
