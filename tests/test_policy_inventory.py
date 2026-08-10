"""Every policy artifact is validated, by DISCOVERY (Gate 4N-I11, Defects 5, 6).

THE DEFECT. Gate 4N-I10 shipped a RoleBootstrapOperator policy its own validator rejects:
`iam:TagRole` conditioned on `iam:PermissionsBoundary`, which TagRole does not support, so
the grant was dead and CreateRole-with-tags would have failed the read-back and looped. The
detector existed. A negative-control test built that exact shape to prove the detector fires.
The policy was never passed to `validate_policy`, because the call sites were a hand-written
list and the newest artifact was not on it.

A hand-maintained list of things to check cannot protect the thing you just added — adding it
is the moment you forget. Hence discovery, and hence the orphan test below, which creates a
brand-new generator at runtime and requires it to be picked up with no edit to any list.

The discovery rule itself already caught one instance of the same mistake: the first glob was
`gen_*policy*.py`, which silently missed `gen_operator_policies.py` because "policies" does
not contain "policy".
"""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

import expiry_authorization as _ea  # noqa: E402
import iam_eval  # noqa: E402
import policy_inventory  # noqa: E402

REPORT = policy_inventory.validate_all()
EXPIRY = _ea.ACTIVE_EXPIRY_UTC


def test_every_discovered_policy_is_valid():
    assert REPORT["clean"], [
        f"{r['policy']}: {r['problems']}" for r in REPORT["invalid"]]


def test_the_role_bootstrap_policy_is_now_validated_and_valid():
    """The specific artifact that escaped validation in Gate 4N-I10."""
    row = next(r for r in REPORT["rows"]
               if r["policy"] == "gen_role_bootstrap_policy.role_bootstrap_policy")
    assert row["result"] == "VALID", row["problems"]


def test_discovery_finds_every_generator_module():
    """A discovery rule narrower than reality is a hand-maintained list wearing a glob."""
    on_disk = {p.stem for p in SCRIPTS.glob("gen_*.py")}
    policy_producers = {m for m in on_disk if "polic" in m}
    assert set(REPORT["modules"]) == policy_producers, (
        f"discovered {REPORT['modules']} but {sorted(policy_producers)} exist on disk")
    assert REPORT["discovered"] >= 5, REPORT["discovered"]


def test_every_known_principal_appears_in_the_inventory():
    """Names, not counts: a renamed callable would keep the count and lose the coverage."""
    discovered = {r["policy"] for r in REPORT["rows"]}
    for expected in (
        "gen_boundary_policy.boundary_policy",
        "gen_operator_policies.permanent_w0_policy",
        "gen_operator_policies.bootstrap_temp_policy",
        "gen_bootstrap_operator_policy.bootstrap_operator_policy",
        "gen_role_bootstrap_policy.role_bootstrap_policy",
    ):
        assert expected in discovered, f"{expected} is not being validated"


def test_a_new_policy_artifact_is_discovered_without_editing_any_list(tmp_path, monkeypatch):
    """THE orphan test. A new generator must be picked up with no edit here."""
    new_module = SCRIPTS / "gen_orphan_probe_policy.py"
    assert not new_module.exists(), "leftover probe module from a previous run"
    new_module.write_text(
        '"""Temporary probe generator created by test_policy_inventory."""\n'
        "def orphan_probe_policy(expiry):\n"
        "    return {'Version': '2012-10-17', 'Statement': [\n"
        "        {'Sid': 'Bad', 'Effect': 'Allow', 'Action': 'iam:TagRole',\n"
        "         'Resource': '*',\n"
        "         'Condition': {'DateLessThan': {'aws:CurrentTime': expiry},\n"
        "                       'StringEquals': {'iam:PermissionsBoundary': 'arn:aws:iam::1:policy/b'}}}]}\n",
        encoding="utf-8")
    try:
        importlib.invalidate_caches()
        result = policy_inventory.validate_all()
        discovered = {r["policy"] for r in result["rows"]}
        assert "gen_orphan_probe_policy.orphan_probe_policy" in discovered, (
            "a NEW policy generator was not discovered — the inventory is not discovery-based")
        assert not result["clean"], "the deliberately invalid orphan policy validated clean"
        row = next(r for r in result["rows"]
                   if r["policy"] == "gen_orphan_probe_policy.orphan_probe_policy")
        assert any("iam:TagRole does not support condition key" in p for p in row["problems"]), \
            row["problems"]
    finally:
        new_module.unlink()
        sys.modules.pop("gen_orphan_probe_policy", None)
        importlib.invalidate_caches()


def test_a_temporary_policy_without_an_expiry_is_reported(tmp_path):
    """Temporary artifacts must carry a real expiry on every Allow.

    Exercised through a real probe MODULE rather than a monkeypatched attribute: discovery
    deliberately ignores callables not defined in the generator module (so an imported helper
    is not mistaken for a policy), which means a patched-in closure is invisible to it. That
    filter is correct; the test has to go through the same door a real generator would.
    """
    probe = SCRIPTS / "gen_expiryless_probe_policy.py"
    assert not probe.exists()
    probe.write_text(
        '"""Temporary probe generator created by test_policy_inventory."""\n'
        "def expiryless_probe_policy(expiry):\n"
        "    return {'Version': '2012-10-17', 'Statement': [\n"
        "        {'Sid': 'NoExpiry', 'Effect': 'Allow', 'Action': 'iam:GetRole',\n"
        "         'Resource': 'arn:aws:iam::111122223333:role/x'}]}\n",
        encoding="utf-8")
    try:
        importlib.invalidate_caches()
        result = policy_inventory.validate_all()
        row = next(r for r in result["rows"]
                   if r["policy"] == "gen_expiryless_probe_policy.expiryless_probe_policy")
        assert row["result"] == "INVALID"
        assert any("no expiry" in p for p in row["problems"]), row["problems"]
        assert not result["clean"]
    finally:
        probe.unlink()
        sys.modules.pop("gen_expiryless_probe_policy", None)
        importlib.invalidate_caches()


def test_a_temporary_policy_with_a_malformed_expiry_is_reported(tmp_path):
    probe = SCRIPTS / "gen_badexpiry_probe_policy.py"
    assert not probe.exists()
    probe.write_text(
        '"""Temporary probe generator created by test_policy_inventory."""\n'
        "def badexpiry_probe_policy(expiry):\n"
        "    return {'Version': '2012-10-17', 'Statement': [\n"
        "        {'Sid': 'BadExpiry', 'Effect': 'Allow', 'Action': 'iam:GetRole',\n"
        "         'Resource': 'arn:aws:iam::111122223333:role/x',\n"
        "         'Condition': {'DateLessThan': {'aws:CurrentTime': 'not-a-timestamp'}}}]}\n",
        encoding="utf-8")
    try:
        importlib.invalidate_caches()
        result = policy_inventory.validate_all()
        row = next(r for r in result["rows"]
                   if r["policy"] == "gen_badexpiry_probe_policy.badexpiry_probe_policy")
        assert row["result"] == "INVALID", row
        assert not result["clean"]
    finally:
        probe.unlink()
        sys.modules.pop("gen_badexpiry_probe_policy", None)
        importlib.invalidate_caches()


# --- PHASE L: a shipped statement must never equal a known-broken fixture ----------------

KNOWN_BROKEN = {
    "tagrole_conditioned_on_permissions_boundary": {
        "Effect": "Allow", "Action": "iam:TagRole",
        "_why": "iam:TagRole does not support iam:PermissionsBoundary; the grant is dead. "
                "Gate 4N-I10 shipped exactly this shape while a negative-control test used "
                "it as its broken mutant.",
        "_match": lambda s: ("iam:TagRole" in iam_eval._as_list(s.get("Action"))
                             and "iam:PermissionsBoundary" in json.dumps(
                                 s.get("Condition") or {})),
    },
    "unconditioned_createrole": {
        "_why": "CreateRole with no iam:PermissionsBoundary condition mints an unbounded "
                "successor — Gate 4N-H4 BR-2.",
        "_match": lambda s: (s.get("Effect") == "Allow"
                             and "iam:CreateRole" in iam_eval._as_list(s.get("Action"))
                             and "iam:PermissionsBoundary" not in json.dumps(
                                 s.get("Condition") or {})),
    },
    "wildcard_resource_role_authoring": {
        "_why": "role authoring on Resource '*' is account-wide role administration.",
        "_match": lambda s: (s.get("Effect") == "Allow"
                             and s.get("Resource") == "*"
                             and any(a in iam_eval._as_list(s.get("Action"))
                                     for a in ("iam:CreateRole", "iam:PutRolePolicy",
                                               "iam:UpdateAssumeRolePolicy"))),
    },
}


@pytest.mark.parametrize("fixture", sorted(KNOWN_BROKEN))
def test_no_generated_statement_matches_a_known_broken_fixture(fixture):
    """Gate 4N-I10 shipped the mutant its own negative control was written against."""
    match = KNOWN_BROKEN[fixture]["_match"]
    offenders = []
    for key, entry in sorted(policy_inventory.discover().items()):
        if "document" not in entry:
            continue
        for statement in entry["document"]["Statement"]:
            if match(statement):
                offenders.append(f"{key}/{statement.get('Sid')}")
    assert not offenders, (
        f"a shipped statement matches known-broken fixture {fixture!r} "
        f"({KNOWN_BROKEN[fixture]['_why']}): {offenders}")


def test_the_known_broken_fixtures_actually_match_something_broken():
    """Controls the control: a fixture that matches nothing would pass forever."""
    broken = {"Effect": "Allow", "Action": "iam:TagRole", "Resource": "*",
              "Condition": {"StringEquals": {"iam:PermissionsBoundary": "arn:aws:iam::1:policy/b"}}}
    assert KNOWN_BROKEN["tagrole_conditioned_on_permissions_boundary"]["_match"](broken)
    assert KNOWN_BROKEN["wildcard_resource_role_authoring"]["_match"](
        {"Effect": "Allow", "Action": "iam:CreateRole", "Resource": "*"})
    assert KNOWN_BROKEN["unconditioned_createrole"]["_match"](
        {"Effect": "Allow", "Action": "iam:CreateRole", "Resource": "arn:aws:iam::1:role/x"})
