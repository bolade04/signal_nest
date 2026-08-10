"""Semantic ARN component verification (Gate 4N-I12, Defect 4).

THE DEFECT. The critical-ARN audit was a line-based regex and the Gate 4N-I10 adversarial
lane evaded it with string concatenation:

    SECRETS_CMK = "arn:aws:kms:us-east-1:...:key/" + "548ef" + "eee-..." + "fb"

One wrong final hex digit. The audit passed 15/15 and the suite passed 933/1, and the shipped
boundary then fenced DenyKmsUseOutsideSecretsCmk at a key that does not exist — which, per
Gate 4N-H4, CONFINES rather than denies, so the real secrets CMK lands inside the Deny and
task startup breaks.

A regex knows what a string looks like. It does not know what an ARN is. These tests mutate
ARNs COMPONENT BY COMPONENT and require each to be a named mismatch.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import arn_model  # noqa: E402
import signalnest_identity as identity  # noqa: E402

CRITICAL = arn_model.critical_arns()


def test_every_critical_arn_parses():
    result = arn_model.model()
    assert result["clean"], result["malformed"]
    assert result["count"] >= 15, result["count"]


@pytest.mark.parametrize("name", sorted(CRITICAL))
def test_each_critical_arn_round_trips(name):
    parsed = arn_model.parse(CRITICAL[name])
    assert parsed.partition and parsed.service
    assert arn_model.compare(CRITICAL[name], CRITICAL[name])["result"] == "MATCH"


# --- component mutations ------------------------------------------------------------------

MUTATIONS = {
    "partition": lambda a: a.replace("arn:aws:", "arn:aws-us-gov:", 1),
    "service": lambda a: a.replace(":kms:", ":kmz:").replace(":s3:", ":s4:")
                          .replace(":iam:", ":iamx:").replace(":dynamodb:", ":dynamodbx:")
                          .replace(":cloudtrail:", ":cloudtrailx:")
                          .replace(":secretsmanager:", ":secretsmanagerx:")
                          .replace(":ecr:", ":ecrx:").replace(":rds:", ":rdsx:"),
    "region": lambda a: a.replace(":us-east-1:", ":eu-west-3:"),
    "account": lambda a: a.replace("111122223333", "999988887777"),
    "resource_type": lambda a: a.replace(":key/", ":alias/").replace(":table/", ":index/")
                                .replace(":role/", ":user/").replace(":policy/", ":group/")
                                .replace(":trail/", ":channel/")
                                .replace(":repository/", ":registry/"),
    "one_identifier_character": lambda a: (a[:-1] + ("b" if a[-1] != "b" else "c")),
    "separator_slash_to_hyphen": lambda a: a.replace("/", "-", 1) if "/" in a else None,
    "separator_colon_to_slash": lambda a: a.replace(":secret:", ":secret/") if ":secret:" in a else None,
    "wildcard_inserted": lambda a: a.rsplit("/", 1)[0] + "/*" if "/" in a else a + "*",
    "missing_component": lambda a: a.replace(":us-east-1:", "::", 1) if ":us-east-1:" in a else None,
}

CASES = [(name, mutation) for name in sorted(CRITICAL) for mutation in sorted(MUTATIONS)]


@pytest.mark.parametrize("name,mutation", CASES, ids=[f"{n}|{m}" for n, m in CASES])
def test_every_component_mutation_is_a_named_mismatch(name, mutation):
    original = CRITICAL[name]
    mutated = MUTATIONS[mutation](original)
    if mutated is None or mutated == original:
        pytest.skip(f"{mutation} does not apply to {name}")  # shape-inapplicable, not a pass
    result = arn_model.compare(original, mutated)
    assert result["result"] in ("MISMATCH", "MALFORMED"), (
        f"{mutation} on {name} produced {result['result']}: {mutated}")
    if result["result"] == "MISMATCH":
        assert result["differences"], "a mismatch must NAME the differing component"


# --- THE decisive case --------------------------------------------------------------------


def test_every_critical_arn_gets_enough_applicable_mutations():
    """This gate is about skips hiding things, so the skips here are BOUNDED and counted.

    A skip above means the mutation is shape-inapplicable — there is no slash in an S3 bucket
    ARN to convert to a hyphen. That is honest, but an unbounded skip rate would let a
    mutation set decay into nothing while still reporting passes.

    The floor is derived from the ARN's OWN STRUCTURE rather than being a flat number. An S3
    bucket ARN legitimately carries no region, no account and no resource type
    (arn:aws:s3:::bucket), so demanding six applicable mutations of it would be demanding
    mutations of components that do not exist. The requirement is: every component the ARN
    actually HAS must be mutable, plus the two structural mutations that always apply
    (identifier character, wildcard).
    """
    thin = {}
    for name, arn in sorted(CRITICAL.items()):
        parsed = arn_model.parse(arn)
        present = sum(1 for value in (parsed.partition, parsed.service, parsed.region,
                                      parsed.account, parsed.resource_type) if value)
        floor = present + 2
        applicable = sum(1 for fn in MUTATIONS.values()
                         if (m := fn(arn)) is not None and m != arn)
        if applicable < floor:
            thin[name] = f"{applicable} applicable, {floor} expected for its shape"
    assert not thin, f"too few applicable mutations: {thin}"


def test_every_applicable_mutation_is_actually_detected():
    """No skip may conceal a mutation that applies but is NOT caught."""
    undetected = []
    for name, arn in sorted(CRITICAL.items()):
        for mutation, fn in MUTATIONS.items():
            mutated = fn(arn)
            if mutated is None or mutated == arn:
                continue
            if arn_model.compare(arn, mutated)["result"] == "MATCH":
                undetected.append(f"{name}|{mutation}")
    assert not undetected, undetected


def test_one_character_secrets_cmk_corruption_is_caught():
    """The exact mutation that passed the regex audit 15/15 with a green 933-test suite."""
    real = identity.SECRETS_CMK_ARN
    # GATE 4N-I18, SEC-1: the corruption is derived from the resolved ARN rather than asserting
    # a fragment of the real key id. Pasting "fa" here pinned the test to a live identifier and
    # silently became a no-op the moment the value stopped being a repository literal.
    last = real[-1]
    forged = real[:-1] + ("b" if last != "b" else "c")
    assert forged != real
    result = arn_model.compare(real, forged)
    assert result["result"] == "MISMATCH", "the one-character CMK corruption was accepted"
    assert any("resource_id" in d for d in result["differences"]), result["differences"]


def test_concatenation_cannot_evade_the_semantic_comparison():
    """The evasion technique itself: the value is compared, not the source text."""
    real = identity.SECRETS_CMK_ARN
    head, tail = real[:-10], real[-10:]
    reassembled_wrong = head + tail[:-1] + ("b" if tail[-1] != "b" else "c")
    assert arn_model.compare(real, reassembled_wrong)["result"] == "MISMATCH"
    reassembled_right = head + tail
    assert arn_model.compare(real, reassembled_right)["result"] == "MATCH"


def test_the_state_and_secrets_cmks_are_distinguished():
    """Fencing the wrong CMK confines the right one — the 4N-H4 NotResource finding."""
    result = arn_model.compare(identity.SECRETS_CMK_ARN, identity.STATE_CMK_ARN)
    assert result["result"] == "MISMATCH"
    assert any("resource_id" in d for d in result["differences"])


def test_the_reader_ecr_slash_form_is_not_interchangeable_with_a_hyphen():
    """Gate 4N-I2: the reader ECR path uses a SLASH; a hyphen is a different repository."""
    real = identity.READER_ECR_ARN
    hyphenated = real.replace("signalnest-staging/revision-reader",
                              "signalnest-staging-revision-reader")
    assert real != hyphenated
    assert arn_model.compare(real, hyphenated)["result"] == "MISMATCH"


def test_a_malformed_arn_is_reported_not_ignored():
    for bad in ("not-an-arn", "arn:aws:iam", "", "arn::iam::123:role/x"):
        result = arn_model.compare(identity.STATE_CMK_ARN, bad)
        assert result["result"] in ("MALFORMED", "MISMATCH"), bad


def test_the_model_does_not_rely_on_regex_for_the_comparison():
    """Static guard on the technique, not just the outcome."""
    import ast

    source = (REPO_ROOT / "scripts" / "arn_model.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    assert "re" not in names, "the semantic model imported the regex module"
    assert not {"match", "search", "fullmatch"} & attrs, "a regex call is back in the model"
