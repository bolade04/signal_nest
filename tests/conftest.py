"""Test-only deny-mutation hook (Gate 4N-I7, Defect 3 support).

Deny-shadow testing needs to remove a single action from every Deny statement in the
generated policies and then observe which suites notice. The mutation must NOT live in the
generators — production code with a "make me insecure" switch is worse than the defect it
tests. So the hook lives here, in test-only code, and activates only when
SIGNALNEST_DENY_MUTATION is set, which nothing but tests/test_deny_shadow.py ever does.

The mutation is applied at collection time, before any test module computes its policy
constants at import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

MUTATION_ENV = "SIGNALNEST_DENY_MUTATION"


def _strip_action_from_denies(doc: dict, action: str) -> dict:
    """Remove `action` from every Deny statement, dropping statements left empty.

    Case-insensitive, because IAM action matching is.
    """
    target = action.lower()
    statements = []
    for statement in doc["Statement"]:
        if statement.get("Effect") != "Deny":
            statements.append(statement)
            continue
        raw = statement.get("Action", [])
        actions = raw if isinstance(raw, list) else [raw]
        kept = [a for a in actions if a.lower() != target]
        if not kept:
            continue  # the whole statement existed only for this action
        statements.append({**statement, "Action": kept if len(kept) > 1 else kept[0]})
    return {**doc, "Statement": statements}


def pytest_configure(config):
    # Registered here rather than in a second pytest_configure: a second definition SHADOWS
    # the first, which would have silently disabled the deny-mutation hook below.
    config.addinivalue_line("markers", "tier2: needs the real protected anchor")

    action = os.environ.get(MUTATION_ENV)
    if not action:
        return

    import gen_boundary_policy as gb
    import gen_bootstrap_operator_policy as boot
    import gen_operator_policies as gen

    for module, attr in (
        (gb, "boundary_policy"),
        (gen, "permanent_w0_policy"),
        (gen, "bootstrap_temp_policy"),
        (boot, "bootstrap_operator_policy"),
    ):
        original = getattr(module, attr)

        def mutated(*args, _original=original, **kwargs):
            return _strip_action_from_denies(_original(*args, **kwargs), action)

        setattr(module, attr, mutated)

    config.stash.setdefault("signalnest_deny_mutation", action)


# --- Gate 4N-I13: explicit anchor tiers in tests -------------------------------------------
#
# There is no implicit anchor resolution anywhere any more. A test that needs the REAL anchor
# declares TIER_2_PROTECTED through this fixture, which sets the environment for that test
# only. Ordinary tests run under TIER_1_SYNTHETIC. Nothing reads a developer home directory
# by accident, which is what made the Gate 4N-I10 "clean checkout" evidence worthless.

import hashlib as _hashlib  # noqa: E402
import os as _os  # noqa: E402

import pytest as _pytest  # noqa: E402

REAL_ANCHOR = Path.home() / ".signalnest" / "anchor" / "signalnest-account-environment-anchor.json"


@_pytest.fixture(autouse=True)
def _declared_anchor_tier(request, monkeypatch):
    """Every test runs under a DECLARED tier. Tier 2 only where the test asks for it."""
    wants_real = request.node.get_closest_marker("tier2") is not None or \
        "tier2" in getattr(request.node, "fixturenames", ())
    if wants_real and REAL_ANCHOR.exists():
        monkeypatch.setenv("SIGNALNEST_ANCHOR_TIER", "TIER_2_PROTECTED")
        monkeypatch.setenv("SIGNALNEST_ANCHOR_PATH", str(REAL_ANCHOR))
        monkeypatch.setenv("SIGNALNEST_ANCHOR_SHA256",
                           _hashlib.sha256(REAL_ANCHOR.read_bytes()).hexdigest())
    elif not _os.environ.get("SIGNALNEST_ANCHOR_TIER"):
        monkeypatch.setenv("SIGNALNEST_ANCHOR_TIER", "TIER_1_SYNTHETIC")
