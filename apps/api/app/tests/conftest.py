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

from collections.abc import Iterator

import pytest

from app.jobs import registry as _registry


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
