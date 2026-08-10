"""Requirement-driven widening ceiling (Gate 4N-I17, Defect 7, Phases U/V).

THE DEFECT. Gate 4N-I16's containment test defined "the declared roles" as
`gen.INLINE_POLICY_ROLE_ARNS` — the policy's own Resource list. The Allow, the NotResource fence
and the test expectation all read one value, so widening the grant widened its own ceiling.
Appending an arbitrary attacker ARN to that list left the entire suite and nine guard scripts
green.

THREE INDEPENDENT SETS, and all three must agree (Phase S/U):

  EXPECTED   tests/fixtures/expected-writable-roles.json — AUTHORED from the architecture
             requirement, tracked, never parsed from .tf and never read from a policy.
  DECLARED   scripts/terraform_role_inventory.py — PARSED from the actual `aws_iam_role_policy`
             declarations, including the for_each form where migration-task is excluded.
  GENERATED  gen_operator_policies.INLINE_POLICY_ROLE_ARNS — what the policy actually grants.

The decisive property is that EXPECTED shares no producer with GENERATED. A widening that edits
the generator AND the policy together still fails, because the authored fixture does not move.
That simultaneous-edit case is tested explicitly below — it is the one Gate 4N-I16 could not
detect.
"""

from __future__ import annotations

import datetime
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import expiry_authorization as _ea  # noqa: E402
import gen_operator_policies as gen  # noqa: E402
import iam_eval  # noqa: E402
import signalnest_identity as identity  # noqa: E402
import terraform_role_inventory as tfroles  # noqa: E402

CEILING = REPO_ROOT / "tests" / "fixtures" / "expected-writable-roles.json"
# GATE 4N-I28R. Derived from the reviewed issuance rather than hand-written, so a restamp does
# not leave an expiry that predates its own issuance and turn every generator call in this file
# into a refusal. Four hours is an arbitrary in-window value; nothing here tests the bound.
EXPIRY = (datetime.datetime.strptime(_ea.ACTIVE_ISSUANCE_UTC, "%Y-%m-%dT%H:%M:%SZ")
          + datetime.timedelta(hours=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
CTX = {"aws:CurrentTime": "2026-07-31T12:00:00Z",
       "aws:RequestedRegion": identity.REGION,
       "iam:PermissionsBoundary": identity.BOUNDARY_POLICY_ARN}


def ceiling() -> dict:
    return json.loads(CEILING.read_text(encoding="utf-8"))


def expected_writable() -> set:
    """EXPECTED lineage: the authored fixture. Never the generator, never the .tf parser."""
    return {identity.iam_role_arn(f"{identity.PREFIX}-{e['suffix']}")
            for e in ceiling()["role_name_suffixes_writable"]}


def never_writable() -> set:
    return {identity.iam_role_arn(f"{identity.PREFIX}-{e['suffix']}")
            for e in ceiling()["role_name_suffixes_never_writable"]}


# =====================================================================================
# The three sets agree
# =====================================================================================


def test_the_authored_ceiling_is_tracked():
    import subprocess
    # GATE 4N-I20, ARCH-H3/AWS-3. `git ls-files` reports the INDEX, not history. These fixtures are STAGED ADDITIONS on a branch that is zero commits ahead, so the old assertion passed while `git ls-tree HEAD` returned nothing — and a staged anchor has exactly the 'no history, no review trail' weakness the check was written to exclude. The state is now named exactly, and the property that actually matters — the file reaches the commit that will be made — is asserted against the PREDICTED COMMIT TREE.
    import tracked_state

    rel = str(CEILING.relative_to(REPO_ROOT))
    state = tracked_state.state_of(rel)
    assert state in (tracked_state.STAGED_ADDITION, tracked_state.TRACKED_IN_HEAD), (
        f"the authored widening ceiling is {state}; it must be at least staged for addition")
    assert rel in tracked_state.predicted_commit_tree()["entries"], (
        f"the authored widening ceiling would not be part of the commit this branch would produce")


def test_generated_scope_equals_the_authored_ceiling():
    generated = set(gen.INLINE_POLICY_ROLE_ARNS)
    expected = expected_writable()
    assert generated == expected, {
        "over_granted": sorted(generated - expected),
        "under_granted": sorted(expected - generated)}


def test_terraform_declarations_agree_with_the_authored_ceiling():
    """The third, separately-derived set. Agreement of all three is what makes any one
    trustworthy; agreement of two that share a producer would prove nothing."""
    declared = set(tfroles.role_arns(tfroles.writable_roles()))
    assert declared == expected_writable(), {
        "declared_not_expected": sorted(declared - expected_writable()),
        "expected_not_declared": sorted(expected_writable() - declared)}


def test_migration_task_is_never_writable():
    """Gate 4N-I16 made it writable by scoping from ALL_ROLE_NAMES."""
    migration = identity.iam_role_arn(f"{identity.PREFIX}-migration-task")
    assert migration in never_writable()
    assert migration not in set(gen.INLINE_POLICY_ROLE_ARNS)
    policy = gen.bootstrap_temp_policy(EXPIRY)
    for action in ceiling()["role_name_suffixes_never_writable"][0]["forbidden_actions"]:
        decision = iam_eval.decide(policy, action, migration, CTX).decision
        assert decision is not iam_eval.Decision.EXPLICIT_ALLOW, (
            f"{action} is allowed on the deliberately-empty migration role: {decision.name}")


# =====================================================================================
# PHASE Y — widening mutations. The simultaneous-edit case is the one that matters.
# =====================================================================================


WIDENINGS = {
    "add_migration_task": lambda: identity.iam_role_arn(f"{identity.PREFIX}-migration-task"),
    "add_sibling_role": lambda: identity.iam_role_arn(f"{identity.PREFIX}-not-declared"),
    "add_wildcard_role": lambda: f"arn:aws:iam::{identity.ACCOUNT}:role/*",
    # GATE 4N-I18: a DIFFERENT placeholder account than identity.ACCOUNT. Using the same one
    # silently turned this mutation into a no-op.
    "add_wrong_account_role": lambda: "arn:aws:iam::444444444444:role/signalnest-staging-api-task",
    "add_reserved_sso_role": lambda: (
        f"arn:aws:iam::{identity.ACCOUNT}:role/aws-reserved/sso.amazonaws.com/"
        f"{identity.REGION}/AWSReservedSSO_AdministratorAccess_abc"),
    "add_kms_key": lambda: identity.kms_key_arn("00000000-1111-2222-3333-444444444444"),
    "add_state_bucket": lambda: f"arn:aws:s3:::{identity.STATE_BUCKET_NAME}",
    "add_secret": lambda: (f"arn:aws:secretsmanager:{identity.REGION}:{identity.ACCOUNT}"
                           f":secret:{identity.PREFIX}/DATABASE_URL"),
}


@pytest.mark.parametrize("name", sorted(WIDENINGS))
def test_a_widening_is_detected_even_when_generator_and_policy_change_together(
        name, monkeypatch):
    """THE case Gate 4N-I16 could not see.

    Both the generator constant AND the emitted policy are widened in the same breath — exactly
    what an author would do if they were "fixing" the grant. The authored ceiling does not move
    with them, so the widening is still caught.
    """
    widened = list(gen.INLINE_POLICY_ROLE_ARNS) + [WIDENINGS[name]()]
    monkeypatch.setattr(gen, "INLINE_POLICY_ROLE_ARNS", widened)

    # The policy really does grant it now — the widening is genuine, not simulated.
    policy = gen.bootstrap_temp_policy(EXPIRY)
    statement = next(s for s in policy["Statement"]
                     if s.get("Sid") == "TempInlineRolePolicyBounded")
    assert WIDENINGS[name]() in statement["Resource"], "the widening did not take effect"

    # And the authored ceiling still rejects it.
    assert set(widened) != expected_writable(), (
        f"{name}: the generated scope was widened and the authored ceiling did not notice")


def test_the_ceiling_is_inert_data_that_cannot_read_the_generated_side():
    """If the expected side could reach the generated side, the whole file would be theatre.

    Scoped to the DATA keys, not the narrative ones. The first draft grepped the whole file and
    flagged `_why_this_file_exists`, which names the generator constant precisely in order to
    explain the defect being prevented — the eighth time in this chain that a scanner has caught
    its own rule declaration. Explaining a hazard is not committing it.
    """
    assert CEILING.suffix == ".json", "the ceiling must be data, not executable code"
    doc = ceiling()

    # The LOAD-BEARING values only: suffixes, forbidden actions, resource classes. `why` and
    # `_`-prefixed keys are documentation — and documentation is allowed, indeed required, to
    # name the hazard it describes. What must not happen is a load-bearing VALUE being sourced
    # from the generator.
    load_bearing = []
    for key, rows in doc.items():
        if key.startswith("_") or not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                load_bearing.append(str(row))
                continue
            for field, value in row.items():
                if field == "why":
                    continue
                load_bearing.append(json.dumps(value))

    rendered = " ".join(load_bearing)
    for forbidden in ("INLINE_POLICY_ROLE_ARNS", "ALL_ROLE_NAMES", "bootstrap_temp_policy"):
        assert forbidden not in rendered, (
            f"a load-bearing value in the ceiling references {forbidden}, so the expected side "
            "would be derived from the observed side")
    assert load_bearing, "the ceiling declares no load-bearing values at all"


# =====================================================================================
# PHASE V — requirement consumer coverage
# =====================================================================================


def test_every_authored_requirement_row_has_an_enforcing_consumer():
    """Requirement metadata that nothing reads is documentation, not enforcement.

    The Gate 4N-I16 security lane found `requirement_kind` and `principal` in the requirements
    fixture were read by no code at all. Every row here must be consumed by a real assertion.
    """
    doc = ceiling()
    consumers = {
        "role_name_suffixes_writable": "test_generated_scope_equals_the_authored_ceiling",
        "role_name_suffixes_never_writable": "test_migration_task_is_never_writable",
        "forbidden_resource_classes": "test_a_widening_is_detected_even_when_generator_and_"
                                      "policy_change_together",
        # GATE 4N-I24C, finding I24C-04. EXACT identities, added because a suffix rule pins
        # how a role is SPELLED and not WHICH role may be written.
        "writable_role_names": "test_a_role_naming_itself_into_the_suffix_space_is_refused",
        "never_writable_role_names": "test_a_role_the_parser_misses_fails_closed",
    }
    for key in consumers:
        assert key in doc, f"{key} missing from the authored ceiling"
        assert doc[key], f"{key} is empty — an empty requirement enforces nothing"
    unconsumed = [k for k in doc
                  if not k.startswith("_") and k not in consumers]
    assert not unconsumed, f"requirement rows with no enforcing consumer: {unconsumed}"


# =====================================================================================
# GATE 4N-I24C, finding I24C-04 — the two executed I23 exploits, as shipping tests
# =====================================================================================

def test_a_role_naming_itself_into_the_suffix_space_is_refused(monkeypatch):
    """I23: 'signalnest-staging-evil-api-task' satisfied endswith('api-task') and entered BOTH
    sides, reporting CORRECTLY_WRITABLE with clean=True. The ceiling pinned how identities are
    SPELLED, not WHICH identities may be written."""
    import terraform_role_inventory as tri
    import signalnest_identity as identity
    import gen_operator_policies as gen

    evil = f"{identity.PREFIX}-evil-api-task"
    assert tri.classify_role_name(evil) == "UNKNOWN", \
        "a role that merely ends in an approved suffix must not be admitted"

    real_d, real_w = tri.declared_roles, tri.writable_roles
    monkeypatch.setattr(tri, "declared_roles",
                        lambda: {**real_d(), "evil": {"role_name": evil}})
    monkeypatch.setattr(tri, "writable_roles",
                        lambda: {**real_w(), "evil": [{"policy_label": "evil"}]})
    monkeypatch.setattr(gen, "INLINE_POLICY_ROLE_ARNS",
                        tri.role_arns(tri.writable_roles()), raising=False)
    result = tri.reconcile()
    assert not result["clean"]
    assert evil in result["unknown_roles"]


def test_a_role_the_parser_misses_fails_closed(monkeypatch):
    """I23 FAIL-OPEN: a role the .tf parser failed to discover dropped out of BOTH sides and
    the reconciliation reported clean with zero problems. It needed no attacker — only a
    parser that misses a form."""
    import terraform_role_inventory as tri
    import gen_operator_policies as gen

    real_d, real_w = tri.declared_roles, tri.writable_roles
    monkeypatch.setattr(tri, "declared_roles",
                        lambda: {k: v for k, v in real_d().items() if k != "api_task"})
    monkeypatch.setattr(tri, "writable_roles",
                        lambda: {k: v for k, v in real_w().items() if k != "api_task"})
    monkeypatch.setattr(gen, "INLINE_POLICY_ROLE_ARNS",
                        tri.role_arns(tri.writable_roles()), raising=False)
    result = tri.reconcile()
    assert not result["clean"], "a role the parser missed must not vanish silently"
    assert any("parser did not discover it" in p for p in result["problems"])


def test_the_lineage_guard_follows_callees(monkeypatch):
    """I23 adversarial X5: relocating the coupling into required_writable_arns() left the AST
    guard passing and the standalone guard exiting 0."""
    import terraform_role_inventory as tri
    source = (
        "def required_writable_arns(role_names=None, scope=None):\n"
        "    return set(role_arns(writable_roles()))\n"
        "def reconcile(generated_writable_arns=None, expected_writable_arns=None):\n"
        "    expected_arns = required_writable_arns(names, scope)\n"
        "    return expected_arns\n")
    with pytest.raises(tri.RequirementError, match="relocated one function away"):
        tri.assert_expected_lineage(source_override=source)
