"""Shared harness for the Project Phase 5C build tests (W21 lane P5C). Not a test module.

A real HTTP world on a temporary SQLite file with foreign keys on (the P6-PLAT-8 harness of
``test_campaign_context_integrity``): organization A with one member per role and two
workspaces (A1 acting, A2 a sibling), organization B with one workspace. Briefs are seeded
through the ORM on a real evidence chain (scout request → signal → opportunity →
intelligence record → brief), approvals as 5B rows, the capability through a workspace
override (the global flag stays ``False``), and durable jobs are run by the real worker
(:class:`app.jobs.worker.JobRunner`) against the same database. Mock provider only.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from app.approvals.models import Approval
from app.brands.service import get_primary_brand
from app.briefs.models import RecommendationBrief
from app.campaign_context.models import ClaimsLibraryEntry, OfferCalendarEntry
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.actor_snapshot import actor_snapshot
from app.core.config import get_settings
from app.db.models import Base
from app.db.session import get_db
from app.intelligence.records import SignalIntelligenceRecord
from app.jobs.models import Job
from app.jobs.store import DurableJobStore
from app.jobs.worker import JobRunner
from app.main import app
from app.opportunities.models import Opportunity
from app.organizations.models import User
from app.scouting_requests.models import ScoutRequest
from app.signals.models import NormalizedSignal, RawSignal, SignalCluster
from app.tests.test_campaign_context_integrity import (
    API,
    _engine,
    _invite,
    _rate_limiter,
    _register,
    _workspace,
)

ROLES = ("admin", "marketer", "reviewer", "compliance_reviewer", "viewer")
ALL_ROLES = ("owner", *ROLES)
#: Brief content the mock drafts from (no claim-engine trigger words).
BRIEF_SUMMARY = "Local families are asking for weekend parking near the market."
BRIEF_ACTIONS = ["Share the new weekend parking map with nearby families."]
BRIEF_ANGLES = ["Easy weekend visits", "Parking made simple"]
#: Every ceiling set (CF-2 leaves them unset; generation refuses until all are set).
CEILINGS = {
    "llm_daily_cost_ceiling_usd_global": 50.0,
    "llm_daily_cost_ceiling_usd_per_workspace": 5.0,
    "llm_max_calls_per_workspace_per_day": 20,
    "llm_max_output_tokens_per_call": 600,
}


@dataclass
class CreativeWorld:
    client: TestClient
    engine: Engine
    witness: Engine
    factory: sessionmaker
    org: str = ""
    ws: str = ""
    ws2: str = ""  # a sibling workspace of the same organization
    org_b: str = ""
    ws_b: str = ""
    tokens: dict[str, str] = field(default_factory=dict)  # role -> token (organization A)
    users: dict[str, str] = field(default_factory=dict)  # role -> user id (organization A)
    owner_b: str = ""
    owner_b_id: str = ""

    # -- HTTP ------------------------------------------------------------------
    def send(self, method: str, path: str, role: str = "owner", body=None, *, token=None):
        assert set(app.dependency_overrides) == {get_db}, "only get_db may be overridden"
        _rate_limiter()._hits.clear()
        kwargs: dict = {"headers": {"Authorization": f"Bearer {token or self.tokens[role]}"}}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, API + path, **kwargs)

    # -- database ----------------------------------------------------------------
    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    def row(self, model, row_id: str) -> dict | None:
        table = model.__table__
        with self.witness.connect() as conn:
            r = conn.execute(select(*table.c).where(table.c.id == row_id)).first()
            return dict(r._mapping) if r else None

    def rows(self, model, **where) -> list[dict]:
        table = model.__table__
        stmt = select(*table.c)
        for key, value in where.items():
            stmt = stmt.where(table.c[key] == value)
        with self.witness.connect() as conn:
            return [dict(r._mapping) for r in conn.execute(stmt).all()]

    def count(self, model, **where) -> int:
        table = model.__table__
        stmt = select(func.count()).select_from(table)
        for key, value in where.items():
            stmt = stmt.where(table.c[key] == value)
        with self.witness.connect() as conn:
            return int(conn.execute(stmt).scalar_one())

    def org_of(self, ws: str) -> str:
        return self.org_b if ws == self.ws_b else self.org

    def owner_id(self, ws: str) -> str:
        return self.owner_b_id if ws == self.ws_b else self.users["owner"]

    # -- seeding -------------------------------------------------------------------
    def enable(self, ws: str | None = None, enabled: bool = True) -> None:
        """A workspace override for ``creative_generation`` (the global flag stays off)."""
        ws = ws or self.ws
        with self.session() as s:
            existing = s.scalar(
                select(WorkspaceCapabilityOverride).where(
                    WorkspaceCapabilityOverride.workspace_id == ws,
                    WorkspaceCapabilityOverride.capability == "creative_generation",
                )
            )
            if existing is not None:
                s.delete(existing)
                s.flush()
            s.add(
                WorkspaceCapabilityOverride(
                    organization_id=self.org_of(ws), workspace_id=ws,
                    capability="creative_generation", enabled=enabled,
                    set_by_user_id=self.owner_id(ws), reason="5C test",
                )
            )

    def seed_brief(
        self,
        ws: str | None = None,
        *,
        author_role: str = "marketer",
        approved: bool = True,
        summary: str = BRIEF_SUMMARY,
        version: int = 1,
        opportunity_id: str | None = None,
    ) -> str:
        """One brief on a real evidence chain; approved by the reviewer unless told not."""
        ws = ws or self.ws
        org = self.org_of(ws)
        author = self.owner_id(ws) if ws == self.ws_b else self.users[author_role]
        tag = uuid.uuid4().hex[:12]
        with self.session() as s:
            brand = get_primary_brand(s, ws)
            scoped = {"organization_id": org, "workspace_id": ws}
            if opportunity_id is None:
                s.add(ScoutRequest(id=f"sr{tag}", name="Scout", brand_id=brand.id, **scoped))
                s.flush()
                req = {**scoped, "scout_request_id": f"sr{tag}"}
                s.add(RawSignal(id=f"rs{tag}", source_type="rss", content="parking", **req))
                s.add(SignalCluster(id=f"cl{tag}", label="parking", workspace_id=ws,
                                    scout_request_id=f"sr{tag}"))
                s.flush()
                s.add(NormalizedSignal(id=f"ns{tag}", raw_signal_id=f"rs{tag}",
                                       cluster_id=f"cl{tag}", source_type="rss",
                                       excerpt="parking", content_hash="0" * 64, **req))
                s.flush()
                opportunity_id = f"op{tag}"
                s.add(Opportunity(id=opportunity_id, brand_id=brand.id, title="Parking",
                                  scout_request_id=f"sr{tag}", cluster_id=f"cl{tag}",
                                  classification="x", decision="act_now", **scoped))
                s.flush()
                s.add(SignalIntelligenceRecord(
                    id=f"in{tag}", normalized_signal_id=f"ns{tag}",
                    opportunity_id=opportunity_id, analysis_version="1", scoring_version="1",
                    fingerprint="f" * 64, enricher="test", accepted=True, classification="x",
                    **req,
                ))
                s.flush()
                record_id = f"in{tag}"
            else:
                record_id = s.scalar(
                    select(SignalIntelligenceRecord.id).where(
                        SignalIntelligenceRecord.opportunity_id == opportunity_id
                    )
                )
            user = s.get(User, author)
            brief_id = f"br{tag}"
            s.add(RecommendationBrief(
                id=brief_id, opportunity_id=opportunity_id, intelligence_record_id=record_id,
                brief_version=version, analysis_version="1", scoring_version="1",
                prompt_name="explain_opportunity", prompt_version="1.2.0",
                model_identifier="mock-1", input_hash="0" * 64, is_simulated=True,
                created_by=author,
                created_by_snapshot=actor_snapshot(
                    user_id=user.id, full_name=user.full_name, email=user.email
                ),
                summary=summary, rationale="Inference: demand is forming around parking.",
                recommended_actions=list(BRIEF_ACTIONS), suggested_angles=list(BRIEF_ANGLES),
                risk_notes=[], evidence_refs=[record_id], **scoped,
            ))
        if approved:
            self.decide(brief_id, "approved")
        return brief_id

    def decide(self, brief_id: str, decision: str, *, version: int | None = None) -> None:
        """A 5B decision row (the next ``decision_seq``) by organization A's reviewer."""
        with self.session() as s:
            brief = s.get(RecommendationBrief, brief_id)
            seq = (
                s.scalar(
                    select(func.max(Approval.decision_seq)).where(
                        Approval.subject_id == brief_id
                    )
                )
                or 0
            ) + 1
            reviewer = s.get(User, self.users["reviewer"])
            s.add(Approval(
                organization_id=brief.organization_id, workspace_id=brief.workspace_id,
                subject_type="recommendation_brief", subject_id=brief_id,
                subject_version=version or brief.brief_version, brief_id=brief_id,
                decision=decision, decision_seq=seq,
                reason_code=None,
                decided_by=reviewer.id,
                decided_by_snapshot=actor_snapshot(
                    user_id=reviewer.id, full_name=reviewer.full_name, email=reviewer.email
                ),
                decided_role="reviewer", is_simulated=False,
            ))

    def claim(self, text: str, kind: str = "blocked", *, ws: str | None = None,
              retired: bool = False) -> str:
        ws = ws or self.ws
        with self.session() as s:
            entry = ClaimsLibraryEntry(
                organization_id=self.org_of(ws), workspace_id=ws,
                brand_id=get_primary_brand(s, ws).id, text=text, kind=kind,
            )
            if retired:
                from datetime import UTC, datetime

                entry.retired_at = datetime.now(UTC)
            s.add(entry)
            s.flush()
            return entry.id

    def offer(self, disclaimer: str, *, ws: str | None = None, active: bool = True,
              eligible: list[str] | None = None, **dates) -> str:
        ws = ws or self.ws
        with self.session() as s:
            entry = OfferCalendarEntry(
                organization_id=self.org_of(ws), workspace_id=ws,
                brand_id=get_primary_brand(s, ws).id, name="Offer",
                required_disclaimer=disclaimer, is_active=active,
                eligible_location_ids=eligible or [], **dates,
            )
            s.add(entry)
            s.flush()
            return entry.id

    # -- generation ----------------------------------------------------------------
    def generate(self, brief_id: str, role: str = "marketer", ws: str | None = None,
                 **body):
        body.setdefault("channel", "social_post")
        return self.send("POST", f"/workspaces/{ws or self.ws}/briefs/{brief_id}/drafts",
                         role, body)

    def drain(self, limit: int = 10) -> int:
        """Run every due job with the real worker; returns how many ran."""
        runner = JobRunner(
            settings=get_settings(), session_factory=self.factory, store=DurableJobStore()
        )
        ran = 0
        for _ in range(limit):
            if not runner.poll_once(worker_id="w-creative"):
                return ran
            ran += 1
        raise AssertionError("worker did not reach quiescence")

    def job(self, job_id: str) -> dict:
        row = self.row(Job, job_id)
        assert row is not None
        return row

    def generated(self, brief_id: str, role: str = "marketer", **body) -> dict:
        """Generate through the route and the worker; return the job row."""
        r = self.generate(brief_id, role, **body)
        assert r.status_code == 202, r.text
        job_id = r.json()["job_id"]
        self.drain()
        return self.job(job_id)


def _me(client: TestClient, token: str) -> str:
    _rate_limiter()._hits.clear()
    r = client.get(API + "/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    return r.json()["user"]["id"]


@contextmanager
def creative_world(db_file: Path, key: str) -> Iterator[CreativeWorld]:
    request_engine, witness = _engine(db_file), _engine(db_file)
    Base.metadata.create_all(request_engine)
    factory = sessionmaker(bind=request_engine, autoflush=False, expire_on_commit=False,
                           future=True)

    def _override_get_db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    app.dependency_overrides[get_db] = _override_get_db
    try:
        w = CreativeWorld(TestClient(app), request_engine, witness, factory)
        w.org, w.tokens["owner"] = _register(w.client, f"{key}a")
        w.ws = _workspace(w.client, w.org, w.tokens["owner"], f"{key} A1")
        w.ws2 = _workspace(w.client, w.org, w.tokens["owner"], f"{key} A2")
        for role in ROLES:
            w.tokens[role] = _invite(w.client, w.org, w.tokens["owner"], role)
        for role, token in w.tokens.items():
            w.users[role] = _me(w.client, token)
        w.org_b, w.owner_b = _register(w.client, f"{key}b")
        w.ws_b = _workspace(w.client, w.org_b, w.owner_b, f"{key} B")
        w.owner_b_id = _me(w.client, w.owner_b)
        yield w
    finally:
        app.dependency_overrides.clear()
        _rate_limiter()._hits.clear()
        request_engine.dispose()
        witness.dispose()


__all__ = [
    "ALL_ROLES",
    "API",
    "BRIEF_ACTIONS",
    "BRIEF_ANGLES",
    "BRIEF_SUMMARY",
    "CEILINGS",
    "ROLES",
    "CreativeWorld",
    "creative_world",
]
