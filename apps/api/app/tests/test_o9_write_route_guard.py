"""O9 guard G (FU-267-F7): no served route that writes through ``get_db`` answers before its commit.

``get_db`` commits after its ``yield``, which FastAPI runs only after the response has been
sent. A route that writes must therefore commit before it answers. This guard walks every
served route of the application (the effective routes, as ``test_e7_role_policy_matrix.py``
does) and requires every non-GET route whose dependencies include ``get_db`` to be exactly
one of:

* **on ``WriteDb``**: a ``commit_before_response`` dependency with scope ``"function"``
  (:mod:`app.db.request_tx`);
* in :data:`EXPLICIT_COMMIT`: the route commits itself, at a position its contract fixes
  (each is checked for a ``.commit()`` call in the endpoint, or in a function of its module
  that the endpoint calls);
* in :data:`READ_ONLY`: a non-GET route that writes nothing;
* in :data:`PENDING`: not yet converted, with the reason. The list only shrinks: an entry
  whose route is on ``WriteDb`` fails the guard until it is removed.

Every listed route must be served and must use ``get_db``, so no entry outlives its route. A
new write route (including one an open PR adds) that uses plain ``Depends(get_db)`` fails here
until it declares ``db: Session = WriteDb`` or is listed with a reason.

GET routes are not checked: the O9 audit (``P6-W21-O9-01/AUDIT.md`` §2) read every GET
endpoint and the helpers it calls, and none writes.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from collections.abc import Iterator
from dataclasses import dataclass

from fastapi import Depends, FastAPI
from fastapi.routing import iter_route_contexts
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.request_tx import WriteDb, commit_before_response
from app.db.session import get_db
from app.main import app

API = get_settings().api_prefix
_READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Routes that commit explicitly before they answer (AUDIT §2.2). Their commit position is
#: contractual, so they keep it.
EXPLICIT_COMMIT: dict[tuple[str, str], str] = {
    ("POST", f"{API}/auth/register"): "_fresh_session: commit before the token is minted",
    ("POST", f"{API}/auth/login"): "_fresh_session: commit before the token is minted",
    ("POST", f"{API}/auth/logout"): "revocation committed before the 204",
    ("POST", f"{API}/auth/logout-all"): "revocation committed before the 204",
    ("POST", f"{API}/auth/invitations/register"): "_fresh_session",
    ("POST", f"{API}/auth/invitations/accept"): "acceptance committed before the answer",
    ("POST", f"{API}/auth/password-reset/request"): "committed before the mail task",
    ("POST", f"{API}/auth/password-reset/confirm"): "committed before the answer",
    ("POST", f"{API}/auth/password/change"): "committed before the answer",
    ("POST", f"{API}/auth/email-verification/request"): "committed before the mail task",
    ("POST", f"{API}/auth/email-verification/confirm"): "committed before the answer",
    ("POST", f"{API}/organizations/{{organization_id}}/invitations"): "token after the commit",
    (
        "DELETE",
        f"{API}/organizations/{{organization_id}}/invitations/{{invitation_id}}",
    ): "committed before the 204",
    (
        "PUT",
        f"{API}/organizations/{{organization_id}}/members/{{user_id}}/role",
    ): "committed before the answer",
    (
        "DELETE",
        f"{API}/organizations/{{organization_id}}/members/{{user_id}}",
    ): "committed before the 204",
    (
        "POST",
        f"{API}/internal/system/data-sources/{{source_key}}/takedown",
    ): "committed before the answer",
    (
        "POST",
        f"{API}/workspaces/{{workspace_id}}/jobs/{{job_id}}/cancel",
    ): "committed before the answer",
    ("POST", f"{API}/account/delete"): "committed before the answer",
    ("POST", f"{API}/organizations/{{organization_id}}/delete"): "committed before the answer",
    (
        "POST",
        f"{API}/workspaces/{{workspace_id}}/scout-requests/{{request_id}}/pause",
    ): "D-25: the pause commit is the admission cut-off",
    (
        "POST",
        f"{API}/workspaces/{{workspace_id}}/scout-requests/{{request_id}}/run",
    ): "D-25: the run commits its enqueue before answering",
    # Project Phase 5C (W21 integration, I-O9-1): both commit before they answer, by the
    # scout-run route's rule, so the job id (202) and the decision (201) are visible at once.
    (
        "POST",
        f"{API}/workspaces/{{workspace_id}}/briefs/{{brief_id}}/drafts",
    ): "5C: the generation job is committed before the 202",
    (
        "POST",
        f"{API}/workspaces/{{workspace_id}}/drafts/{{draft_id}}/approvals",
    ): "5C: the decision and its audit row are committed before the 201",
}

#: Non-GET routes that write nothing.
READ_ONLY: dict[tuple[str, str], str] = {
    ("POST", f"{API}/auth/invitations/preview"): "previewing does not spend the token",
    ("POST", f"{API}/geocode"): "geocoder cache only; get_db only through the tenant context",
}

_TIER_2 = "blocked on #292 (O9 Tier 2)"
_CTX = f"{API}/workspaces/{{workspace_id}}"

#: Not yet on ``WriteDb``. Shrinks only: remove an entry when its route is converted.
PENDING: dict[tuple[str, str], str] = {
    ("POST", f"{_CTX}/products"): _TIER_2,
    ("PATCH", f"{_CTX}/products/{{item_id}}"): _TIER_2,
    ("DELETE", f"{_CTX}/products/{{item_id}}"): _TIER_2,
    ("POST", f"{_CTX}/audiences"): _TIER_2,
    ("PATCH", f"{_CTX}/audiences/{{item_id}}"): _TIER_2,
    ("DELETE", f"{_CTX}/audiences/{{item_id}}"): _TIER_2,
    ("POST", f"{_CTX}/competitors"): _TIER_2,
    ("PATCH", f"{_CTX}/competitors/{{item_id}}"): _TIER_2,
    ("DELETE", f"{_CTX}/competitors/{{item_id}}"): _TIER_2,
    ("POST", f"{_CTX}/brand-voice"): _TIER_2,
    ("PATCH", f"{_CTX}/brand-voice/{{item_id}}"): _TIER_2,
    ("DELETE", f"{_CTX}/brand-voice/{{item_id}}"): _TIER_2,
    ("POST", f"{_CTX}/offers"): _TIER_2,
    ("PATCH", f"{_CTX}/offers/{{item_id}}"): _TIER_2,
    ("DELETE", f"{_CTX}/offers/{{item_id}}"): _TIER_2,
    ("POST", f"{_CTX}/claims"): _TIER_2,
    ("PATCH", f"{_CTX}/claims/{{item_id}}"): _TIER_2,
    ("DELETE", f"{_CTX}/claims/{{item_id}}"): _TIER_2,
    ("POST", f"{_CTX}/source-preferences"): _TIER_2,
    ("PATCH", f"{_CTX}/source-preferences/{{item_id}}"): _TIER_2,
    ("DELETE", f"{_CTX}/source-preferences/{{item_id}}"): _TIER_2,
    ("POST", f"{_CTX}/channel-preferences"): _TIER_2,
    ("PATCH", f"{_CTX}/channel-preferences/{{item_id}}"): _TIER_2,
    ("DELETE", f"{_CTX}/channel-preferences/{{item_id}}"): _TIER_2,
    ("POST", f"{_CTX}/campaigns"): _TIER_2,
    ("PATCH", f"{_CTX}/campaigns/{{item_id}}"): _TIER_2,
    ("DELETE", f"{_CTX}/campaigns/{{item_id}}"): _TIER_2,
}


# --------------------------------------------------------------------------- #
# Route introspection: the served (effective) routes, not the declared ones
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class ServedRoute:
    method: str
    path: str
    endpoint: object
    uses_get_db: bool
    #: ``commit_before_response`` with scope "function" is in the dependency tree.
    on_write_db: bool
    #: ``commit_before_response`` with any other scope is in the dependency tree.
    misscoped_helper: bool

    @property
    def key(self) -> tuple[str, str]:
        return (self.method, self.path)


def _dependants(root) -> Iterator:
    stack = [root]
    while stack:
        d = stack.pop()
        yield d
        stack.extend(d.dependencies)


def served_routes(application: FastAPI) -> list[ServedRoute]:
    out = []
    for rc in iter_route_contexts(application.routes):
        dependant = getattr(rc, "dependant", None)
        if dependant is None:
            continue
        deps = list(_dependants(dependant))
        helpers = [d for d in deps if d.call is commit_before_response]
        for method in sorted(set(rc.methods) - {"HEAD", "OPTIONS"}):
            out.append(
                ServedRoute(
                    method=method,
                    path=rc.path,
                    endpoint=dependant.call,
                    uses_get_db=any(d.call is get_db for d in deps),
                    on_write_db=any(d.scope == "function" for d in helpers),
                    misscoped_helper=any(d.scope != "function" for d in helpers),
                )
            )
    return out


def _calls_commit(fn) -> bool:
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    return any(
        isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "commit"
        for n in ast.walk(tree)
    )


def commits_explicitly(endpoint) -> bool:
    """A ``.commit()`` call in the endpoint, or in a function of its own module it calls."""
    if _calls_commit(endpoint):
        return True
    tree = ast.parse(textwrap.dedent(inspect.getsource(endpoint)))
    module_globals = getattr(endpoint, "__globals__", {})
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            callee = module_globals.get(node.func.id)
            if (
                inspect.isfunction(callee)
                and callee.__module__ == endpoint.__module__
                and _calls_commit(callee)
            ):
                return True
    return False


def violations(
    application: FastAPI,
    *,
    explicit: dict[tuple[str, str], str],
    read_only: dict[tuple[str, str], str],
    pending: dict[tuple[str, str], str],
) -> list[str]:
    """Every way the served routes disagree with the rule; empty when the guard holds."""
    found: list[str] = []
    routes = {r.key: r for r in served_routes(application)}
    listed = {"EXPLICIT_COMMIT": explicit, "READ_ONLY": read_only, "PENDING": pending}
    for route in routes.values():
        if route.misscoped_helper:
            found.append(f"{route.key}: commit_before_response without scope='function'")
        if route.method in _READ_METHODS or not route.uses_get_db:
            continue
        homes = [name for name, table in listed.items() if route.key in table]
        if route.on_write_db:
            homes.append("WriteDb")
        if not homes:
            found.append(
                f"{route.key}: writes through get_db but is not on WriteDb and is not listed "
                "(declare `db: Session = WriteDb`)"
            )
        elif len(homes) > 1:
            found.append(f"{route.key}: in more than one place: {homes}")
    for name, table in listed.items():
        for key in table:
            route = routes.get(key)
            if route is None:
                found.append(f"{name} {key}: no such served route")
            elif not route.uses_get_db or route.method in _READ_METHODS:
                found.append(f"{name} {key}: not a non-GET route on get_db")
    for key in explicit:
        route = routes.get(key)
        if route is not None and not commits_explicitly(route.endpoint):
            found.append(f"EXPLICIT_COMMIT {key}: no .commit() call found")
    return found


# --------------------------------------------------------------------------- #
# The guard
# --------------------------------------------------------------------------- #
def test_guard_g_every_served_write_route_commits_before_its_response():
    found = violations(app, explicit=EXPLICIT_COMMIT, read_only=READ_ONLY, pending=PENDING)
    assert found == [], "\n".join(found)


def test_pending_is_exactly_the_27_campaign_context_routes_blocked_on_292():
    assert len(PENDING) == 27
    assert set(PENDING.values()) == {"blocked on #292 (O9 Tier 2)"}
    routes = {r.key: r for r in served_routes(app)}
    modules = {routes[key].endpoint.__module__ for key in PENDING}
    assert modules == {"app.campaign_context.routes"}
    # Every campaign-context write route is pending: none is on WriteDb yet, none is missed.
    context_writes = {
        key
        for key, r in routes.items()
        if r.endpoint.__module__ == "app.campaign_context.routes"
        and r.method not in _READ_METHODS
    }
    assert context_writes == set(PENDING)


def test_the_listed_routes_match_the_audit_counts():
    """AUDIT §2: 21 explicit-commit routes and 2 read-only non-GET routes; the W21
    integration (I-O9-1) adds the two Project Phase 5C routes that commit before they answer:
    21 + 2 = 23."""
    assert len(EXPLICIT_COMMIT) == 23
    assert len(READ_ONLY) == 2


# --------------------------------------------------------------------------- #
# The guard can fail (on an application of its own)
# --------------------------------------------------------------------------- #
def _synthetic() -> FastAPI:
    application = FastAPI()

    @application.get("/read")
    def read(db: Session = Depends(get_db)) -> dict:
        return {}

    @application.post("/converted")
    def converted(db: Session = WriteDb) -> dict:
        return {}

    @application.post("/unlisted")
    def unlisted(db: Session = Depends(get_db)) -> dict:
        return {}

    @application.put("/request-scoped-helper")
    def request_scoped_helper(db: Session = Depends(commit_before_response)) -> dict:
        return {}

    @application.post("/explicit")
    def explicit(db: Session = Depends(get_db)) -> dict:
        db.commit()
        return {}

    @application.post("/claims-explicit")
    def claims_explicit(db: Session = Depends(get_db)) -> dict:
        return {}

    return application


def test_the_guard_flags_an_unlisted_write_route_and_a_misscoped_helper():
    found = violations(
        _synthetic(),
        explicit={("POST", "/explicit"): "x", ("POST", "/claims-explicit"): "x"},
        read_only={},
        pending={("POST", "/converted"): "stale", ("DELETE", "/gone"): "stale"},
    )
    assert sorted(found) == sorted(
        [
            "('POST', '/unlisted'): writes through get_db but is not on WriteDb and is not "
            "listed (declare `db: Session = WriteDb`)",
            "('PUT', '/request-scoped-helper'): commit_before_response without scope='function'",
            "('PUT', '/request-scoped-helper'): writes through get_db but is not on WriteDb and "
            "is not listed (declare `db: Session = WriteDb`)",
            "('POST', '/converted'): in more than one place: ['PENDING', 'WriteDb']",
            "PENDING ('DELETE', '/gone'): no such served route",
            "EXPLICIT_COMMIT ('POST', '/claims-explicit'): no .commit() call found",
        ]
    )
