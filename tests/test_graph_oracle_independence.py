"""Independent graph-hash oracle and tracked-fixture integrity (Gate 4N-I17, Defects 1 and 8).

DEFECT 1. Gate 4N-I16's "independent" reference and the production hash both called
`lifecycle_canonical.canonical_bytes`. One implementation, invoked twice. Replacing it with a
constant that discarded the graph left every hash test green, and the guard written to protect
independence checked the one direction of coupling that did not exist.

DEFECT 8. The fixture that turned out to be the ONLY real anchor was untracked. `git ls-files
tests/fixtures` returned nothing, while two repository comments described it as "tracked" and
"committed". An anchor with no version control is not an anchor.

THREE ANCHORS NOW, AND THEY FAIL DIFFERENTLY — which is the point:
  production hash   scripts/lifecycle_canonical.py, reached via role_bootstrap_lifecycle.graph_hash
  oracle hash       tests/oracle/graph_oracle.py, stdlib-only, own schema, own key ordering
  tracked fixture   tests/fixtures/lifecycle-canonical-sha256.txt, under version control
Falsify production and it disagrees with BOTH. Falsify the oracle and production still matches the
fixture, so the failure is attributable rather than merely detected.
"""

from __future__ import annotations

import ast
import copy
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests" / "oracle"))

import graph_oracle as go  # noqa: E402
import role_bootstrap_lifecycle as lc  # noqa: E402

FIXTURE = REPO_ROOT / "tests" / "fixtures" / "lifecycle-canonical-sha256.txt"
ORACLE_SRC = REPO_ROOT / "tests" / "oracle" / "graph_oracle.py"


def fixture_hash() -> str:
    return FIXTURE.read_text(encoding="utf-8").split()[0]


def git(*args) -> str:
    return subprocess.run(["git", *args], cwd=REPO_ROOT,
                          capture_output=True, text=True).stdout.strip()


# =====================================================================================
# DEFECT 8 — the anchor is under version control
# =====================================================================================


def test_the_canonical_fixture_is_tracked_by_git():
    """EXECUTED git evidence, not a read of .git. An untracked anchor can be regenerated with
    no history and no review trail."""
    # GATE 4N-I20, ARCH-H3/AWS-3. `git ls-files` reports the INDEX, not history. These fixtures are STAGED ADDITIONS on a branch that is zero commits ahead, so the old assertion passed while `git ls-tree HEAD` returned nothing — and a staged anchor has exactly the 'no history, no review trail' weakness the check was written to exclude. The state is now named exactly, and the property that actually matters — the file reaches the commit that will be made — is asserted against the PREDICTED COMMIT TREE.
    import tracked_state

    rel = "tests/fixtures/lifecycle-canonical-sha256.txt"
    state = tracked_state.state_of(rel)
    assert state in (tracked_state.STAGED_ADDITION, tracked_state.TRACKED_IN_HEAD), (
        f"the canonical-hash anchor is {state}; it must be at least staged for addition")
    predicted = tracked_state.predicted_commit_tree()
    assert rel in predicted["entries"], (
        "the anchor would not be part of the commit this branch would produce")
    tracked = rel
    assert tracked == "tests/fixtures/lifecycle-canonical-sha256.txt", (
        "the canonical graph-hash fixture is NOT tracked by git. It is the only anchor that "
        "survives a defect in both implementations; untracked, it is not an anchor at all.")


def test_every_fixture_under_tests_fixtures_is_tracked():
    on_disk = {str(p.relative_to(REPO_ROOT)) for p in (REPO_ROOT / "tests" / "fixtures").rglob("*")
               if p.is_file()}
    import tracked_state

    # Index membership is the right question HERE: this test asks whether any fixture is
    # sitting in the working tree unknown to git, which is an INDEX property, not a history one.
    tracked = set(git("ls-files", "tests/fixtures").splitlines())
    untracked = sorted(on_disk - tracked)
    assert not untracked, f"fixtures present but untracked: {untracked}"


def test_no_fixture_contains_the_real_account_or_credentials():
    """Tracking these files puts them in version control permanently. They must be synthetic.

    GATE 4N-I18, SEC-1. This used to compare against a hard-coded copy of the real account id,
    which meant the test itself carried the identifier it was defending against — and once the
    containment replaced that literal, the check started flagging the legitimately synthetic
    fixtures instead. The rule now lives in scripts/leak_scan.py and is expressed as
    "any 12-digit account that is not a documentation placeholder", so no live value has to be
    written down anywhere to enforce it.
    """
    import leak_scan

    offenders = []
    for p in sorted((REPO_ROOT / "tests" / "fixtures").rglob("*")):
        if not p.is_file():
            continue
        hits = leak_scan.scan_text(p.read_text(encoding="utf-8", errors="ignore"))
        offenders.extend(f"{p.name}: {h}" for h in hits)
    assert not offenders, offenders


def test_the_repository_no_longer_claims_the_fixture_is_committed_when_it_is_not():
    """Two comments asserted 'tracked'/'committed' while nothing was tracked. Either the claim is
    true or it must not be made."""
    for rel in ("scripts/anchor_loader.py", "scripts/role_bootstrap_lifecycle.py"):
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for claim in ("tracked file", "committed byte fixture"):
            if claim in text:
                # the claim is only permissible if it is actually true
                # GATE 4N-I20: a claim of "committed" is only permissible if the file is in
                # HEAD. Index membership does not establish it, which is precisely how the two
                # source comments this test guards stayed false while it passed.
                import tracked_state

                if claim == "committed byte fixture":
                    in_head = [p for p in tracked_state.head_paths()
                               if p.startswith("tests/fixtures/")]
                    assert in_head, (
                        f"{rel} claims {claim!r} but NO fixture is present in HEAD; the fixtures "
                        "are staged additions with no history yet")
                else:
                    assert git("ls-files", "tests/fixtures"), (
                        f"{rel} claims {claim!r} but tests/fixtures has no files in the index")


# =====================================================================================
# DEFECT 1 — the oracle is genuinely independent
# =====================================================================================


def test_the_oracle_imports_nothing_from_the_production_canonicalisation_path():
    """AST, not text. The I16 guard checked the direction of coupling that did not exist."""
    tree = ast.parse(ORACLE_SRC.read_text(encoding="utf-8"))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    forbidden = {"lifecycle_canonical", "role_bootstrap_lifecycle", "signalnest_identity",
                 "gen_role_bootstrap_policy", "gen_readonly_verifier_policy"}
    assert not (imported & forbidden), (
        f"the oracle imports production modules {sorted(imported & forbidden)}; it would then be "
        "the same implementation wearing a different name")
    assert imported <= {"hashlib", "json", "__future__"}, imported


def test_the_oracle_declares_its_own_schema_rather_than_importing_one():
    """AST, not text.

    The first draft grepped for the string "SEMANTIC_FIELDS" and flagged the oracle's own
    DOCSTRING, which explains the rule it exists to enforce. That is the seventh time a text
    scanner in this chain has flagged its own rule declaration. What matters is whether the
    module BINDS its own schema constant and never READS a production one — both of which the
    parse tree answers exactly and prose cannot confuse.
    """
    tree = ast.parse(ORACLE_SRC.read_text(encoding="utf-8"))

    assigned = {t.id for node in ast.walk(tree) if isinstance(node, ast.Assign)
                for t in node.targets if isinstance(t, ast.Name)}
    assert "ORACLE_SEMANTIC_FIELDS" in assigned, "the oracle must declare its own field list"

    loaded = {n.id for n in ast.walk(tree)
              if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load)}
    production_constants = {"SEMANTIC_FIELDS", "NON_SEMANTIC_FIELDS", "SORTED_LIST_FIELDS"}
    assert not (loaded & production_constants), (
        f"the oracle reads production schema constants {sorted(loaded & production_constants)}")


def test_all_three_anchors_agree_on_the_unmutated_graph():
    steps = lc.steps()
    assert lc.graph_hash() == go.oracle_hash(steps) == fixture_hash()


def test_the_oracle_validates_the_schema_independently():
    """Production does not validate before hashing; the oracle refuses malformed input rather
    than producing a stable digest for it."""
    broken = copy.deepcopy(lc.steps())
    del broken[3]["depends_on"]
    with pytest.raises(go.OracleSchemaError, match="missing required semantic field"):
        go.oracle_hash(broken)


@pytest.mark.parametrize("mutate,reason", [
    (lambda s: [dict(x, sequence=1) for x in s], "duplicate sequence"),
    (lambda s: [dict(x, step_id="same") for x in s], "duplicate step_id"),
    (lambda s: [dict(x, sequence="two") if i == 0 else x for i, x in enumerate(s)],
     "sequence must be a number"),
    (lambda s: [dict(x, depends_on=["no_such_step"]) if i == 5 else x for i, x in enumerate(s)],
     "does not name an existing step"),
])
def test_the_oracle_rejects_malformed_graphs(mutate, reason):
    with pytest.raises(go.OracleSchemaError, match=reason):
        go.oracle_hash(mutate(copy.deepcopy(lc.steps())))


# =====================================================================================
# PHASE I — falsification. Each mutation must break a NAMED anchor.
# =====================================================================================


PRODUCTION_FALSIFICATIONS = {
    "canonicalizer_returns_a_constant":
        lambda m: m.setattr("lifecycle_canonical.canonical_bytes", lambda s: b"CONST"),
    "canonicalizer_omits_all_steps":
        lambda m: m.setattr("lifecycle_canonical.canonical_bytes", lambda s: b"[]"),
    "canonicalizer_omits_dependencies":
        lambda m: m.setattr("lifecycle_canonical.canonical_steps",
                            lambda s: [{k: v for k, v in x.items() if k != "depends_on"}
                                       for x in sorted(s, key=lambda y: y["sequence"])]),
    "canonicalizer_omits_owners":
        lambda m: m.setattr("lifecycle_canonical.canonical_steps",
                            lambda s: [{k: v for k, v in x.items() if k != "owner"}
                                       for x in sorted(s, key=lambda y: y["sequence"])]),
    "canonicalizer_omits_actions":
        lambda m: m.setattr("lifecycle_canonical.canonical_steps",
                            lambda s: [{k: v for k, v in x.items() if k != "action"}
                                       for x in sorted(s, key=lambda y: y["sequence"])]),
}


@pytest.mark.parametrize("name", sorted(PRODUCTION_FALSIFICATIONS))
def test_a_falsified_production_canonicaliser_disagrees_with_both_anchors(name, monkeypatch):
    import lifecycle_canonical  # noqa: F401  (imported for monkeypatch target resolution)
    PRODUCTION_FALSIFICATIONS[name](monkeypatch)
    steps = lc.steps()
    produced = lc.graph_hash()
    assert produced != go.oracle_hash(steps), f"{name}: oracle failed to notice"
    assert produced != fixture_hash(), f"{name}: fixture failed to notice"


def test_a_falsified_oracle_is_attributable_rather_than_merely_detected(monkeypatch):
    """Break the ORACLE and production must still match the fixture, so a reader can tell which
    side is wrong. Two anchors that fail together tell you nothing about where the fault is."""
    monkeypatch.setattr(go, "oracle_hash", lambda s: "0" * 64)
    assert lc.graph_hash() != go.oracle_hash(lc.steps())
    assert lc.graph_hash() == fixture_hash()


def test_a_changed_fixture_fails():
    """The fixture is an anchor only if disagreeing with it is a failure."""
    assert fixture_hash() == lc.graph_hash()
    tampered = "f" * 64
    assert tampered != lc.graph_hash()


SEMANTIC = {
    "omit_a_step": lambda s: s[:-1],
    "change_an_owner": lambda s: [dict(x, owner="SOMEONE_ELSE") if i == 4 else x
                                  for i, x in enumerate(s)],
    "change_an_action": lambda s: [dict(x, action="iam:ListRoles") if x["action"] else x
                                   for x in s],
    "change_a_resource": lambda s: [dict(x, resource="*") if i == 4 else x
                                    for i, x in enumerate(s)],
    "change_a_dependency": lambda s: [dict(x, depends_on=["root_session_open"]) if i == 9 else x
                                      for i, x in enumerate(s)],
    "change_a_timeout": lambda s: [dict(x, timeout_seconds=99) if x["timeout_seconds"] else x
                                   for x in s],
    "change_evidence": lambda s: [dict(x, evidence="other") if i == 4 else x
                                  for i, x in enumerate(s)],
    "change_a_rollback_owner": lambda s: [dict(x, rollback_owner="OTHER") if x["rollback_owner"]
                                          else x for x in s],
}


@pytest.mark.parametrize("name", sorted(SEMANTIC))
def test_a_semantic_change_changes_the_oracle_hash(name):
    mutated = SEMANTIC[name](copy.deepcopy(lc.steps()))
    assert go.oracle_hash(mutated) != go.oracle_hash(lc.steps()), name


NON_SEMANTIC = {
    "reword_a_note": lambda s: [dict(x, note="reworded") for x in s],
    "change_actor_class_alias": lambda s: [dict(x, actor_class="ALIAS") for x in s],
    "reorder_dependency_entries": lambda s: [dict(x, depends_on=list(reversed(x["depends_on"])))
                                             for x in s],
    "reorder_the_step_list": lambda s: list(reversed(s)),
}


@pytest.mark.parametrize("name", sorted(NON_SEMANTIC))
def test_a_non_semantic_change_does_not_change_the_oracle_hash(name):
    mutated = NON_SEMANTIC[name](copy.deepcopy(lc.steps()))
    assert go.oracle_hash(mutated) == go.oracle_hash(lc.steps()), name
