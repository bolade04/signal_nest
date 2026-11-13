"""D-N4 item 34 (operator ruling 2026-11-12): the approver's membership role is re-read under a
``FOR SHARE`` lock, for brief and draft decisions.

The adopted recommendation: "Re-reading the approver's membership role under a FOR SHARE lock,
for brief and draft decisions alike, is an activation precondition for recommendation_briefs
and creative_generation." Before it, a reviewer demoted while a decision request was in flight
still recorded that decision with ``decided_role = reviewer`` (reproduced on SQLite and
PostgreSQL; review W21-REV-R4, probe P6, drafts included).

**Both engines** (the service, called exactly as the routes call it):

* a decision whose request resolved the reviewer role (``ctx.role``) but whose membership now
  says otherwise is refused 403 and writes nothing: demoted to each other role, or removed;
* a decider whose account was deleted after the request resolved its context, and before the
  decision, is refused 403 (no membership is left), never a 500, and nothing is written
  (review W21-REV-DN4A r2 O-r2-2, ruling A-R-8);
* an unchanged reviewer still decides, and ``decided_role`` is the role read under the lock;
* the lock order (``approvals.service`` "Lock order"; review W21-REV-DN4A r1 C-1): the decider's
  user row, then the organization, then the membership, then the subject's row lock, then the
  sequence read.

**PostgreSQL** (``TEST_POSTGRES_URL``; unset FAILS in CI and skips elsewhere), real sessions in
real threads, every wait bounded (``lock_timeout`` / ``statement_timeout`` and join bounds):

* **the demotion waits for the decision, without deadlock**: a decision holds its locks until it
  commits; a concurrent demotion (or removal) of the decider is seen waiting on the
  organization row (it locks it ``FOR UPDATE`` first), blocked by the decider's backend, and
  completes once the decision commits; the decision is recorded as a reviewer's, and the
  demotion after it. Brief and draft paths;
* **the membership itself is held ``FOR SHARE``**: a direct ``UPDATE`` of the decider's
  membership (a writer that skips the organization lock) waits on it until the decision commits;
* **a decision that comes second is refused**: while an uncommitted demotion holds the
  organization, the decision waits on it, then reads the committed role and is refused 403,
  writing nothing;
* **organization deletion never deadlocks with a decision** (the r1 defect, 40P01): with the
  real ``delete_organization`` paused after it deleted the memberships, the decision waits on
  the organization and is refused 403 once the deletion commits; with the decision paused
  first, the deletion waits on the organization and erases it once the decision commits (the
  approval is kept, anonymised). Brief and draft paths; neither transaction is aborted;
* **account deletion of the decider never deadlocks with a decision**: with the decision paused
  after its subject lock, the deletion waits on the decider's user row (the decision's first
  lock), and both complete in order. Without that lock the deletion would hold the user row
  and wait on the organization while the decision's foreign key waited on the user row.
* the lock statements compile to ``FOR KEY SHARE``, ``FOR KEY SHARE`` and ``FOR SHARE`` on
  PostgreSQL.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

import app.main  # noqa: F401 - registers every ORM model
from app.approvals import service as svc
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.auth.dependencies import OrganizationContext, TenantContext
from app.briefs.models import RecommendationBrief
from app.core.enums import ApprovalDecision, Role
from app.core.errors import PermissionDeniedError
from app.core.security import hash_password
from app.creative.models import ContentReview, CreativeDraft
from app.db.models import Base
from app.organizations import members
from app.organizations.models import Organization, OrganizationMember, User, Workspace
from app.privacy import erasure
from app.privacy.erasure import delete_account, delete_organization
from app.tests._approvals_support import add_brief, add_evidence, add_org, add_user
from app.tests._auth2_support import pg_database, require_postgres, sqlite_file_engine

ORG, WS, BRIEF, DRAFT = "org-rr", "ws-rr", "brief-rr", "draft-rr"
AUTHOR, DECIDER, ADMIN, OTHER = "rr-author", "rr-decider", "rr-admin", "rr-other"
OWNER = "rr-owner"
PASSWORD = "role-reread-pass-1"
JOIN_SECONDS = 60.0
POLL_SECONDS = 30.0
SUBJECTS = ("brief", "draft")


def _seed(s: Session) -> None:
    add_org(s, ORG, WS)
    author = add_user(s, AUTHOR, org_id=ORG, role=Role.MARKETER)
    add_user(s, ADMIN, org_id=ORG, role=Role.ADMIN)
    add_user(s, OTHER, org_id=ORG, role=Role.REVIEWER)
    for user_id, role in ((DECIDER, Role.REVIEWER), (OWNER, Role.OWNER)):
        s.add(
            User(
                id=user_id, email=f"{user_id}@example.test", full_name=f"Name {user_id}",
                hashed_password=hash_password(PASSWORD), is_active=True,
            )
        )
        s.flush()
        s.add(OrganizationMember(id=f"m-{user_id}", organization_id=ORG, user_id=user_id,
                                 role=role.value))
        s.flush()
    evidence = add_evidence(s, org_id=ORG, ws_id=WS, key="rr")
    brief = add_brief(s, evidence, brief_id=BRIEF, author=author)
    s.add(
        CreativeDraft(
            id=DRAFT, organization_id=ORG, workspace_id=WS, brief_id=brief.id,
            opportunity_id=evidence.opportunity_id, draft_version=1, channel="email",
            headline="Weekend parking", body="Plenty of room near the market.",
            call_to_action="Plan your visit", prompt_name="generate_creative",
            prompt_version="1.0.0", model_identifier="mock-model", input_hash="0" * 64,
            is_simulated=True, input_tokens=1, output_tokens=1, estimated_cost_usd=0,
            created_by=author.id, created_by_snapshot=brief.created_by_snapshot,
        )
    )
    s.flush()
    s.add(
        ContentReview(
            organization_id=ORG, workspace_id=WS, draft_id=DRAFT, draft_version=1,
            risk_level="low", is_blocked=False, findings=[], warnings=[], blocked_claim_ids=[],
            required_disclaimers=[], disclaimers_resolved=True, passed=True,
        )
    )
    s.commit()


def _ctx(s: Session, user_id: str, role: Role = Role.REVIEWER) -> TenantContext:
    """The context the request resolved at its start (``role`` as it read it then)."""
    return TenantContext(
        user=s.get(User, user_id), organization=s.get(Organization, ORG),
        workspace=s.get(Workspace, WS), role=role,
    )


def _decide(s: Session, subject: str, user_id: str = DECIDER,
            decision: ApprovalDecision = ApprovalDecision.APPROVED) -> Approval:
    ctx = _ctx(s, user_id)
    if subject == "brief":
        return svc.record_decision(
            s, ctx=ctx, brief=s.get(RecommendationBrief, BRIEF), decision=decision,
            subject_version=1,
        )
    return svc.record_draft_decision(
        s, ctx=ctx, draft=s.get(CreativeDraft, DRAFT), decision=decision, subject_version=1,
    )


def _approvals(factory: sessionmaker) -> list[tuple]:
    with factory() as s:
        return [
            (a.subject_type, a.decision, a.decided_by, a.decided_role)
            for a in s.scalars(select(Approval).order_by(Approval.created_at, Approval.id))
        ]


def _approval_audits(factory: sessionmaker) -> int:
    with factory() as s:
        return len(s.scalars(select(AuditLog).where(AuditLog.action.like("approval.%"))).all())


def _set_role(factory: sessionmaker, role: Role | None) -> None:
    with factory() as s:
        member = s.scalar(select(OrganizationMember).where(
            OrganizationMember.organization_id == ORG, OrganizationMember.user_id == DECIDER))
        if role is None:
            s.delete(member)
        else:
            member.role = role.value
        s.commit()


# --------------------------------------------------------------------------- #
# Both engines: the persisted role decides, not the request's
# --------------------------------------------------------------------------- #
@dataclass
class World:
    engine: Engine
    factory: sessionmaker


@pytest.fixture(params=["sqlite", "postgres"])
def world(request, tmp_path: Path) -> Iterator[World]:
    if request.param == "postgres":
        require_postgres()
        with pg_database("sn_rr") as url:
            engine = create_engine(url, future=True)
            yield from _world(engine)
        return
    yield from _world(sqlite_file_engine(tmp_path / "role-reread.db"))


def _world(engine: Engine) -> Iterator[World]:
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    with factory() as s:
        _seed(s)
    try:
        yield World(engine, factory)
    finally:
        engine.dispose()


@pytest.mark.parametrize("subject", SUBJECTS)
@pytest.mark.parametrize(
    "now", [Role.OWNER, Role.ADMIN, Role.MARKETER, Role.COMPLIANCE_REVIEWER, Role.VIEWER, None],
    ids=["owner", "admin", "marketer", "compliance_reviewer", "viewer", "removed"],
)
def test_a_decider_whose_membership_changed_is_refused_and_nothing_is_written(
    world: World, subject: str, now: Role | None
):
    _set_role(world.factory, now)  # committed before the decision; the request still says reviewer
    with world.factory() as s:
        with pytest.raises(PermissionDeniedError) as refused:
            _decide(s, subject)
        s.rollback()
    expected = (
        svc.NOT_A_MEMBER if now is None
        else f"Role '{now.value}' is not permitted for this action."
    )
    assert (refused.value.message, refused.value.status_code) == (expected, 403)
    assert _approvals(world.factory) == []
    assert _approval_audits(world.factory) == 0


@pytest.mark.parametrize("subject", SUBJECTS)
def test_a_decider_whose_account_was_deleted_first_is_refused_and_nothing_is_written(
    world: World, subject: str
):
    """Review W21-REV-DN4A r2 O-r2-2 (ruling A-R-8): the request resolved its context while the
    decider existed; the decider's own account deletion then committed, before the decision.
    The re-read finds no user row and no membership: 403 ``NOT_A_MEMBER``, never a 500, and
    nothing is written ("A deleted organization or user leaves no membership: 403 too")."""
    with world.factory() as s:
        ctx = _ctx(s, DECIDER)  # as the request resolved it at its start
        brief = s.get(RecommendationBrief, BRIEF)
        draft = s.get(CreativeDraft, DRAFT)
        s.commit()  # ends the read; expire_on_commit=False keeps ctx and the subject loaded
        with world.factory() as deletion:
            delete_account(deletion, user=deletion.get(User, DECIDER), current_password=PASSWORD)
            deletion.commit()
        with pytest.raises(PermissionDeniedError) as refused:
            if subject == "brief":
                svc.record_decision(s, ctx=ctx, brief=brief,
                                    decision=ApprovalDecision.APPROVED, subject_version=1)
            else:
                svc.record_draft_decision(s, ctx=ctx, draft=draft,
                                          decision=ApprovalDecision.APPROVED, subject_version=1)
        s.rollback()
    assert (refused.value.message, refused.value.status_code) == (svc.NOT_A_MEMBER, 403)
    with world.factory() as s:
        assert s.get(User, DECIDER) is None
        assert s.scalar(select(OrganizationMember.id).where(
            OrganizationMember.user_id == DECIDER).limit(1)) is None
    assert _approvals(world.factory) == []
    assert _approval_audits(world.factory) == 0


@pytest.mark.parametrize("subject", SUBJECTS)
def test_an_unchanged_reviewer_decides_and_the_reread_role_is_recorded(world: World, subject):
    with world.factory() as s:
        _decide(s, subject)
        s.commit()
    assert _approvals(world.factory) == [
        ("recommendation_brief" if subject == "brief" else "creative_draft", "approved",
         DECIDER, "reviewer"),
    ]
    assert _approval_audits(world.factory) == 1


@pytest.mark.parametrize("subject", SUBJECTS)
def test_the_reread_precedes_the_subject_lock_in_the_lock_order(world: World, subject):
    """Statement order inside the decision ("Lock order" in ``approvals.service``): the
    decider's user row, then the organization, then the membership, then the subject row, then
    ``max(decision_seq)``."""
    seen: list[str] = []

    def record(conn, cursor, statement, parameters, context, executemany):
        seen.append(" ".join(statement.split()).lower())

    event.listen(world.engine, "before_cursor_execute", record)
    try:
        with world.factory() as s:
            # Load the context and the subject first, as the request does.
            ctx = _ctx(s, DECIDER)
            brief = s.get(RecommendationBrief, BRIEF)
            draft = s.get(CreativeDraft, DRAFT)
            seen.clear()
            if subject == "brief":
                svc.record_decision(s, ctx=ctx, brief=brief,
                                    decision=ApprovalDecision.APPROVED, subject_version=1)
            else:
                svc.record_draft_decision(s, ctx=ctx, draft=draft,
                                          decision=ApprovalDecision.APPROVED, subject_version=1)
            s.commit()
    finally:
        event.remove(world.engine, "before_cursor_execute", record)

    table, version = (
        ("recommendation_briefs", "brief_version") if subject == "brief"
        else ("creative_drafts", "draft_version")
    )

    def first(predicate) -> int:
        return next(i for i, sql in enumerate(seen) if predicate(sql))

    # The subject lock selects exactly the id and the version (``subject_lock_select``).
    lock = first(lambda q: q.startswith(f"select {table}.id, {table}.{version} from {table}"))
    user = first(lambda q: q.startswith("select users.id from users"))
    organization = first(lambda q: q.startswith("select organizations.id from organizations"))
    member = first(lambda q: q.startswith("select organization_members.role"))
    seq = first(lambda q: "max(approvals.decision_seq)" in q)
    assert user < organization < member < lock < seq, seen


def test_the_lock_statements_compile_to_for_share_and_for_key_share():
    dialect = postgresql.dialect()
    member = str(svc.approver_membership_select(ORG, DECIDER).compile(dialect=dialect))
    user = str(svc.approver_user_lock_select(DECIDER).compile(dialect=dialect))
    organization = str(svc.approver_organization_lock_select(ORG).compile(dialect=dialect))
    assert member.rstrip().endswith("FOR SHARE") and "KEY SHARE" not in member
    assert "organization_members" in member and "role" in member
    assert user.rstrip().endswith("FOR KEY SHARE") and "users" in user
    assert organization.rstrip().endswith("FOR KEY SHARE") and "organizations" in organization


# --------------------------------------------------------------------------- #
# PostgreSQL: concurrency
# --------------------------------------------------------------------------- #
@dataclass
class PgWorld:
    url: str
    engine: Engine
    factory: sessionmaker
    observer: Engine


def _pg_engine(url: str, **kwargs) -> Engine:
    # Bounded waits on every connection: a missed release fails the test instead of hanging.
    return create_engine(
        url, future=True, pool_size=8, max_overflow=4,
        connect_args={"options": "-c lock_timeout=30000 -c statement_timeout=60000"},
        **kwargs,
    )


@pytest.fixture
def pg() -> Iterator[PgWorld]:
    require_postgres()
    with pg_database("sn_rrpg") as url:
        engine = _pg_engine(url)
        observer = _pg_engine(url, isolation_level="AUTOCOMMIT")
        Base.metadata.create_all(engine)
        factory = sessionmaker(bind=engine, expire_on_commit=False, future=True)
        with factory() as s:
            _seed(s)
        try:
            yield PgWorld(url, engine, factory, observer)
        finally:
            engine.dispose()
            observer.dispose()


class _Runner(threading.Thread):
    def __init__(self, fn, *args, **kwargs):
        super().__init__(daemon=True)
        self._call = (fn, args, kwargs)
        self.result = None
        self.error: BaseException | None = None

    def run(self) -> None:  # pragma: no cover - exercised through join
        fn, args, kwargs = self._call
        try:
            self.result = fn(*args, **kwargs)
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            self.error = exc

    def outcome(self):
        self.join(JOIN_SECONDS)
        assert not self.is_alive(), "a transaction did not finish"
        if self.error is not None:
            raise self.error
        return self.result


def _pid(s: Session) -> int:
    return s.execute(text("SELECT pg_backend_pid()")).scalar_one()


def _waiting(w: PgWorld, holder: int, *needles: str) -> tuple[list[dict], list[dict]]:
    """``(matching, seen)``: backends in a heavyweight-lock wait, and those blocked by
    ``holder`` whose statement contains every needle (the text is part of the predicate: a
    backend can be seen waiting while ``pg_stat_activity`` still shows its previous text)."""
    with w.observer.connect() as c:
        seen = [dict(r) for r in c.execute(text(
            "SELECT pid, pg_blocking_pids(pid) AS blockers, wait_event, query"
            " FROM pg_stat_activity"
            " WHERE datname = current_database() AND wait_event_type = 'Lock'"
        )).mappings()]
    matching = [
        r for r in seen
        if holder in (r["blockers"] or []) and all(n in r["query"] for n in needles)
    ]
    return matching, seen


def _wait_for(observe, what: str):
    deadline = time.monotonic() + POLL_SECONDS
    seen = None
    while time.monotonic() < deadline:
        value, seen = observe()
        if value:
            return value
        time.sleep(0.05)
    raise AssertionError(f"timed out waiting for {what} after {POLL_SECONDS} s; last: {seen}")


def _decision_held(w: PgWorld, subject: str, holding: threading.Event, hold: threading.Event,
                   pid: list[int]) -> tuple:
    """A decision that flushes, signals, and commits only when released."""
    with w.factory() as s:
        pid.append(_pid(s))
        try:
            approval = _decide(s, subject)
        except PermissionDeniedError as exc:
            s.rollback()
            return ("forbidden", str(exc))
        holding.set()
        assert hold.wait(JOIN_SECONDS)
        s.commit()
        return ("ok", approval.decided_role)


def _membership_change(w: PgWorld, change: str, *, holding: threading.Event | None = None,
                       hold: threading.Event | None = None, pid: list[int] | None = None):
    """The admin demotes (to marketer) or removes the decider, as the members routes do."""
    with w.factory() as s:
        if pid is not None:
            pid.append(_pid(s))
        ctx = OrganizationContext(user=s.get(User, ADMIN), organization=s.get(Organization, ORG),
                                  role=Role.ADMIN)
        if change == "demote":
            members.change_member_role(s, ctx=ctx, target_user_id=DECIDER,
                                       new_role=Role.MARKETER)
        else:
            members.remove_member(s, ctx=ctx, target_user_id=DECIDER)
        if holding is not None:
            holding.set()
            assert hold is not None and hold.wait(JOIN_SECONDS)
        s.commit()
        return "ok"


def _membership(w: PgWorld) -> str | None:
    with w.factory() as s:
        return s.scalar(select(OrganizationMember.role).where(
            OrganizationMember.organization_id == ORG, OrganizationMember.user_id == DECIDER))


@pytest.mark.parametrize("subject", SUBJECTS)
@pytest.mark.parametrize("change", ["demote", "remove"])
def test_a_demotion_or_removal_waits_for_the_decision_without_deadlock(pg: PgWorld, subject,
                                                                       change):
    holding, hold, decider_pid = threading.Event(), threading.Event(), []
    decision = _Runner(_decision_held, pg, subject, holding, hold, decider_pid)
    decision.start()
    assert holding.wait(JOIN_SECONDS), "the decision never flushed"
    change_runner = _Runner(_membership_change, pg, change)
    change_runner.start()
    try:
        # It locks the organization FOR UPDATE first (members.lock_organization): it waits
        # there, on the decision's organization KEY SHARE (step 2 of the lock order).
        _wait_for(lambda: _waiting(pg, decider_pid[0], "organizations", "FOR UPDATE"),
                  f"the {change} to wait on the organization row the decision holds")
        time.sleep(0.3)
        assert change_runner.is_alive()  # still waiting: the decision has not committed
        assert _membership(pg) == "reviewer"
    finally:
        hold.set()
    assert decision.outcome() == ("ok", "reviewer")
    assert change_runner.outcome() == "ok"  # then it completes: no deadlock, no timeout
    assert _membership(pg) == ("marketer" if change == "demote" else None)
    kind = "recommendation_brief" if subject == "brief" else "creative_draft"
    assert _approvals(pg.factory) == [(kind, "approved", DECIDER, "reviewer")]
    assert _approval_audits(pg.factory) == 1


@pytest.mark.parametrize("subject", SUBJECTS)
def test_a_decision_after_an_uncommitted_demotion_waits_and_is_refused(pg: PgWorld, subject):
    holding, hold, admin_pid = threading.Event(), threading.Event(), []
    demotion = _Runner(_membership_change, pg, "demote", holding=holding, hold=hold,
                       pid=admin_pid)
    demotion.start()
    assert holding.wait(JOIN_SECONDS), "the demotion never flushed"
    decided, release, decider_pid = threading.Event(), threading.Event(), []
    decision = _Runner(_decision_held, pg, subject, decided, release, decider_pid)
    decision.start()
    try:
        _wait_for(lambda: _waiting(pg, admin_pid[0], "organizations", "FOR KEY SHARE"),
                  "the decision to wait on the organization row the demotion holds")
        assert not decided.is_set()
    finally:
        hold.set()
        release.set()
    assert demotion.outcome() == "ok"
    assert decision.outcome() == (
        "forbidden", "Role 'marketer' is not permitted for this action."
    )
    assert _approvals(pg.factory) == []
    assert _approval_audits(pg.factory) == 0


@pytest.mark.parametrize("subject", SUBJECTS)
def test_account_deletion_of_the_decider_never_deadlocks_with_a_decision(pg: PgWorld, subject,
                                                                         monkeypatch):
    """The decision pauses after its subject lock (it holds the user ``FOR KEY SHARE``, the
    organization ``FOR KEY SHARE``, the membership ``FOR SHARE`` and the subject, its approval
    not yet inserted); the decider's account deletion then waits on the user row, and both
    complete once the decision commits."""
    reread, go = threading.Event(), threading.Event()
    real_next = svc._next_decision_seq

    def paused(*args, **kwargs):
        reread.set()
        assert go.wait(JOIN_SECONDS)
        return real_next(*args, **kwargs)

    monkeypatch.setattr(svc, "_next_decision_seq", paused)
    holding, hold, decider_pid = threading.Event(), threading.Event(), []
    hold.set()  # commit as soon as it has flushed
    decision = _Runner(_decision_held, pg, subject, holding, hold, decider_pid)
    decision.start()
    assert reread.wait(JOIN_SECONDS), "the decision never reached its sequence read"

    def delete() -> str:
        with pg.factory() as s:
            delete_account(s, user=s.get(User, DECIDER), current_password=PASSWORD)
            s.commit()
            return "ok"

    deletion = _Runner(delete)
    deletion.start()
    try:
        _wait_for(lambda: _waiting(pg, decider_pid[0], "users"),
                  "the account deletion to wait on the decider's user row")
    finally:
        go.set()
    assert decision.outcome() == ("ok", "reviewer")
    assert deletion.outcome() == "ok"  # no deadlock: neither transaction was aborted
    kind = "recommendation_brief" if subject == "brief" else "creative_draft"
    # The decision stands (O-6); the deletion then cleared the decider's id from it.
    assert _approvals(pg.factory) == [(kind, "approved", None, "reviewer")]
    with pg.factory() as s:
        assert s.get(User, DECIDER) is None


def test_a_direct_membership_update_waits_on_the_decisions_for_share(pg: PgWorld):
    """Step 3 on its own: a writer that changes the decider's membership without taking the
    organization first (none does today) still waits for the decision, on the membership's
    ``FOR SHARE``, and goes through once it commits."""
    holding, hold, decider_pid = threading.Event(), threading.Event(), []
    decision = _Runner(_decision_held, pg, "brief", holding, hold, decider_pid)
    decision.start()
    assert holding.wait(JOIN_SECONDS), "the decision never flushed"

    def direct_update() -> int:
        with pg.engine.begin() as conn:
            return conn.execute(text(
                "UPDATE organization_members SET role = 'marketer'"
                " WHERE organization_id = :o AND user_id = :u"
            ), {"o": ORG, "u": DECIDER}).rowcount

    writer = _Runner(direct_update)
    writer.start()
    try:
        _wait_for(lambda: _waiting(pg, decider_pid[0], "UPDATE organization_members"),
                  "the direct update to wait on the decider's membership row")
        time.sleep(0.3)
        assert writer.is_alive()
    finally:
        hold.set()
    assert decision.outcome() == ("ok", "reviewer")
    assert writer.outcome() == 1
    assert _membership(pg) == "marketer"


def _paused_organization_deletion(monkeypatch, deleted: threading.Event, resume: threading.Event):
    """Pause the real organization deletion right after its walk has deleted the memberships
    (``erasure.delete_rows`` on ``organization_members``), before it reaches the subject."""
    real = erasure.delete_rows

    def delete_rows(db, table, ids):
        counts = real(db, table, ids)
        if table.name == "organization_members" and not deleted.is_set():
            deleted.set()
            assert resume.wait(JOIN_SECONDS)
        return counts

    monkeypatch.setattr(erasure, "delete_rows", delete_rows)


def _delete_organization(w: PgWorld, pid: list[int]) -> str:
    with w.factory() as s:
        pid.append(_pid(s))
        delete_organization(
            s, user=s.get(User, OWNER), organization_id=ORG, current_password=PASSWORD,
            confirm_organization_name=f"Org {ORG}",
        )
        s.commit()
        return "ok"


def _organization_gone(w: PgWorld) -> bool:
    with w.factory() as s:
        return (
            s.get(Organization, ORG) is None
            and s.get(RecommendationBrief, BRIEF) is None
            and s.get(CreativeDraft, DRAFT) is None
            and s.scalar(select(OrganizationMember.id).where(
                OrganizationMember.organization_id == ORG).limit(1)) is None
            and s.get(User, DECIDER) is not None  # members' accounts survive
        )


@pytest.mark.parametrize("subject", SUBJECTS)
def test_organization_deletion_first_the_decision_waits_and_is_refused(pg: PgWorld, subject,
                                                                       monkeypatch):
    """The r1 interleaving (review W21-REV-DN4A r1 C-1, 40P01 at ``dd2d1a56``): the deletion
    has deleted the memberships and not yet reached the subject. The decision now waits on the
    organization (step 2) before it holds the membership or the subject; once the deletion
    commits it finds no membership and is refused 403. Neither transaction is aborted."""
    deleted, resume, owner_pid = threading.Event(), threading.Event(), []
    _paused_organization_deletion(monkeypatch, deleted, resume)
    deletion = _Runner(_delete_organization, pg, owner_pid)
    deletion.start()
    assert deleted.wait(JOIN_SECONDS), "the deletion never deleted the memberships"
    holding, hold, decider_pid = threading.Event(), threading.Event(), []
    hold.set()
    decision = _Runner(_decision_held, pg, subject, holding, hold, decider_pid)
    decision.start()
    try:
        _wait_for(lambda: _waiting(pg, owner_pid[0], "organizations", "FOR KEY SHARE"),
                  "the decision to wait on the organization the deletion holds")
        assert not holding.is_set()
    finally:
        resume.set()
    assert deletion.outcome() == "ok"  # no 40P01: outcome() re-raises any error
    assert decision.outcome() == ("forbidden", svc.NOT_A_MEMBER)
    assert _organization_gone(pg)
    assert _approvals(pg.factory) == []
    assert _approval_audits(pg.factory) == 0


@pytest.mark.parametrize("subject", SUBJECTS)
def test_a_decision_first_the_organization_deletion_waits_then_erases(pg: PgWorld, subject,
                                                                      monkeypatch):
    """The other order: the decision holds every lock (paused after its subject lock); the
    deletion waits on the organization, then, once the decision commits, erases the
    organization and keeps the approval, anonymised (O-6). Neither transaction is aborted."""
    reread, go = threading.Event(), threading.Event()
    real_next = svc._next_decision_seq

    def paused(*args, **kwargs):
        reread.set()
        assert go.wait(JOIN_SECONDS)
        return real_next(*args, **kwargs)

    monkeypatch.setattr(svc, "_next_decision_seq", paused)
    holding, hold, decider_pid = threading.Event(), threading.Event(), []
    hold.set()
    decision = _Runner(_decision_held, pg, subject, holding, hold, decider_pid)
    decision.start()
    assert reread.wait(JOIN_SECONDS), "the decision never reached its sequence read"
    owner_pid: list[int] = []
    deletion = _Runner(_delete_organization, pg, owner_pid)
    deletion.start()
    try:
        _wait_for(lambda: _waiting(pg, decider_pid[0], "organizations", "FOR UPDATE"),
                  "the deletion to wait on the organization the decision holds")
    finally:
        go.set()
    assert decision.outcome() == ("ok", "reviewer")
    assert deletion.outcome() == "ok"
    assert _organization_gone(pg)
    kind = "recommendation_brief" if subject == "brief" else "creative_draft"
    assert _approvals(pg.factory) == [(kind, "approved", None, "reviewer")]  # anonymised
    with pg.factory() as s:
        [approval] = s.scalars(select(Approval)).all()
        assert (approval.brief_id, approval.draft_id, approval.organization_id) == (
            None, None, ORG
        )
