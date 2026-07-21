"""Type-foundation tests for the governed capability override service (Phase 4A-C.3.1).

This sub-batch ships only the *types*: the capabilities-local typed errors
(:mod:`app.capabilities.errors`) and the immutable result models
(:mod:`app.capabilities.results`). No service constructs or raises them yet, so
these tests exercise shape/immutability and error status/code mapping in isolation
(mirroring the resolver's result-shape tests).

They also stand guard over the resolver's live-gate import boundary: after Phase 4B-A
exactly one live gate (the opportunity-feedback route) is a sanctioned resolver consumer,
while the scheduling and RSS live gates must remain unwired.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from app.capabilities.errors import (
    CapabilityOverrideNotPermittedError,
    CapabilityTenantMismatchError,
)
from app.capabilities.registry import Capability
from app.capabilities.results import OverrideMutation, OverridePage
from app.core.errors import NotFoundError, SignalNestError, ValidationDomainError

# apps/api (holds the app package) — three levels up from this test file.
API_DIR = Path(__file__).resolve().parents[2]


# --------------------------------------------------------------------------- #
# Typed errors — taxonomy, status, and code mapping (§8.15, §8.27)
# --------------------------------------------------------------------------- #
def test_not_permitted_error_is_a_validation_domain_error() -> None:
    exc = CapabilityOverrideNotPermittedError("RSS cannot be enabled per workspace.")
    assert isinstance(exc, ValidationDomainError)
    assert isinstance(exc, SignalNestError)


def test_not_permitted_error_maps_to_422_with_stable_code() -> None:
    exc = CapabilityOverrideNotPermittedError("nope")
    assert exc.status_code == 422
    assert exc.code == "capability_override_not_permitted"
    assert exc.message == "nope"


def test_tenant_mismatch_error_is_a_not_found_error() -> None:
    exc = CapabilityTenantMismatchError("workspace not found")
    assert isinstance(exc, NotFoundError)
    assert isinstance(exc, SignalNestError)


def test_tenant_mismatch_is_envelope_indistinguishable_from_absent_workspace() -> None:
    # Non-enumeration (§8.27): the tenant-mismatch error must present the *same*
    # 404 status and the *same* generic ``not_found`` code as a genuinely absent
    # workspace, so a caller cannot tell "exists but not yours" from "does not
    # exist". It deliberately does not define its own code.
    mismatch = CapabilityTenantMismatchError("not found")
    absent = NotFoundError("not found")
    assert mismatch.status_code == absent.status_code == 404
    assert mismatch.code == absent.code == "not_found"


def test_capability_errors_can_be_raised_and_caught_as_base() -> None:
    with pytest.raises(SignalNestError):
        raise CapabilityOverrideNotPermittedError("x")
    with pytest.raises(SignalNestError):
        raise CapabilityTenantMismatchError("y")


# --------------------------------------------------------------------------- #
# OverrideMutation — frozen, slotted, exact documented fields (§8.14)
# --------------------------------------------------------------------------- #
def test_override_mutation_fields_are_exactly_the_documented_set() -> None:
    fields = {f.name for f in dataclasses.fields(OverrideMutation)}
    assert fields == {
        "capability",
        "workspace_id",
        "created",
        "changed",
        "enabled",
        "override_id",
    }


def test_override_mutation_is_frozen_and_slotted() -> None:
    result = OverrideMutation(
        capability=Capability.OPPORTUNITY_FEEDBACK,
        workspace_id="ws-1",
        created=True,
        changed=True,
        enabled=True,
        override_id="ovr-1",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        result.changed = False  # type: ignore[misc]
    # slots=True → no per-instance __dict__.
    assert not hasattr(result, "__dict__")


def test_override_mutation_allows_cleared_shape() -> None:
    # After a clear, no override remains: enabled and override_id are None.
    cleared = OverrideMutation(
        capability=Capability.SCOUT_SCHEDULING,
        workspace_id="ws-2",
        created=False,
        changed=True,
        enabled=None,
        override_id=None,
    )
    assert cleared.enabled is None
    assert cleared.override_id is None


# --------------------------------------------------------------------------- #
# OverridePage — frozen, slotted, exact documented fields (§8.14)
# --------------------------------------------------------------------------- #
def test_override_page_fields_are_exactly_the_documented_set() -> None:
    fields = {f.name for f in dataclasses.fields(OverridePage)}
    assert fields == {"items", "total", "limit", "offset"}


def test_override_page_is_frozen_and_slotted() -> None:
    page = OverridePage(items=(), total=0, limit=20, offset=0)
    with pytest.raises(dataclasses.FrozenInstanceError):
        page.total = 5  # type: ignore[misc]
    assert not hasattr(page, "__dict__")


def test_override_page_empty_defaults_are_coherent() -> None:
    page = OverridePage(items=(), total=0, limit=20, offset=0)
    assert page.items == ()
    assert page.total == 0
    assert page.limit == 20
    assert page.offset == 0


# --------------------------------------------------------------------------- #
# Import-boundary guard: only the sanctioned feedback gate consumes the resolver
# --------------------------------------------------------------------------- #
def test_resolver_consumed_only_by_the_sanctioned_feedback_gate() -> None:
    # Phase 4B-A sanctions exactly ONE live gate — the opportunity-feedback route — as a
    # resolver consumer, and it MUST route its capability decision through the central
    # deny-biased resolver (no raw-flag shortcut). Every other live gate must remain
    # unwired so no capability can silently activate through it: the scheduling
    # read/write gates and the RSS connector selection still must not import the
    # resolver. (The sanctioned operator surface consumes the resolver too, but it is
    # not a live customer gate and is out of scope for this guard.)
    feedback_gate = (API_DIR / "app/feedback/routes.py").read_text(encoding="utf-8")
    assert "resolve_capability" in feedback_gate  # sanctioned live consumer (4B-A)

    for module_path in (
        Path("app/scouting_requests/routes.py"),
        Path("app/scouting_requests/schedules.py"),
        Path("app/connectors/registry.py"),
    ):
        source = (API_DIR / module_path).read_text(encoding="utf-8")
        assert "capabilities.resolver" not in source, module_path
        assert "resolve_capability" not in source, module_path
