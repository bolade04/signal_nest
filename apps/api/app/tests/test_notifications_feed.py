"""In-app notifications: emission, recipients, the feed and mark-read (Project Phase 5E exit
criterion 5, behaviour half; lane 5E-W1 testing lane 5E-W3's code).

On SQLite and PostgreSQL, with the O-12(B) capability opened by a workspace override (the
global flag stays off):

* **The reflection** (``GET …/notifications-analytics-capability``) answers the
  workspace-effective decision: off by default, on with the workspace's override.
* **An export notifies** (``export.completed``, the export service's one emission at this
  head): the draft's author and the approving reviewer, never the exporter, one row each, in
  the export's transaction; with O-12(B) off for the workspace the export still succeeds and
  nothing is written.
* **Recipients per event** (interface sheet P9, the coordinator's default):
  ``brief.created`` → the organization's REVIEWER members; ``approval.decided`` → the author
  of the decided brief or draft; ``draft.generated`` and ``review.blocked`` → the requester
  (the draft's author); ``export.completed`` → the draft's author and the approver. The
  actor is never a recipient.
* **Fan-out**: one row per member per event; a user outside the organization, an unknown id
  and an empty id get nothing; rows are written in the caller's transaction and vanish with
  its rollback.
* **The feed** (``GET …/notifications``) lists the session user's own live notifications in
  that workspace, newest first, paged, with total and unread counts; items carry the id, the
  event code, the subject id and the two instants, nothing else; every member has one.
* **Mark-read** (``POST …/notifications/{id}/read``) sets ``read_at`` once: a repeat, and a
  request that raced another, answer the first ``read_at``; the unread count follows.
* **The hooks** (5E-W3 phase B), each through its real write path: a new brief version
  (``POST …/briefs``) tells the reviewers ``brief.created``; a decision on a brief or a draft
  (``POST …/approvals``) tells the subject's author ``approval.decided``; a generation (the
  route and the real worker) tells the requester ``draft.generated``, and ``review.blocked``
  when the stored review is blocked. With the capability off every one of them succeeds and
  writes nothing. On PostgreSQL, an emission never waits for a recipient's user row held
  elsewhere (account erasure holds it ``FOR UPDATE``): that recipient is skipped and the
  decision commits.

Positive paths and behaviour only: every refusal (cross-user and cross-workspace 404s, the
membership 403, the dark 503s) is lane 5E-W4's.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from sqlalchemy import create_engine, select, text

from app.approvals.models import Approval
from app.approvals.service import record_draft_decision
from app.auth.dependencies import TenantContext
from app.briefs.models import RecommendationBrief
from app.core.config import get_settings
from app.core.enums import ApprovalDecision, NotificationEventCode, Role
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.notifications.models import Notification
from app.notifications.recipients import notify, resolve_recipients
from app.notifications.service import emit_notifications, mark_read
from app.organizations.models import Organization, User, Workspace
from app.tests._creative_support import CEILINGS
from app.tests.fixtures.analytics import T0, T1, Rows, at, creative_world_on, set_capability

CODES = NotificationEventCode
NOTIFICATIONS = "notifications_analytics"


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator:
    with creative_world_on(request.param, tmp_path_factory, "w1feed") as w:
        yield w


def _workspace(world, name: str, *, notifications: bool = True, export: bool = False) -> str:
    ws = world.workspace(name)
    if notifications:
        set_capability(world, ws, NOTIFICATIONS)
    if export:
        set_capability(world, ws, "content_export")
    return ws


def _subjects(world, ws: str) -> dict[str, str]:
    """One live row of each subject table: a brief by the marketer approved by the reviewer, a
    draft of it by the marketer with a passed review and the reviewer's approval, a second
    draft whose review is blocked, and an export of the approved draft by the owner."""
    users = world.users
    with world.session() as s:
        rows = Rows(s, organization_id=world.org_of(ws), workspace_id=ws)
        opp = rows.opportunity(location_id=None)
        ids = {"brief": rows.brief(opportunity_id=opp, author=users["marketer"],
                                   location_id=None, code="US-TX", created_at=T0)}
        ids["brief_decision"] = rows.decision(subject=ids["brief"], kind="brief",
                                              decision="approved", seq=1,
                                              reviewer=users["reviewer"], decided_at=at(T0, 60))
        ids["draft"] = rows.draft(brief_id=ids["brief"], author=users["marketer"], created_at=T1)
        ids["review"] = rows.review(draft_id=ids["draft"], outcome="passed")
        ids["draft_decision"] = rows.decision(subject=ids["draft"], kind="draft",
                                              decision="approved", seq=1,
                                              reviewer=users["reviewer"], decided_at=at(T1, 60))
        ids["blocked_draft"] = rows.draft(brief_id=ids["brief"], author=users["marketer"],
                                          version=2, created_at=at(T1, 5))
        ids["blocked_review"] = rows.review(draft_id=ids["blocked_draft"], outcome="blocked")
        ids["export"] = rows.export(draft_id=ids["draft"], approval_id=ids["draft_decision"],
                                    fmt="text/plain", exporter=users["owner"])
    return ids


def _received(world, ws: str) -> dict[str, list[tuple[str, str]]]:
    """``user id -> [(event code, subject id)]`` of the workspace's notification rows."""
    out: dict[str, list[tuple[str, str]]] = {}
    for row in sorted(world.rows(Notification, workspace_id=ws),
                      key=lambda r: (r["event_code"], r["subject_id"])):
        out.setdefault(row["user_id"], []).append((row["event_code"], row["subject_id"]))
    return out


def _feed(world, ws: str, role: str, query: str = ""):
    r = world.send("GET", f"/workspaces/{ws}/notifications{query}", role)
    assert r.status_code == 200, r.text
    return r.json()


def _export(world, ws: str, draft_id: str, role: str):
    r = world.send("POST", f"/workspaces/{ws}/drafts/{draft_id}/exports", role,
                   {"format": "text/markdown"})
    assert r.status_code == 201, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# The reflection
# --------------------------------------------------------------------------- #
def test_the_reflection_answers_the_workspace_effective_decision(world):
    ws = _workspace(world, "feed reflection", notifications=False)
    path = f"/workspaces/{ws}/notifications-analytics-capability"
    for role in ("owner", "viewer", "compliance_reviewer"):
        r = world.send("GET", path, role)
        assert (r.status_code, r.json()) == (200, {"enabled": False})
    set_capability(world, ws, NOTIFICATIONS)  # the override alone; the global flag stays off
    for role in ("owner", "viewer", "compliance_reviewer"):
        r = world.send("GET", path, role)
        assert (r.status_code, r.json()) == (200, {"enabled": True})


# --------------------------------------------------------------------------- #
# An export notifies (export.completed through the export service)
# --------------------------------------------------------------------------- #
def test_an_export_notifies_the_draft_author_and_the_approver_never_the_exporter(world):
    users = world.users
    ws = _workspace(world, "feed export", export=True)
    ids = _subjects(world, ws)
    base = _received(world, ws)
    assert base == {}

    by_owner = _export(world, ws, ids["draft"], "owner")
    assert _received(world, ws) == {
        users["marketer"]: [("export.completed", by_owner["id"])],
        users["reviewer"]: [("export.completed", by_owner["id"])],
    }
    # The author exports: only the approver is told (the actor is never a recipient).
    by_author = _export(world, ws, ids["draft"], "marketer")
    assert _received(world, ws) == {
        users["marketer"]: [("export.completed", by_owner["id"])],
        users["reviewer"]: sorted([("export.completed", by_owner["id"]),
                                   ("export.completed", by_author["id"])]),
    }
    # The approver reads them in the feed, newest first.
    feed = _feed(world, ws, "reviewer")
    assert [(i["event_code"], i["subject_id"]) for i in feed["items"]] == [
        ("export.completed", by_author["id"]), ("export.completed", by_owner["id"])
    ]
    assert (feed["total"], feed["unread"]) == (2, 2)
    assert _feed(world, ws, "owner")["total"] == 0


def test_an_export_in_a_workspace_without_notifications_writes_none(world):
    ws = _workspace(world, "feed export dark", notifications=False, export=True)
    ids = _subjects(world, ws)
    exported = _export(world, ws, ids["draft"], "owner")
    assert world.row(Export, exported["id"]) is not None
    assert world.rows(Notification, workspace_id=ws) == []


# --------------------------------------------------------------------------- #
# Recipients per event (P9) and fan-out
# --------------------------------------------------------------------------- #
def _load(s, ids: dict[str, str]) -> dict[str, object]:
    return {
        "brief": s.get(RecommendationBrief, ids["brief"]),
        "brief_decision": s.get(Approval, ids["brief_decision"]),
        "draft": s.get(CreativeDraft, ids["draft"]),
        "draft_decision": s.get(Approval, ids["draft_decision"]),
        "blocked_review": s.get(ContentReview, ids["blocked_review"]),
        "export": s.get(Export, ids["export"]),
    }


def test_each_event_reaches_its_recipients(world):
    users = world.users
    ws = _workspace(world, "feed recipients")
    ids = _subjects(world, ws)
    events = [
        (CODES.BRIEF_CREATED, "brief", users["marketer"], [users["reviewer"]]),
        (CODES.APPROVAL_DECIDED, "brief_decision", users["reviewer"], [users["marketer"]]),
        (CODES.APPROVAL_DECIDED, "draft_decision", users["reviewer"], [users["marketer"]]),
        (CODES.DRAFT_GENERATED, "draft", None, [users["marketer"]]),
        (CODES.REVIEW_BLOCKED, "blocked_review", None, [users["marketer"]]),
        (CODES.EXPORT_COMPLETED, "export", users["owner"],
         [users["marketer"], users["reviewer"]]),
    ]
    with world.session() as s:
        subjects = _load(s, ids)
        for code, key, actor, expected in events:
            assert resolve_recipients(s, code, subjects[key], actor_user_id=actor) == expected, (
                code, key)
            assert notify(s, code, subjects[key], actor_user_id=actor) == len(expected)
    received = _received(world, ws)
    assert received == {
        users["reviewer"]: [("brief.created", ids["brief"]),
                            ("export.completed", ids["export"])],
        users["marketer"]: sorted([
            ("approval.decided", ids["brief_decision"]),
            ("approval.decided", ids["draft_decision"]),
            ("draft.generated", ids["draft"]),
            ("export.completed", ids["export"]),
            ("review.blocked", ids["blocked_review"]),
        ]),
    }


def test_the_actor_is_never_a_recipient(world):
    users = world.users
    ws = _workspace(world, "feed actor")
    ids = _subjects(world, ws)
    with world.session() as s:
        subjects = _load(s, ids)
        # The only reviewer creates the brief; the author requests and decides nothing else.
        assert resolve_recipients(s, CODES.BRIEF_CREATED, subjects["brief"],
                                  actor_user_id=users["reviewer"]) == []
        assert resolve_recipients(s, CODES.DRAFT_GENERATED, subjects["draft"],
                                  actor_user_id=users["marketer"]) == []
        assert resolve_recipients(s, CODES.APPROVAL_DECIDED, subjects["draft_decision"],
                                  actor_user_id=users["marketer"]) == []
        assert resolve_recipients(s, CODES.EXPORT_COMPLETED, subjects["export"],
                                  actor_user_id=users["reviewer"]) == [users["marketer"]]
        assert notify(s, CODES.DRAFT_GENERATED, subjects["draft"],
                      actor_user_id=users["marketer"]) == 0
    assert world.rows(Notification, workspace_id=ws) == []


def test_one_row_per_member_and_none_outside_the_organization(world):
    users = world.users
    ws = _workspace(world, "feed fan-out")
    ids = _subjects(world, ws)
    with world.session() as s:
        written = emit_notifications(
            s, organization_id=world.org, workspace_id=ws,
            recipient_user_ids=[users["viewer"], users["viewer"], world.owner_b_id,
                                "0" * 32, "", users["compliance_reviewer"]],
            event_code=CODES.BRIEF_CREATED, subject_id=ids["brief"],
        )
    assert written == 2
    assert _received(world, ws) == {
        users["viewer"]: [("brief.created", ids["brief"])],
        users["compliance_reviewer"]: [("brief.created", ids["brief"])],
    }
    assert world.rows(Notification, user_id=world.owner_b_id) == []


def test_rows_commit_with_the_event_or_not_at_all(world):
    users = world.users
    ws = _workspace(world, "feed transaction")
    ids = _subjects(world, ws)
    s = world.factory()
    try:
        assert emit_notifications(s, organization_id=world.org, workspace_id=ws,
                                  recipient_user_ids=[users["admin"]],
                                  event_code=CODES.BRIEF_CREATED, subject_id=ids["brief"]) == 1
        # Written in the caller's transaction (visible to it) ...
        assert s.scalar(select(Notification.id).where(Notification.workspace_id == ws))
        s.rollback()
    finally:
        s.close()
    # ... and gone with its rollback: the emitter never commits.
    assert world.rows(Notification, workspace_id=ws) == []


# --------------------------------------------------------------------------- #
# The feed
# --------------------------------------------------------------------------- #
def _note(rows: Rows, *, user: str, code: str, subject: str, created, deleted: bool = False,
          workspace_id: str | None = None) -> str:
    row = Notification(organization_id=rows.org, workspace_id=workspace_id or rows.ws,
                       user_id=user, event_code=code, subject_id=subject, created_at=created,
                       deleted_at=created if deleted else None)
    rows.s.add(row)
    rows.s.flush()
    return row.id


def _seed_feed(world, ws: str, other_ws: str) -> dict[str, str]:
    """The viewer's three live notifications in ``ws``, plus rows the feed must leave out: a
    soft-deleted one, one in ``other_ws`` and the reviewer's."""
    viewer, reviewer = world.users["viewer"], world.users["reviewer"]
    with world.session() as s:
        rows = Rows(s, organization_id=world.org, workspace_id=ws)
        return {
            "oldest": _note(rows, user=viewer, code="brief.created", subject="s1", created=T0),
            "middle": _note(rows, user=viewer, code="approval.decided", subject="s2",
                            created=at(T0, 10)),
            "newest": _note(rows, user=viewer, code="export.completed", subject="s3",
                            created=at(T0, 20)),
            "deleted": _note(rows, user=viewer, code="review.blocked", subject="s4",
                             created=at(T0, 30), deleted=True),
            "elsewhere": _note(rows, user=viewer, code="draft.generated", subject="s5",
                               created=at(T0, 40), workspace_id=other_ws),
            "reviewers": _note(rows, user=reviewer, code="brief.created", subject="s6",
                               created=at(T0, 50)),
        }


def test_the_feed_lists_the_users_own_live_rows_newest_first(world):
    ws = _workspace(world, "feed list")
    other = _workspace(world, "feed list other")
    ids = _seed_feed(world, ws, other)
    feed = _feed(world, ws, "viewer")
    assert [i["id"] for i in feed["items"]] == [ids["newest"], ids["middle"], ids["oldest"]]
    assert (feed["total"], feed["unread"], feed["limit"], feed["offset"]) == (3, 3, 20, 0)
    newest = feed["items"][0]
    assert set(newest) == {"id", "event_code", "subject_id", "created_at", "read_at"}
    assert (newest["event_code"], newest["subject_id"], newest["read_at"]) == (
        "export.completed", "s3", None)

    page = _feed(world, ws, "viewer", "?limit=2&offset=1")
    assert [i["id"] for i in page["items"]] == [ids["middle"], ids["oldest"]]
    assert (page["total"], page["unread"], page["limit"], page["offset"]) == (3, 3, 2, 1)

    assert [i["id"] for i in _feed(world, other, "viewer")["items"]] == [ids["elsewhere"]]
    assert [i["id"] for i in _feed(world, ws, "reviewer")["items"]] == [ids["reviewers"]]
    assert _feed(world, ws, "admin")["items"] == []


@pytest.mark.parametrize("role", ["owner", "admin", "marketer", "reviewer",
                                  "compliance_reviewer", "viewer"])
def test_every_member_reads_their_own_feed(world, role):
    ws = _workspace(world, f"feed role {role}")
    user = world.users[role]
    with world.session() as s:
        note = _note(Rows(s, organization_id=world.org, workspace_id=ws), user=user,
                     code="brief.created", subject="s-role", created=T0)
    feed = _feed(world, ws, role)
    assert [i["id"] for i in feed["items"]] == [note] and feed["unread"] == 1


# --------------------------------------------------------------------------- #
# Mark-read
# --------------------------------------------------------------------------- #
def _mark(world, ws: str, note_id: str, role: str = "viewer") -> dict:
    r = world.send("POST", f"/workspaces/{ws}/notifications/{note_id}/read", role)
    assert r.status_code == 200, r.text
    return r.json()


def test_mark_read_sets_read_at_once_and_the_unread_count_follows(world):
    ws = _workspace(world, "feed mark read")
    other = _workspace(world, "feed mark read other")
    ids = _seed_feed(world, ws, other)
    first = _mark(world, ws, ids["middle"])
    assert first["id"] == ids["middle"] and first["read_at"] is not None
    stored = world.row(Notification, ids["middle"])["read_at"]
    assert stored is not None

    again = _mark(world, ws, ids["middle"])
    assert again["read_at"] == first["read_at"]
    assert world.row(Notification, ids["middle"])["read_at"] == stored

    feed = _feed(world, ws, "viewer")
    assert (feed["total"], feed["unread"]) == (3, 2)
    read = {i["id"]: i["read_at"] for i in feed["items"]}
    assert read[ids["middle"]] == first["read_at"]
    assert read[ids["newest"]] is None and read[ids["oldest"]] is None
    assert world.row(Notification, ids["newest"])["read_at"] is None


def test_a_mark_read_that_raced_another_answers_the_first_read_at(world):
    """This request loaded the row unread; another marked it read and committed first. The
    conditional write keeps the first ``read_at`` and the answer carries it."""
    ws = _workspace(world, "feed race")
    other = _workspace(world, "feed race other")
    ids = _seed_feed(world, ws, other)
    note_id, viewer = ids["oldest"], world.users["viewer"]
    s = world.factory()
    try:
        stale = s.get(Notification, note_id)
        assert stale is not None and stale.read_at is None
        winner = _mark(world, ws, note_id)
        stored = world.row(Notification, note_id)["read_at"]
        row = mark_read(s, workspace_id=ws, user_id=viewer, notification_id=note_id)
        s.commit()
        assert row.read_at is not None
        assert row.read_at.replace(tzinfo=None) == stored.replace(tzinfo=None)
    finally:
        s.close()
    assert world.row(Notification, note_id)["read_at"] == stored
    assert winner["read_at"] is not None


def test_read_notifications_stay_in_the_feed_newest_first(world):
    """Reading changes the unread count, never the order (newest created first)."""
    ws = _workspace(world, "feed after read")
    other = _workspace(world, "feed after read other")
    ids = _seed_feed(world, ws, other)
    for key in ("oldest", "newest"):
        _mark(world, ws, ids[key])
    feed = _feed(world, ws, "viewer")
    assert [i["id"] for i in feed["items"]] == [ids["newest"], ids["middle"], ids["oldest"]]
    assert (feed["total"], feed["unread"]) == (3, 1)


# --------------------------------------------------------------------------- #
# The hooks (5E-W3 phase B): each event through its real write path
# --------------------------------------------------------------------------- #
@pytest.fixture
def generation_ceilings(monkeypatch) -> None:
    """Every generation ceiling set (they ship unset, and generation refuses until set)."""
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 1000}.items():
        monkeypatch.setattr(get_settings(), name, value)


def _hook_workspace(world, name: str, *, notifications: bool = True) -> str:
    """A workspace with briefs and creative generation open by override (flags off)."""
    ws = world.workspace(name)  # creative_generation: the world's override
    set_capability(world, ws, "recommendation_briefs")
    if notifications:
        set_capability(world, ws, NOTIFICATIONS)
    return ws


def _new_brief_version(world, ws: str) -> tuple[str, str]:
    """A brief version captured through ``POST …/briefs`` by the marketer (create_brief)."""
    seeded = world.row(RecommendationBrief, world.seed_brief(ws, approved=False))
    r = world.send("POST", f"/workspaces/{ws}/opportunities/{seeded['opportunity_id']}/briefs",
                   "marketer", {"intelligence_record_id": seeded["intelligence_record_id"]})
    assert r.status_code == 201, r.text
    return r.json()["id"], seeded["opportunity_id"]


def _decide_brief(world, ws: str, opportunity_id: str, brief_id: str, decision: str) -> str:
    version = world.row(RecommendationBrief, brief_id)["brief_version"]
    r = world.send("POST",
                   f"/workspaces/{ws}/opportunities/{opportunity_id}/briefs/{brief_id}/approvals",
                   "reviewer", {"decision": decision, "subject_version": version})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _decide_draft(world, ws: str, draft_id: str, decision: str) -> str:
    r = world.send("POST", f"/workspaces/{ws}/drafts/{draft_id}/approvals", "reviewer",
                   {"decision": decision, "subject_version": 1})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _generate(world, ws: str) -> str:
    """One draft of a fresh approved brief, requested by the marketer and run by the worker."""
    brief = world.seed_brief(ws)  # approved by the reviewer (a 5B row, written directly)
    job = world.generated(brief, ws=ws)
    assert job["status"] == "succeeded", (job["last_error_code"], job["last_error_summary"])
    [draft] = world.rows(CreativeDraft, brief_id=brief)
    return draft["id"]


def test_creating_a_brief_notifies_the_reviewers_not_its_author(world, generation_ceilings):
    ws = _hook_workspace(world, "hook brief")
    brief_id, _ = _new_brief_version(world, ws)
    assert _received(world, ws) == {world.users["reviewer"]: [("brief.created", brief_id)]}


def test_a_brief_decision_notifies_the_briefs_author(world, generation_ceilings):
    users = world.users
    ws = _hook_workspace(world, "hook brief decision", notifications=False)
    brief_id, opportunity_id = _new_brief_version(world, ws)
    set_capability(world, ws, NOTIFICATIONS)
    approval_id = _decide_brief(world, ws, opportunity_id, brief_id, "approved")
    assert _received(world, ws) == {users["marketer"]: [("approval.decided", approval_id)]}


def test_generation_notifies_the_requester_and_a_blocked_review_too(world, generation_ceilings):
    marketer = world.users["marketer"]
    ws = _hook_workspace(world, "hook generation")
    passed = _generate(world, ws)
    assert _received(world, ws) == {marketer: [("draft.generated", passed)]}

    world.claim("weekend parking", ws=ws)  # the claims library now blocks the model's text
    blocked = _generate(world, ws)
    [review] = world.rows(ContentReview, draft_id=blocked)
    assert review["is_blocked"] is True
    assert _received(world, ws) == {marketer: sorted([
        ("draft.generated", passed),
        ("draft.generated", blocked),
        ("review.blocked", review["id"]),
    ])}


def test_a_draft_decision_notifies_the_drafts_author(world, generation_ceilings):
    marketer = world.users["marketer"]
    ws = _hook_workspace(world, "hook draft decision", notifications=False)
    draft = _generate(world, ws)
    set_capability(world, ws, NOTIFICATIONS)
    approved = _decide_draft(world, ws, draft, "approved")
    revoked = _decide_draft(world, ws, draft, "revoked")
    assert _received(world, ws) == {marketer: sorted([
        ("approval.decided", approved), ("approval.decided", revoked),
    ])}


def test_with_notifications_off_every_hook_succeeds_and_writes_none(world, generation_ceilings):
    ws = _hook_workspace(world, "hook dark", notifications=False)
    brief_id, opportunity_id = _new_brief_version(world, ws)
    _decide_brief(world, ws, opportunity_id, brief_id, "approved")
    world.claim("weekend parking", ws=ws)
    draft = _generate(world, ws)
    assert world.rows(ContentReview, draft_id=draft)[0]["is_blocked"] is True
    _decide_draft(world, ws, draft, "rejected")
    assert world.rows(Notification, workspace_id=ws) == []


@contextmanager
def _row_held_for_update(world, user_id: str) -> Iterator[None]:
    """Another connection holds ``user_id``'s users row FOR UPDATE (as account erasure does)
    until the block ends."""
    other = create_engine(world.engine.url, future=True)
    conn = other.connect()
    tx = conn.begin()
    try:
        conn.execute(text("SELECT id FROM users WHERE id = :u FOR UPDATE"), {"u": user_id})
        yield
    finally:
        tx.rollback()
        conn.close()
        other.dispose()


def _decide_draft_in_session(world, ws: str, draft_id: str, decision: ApprovalDecision) -> str:
    """``record_draft_decision`` as the route runs it, bounded by a 2 s lock timeout."""
    s = world.factory()
    try:
        s.execute(text("SET LOCAL lock_timeout = '2s'"))
        ctx = TenantContext(user=s.get(User, world.users["reviewer"]),
                            organization=s.get(Organization, world.org),
                            workspace=s.get(Workspace, ws), role=Role.REVIEWER)
        approval = record_draft_decision(s, ctx=ctx, draft=s.get(CreativeDraft, draft_id),
                                         decision=decision, subject_version=1)
        s.commit()
        return approval.id
    finally:
        s.close()


def test_an_emission_never_waits_for_a_recipient_row_held_elsewhere(world, generation_ceilings):
    if world.engine.dialect.name != "postgresql":
        pytest.skip("row locks are PostgreSQL's (SQLite serializes every writer)")
    marketer = world.users["marketer"]
    ws = _hook_workspace(world, "hook lock", notifications=False)
    draft = _generate(world, ws)
    set_capability(world, ws, NOTIFICATIONS)
    with _row_held_for_update(world, marketer):
        skipped = _decide_draft_in_session(world, ws, draft, ApprovalDecision.APPROVED)
    # The decision committed without waiting; the author, whose row was held, was skipped.
    assert world.row(Approval, skipped) is not None
    assert world.rows(Notification, workspace_id=ws) == []
    # Released: the next decision reaches the author.
    revoked = _decide_draft_in_session(world, ws, draft, ApprovalDecision.REVOKED)
    assert _received(world, ws) == {marketer: [("approval.decided", revoked)]}
