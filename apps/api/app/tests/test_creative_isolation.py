"""Project Phase 5C-W4 adversarial battery, item 2: isolation (Phase 5C plan §4 exit
criterion 9).

* **Every draft and decision route, every foreign id.** For each of the six 5C draft and
  decision operations, an id from a sibling workspace of the same organization, from another
  organization, a missing id, and an id of the wrong kind (a brief id as a draft id, a draft id
  as a brief id) — through ``brief_id``, ``draft_id`` and ``parent_draft_id`` — answers exactly
  what a missing id answers, for every role the route admits, and writes nothing.
* **The ``content_reviews`` direct-query cross-workspace test** the plan names (exit criterion
  9): the table is queried on its own scope columns, never joined through ``creative_drafts``,
  on SQLite and on PostgreSQL. Each workspace's query returns exactly its own drafts' reviews,
  the organization filter never reaches another organization, and every row's scope equals its
  draft's. The reads of the service use the review's own scope too: a row whose scope is not
  its draft's is invisible to that draft, and the draft then fails closed (no review, not
  approvable).
* **Decision histories** never cross drafts, versions, briefs or workspaces, including a
  decision row scoped to another workspace.
* **The job's payload** (its only input) naming another organization's brief, a sibling's or
  another organization's parent, or another organization's requester is refused by the job
  itself before any model call.
* **The reflection** (``GET /creative-capability``) reports only its own workspace, carries
  nothing but ``enabled`` and ``generation_configured`` (I-5C; every ceiling is set here, so
  the second follows the first), and agrees with every gated route for every role.
"""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, update

from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.core.config import get_settings
from app.creative.models import ContentReview, CreativeDraft
from app.creative.service import draft_approval_refusal
from app.jobs.context import ExecutionContext
from app.jobs.models import Job
from app.jobs.service import enqueue_job
from app.jobs.status import JobType
from app.llm import service as service_module
from app.tests._creative_support import ALL_ROLES, CEILINGS, CreativeWorld, creative_world

EDITORS = ("owner", "admin", "marketer")
HISTORY_READERS = ("owner", "admin", "marketer", "reviewer", "compliance_reviewer")
MISSING = "0" * 32


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("w4-iso") / "iso.db", "w4iso") as w:
        yield w


@pytest.fixture(autouse=True)
def ceilings(monkeypatch) -> None:
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 10_000}.items():
        monkeypatch.setattr(get_settings(), name, value)


def _body(r) -> tuple[int, dict]:
    """Status and error body without the per-request correlation id."""
    payload = r.json()
    error = dict(payload.get("error") or {})
    error.pop("request_id", None)
    return r.status_code, {**payload, "error": error} if "error" in payload else payload


def _draft_of(world: CreativeWorld, ws: str, brief_id: str | None = None) -> tuple[str, str]:
    """A brief (seeded) and one generated draft of it; org B's through its own owner."""
    brief_id = brief_id or world.seed_brief(ws)
    token = world.owner_b if ws == world.ws_b else None
    r = world.send("POST", f"/workspaces/{ws}/briefs/{brief_id}/drafts", "marketer",
                   {"channel": "social_post"}, token=token)
    assert r.status_code == 202, r.text
    world.drain()
    job = world.job(r.json()["job_id"])
    assert job["status"] == "succeeded", (job["last_error_code"], job["last_error_summary"])
    return brief_id, job["result_summary"]["draft_id"]


def _counts(world: CreativeWorld) -> tuple[int, ...]:
    return tuple(world.count(m) for m in (Job, CreativeDraft, ContentReview, Approval, AuditLog))


# --------------------------------------------------------------------------- #
# IDOR: every route, every foreign id, every admitted role
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module")
def scene(world: CreativeWorld) -> SimpleNamespace:
    with pytest.MonkeyPatch.context() as mp:
        for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 10_000}.items():
            mp.setattr(get_settings(), name, value)
        own = world.workspace("idor own")
        sibling = world.workspace("idor sibling")
        world.enable(world.ws_b)
        own_brief, own_draft = _draft_of(world, own)
        sib_brief, sib_draft = _draft_of(world, sibling)
        b_brief = world.seed_brief(world.ws_b)
        _, b_draft = _draft_of(world, world.ws_b, b_brief)
    return SimpleNamespace(own=own, own_brief=own_brief, own_draft=own_draft,
                           sib_brief=sib_brief, sib_draft=sib_draft,
                           b_brief=b_brief, b_draft=b_draft)


#: (name, method, path template, body template, roles admitted, which id is foreign)
ROUTES = [
    ("list_drafts", "GET", "/workspaces/{ws}/briefs/{brief}/drafts", None, ALL_ROLES, "brief"),
    ("generate", "POST", "/workspaces/{ws}/briefs/{brief}/drafts", {"channel": "email"},
     EDITORS, "brief"),
    ("generate_parent", "POST", "/workspaces/{ws}/briefs/{brief}/drafts",
     {"channel": "email", "parent_draft_id": "{draft}"}, EDITORS, "draft"),
    ("get_draft", "GET", "/workspaces/{ws}/drafts/{draft}", None, ALL_ROLES, "draft"),
    ("decide", "POST", "/workspaces/{ws}/drafts/{draft}/approvals",
     {"decision": "approved", "subject_version": 1}, ("reviewer",), "draft"),
    ("history", "GET", "/workspaces/{ws}/drafts/{draft}/approvals", None, HISTORY_READERS,
     "draft"),
]


def _foreign_ids(scene, kind: str) -> dict[str, str]:
    if kind == "brief":
        return {"sibling_workspace": scene.sib_brief, "other_organization": scene.b_brief,
                "wrong_kind": scene.own_draft, "missing": MISSING}
    return {"sibling_workspace": scene.sib_draft, "other_organization": scene.b_draft,
            "wrong_kind": scene.own_brief, "missing": MISSING}


def _call(world, scene, route, role: str, foreign: str):
    _, method, path, body, _, kind = route
    ids = {"ws": scene.own, "brief": scene.own_brief, "draft": scene.own_draft}
    ids[kind] = foreign
    if body is not None:
        body = {k: (v.format(**ids) if isinstance(v, str) else v) for k, v in body.items()}
    return world.send(method, path.format(**ids), role, body)


@pytest.mark.parametrize("route", ROUTES, ids=[r[0] for r in ROUTES])
def test_every_foreign_id_answers_exactly_like_a_missing_one(world, scene, route):
    name, _, _, _, roles, kind = route
    before = _counts(world)
    for role in roles:
        missing = _body(_call(world, scene, route, role, MISSING))
        assert missing[0] == 404, (name, role, missing)
        for source, foreign in _foreign_ids(scene, kind).items():
            got = _body(_call(world, scene, route, role, foreign))
            assert got == missing, (name, role, source, got)
    assert _counts(world) == before  # nothing enqueued, generated, decided or audited


def test_the_routes_answer_their_own_ids(world, scene):
    """The positive control of the matrix above: the same calls with the caller's own ids."""
    own = {"ws": scene.own, "brief": scene.own_brief, "draft": scene.own_draft}
    assert world.send("GET", "/workspaces/{ws}/briefs/{brief}/drafts".format(**own),
                      "viewer").json()["items"][0]["id"] == scene.own_draft
    assert world.send("GET", "/workspaces/{ws}/drafts/{draft}".format(**own),
                      "viewer").status_code == 200
    assert world.send("GET", "/workspaces/{ws}/drafts/{draft}/approvals".format(**own),
                      "reviewer").status_code == 200
    revise = world.send("POST", "/workspaces/{ws}/briefs/{brief}/drafts".format(**own),
                        "owner", {"channel": "sms", "parent_draft_id": scene.own_draft})
    assert revise.status_code == 202, revise.text
    world.drain()


@pytest.mark.parametrize("route", ROUTES, ids=[r[0] for r in ROUTES])
def test_another_organizations_workspace_path_is_the_membership_403(world, scene, route):
    """Org A's members on org B's workspace path, with org B's own ids: the membership 403
    before any lookup, never the 404 that would confirm or deny the id."""
    _, method, path, body, roles, _ = route
    ids = {"ws": world.ws_b, "brief": scene.b_brief, "draft": scene.b_draft}
    if body is not None:
        body = {k: (v.format(**ids) if isinstance(v, str) else v) for k, v in body.items()}
    for role in ALL_ROLES:
        r = world.send(method, path.format(**ids), role, body)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "permission_denied"), role


# --------------------------------------------------------------------------- #
# content_reviews, queried directly on its own scope columns (exit criterion 9)
# --------------------------------------------------------------------------- #
def _direct_review_scope_check(world: CreativeWorld, workspaces: dict[str, str]) -> None:
    """Generate a passed and a blocked draft in every workspace, then query ``content_reviews``
    alone — no join — and compare with what each workspace's own API answers."""
    expected: dict[str, set[str]] = {}
    for ws, org in workspaces.items():
        world.claim("weekend parking", ws=ws)  # a blocked claim the mock's draft carries
        _draft_of(world, ws)
        with world.session() as s:  # retire it so the second draft passes
            from app.campaign_context.models import ClaimsLibraryEntry

            s.execute(update(ClaimsLibraryEntry).where(ClaimsLibraryEntry.workspace_id == ws)
                      .values(retired_at=func.now()))
        _draft_of(world, ws)
        role = "owner" if org == world.org else None
        token = None if role else world.owner_b
        ids = set()
        for brief in world.rows(RecommendationBrief, workspace_id=ws):
            listed = world.send("GET", f"/workspaces/{ws}/briefs/{brief['id']}/drafts",
                                role or "owner", token=token).json()["items"]
            ids |= {d["review"]["id"] for d in listed}
        assert len(ids) >= 2  # this check's two drafts, and any the workspace held before
        expected[ws] = ids

    table = ContentReview.__table__
    with world.witness.connect() as conn:
        def ids_where(*conds) -> set[str]:
            return set(conn.execute(select(table.c.id).where(*conds)).scalars())

        for ws, org in workspaces.items():
            got = ids_where(table.c.workspace_id == ws)
            assert got == expected[ws], ws
            others = set().union(*(v for k, v in expected.items() if k != ws))
            assert not got & others
            assert ids_where(table.c.organization_id == org, table.c.workspace_id == ws) == got
        orgs = set(workspaces.values())
        for org in orgs:
            mine = set().union(*(expected[w] for w, o in workspaces.items() if o == org))
            theirs = set().union(*(expected[w] for w, o in workspaces.items() if o != org))
            by_org = ids_where(table.c.organization_id == org)
            assert mine <= by_org and not by_org & theirs
            for ws, ws_org in workspaces.items():
                if ws_org != org:
                    assert ids_where(table.c.organization_id == org,
                                     table.c.workspace_id == ws) == set()
        # Every review's organization is its workspace's (read separately, not joined).
        from app.organizations.models import Workspace

        owner_of = dict(conn.execute(select(Workspace.__table__.c.id,
                                            Workspace.__table__.c.organization_id)).all())
        for org_id, ws_id in conn.execute(select(table.c.organization_id, table.c.workspace_id)):
            assert owner_of[ws_id] == org_id
        # Every review's own scope (and version) is its draft's — read separately, not joined.
        reviews = conn.execute(select(table.c.draft_id, table.c.organization_id,
                                      table.c.workspace_id, table.c.draft_version)).all()
        drafts = {
            r.id: (r.organization_id, r.workspace_id, r.draft_version)
            for r in conn.execute(select(CreativeDraft.__table__.c.id,
                                         CreativeDraft.__table__.c.organization_id,
                                         CreativeDraft.__table__.c.workspace_id,
                                         CreativeDraft.__table__.c.draft_version))
        }
        assert reviews
        for r in reviews:
            assert (r.organization_id, r.workspace_id, r.draft_version) == drafts[r.draft_id]


def test_content_reviews_hold_their_own_scope_queried_directly(world):
    a1, a2 = world.workspace("direct a1"), world.workspace("direct a2")
    b = world.ws_b
    world.enable(b)
    _direct_review_scope_check(world, {a1: world.org, a2: world.org, b: world.org_b})


def test_postgres_content_reviews_hold_their_own_scope_queried_directly(monkeypatch):
    from app.tests._creative_support import postgres_creative_world

    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 10_000}.items():
        monkeypatch.setattr(get_settings(), name, value)
    with postgres_creative_world("w4direct") as world:
        for ws in (world.ws, world.ws2, world.ws_b):
            world.enable(ws)
        _direct_review_scope_check(
            world, {world.ws: world.org, world.ws2: world.org, world.ws_b: world.org_b}
        )


@pytest.mark.parametrize("scope", ["sibling_workspace", "other_organization"])
def test_a_review_row_scoped_elsewhere_is_invisible_to_its_draft_and_fails_closed(world, scope):
    """The service reads a review by its OWN scope columns and the draft id (never the draft id
    alone): a row whose scope is not its draft's (only possible by a direct write) is not that
    draft's review — the draft shows none and cannot be approved (fail closed)."""
    ws = world.workspace(f"misscoped {scope}")
    _, draft_id = _draft_of(world, ws)
    [review] = world.rows(ContentReview, draft_id=draft_id)
    assert review["passed"] is True
    elsewhere = (
        {"workspace_id": world.workspace("misscoped target")}
        if scope == "sibling_workspace"
        else {"workspace_id": world.ws_b, "organization_id": world.org_b}
    )
    table = ContentReview.__table__
    with world.witness.begin() as conn:
        conn.execute(update(table).where(table.c.id == review["id"]).values(**elsewhere))
    try:
        got = world.send("GET", f"/workspaces/{ws}/drafts/{draft_id}", "viewer").json()
        assert got["review"] is None
        brief = world.row(CreativeDraft, draft_id)["brief_id"]
        listed = world.send("GET", f"/workspaces/{ws}/briefs/{brief}/drafts", "viewer").json()
        assert [d["review"] for d in listed["items"]] == [None]
        with world.session() as s:
            assert draft_approval_refusal(s, s.get(CreativeDraft, draft_id)) == "review_missing"
        r = world.send("POST", f"/workspaces/{ws}/drafts/{draft_id}/approvals", "reviewer",
                       {"decision": "approved", "subject_version": 1})
        assert (r.status_code, r.json()["error"]["code"]) == (409, "approval_review_blocked")
        assert world.count(Approval, draft_id=draft_id) == 0
    finally:
        with world.witness.begin() as conn:
            conn.execute(update(table).where(table.c.id == review["id"]).values(
                workspace_id=review["workspace_id"], organization_id=review["organization_id"]
            ))


# --------------------------------------------------------------------------- #
# Decision histories never cross
# --------------------------------------------------------------------------- #
def _decide(world, ws, draft_id, decision="approved", version=1):
    return world.send("POST", f"/workspaces/{ws}/drafts/{draft_id}/approvals", "reviewer",
                      {"decision": decision, "subject_version": version})


def _history(world, ws, draft_id, role="reviewer") -> list[tuple]:
    r = world.send("GET", f"/workspaces/{ws}/drafts/{draft_id}/approvals", role)
    assert r.status_code == 200, r.text
    return [(i["subject_type"], i["subject_id"], i["subject_version"], i["workspace_id"],
             i["decision"], i["decision_seq"]) for i in r.json()["items"]]


def test_draft_decision_histories_never_cross_drafts_versions_briefs_or_workspaces(world):
    a1, a2 = world.workspace("hist a1"), world.workspace("hist a2")
    brief, v1 = _draft_of(world, a1)
    job = world.generated(brief, ws=a1, channel="sms", parent_draft_id=v1)
    v2 = job["result_summary"]["draft_id"]
    _, other = _draft_of(world, a2)
    assert _decide(world, a1, v1).status_code == 201
    assert _decide(world, a1, v2, "rejected", version=2).status_code == 201
    assert _decide(world, a2, other).status_code == 201
    assert _decide(world, a2, other, "revoked").status_code == 201

    assert _history(world, a1, v1) == [("creative_draft", v1, 1, a1, "approved", 1)]
    assert _history(world, a1, v2) == [("creative_draft", v2, 2, a1, "rejected", 1)]
    assert _history(world, a2, other) == [
        ("creative_draft", other, 1, a2, "revoked", 2),
        ("creative_draft", other, 1, a2, "approved", 1),
    ]
    # The brief's own history (the 5B service) holds no draft decision, only its approval.
    from app.approvals.service import list_decisions

    with world.session() as s:
        page = list_decisions(s, brief=s.get(RecommendationBrief, brief))
        assert [(a.subject_type, a.decision) for a in page.items] == [
            ("recommendation_brief", "approved")
        ]
    # Each workspace's audit log holds only its own drafts' decisions.
    for ws, mine, theirs in ((a1, {v1, v2}, {other}), (a2, {other}, {v1, v2})):
        rows = world.send("GET", f"/workspaces/{ws}/audit-logs?entity_type=creative_draft",
                          "owner").json()
        decided = {r["entity_id"] for r in rows if r["action"].startswith("approval.")}
        assert mine <= decided and not decided & theirs, ws

    # A decision row on v1 scoped to the other workspace (a direct write) is not v1's history.
    reviewer = world.users["reviewer"]
    with world.witness.begin() as conn:
        conn.execute(insert(Approval.__table__).values(
            id="misscoped" + v1[:23], organization_id=world.org, workspace_id=a2,
            subject_type="creative_draft", subject_id=v1, subject_version=1, draft_id=v1,
            decision="approved", decision_seq=9, decided_by=reviewer,
            decided_by_snapshot=f"{reviewer} reviewer", decided_role="reviewer",
            is_simulated=True,
        ))
    assert _history(world, a1, v1) == [("creative_draft", v1, 1, a1, "approved", 1)]
    assert world.send("GET", f"/workspaces/{a2}/drafts/{v1}/approvals",
                      "reviewer").status_code == 404


# --------------------------------------------------------------------------- #
# The job's payload names only its own tenant's rows
# --------------------------------------------------------------------------- #
class _Spy:
    def __init__(self, real):
        self.real, self.calls = real, 0
        self.name, self.model = real.name, real.model

    def generate(self, request, prompt_name, prompt_version):
        self.calls += 1
        return self.real.generate(request, prompt_name, prompt_version)


def test_a_payload_naming_another_tenants_rows_is_refused_by_the_job(world, monkeypatch):
    spy = _Spy(service_module.llm_service._provider)
    monkeypatch.setattr(service_module.llm_service, "_provider", spy)
    ws, sibling = world.workspace("payload own"), world.workspace("payload sibling")
    world.enable(world.ws_b)
    own_brief = world.seed_brief(ws)
    _, sib_draft = _draft_of(world, sibling)
    b_brief, b_draft = _draft_of(world, world.ws_b)
    marketer = world.users["marketer"]
    cases = {
        "brief_not_found": {"brief_id": b_brief, "requested_by": marketer},
        "parent_not_found": {"brief_id": own_brief, "parent_draft_id": sib_draft,
                             "requested_by": marketer},
        "parent_not_found ": {"brief_id": own_brief, "parent_draft_id": b_draft,
                              "requested_by": marketer},
        "requester_not_permitted": {"brief_id": own_brief, "requested_by": world.owner_b_id},
    }
    jobs = {}
    with world.session() as s:
        for summary, payload in cases.items():
            jobs[summary] = enqueue_job(
                s, job_type=JobType.CREATIVE_GENERATE,
                context=ExecutionContext(organization_id=world.org, workspace_id=ws),
                payload={**payload, "channel": "email"},
            ).id
    spy.calls = 0
    drafts_before = world.count(CreativeDraft)
    world.drain()
    for summary, job_id in jobs.items():
        job = world.job(job_id)
        assert (job["status"], job["last_error_summary"]) == ("failed", summary.strip()), summary
    assert spy.calls == 0
    assert world.count(CreativeDraft) == drafts_before


# --------------------------------------------------------------------------- #
# The reflection
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("global_flag", [False, True])
def test_the_reflection_reports_its_own_workspace_and_agrees_with_every_gate(
    world, monkeypatch, global_flag
):
    monkeypatch.setattr(get_settings(), "creative_generation_enabled", global_flag)
    plain = world.workspace("reflect plain", enabled=False)
    on = world.workspace("reflect on")
    off = world.workspace("reflect off", enabled=False)
    world.enable(off, enabled=False)
    expected = {plain: global_flag, on: True, off: False}
    # I-5C: the body is exactly two booleans. Every ceiling is set (the autouse fixture), so
    # ``generation_configured`` is ``enabled`` itself: true only where generation is enabled,
    # false for a dark workspace whatever the deployment's ceilings.
    for ws, enabled in expected.items():
        brief = world.seed_brief(ws)
        for role in ALL_ROLES:
            r = world.send("GET", f"/workspaces/{ws}/creative-capability", role)
            assert (r.status_code, r.json()) == (
                200, {"enabled": enabled, "generation_configured": enabled}
            ), (ws, role)
            gated = [
                world.send("GET", f"/workspaces/{ws}/briefs/{brief}/drafts", role),
                world.send("GET", f"/workspaces/{ws}/drafts/{MISSING}", role),
            ]
            if role in HISTORY_READERS:
                gated.append(world.send("GET", f"/workspaces/{ws}/drafts/{MISSING}/approvals",
                                        role))
            for g in gated:
                assert (g.status_code != 503) is enabled, (ws, role, g.status_code)
    # Another organization's workspace: the membership 403, never its state.
    world.enable(world.ws_b)
    for role in ALL_ROLES:
        r = world.send("GET", f"/workspaces/{world.ws_b}/creative-capability", role)
        assert (r.status_code, r.json()["error"]["code"]) == (403, "permission_denied")
    own_b = world.send("GET", f"/workspaces/{world.ws_b}/creative-capability",
                       token=world.owner_b)
    assert own_b.json() == {"enabled": True, "generation_configured": True}
    # Turning one workspace off never changes a sibling's reflection.
    world.enable(on, enabled=False)
    assert world.send("GET", f"/workspaces/{plain}/creative-capability",
                      "viewer").json() == {"enabled": global_flag,
                                           "generation_configured": global_flag}
