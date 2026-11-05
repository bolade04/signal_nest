"""P6-PLAT-5: the OpenAPI document declares the bearer scheme the API enforces.

* **Declared.** ``components.securitySchemes`` holds exactly one scheme, ``bearerAuth``
  (HTTP ``bearer``, format ``JWT``).
* **Exactly the authenticating operations.** An operation carries the requirement
  ``[{"bearerAuth": []}]`` if and only if its dependency tree contains
  ``get_authenticated_session``. Both sides are derived independently: one from the mounted
  routes' dependants, the other from the generated document. The committed
  ``apps/api/openapi.json`` matches the document (the API-contract CI job enforces that).
* **Documentation only.** The scheme is declared with ``auto_error=False`` and its value is not
  read, so authentication answers exactly as before: a missing, non-bearer or empty bearer
  header is the dependency's own 401 "Missing bearer token.", a lowercase ``bearer`` scheme is
  still accepted, and a malformed token is still "Invalid or expired token.".
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.routing import iter_route_contexts

from app.auth import dependencies as deps
from app.main import app
from app.tests._auth2_support import ALICE, ME, Env, active_rate_limiter, bearer, environment, error

SCHEME = "bearerAuth"
METHODS = ("get", "put", "post", "delete", "options", "head", "patch", "trace")
MISSING_BEARER = (401, "unauthorized", "Missing bearer token.")
UNAUTHORIZED = (401, "unauthorized", "Invalid or expired token.")


def _dependency_calls(dependant) -> list:
    seen: set[int] = set()
    out: list = []

    def walk(d) -> None:
        if d.call is not None and id(d.call) not in seen:
            seen.add(id(d.call))
            out.append(d.call)
        for sub in d.dependencies:
            walk(sub)

    walk(dependant)
    return out


def _authenticating_operations() -> set[tuple[str, str]]:
    """(method, path) of every mounted operation whose dependants reach the session check."""
    out = set()
    for rc in iter_route_contexts(app.routes):
        dependant = getattr(rc, "dependant", None)
        if dependant is None or not getattr(rc, "include_in_schema", True):
            continue
        if deps.get_authenticated_session in _dependency_calls(dependant):
            for method in set(rc.methods) - {"HEAD", "OPTIONS"}:
                out.add((method.lower(), rc.path))
    return out


def _document_operations() -> dict[tuple[str, str], dict]:
    document = app.openapi()
    return {
        (method, path): operation
        for path, item in document["paths"].items()
        for method, operation in item.items()
        if method in METHODS
    }


def test_exactly_one_bearer_scheme_is_declared():
    schemes = app.openapi()["components"]["securitySchemes"]
    assert list(schemes) == [SCHEME]
    scheme = schemes[SCHEME]
    assert (scheme["type"], scheme["scheme"], scheme["bearerFormat"]) == ("http", "bearer", "JWT")


def test_requirement_marks_exactly_the_authenticating_operations():
    operations = _document_operations()
    authenticating = _authenticating_operations()
    assert authenticating, "no authenticating operation was found: the walk is broken"
    assert authenticating <= set(operations)
    marked = {key for key, op in operations.items() if "security" in op}
    assert marked == authenticating
    for key in marked:
        assert operations[key]["security"] == [{SCHEME: []}], key
    public = set(operations) - authenticating
    assert public, "no public operation was found: login and health must stay public"


def test_login_and_health_declare_no_requirement():
    operations = _document_operations()
    for key in (("post", "/api/v1/auth/login"), ("get", "/health")):
        assert key in operations, key
        assert "security" not in operations[key], key


def test_the_declared_scheme_cannot_refuse_a_request():
    assert deps.BEARER_SCHEME.auto_error is False


@pytest.fixture
def env(tmp_path) -> Iterator[Env]:
    with environment(tmp_path / "plat5.db") as e:
        yield e


def _me(env: Env, headers: dict[str, str]):
    active_rate_limiter()._hits.clear()
    return env.client.get(ME, headers=headers)


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": ""},
        {"Authorization": "Bearer"},
        {"Authorization": "Basic not-a-credential"},
        {"Authorization": "Token abc.def.ghi"},
    ],
    ids=["absent", "empty", "scheme-only", "basic", "other-scheme"],
)
def test_missing_or_non_bearer_header_is_the_dependencys_own_401(env, headers):
    assert error(_me(env, headers)) == MISSING_BEARER


def test_malformed_bearer_token_is_still_invalid_or_expired(env):
    assert error(_me(env, {"Authorization": "Bearer not-a-token"})) == UNAUTHORIZED


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER"])
def test_a_live_token_is_accepted_under_any_case_of_the_scheme(env, scheme):
    token = bearer(env, ALICE)
    r = _me(env, {"Authorization": f"{scheme} {token}"})
    assert r.status_code == 200, r.text
    assert r.json()["user"]["id"] == ALICE
