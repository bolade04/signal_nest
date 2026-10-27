"""Shared test fixtures.

The durable-job handler registry (:mod:`app.jobs.registry`) is module-global
state. Test modules register their own synthetic handlers at *import* time, and
pytest imports every collected module before the first test runs — so without
isolation, one module's handlers are visible to every other module for the whole
session, and a test asserting on registry contents becomes order-dependent.

Worse for this repository's history: that leakage is what let a worker with an
empty registry look healthy under test. The autouse fixture below restores the
registry after each test so a test can mutate it (e.g. to prove the worker
refuses to start without handlers) without silently repairing — or breaking —
anything that runs after it.
"""

from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from app.tests import _offline_provider as _provider

# Must run before the application modules below are imported: they build llm_service from
# the environment at import time (FU-237-F1 part 2).
_provider.scrub_environment()

from app.jobs import registry as _registry  # noqa: E402


@pytest.fixture(autouse=True)
def _preserve_job_registry() -> Iterator[None]:
    """Snapshot and restore the handler + terminal-failure-hook registries.

    Shallow copies are enough: the values are plain functions, and a test that
    needs to change one replaces the binding rather than mutating the callable.
    Restoring (rather than clearing) keeps the real application registrations
    that import side effects legitimately established — the fixture must not
    destroy them.
    """
    handlers = dict(_registry._HANDLERS)
    hooks = dict(_registry._TERMINAL_FAILURE_HOOKS)
    try:
        yield
    finally:
        _registry._HANDLERS.clear()
        _registry._HANDLERS.update(handlers)
        _registry._TERMINAL_FAILURE_HOOKS.clear()
        _registry._TERMINAL_FAILURE_HOOKS.update(hooks)


# --- Offline guard (FU-237-F1): no API test may reach a non-loopback address. -----------
from app.tests import _offline_guard as _guard  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        f"{_guard.MARKER}: allow non-loopback network in one test; refused unless "
        f"{_guard.ALLOW_ENV}=1 (and no test in the repository may carry it).",
    )
    config.addinivalue_line(
        "markers",
        f"{_provider.MARKER}: use the real LLM provider in one test; refused unless "
        f"{_provider.ALLOW_ENV}=1 (and no test in the repository may carry it).",
    )
    _provider.scrub_environment()
    _provider.verify_and_swap()
    _guard.install()  # before collection, so import-time connects are caught too


def pytest_unconfigure(config: pytest.Config) -> None:
    if _guard._installed:
        _guard.uninstall()


@pytest.hookimpl(wrapper=True)
def pytest_runtest_call(item: pytest.Item) -> Iterator[None]:
    """Check the guard right after the test body, before fixtures (monkeypatch) undo anything."""
    try:
        result = yield
    except BaseException:
        if _guard.permissive_replacements():
            _guard.restore_if_tampered()
        raise
    bad = _guard.permissive_replacements()
    if bad:
        _guard.restore_if_tampered()
        pytest.fail(
            f"offline guard: {', '.join(bad)} replaced by a non-refusing function during the test",
            pytrace=False,
        )
    return result


@pytest.fixture(scope="session", autouse=True)
def _offline_guard_session() -> Iterator[None]:
    _guard.install()
    _provider.verify_and_swap()
    yield


@pytest.fixture(autouse=True)
def _offline_guard_per_test(request: pytest.FixtureRequest) -> Iterator[None]:
    """Attribute refusals to the running test and fail it if one happened or the guard was removed.

    Declared in the root conftest and autouse, so it is set up before (and torn down after)
    every other fixture, including ``monkeypatch``: a test that replaced the socket methods
    has them restored by the time the integrity check runs.
    """
    allow = request.node.get_closest_marker(_guard.MARKER) is not None
    if allow and os.environ.get(_guard.ALLOW_ENV) != "1":
        pytest.fail(
            f"@pytest.mark.{_guard.MARKER} is forbidden unless {_guard.ALLOW_ENV}=1 is set",
            pytrace=False,
        )
    real_provider = request.node.get_closest_marker(_provider.MARKER) is not None
    if real_provider and os.environ.get(_provider.ALLOW_ENV) != "1":
        pytest.fail(
            f"@pytest.mark.{_provider.MARKER} is forbidden unless {_provider.ALLOW_ENV}=1 is set",
            pytrace=False,
        )
    _guard.install()
    _guard.take_violations()
    _guard.set_current_test(request.node.nodeid, allow=allow)
    window = _provider.RealProviderWindow() if real_provider else None
    if window:
        window.__enter__()
    try:
        yield
    finally:
        if window:
            window.__exit__(None, None, None)
        _guard.set_current_test(None)
        tampered = _guard.restore_if_tampered()
        violations = _guard.take_violations()
        if tampered:
            pytest.fail(
                "offline guard was replaced during the test and has been restored", pytrace=False
            )
        if violations:
            pytest.fail("\n".join(violations), pytrace=False)
