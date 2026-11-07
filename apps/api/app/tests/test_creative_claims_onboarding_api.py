"""Project Phase 5C (W21 lane P5C): onboarding's claims input is a claims-library write.

Coordinator ruling R1, extended after review W21-REV-CF2-CONTROLS r1 (C-4): at CF-2,
``POST /workspaces/{workspace_id}/onboarding`` let any editor — marketers included — add
claims of any kind (approved included), at any time (no "already onboarded" guard), with no
audit row and no validation (an empty text was accepted). The ``generate_creative`` prompt
trusts the library's approved claims, so all four are closed (:mod:`app.brands.service`):

* **stewards only** — a marketer's onboarding that carries claims is refused 403
  ``permission_denied`` (the ``/claims`` answer) and writes nothing; without claims it
  onboards exactly as before;
* **audited like ``/claims``** — one ``claims_library_entry.created`` row per entry, with the
  same entity, actor and field-name context a ``/claims`` create writes;
* **validated like ``/claims``** — each claim must be a valid ``ClaimIn`` (empty text 422);
* **first onboarding only** — once the workspace is onboarded, claims are added through
  ``/claims``: a repeated onboarding with claims is 409 ``onboarding_claims_closed``.

And the point of it: a marketer's onboarding cannot put an approved claim in front of the
generation model.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from sqlalchemy import select

from app.audit.models import AuditLog
from app.brands.models import Brand
from app.brands.service import CLAIMS_INPUT_CLOSED_CODE
from app.briefs.models import RecommendationBrief
from app.campaign_context.models import ClaimsLibraryEntry
from app.claims.library import CLAIM_CREATE_FIELDS
from app.core.config import get_settings
from app.creative.service import generation_context
from app.organizations.models import Workspace
from app.tests._creative_support import CEILINGS, CreativeWorld, creative_world

MARKETER_CLAIM = "Marketer-approved: doubles your sales overnight"


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[CreativeWorld]:
    with creative_world(tmp_path_factory.mktemp("p5c-r1") / "r1.db", "p5cr1") as w:
        yield w


def _fresh_workspace(world: CreativeWorld, name: str) -> str:
    """A workspace that is not onboarded yet (created by the owner)."""
    r = world.send("POST", f"/organizations/{world.org}/workspaces", "owner", {"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _onboard(world: CreativeWorld, ws: str, role: str, claims: list | None = None):
    body: dict = {"brand_name": f"Brand {ws[:6]}", "profile": {"company_name": "Co"}}
    if claims is not None:
        body["claims"] = claims
    return world.send("POST", f"/workspaces/{ws}/onboarding", role, body)


def _error(r) -> tuple[int, str, str]:
    err = r.json()["error"]
    return r.status_code, err["code"], err["message"]


def _nothing_written(world: CreativeWorld, ws: str) -> None:
    assert world.count(ClaimsLibraryEntry, workspace_id=ws) == 0
    assert world.count(Brand, workspace_id=ws) == 0
    assert world.row(Workspace, ws)["onboarding_completed"] is False
    assert world.count(AuditLog, workspace_id=ws, action="claims_library_entry.created") == 0


# --------------------------------------------------------------------------- #
# Stewards only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("kind", ["approved", "blocked", "required_disclaimer", "restricted"])
def test_a_marketer_cannot_add_claims_through_onboarding(world, kind):
    ws = _fresh_workspace(world, f"R1 marketer {kind}")
    r = _onboard(world, ws, "marketer", [{"text": "Any claim at all", "kind": kind}])
    status, code, message = _error(r)
    assert (status, code) == (403, "permission_denied")
    assert "Only an owner, admin or compliance reviewer" in message
    assert "Any claim at all" not in r.text
    _nothing_written(world, ws)


def test_a_marketer_still_onboards_without_claims(world):
    ws = _fresh_workspace(world, "R1 marketer no claims")
    for body_claims in (None, []):
        r = _onboard(world, ws, "marketer", body_claims)
        assert r.status_code == 201, r.text
    assert world.row(Workspace, ws)["onboarding_completed"] is True
    assert world.count(ClaimsLibraryEntry, workspace_id=ws) == 0


@pytest.mark.parametrize("role", ["owner", "admin"])
def test_a_stewards_first_onboarding_creates_the_claims_and_audits_each(world, role):
    ws = _fresh_workspace(world, f"R1 steward {role}")
    claims = [
        {"text": "Locally roasted", "kind": "approved"},
        {"text": "Cures everything", "kind": "blocked", "category": "health",
         "risk_level": "high"},
    ]
    r = _onboard(world, ws, role, claims)
    assert r.status_code == 201, r.text

    rows = sorted(world.rows(ClaimsLibraryEntry, workspace_id=ws), key=lambda r: r["text"])
    assert [(r["text"], r["kind"], r["category"], r["risk_level"], r["notes"]) for r in rows] == [
        ("Cures everything", "blocked", "health", "high", None),
        ("Locally roasted", "approved", None, "low", None),  # the /claims defaults
    ]
    audits = world.rows(AuditLog, workspace_id=ws, action="claims_library_entry.created")
    assert sorted(a["entity_id"] for a in audits) == sorted(r["id"] for r in rows)
    for audit in audits:
        assert audit["actor_user_id"] == world.users[role]
        assert audit["entity_type"] == "claims_library_entry"
        assert audit["context"] == {"fields": list(CLAIM_CREATE_FIELDS)}
        assert "Locally roasted" not in str(audit) and "Cures everything" not in str(audit)


def test_the_onboarding_audit_has_the_claims_route_shape(world):
    """The same body through ``POST /claims`` and through onboarding: identical audit rows
    apart from the ids and the time."""
    ws = _fresh_workspace(world, "R1 audit shape")
    assert _onboard(world, ws, "owner", [{"text": "Shape A"}]).status_code == 201
    r = world.send("POST", f"/workspaces/{ws}/claims", "owner", {"text": "Shape B"})
    assert r.status_code == 201, r.text
    audits = world.rows(AuditLog, workspace_id=ws, action="claims_library_entry.created")
    assert len(audits) == 2
    volatile = {"id", "entity_id", "created_at", "updated_at"}
    shapes = [{k: v for k, v in a.items() if k not in volatile} for a in audits]
    assert shapes[0] == shapes[1]


# --------------------------------------------------------------------------- #
# Validated like /claims
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "claim, field",
    [({"text": ""}, "claims[0].text"), ({"kind": "approved"}, "claims[0].text"),
     ({"text": 5}, "claims[0].text")],
)
def test_an_invalid_claim_is_refused_like_the_claims_route_and_writes_nothing(world, claim, field):
    ws = _fresh_workspace(world, f"R1 invalid {field} {claim}")
    r = _onboard(world, ws, "owner", [claim])
    status, code, message = _error(r)
    assert (status, code) == (422, "validation_error")
    assert field in message
    _nothing_written(world, ws)
    # /claims refuses the same body.
    assert world.send("POST", f"/workspaces/{world.ws}/claims", "owner", claim).status_code == 422


def test_one_invalid_claim_refuses_the_whole_onboarding(world):
    ws = _fresh_workspace(world, "R1 one invalid")
    r = _onboard(world, ws, "owner", [{"text": "Fine"}, {"text": ""}])
    assert _error(r)[:2] == (422, "validation_error")
    assert "claims[1].text" in r.json()["error"]["message"]
    _nothing_written(world, ws)


# --------------------------------------------------------------------------- #
# First onboarding only
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", ["owner", "admin"])
def test_claims_cannot_be_added_by_onboarding_again(world, role):
    ws = _fresh_workspace(world, f"R1 again {role}")
    assert _onboard(world, ws, "owner").status_code == 201
    brands = world.count(Brand, workspace_id=ws)
    r = _onboard(world, ws, role, [{"text": "Late claim", "kind": "approved"}])
    assert _error(r)[:2] == (409, CLAIMS_INPUT_CLOSED_CODE)
    assert world.count(ClaimsLibraryEntry, workspace_id=ws) == 0
    assert world.count(Brand, workspace_id=ws) == brands
    # The claims library route is the way in.
    created = world.send("POST", f"/workspaces/{ws}/claims", "owner", {"text": "Late claim"})
    assert created.status_code == 201, created.text


def test_a_repeated_onboarding_without_claims_behaves_as_before(world):
    ws = _fresh_workspace(world, "R1 again no claims")
    assert _onboard(world, ws, "owner").status_code == 201
    assert _onboard(world, ws, "marketer").status_code == 201  # unchanged: no claims guard
    assert world.count(Brand, workspace_id=ws) == 2


def test_a_marketer_re_onboarding_with_claims_is_refused_as_a_non_steward(world):
    ws = _fresh_workspace(world, "R1 again marketer")
    assert _onboard(world, ws, "owner").status_code == 201
    r = _onboard(world, ws, "marketer", [{"text": "x"}])
    assert _error(r)[:2] == (403, "permission_denied")


@pytest.mark.parametrize("role", ["reviewer", "compliance_reviewer", "viewer"])
def test_roles_below_the_editor_floor_still_cannot_onboard(world, role):
    ws = _fresh_workspace(world, f"R1 floor {role}")
    assert _error(_onboard(world, ws, role, [{"text": "x"}]))[:2] == (403, "permission_denied")
    _nothing_written(world, ws)


# --------------------------------------------------------------------------- #
# The reason it matters: the generation prompt trusts approved claims
# --------------------------------------------------------------------------- #
def test_a_marketers_onboarding_cannot_put_an_approved_claim_in_front_of_generation(
    world, monkeypatch
):
    for name, value in CEILINGS.items():
        monkeypatch.setattr(get_settings(), name, value)
    ws = _fresh_workspace(world, "R1 into generation")
    r = _onboard(world, ws, "marketer", [{"text": MARKETER_CLAIM, "kind": "approved"}])
    assert r.status_code == 403
    assert _onboard(world, ws, "marketer").status_code == 201  # onboarded without it
    steward = world.send(
        "POST", f"/workspaces/{ws}/claims", "owner", {"text": "Locally roasted"}
    )
    assert steward.status_code == 201

    world.enable(ws)
    brief_id = world.seed_brief(ws)
    with world.session() as s:
        brief = s.get(RecommendationBrief, brief_id)
        from datetime import UTC, datetime

        inputs = generation_context(s, brief, "email", datetime.now(UTC)).inputs
    assert inputs["approved_claims"] == ["Locally roasted"]

    # And end to end: the model request never carries it.
    seen: list[dict] = []
    from app.llm import service as service_module

    real = service_module.llm_service._provider

    class _Spy:
        name, model = real.name, real.model

        def generate(self, request, prompt_name, prompt_version):
            seen.append(dict(request.inputs))
            return real.generate(request, prompt_name, prompt_version)

    monkeypatch.setattr(service_module.llm_service, "_provider", _Spy())
    job = world.generated(brief_id, ws=ws)
    assert job["status"] == "succeeded", job
    assert len(seen) == 1
    assert MARKETER_CLAIM not in str(seen[0])
    assert seen[0]["approved_claims"] == ["Locally roasted"]
    assert world.count(ClaimsLibraryEntry, workspace_id=ws, text=MARKETER_CLAIM) == 0
    with world.witness.connect() as conn:
        assert conn.execute(
            select(ClaimsLibraryEntry.__table__.c.id).where(
                ClaimsLibraryEntry.__table__.c.text == MARKETER_CLAIM
            )
        ).all() == []
