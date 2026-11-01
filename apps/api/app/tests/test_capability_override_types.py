"""Type-foundation tests for the governed capability override service (Phase 4A-C.3.1).

This sub-batch ships only the *types*: the capabilities-local typed errors
(:mod:`app.capabilities.errors`) and the immutable result models
(:mod:`app.capabilities.results`). No service constructs or raises them yet, so
these tests exercise shape/immutability and error status/code mapping in isolation
(mirroring the resolver's result-shape tests).

They also stand guard over the live gates' decision source: since P6-CAP-1 every live
gate (feedback, scheduling, RSS) decides through the resolver, and no production module
other than the raw-global reflection reads a capability flag directly.
"""

from __future__ import annotations

import ast
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
# Decision-source guard: every live gate decides through the resolver (P6-CAP-1)
# --------------------------------------------------------------------------- #
#: The module holding each capability's single decision call site.
_DECISION_SITES = {
    "opportunity_feedback": Path("app/feedback/routes.py"),  # Phase 4B-A
    "scout_scheduling": Path("app/scouting_requests/schedules.py"),  # P6-CAP-1
    "connector_rss": Path("app/scouting_requests/connectors.py"),  # P6-CAP-1
    "recommendation_briefs": Path("app/briefs/routes.py"),  # Project Phase 5A
}
#: The raw global flags. Reading one directly is how a gate bypasses the resolver.
_RAW_FLAGS = frozenset(
    {
        "opportunity_feedback_enabled",
        "scout_scheduling_enabled",
        "connector_rss_enabled",
        # Project Phase 5 CF-1 reserved it; the 5A gate (app/briefs/routes.py) decides
        # through the resolver, and this guard forbids it the raw flag.
        "recommendation_briefs_enabled",
    }
)
#: The only production module allowed to read them: the deliberately raw-global
#: ``FeatureFlagsOut`` reflection on ``GET /system/capabilities`` (P6-CAP-2).
_RAW_FLAG_READERS = frozenset({Path("app/system/routes.py")})


def test_every_live_gate_decides_through_the_resolver() -> None:
    # Before P6-CAP-1 this guard asserted the opposite for scheduling and RSS — that
    # they must NOT import the resolver — which pinned the defect the row describes.
    for capability, module_path in _DECISION_SITES.items():
        tree = ast.parse((API_DIR / module_path).read_text(encoding="utf-8"))
        calls = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "id", getattr(node.func, "attr", None))
            == "resolve_capability"
        ]
        assert calls, f"{capability}: {module_path} must decide via resolve_capability"


def test_no_gate_reads_a_raw_capability_flag() -> None:
    # An attribute read like ``get_settings().scout_scheduling_enabled`` anywhere in
    # production code would re-open the P6-CAP-1 bypass. Docstring/comment mentions
    # are not reads; only ``ast.Attribute`` nodes count.
    offenders = []
    for path in sorted((API_DIR / "app").rglob("*.py")):
        rel = path.relative_to(API_DIR)
        if "tests" in rel.parts or rel in _RAW_FLAG_READERS:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Attribute) and node.attr in _RAW_FLAGS:
                offenders.append(f"{rel}:{node.lineno} .{node.attr}")
    assert not offenders, offenders
    # The allow-listed reader really does read them (the entry is not vacuous).
    reflection = ast.parse((API_DIR / "app/system/routes.py").read_text(encoding="utf-8"))
    reads = {n.attr for n in ast.walk(reflection) if isinstance(n, ast.Attribute)}
    assert _RAW_FLAGS <= reads
