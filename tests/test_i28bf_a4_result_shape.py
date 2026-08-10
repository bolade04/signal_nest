"""Gate 4N-I28BF-A4 — the production reverify result-shape contract.

WHAT THIS BINDS. Section 15: the exact shape of what the PRODUCTION path
``signalnest_bootstrap.reverify()`` returns, so a caller cannot be fed a differently-shaped object
that hides a Docker failure. The real shape is FLATTENED, exactly as Gate 4N-I28BF-A observed and
as ``test_i28bfa`` records: ``reverify`` returns

    {"clean": bool, "problems": list[str], "layers": {name: bool, ...}}

Every layer — ``docker_per_site`` included — is a BOOLEAN inside ``layers``; there is no nested
per-layer dictionary in the return value. This module invokes the real reverify path to obtain a
genuine outcome, binds that shape with ``validate_reverify_shape``, and requires the ten shape
violations the gate enumerates to fail. The schema is versioned so a stale contract is refused.
"""

from __future__ import annotations

import copy
import sys
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import signalnest_bootstrap as boot                # noqa: E402

REVERIFY_RESULT_SCHEMA_VERSION = "i28bf-a4.1"

_REQUIRED_TOP_LEVEL = ("clean", "problems", "layers")
# Docker assurance layers that MUST survive in the flattened shape; their disappearance is the
# "Docker layer flattened away" attack.
_REQUIRED_DOCKER_LAYERS = ("docker_per_site", "docker_boundary")
_REQUIRED_CORE_LAYERS = ("executed_code", "executed_state")


def validate_reverify_shape(outcome, *, schema_version=REVERIFY_RESULT_SCHEMA_VERSION) -> list:
    """Return a list of shape problems for one reverify outcome. Empty means the shape holds."""
    problems = []
    if schema_version != REVERIFY_RESULT_SCHEMA_VERSION:
        problems.append(f"stale schema version {schema_version!r}; expected "
                        f"{REVERIFY_RESULT_SCHEMA_VERSION!r}")
        return problems
    if not isinstance(outcome, dict):
        problems.append(f"malformed return type {type(outcome).__name__}; a mapping is required")
        return problems
    extra = sorted(set(outcome) - set(_REQUIRED_TOP_LEVEL))
    if extra:
        problems.append(f"unknown result field(s) {extra}")
    for key in _REQUIRED_TOP_LEVEL:
        if key not in outcome:
            problems.append(f"required field {key!r} is missing (renamed or omitted)")
    if problems:
        return problems
    if not isinstance(outcome["clean"], bool):
        problems.append("'clean' must be a boolean")
    if not isinstance(outcome["problems"], list):
        problems.append("'problems' must be a list")
    if not isinstance(outcome["layers"], dict):
        problems.append("'layers' must be a mapping of layer name to boolean")
        return problems
    for name, value in outcome["layers"].items():
        if not isinstance(value, bool):
            problems.append(f"layer {name!r} is not a flattened boolean (got "
                            f"{type(value).__name__}); a nested layer object hides its verdict")
    for name in _REQUIRED_DOCKER_LAYERS + _REQUIRED_CORE_LAYERS:
        if name not in outcome["layers"]:
            problems.append(f"required layer {name!r} is absent (flattened away)")
    # consistency: clean is exactly the absence of problems, and any failing layer forces non-clean.
    if isinstance(outcome["clean"], bool) and isinstance(outcome["problems"], list):
        if outcome["clean"] != (not outcome["problems"]):
            problems.append("'clean' is not equal to (not problems); the aggregate disagrees with "
                            "the problem set")
    if outcome.get("clean") is True:
        false_layers = [n for n, v in outcome["layers"].items() if v is False]
        if false_layers:
            problems.append(f"'clean' is True while layer(s) {false_layers} are False; a failing "
                            "Docker/assurance layer cannot coexist with a clean aggregate")
    return problems


_CACHED_OUTCOME = None


def _real_outcome() -> dict:
    """A genuine outcome from the PRODUCTION reverify path, with a full session baseline bound.

    Computed once (establish + reverify is heavy) and returned as a deep copy so a mutating caller
    never poisons another test's copy.
    """
    global _CACHED_OUTCOME
    if _CACHED_OUTCOME is None:
        attestation = boot.establish(strict=False)
        config = types.SimpleNamespace()
        setattr(config, boot.BOOTSTRAP_ATTESTATION, attestation)
        _CACHED_OUTCOME = boot.reverify(config)
    return copy.deepcopy(_CACHED_OUTCOME)


# ===================================================================== the real shape holds
def test_the_real_production_reverify_shape_is_valid():
    outcome = _real_outcome()
    assert validate_reverify_shape(outcome) == [], validate_reverify_shape(outcome)


def test_the_real_shape_is_flattened_booleans_not_nested_dicts():
    """Pinned so a later refactor to nested layer objects is a visible, deliberate change."""
    outcome = _real_outcome()
    assert isinstance(outcome["layers"], dict)
    assert outcome["layers"], "there must be layers"
    assert all(isinstance(v, bool) for v in outcome["layers"].values()), (
        "every layer must be a flattened boolean, exactly as production returns")
    assert outcome["layers"]["docker_per_site"] is True


# ===================================================================== the ten shape violations
def _mutations():
    def boolean_to_dict(o):
        o["layers"]["docker_per_site"] = {"clean": True}

    def dict_to_boolean(o):
        o["layers"] = True

    def docker_per_site_omitted(o):
        o["layers"].pop("docker_per_site")

    def field_renamed(o):
        o["is_clean"] = o.pop("clean")

    def docker_layer_flattened_away(o):
        for name in ("docker_per_site", "docker_boundary"):
            o["layers"].pop(name, None)

    def docker_false_but_clean_true(o):
        o["clean"] = True
        o["problems"] = []
        o["layers"]["docker_per_site"] = False

    def aggregate_false_but_no_problems(o):
        o["clean"] = False
        o["problems"] = []

    def unknown_result_field(o):
        o["surprise"] = 1

    def malformed_return_type(o):
        return ["not", "a", "dict"]

    def stale_schema(o):
        return o                          # object is fine; the STALE VERSION is the violation

    return [
        ("boolean replaced by dictionary", boolean_to_dict, {}),
        ("dictionary replaced by boolean", dict_to_boolean, {}),
        ("docker_per_site omitted", docker_per_site_omitted, {}),
        ("field renamed", field_renamed, {}),
        ("docker layer flattened away", docker_layer_flattened_away, {}),
        ("docker layer false while aggregate true", docker_false_but_clean_true, {}),
        ("aggregate false while final success true", aggregate_false_but_no_problems, {}),
        ("unknown result field", unknown_result_field, {}),
        ("malformed return type", malformed_return_type, {}),
        ("stale schema version", stale_schema, {"schema_version": "i28bf-a3.0"}),
    ]


@pytest.mark.parametrize("label,mutate,kwargs", _mutations(), ids=[m[0] for m in _mutations()])
def test_each_shape_violation_is_refused(label, mutate, kwargs):
    outcome = _real_outcome()
    replaced = mutate(outcome)
    target = replaced if replaced is not None else outcome
    problems = validate_reverify_shape(target, **kwargs)
    assert problems, f"{label} must be refused by the result-shape contract"


def test_the_schema_version_is_bound_and_a_stale_one_is_refused():
    outcome = _real_outcome()
    assert validate_reverify_shape(outcome, schema_version=REVERIFY_RESULT_SCHEMA_VERSION) == []
    assert validate_reverify_shape(outcome, schema_version="i28bf-a3.0"), (
        "a stale schema version must be refused so the contract cannot silently pre-date the shape")
