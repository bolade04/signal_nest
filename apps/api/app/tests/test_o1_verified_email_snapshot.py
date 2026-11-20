"""O-1 repair R-a: a new Phase 5 actor snapshot carries the address only when it is verified.

The operator's instruction of 2026-11-20 (register §49). §1: "Prevent the retained, unverified
email value from being copied into new Phase 5 actor snapshots." §2: "Include the email
component only when verification applies to the email value being recorded. Otherwise use a
consistent fixed marker. Preserve the leading account ID, name, length bound and downstream
identity checks." §3: "Use failing-first tests and the actual application paths on SQLite and
PostgreSQL."

**The world** is the 5C harness (:mod:`app.tests._creative_support`) on two engines: SQLite
through the application's own engine factory and settings
(:func:`app.tests.test_i5e_export_serialization_sqlite.application_sqlite_world`), and
PostgreSQL (``TEST_POSTGRES_URL``; a database of its own, migrated to head). Every account
registers or joins through the real routes, so every address starts unverified. A test
verifies an address only through the real flow (``/auth/email-verification/request`` and
``/confirm``; the mail is the test environment's in-memory outbox). Every action goes through
its real route, and a draft through the durable creative job run by the real worker, with the
mock provider. Each assertion reads the committed row raw on a separate connection
(``CreativeWorld.row``). The expected values are written out here, never computed by the code
under test.

What is proven:

1. **Every writer** (brief generation, draft generation, a brief decision, a draft decision, an
   export) records ``"<id> <name> <[unverified]>"`` for an actor whose address is not
   verified, and ``"<id> <name> <address>"`` for one who verified it. The API answers the same
   value.
2. **Erasure first.** An unverified account holding the address (one self-registered, one that
   joined by invitation) keeps its account after ``erase_invitee_email`` or the CLI's
   ``erase-email --confirm``. Its later brief, draft, decisions and export name no address. The
   operator's verification still reports exactly the ``users.email`` copy (``EXIT_FINDINGS``).
   It still signs in, with the same memberships and roles.
3. **Legitimate verification afterwards.** The same account verifies through the real flow. Its
   next snapshot carries the address, ``erase-email`` is refused (``erasure_account_exists``),
   its own privacy export lists both values as stored, and its own deletion erases both.
4. **Identity-based authorization.** An unverified author cannot decide their own brief or
   draft (403). The author check reads the leading id of a marker snapshot once ``created_by``
   is NULL. That state is not reachable through the real paths: ``created_by`` is nulled only by
   the foreign key when the ``users`` row is deleted, the same deletion first replaces the
   snapshot with ``"[erased]"`` (``app.privacy.erasure.delete_account``), and a deleted account
   cannot authenticate. So it is tested on the functions directly.
5. **History.** A row written in the old format before the change stays byte-identical through
   later actions of the same account.
6. **Erasure matching.** The unverified account's deletion clears its marker snapshots (by id);
   ``erase-email`` still clears an old-format row naming the address and leaves a marker row.
7. **The helper.** The marker is fixed, is not an address, is not ``"[erased]"`` and names
   nobody to the erasure matcher. The 600-character bound holds and a maximal name fits. No
   production module formats a snapshot except through the one helper.

Tenant isolation and the approval requirements are not re-proven here: the existing modules
(``test_approvals_isolation``, ``test_briefs_isolation``, ``test_creative_isolation``,
``test_exports_isolation``, ``test_cross_organization_isolation``, ``test_approvals_api``,
``test_creative_approvals_api``, ``test_dn4_38_brief_approved_parent``) run unchanged.
"""

from __future__ import annotations

import ast
import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select

import app as app_package
from app.approvals.models import Approval
from app.approvals.service import is_brief_author, is_draft_author
from app.briefs.models import RecommendationBrief
from app.core.actor_snapshot import actor_snapshot
from app.core.config import get_settings
from app.creative.models import CreativeDraft
from app.exports.models import Export
from app.infra import mail
from app.organizations.models import User
from app.privacy import cli as privacy_cli
from app.privacy.erasure import erase_invitee_email
from app.privacy.matching import Mention, Subject, mention, registrable
from app.tests._auth2_support import VERIFY_PATH, link_token
from app.tests._creative_support import CEILINGS, CreativeWorld, postgres_creative_world
from app.tests.test_campaign_context_integrity import API, PASSWORD, _rate_limiter
from app.tests.test_exports_refusal_preconditions import decide, draft, set_cap
from app.tests.test_i5e_export_serialization_sqlite import application_sqlite_world

#: The e-mail component of an unverified actor's snapshot (operator §2: "a consistent fixed
#: marker"). Written out, not imported: the tests must not take it from the code under test.
MARKER = "[unverified]"
ERASED = "[erased]"
#: What the operator's verification of the address reports after the erasure: the retained
#: ``users.email`` of the unverified holder, and nothing else (operator §1: "The retained
#: users.email copy must remain disclosed").
RESIDUE = [{"table": "users", "column": "email", "rows": 1}]

WRITERS = ("brief", "draft", "brief_decision", "draft_decision", "export")
#: The role each writer's route admits (EDITORS, GENERATOR_ROLES, APPROVER_ROLES, EXPORTER_ROLES).
ROLE_FOR = {
    "brief": "marketer",
    "draft": "marketer",
    "export": "marketer",
    "brief_decision": "reviewer",
    "draft_decision": "reviewer",
}
#: The table and snapshot column each writer fills.
COLUMN = {
    "brief": (RecommendationBrief, "created_by_snapshot", "created_by"),
    "draft": (CreativeDraft, "created_by_snapshot", "created_by"),
    "brief_decision": (Approval, "decided_by_snapshot", "decided_by"),
    "draft_decision": (Approval, "decided_by_snapshot", "decided_by"),
    "export": (Export, "exported_by_snapshot", "exported_by"),
}
SNAPSHOT_COLUMNS = (
    (RecommendationBrief, "created_by_snapshot"),
    (CreativeDraft, "created_by_snapshot"),
    (Approval, "decided_by_snapshot"),
    (Approval, "note"),
    (Export, "exported_by_snapshot"),
)
KINDS = ("self-registered", "invited")


# --------------------------------------------------------------------------- #
# The world: SQLite (the application's engine factory) and PostgreSQL
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        db_file = tmp_path_factory.mktemp("o1ra") / "app.db"
        with application_sqlite_world(db_file, "o1ra") as w:
            yield w
    else:
        with postgres_creative_world("o1ra") as w:  # skips unless TEST_POSTGRES_URL is set
            yield w


@pytest.fixture(autouse=True)
def settings_for_the_world(monkeypatch) -> Iterator[None]:
    """Every generation ceiling set (CF-2 leaves them unset), and the test environment's
    in-memory mail outbox, from which a verification link is read. The global capability
    flags stay off: each workspace is opened by override rows, as an operator would."""
    settings = get_settings()
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 1000}.items():
        monkeypatch.setattr(settings, name, value)
    for flag in ("recommendation_briefs_enabled", "creative_generation_enabled",
                 "content_export_enabled", "notifications_analytics_enabled"):
        monkeypatch.setattr(settings, flag, False)
    monkeypatch.setattr(settings, "mail_backend", "memory")
    mail.reset_mail_sender()
    try:
        yield
    finally:
        mail.reset_mail_sender()


# --------------------------------------------------------------------------- #
# Accounts
# --------------------------------------------------------------------------- #
@dataclass
class Actor:
    user_id: str
    email: str
    full_name: str
    token: str
    kind: str

    def snapshot(self, *, verified: bool) -> str:
        """The expected snapshot, written out: the id, the name, then the address or the
        marker in the e-mail position."""
        return f"{self.user_id} {self.full_name} <{self.email if verified else MARKER}>"


def _tag() -> str:
    return uuid.uuid4().hex[:10]


def _call(w: CreativeWorld, method: str, path: str, body=None, *, token: str | None = None):
    _rate_limiter()._hits.clear()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    kwargs: dict = {"headers": headers}
    if body is not None:
        kwargs["json"] = body
    return w.client.request(method, API + path, **kwargs)


def _ok(response, status: int = 201) -> dict:
    assert response.status_code == status, response.text
    return response.json() if response.content else {}


def _invited(w: CreativeWorld, role: str) -> Actor:
    """An account that registered through an invitation to organization A (unverified)."""
    tag = _tag()
    email, name = f"o1ra-inv-{tag}@example.com", f"Invited Actor {tag}"
    invitation = _ok(_call(w, "POST", f"/organizations/{w.org}/invitations",
                           {"email": email, "role": role}, token=w.tokens["owner"]))
    joined = _ok(_call(w, "POST", "/auth/invitations/register",
                       {"token": invitation["token"], "full_name": name, "password": PASSWORD}))
    return Actor(joined["user"]["id"], email, name, joined["access_token"], "invited")


def _self_registered(w: CreativeWorld, role: str) -> Actor:
    """An account that registered itself (the OWNER of its own organization, unverified), then
    accepted an invitation to organization A while signed in."""
    tag = _tag()
    email, name = f"o1ra-reg-{tag}@example.com", f"Registered Actor {tag}"
    registered = _ok(_call(w, "POST", "/auth/register", {
        "email": email, "full_name": name, "password": PASSWORD,
        "organization_name": f"O1RA own organization {tag}",
    }))
    invitation = _ok(_call(w, "POST", f"/organizations/{w.org}/invitations",
                           {"email": email, "role": role}, token=w.tokens["owner"]))
    _ok(_call(w, "POST", "/auth/invitations/accept", {"token": invitation["token"]},
              token=registered["access_token"]), 200)
    return Actor(registered["user"]["id"], email, name, registered["access_token"],
                 "self-registered")


def _actor(w: CreativeWorld, kind: str, role: str) -> Actor:
    return _self_registered(w, role) if kind == "self-registered" else _invited(w, role)


def _set_role(w: CreativeWorld, actor: Actor, role: str) -> None:
    """Organization A's owner changes the actor's role through the real route."""
    _ok(_call(w, "PUT", f"/organizations/{w.org}/members/{actor.user_id}/role", {"role": role},
              token=w.tokens["owner"]), 200)


def _verify(w: CreativeWorld, actor: Actor) -> None:
    """The real verification flow: request a link, read it from the outbox, confirm it."""
    outbox = mail.get_mail_sender().outbox
    before = len(outbox)
    _ok(_call(w, "POST", "/auth/email-verification/request", {}, token=actor.token), 204)
    [message] = [m for m in outbox[before:] if m.to == actor.email]
    raw = link_token(message, VERIFY_PATH)
    _ok(_call(w, "POST", "/auth/email-verification/confirm", {"token": raw}, token=actor.token),
        204)
    assert w.row(User, actor.user_id)["email_verified_at"] is not None


def _memberships(w: CreativeWorld, actor: Actor) -> list[tuple[str, str]]:
    me = _ok(_call(w, "GET", "/auth/me", token=actor.token), 200)
    return sorted((m["organization_id"], m["role"]) for m in me["memberships"])


def _sign_in(w: CreativeWorld, actor: Actor) -> list[tuple[str, str]]:
    """A fresh sign-in with the account's password; returns its memberships and roles."""
    session = _ok(_call(w, "POST", "/auth/login",
                        {"email": actor.email, "password": PASSWORD}), 200)
    assert session["user"]["id"] == actor.user_id
    return sorted((m["organization_id"], m["role"]) for m in session["memberships"])


# --------------------------------------------------------------------------- #
# Workspaces and actions
# --------------------------------------------------------------------------- #
def _workspace(w: CreativeWorld, name: str) -> str:
    """A fresh workspace of organization A with briefs, generation and export opened by
    override rows (the global flags stay off)."""
    ws = w.workspace(f"o1ra {name}")  # creative_generation on
    set_cap(w, ws, "recommendation_briefs", True)
    set_cap(w, ws, "content_export", True)
    return ws


def _act(w: CreativeWorld, ws: str, writer: str, actor: Actor) -> tuple[str, str]:
    """``actor`` performs ``writer``'s action through its real route on material written by
    organization A's other members; returns ``(new row id, the snapshot the API answered)``."""
    if writer == "brief":
        source = w.row(RecommendationBrief, w.seed_brief(ws, approved=False))
        body = _ok(_call(
            w, "POST", f"/workspaces/{ws}/opportunities/{source['opportunity_id']}/briefs",
            {"intelligence_record_id": source["intelligence_record_id"]}, token=actor.token,
        ))
        return body["id"], body["created_by_snapshot"]
    if writer == "draft":
        brief_id = w.seed_brief(ws)  # approved by the organization's reviewer
        queued = _ok(_call(w, "POST", f"/workspaces/{ws}/briefs/{brief_id}/drafts",
                           {"channel": "social_post"}, token=actor.token), 202)
        w.drain()
        job = w.job(queued["job_id"])
        assert job["status"] == "succeeded", job
        [made] = w.rows(CreativeDraft, brief_id=brief_id)
        shown = _ok(_call(w, "GET", f"/workspaces/{ws}/drafts/{made['id']}", token=actor.token),
                    200)
        return made["id"], shown["created_by_snapshot"]
    if writer == "brief_decision":
        brief = w.row(RecommendationBrief, w.seed_brief(ws, approved=False))
        body = _ok(_call(
            w, "POST",
            f"/workspaces/{ws}/opportunities/{brief['opportunity_id']}/briefs/{brief['id']}"
            "/approvals",
            {"decision": "approved", "subject_version": brief["brief_version"]},
            token=actor.token,
        ))
        return body["id"], body["decided_by_snapshot"]
    if writer == "draft_decision":
        made = draft(w, ws)  # by the organization's marketer; passed review; brief approved
        body = _ok(_call(w, "POST", f"/workspaces/{ws}/drafts/{made}/approvals",
                         {"decision": "approved", "subject_version": 1}, token=actor.token))
        return body["id"], body["decided_by_snapshot"]
    assert writer == "export"
    made = draft(w, ws)
    decide(w, made, "approved")  # by the organization's reviewer
    body = _ok(_call(w, "POST", f"/workspaces/{ws}/drafts/{made}/exports",
                     {"format": "text/plain"}, token=actor.token))
    return body["id"], body["exported_by_snapshot"]


def _stored(w: CreativeWorld, writer: str, row_id: str) -> tuple[str, str | None]:
    """``(snapshot, actor foreign key)`` of the committed row, read raw."""
    model, snapshot, actor = COLUMN[writer]
    row = w.row(model, row_id)
    assert row is not None, (writer, row_id)
    return row[snapshot], row[actor]


def _act_as_every_writer(w: CreativeWorld, ws: str, actor: Actor) -> dict[str, tuple[str, str]]:
    """The actor performs all five actions: as marketer, then (role changed by the owner) as
    reviewer. Returns ``{writer: (row id, answered snapshot)}``."""
    done = {writer: _act(w, ws, writer, actor) for writer in ("brief", "draft", "export")}
    _set_role(w, actor, "reviewer")
    done.update({writer: _act(w, ws, writer, actor)
                 for writer in ("brief_decision", "draft_decision")})
    return done


def _phase5_values_naming(w: CreativeWorld, needle: str) -> list[tuple[str, str, str]]:
    """Every Phase 5 snapshot or note that contains ``needle`` (letter case ignored), read raw."""
    found = []
    with w.witness.connect() as conn:
        for model, column in SNAPSHOT_COLUMNS:
            table = model.__table__
            for row in conn.execute(
                select(table.c.id, table.c[column]).where(
                    func.lower(table.c[column]).contains(needle.lower())
                )
            ):
                found.append((table.name, column, row[1]))
    return found


def _cli(w: CreativeWorld, monkeypatch, capsys, *argv: str) -> tuple[int, dict]:
    """The operator's CLI, in process, on the world's database; ``(exit status, its JSON)``."""
    monkeypatch.setattr(privacy_cli, "SessionLocal", w.factory)
    capsys.readouterr()
    code = privacy_cli.main(list(argv))
    return code, json.loads(capsys.readouterr().out)


def _erase(w: CreativeWorld, monkeypatch, capsys, how: str, address: str) -> None:
    """The operator's invitee-email erasure of ``address``: the service function, or the CLI.
    Either way the operator's verification then reports exactly the retained users.email."""
    if how == "service":
        with w.session() as s:
            erase_invitee_email(s, email=address)
        code, report = _cli(w, monkeypatch, capsys, "verify-email", "--email", address)
    else:
        code, printed = _cli(w, monkeypatch, capsys, "erase-email", "--email", address,
                             "--confirm")
        report = printed["verification"]
    assert code == privacy_cli.EXIT_FINDINGS, report
    assert report["content_findings"] == RESIDUE, report


# --------------------------------------------------------------------------- #
# 1. Every writer: the address only when verified
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("verified", [False, True], ids=["unverified", "verified"])
@pytest.mark.parametrize("writer", WRITERS)
def test_each_writer_records_the_address_only_for_a_verified_actor(
    world: CreativeWorld, writer: str, verified: bool
):
    ws = _workspace(world, f"{writer} {verified}")
    actor = _invited(world, ROLE_FOR[writer])
    if verified:
        _verify(world, actor)
    row_id, answered = _act(world, ws, writer, actor)

    expected = actor.snapshot(verified=verified)
    assert _stored(world, writer, row_id) == (expected, actor.user_id)
    assert answered == expected
    if not verified:
        assert _phase5_values_naming(world, actor.email) == []


# --------------------------------------------------------------------------- #
# 2. Erasure first, then later actions by the retained unverified account
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("how", ["service", "cli"])
@pytest.mark.parametrize("kind", KINDS)
def test_after_the_invitee_erasure_the_retained_unverified_account_writes_no_address(
    world: CreativeWorld, kind: str, how: str, monkeypatch, capsys
):
    ws = _workspace(world, f"erasure first {kind} {how}")
    actor = _actor(world, kind, "marketer")
    memberships = _memberships(world, actor)
    assert (world.org, "marketer") in memberships

    _erase(world, monkeypatch, capsys, how, actor.email)

    # The account is not deleted or altered (operator §1), and its access is intact.
    account = world.row(User, actor.user_id)
    assert (account["email"], account["email_verified_at"]) == (actor.email, None)
    assert _sign_in(world, actor) == memberships
    assert _memberships(world, actor) == memberships

    done = _act_as_every_writer(world, ws, actor)

    expected = actor.snapshot(verified=False)
    for writer, (row_id, answered) in done.items():
        assert _stored(world, writer, row_id) == (expected, actor.user_id), writer
        assert answered == expected, writer
    assert _phase5_values_naming(world, actor.email) == []
    # The disclosed residue is unchanged: the retained users.email copy, and nothing else.
    code, report = _cli(world, monkeypatch, capsys, "verify-email", "--email", actor.email)
    assert (code, report["content_findings"]) == (privacy_cli.EXIT_FINDINGS, RESIDUE), report


# --------------------------------------------------------------------------- #
# 3. Legitimate verification afterwards
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", KINDS)
def test_once_the_account_verifies_its_snapshots_carry_the_address_and_its_deletion_erases_both(
    world: CreativeWorld, kind: str, monkeypatch, capsys
):
    ws = _workspace(world, f"verify later {kind}")
    actor = _actor(world, kind, "marketer")
    _erase(world, monkeypatch, capsys, "cli", actor.email)
    before, _answered = _act(world, ws, "brief", actor)
    assert _stored(world, "brief", before) == (actor.snapshot(verified=False), actor.user_id)

    _verify(world, actor)
    after, answered = _act(world, ws, "brief", actor)
    exported, exported_answer = _act(world, ws, "export", actor)
    assert _stored(world, "brief", after) == (actor.snapshot(verified=True), actor.user_id)
    assert _stored(world, "export", exported) == (actor.snapshot(verified=True), actor.user_id)
    assert answered == exported_answer == actor.snapshot(verified=True)

    # The address is now the account's own: erase-email is refused in favour of its deletion.
    code, printed = _cli(world, monkeypatch, capsys, "erase-email", "--email", actor.email,
                         "--confirm")
    assert (code, printed) == (privacy_cli.EXIT_REFUSED, {"refused": "erasure_account_exists"})

    # The account's own privacy export lists each snapshot as stored.
    own = _ok(_call(world, "GET", "/account/export", token=actor.token), 200)
    assert {(r["id"], r["created_by_snapshot"]) for r in own["briefs_authored"]} == {
        (before, actor.snapshot(verified=False)), (after, actor.snapshot(verified=True)),
    }
    assert [(r["id"], r["exported_by_snapshot"]) for r in own["exports_performed"]] == [
        (exported, actor.snapshot(verified=True))
    ]

    # Its own deletion erases both formats (by id), and no Phase 5 snapshot or note names the
    # id or the address.
    _ok(_call(world, "POST", "/account/delete",
              {"current_password": PASSWORD, "confirmation": "DELETE"}, token=actor.token), 200)
    assert _stored(world, "brief", before) == (ERASED, None)
    assert _stored(world, "brief", after) == (ERASED, None)
    assert _stored(world, "export", exported) == (ERASED, None)
    assert _phase5_values_naming(world, actor.email) == []
    assert _phase5_values_naming(world, actor.user_id) == []


# --------------------------------------------------------------------------- #
# 4. Identity-based authorization with the marker
# --------------------------------------------------------------------------- #
def test_an_unverified_author_cannot_decide_their_own_brief_or_draft(world: CreativeWorld):
    ws = _workspace(world, "self approval")
    actor = _invited(world, "marketer")
    brief_id, _answered = _act(world, ws, "brief", actor)
    draft_id, _answered = _act(world, ws, "draft", actor)
    _set_role(world, actor, "reviewer")

    brief = world.row(RecommendationBrief, brief_id)
    refused = _call(
        world, "POST",
        f"/workspaces/{ws}/opportunities/{brief['opportunity_id']}/briefs/{brief_id}/approvals",
        {"decision": "approved", "subject_version": brief["brief_version"]}, token=actor.token,
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "approval_author_conflict"
    refused = _call(world, "POST", f"/workspaces/{ws}/drafts/{draft_id}/approvals",
                    {"decision": "approved", "subject_version": 1}, token=actor.token)
    assert refused.status_code == 403, refused.text
    assert refused.json()["error"]["code"] == "approval_author_conflict"
    assert world.count(Approval, subject_id=brief_id) == 0
    assert world.count(Approval, subject_id=draft_id) == 0
    # ... and the author's snapshots are the marker form (the refusals above did not need it).
    assert _stored(world, "brief", brief_id) == (actor.snapshot(verified=False), actor.user_id)
    assert _stored(world, "draft", draft_id) == (actor.snapshot(verified=False), actor.user_id)


def test_the_author_check_reads_the_leading_id_of_a_marker_snapshot(world: CreativeWorld):
    """``created_by`` NULL with a live author is not reachable through the real paths (module
    docstring, item 4), so the committed marker snapshots are given to the checks directly."""
    ws = _workspace(world, "leading id")
    actor = _invited(world, "marketer")
    brief_id, _answered = _act(world, ws, "brief", actor)
    draft_id, _answered = _act(world, ws, "draft", actor)
    brief_snapshot, _fk = _stored(world, "brief", brief_id)
    draft_snapshot, _fk = _stored(world, "draft", draft_id)

    nulled_brief = SimpleNamespace(created_by=None, created_by_snapshot=brief_snapshot)
    nulled_draft = SimpleNamespace(created_by=None, created_by_snapshot=draft_snapshot)
    assert is_brief_author(nulled_brief, actor.user_id)
    assert is_draft_author(nulled_draft, actor.user_id)
    for other in (world.users["reviewer"], f"{actor.user_id}x", actor.user_id[:-1], MARKER):
        assert not is_brief_author(nulled_brief, other)
        assert not is_draft_author(nulled_draft, other)
    # ... on the marker form the author's own actions committed.
    assert brief_snapshot == draft_snapshot == actor.snapshot(verified=False)


# --------------------------------------------------------------------------- #
# 5. Historical rows are preserved
# --------------------------------------------------------------------------- #
def _historical_rows(w: CreativeWorld, ws: str, actor: Actor) -> tuple[str, str]:
    """A brief and a decision of ``actor`` written as N's services wrote them, before the
    change: the snapshot names the address whatever its verification."""
    old = f"{actor.user_id} {actor.full_name} <{actor.email}>"
    source = w.row(RecommendationBrief, w.seed_brief(ws, approved=False))
    tag = _tag()
    content = {k: source[k] for k in (
        "organization_id", "workspace_id", "opportunity_id", "intelligence_record_id",
        "analysis_version", "scoring_version", "prompt_name", "prompt_version",
        "model_identifier", "input_hash", "is_simulated", "summary", "rationale",
        "recommended_actions", "suggested_angles", "risk_notes", "evidence_refs",
        "jurisdiction_code",
    )}
    with w.session() as s:
        s.add(RecommendationBrief(id=f"ob{tag}", brief_version=source["brief_version"] + 1,
                                  parent_brief_id=source["id"], created_by=actor.user_id,
                                  created_by_snapshot=old, **content))
        s.add(Approval(
            organization_id=source["organization_id"], workspace_id=source["workspace_id"],
            subject_type="recommendation_brief", subject_id=source["id"],
            subject_version=source["brief_version"], brief_id=source["id"], decision="rejected",
            decision_seq=1, reason_code="off_strategy", decided_by=actor.user_id,
            decided_by_snapshot=old, decided_role="reviewer", is_simulated=True,
        ))
    [approval] = w.rows(Approval, subject_id=source["id"])
    return f"ob{tag}", approval["id"]


def test_rows_written_in_the_old_format_are_byte_identical_after_later_actions(
    world: CreativeWorld,
):
    ws = _workspace(world, "history")
    actor = _invited(world, "marketer")
    old_brief, old_approval = _historical_rows(world, ws, actor)
    before = (world.row(RecommendationBrief, old_brief), world.row(Approval, old_approval))
    assert before[0]["created_by_snapshot"] == f"{actor.user_id} {actor.full_name} <{actor.email}>"

    # A new version on the same opportunity as the old row, by the same account; then every
    # other action of that account.
    brief = world.row(RecommendationBrief, old_brief)
    newer = _ok(_call(world, "POST",
                      f"/workspaces/{ws}/opportunities/{brief['opportunity_id']}/briefs",
                      {"intelligence_record_id": brief["intelligence_record_id"]},
                      token=actor.token))
    assert newer["parent_brief_id"] == old_brief
    assert _stored(world, "brief", newer["id"]) == (actor.snapshot(verified=False), actor.user_id)
    done = _act_as_every_writer(world, ws, actor)

    assert (world.row(RecommendationBrief, old_brief), world.row(Approval, old_approval)) == before
    for writer, (row_id, _answered) in done.items():
        assert _stored(world, writer, row_id) == (actor.snapshot(verified=False), actor.user_id)


# --------------------------------------------------------------------------- #
# 6. Erasure matching
# --------------------------------------------------------------------------- #
def test_the_unverified_accounts_deletion_clears_its_marker_snapshots_by_id(
    world: CreativeWorld,
):
    ws = _workspace(world, "deletion by id")
    actor = _invited(world, "marketer")
    done = _act_as_every_writer(world, ws, actor)
    for writer, (row_id, _answered) in done.items():
        assert _stored(world, writer, row_id) == (actor.snapshot(verified=False), actor.user_id)

    _ok(_call(world, "POST", "/account/delete",
              {"current_password": PASSWORD, "confirmation": "DELETE"}, token=actor.token), 200)

    for writer, (row_id, _answered) in done.items():
        assert _stored(world, writer, row_id) == (ERASED, None), writer
    assert _phase5_values_naming(world, actor.user_id) == []


def test_erase_email_clears_an_old_format_row_and_leaves_a_marker_row(
    world: CreativeWorld, monkeypatch, capsys
):
    ws = _workspace(world, "erase-email matching")
    actor = _invited(world, "marketer")
    old_brief, old_approval = _historical_rows(world, ws, actor)
    new_brief, _answered = _act(world, ws, "brief", actor)
    assert _stored(world, "brief", new_brief) == (actor.snapshot(verified=False), actor.user_id)

    _erase(world, monkeypatch, capsys, "cli", actor.email)

    assert _stored(world, "brief", old_brief) == (ERASED, actor.user_id)
    assert world.row(Approval, old_approval)["decided_by_snapshot"] == ERASED
    assert _stored(world, "brief", new_brief) == (actor.snapshot(verified=False), actor.user_id)
    assert _phase5_values_naming(world, actor.email) == []


def test_erase_email_clears_every_old_format_snapshot_and_leaves_every_marker_row(
    world: CreativeWorld, monkeypatch, capsys
):
    """Review RA-B F-RAB-1 and O-RAB-1; coordinator ruling RA-3 (adapted from RA-B's probe
    ``test_zz_rab_probe_historical_export``). The runbook promises that rows written before the
    change and naming the address are still cleared by ``erase-email``. For each of the four
    snapshot columns (``recommendation_briefs.created_by_snapshot``,
    ``creative_drafts.created_by_snapshot``, ``approvals.decided_by_snapshot``, twice: a brief
    and a draft decision, and ``exports.exported_by_snapshot``), an unverified holder of the
    address has a marker row written through the real path and, beside it, a copy written as N's
    writers wrote it: the raw formatter with the address. ``erase-email --confirm``, through the
    real CLI, turns every old snapshot into ``[erased]`` and keeps its actor column, leaves every
    marker row byte-identical, and still reports exactly the holder's ``users.email``."""
    ws = _workspace(world, "erase-email every old format")
    actor = _invited(world, "marketer")
    marker_rows = _act_as_every_writer(world, ws, actor)
    old = actor_snapshot(user_id=actor.user_id, full_name=actor.full_name, email=actor.email)
    assert old == f"{actor.user_id} {actor.full_name} <{actor.email}>"
    # The next free value of each unique key, so the copy sits beside its marker row.
    bump = {RecommendationBrief: "brief_version", CreativeDraft: "draft_version",
            Approval: "decision_seq"}
    scope = {RecommendationBrief: ("workspace_id", "opportunity_id"),
             CreativeDraft: ("workspace_id", "brief_id"),
             Approval: ("subject_type", "subject_id", "subject_version")}
    tag = _tag()
    olds, markers = {}, {}
    with world.witness.begin() as conn:
        for writer, (row_id, _answered) in marker_rows.items():
            model, snapshot, _actor = COLUMN[writer]
            table = model.__table__
            row = dict(conn.execute(select(*table.c).where(table.c.id == row_id)).one()._mapping)
            markers[writer] = row
            copy = {**row, "id": f"old{writer.replace('_', '')[:9]}{tag}", snapshot: old}
            if model in bump:
                column = bump[model]
                same = [table.c[c] == row[c] for c in scope[model]]
                top = conn.execute(select(func.max(table.c[column])).where(*same)).scalar()
                copy[column] = top + 1
            conn.execute(insert(table).values(**copy))
            olds[writer] = copy["id"]
    for writer, row_id in olds.items():
        assert _stored(world, writer, row_id) == (old, actor.user_id), writer
    assert {writer: row[COLUMN[writer][1]] for writer, row in markers.items()} == {
        writer: actor.snapshot(verified=False) for writer in WRITERS
    }

    code, printed = _cli(world, monkeypatch, capsys, "erase-email", "--email", actor.email,
                         "--confirm")
    assert (code, printed["verification"]["content_findings"]) == (
        privacy_cli.EXIT_FINDINGS, RESIDUE
    ), printed

    for writer, row_id in olds.items():
        assert _stored(world, writer, row_id) == (ERASED, actor.user_id), writer
    for writer, row in markers.items():
        assert world.row(COLUMN[writer][0], row["id"]) == row, writer
    assert _phase5_values_naming(world, actor.email) == []


# --------------------------------------------------------------------------- #
# 7. The helper: the marker, the bound, and its callers
# --------------------------------------------------------------------------- #
def _account(*, verified: bool, user_id: str = "u-1", full_name: str = "Ada Lovelace",
             email: str = "ada@example.com"):
    from datetime import UTC, datetime

    return SimpleNamespace(id=user_id, full_name=full_name, email=email,
                           email_verified_at=datetime.now(UTC) if verified else None)


def test_the_marker_is_fixed_not_an_address_not_the_erased_marker_and_names_nobody():
    from app.core.actor_snapshot import (
        ERASED_ACTOR_SNAPSHOT,
        UNVERIFIED_EMAIL_MARKER,
        account_snapshot,
    )

    assert UNVERIFIED_EMAIL_MARKER == MARKER
    assert UNVERIFIED_EMAIL_MARKER != ERASED_ACTOR_SNAPSHOT
    assert "@" not in MARKER and not registrable(MARKER)
    snapshot = account_snapshot(_account(verified=False))
    assert snapshot == f"u-1 Ada Lovelace <{MARKER}>"
    assert account_snapshot(_account(verified=True)) == "u-1 Ada Lovelace <ada@example.com>"
    for address in ("ada@example.com", "unverified@example.com", "a@b.co"):
        assert mention(snapshot, Subject(addresses=(address,))) is Mention.NONE, address
    assert mention(snapshot, Subject(ids=("u-1",))) is Mention.WHOLE


def test_the_bound_holds_and_a_maximal_name_with_the_marker_fits():
    from app.core.actor_snapshot import ACTOR_SNAPSHOT_MAX_LENGTH, account_snapshot

    assert ACTOR_SNAPSHOT_MAX_LENGTH == 600
    user_id, name = "0" * 32, "N" * 200  # users.id and users.full_name at their widths
    unverified = account_snapshot(_account(verified=False, user_id=user_id, full_name=name))
    assert unverified == f"{user_id} {name} <{MARKER}>" and len(unverified) == 248
    address = f"{'e' * 64}@{'d' * 243}.co"  # 310 characters, within users.email's 320
    verified = account_snapshot(
        _account(verified=True, user_id=user_id, full_name=name, email=address)
    )
    assert verified == f"{user_id} {name} <{address}>" and len(verified) <= 600
    with pytest.raises(ValueError, match="actor snapshot exceeds its bound"):
        account_snapshot(_account(verified=False, full_name="N" * 600))
    with pytest.raises(ValueError, match="actor snapshot exceeds its bound"):
        account_snapshot(_account(verified=True, full_name="N" * 300, email="e" * 300))


#: Every production writer of a snapshot column, by file: the one helper, never the raw format.
HELPER_CALLS = {
    ("briefs/service.py", "created_by_snapshot"): 1,
    ("creative/service.py", "created_by_snapshot"): 1,
    ("approvals/service.py", "decided_by_snapshot"): 2,
    ("exports/service.py", "exported_by_snapshot"): 1,
}
_COLUMNS = {"created_by_snapshot", "decided_by_snapshot", "exported_by_snapshot"}


def _called(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        func_node = node.func
        if isinstance(func_node, ast.Name):
            return func_node.id
        if isinstance(func_node, ast.Attribute):
            return func_node.attr
    return None


def test_no_production_module_formats_a_snapshot_except_through_the_one_helper():
    """Operator §2, "across every production caller": the raw formatter is called only inside
    ``app.core.actor_snapshot``, and every value a production module gives a snapshot column is
    either the helper's (the verification fact read from the account) or the erased marker.
    A value is given as a keyword argument, a dict key, an attribute assignment (plain,
    annotated or augmented) or a ``setattr`` with a constant column name (review RA-A F-A-1,
    coordinator ruling RA-3)."""
    root = Path(app_package.__file__).parent
    raw_calls, values = [], {}
    for path in sorted(root.rglob("*.py")):
        rel = path.relative_to(root).as_posix()
        if rel.startswith("tests/") or rel == "core/actor_snapshot.py":
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if _called(node) == "actor_snapshot" or (
                isinstance(node, ast.ImportFrom) and node.module == "app.core.actor_snapshot"
                and any(alias.name == "actor_snapshot" for alias in node.names)
            ):
                raw_calls.append(f"{rel}:{node.lineno}")
            pairs = []
            if isinstance(node, ast.keyword) and node.arg in _COLUMNS:
                pairs.append((node.arg, node.value))
            if isinstance(node, ast.Dict):
                pairs += [(k.value, v) for k, v in zip(node.keys, node.values, strict=True)
                          if isinstance(k, ast.Constant) and k.value in _COLUMNS]
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    unpacked = isinstance(target, (ast.Tuple, ast.List))
                    for item in target.elts if unpacked else [target]:
                        if isinstance(item, ast.Attribute) and item.attr in _COLUMNS:
                            # An augmented or unpacked assignment is never the helper's value.
                            whole = not unpacked and not isinstance(node, ast.AugAssign)
                            pairs.append((item.attr, node.value if whole else None))
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "setattr" and len(node.args) >= 3
                and isinstance(node.args[1], ast.Constant) and node.args[1].value in _COLUMNS
            ):
                pairs.append((node.args[1].value, node.args[2]))
            for column, value in pairs:
                if isinstance(value, ast.Name) and value.id in {"ERASED", "ERASED_ACTOR_SNAPSHOT"}:
                    continue
                values.setdefault((rel, column), []).append(_called(value))
    assert raw_calls == []
    assert {key: len(calls) for key, calls in values.items()} == HELPER_CALLS
    assert {call for calls in values.values() for call in calls} == {"account_snapshot"}


def test_every_snapshot_column_is_covered_by_this_module():
    """The four columns (and the approvals note the erasure clears) are the whole set: a new
    snapshot column would need its writer added here."""
    columns = {
        (table.name, column.name)
        for table in (RecommendationBrief.__table__, CreativeDraft.__table__,
                      Approval.__table__, Export.__table__)
        for column in table.c
        if column.name.endswith("_snapshot")
    }
    assert columns == {(m.__table__.name, c) for m, c in SNAPSHOT_COLUMNS if c != "note"}
