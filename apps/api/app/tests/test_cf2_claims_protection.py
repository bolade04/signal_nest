"""Project Phase 5 CF-2 (W21): the claims library is a protected safety control.

5C's output-side review blocks a generated draft on the workspace's ``blocked`` claims, so the
person whose draft is blocked must not be able to remove the claim that blocks it (Phase 5C
plan §2). For ``/workspaces/{workspace_id}/claims`` only:

* **Exact writers.** Create, update and delete admit exactly ``OWNER``, ``ADMIN`` and
  ``COMPLIANCE_REVIEWER`` (no rank inheritance). **A marketer cannot unblock**: creating,
  editing or retiring a claim is refused (403) and the blocked claim stays as it was and still
  listed. Reviewers and viewers are refused too. Reading stays membership-only.
* **Soft retirement.** ``DELETE`` sets ``retired_at`` and keeps the row; a retired entry is
  no longer listed and cannot be edited or retired again (404, as a missing one).
* **Audited.** Every create, update and retirement writes ``claims_library_entry.created`` /
  ``.updated`` / ``.retired`` naming the actor, the entry and the changed field *names* —
  never the claim text.
* **Nothing else changed.** Another collection (products) keeps the OWNER/ADMIN/MARKETER
  editors, hard delete and no audit record.

This is a disclosed live behaviour change at merge: marketers lose write access to the
``/claims`` routes. **Known exception (coordinator ruling R1):** ``POST /onboarding`` still
lets any editor, a marketer included, *add* claims (any kind, at any time, without a
per-claim audit record) until the 5C build closes it; this module does not test onboarding.

The world is built through real HTTP flows (the P6-PLAT-8 harness of
``test_campaign_context_integrity``) on a temporary SQLite file with foreign keys on; this
module is a CF-2 mutation-check target (writers widened, hard delete, audit removed).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass, field

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from app.audit.models import AuditLog
from app.campaign_context.models import ClaimsLibraryEntry, ProductProfile
from app.campaign_context.routes import CLAIMS_STEWARD_ROLES
from app.core.enums import Role
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.tests.test_campaign_context_integrity import (
    API,
    _engine,
    _error,
    _invite,
    _rate_limiter,
    _register,
    _workspace,
)

BLOCKED_TEXT = "Guaranteed to cure everything"
ROLES = ("admin", "marketer", "reviewer", "compliance_reviewer", "viewer")
STEWARDS = {"owner", "admin", "compliance_reviewer"}


def _denied(role: str) -> tuple[int, str, str]:
    return 403, "permission_denied", f"Role '{role}' is not permitted for this action."


ITEM_404 = (404, "not_found", "Item not found in this workspace.")


@dataclass
class World:
    client: TestClient
    witness: Engine
    org: str = ""
    ws: str = ""
    tokens: dict[str, str] = field(default_factory=dict)  # role -> token (owner included)
    user_ids: dict[str, str] = field(default_factory=dict)

    def send(self, method: str, path: str, role: str, body=None):
        _rate_limiter()._hits.clear()
        kwargs: dict = {"headers": {"Authorization": f"Bearer {self.tokens[role]}"}}
        if body is not None:
            kwargs["json"] = body
        return self.client.request(method, f"{API}/workspaces/{self.ws}{path}", **kwargs)

    def row(self, model, row_id: str) -> dict | None:
        table = model.__table__
        with self.witness.connect() as conn:
            r = conn.execute(select(*table.c).where(table.c.id == row_id)).first()
            return dict(r._mapping) if r else None

    def audits(self, entity_id: str) -> list[dict]:
        table = AuditLog.__table__
        with self.witness.connect() as conn:
            rows = conn.execute(
                select(*table.c)
                .where(table.c.entity_id == entity_id)
                .order_by(table.c.created_at, table.c.id)
            ).all()
        return [dict(r._mapping) for r in rows]

    def claim(self, role: str = "owner", text: str = BLOCKED_TEXT) -> str:
        r = self.send("POST", "/claims", role, {"text": text, "kind": "blocked"})
        assert r.status_code == 201, r.text
        return r.json()["id"]

    def listed(self, role: str = "marketer") -> set[str]:
        r = self.send("GET", "/claims", role)
        assert r.status_code == 200, r.text
        return {x["id"] for x in r.json()}


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[World]:
    db_file = tmp_path_factory.mktemp("cf2-claims") / "claims.db"
    request_engine, witness = _engine(db_file), _engine(db_file)
    Base.metadata.create_all(request_engine)
    factory = sessionmaker(bind=request_engine, autoflush=False, expire_on_commit=False)

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
        w = World(TestClient(app), witness)
        w.org, w.tokens["owner"] = _register(w.client, "cf2claims")
        w.ws = _workspace(w.client, w.org, w.tokens["owner"], "Claims")
        for role in ROLES:
            w.tokens[role] = _invite(w.client, w.org, w.tokens["owner"], role)
        yield w
    finally:
        app.dependency_overrides.clear()
        _rate_limiter()._hits.clear()
        request_engine.dispose()
        witness.dispose()


def test_the_steward_set_is_exact() -> None:
    assert CLAIMS_STEWARD_ROLES == {Role.OWNER, Role.ADMIN, Role.COMPLIANCE_REVIEWER}
    assert {r.value for r in CLAIMS_STEWARD_ROLES} == STEWARDS


# --------------------------------------------------------------------------- #
# A marketer cannot unblock (the negative tests the brief requires)
# --------------------------------------------------------------------------- #
def test_a_marketer_cannot_unblock_a_blocked_claim(world: World) -> None:
    blocked = world.claim()
    before = world.row(ClaimsLibraryEntry, blocked)
    audits_before = world.audits(blocked)

    retire = world.send("DELETE", f"/claims/{blocked}", "marketer")
    assert _error(retire) == _denied("marketer"), retire.text
    downgrade = world.send("PATCH", f"/claims/{blocked}", "marketer", {"kind": "approved"})
    assert _error(downgrade) == _denied("marketer"), downgrade.text
    reword = world.send("PATCH", f"/claims/{blocked}", "marketer", {"text": "Mild claim"})
    assert _error(reword) == _denied("marketer"), reword.text

    # The blocked claim is exactly as it was, still active and still listed.
    assert world.row(ClaimsLibraryEntry, blocked) == before
    assert before["kind"] == "blocked" and before["retired_at"] is None
    assert blocked in world.listed("marketer")
    assert world.audits(blocked) == audits_before  # a refusal writes nothing


def test_a_marketer_cannot_add_a_claim(world: World) -> None:
    with world.witness.connect() as conn:
        count = conn.execute(select(ClaimsLibraryEntry.__table__.c.id)).all()
    r = world.send("POST", "/claims", "marketer", {"text": "Approved claim", "kind": "approved"})
    assert _error(r) == _denied("marketer"), r.text
    with world.witness.connect() as conn:
        assert conn.execute(select(ClaimsLibraryEntry.__table__.c.id)).all() == count


@pytest.mark.parametrize("role", ["marketer", "reviewer", "viewer"])
def test_every_non_steward_is_refused_every_write(world: World, role: str) -> None:
    blocked = world.claim()
    before = world.row(ClaimsLibraryEntry, blocked)
    for method, path, body in (
        ("POST", "/claims", {"text": "x", "kind": "approved"}),
        ("PATCH", f"/claims/{blocked}", {"kind": "approved"}),
        ("DELETE", f"/claims/{blocked}", None),
    ):
        r = world.send(method, path, role, body)
        assert _error(r) == _denied(role), (method, r.text)
    assert world.row(ClaimsLibraryEntry, blocked) == before


@pytest.mark.parametrize("role", ["marketer", "reviewer", "viewer", "compliance_reviewer"])
def test_every_member_can_still_read_the_library(world: World, role: str) -> None:
    assert world.claim() in world.listed(role)


# --------------------------------------------------------------------------- #
# Stewards: create, update, soft-retire — audited
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("role", sorted(STEWARDS))
def test_a_steward_creates_updates_and_retires_with_an_audit_trail(
    world: World, role: str
) -> None:
    created = world.send(
        "POST", "/claims", role, {"text": BLOCKED_TEXT, "kind": "blocked", "category": "health"}
    )
    assert created.status_code == 201, created.text
    claim_id = created.json()["id"]
    assert created.json()["retired_at"] is None

    updated = world.send("PATCH", f"/claims/{claim_id}", role, {"kind": "restricted"})
    assert updated.status_code == 200, updated.text
    assert world.row(ClaimsLibraryEntry, claim_id)["kind"] == "restricted"

    retired = world.send("DELETE", f"/claims/{claim_id}", role)
    assert retired.status_code == 204, retired.text

    # Soft retirement: the row stays, marked; it is gone from the list and from every route.
    row = world.row(ClaimsLibraryEntry, claim_id)
    assert row is not None and row["retired_at"] is not None
    assert row["text"] == BLOCKED_TEXT  # the record of what was blocked is kept
    assert claim_id not in world.listed(role)
    assert _error(world.send("PATCH", f"/claims/{claim_id}", role, {"kind": "blocked"})) == (
        ITEM_404
    )
    assert _error(world.send("DELETE", f"/claims/{claim_id}", role)) == ITEM_404
    assert world.row(ClaimsLibraryEntry, claim_id)["retired_at"] == row["retired_at"]

    audits = world.audits(claim_id)
    assert [(a["action"], a["context"]) for a in audits] == [
        (
            "claims_library_entry.created",
            {"fields": ["category", "kind", "notes", "risk_level", "text"]},
        ),
        ("claims_library_entry.updated", {"fields": ["kind"]}),
        ("claims_library_entry.retired", {"fields": ["retired_at"]}),
    ]
    actor = world.audits(claim_id)[0]["actor_user_id"]
    for audit in audits:
        assert audit["entity_type"] == "claims_library_entry"
        assert (audit["organization_id"], audit["workspace_id"]) == (world.org, world.ws)
        assert audit["actor_user_id"] == actor is not None
        # Field names only: the claim text never reaches the audit record.
        assert BLOCKED_TEXT not in json.dumps(audit, default=str)
        assert audit["previous_state"] is None and audit["new_state"] is None


def test_an_empty_update_writes_no_audit_record(world: World) -> None:
    claim_id = world.claim()
    r = world.send("PATCH", f"/claims/{claim_id}", "compliance_reviewer", {})
    assert r.status_code == 200, r.text
    assert [a["action"] for a in world.audits(claim_id)] == ["claims_library_entry.created"]


# --------------------------------------------------------------------------- #
# Every other collection is unchanged
# --------------------------------------------------------------------------- #
def test_other_collections_keep_marketer_writes_hard_delete_and_no_audit(world: World) -> None:
    created = world.send("POST", "/products", "marketer", {"name": "Widget"})
    assert created.status_code == 201, created.text
    product = created.json()["id"]
    assert "retired_at" not in created.json()
    assert world.send("PATCH", f"/products/{product}", "marketer", {"name": "W2"}).status_code == (
        200
    )
    assert world.send("DELETE", f"/products/{product}", "marketer").status_code == 204
    assert world.row(ProductProfile, product) is None  # hard delete, as before
    assert world.audits(product) == []
    denied = world.send("POST", "/products", "compliance_reviewer", {"name": "Nope"})
    assert _error(denied) == _denied("compliance_reviewer")
