"""Critical-ARN construction audit (Gate 4N-I10, Defect 5).

THE DEFECT. Gate 4N-I7 de-duplicated the boundary POLICY ARN and I reported the ARN
self-witnessing problem closed. The architect lane found it one layer down: the boundary
generator still rebuilt SECRETS_CMK, STATE_CMK, STATE_BUCKET, AUDIT_BUCKET and LOCK_TABLE as
its own f-strings, and every test probed those values FROM THE GENERATOR. One wrong hex digit
in the secrets CMK would fence a nonexistent key — denying kms:Decrypt to the execution roles
and failing task startup at runtime — while the test asking the boundary about the boundary's
own value still passed. Gate 4N-I8 made them WITNESSED by the external anchor; it did not
make them SINGLE-SOURCED.

They now live in scripts/signalnest_identity.py. This file is the enforcement: a static scan
that fails when a policy generator reconstructs a critical ARN instead of importing it. A
one-time cleanup that nothing defends is a cleanup that comes back.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import signalnest_identity as identity  # noqa: E402

# Modules that produce IAM policy documents. These may IMPORT critical identities; they may
# not construct them.
POLICY_GENERATORS = [
    "gen_boundary_policy.py",
    "gen_operator_policies.py",
    "gen_bootstrap_operator_policy.py",
    "gen_role_bootstrap_policy.py",
    "trust_policies.py",
]

# The authoritative layer itself, plus the modules whose whole job is to derive or witness
# identities INDEPENDENTLY. Each entry is a commitment, not a convenience.
EXEMPT = {
    "signalnest_identity.py": "the authoritative layer; this is where construction belongs",
    "resource_oracle.py": "the independent witness — it must derive, not import, or it "
                          "becomes a mirror of the thing it checks",
    "external_anchor.py": "joins generated values against the anchor; builds nothing",
    "trust_validator.py": "derives expectations from the anchor and git remote",
}

# WHAT IS ACTUALLY FORBIDDEN, and what deliberately is not.
#
# The first draft of this audit flagged EVERY literal ARN in a policy generator. That was
# wrong and the audit itself showed it: it fired on broad read scopes such as
# `arn:aws:iam::{ACCOUNT}:role/*` and `...:secret:*`, which are wildcard SCOPES for
# read-only actions, not identities of protected resources. Forcing those through the
# authoritative layer would add indirection without adding a witness, and would train the
# next person to add exemptions.
#
# The defect was the reconstruction of CRITICAL IDENTITIES — the specific resources the
# Denies protect, where one wrong character silently fences the wrong thing. Those are what
# this audit catches: AWS-assigned suffixes and key ids, and the exact names of the
# protected state, audit, lock, trail, boundary, reader-role and reader-task resources.
FORBIDDEN_PATTERNS = [
    (re.compile(r'-tfstate-|-audit-dd|-app-7z'), "AWS-assigned bucket suffix reconstruction"),
    (re.compile(r'77a887bd|548efeee'), "KMS key id reconstruction"),
    (re.compile(r'\{PREFIX\}-tf-lock'), "lock table name reconstruction"),
    (re.compile(r'\{PREFIX\}-audit'), "trail name reconstruction"),
    (re.compile(r'role-boundary["\']'), "boundary policy name reconstruction"),
    (re.compile(r'\{PREFIX\}-revision-reader'), "reader role/task name reconstruction"),
    (re.compile(r'\{PREFIX\}/revision-reader'), "reader ECR path reconstruction"),
    (re.compile(r'\{PREFIX\}/root\.tfstate'), "state object key reconstruction"),
    (re.compile(r'arn:\$\{'), "interpolated ARN template"),
    # GATE 4N-I27Z, AGENDA D. A hosted-zone id is `Z` + 20 uppercase alphanumerics and
    # its ARN carries no account segment, so leak_scan's patterns — 12-digit accounts,
    # 32+ hex runs, UUIDs, AKIA keys, account-bearing ARNs — cannot match it. Gate
    # 4N-I27Y's aws-permissions lane found one hardcoded in three files at once. The
    # identifier class is now named here so a reconstruction is refused rather than
    # invisible. `ZSYNTH...` is exempt: it is the fixture's declared synthetic value.
    (re.compile(r'hostedzone/Z(?!SYNTH)[A-Z0-9]{20}'),
     "Route53 hosted-zone id reconstruction"),
]


def _code_lines(path: Path):
    """Yield (lineno, line) for real code — comments and docstring bodies excluded.

    A docstring that NAMES the forbidden pattern in order to explain it must not fail the
    check that enforces it.
    """
    in_docstring = False
    delimiter = ""
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        stripped = line.strip()
        if in_docstring:
            if delimiter in stripped:
                in_docstring = False
            continue
        if stripped.startswith(('"""', "'''")):
            delimiter = stripped[:3]
            if not (stripped.endswith(delimiter) and len(stripped) > 3):
                in_docstring = True
            continue
        if stripped.startswith("#") or not stripped:
            continue
        yield lineno, line.split("  #")[0]


@pytest.mark.parametrize("module", POLICY_GENERATORS)
def test_no_policy_generator_reconstructs_a_critical_arn(module):
    path = SCRIPTS / module
    assert path.exists(), f"{module} is missing"
    offenders = []
    for lineno, line in _code_lines(path):
        for pattern, why in FORBIDDEN_PATTERNS:
            if pattern.search(line):
                offenders.append(f"{module}:{lineno}: {why}: {line.strip()[:100]}")
    assert not offenders, (
        "policy generators must IMPORT critical identities from signalnest_identity, never "
        "rebuild them — rebuilding is what made the Gate 4N-I7 tests self-witnessing:\n"
        + "\n".join(offenders))


def test_the_authoritative_layer_actually_owns_the_identities():
    """The audit is only meaningful if the layer has something to import."""
    resources = identity.critical_resources()
    for key in ("state_bucket", "state_object", "audit_bucket", "audit_objects",
                "state_cmk", "secrets_cmk", "lock_table", "trail", "boundary_policy"):
        assert key in resources, f"{key} is not owned by the authoritative layer"
        assert resources[key].startswith("arn:aws:"), resources[key]
    assert len([k for k in resources if k.startswith("role:")]) == 8


def test_every_critical_identity_is_witnessed_by_the_external_anchor():
    """Single-sourcing without a witness would just be a tidier version of the defect."""
    import external_anchor

    checked = {row["identity"] for row in external_anchor.join()["rows"]}
    assert len(checked) >= 20, checked


def test_the_boundary_generator_imports_rather_than_builds():
    source = (SCRIPTS / "gen_boundary_policy.py").read_text(encoding="utf-8")
    assert "STATE_BUCKET_ARN" in source and "SECRETS_CMK_ARN" in source, (
        "the boundary generator must import the authoritative names")
    # GATE 4N-I18, SEC-1: the forbidden token is DERIVED from the tier-resolved identity
    # rather than pasted. Writing the live suffix here to prove it is absent would put the
    # very identifier into version control that the containment exists to keep out.
    suffix = identity.STATE_BUCKET_NAME.rsplit("-", 1)[-1]
    assert suffix not in source, "the state bucket suffix is reconstructed again"


def test_the_dead_duplicate_constants_are_gone():
    """The architect lane found ECS_EXECUTION_ROLE, APP_TASK_ROLES and TRAIL unreferenced."""
    import gen_boundary_policy as gb

    for attr in ("ECS_EXECUTION_ROLE", "APP_TASK_ROLES"):
        assert not hasattr(gb, attr), f"{attr} is back; it was dead AND a duplicate"


@pytest.mark.parametrize("module,why", sorted(EXEMPT.items()))
def test_every_exemption_is_justified_and_still_needed(module, why):
    assert (SCRIPTS / module).exists(), f"stale exemption for {module}"
    assert len(why.split()) >= 6, f"the exemption for {module} is not justified"
    assert module not in POLICY_GENERATORS, (
        f"{module} is exempt AND audited — the exemption would silently win")


def test_the_audit_can_actually_fail(tmp_path):
    """Controls the control: a scanner nobody has seen fail is not a scanner."""
    offender = tmp_path / "gen_fake_policy.py"
    offender.write_text(
        'BAD = f"arn:aws:s3:::{PREFIX}-tfstate-synth0"\n', encoding="utf-8")  # noqa
    hits = [why for _, line in _code_lines(offender)
            for pattern, why in FORBIDDEN_PATTERNS if pattern.search(line)]
    assert hits, "the scanner did not flag a literal reconstructed ARN"


def test_a_docstring_mentioning_the_pattern_does_not_trip_the_audit(tmp_path):
    """Otherwise the honest explanation of the rule would break the rule."""
    documented = tmp_path / "gen_documented.py"
    documented.write_text(
        '"""This module must not reconstruct {PREFIX}-tf-lock or 77a887bd.\n\nProse.\n"""\n'
        "VALUE = 1\n", encoding="utf-8")
    hits = [why for _, line in _code_lines(documented)
            for pattern, why in FORBIDDEN_PATTERNS if pattern.search(line)]
    assert not hits, f"docstring text was treated as code: {hits}"
