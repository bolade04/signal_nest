"""Project Phase 5E (lane 5E-W4): the export capability gate, precondition 1 (5E §2, exit 2(a)).

``content_export`` decides both export endpoints, through the deny-biased resolver only:

* **Dark** (the shipped state: flag ``False``, no override): ``POST`` and ``GET …/exports``
  answer 503 ``capability_unavailable`` ("Export is not available yet.") for every role the
  route admits, and write nothing (no export, no audit row, no notification). The answer
  does not depend on the draft id (own, another workspace's, unknown, deleted): the gate runs
  before any lookup, so a dark deployment discloses nothing. The reflection
  ``GET /export-capability`` answers 200 ``{"enabled": false}`` (P7). A role outside the
  route's set is refused 403 before the gate.
* **Workspace override** (O-3's pattern): an enable override opens one workspace while the
  flag is off; a disable override closes one workspace while the flag is on; neither touches
  another workspace. Other capabilities' overrides (creative generation, O-12(B)) never open
  export.
* **Safety ceiling** (precedence rule 1) outranks an enable override and the flag together.
* A resolver failure is re-raised, never turned into a decision.
* No module of ``app/exports`` reads the raw flag (the functional cases above would also fail
  if one did: a flag read opens a disabled workspace and closes an enabled one).

The world is the 5C harness on SQLite and, with ``TEST_POSTGRES_URL``, PostgreSQL at head.
"""

from __future__ import annotations

import ast
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import delete, update

import app.capabilities.resolver as resolver_mod
from app.approvals.models import Approval
from app.audit.models import AuditLog
from app.briefs.models import RecommendationBrief
from app.capabilities.models import WorkspaceCapabilityOverride
from app.capabilities.registry import Capability
from app.core.actor_snapshot import actor_snapshot
from app.core.config import get_settings
from app.creative.models import ContentReview, CreativeDraft
from app.exports.models import Export
from app.notifications.models import Notification
from app.organizations.models import User
from app.privacy.writer import mark_privacy_writer
from app.tests._creative_support import CreativeWorld, creative_world, postgres_creative_world

DARK = {"code": "capability_unavailable", "message": "Export is not available yet."}
BODY = {"format": "text/plain"}
EXPORTERS = ("owner", "admin", "marketer")
HISTORY_READERS = ("owner", "admin", "marketer", "reviewer", "compliance_reviewer")


@pytest.fixture(scope="module", params=["sqlite", "postgresql"])
def world(request, tmp_path_factory) -> Iterator[CreativeWorld]:
    if request.param == "sqlite":
        with creative_world(tmp_path_factory.mktemp("w4xg") / "g.db", "w4xg") as w:
            yield w
        return
    with postgres_creative_world("w4xg") as w:  # skips unless TEST_POSTGRES_URL is set
        yield w


@pytest.fixture(autouse=True)
def flag_off(monkeypatch) -> None:
    """The shipped global flag, whatever another module left behind."""
    monkeypatch.setattr(get_settings(), "content_export_enabled", False)


def set_cap(world: CreativeWorld, ws: str, capability: str, enabled: bool | None) -> None:
    with world.session() as s:
        s.execute(
            delete(WorkspaceCapabilityOverride).where(
                WorkspaceCapabilityOverride.workspace_id == ws,
                WorkspaceCapabilityOverride.capability == capability,
            )
        )
        if enabled is not None:
            s.add(WorkspaceCapabilityOverride(
                organization_id=world.org_of(ws), workspace_id=ws, capability=capability,
                enabled=enabled, set_by_user_id=world.owner_id(ws), reason="5E-W4 test",
            ))


@pytest.fixture
def ws(world: CreativeWorld, request) -> str:
    """A fresh workspace with NO content_export override, but with every other Phase 5
    capability that has a workspace switch turned on: none of them may open export."""
    ws = world.workspace(f"g {request.node.name[:40]}")  # creative_generation on by override
    for other in ("notifications_analytics", "recommendation_briefs", "opportunity_feedback"):
        set_cap(world, ws, other, True)
    return ws


def exportable(world: CreativeWorld, ws: str) -> str:
    brief_id = world.seed_brief(ws)
    tag = uuid.uuid4().hex[:12]
    with world.session() as s:
        brief = s.get(RecommendationBrief, brief_id)
        author = s.get(User, world.owner_id(ws) if ws == world.ws_b else world.users["marketer"])
        reviewer = s.get(User, world.owner_id(ws) if ws == world.ws_b else world.users["reviewer"])
        scoped = {"organization_id": brief.organization_id, "workspace_id": ws}
        s.add(CreativeDraft(
            id=f"dr{tag}", brief_id=brief_id, opportunity_id=brief.opportunity_id,
            draft_version=1, channel="social_post", body="Weekend parking is easy.",
            prompt_name="generate_creative", prompt_version="1.0.0", model_identifier="mock-1",
            input_hash="0" * 64, is_simulated=True, input_tokens=1, output_tokens=1,
            estimated_cost_usd=0, jurisdiction_code="US-TX", created_by=author.id,
            created_by_snapshot=actor_snapshot(
                user_id=author.id, full_name=author.full_name, email=author.email
            ),
            **scoped,
        ))
        s.flush()
        s.add(ContentReview(
            draft_id=f"dr{tag}", draft_version=1, risk_level="low", is_blocked=False,
            findings=[], warnings=[], blocked_claim_ids=[], required_disclaimers=[],
            disclaimers_resolved=True, passed=True, jurisdiction_code="US-TX", **scoped,
        ))
        s.add(Approval(
            subject_type="creative_draft", subject_id=f"dr{tag}", subject_version=1,
            draft_id=f"dr{tag}", decision="approved", decision_seq=1, decided_by=reviewer.id,
            decided_by_snapshot=actor_snapshot(
                user_id=reviewer.id, full_name=reviewer.full_name, email=reviewer.email
            ),
            decided_role="reviewer", is_simulated=False, **scoped,
        ))
    return f"dr{tag}"


def written(world: CreativeWorld) -> tuple[int, int, int]:
    return (
        world.count(Export),
        world.count(AuditLog, action="creative_draft.exported"),
        world.count(Notification),
    )


def path(ws: str, draft_id: str) -> str:
    return f"/workspaces/{ws}/drafts/{draft_id}/exports"


def error(r) -> dict:
    return {k: v for k, v in r.json()["error"].items() if k != "request_id"}


def reflection(world: CreativeWorld, ws: str, role: str = "owner") -> dict:
    r = world.send("GET", f"/workspaces/{ws}/export-capability", role)
    assert r.status_code == 200, r.text
    return r.json()


def assert_dark(world: CreativeWorld, ws: str, draft_id: str) -> None:
    before = written(world)
    for role in EXPORTERS:
        r = world.send("POST", path(ws, draft_id), role, BODY)
        assert r.status_code == 503, (role, r.text)
        assert error(r) == DARK
    for role in HISTORY_READERS:
        r = world.send("GET", path(ws, draft_id), role)
        assert r.status_code == 503, (role, r.text)
        assert error(r) == DARK
    for role in ("owner", "admin", "marketer", "reviewer", "compliance_reviewer", "viewer"):
        assert reflection(world, ws, role) == {"enabled": False}
    assert written(world) == before


def assert_open(world: CreativeWorld, ws: str, draft_id: str) -> None:
    assert reflection(world, ws) == {"enabled": True}
    made = world.send("POST", path(ws, draft_id), "marketer", BODY)
    assert made.status_code == 201, made.text
    listed = world.send("GET", path(ws, draft_id), "reviewer")
    assert listed.status_code == 200 and made.json()["id"] in {
        i["id"] for i in listed.json()["items"]
    }


# --------------------------------------------------------------------------- #
# Dark
# --------------------------------------------------------------------------- #
def test_dark_every_export_endpoint_answers_503_and_writes_nothing(world, ws):
    assert_dark(world, ws, exportable(world, ws))


def test_dark_the_answer_does_not_depend_on_the_draft_id(world, ws):
    """Gate before lookup: own, another workspace's, another organization's, unknown and
    soft-deleted ids all get the same 503 body, never a 404 that would tell them apart."""
    set_cap(world, world.ws2, "content_export", True)
    set_cap(world, world.ws_b, "content_export", True)
    deleted = exportable(world, ws)
    with world.session() as s:
        mark_privacy_writer(s)
        s.execute(
            update(CreativeDraft.__table__)
            .where(CreativeDraft.__table__.c.id == deleted)
            .values(deleted_at=datetime.now(UTC))
        )
    for draft_id in (exportable(world, ws), exportable(world, world.ws2),
                     exportable(world, world.ws_b), uuid.uuid4().hex, deleted):  # fmt: skip
        assert_dark(world, ws, draft_id)


def test_dark_a_role_outside_the_set_is_refused_403_before_the_gate(world, ws):
    draft = exportable(world, ws)
    before = written(world)
    for role in ("reviewer", "compliance_reviewer", "viewer"):
        r = world.send("POST", path(ws, draft), role, BODY)
        assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    r = world.send("GET", path(ws, draft), "viewer")
    assert r.status_code == 403 and r.json()["error"]["code"] == "permission_denied"
    assert written(world) == before


@pytest.mark.parametrize("open_", [False, True])
def test_a_malformed_body_is_refused_without_a_write_dark_or_open(world, ws, open_):
    """An unknown format, an extra field (no body names a user or anything else) and an empty
    body are refused whatever the capability; while open it is exactly the 422."""
    draft = exportable(world, ws)
    set_cap(world, ws, "content_export", True if open_ else None)
    before = written(world)
    for body in ({"format": "application/pdf"}, {"format": "text/plain", "user_id": "x"},
                 {"format": "text/plain", "exported_by": "x"}, {}):  # fmt: skip
        r = world.send("POST", path(ws, draft), "owner", body)
        assert r.status_code in ((422,) if open_ else (422, 503)), r.text
    assert written(world) == before


# --------------------------------------------------------------------------- #
# Override and flag, through the resolver
# --------------------------------------------------------------------------- #
def test_an_enable_override_opens_one_workspace_while_the_flag_is_off(world, ws):
    draft = exportable(world, ws)
    other = world.workspace("g neighbour")
    other_draft = exportable(world, other)
    set_cap(world, ws, "content_export", True)
    assert_open(world, ws, draft)
    assert_dark(world, other, other_draft)


def test_the_global_flag_opens_every_workspace_without_an_override(world, ws, monkeypatch):
    monkeypatch.setattr(get_settings(), "content_export_enabled", True)
    assert_open(world, ws, exportable(world, ws))


def test_a_disable_override_closes_one_workspace_while_the_flag_is_on(world, ws, monkeypatch):
    monkeypatch.setattr(get_settings(), "content_export_enabled", True)
    draft = exportable(world, ws)
    other = world.workspace("g neighbour on")
    other_draft = exportable(world, other)
    set_cap(world, ws, "content_export", False)
    assert_dark(world, ws, draft)
    assert_open(world, other, other_draft)
    set_cap(world, ws, "content_export", None)  # removing it restores the flag's answer
    assert_open(world, ws, draft)


def test_overrides_of_other_capabilities_never_open_export(world, ws):
    """The fixture switched creative generation, briefs, feedback and O-12(B) on for ``ws``."""
    assert {r["capability"] for r in world.rows(WorkspaceCapabilityOverride, workspace_id=ws)
            if r["enabled"]} >= {"creative_generation", "notifications_analytics"}  # fmt: skip
    assert_dark(world, ws, exportable(world, ws))


# --------------------------------------------------------------------------- #
# The safety ceiling, and a resolver failure
# --------------------------------------------------------------------------- #
def _ceiling_blocks(monkeypatch, capability: Capability) -> None:
    """Make precedence rule 1 prohibit ``capability`` (test_capability_enforcement's pattern:
    the shipped ceiling blocks only unregistered capabilities and engaged environment
    ceilings, so the rule-1 predicate is patched to prove export consumes it)."""
    real = resolver_mod._ceiling_blocks
    monkeypatch.setattr(
        resolver_mod, "_ceiling_blocks", lambda c, **kw: c is capability or real(c, **kw)
    )


def test_the_safety_ceiling_outranks_an_enable_override_and_the_flag(world, ws, monkeypatch):
    draft = exportable(world, ws)
    set_cap(world, ws, "content_export", True)
    monkeypatch.setattr(get_settings(), "content_export_enabled", True)
    assert_open(world, ws, draft)
    _ceiling_blocks(monkeypatch, Capability.CONTENT_EXPORT)
    assert_dark(world, ws, draft)


def test_a_ceiling_on_another_capability_leaves_export_open(world, ws, monkeypatch):
    set_cap(world, ws, "content_export", True)
    _ceiling_blocks(monkeypatch, Capability.NOTIFICATIONS_ANALYTICS)
    assert_open(world, ws, exportable(world, ws))


def test_a_resolver_failure_is_raised_never_a_decision(world, ws, monkeypatch):
    draft = exportable(world, ws)
    set_cap(world, ws, "content_export", True)
    import app.exports.service as service

    def broken(**_kw):
        raise RuntimeError("resolver unavailable")

    monkeypatch.setattr(service, "resolve_capability", broken)
    before = written(world)
    for method, body in (("POST", BODY), ("GET", None)):
        with pytest.raises(RuntimeError, match="resolver unavailable"):
            world.send(method, path(ws, draft), "owner", body)
    assert written(world) == before


# --------------------------------------------------------------------------- #
# No raw flag read
# --------------------------------------------------------------------------- #
def test_no_export_module_reads_the_raw_flag():
    """Only the resolver may read ``content_export_enabled`` (interface sheet A; the
    functional cases above prove the same from outside)."""
    root = Path(__file__).resolve().parents[1] / "exports"
    readers = []
    for source in sorted(root.glob("*.py")):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            named = (isinstance(node, ast.Attribute) and node.attr == "content_export_enabled") or (
                isinstance(node, ast.Constant) and node.value == "content_export_enabled"
            )
            if named:
                readers.append(f"{source.name}:{node.lineno}")
    assert readers == []
    assert (root / "service.py").exists() and (root / "routes.py").exists()
