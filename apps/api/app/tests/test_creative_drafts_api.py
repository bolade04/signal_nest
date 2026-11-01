"""Project Phase 5C (W21 lane P5C): the creative-draft routes (:mod:`app.creative.routes`).

* **Dark** (exit criterion 2): with ``creative_generation_enabled`` off and no override,
  every draft endpoint answers 503 ``capability_unavailable`` — before any id lookup, for
  every role the role gate admits — nothing is enqueued and no model is called; the
  reflection answers ``{"enabled": false}``. An override enables exactly its workspace, and a
  disable override wins over the global flag.
* **Roles** (the brief precedent; the plan names no other set): generating is editor-only
  (owner, admin, marketer); reading drafts and the reflection is open to every member.
* **Scope**: a brief or draft of another workspace — of the same organization too — is the
  same 404 as a missing id; a revision's parent must be a draft of the same brief.
* **Generate**: 422 ``brief_not_approved`` unless the brief's controlling decision on its
  exact version is ``approved``; 202 with the job to poll; an identical request while that
  job is queued joins it.
* **Shape**: the draft response carries no scope ids beyond the brief/opportunity it names,
  no ``input_hash``, ``trace_id``, usage snapshot or ledger link (CF-2's safe projection).

Cross-ORGANIZATION probes for these routes are in ``test_cross_organization_isolation``
(E9), and their role rows in ``test_e7_role_policy_matrix`` (E7).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.capabilities import resolver
from app.core.config import get_settings
from app.creative.models import CreativeDraft
from app.jobs.models import Job
from app.llm import service as service_module
from app.main import app
from app.tests._creative_support import ALL_ROLES, CEILINGS, CreativeWorld, creative_world

EDITORS = ("owner", "admin", "marketer")
NON_EDITORS = ("reviewer", "compliance_reviewer", "viewer")
DARK = (503, "capability_unavailable", "Creative generation is not available yet.")


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("p5c-api") / "api.db", "p5capi") as w:
        yield w


@pytest.fixture(autouse=True)
def ceilings(monkeypatch) -> None:
    for name, value in {**CEILINGS, "llm_max_calls_per_workspace_per_day": 1000}.items():
        monkeypatch.setattr(get_settings(), name, value)


def _error(r) -> tuple[int, str, str]:
    err = r.json()["error"]
    return r.status_code, err["code"], err["message"]


class _NoModel:
    name, model = "mock", "mock-1"

    def generate(self, *args):  # pragma: no cover - reaching it fails the test
        raise AssertionError("no model call may be made while dark")


# --------------------------------------------------------------------------- #
# Dark by default (exit criterion 2)
# --------------------------------------------------------------------------- #
def test_every_draft_endpoint_is_dark_and_nothing_is_reachable(world, monkeypatch):
    monkeypatch.setattr(service_module.llm_service, "_provider", _NoModel())
    assert get_settings().creative_generation_enabled is False
    ws = world.workspace("dark", enabled=False)
    brief_id = world.seed_brief(ws)
    jobs_before = world.count(Job)
    for role in ALL_ROLES:
        for method, path, body in (
            ("GET", f"/workspaces/{ws}/briefs/{brief_id}/drafts", None),
            ("GET", f"/workspaces/{ws}/briefs/missing-brief/drafts", None),
            ("GET", f"/workspaces/{ws}/drafts/missing-draft", None),
            ("POST", f"/workspaces/{ws}/briefs/{brief_id}/drafts", {"channel": "email"}),
            ("POST", f"/workspaces/{ws}/briefs/missing-brief/drafts", {"channel": "email"}),
        ):
            r = world.send(method, path, role, body)
            if method == "POST" and role in NON_EDITORS:
                assert _error(r)[:2] == (403, "permission_denied"), (role, path)
            else:
                assert _error(r) == DARK, (role, method, path, r.text)
        cap = world.send("GET", f"/workspaces/{ws}/creative-capability", role)
        assert (cap.status_code, cap.json()) == (200, {"enabled": False})
    assert world.count(Job) == jobs_before
    world.drain()
    assert world.count(CreativeDraft, workspace_id=ws) == 0


def test_an_override_enables_exactly_its_workspace(world):
    on, off = world.workspace("override on"), world.workspace("override off", enabled=False)
    for ws, enabled in ((on, True), (off, False)):
        cap = world.send("GET", f"/workspaces/{ws}/creative-capability", "viewer")
        assert cap.json() == {"enabled": enabled}
        brief_id = world.seed_brief(ws)
        r = world.send("GET", f"/workspaces/{ws}/briefs/{brief_id}/drafts", "viewer")
        assert r.status_code == (200 if enabled else 503)
    assert get_settings().creative_generation_enabled is False


def test_a_disable_override_wins_over_the_global_flag(world, monkeypatch):
    monkeypatch.setattr(get_settings(), "creative_generation_enabled", True)
    ws = world.workspace("disabled", enabled=False)
    world.enable(ws, enabled=False)
    assert _error(world.send("GET", f"/workspaces/{ws}/drafts/x", "owner")) == DARK
    assert world.send("GET", f"/workspaces/{ws}/creative-capability", "owner").json() == {
        "enabled": False
    }
    other = world.workspace("global on", enabled=False)  # no override: the flag decides
    assert world.send("GET", f"/workspaces/{other}/creative-capability", "owner").json() == {
        "enabled": True
    }


def test_a_resolver_failure_is_an_error_never_a_decision(world, monkeypatch):
    ws = world.workspace("resolver")

    def boom(**_kwargs):
        raise RuntimeError("override storage unavailable")

    monkeypatch.setattr("app.creative.service.resolve_capability", boom)
    assert resolver is not None
    client = TestClient(app, raise_server_exceptions=False)
    monkeypatch.setattr(world, "client", client)
    for path in (f"/workspaces/{ws}/creative-capability", f"/workspaces/{ws}/drafts/x"):
        r = world.send("GET", path, "owner")
        assert r.status_code == 500 and r.json()["error"]["code"] != "capability_unavailable"


# --------------------------------------------------------------------------- #
# Roles
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ALL_ROLES)
def test_generating_is_editor_only_and_reading_is_open_to_members(world, role):
    ws = world.workspace(f"roles {role}")
    brief_id = world.seed_brief(ws)
    r = world.generate(brief_id, role, ws=ws, channel="email")
    if role in EDITORS:
        assert r.status_code == 202, r.text
        world.drain()
    else:
        assert _error(r) == (403, "permission_denied",
                             f"Role '{role}' is not permitted for this action.")
        assert world.count(Job, workspace_id=ws) == 0
        world.generated(brief_id, "marketer", ws=ws)  # a draft to read
    listed = world.send("GET", f"/workspaces/{ws}/briefs/{brief_id}/drafts", role)
    assert listed.status_code == 200 and listed.json()["total"] == 1
    draft_id = listed.json()["items"][0]["id"]
    assert world.send("GET", f"/workspaces/{ws}/drafts/{draft_id}", role).status_code == 200


def test_a_non_member_is_refused_by_membership(world):
    ws = world.workspace("outsider")
    for method, path in (("GET", f"/workspaces/{ws}/creative-capability"),
                         ("GET", f"/workspaces/{ws}/drafts/x")):
        r = world.send(method, path, token=world.owner_b)
        assert _error(r)[:2] == (403, "permission_denied")


# --------------------------------------------------------------------------- #
# Scope: non-enumerating 404s, the sibling workspace included
# --------------------------------------------------------------------------- #
def test_a_sibling_workspaces_brief_or_draft_is_the_same_404_as_a_missing_id(world):
    ws, sibling = world.workspace("scope a"), world.workspace("scope b")
    their_brief = world.seed_brief(sibling)
    world.generated(their_brief, ws=sibling)
    their_draft = world.rows(CreativeDraft, brief_id=their_brief)[0]["id"]
    jobs_before = world.count(Job)
    for brief_id in (their_brief, "0" * 32):
        for method, body in (("GET", None), ("POST", {"channel": "email"})):
            r = world.send(method, f"/workspaces/{ws}/briefs/{brief_id}/drafts", "owner", body)
            assert _error(r) == (404, "not_found", "Brief not found in this workspace.")
    for draft_id in (their_draft, "0" * 32):
        r = world.send("GET", f"/workspaces/{ws}/drafts/{draft_id}", "owner")
        assert _error(r) == (404, "not_found", "Draft not found in this workspace.")
    assert world.count(Job) == jobs_before


def test_a_parent_must_be_a_draft_of_the_same_brief(world):
    ws = world.workspace("parent")
    first, second = world.seed_brief(ws), world.seed_brief(ws)
    world.generated(second, ws=ws)
    other_draft = world.rows(CreativeDraft, brief_id=second)[0]["id"]
    for parent in (other_draft, "0" * 32):
        r = world.generate(first, ws=ws, parent_draft_id=parent)
        assert _error(r) == (404, "not_found", "Draft not found in this workspace.")
    assert world.count(Job, workspace_id=ws) == 1  # only the generation of ``second``


# --------------------------------------------------------------------------- #
# Generate
# --------------------------------------------------------------------------- #
def test_only_an_approved_brief_version_generates(world):
    ws = world.workspace("approval")
    not_decided = world.seed_brief(ws, approved=False)
    rejected = world.seed_brief(ws)
    world.decide(rejected, "rejected")  # the latest decision controls
    revoked = world.seed_brief(ws)
    world.decide(revoked, "revoked")
    stale = world.seed_brief(ws, approved=False, version=2)
    world.decide(stale, "approved", version=1)  # an approval of another version
    for brief_id in (not_decided, rejected, revoked, stale):
        r = world.generate(brief_id, ws=ws)
        assert _error(r) == (422, "brief_not_approved",
                             "Approve this brief before generating drafts from it."), brief_id
    reapproved = world.seed_brief(ws)
    world.decide(reapproved, "rejected")
    world.decide(reapproved, "approved")
    assert world.generate(reapproved, ws=ws).status_code == 202
    assert world.count(Job, workspace_id=ws) == 1


def test_a_repeated_request_joins_the_queued_job(world):
    ws = world.workspace("idempotent")
    brief_id = world.seed_brief(ws)
    first = world.generate(brief_id, ws=ws, channel="email").json()["job_id"]
    assert world.generate(brief_id, ws=ws, channel="email").json()["job_id"] == first
    other_channel = world.generate(brief_id, ws=ws, channel="sms").json()["job_id"]
    other_author = world.generate(brief_id, "owner", ws=ws, channel="email").json()["job_id"]
    assert len({first, other_channel, other_author}) == 3
    world.drain()
    assert world.count(CreativeDraft, brief_id=brief_id) == 3
    again = world.generate(brief_id, ws=ws, channel="email").json()["job_id"]
    assert again not in (first, other_channel, other_author)  # a new version is a new job
    assert world.job(first)["idempotency_key"].startswith(f"creative:{brief_id}:v1:")


def test_a_failed_job_does_not_block_a_new_request(world, monkeypatch):
    ws = world.workspace("after failure")
    brief_id = world.seed_brief(ws)
    first = world.generate(brief_id, ws=ws).json()["job_id"]
    world.enable(ws, enabled=False)
    world.drain()
    assert world.job(first)["status"] == "failed"
    world.enable(ws)
    retry = world.generate(brief_id, ws=ws).json()["job_id"]
    assert retry != first
    world.drain()
    assert world.job(retry)["status"] == "succeeded"


def test_the_generate_route_commits_its_job_before_answering(world):
    """The 202 names a job that is already committed: with a request session whose teardown
    rolls back instead of committing, the job is still there (the scout-run route's rule;
    the teardown commit runs after the response is sent, O9 audit)."""
    from app.db.session import get_db

    ws = world.workspace("commit")
    brief_id = world.seed_brief(ws)
    committed = app.dependency_overrides[get_db]

    def rollback_teardown():
        s = world.factory()
        try:
            yield s
            s.rollback()
        finally:
            s.close()

    app.dependency_overrides[get_db] = rollback_teardown
    try:
        r = world.generate(brief_id, ws=ws)
    finally:
        app.dependency_overrides[get_db] = committed
    assert r.status_code == 202, r.text
    assert world.row(Job, r.json()["job_id"]) is not None


@pytest.mark.parametrize(
    "body",
    [{}, {"channel": "fax"}, {"channel": "email", "body": "free text"},
     {"channel": "email", "parent_draft_id": ""}, {"channel": "email", "requested_by": "x"}],
)
def test_the_request_body_is_strict(world, body):
    ws = world.workspace("strict")
    brief_id = world.seed_brief(ws)
    r = world.send("POST", f"/workspaces/{ws}/briefs/{brief_id}/drafts", "owner", body)
    assert _error(r)[:2] == (422, "validation_error")
    assert world.count(Job, workspace_id=ws) == 0


# --------------------------------------------------------------------------- #
# Response shape and paging
# --------------------------------------------------------------------------- #
def test_the_draft_response_is_the_safe_projection(world):
    ws = world.workspace("shape")
    brief_id = world.seed_brief(ws)
    world.generated(brief_id, ws=ws, channel="email")
    listed = world.send("GET", f"/workspaces/{ws}/briefs/{brief_id}/drafts", "viewer").json()
    [item] = listed["items"]
    assert set(item) == {
        "id", "brief_id", "opportunity_id", "location_id", "campaign_id", "draft_version",
        "parent_draft_id", "channel", "headline", "body", "call_to_action", "prompt_name",
        "prompt_version", "model_identifier", "is_simulated", "created_by",
        "created_by_snapshot", "created_at", "review",
    }
    assert set(item["review"]) == {
        "id", "draft_id", "draft_version", "risk_level", "is_blocked", "findings", "warnings",
        "blocked_claim_ids", "required_disclaimers", "disclaimers_resolved", "passed",
        "created_at",
    }
    one = world.send("GET", f"/workspaces/{ws}/drafts/{item['id']}", "viewer").json()
    assert one == item


def test_the_list_is_bounded_and_newest_first(world):
    ws = world.workspace("paging")
    brief_id = world.seed_brief(ws)
    for channel in ("email", "sms", "search_ad"):
        world.generated(brief_id, ws=ws, channel=channel)
    base = f"/workspaces/{ws}/briefs/{brief_id}/drafts"
    page = world.send("GET", f"{base}?limit=2&offset=1", "viewer").json()
    assert (page["total"], page["limit"], page["offset"]) == (3, 2, 1)
    assert [d["draft_version"] for d in page["items"]] == [2, 1]
    for query in ("limit=0", "limit=101", "offset=-1"):
        assert world.send("GET", f"{base}?{query}", "viewer").status_code == 422
