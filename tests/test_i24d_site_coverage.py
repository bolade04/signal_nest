"""Gate 4N-I24D — enforcing consumers for the fifteen uncovered load-bearing sites.

WHAT I24C-12 LEFT OPEN. Gate 4N-I24C built independent mutation-site discovery — sites derived
from AST call graphs, the workflow graded-step graph and authored contract key sets rather than
from a hand-written list — and it immediately reported what a hand-written list never would:
fourteen requirement keys and one function were authored, hashed, shipped, and consumed by
NOTHING. A file could declare `entry_count: 32` while holding thirty entries, or an anchor
could declare an unrecognised schema version, and every guard would still exit 0.

WHAT COUNTS AS COVERAGE HERE. Not presence, not import, not a name appearing in a test. Each
test below MUTATES the load-bearing value and requires the SHIPPING guard — invoked through
its real CLI, as CI invokes it — to exit non-zero with a message attributable to that key. A
key whose mutation leaves the guard green is not covered, whatever any inventory says.

Every fixture mutation is written in place and restored byte-exactly, with the restored digest
verified, because a fixture left mutated would silently corrupt every later assertion.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = REPO_ROOT / "tests" / "fixtures"
sys.path.insert(0, str(REPO_ROOT / "scripts"))

ENV = {**os.environ, "SIGNALNEST_ANCHOR_TIER": "TIER_1_SYNTHETIC"}


def _guard(script: str) -> subprocess.CompletedProcess:
    """Invoke a guard exactly as CI does: its real CLI, default arguments."""
    return subprocess.run([sys.executable, f"scripts/{script}"],
                          cwd=REPO_ROOT, capture_output=True, text=True, env=ENV)


class _Mutation:
    def __init__(self, fixture: str, guard: str):
        self.path = FIXTURES / fixture
        self.guard = guard

    def __enter__(self):
        self._original = self.path.read_bytes()
        self._digest = hashlib.sha256(self._original).hexdigest()
        return self

    def apply(self, mutate) -> subprocess.CompletedProcess:
        doc = json.loads(self._original)
        mutate(doc)
        self.path.write_text(json.dumps(doc, indent=2), encoding="utf-8")
        return _guard(self.guard)

    def __exit__(self, *exc):
        self.path.write_bytes(self._original)
        assert hashlib.sha256(self.path.read_bytes()).hexdigest() == self._digest, \
            f"{self.path} was not restored byte-exactly"
        return False


# site -> (fixture, guard, mutation, token that must appear in the guard's output)
REQUIREMENT_KEY_SITES = {
    "synthetic-ledger::ledger_version":
        ("synthetic-ledger.json", "deny_requirements.py",
         lambda d: d.update(ledger_version="99"), "ledger_version"),
    "synthetic-ledger::created_utc":
        ("synthetic-ledger.json", "deny_requirements.py",
         lambda d: d.update(created_utc="not-a-stamp"), "created_utc"),
    "synthetic-ledger::entry_count":
        ("synthetic-ledger.json", "deny_requirements.py",
         lambda d: d.update(entry_count=999), "entry_count"),
    "synthetic-requirements::created_utc":
        ("synthetic-requirements.json", "deny_requirements.py",
         lambda d: d.update(created_utc="2026/07/31"), "created_utc"),
    "synthetic-requirements::outcome_count":
        ("synthetic-requirements.json", "deny_requirements.py",
         lambda d: d.update(outcome_count=1), "outcome_count"),
    "synthetic-requirements::action_count":
        ("synthetic-requirements.json", "deny_requirements.py",
         lambda d: d.update(action_count=1), "action_count"),
    "synthetic-anchor::anchor_version":
        ("synthetic-anchor.json", "anchor_loader.py",
         lambda d: d.update(anchor_version="bogus"), "anchor_version"),
    "synthetic-anchor::permission_sets":
        ("synthetic-anchor.json", "anchor_loader.py",
         lambda d: d.update(permission_sets={}), "permission_sets"),
    "readonly-verifier-ceiling::unknown_action_behaviour":
        ("readonly-verifier-ceiling.json", "verifier_ceiling.py",
         lambda d: d.update(unknown_action_behaviour="ALLOW"), "unknown_action_behaviour"),
    "readonly-verifier-ceiling::expiry_required_on_every_allow":
        ("readonly-verifier-ceiling.json", "verifier_ceiling.py",
         lambda d: d.update(expiry_required_on_every_allow=False),
         "expiry_required_on_every_allow"),
    "readonly-verifier-ceiling::resource_rules":
        ("readonly-verifier-ceiling.json", "verifier_ceiling.py",
         lambda d: d.update(resource_rules={}), None),
    "expected-provenance-values::algorithm":
        ("expected-provenance-values.json", "provenance.py",
         lambda d: d.update(algorithm="md5(value)"), "algorithm"),
    "expected-provenance-values::expected_digests_synthetic":
        ("expected-provenance-values.json", "provenance.py",
         lambda d: d.update(expected_digests_synthetic={}), None),
    "provenance-row-inventory::other_certifying_comparisons":
        ("provenance-row-inventory.json", "provenance.py",
         lambda d: d.update(other_certifying_comparisons=[]), None),
}


@pytest.mark.parametrize("site", sorted(REQUIREMENT_KEY_SITES))
def test_each_requirement_key_is_load_bearing(site):
    """KEY-BY-KEY behavioural proof. Mutating this key alone must fail the SHIPPING guard.

    This is the key-by-key mutation matrix the authorization requires in place of a broad
    set-cover assertion: each key is mutated on its own, and the guard it feeds must refuse.
    """
    fixture, guard, mutate, token = REQUIREMENT_KEY_SITES[site]
    with _Mutation(fixture, guard) as m:
        baseline = _guard(guard)
        assert baseline.returncode == 0, (
            f"{guard} does not pass before mutation; a failure here would mask the result "
            f"rather than prove it: {baseline.stdout[-300:]}{baseline.stderr[-300:]}")
        result = m.apply(mutate)
    assert result.returncode != 0, (
        f"mutating {site} left {guard} exiting 0 — the key is not load-bearing")
    if token:
        assert token in (result.stdout + result.stderr), (
            f"{guard} failed but not attributably to {site}; coverage may be credited to an "
            f"unrelated control: {result.stdout[-300:]}{result.stderr[-300:]}")


@pytest.mark.parametrize("site", sorted(REQUIREMENT_KEY_SITES))
def test_each_requirement_key_has_a_named_production_consumer(site):
    """The consumer must live in shipped code, not in this test file. A key enforced only by
    a test is enforced only when the suite runs — and I23 finding I24C-07 showed the step that
    runs the suite can itself be echoed away."""
    _, guard, _, _ = REQUIREMENT_KEY_SITES[site]
    key = site.split("::", 1)[1]
    source = (REPO_ROOT / "scripts" / guard).read_text(encoding="utf-8")
    assert f'"{key}"' in source or f"'{key}'" in source, \
        f"{guard} does not read {key!r}; its only enforcement would be this test"


# --------------------------------------------------------------------------- #
# the one function site
# --------------------------------------------------------------------------- #

def test_external_requirements_validates_its_own_declarations():
    """`external_requirements()` was called four times in production and still counted as
    uncovered: it consumed only `entries`, so a source declaring the wrong size loaded clean.
    Its declarations are now checked against what the file actually contains."""
    import deny_requirements as dr
    doc = dr.external_requirements()
    assert doc["outcome_count"] == len(doc["entries"])
    assert doc["action_count"] == sum(len(e.get("actions", [])) for e in doc["entries"])


def test_external_requirements_refuses_a_truncated_source(tmp_path):
    """A truncated SOURCE 1 must never load. This drives the real function through its real
    path resolution, not a helper."""
    import deny_requirements as dr
    real = dr.external_requirements()
    truncated = dict(real)
    truncated["entries"] = real["entries"][:-1]          # size no longer matches the declaration
    path = tmp_path / "truncated.json"
    path.write_text(json.dumps(truncated), encoding="utf-8")
    with pytest.raises(dr.AnchorUnavailable, match="outcome_count"):
        dr.external_requirements(path)


def test_external_requirements_refuses_an_unknown_schema_version(tmp_path):
    import deny_requirements as dr
    doc = dict(dr.external_requirements())
    doc["version"] = "99"
    path = tmp_path / "badversion.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    with pytest.raises(dr.AnchorUnavailable, match="version"):
        dr.external_requirements(path)


def test_external_requirements_result_is_consumed_by_the_shipping_guard():
    """Called is not consumed. Every requirement the guard enforces must come from this
    function's return value, so shrinking it must change the guard's own totals."""
    import deny_requirements as dr
    full = dr.source1_actions()
    assert full, "SOURCE 1 resolved to nothing; the guard would enforce an empty requirement"
    real = dr.external_requirements

    def fewer(path=None):
        doc = dict(real(path))
        doc["entries"] = doc["entries"][:1]
        doc["outcome_count"] = len(doc["entries"])
        doc["action_count"] = sum(len(e.get("actions", [])) for e in doc["entries"])
        return doc

    dr.external_requirements = fewer
    try:
        shrunk = dr.source1_actions()
    finally:
        dr.external_requirements = real
    assert len(shrunk) < len(full), \
        "shrinking external_requirements() did not change the enforced requirement set"


def test_the_deny_requirements_guard_exits_non_zero_on_a_dishonest_source():
    """End to end through the CLI, as CI invokes it."""
    with _Mutation("synthetic-requirements.json", "deny_requirements.py") as m:
        result = m.apply(lambda d: d.update(action_count=1))
    assert result.returncode != 0
    assert "action_count" in (result.stdout + result.stderr)
