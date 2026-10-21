"""Shared harness for the P6-PRIV-1 test modules. Not itself a test module.

One world, three organizations, every table populated:

* ``ORG_A`` — ALICE (the only OWNER), DAVE (ADMIN) and CAROL (VIEWER, who joined through an
  accepted invitation). A pending invitation to an address with no account (``INVITEE``).
* ``ORG_B`` — BOB, its only member and OWNER. The bystander: nothing done to A, C or to an
  account may change a single B row. It also holds a pending invitation to CAROL's address
  in different letter case.
* ``ORG_C`` — ALICE again, its only member. Deleting ALICE's account deletes C.

Each organization gets one row in every tenant table (:func:`populate_organization`), so a
deletion that misses a table leaves a row the tests and the verifier can see. Audit records
are written the way the product writes them: the invitation audits carry the invitee's
email in ``new_state``, the member audits carry the member's id in ``entity_id``.

As elsewhere in the suite, only ``get_db`` is overridden; committed state is read through
a separate engine. ``db_file=None`` builds the world in a PostgreSQL database of its own
(``TEST_POSTGRES_URL``), created and dropped here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.auth import service as auth_service
from app.auth.models import EmailVerificationToken, PasswordResetToken
from app.brands.models import Brand, BusinessProfile
from app.campaign_context.models import (
    AudienceProfile,
    BrandVoiceProfile,
    Campaign,
    ChannelPreference,
    ClaimsLibraryEntry,
    CompetitorProfile,
    OfferCalendarEntry,
    ProductProfile,
    SourcePreference,
)
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.config import get_settings
from app.core.security import hash_password
from app.db.models import Base
from app.db.session import SessionLocal, get_db
from app.feedback.models import OpportunityFeedback
from app.intelligence.records import SignalIntelligenceRecord
from app.jobs.models import Job, JobEvent
from app.jobs.worker_models import WorkerRegistration
from app.locations.models import BusinessLocation, GeoCoverageRule
from app.main import app
from app.opportunities.models import Opportunity, OpportunityScore, ValidationEvidence
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
    User,
    Workspace,
)
from app.scouting_requests.models import ScoutRequest, ScoutSchedule
from app.signals.models import NormalizedSignal, RawSignal, SignalCluster, SignalLocationEvidence
from app.tests._auth2_support import active_rate_limiter, pg_database, sqlite_file_engine

API = get_settings().api_prefix

PASSWORD = "privacy-pass-1"
PASSWORD_HASH = hash_password(PASSWORD)

ORG_A, ORG_B, ORG_C = "prv-org-a", "prv-org-b", "prv-org-c"
WS_A, WS_A2, WS_B, WS_C = "prv-ws-a", "prv-ws-a2", "prv-ws-b", "prv-ws-c"
ALICE, BOB, CAROL, DAVE = "prv-alice", "prv-bob", "prv-carol", "prv-dave"
EMAIL = {
    ALICE: "alice@privacy.example",
    BOB: "bob@privacy.example",
    CAROL: "carol_x@privacy.example",  # an underscore: LIKE wildcards must be escaped
    DAVE: "dave@privacy.example",
}
INVITEE = "invitee@privacy.example"
NAME = {ORG_A: "Acme Privacy", ORG_B: "Bravo Privacy", ORG_C: "Charlie Privacy"}

#: Every organization's workspaces, as seeded.
WORKSPACES = {ORG_A: (WS_A, WS_A2), ORG_B: (WS_B,), ORG_C: (WS_C,)}


def _id(tag: str, kind: str) -> str:
    value = f"{tag}-{kind}"
    assert len(value) <= 32, value
    return value


def populate_organization(s, *, org: str, ws: str, actor: str, tag: str, at: datetime) -> None:
    """One row in every tenant table of ``ws`` (``tag`` makes the ids unique)."""

    def i(kind: str) -> str:
        return _id(tag, kind)

    scoped = {"organization_id": org, "workspace_id": ws}
    s.add(Brand(id=i("brand"), name=f"Brand {tag}", **scoped))
    s.flush()
    branded = {**scoped, "brand_id": i("brand")}
    s.add(BusinessProfile(id=i("profile"), company_name=f"Company {tag}", **branded))
    s.add(
        BusinessLocation(
            id=i("loc"), name=f"Shop {tag}", address="1 High St", local_phone="+1 555 0100",
            **branded,
        )
    )
    s.flush()
    s.add(
        GeoCoverageRule(id=i("geo"), location_id=i("loc"), business_address="1 High St", **scoped)
    )
    s.add(ProductProfile(id=i("product"), name="Product", **branded))
    s.add(AudienceProfile(id=i("audience"), label="Busy parents", **branded))
    s.add(CompetitorProfile(id=i("competitor"), name="Rival", **branded))
    s.add(BrandVoiceProfile(id=i("voice"), example_copy="Hello", **branded))
    s.add(OfferCalendarEntry(id=i("offer"), name="Offer", promo_code="SAVE10", **branded))
    s.add(ClaimsLibraryEntry(id=i("claim"), text="Claim", kind="approved", **branded))
    s.add(SourcePreference(id=i("srcpref"), source_type="rss", **branded))
    s.add(ChannelPreference(id=i("chanpref"), channel="email", **branded))
    s.add(Campaign(id=i("campaign"), name="Campaign", location_ids=[i("loc")], **branded))
    s.flush()
    s.add(
        ScoutRequest(
            id=i("scout"), name="Scout", notes="notes", location_id=i("loc"),
            campaign_id=i("campaign"), **branded,
        )
    )
    s.flush()
    request = {**scoped, "scout_request_id": i("scout")}
    s.add(
        ScoutSchedule(
            id=i("schedule"), interval="daily", enabled=False, location_id=i("loc"), **request
        )
    )
    s.add(
        RawSignal(
            id=i("raw"), source_type="rss", author="Jane Public",
            content="Jane Public wrote about parking", created_at=at, **request,
        )
    )
    s.add(
        SignalCluster(
            id=i("cluster"), label="parking", workspace_id=ws, scout_request_id=i("scout"),
            created_at=at,
        )
    )
    s.flush()
    s.add(
        NormalizedSignal(
            id=i("norm"), raw_signal_id=i("raw"), cluster_id=i("cluster"), source_type="rss",
            author="Jane Public", excerpt="parking", content_hash="0" * 64, created_at=at,
            **request,
        )
    )
    s.flush()
    s.add(
        SignalLocationEvidence(
            id=i("locev"), workspace_id=ws, normalized_signal_id=i("norm"),
            evidence=["city: Springfield"],
        )
    )
    s.add(
        Opportunity(
            id=i("opp"), brand_id=i("brand"), scout_request_id=i("scout"), location_id=i("loc"),
            campaign_id=i("campaign"), cluster_id=i("cluster"), title="Parking",
            classification="x", decision="act_now", created_at=at, **scoped,
        )
    )
    s.flush()
    s.add(
        SignalIntelligenceRecord(
            id=i("intel"), normalized_signal_id=i("norm"), opportunity_id=i("opp"),
            location_id=i("loc"), analysis_version="1", scoring_version="1",
            fingerprint="f" * 64, enricher="test", accepted=True, classification="x",
            facts={"author": "Jane Public"}, **request,
        )
    )
    s.add(
        OpportunityScore(id=i("score"), workspace_id=ws, opportunity_id=i("opp"), kind="opp")
    )
    s.add(
        ValidationEvidence(
            id=i("valev"), workspace_id=ws, opportunity_id=i("opp"), source_type="rss",
            detail="Jane Public",
        )
    )
    s.flush()
    s.add(
        OpportunityFeedback(
            id=i("feedback"), opportunity_id=i("opp"), intelligence_record_id=i("intel"),
            submitted_by_user_id=actor, is_useful=True, analysis_version="1",
            scoring_version="1", fingerprint="f" * 64, **scoped,
        )
    )
    s.add(
        WorkspaceCapabilityOverride(
            id=i("override"), capability="opportunity_feedback", enabled=True,
            set_by_user_id=actor, reason="pilot", **scoped,
        )
    )
    s.add(
        Job(
            id=i("job"), job_type="scout_request.execute",
            payload={"scout_request_id": i("scout")}, payload_hash="0" * 64,
            status="succeeded", scout_request_id=i("scout"), updated_at=at, **scoped,
        )
    )
    s.flush()
    s.add(JobEvent(id=i("jobev"), job_id=i("job"), event_type="succeeded", **scoped))
    s.flush()


#: (organization, workspace, actor, action, entity_type, entity_id, reason, new_state)
_AUDIT = (
    (ORG_A, None, ALICE, "auth.register", "user", ALICE, None, None),
    (ORG_A, None, ALICE, "organization_invitation.created", "organization_invitation",
     "prv-inv-carol", None, {"email": EMAIL[CAROL], "role": "viewer"}),
    (ORG_A, None, CAROL, "organization_invitation.accepted", "organization_invitation",
     "prv-inv-carol", None,
     {"email": EMAIL[CAROL], "role": "viewer", "accepted_by_user_id": CAROL}),
    (ORG_A, None, ALICE, "organization_member.role_changed", "organization_member", CAROL,
     None, {"role": "viewer"}),
    (ORG_A, WS_A, DAVE, "opportunity.reviewed", "opportunity", "a1-opp",
     f"Asked by {EMAIL[CAROL].upper()} on a call", {"status": "reviewed"}),
    (ORG_A, None, ALICE, "organization_invitation.created", "organization_invitation",
     "prv-inv-invitee", None, {"email": INVITEE, "role": "viewer"}),
    (ORG_B, None, BOB, "auth.register", "user", BOB, None, None),
    (ORG_B, None, BOB, "organization_invitation.created", "organization_invitation",
     "prv-inv-b-carol", None, {"email": EMAIL[CAROL].upper(), "role": "marketer"}),
    (ORG_C, WS_C, ALICE, "location.created", "location", "c1-loc", None, {"name": "x"}),
)


def seed(s, *, at: datetime) -> None:
    for org in (ORG_A, ORG_B, ORG_C):
        s.add(Organization(id=org, name=NAME[org], slug=org))
    for user_id, email in EMAIL.items():
        s.add(
            User(
                id=user_id, email=email, full_name=f"Name {user_id}",
                hashed_password=PASSWORD_HASH,
            )
        )
    s.flush()
    for org, user_id, role in (
        (ORG_A, ALICE, "owner"),
        (ORG_A, DAVE, "admin"),
        (ORG_A, CAROL, "viewer"),
        (ORG_B, BOB, "owner"),
        (ORG_C, ALICE, "owner"),
    ):
        s.add(OrganizationMember(organization_id=org, user_id=user_id, role=role))
    for org, workspaces in WORKSPACES.items():
        for ws in workspaces:
            s.add(Workspace(id=ws, organization_id=org, name=ws, slug=ws))
    s.flush()
    populate_organization(s, org=ORG_A, ws=WS_A, actor=CAROL, tag="a1", at=at)
    populate_organization(s, org=ORG_A, ws=WS_A2, actor=ALICE, tag="a2", at=at)
    populate_organization(s, org=ORG_B, ws=WS_B, actor=BOB, tag="b1", at=at)
    populate_organization(s, org=ORG_C, ws=WS_C, actor=ALICE, tag="c1", at=at)

    later = at + timedelta(days=30)
    s.add(
        OrganizationInvitation(
            id="prv-inv-carol", organization_id=ORG_A, email=EMAIL[CAROL], role="viewer",
            token_hash="1" * 64, invited_by_user_id=ALICE, expires_at=later, accepted_at=at,
            accepted_by_user_id=CAROL,
        )
    )
    s.add(
        OrganizationInvitation(
            id="prv-inv-invitee", organization_id=ORG_A, email=INVITEE, role="viewer",
            token_hash="2" * 64, invited_by_user_id=ALICE, expires_at=later,
        )
    )
    s.add(
        OrganizationInvitation(
            id="prv-inv-b-carol", organization_id=ORG_B, email=EMAIL[CAROL].upper(),
            role="marketer", token_hash="3" * 64, invited_by_user_id=BOB, expires_at=later,
        )
    )
    s.add(
        PasswordResetToken(
            id="prv-reset-carol", user_id=CAROL, token_hash="4" * 64, expires_at=later
        )
    )
    s.add(
        EmailVerificationToken(
            id="prv-verify-carol", user_id=CAROL, token_hash="5" * 64,
            email_snapshot=EMAIL[CAROL], expires_at=later,
        )
    )
    for n, (org, ws, actor, action, etype, eid, reason, new_state) in enumerate(_AUDIT):
        s.add(
            AuditLog(
                id=f"prv-audit-{n}", organization_id=org, workspace_id=ws,
                actor_user_id=actor, action=action, entity_type=etype, entity_id=eid,
                reason=reason, new_state=new_state, context={"request": f"r{n}"},
            )
        )
    s.add(WorkerRegistration(id="prv-worker-row", worker_id="prv-worker", worker_type="dj"))
    s.commit()


# --------------------------------------------------------------------------- #
# Environment
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class PrivacyEnv:
    client: TestClient
    engine: Engine  # the request engine
    witness: Engine  # a separate engine for reading committed state
    seeded_at: datetime

    def session(self):
        return sessionmaker(bind=self.engine, autoflush=False, expire_on_commit=False)()

    def auth(self, user_id: str) -> dict[str, str]:
        """A bearer header bound to a new live session of ``user_id``."""
        with self.session() as s:
            user = s.get(User, user_id)
            session = auth_service.create_session(s, user)
            s.commit()
            return {"Authorization": f"Bearer {auth_service.issue_token(user, session)}"}

    def count(self, model, *where) -> int:
        with self.witness.connect() as conn:
            return conn.execute(
                select(func.count()).select_from(model.__table__).where(*where)
            ).scalar_one()

    def rows(self, model, *where) -> list[dict]:
        table = model.__table__
        with self.witness.connect() as conn:
            result = conn.execute(select(*table.c).where(*where).order_by(table.c.id))
            return [dict(r._mapping) for r in result]

    def snapshot(self, predicate=None) -> dict[str, tuple]:
        """Every row of every table (optionally only the rows ``predicate(table, row)`` keeps)."""
        out = {}
        with self.witness.connect() as conn:
            for table in Base.metadata.sorted_tables:
                rows = conn.execute(select(*table.c).order_by(table.c.id)).all()
                out[table.name] = tuple(
                    tuple(r) for r in rows if predicate is None or predicate(table, r._mapping)
                )
        return out


def belongs_to(organization_id: str):
    """A :meth:`PrivacyEnv.snapshot` predicate: rows of one organization or its workspaces."""
    workspaces = set(WORKSPACES[organization_id])

    def keep(table, row) -> bool:
        if table.name == "organizations":
            return row["id"] == organization_id
        if "organization_id" in row:
            return row["organization_id"] == organization_id
        if "workspace_id" in row:
            return row["workspace_id"] in workspaces
        return False

    return keep


def _override(factory):
    def _get_db():
        s = factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()

    return _get_db


@contextmanager
def privacy_world(db_file: Path | None) -> Iterator[PrivacyEnv]:
    """The seeded world in a SQLite file, or (``db_file=None``) a PostgreSQL database."""
    if db_file is None:  # pragma: no cover - gated on PostgreSQL
        with pg_database("sn_prv") as url:
            with _built(create_engine(url, future=True), create_engine(url, future=True)) as env:
                yield env
        return
    with _built(sqlite_file_engine(db_file), sqlite_file_engine(db_file)) as env:
        yield env


@contextmanager
def _built(engine: Engine, witness: Engine) -> Iterator[PrivacyEnv]:
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    at = datetime.now(UTC) - timedelta(days=400)
    with factory() as s:
        seed(s, at=at)
    saved_bind = SessionLocal.kw.get("bind")
    SessionLocal.kw["bind"] = engine
    app.dependency_overrides[get_db] = _override(factory)
    active_rate_limiter()._hits.clear()
    try:
        yield PrivacyEnv(TestClient(app), engine, witness, at)
    finally:
        app.dependency_overrides.clear()
        active_rate_limiter()._hits.clear()
        SessionLocal.kw["bind"] = saved_bind
        engine.dispose()
        witness.dispose()
