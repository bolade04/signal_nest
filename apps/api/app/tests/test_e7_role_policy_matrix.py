"""6B-3A: the E7 authorization matrix, on memberships obtained only through real flows.

Phase-6 demonstration E7 reads: "A second user has been invited to an organization, holds
a non-OWNER role, and the full authorization matrix -- including the P6-AUTH-3 audit-route
case -- is proven by test." This module is that proof for the backend. It does not close
E7 on its own: the customer UI (6B-3B) and the role-aware mutation controls are not built.

How the six roles come to exist. No membership row is written by this module. The OWNER
registers through ``POST /auth/register``, which creates the organization. Every other
role is obtained through a real invitation created by that OWNER through
``POST /organizations/{id}/invitations``:

* ADMIN, MARKETER and REVIEWER are new users who register through the invitation
  (``POST /auth/invitations/register``);
* COMPLIANCE_REVIEWER and VIEWER already have accounts -- each registered normally and so
  OWNS an organization of their own -- and accept the invitation while authenticated
  (``POST /auth/invitations/accept``). Their OWNER role elsewhere is exactly what must not
  leak into the invited organization.

The non-member is also a real registered user (OWNER of an unrelated organization). Every
bearer token is the ``access_token`` a real flow returned. A structural test pins that this
module constructs no membership, user or organization row itself.

The policy table. ``E7_POLICY`` is the explicit, machine-readable policy: one row per
role-gated or membership-only customer route -- method, path, scope, allowed roles, denied
roles, category, and the probe that proves an allowed role cleared the gate. Its
completeness is not taken on trust: the served routes are walked through FastAPI's own
effective route contexts (the ones that handle requests and generate OpenAPI, include-level
dependencies included), every role checker found is *called* with each of the six roles to
read the set it admits, and the table must equal what the application actually mounts. A
new role-gated route without a row, a row whose roles drift from the checker, or a new
customer route in no category fails here. The detector is shown to find a checker on a
synthetic route (positive control) and the completeness check is shown to fail on a table
missing one row.

What each probe proves, and why it is side-effect free.

* A **denied** role must receive exactly ``403 / permission_denied`` with the role message
  ``Role '<role>' is not permitted for this action.`` -- not the membership message, not
  some other failure.
* An **allowed** role must provably pass the gate. Any failure is not success, so each row
  names the exact outcome that can only occur after the gate: ``422`` on a valid-JSON but
  schema-invalid body (``[]``) whose error location is ``body`` -- FastAPI resolves
  dependencies before validating the body, and the denied roles sending the *same* body
  get 403 --; a handler-level ``404`` for a sentinel id, with the handler's own message
  (distinct from the dependency's "Workspace not found."); ``503 capability_unavailable``
  from a dark capability checked in the handler; or a plain ``200`` read.
* Membership-only routes: every member passes with the same exact outcome; the non-member
  gets ``403`` with the membership message.
* Every probe is sent with the SQLite file's bytes hashed before and after, so a probe that
  committed anything at all -- not just an insert -- fails.
"""

from __future__ import annotations

import ast
import hashlib
import json
from collections.abc import Iterator
from dataclasses import asdict, dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, func, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

import app.auth.dependencies as deps
from app.core.config import get_settings
from app.core.enums import Role
from app.core.errors import PermissionDeniedError
from app.core.middleware import RateLimitMiddleware
from app.db.models import Base
from app.db.session import get_db
from app.main import app
from app.organizations.models import (
    Organization,
    OrganizationInvitation,
    OrganizationMember,
)

API = get_settings().api_prefix

OWNER, ADMIN, MARKETER = "owner", "admin", "marketer"
REVIEWER, COMPLIANCE, VIEWER = "reviewer", "compliance_reviewer", "viewer"
ALL_ROLES = (OWNER, ADMIN, MARKETER, REVIEWER, COMPLIANCE, VIEWER)
assert set(ALL_ROLES) == {r.value for r in Role}, "the role vocabulary changed"

EDITORS = (OWNER, ADMIN, MARKETER)  # require_role(OWNER, ADMIN, MARKETER): a rank floor
ORG_ADMINS = (OWNER, ADMIN)  # require_exact_organization_roles(OWNER, ADMIN)
AUDIT_READERS = (OWNER, ADMIN, COMPLIANCE)  # require_exact_roles(...) -- P6-AUTH-3

# Categories.
RANK_FLOOR = "role_gated_workspace_rank_floor"
WS_EXACT = "role_gated_workspace_exact"
ORG_EXACT = "role_gated_organization_exact"
MEMBER_WS = "membership_workspace"
MEMBER_ORG = "membership_organization"
MEMBER_HANDLER = "membership_handler_checked"
ROLE_GATED = (RANK_FLOOR, WS_EXACT, ORG_EXACT)
MEMBERSHIP_ONLY = (MEMBER_WS, MEMBER_ORG)

SENTINEL = "e7-sentinel-never-created"  # fits String(32)
ROLE_DENIAL = "Role '{role}' is not permitted for this action."
NOT_A_MEMBER = "You are not a member of this organization."  # shared seam
LEGACY_NOT_A_MEMBER = "Not a member of this organization."  # GET-route helper


@dataclass(frozen=True)
class Probe:
    """The request an allowed role sends, and the outcome that proves it passed the gate."""

    body: object | None  # JSON body sent, or None for no body
    status: int
    code: str | None  # error envelope code, or None for a success
    message: str | None


def _body_422() -> Probe:
    return Probe(body=[], status=422, code="validation_error", message="Request validation failed")


def _handler_404(message: str, code: str = "not_found") -> Probe:
    return Probe(body=None, status=404, code=code, message=message)


def _dark_503(message: str) -> Probe:
    return Probe(body=None, status=503, code="capability_unavailable", message=message)


OK_200 = Probe(body=None, status=200, code=None, message=None)
ITEM_404 = "Item not found in this workspace."
SCOUT_404 = "Scout request not found in this workspace."
OPP_404 = "Opportunity not found in this workspace."
JOB_404 = "Job not found in this workspace."
SCHEDULING_DARK = "Scout scheduling is not available yet."
FEEDBACK_DARK = "Opportunity feedback is not available yet."
# New in 6B-3A. Messages are the service's; the codes are frozen by the contract (§50).
INVITATION_NOT_FOUND = ("invitation_not_found", "Invitation not found.")
MEMBER_NOT_FOUND = ("member_not_found", "Member not found.")


@dataclass(frozen=True)
class Policy:
    method: str
    path: str  # without the API prefix
    scope: str  # "workspace" | "organization"
    allowed: tuple[str, ...]
    denied: tuple[str, ...]
    category: str
    probe: Probe

    @property
    def key(self) -> tuple[str, str]:
        return self.method, API + self.path


def _row(method, path, scope, allowed, category, probe) -> Policy:
    denied = tuple(r for r in ALL_ROLES if r not in allowed)
    return Policy(method, path, scope, tuple(allowed), denied, category, probe)


_CONTEXT = (
    "products",
    "audiences",
    "competitors",
    "brand-voice",
    "offers",
    "claims",
    "source-preferences",
    "channel-preferences",
    "campaigns",
)
_W = "/workspaces/{workspace_id}"
_O = "/organizations/{organization_id}"

#: The explicit policy table. 44 role-gated rows (39 that existed before 6B-3A + 5 new
#: OWNER/ADMIN routes), 25 membership-only rows (24 workspace + 1 new organization route),
#: and the 2 GET routes whose membership check is inside the handler.
E7_POLICY: tuple[Policy, ...] = (
    # --- role-gated, workspace scope, rank floor (require_role(OWNER, ADMIN, MARKETER)) -----
    _row("POST", f"{_W}/onboarding", "workspace", EDITORS, RANK_FLOOR, _body_422()),
    _row("PUT", f"{_W}/business-profile", "workspace", EDITORS, RANK_FLOOR, _body_422()),
    *(_row("POST", f"{_W}/{c}", "workspace", EDITORS, RANK_FLOOR, _body_422()) for c in _CONTEXT),
    *(
        _row(
            "DELETE",
            f"{_W}/{c}/{{item_id}}",
            "workspace",
            EDITORS,
            RANK_FLOOR,
            _handler_404(ITEM_404),
        )
        for c in _CONTEXT
    ),
    _row("POST", f"{_W}/locations", "workspace", EDITORS, RANK_FLOOR, _body_422()),
    _row("PUT", f"{_W}/locations/{{location_id}}", "workspace", EDITORS, RANK_FLOOR, _body_422()),
    _row(
        "PUT",
        f"{_W}/locations/{{location_id}}/geo-coverage",
        "workspace",
        EDITORS,
        RANK_FLOOR,
        _body_422(),
    ),
    _row("POST", f"{_W}/scout-requests", "workspace", EDITORS, RANK_FLOOR, _body_422()),
    _row(
        "PUT", f"{_W}/scout-requests/{{request_id}}", "workspace", EDITORS, RANK_FLOOR, _body_422()
    ),
    *(
        _row(
            "POST",
            f"{_W}/scout-requests/{{request_id}}/{verb}",
            "workspace",
            EDITORS,
            RANK_FLOOR,
            _handler_404(SCOUT_404),
        )
        for verb in ("pause", "resume", "run")
    ),
    _row(
        "POST",
        f"{_W}/scout-requests/{{request_id}}/schedule",
        "workspace",
        EDITORS,
        RANK_FLOOR,
        _body_422(),
    ),
    *(
        _row(
            "POST",
            f"{_W}/scout-requests/{{request_id}}/schedule/{verb}",
            "workspace",
            EDITORS,
            RANK_FLOOR,
            _dark_503(SCHEDULING_DARK),
        )
        for verb in ("pause", "resume")
    ),
    _row(
        "DELETE",
        f"{_W}/scout-requests/{{request_id}}/schedule",
        "workspace",
        EDITORS,
        RANK_FLOOR,
        _dark_503(SCHEDULING_DARK),
    ),
    _row(
        "PUT",
        f"{_W}/opportunities/{{opportunity_id}}/status",
        "workspace",
        EDITORS,
        RANK_FLOOR,
        _body_422(),
    ),
    _row("GET", f"{_W}/feedback-capability", "workspace", EDITORS, RANK_FLOOR, OK_200),
    _row(
        "POST",
        f"{_W}/opportunities/{{opportunity_id}}/feedback",
        "workspace",
        EDITORS,
        RANK_FLOOR,
        _body_422(),
    ),
    _row(
        "GET",
        f"{_W}/opportunities/{{opportunity_id}}/feedback",
        "workspace",
        EDITORS,
        RANK_FLOOR,
        _dark_503(FEEDBACK_DARK),
    ),
    _row(
        "POST",
        f"{_W}/jobs/{{job_id}}/cancel",
        "workspace",
        EDITORS,
        RANK_FLOOR,
        _handler_404(JOB_404),
    ),
    # --- role-gated, workspace scope, exact set -- the P6-AUTH-3 audit-route case ----------
    _row("GET", f"{_W}/audit-logs", "workspace", AUDIT_READERS, WS_EXACT, OK_200),
    # --- role-gated, organization scope, exact set ------------------------------------------
    _row("POST", f"{_O}/workspaces", "organization", ORG_ADMINS, ORG_EXACT, _body_422()),
    _row("POST", f"{_O}/invitations", "organization", ORG_ADMINS, ORG_EXACT, _body_422()),
    _row("GET", f"{_O}/invitations", "organization", ORG_ADMINS, ORG_EXACT, OK_200),
    _row(
        "DELETE",
        f"{_O}/invitations/{{invitation_id}}",
        "organization",
        ORG_ADMINS,
        ORG_EXACT,
        Probe(None, 404, INVITATION_NOT_FOUND[0], INVITATION_NOT_FOUND[1]),
    ),
    _row(
        "PUT", f"{_O}/members/{{user_id}}/role", "organization", ORG_ADMINS, ORG_EXACT, _body_422()
    ),
    _row(
        "DELETE",
        f"{_O}/members/{{user_id}}",
        "organization",
        ORG_ADMINS,
        ORG_EXACT,
        Probe(None, 404, MEMBER_NOT_FOUND[0], MEMBER_NOT_FOUND[1]),
    ),
    # --- membership only, workspace scope (get_tenant_context, no role checker) ------------
    *(_row("GET", f"{_W}/{c}", "workspace", ALL_ROLES, MEMBER_WS, OK_200) for c in _CONTEXT),
    *(
        _row("GET", f"{_W}/{c}", "workspace", ALL_ROLES, MEMBER_WS, OK_200)
        for c in ("brands", "locations", "scout-requests", "opportunities", "jobs")
    ),
    _row(
        "GET",
        f"{_W}/business-profile",
        "workspace",
        ALL_ROLES,
        MEMBER_WS,
        _handler_404("No business profile for this workspace yet."),
    ),
    _row(
        "GET",
        f"{_W}/locations/{{location_id}}/geo-coverage",
        "workspace",
        ALL_ROLES,
        MEMBER_WS,
        _handler_404("No Scout Reach configured for this location yet."),
    ),
    *(
        _row(
            "GET",
            f"{_W}/scout-requests/{{request_id}}{tail}",
            "workspace",
            ALL_ROLES,
            MEMBER_WS,
            _handler_404(SCOUT_404),
        )
        for tail in ("", "/runs", "/schedule")
    ),
    *(
        _row(
            "GET",
            f"{_W}/opportunities/{{opportunity_id}}{tail}",
            "workspace",
            ALL_ROLES,
            MEMBER_WS,
            _handler_404(OPP_404),
        )
        for tail in ("", "/intelligence")
    ),
    *(
        _row(
            "GET",
            f"{_W}/jobs/{{job_id}}{tail}",
            "workspace",
            ALL_ROLES,
            MEMBER_WS,
            _handler_404(JOB_404),
        )
        for tail in ("", "/events")
    ),
    # workspace_id is a QUERY parameter here; the body is the probe.
    _row("POST", "/geocode", "workspace", ALL_ROLES, MEMBER_WS, _body_422()),
    # --- membership only, organization scope (get_organization_context) -------------------
    _row("GET", f"{_O}/members", "organization", ALL_ROLES, MEMBER_ORG, OK_200),
    # --- membership checked inside the handler (dependency is get_current_user only) ------
    _row("GET", f"{_O}/workspaces", "organization", ALL_ROLES, MEMBER_HANDLER, OK_200),
    _row("GET", _W, "workspace", ALL_ROLES, MEMBER_HANDLER, OK_200),
)

#: Authenticated routes that are deliberately NOT membership-scoped.
AUTHENTICATED_ONLY = {
    ("GET", f"{API}/auth/me"),
    ("GET", f"{API}/organizations"),
    ("GET", f"{API}/system/capabilities"),
    ("POST", f"{API}/auth/invitations/accept"),
}
#: Routes with no authentication dependency at all.
PUBLIC = {
    ("GET", "/health"),
    ("GET", f"{API}/system/health"),
    ("GET", f"{API}/system/readiness"),
    ("POST", f"{API}/auth/register"),
    ("POST", f"{API}/auth/login"),
    ("POST", f"{API}/auth/invitations/preview"),
    ("POST", f"{API}/auth/invitations/register"),
}
OPERATOR_ROUTE_COUNT = 17


# --------------------------------------------------------------------------- #
# Route introspection -- the served (effective) routes, not the declared ones
# --------------------------------------------------------------------------- #
def _dependency_calls(dependant) -> list:
    seen: set[int] = set()
    out: list = []

    def walk(d) -> None:
        nonlocal seen
        if d.call is not None and id(d.call) not in seen:
            seen = seen | {id(d.call)}
            out.append(d.call)
        for sub in d.dependencies:
            walk(sub)

    walk(dependant)
    return out


def _is_role_checker(call) -> bool:
    return getattr(call, "__module__", "") == deps.__name__ and getattr(
        call, "__qualname__", ""
    ).endswith("<locals>._checker")


def _admitted(checker) -> tuple[str, ...]:
    """The roles ``checker`` admits, read by calling it -- not by re-deriving rank logic."""
    admitted = []
    for role in Role:
        try:
            checker(ctx=SimpleNamespace(role=role))
        except PermissionDeniedError:
            continue
        admitted.append(role.value)
    return tuple(r for r in ALL_ROLES if r in admitted)


@dataclass(frozen=True)
class ServedRoute:
    method: str
    path: str
    #: role_gated | membership_workspace | membership_organization | operator |
    #: authenticated | public
    category: str
    admitted: tuple[str, ...] | None


def _classify(calls) -> tuple[str, tuple[str, ...] | None]:
    checkers = [c for c in calls if _is_role_checker(c)]
    assert len(checkers) <= 1, f"more than one role checker on one route: {checkers}"
    if checkers:
        return "role_gated", _admitted(checkers[0])
    if any(c is deps.require_operator for c in calls):
        return "operator", None
    if any(c is deps.get_tenant_context for c in calls):
        return MEMBER_WS, None
    if any(c is deps.get_organization_context for c in calls):
        return MEMBER_ORG, None
    if any(c is deps.get_current_user for c in calls):
        return "authenticated", None
    return "public", None


def _served_routes(application: FastAPI = app) -> list[ServedRoute]:
    out = []
    for rc in iter_route_contexts(application.routes):
        dependant = getattr(rc, "dependant", None)
        if dependant is None:
            continue
        methods = sorted(set(rc.methods) - {"HEAD", "OPTIONS"})
        assert len(methods) == 1, (rc.path, methods)
        category, admitted = _classify(_dependency_calls(dependant))
        out.append(ServedRoute(methods[0], rc.path, category, admitted))
    return out


def _completeness_diff(table: tuple[Policy, ...], served: list[ServedRoute]) -> dict:
    """Every disagreement between the policy table and the mounted application."""
    table_gated = {p.key: p.allowed for p in table if p.category in ROLE_GATED}
    table_member = {p.key: p.category for p in table if p.category in MEMBERSHIP_ONLY}
    table_handler = {p.key for p in table if p.category == MEMBER_HANDLER}
    served_gated = {(r.method, r.path): r.admitted for r in served if r.category == "role_gated"}
    served_member = {
        (r.method, r.path): r.category for r in served if r.category in MEMBERSHIP_ONLY
    }
    served_auth = {(r.method, r.path) for r in served if r.category == "authenticated"}
    served_public = {(r.method, r.path) for r in served if r.category == "public"}
    return {
        "role_gated_missing_from_table": sorted(set(served_gated) - set(table_gated)),
        "role_gated_rows_not_mounted": sorted(set(table_gated) - set(served_gated)),
        "role_set_mismatch": sorted(
            (k, table_gated[k], served_gated[k])
            for k in set(table_gated) & set(served_gated)
            if table_gated[k] != served_gated[k]
        ),
        "membership_missing_from_table": sorted(set(served_member) - set(table_member)),
        "membership_rows_not_mounted": sorted(set(table_member) - set(served_member)),
        "membership_scope_mismatch": sorted(
            k for k in set(table_member) & set(served_member) if table_member[k] != served_member[k]
        ),
        "unclassified_authenticated": sorted(served_auth - AUTHENTICATED_ONLY - table_handler),
        "handler_rows_not_authenticated_only": sorted(table_handler - served_auth),
        "unexpected_public": sorted(served_public ^ PUBLIC),
    }


class TestPolicyTableCompleteness:
    """The table is complete against the application, and the check itself can fail."""

    def test_the_table_is_complete_against_the_served_routes(self):
        diff = _completeness_diff(E7_POLICY, _served_routes())
        assert diff == {k: [] for k in diff}, json.dumps(diff, indent=2, default=str)

    def test_category_counts(self):
        served = _served_routes()
        counts = {
            c: sum(1 for r in served if r.category == c) for c in {r.category for r in served}
        }
        assert counts == {
            "role_gated": 44,
            MEMBER_WS: 24,
            MEMBER_ORG: 1,
            "operator": OPERATOR_ROUTE_COUNT,
            "authenticated": len(AUTHENTICATED_ONLY) + 2,
            "public": len(PUBLIC),
        }
        by_category = {
            c: sum(1 for p in E7_POLICY if p.category == c) for c in {p.category for p in E7_POLICY}
        }
        assert by_category == {
            RANK_FLOOR: 37,
            WS_EXACT: 1,
            ORG_EXACT: 6,
            MEMBER_WS: 24,
            MEMBER_ORG: 1,
            MEMBER_HANDLER: 2,
        }

    def test_the_walk_sees_every_served_operation(self):
        """The introspection covers exactly what OpenAPI publishes -- no route is invisible."""
        served = {(r.method, r.path) for r in _served_routes()}
        published = {
            (method.upper(), path)
            for path, item in app.openapi()["paths"].items()
            for method in item
            if method in {"get", "post", "put", "delete", "patch"}
        }
        assert served == published
        assert len(served) == 99

    def test_one_row_per_route(self):
        keys = [p.key for p in E7_POLICY]
        assert len(keys) == len(set(keys))

    def test_rows_are_machine_readable_and_partition_the_roles(self):
        rows = json.loads(json.dumps([asdict(p) for p in E7_POLICY]))
        assert len(rows) == 71
        for row in rows:
            assert set(row) == {"method", "path", "scope", "allowed", "denied", "category", "probe"}
            assert set(row["allowed"]) | set(row["denied"]) == set(ALL_ROLES)
            assert not set(row["allowed"]) & set(row["denied"])
            assert row["scope"] in {"workspace", "organization"}

    def test_completeness_check_fails_when_a_row_is_missing(self):
        """Negative control: drop one role-gated row and one membership row."""
        victim_gated = next(
            p for p in E7_POLICY if p.category == ORG_EXACT and "invitations" in p.path
        )
        victim_member = next(p for p in E7_POLICY if p.category == MEMBER_ORG)
        mutated = tuple(p for p in E7_POLICY if p not in (victim_gated, victim_member))
        diff = _completeness_diff(mutated, _served_routes())
        assert diff["role_gated_missing_from_table"] == [victim_gated.key]
        assert diff["membership_missing_from_table"] == [victim_member.key]

    def test_completeness_check_fails_when_a_role_set_drifts(self):
        audit = next(p for p in E7_POLICY if p.category == WS_EXACT)
        drifted = tuple(
            _row(p.method, p.path, p.scope, (OWNER, ADMIN), p.category, p.probe)
            if p is audit
            else p
            for p in E7_POLICY
        )
        diff = _completeness_diff(drifted, _served_routes())
        assert diff["role_set_mismatch"] == [(audit.key, (OWNER, ADMIN), AUDIT_READERS)]

    def test_the_detector_finds_each_checker_kind_on_a_synthetic_route(self):
        """Positive control: each production factory is recognised and its set read by call."""
        router = APIRouter()

        @router.get("/floor/{workspace_id}")
        def _floor(ctx=Depends(deps.require_role(Role.REVIEWER))):  # noqa: B008
            return None

        @router.get("/exact/{workspace_id}")
        def _exact(ctx=Depends(deps.require_exact_roles(Role.OWNER, Role.COMPLIANCE_REVIEWER))):  # noqa: B008
            return None

        @router.get("/org/{organization_id}")
        def _org(ctx=Depends(deps.require_exact_organization_roles(Role.ADMIN))):  # noqa: B008
            return None

        @router.get("/member/{workspace_id}")
        def _member(ctx=Depends(deps.get_tenant_context)):  # noqa: B008
            return None

        synthetic = FastAPI()
        synthetic.include_router(router, prefix="/x")
        found = {r.path: (r.category, r.admitted) for r in _served_routes(synthetic)}
        assert found == {
            "/x/floor/{workspace_id}": (
                "role_gated",
                (OWNER, ADMIN, MARKETER, REVIEWER, COMPLIANCE),
            ),
            "/x/exact/{workspace_id}": ("role_gated", (OWNER, COMPLIANCE)),
            "/x/org/{organization_id}": ("role_gated", (ADMIN,)),
            "/x/member/{workspace_id}": (MEMBER_WS, None),
        }

    def test_include_level_dependencies_are_seen(self):
        """A gate attached at include_router() time is on the served route, not the declared one."""
        router = APIRouter()

        @router.get("/inc/{workspace_id}")
        def _inc():
            return None

        synthetic = FastAPI()
        synthetic.include_router(
            router, dependencies=[Depends(deps.require_exact_roles(Role.VIEWER))]
        )
        assert [(r.category, r.admitted) for r in _served_routes(synthetic)] == [
            ("role_gated", (VIEWER,))
        ]


# --------------------------------------------------------------------------- #
# The world: every membership obtained through a real flow
# --------------------------------------------------------------------------- #
def _sqlite_file_engine(db_file: Path) -> Engine:
    engine = create_engine(
        f"sqlite:///{db_file}", connect_args={"check_same_thread": False}, future=True
    )

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _):  # pragma: no cover - trivial pragma hook
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.close()

    return engine


def _active_rate_limiter() -> RateLimitMiddleware:
    if app.middleware_stack is None:
        app.middleware_stack = app.build_middleware_stack()
    node = app.middleware_stack
    while node is not None and not isinstance(node, RateLimitMiddleware):
        node = getattr(node, "app", None)
    assert node is not None, "RateLimitMiddleware not found in app.middleware_stack"
    return node


def _assert_only_get_db_overridden() -> None:
    assert set(app.dependency_overrides) == {get_db}, (
        f"dependency overrides are {set(app.dependency_overrides)!r}; only get_db may be "
        "overridden, or the authorization under test is not the production path"
    )


@dataclass
class World:
    client: TestClient
    db_file: Path
    witness_engine: Engine
    organization_id: str
    workspace_id: str
    tokens: dict[str, str]  # role -> access_token from a real flow, plus "outsider"
    user_ids: dict[str, str]

    def send(self, method: str, url: str, who: str, body: object | None = None, params=None):
        _assert_only_get_db_overridden()
        _active_rate_limiter()._hits.clear()
        headers = {"Authorization": f"Bearer {self.tokens[who]}"}
        if body is None:
            return self.client.request(method, url, headers=headers, params=params)
        return self.client.request(method, url, headers=headers, json=body, params=params)

    def file_digest(self) -> str:
        return hashlib.sha256(self.db_file.read_bytes()).hexdigest()


def _post(client: TestClient, url: str, body: dict, token: str | None = None):
    _assert_only_get_db_overridden()
    _active_rate_limiter()._hits.clear()
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return client.post(url, json=body, headers=headers)


_PASSWORD = "e7-password-123"


def _register(client, email: str, org_name: str) -> dict:
    r = _post(
        client,
        f"{API}/auth/register",
        {"email": email, "full_name": email, "password": _PASSWORD, "organization_name": org_name},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _invite(client, owner_token: str, org_id: str, email: str, role: str) -> str:
    r = _post(
        client,
        f"{API}/organizations/{org_id}/invitations",
        {"email": email, "role": role},
        owner_token,
    )
    assert r.status_code == 201, r.text
    return r.json()["token"]


@pytest.fixture(scope="module")
def world(tmp_path_factory) -> Iterator[World]:
    db_file = tmp_path_factory.mktemp("e7") / "e7.db"
    request_engine = _sqlite_file_engine(db_file)
    witness_engine = _sqlite_file_engine(db_file)
    Base.metadata.create_all(request_engine)
    factory = sessionmaker(
        bind=request_engine, autoflush=False, expire_on_commit=False, future=True
    )

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
    client = TestClient(app)
    try:
        owner = _register(client, "e7-owner@example.com", "E7 Organization")
        org_id = owner["memberships"][0]["organization_id"]
        owner_token = owner["access_token"]
        ws = _post(
            client, f"{API}/organizations/{org_id}/workspaces", {"name": "E7 WS"}, owner_token
        )
        assert ws.status_code == 201, ws.text
        tokens = {OWNER: owner_token}
        user_ids = {OWNER: owner["user"]["id"]}

        # New users: invited registration.
        for role in (ADMIN, MARKETER, REVIEWER):
            email = f"e7-{role}@example.com"
            invite_token = _invite(client, owner_token, org_id, email, role)
            r = _post(
                client,
                f"{API}/auth/invitations/register",
                {"token": invite_token, "full_name": role, "password": _PASSWORD},
            )
            assert r.status_code == 201, r.text
            tokens[role], user_ids[role] = r.json()["access_token"], r.json()["user"]["id"]

        # Existing users (each OWNER of their own organization): authenticated acceptance.
        for role in (COMPLIANCE, VIEWER):
            email = f"e7-{role.replace('_', '-')}@example.com"
            own = _register(client, email, f"Own org of {role}")
            invite_token = _invite(client, owner_token, org_id, email, role)
            r = _post(
                client,
                f"{API}/auth/invitations/accept",
                {"token": invite_token},
                own["access_token"],
            )
            assert r.status_code == 200, r.text
            assert r.json()["user"]["id"] == own["user"]["id"]
            tokens[role], user_ids[role] = r.json()["access_token"], own["user"]["id"]

        outsider = _register(client, "e7-outsider@example.com", "Unrelated Org")
        tokens["outsider"], user_ids["outsider"] = outsider["access_token"], outsider["user"]["id"]

        yield World(
            client=client,
            db_file=db_file,
            witness_engine=witness_engine,
            organization_id=org_id,
            workspace_id=ws.json()["id"],
            tokens=tokens,
            user_ids=user_ids,
        )
    finally:
        app.dependency_overrides.clear()
        _active_rate_limiter()._hits.clear()
        request_engine.dispose()
        witness_engine.dispose()


def _url(world: World, policy: Policy) -> tuple[str, dict[str, str] | None]:
    path = policy.path
    params = None
    for name, value in (
        ("{workspace_id}", world.workspace_id),
        ("{organization_id}", world.organization_id),
    ):
        path = path.replace(name, value)
    for name in (
        "{item_id}",
        "{location_id}",
        "{request_id}",
        "{opportunity_id}",
        "{job_id}",
        "{invitation_id}",
        "{user_id}",
    ):
        path = path.replace(name, SENTINEL)
    assert "{" not in path, path
    if policy.path == "/geocode":
        params = {"workspace_id": world.workspace_id}
    return API + path, params


def _observe(world: World, policy: Policy, who: str) -> tuple:
    url, params = _url(world, policy)
    r = world.send(policy.method, url, who, body=policy.probe.body, params=params)
    payload = r.json() if r.content else None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = error["code"] if error else None
    message = error["message"] if error else None
    loc = None
    if error and code == "validation_error":
        loc = tuple(error["details"][0]["loc"][:1])
    return r.status_code, code, message, loc


def _expected_allowed(policy: Policy) -> tuple:
    p = policy.probe
    loc = ("body",) if p.status == 422 else None
    return p.status, p.code, p.message, loc


class TestWorldIsBuiltFromRealFlows:
    def test_memberships_are_exactly_the_invited_roles(self, world: World):
        with sessionmaker(bind=world.witness_engine)() as s:
            rows = set(
                s.execute(
                    select(OrganizationMember.user_id, OrganizationMember.role).where(
                        OrganizationMember.organization_id == world.organization_id
                    )
                )
            )
            accepted = set(
                s.execute(
                    select(OrganizationInvitation.accepted_by_user_id, OrganizationInvitation.role)
                    .where(OrganizationInvitation.organization_id == world.organization_id)
                    .where(OrganizationInvitation.accepted_at.is_not(None))
                )
            )
            organizations = s.scalar(select(func.count()).select_from(Organization))
        assert rows == {(world.user_ids[r], r) for r in ALL_ROLES}
        # Every non-OWNER membership is backed by an accepted invitation for that user and role.
        assert accepted == {(world.user_ids[r], r) for r in ALL_ROLES if r != OWNER}
        # E7 org + the two existing users' own orgs + the outsider's; invited registration: none.
        assert organizations == 4

    def test_the_existing_users_keep_their_own_ownership(self, world: World):
        for role in (COMPLIANCE, VIEWER):
            r = world.send("GET", f"{API}/auth/me", role)
            assert r.status_code == 200
            roles = sorted(m["role"] for m in r.json()["memberships"])
            assert roles == sorted([OWNER, role])

    def test_this_module_writes_no_row_itself(self):
        """Structural: no model constructor, no insert/update/delete construct, no session add.

        So every membership the matrix runs on can only have come from a real HTTP flow.
        The module's own database access is ``select`` through the witness, nothing else.
        """
        tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
        models = {
            "OrganizationMember",
            "User",
            "Organization",
            "Workspace",
            "OrganizationInvitation",
        }
        writers = {"insert", "update", "delete"}
        offending = [
            (node.lineno, ast.unparse(node.func))
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                (isinstance(node.func, ast.Name) and node.func.id in models | writers)
                or (
                    isinstance(node.func, ast.Attribute)
                    and node.func.attr in {"add", "add_all", "merge", "bulk_save_objects"}
                )
            )
        ]
        assert offending == []
        # The scan is live: it does see a call in this module (positive control).
        assert any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "select"
            for n in ast.walk(tree)
        )


@pytest.mark.parametrize(
    "policy",
    [p for p in E7_POLICY if p.category in ROLE_GATED],
    ids=lambda p: f"{p.method} {p.path}",
)
def test_role_gated_row(world: World, policy: Policy):
    """Every role on one row: exact role denial, or a provable pass of the gate; no write."""
    before = world.file_digest()
    observed = {who: _observe(world, policy, who) for who in (*ALL_ROLES, "outsider")}
    expected = {role: _expected_allowed(policy) for role in policy.allowed}
    expected |= {
        role: (403, "permission_denied", ROLE_DENIAL.format(role=role), None)
        for role in policy.denied
    }
    expected["outsider"] = (403, "permission_denied", NOT_A_MEMBER, None)
    assert observed == expected
    assert world.file_digest() == before, "a probe committed a write"


@pytest.mark.parametrize(
    "policy",
    [p for p in E7_POLICY if p.category in (*MEMBERSHIP_ONLY, MEMBER_HANDLER)],
    ids=lambda p: f"{p.method} {p.path}",
)
def test_membership_row(world: World, policy: Policy):
    before = world.file_digest()
    observed = {who: _observe(world, policy, who) for who in (*ALL_ROLES, "outsider")}
    expected = {role: _expected_allowed(policy) for role in ALL_ROLES}
    expected["outsider"] = (
        403,
        "permission_denied",
        LEGACY_NOT_A_MEMBER if policy.category == MEMBER_HANDLER else NOT_A_MEMBER,
        None,
    )
    assert observed == expected
    assert world.file_digest() == before, "a probe committed a write"


def test_p6_auth_3_audit_route_case(world: World):
    """Named explicitly because E7 names it: exact OWNER/ADMIN/COMPLIANCE_REVIEWER."""
    url = f"{API}/workspaces/{world.workspace_id}/audit-logs"
    observed = {role: world.send("GET", url, role).status_code for role in ALL_ROLES}
    assert observed == {
        OWNER: 200,
        ADMIN: 200,
        COMPLIANCE: 200,
        MARKETER: 403,
        REVIEWER: 403,
        VIEWER: 403,
    }


def test_the_probes_run_with_the_capabilities_dark(world: World):
    """The 503 rows rely on both flags being off; pinned so a flag flip is visible here."""
    settings = get_settings()
    assert settings.scout_scheduling_enabled is False
    assert settings.opportunity_feedback_enabled is False


def test_a_denied_role_sending_the_allowed_probe_is_refused_before_the_body(world: World):
    """The same bytes: the only difference between 422 and 403 is the caller's role."""
    url = f"{API}/organizations/{world.organization_id}/invitations"
    admin = world.send("POST", url, ADMIN, body=[])
    viewer = world.send("POST", url, VIEWER, body=[])
    assert (admin.status_code, viewer.status_code) == (422, 403)
    assert viewer.json()["error"]["message"] == ROLE_DENIAL.format(role=VIEWER)
