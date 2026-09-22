"""Phase 6U-1H: customer-facing workspace-effective feedback reflection (P6-UI-005).

The feedback *gate* has resolved per workspace since 4B-A. The customer UI, however,
decided availability from ``GET /system/capabilities``, which deliberately reflects the
**raw global** flags (6U-1G / P6-CAP-2). So a workspace whose override enables feedback
was served by the backend while the UI still hid the feature.

These tests pin the customer reflection to the *same decision the gate enforces*, for the
same workspace, and pin the disclosure boundary: a customer learns one boolean and nothing
about how it was decided.

The reflection is advisory. The feedback routes' own 503 remains the enforcement boundary;
nothing here weakens it.

All override rows are synthetic test intent on synthetic workspaces. No shipped flag is
changed and no runtime activation occurs.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

import app.feedback.routes as feedback_routes
from app.capabilities.models import WorkspaceCapabilityOverride
from app.core.config import get_settings
from app.core.enums import Role
from app.core.security import create_access_token

from .test_feedback_capability_gate import h  # noqa: F401  (shared harness fixture)

API = get_settings().api_prefix

# Fields the operator-only effective endpoint exposes that a customer must never see.
GOVERNANCE_FIELDS = frozenset(
    {
        "decided_by",
        "global_flag",
        "has_override",
        "override_value",
        "override_id",
        "reason",
        "set_by_user_id",
        "created_at",
        "updated_at",
        "organization_id",
    }
)


def _url(ws_id: str) -> str:
    return f"{API}/workspaces/{ws_id}/feedback-capability"


def _reflect(h, key: str):  # noqa: F811
    mk = h.m(key)
    return h.client.get(_url(mk.ws_id), headers=mk.owner_auth)


def _force_global(monkeypatch, enabled: bool) -> None:
    """Flip only the feedback global flag, for the route's own settings read."""
    base = get_settings()
    patched = base.model_copy(update={"opportunity_feedback_enabled": enabled})
    monkeypatch.setattr(feedback_routes, "get_settings", lambda: patched)


# --------------------------------------------------------------------------- #
# B1-B4: the precedence matrix, customer-visible
# --------------------------------------------------------------------------- #
def test_b1_global_false_no_override_reflects_disabled(h):  # noqa: F811
    r = _reflect(h, "a")
    assert r.status_code == 200
    assert r.json()["enabled"] is False


def test_b2_global_false_workspace_override_reflects_enabled(h):  # noqa: F811
    """The central P6-UI-005 reproduction: the gate serves this workspace, so must the UI."""
    h.set_override("a", enabled=True)
    r = _reflect(h, "a")
    assert r.status_code == 200
    assert r.json()["enabled"] is True, (
        "workspace override enables the backend gate; the customer reflection must agree"
    )
    # Parity, executed rather than asserted: the gate really does serve this workspace.
    assert h.post("a").status_code == 201


def test_b3_global_true_no_override_reflects_enabled(h, monkeypatch):  # noqa: F811
    _force_global(monkeypatch, True)
    r = _reflect(h, "a")
    assert r.status_code == 200
    assert r.json()["enabled"] is True


def test_b4_global_true_workspace_disable_override_reflects_disabled(h, monkeypatch):  # noqa: F811
    """No test at any layer covered this for feedback before 6U-1H."""
    _force_global(monkeypatch, True)
    h.set_override("a", enabled=False)
    r = _reflect(h, "a")
    assert r.status_code == 200
    assert r.json()["enabled"] is False
    # The gate agrees: a disable override beats a true global flag.
    assert h.post("a").status_code == 503


# --------------------------------------------------------------------------- #
# B5-B6: workspace isolation
# --------------------------------------------------------------------------- #
def test_b5_override_on_a_does_not_enable_sibling_b(h):  # noqa: F811
    h.set_override("a", enabled=True)
    assert _reflect(h, "a").json()["enabled"] is True
    assert _reflect(h, "b").json()["enabled"] is False


def test_b6_disable_on_a_does_not_disable_sibling_b(h, monkeypatch):  # noqa: F811
    _force_global(monkeypatch, True)
    h.set_override("a", enabled=False)
    assert _reflect(h, "a").json()["enabled"] is False
    assert _reflect(h, "b").json()["enabled"] is True


# --------------------------------------------------------------------------- #
# B7-B9: authorization
# --------------------------------------------------------------------------- #
def test_b7_foreign_organization_workspace_is_forbidden(h):  # noqa: F811
    """Caller authenticated, workspace exists, caller is not a member of its org."""
    other = h.m("other")
    r = h.client.get(_url(other.ws_id), headers=h.m("a").owner_auth)
    assert r.status_code == 403


def test_b8_unknown_workspace_is_not_found(h):  # noqa: F811
    """404 must mean *the workspace* is absent, not *the route*.

    A bare `== 404` passes against an unimplemented route, so it is paired with a
    positive control on a real workspace and a check of the error envelope: a routing
    miss answers `{"detail": ...}`, while the tenancy layer answers with the
    application error shape.
    """
    assert h.client.get(_url(h.m("a").ws_id), headers=h.m("a").owner_auth).status_code == 200

    r = h.client.get(_url("ws-does-not-exist"), headers=h.m("a").owner_auth)
    assert r.status_code == 404
    assert "error" in r.json(), f"routing miss, not a tenancy 404: {r.json()}"


def test_b9_non_editor_member_is_forbidden(h):  # noqa: F811
    """Role parity with the feedback routes: a viewer cannot use feedback, so learns nothing."""
    from app.organizations.models import OrganizationMember, User

    make, _ = h.factory
    with make() as s:
        s.add(
            User(
                id="viewer-a",
                email="viewer-a@example.com",
                full_name="v",
                hashed_password="x",
                is_active=True,
            )
        )
        s.flush()
        s.add(
            OrganizationMember(
                id="m-viewer-a",
                organization_id=h.m("a").org_id,
                user_id="viewer-a",
                role=Role.VIEWER.value,
            )
        )
        s.commit()
    viewer = {"Authorization": f"Bearer {create_access_token('viewer-a')}"}
    r = h.client.get(_url(h.m("a").ws_id), headers=viewer)
    assert r.status_code == 403


def test_b9b_anonymous_is_unauthorized(h):  # noqa: F811
    assert h.client.get(_url(h.m("a").ws_id)).status_code == 401


# --------------------------------------------------------------------------- #
# B10: fail-closed — a dependency failure is an error, never a quiet `false`
# --------------------------------------------------------------------------- #
def test_b10_resolver_failure_propagates_and_never_reflects_enabled(h, monkeypatch):  # noqa: F811
    """A coerced ``false`` would be indistinguishable from a real governance denial."""

    def _boom(**_kw):
        raise RuntimeError("resolver down")

    monkeypatch.setattr(feedback_routes, "resolve_capability", _boom)
    with pytest.raises(RuntimeError):
        _reflect(h, "a")


# --------------------------------------------------------------------------- #
# B11-B12: disclosure and read-only guarantees
# --------------------------------------------------------------------------- #
def test_b11_response_carries_no_governance_metadata(h):  # noqa: F811
    h.set_override("a", enabled=True)
    body = _reflect(h, "a").json()
    assert set(body) == {"enabled"}, f"unexpected customer-visible fields: {sorted(body)}"
    assert not (set(body) & GOVERNANCE_FIELDS)


def test_b12_reflection_mutates_nothing(h):  # noqa: F811
    make, _ = h.factory
    with make() as s:
        overrides_before = int(
            s.scalar(select(func.count()).select_from(WorkspaceCapabilityOverride)) or 0
        )
    assert _reflect(h, "a").status_code == 200
    assert _reflect(h, "a").status_code == 200
    with make() as s:
        overrides_after = int(
            s.scalar(select(func.count()).select_from(WorkspaceCapabilityOverride)) or 0
        )
    assert overrides_after == overrides_before
    assert h.feedback_count() == 0
    assert get_settings().opportunity_feedback_enabled is False


# --------------------------------------------------------------------------- #
# The invariant this tranche exists to establish
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("global_enabled", "override"),
    [(False, None), (False, True), (False, False), (True, None), (True, True), (True, False)],
)
def test_reflection_equals_gate_enforcement_for_every_precedence_case(
    h,  # noqa: F811
    monkeypatch,
    global_enabled,
    override,  # noqa: F811
):
    """CUSTOMER_REFLECTION == BACKEND_FEEDBACK_ENFORCEMENT, proven by execution.

    Not by reading both call sites and agreeing they look alike: the reflection is read
    and the gate is exercised, and the two must never disagree.
    """
    _force_global(monkeypatch, global_enabled)
    if override is not None:
        h.set_override("a", enabled=override)

    reflected = _reflect(h, "a").json()["enabled"]
    gate_allows = h.post("a").status_code == 201
    assert reflected is gate_allows, (
        f"reflection={reflected} but gate_allows={gate_allows} "
        f"(global={global_enabled}, override={override})"
    )
