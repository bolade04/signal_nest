#!/usr/bin/env python3
"""GATE 4N-I27M — the tag-key allow-list is bounded by an INDEPENDENT reviewed source.

WHAT WAS OPEN. `gen_role_bootstrap_policy.ALLOWED_TAG_KEYS` is interpolated into
ForAllValues:StringEquals on aws:TagKeys for iam:TagRole. That is an AUTHORIZATION CONDITION —
it bounds which tag keys the role-bootstrap principal may set on the three reader roles inside
a live trust window — and nothing bounded the list itself. Gate 4N-I27M added one unreviewed
key and the repository's own evaluator moved an iam:TagRole request carrying that key from
IMPLICIT_DENY to EXPLICIT_ALLOW, while all seven downstream consumers, the 133-test policy
suite and the graded lifecycle command exited 0.

THE INDEPENDENT SOURCE. `trust_policies.trust_manifest()[*]["tags_expectation"]` — authored
from the trust documents, sent verbatim to AWS by role_bootstrap_executor, and compared by the
ListRoleTags read-back. Neither module imports the other, so it cannot agree with the list by
construction. Comparing ALLOWED_TAG_KEYS to a copy of itself would be the defect, not the fix.
"""
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import expiry_authorization as ea  # noqa: E402
import gen_role_bootstrap_policy as gen  # noqa: E402
import iam_eval  # noqa: E402
import trust_policies  # noqa: E402

EXPIRY = ea.ACTIVE_EXPIRY_UTC
UNREVIEWED = "sn:AuthorizationProvenance"


def _tag_keys(policy: dict) -> list:
    for statement in policy["Statement"]:
        condition = statement.get("Condition", {}).get("ForAllValues:StringEquals", {})
        if "aws:TagKeys" in condition:
            return list(condition["aws:TagKeys"])
    raise AssertionError("the generated policy carries no aws:TagKeys condition at all")


# --- the domain itself ------------------------------------------------------------------


def test_the_expected_domain_comes_from_the_trust_manifest_not_the_allow_list():
    """If this ever reads ALLOWED_TAG_KEYS the check is self-satisfying and worthless."""
    expected = gen.reviewed_tag_key_domain()
    manifest = set()
    for entry in trust_policies.trust_manifest().values():
        manifest |= set(entry["tags_expectation"])
    assert expected == manifest, "the domain drifted from the reviewed trust manifest"
    # AST, not text: the function's DOCSTRING names the symbol on purpose (to say it must not
    # be read), and a substring search cannot tell an explanation from a reference.
    import ast

    tree = ast.parse(Path(gen.__file__).read_text(encoding="utf-8"))
    func = next(n for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef) and n.name == "reviewed_tag_key_domain")
    referenced = {n.id for n in ast.walk(func) if isinstance(n, ast.Name)} | \
                 {n.attr for n in ast.walk(func) if isinstance(n, ast.Attribute)}
    assert "ALLOWED_TAG_KEYS" not in referenced, (
        "reviewed_tag_key_domain() reads the value it is supposed to bound; a list compared "
        "against a copy of itself agrees with anything")


def test_an_empty_expected_domain_is_refused_rather_than_treated_as_permissive(monkeypatch):
    """Absence must never be read as 'every key is approved'."""
    monkeypatch.setattr(trust_policies, "trust_manifest", lambda: {})
    with pytest.raises(gen.TagKeyDomainError, match="NO tag keys"):
        gen.reviewed_tag_key_domain()


# --- the correction, both directions ------------------------------------------------------


def test_the_reviewed_allow_list_generates():
    assert _tag_keys(gen.role_bootstrap_policy(EXPIRY)) == list(gen.ALLOWED_TAG_KEYS)


def test_a_known_reviewed_key_passes():
    gen.require_reviewed_tag_keys(sorted(gen.reviewed_tag_key_domain()))


@pytest.mark.parametrize("key", [UNREVIEWED, "Owner", "aws:PrincipalTag/Escalate", "x"])
def test_an_unreviewed_key_is_refused(key):
    with pytest.raises(gen.TagKeyDomainError, match="NOT declared"):
        gen.require_reviewed_tag_keys(list(gen.ALLOWED_TAG_KEYS) + [key])


def test_a_missing_reviewed_key_is_refused():
    """The narrowing direction: the executor sends exactly the manifest's tags, so a grant
    that omits one refuses the tagging its own read-back verifies."""
    with pytest.raises(gen.TagKeyDomainError, match="does not permit"):
        gen.require_reviewed_tag_keys([])


@pytest.mark.parametrize("bad", [None, "Name", [""], [1], ()])
def test_a_malformed_allow_list_is_refused(bad):
    with pytest.raises(gen.TagKeyDomainError):
        gen.require_reviewed_tag_keys(bad)


def test_generation_refuses_before_any_policy_output_exists(monkeypatch):
    """Rejecting at GENERATION is strictly stronger than detecting downstream: an unreviewed
    key can never reach a generated or hashed artifact."""
    monkeypatch.setattr(gen, "ALLOWED_TAG_KEYS", list(gen.ALLOWED_TAG_KEYS) + [UNREVIEWED])
    with pytest.raises(gen.TagKeyDomainError):
        gen.role_bootstrap_policy(EXPIRY)


def test_generated_policy_is_stable_for_the_valid_input():
    first, second = gen.role_bootstrap_policy(EXPIRY), gen.role_bootstrap_policy(EXPIRY)
    assert first == second


# --- the consequence this closes, stated as the evaluator sees it -------------------------


def test_the_unreviewed_key_would_have_changed_the_authorization_decision(monkeypatch):
    """The reason this is a security control and not metadata hygiene.

    Textual presence in the policy is NOT the finding; the DECISION changing is.
    """
    target = gen.TARGET_ROLE_ARNS[0]
    context = {"aws:CurrentTime": "2026-08-02T12:00:00Z", "aws:TagKeys": [UNREVIEWED]}
    reviewed = gen.role_bootstrap_policy(EXPIRY)
    assert iam_eval.decide(reviewed, "iam:TagRole", target, context).decision \
        is iam_eval.Decision.IMPLICIT_DENY

    # Build what the generator WOULD have emitted before the correction, by hand, so the
    # comparison does not depend on the guard being absent.
    widened = gen.role_bootstrap_policy(EXPIRY)
    for statement in widened["Statement"]:
        condition = statement.get("Condition", {}).get("ForAllValues:StringEquals", {})
        if "aws:TagKeys" in condition:
            condition["aws:TagKeys"] = list(condition["aws:TagKeys"]) + [UNREVIEWED]
    assert iam_eval.decide(widened, "iam:TagRole", target, context).decision \
        is iam_eval.Decision.EXPLICIT_ALLOW, (
        "if this ever stops being an EXPLICIT_ALLOW the finding's premise has changed")


def test_the_reviewed_tag_key_still_authorizes():
    """The correction must not break the tagging the roles actually need."""
    target = gen.TARGET_ROLE_ARNS[0]
    context = {"aws:CurrentTime": "2026-08-02T12:00:00Z",
               "aws:TagKeys": sorted(gen.reviewed_tag_key_domain())}
    assert iam_eval.decide(gen.role_bootstrap_policy(EXPIRY), "iam:TagRole", target,
                           context).decision is iam_eval.Decision.EXPLICIT_ALLOW


# --- propagation: the graded command, not just the function -------------------------------


def test_the_refusal_propagates_through_a_graded_consumer(tmp_path):
    """A guard whose failure path never runs as a process is a guard nobody has seen fail.

    role_bootstrap_lifecycle.py is a graded CI step and imports this generator; with an
    unreviewed key in source it must exit non-zero.
    """
    import os
    import shutil

    source = REPO_ROOT / "scripts" / "gen_role_bootstrap_policy.py"
    backup = tmp_path / "gen_role_bootstrap_policy.py"
    shutil.copy2(source, backup)
    env = dict(os.environ, SIGNALNEST_ANCHOR_TIER="TIER_1_SYNTHETIC")
    try:
        clean = subprocess.run([sys.executable, "scripts/role_bootstrap_lifecycle.py"],
                               cwd=REPO_ROOT, capture_output=True, text=True, env=env)
        assert clean.returncode == 0, (
            f"baseline must be green before the mutation means anything:\n{clean.stdout}"
            f"{clean.stderr}")

        source.write_text(
            source.read_text(encoding="utf-8").replace(
                'ALLOWED_TAG_KEYS = ["Name"]',
                f'ALLOWED_TAG_KEYS = ["Name", "{UNREVIEWED}"]'), encoding="utf-8")
        bad = subprocess.run([sys.executable, "scripts/role_bootstrap_lifecycle.py"],
                             cwd=REPO_ROOT, capture_output=True, text=True, env=env)
        assert bad.returncode != 0, (
            "a graded consumer exited 0 with an unreviewed tag key in the generated policy; "
            "CI grades by exit code, so a zero here means the refusal cannot fail the job")
    finally:
        shutil.copy2(backup, source)
