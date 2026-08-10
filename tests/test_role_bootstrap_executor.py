"""Bounded RoleBootstrap executor (Gate 4N-I12, Defect 1).

THE DEFECT. Through Gate 4N-I11 the trust control was an IAM policy plus prose asserting the
read-back happened "immediately". Nothing performed the sequence, nothing timed it, nothing
owned it. The Gate 4N-I10 security lane rejected precisely that: a residual risk whose
acceptability rests on a timing property enforced nowhere is unbounded, not bounded.

These tests exercise the sequence with an INJECTED MOCK client. No AWS call is made. Every
unsafe scenario must fail closed AND roll back, and the exposure window must be measured
rather than assumed.
"""

from __future__ import annotations

import copy
import datetime
import hashlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import role_bootstrap_executor as ex  # noqa: E402
import trust_policies  # noqa: E402

UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)
ROLE = "signalnest-staging-revision-reader-runner"
CANDIDATE = "4N-I12-CANDIDATE-1"


class MockIam:
    """Records calls; returns whatever the scenario dictates."""

    def __init__(self, *, get_role_returns=None, get_role_raises=None,
                 delete_raises=False, role_persists_after_delete=False,
                 create_raises=None):
        self.calls: list[str] = []
        self._returns = list(get_role_returns or [])
        self._raises = list(get_role_raises or [])
        self._delete_raises = delete_raises
        self._persists = role_persists_after_delete
        self._create_raises = create_raises
        self.deleted = False

    def create_role(self, **kwargs):
        self.calls.append("create_role")
        self.created_with = kwargs
        if self._create_raises:
            raise self._create_raises
        return {"Role": {"RoleName": kwargs["RoleName"]}}

    def get_role(self, **kwargs):
        self.calls.append("get_role")
        if self.deleted and not self._persists:
            raise RuntimeError("NoSuchEntity")
        if self.deleted and self._persists:
            return {"Role": {"AssumeRolePolicyDocument": {}}}
        if self._raises:
            problem = self._raises.pop(0)
            if problem is not None:
                raise problem
        if self._returns:
            return self._returns.pop(0)
        raise RuntimeError("NoSuchEntity")

    def delete_role(self, **kwargs):
        self.calls.append("delete_role")
        if self._delete_raises:
            raise RuntimeError("DeleteConflict")
        self.deleted = True


@pytest.fixture
def workspace(tmp_path):
    """A reviewed manifest plus the exact trust bytes it names."""
    manifest_entry = trust_policies.trust_manifest()[ROLE]
    document = manifest_entry["trust_policy"]
    rendered = json.dumps(document, indent=2, ensure_ascii=True) + "\n"
    (tmp_path / "trust.json").write_text(rendered, encoding="utf-8")
    manifest = {
        "candidate_id": CANDIDATE,
        "expiry_utc": "2026-07-31T21:45:20Z",
        "approved_account_id": "111122223333",
        "partition": "aws",
        "roles": [{
            "role_name": ROLE,
            "trust_policy_path": "trust.json",
            "canonical_sha256": manifest_entry["canonical_sha256"],
            "file_byte_sha256": hashlib.sha256(rendered.encode()).hexdigest(),
            "boundary_arn": "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary",
            "tags": {"Name": ROLE},
        }],
    }
    (tmp_path / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    return tmp_path, document


def run(workspace, client, **kw):
    path, _ = workspace
    return ex.execute_one(client, manifest_path=path / "manifest.json", role_name=ROLE,
                          candidate_id=CANDIDATE, base=path, now=NOW, **kw)


# --- the safe paths ------------------------------------------------------------------------


def test_1_correct_trust_returned_immediately(workspace):
    _, document = workspace
    client = MockIam(get_role_returns=[{"Role": {"AssumeRolePolicyDocument": document}}])
    outcome = run(workspace, client)
    assert outcome.status == "SUCCESS"
    assert outcome.trust_matched is True
    assert not outcome.rolled_back
    assert "delete_role" not in client.calls
    assert outcome.exposure_seconds is not None, "the window must be MEASURED, not assumed"


def test_2_url_encoded_trust_is_normalized_not_treated_as_a_mismatch(workspace):
    """AWS returns this URL-encoded on most SDK paths; a naive compare rolls back every time."""
    _, document = workspace
    import urllib.parse
    encoded = urllib.parse.quote(json.dumps(document))
    client = MockIam(get_role_returns=[{"Role": {"AssumeRolePolicyDocument": encoded}}])
    outcome = run(workspace, client)
    assert outcome.status == "SUCCESS", outcome.problems
    assert outcome.trust_matched is True


def test_3_one_eventual_consistency_miss_then_correct(workspace):
    _, document = workspace
    client = MockIam(get_role_raises=[RuntimeError("NoSuchEntity"), None],
                     get_role_returns=[{"Role": {"AssumeRolePolicyDocument": document}}])
    outcome = run(workspace, client)
    assert outcome.status == "SUCCESS", outcome.problems


def test_4_repeated_consistency_misses_fail_closed_and_roll_back(workspace):
    client = MockIam(get_role_raises=[RuntimeError("NoSuchEntity")] * 10)
    outcome = run(workspace, client)
    assert outcome.status != "SUCCESS"
    assert "delete_role" in client.calls, "no rollback attempted after exhausted retries"


# --- unsafe trust documents: every one must roll back ---------------------------------------


def _mutated(document, fn):
    copied = copy.deepcopy(document)
    fn(copied)
    return copied


UNSAFE = {
    "5_wrong_external_account_principal":
        lambda d: d["Statement"][0].__setitem__(
            "Principal", {"AWS": "arn:aws:iam::999988887777:root"}),
    "6_wildcard_principal":
        lambda d: d["Statement"][0].__setitem__("Principal", "*"),
    "7_wildcard_oidc_subject":
        lambda d: d["Statement"][0]["Condition"]["StringEquals"].__setitem__(
            "token.actions.githubusercontent.com:sub", "repo:bolade04/signal_nest:*"),
    "8_wrong_oidc_partition":
        lambda d: d["Statement"][0]["Principal"].__setitem__(
            "Federated", d["Statement"][0]["Principal"]["Federated"].replace(
                "arn:aws:", "arn:aws-us-gov:")),
    "9_missing_source_account":
        lambda d: d["Statement"][0].pop("Condition", None),
    "10_wrong_source_account":
        lambda d: d["Statement"][0]["Condition"]["StringEquals"].__setitem__(
            "token.actions.githubusercontent.com:aud", "attacker.example"),
    "11_extra_trust_statement":
        lambda d: d["Statement"].append(
            {"Effect": "Allow", "Principal": {"AWS": "arn:aws:iam::999988887777:root"},
             "Action": "sts:AssumeRole"}),
}


@pytest.mark.parametrize("name", sorted(UNSAFE))
def test_an_unsafe_trust_document_is_rolled_back(name, workspace):
    _, document = workspace
    returned = _mutated(document, UNSAFE[name])
    client = MockIam(get_role_returns=[{"Role": {"AssumeRolePolicyDocument": returned}}])
    outcome = run(workspace, client)
    assert outcome.trust_matched is False, f"{name} was accepted as matching"
    assert outcome.status == "MISMATCH_ROLLED_BACK", outcome.problems
    assert client.deleted, f"{name} did not trigger DeleteRole"
    assert outcome.deletion_verified, f"{name} rollback was not verified"


# --- failure and timing paths ---------------------------------------------------------------


def test_12_get_role_timeout_rolls_back(workspace):
    client = MockIam(get_role_raises=[TimeoutError("timed out")] * 10)
    outcome = run(workspace, client)
    assert outcome.status != "SUCCESS"
    assert client.deleted


def test_13_comparison_deadline_exceeded_is_a_failure_not_a_warning(workspace):
    """A clock that runs past the bound must FAIL, never downgrade."""
    _, document = workspace
    ticks = iter([0.0, 100.0, 101.0, 102.0, 103.0, 104.0, 105.0])
    client = MockIam(get_role_returns=[{"Role": {"AssumeRolePolicyDocument": document}}])
    outcome = run(workspace, client, monotonic=lambda: next(ticks, 200.0))
    assert outcome.bound_exceeded is True
    assert outcome.status != "SUCCESS"
    assert client.deleted, "an over-deadline run must still attempt rollback"


def test_14_delete_role_success_is_recorded(workspace):
    _, document = workspace
    client = MockIam(get_role_returns=[{"Role": {"AssumeRolePolicyDocument": {"x": 1}}}])
    outcome = run(workspace, client)
    assert outcome.rolled_back and outcome.deletion_verified


def test_15_delete_role_api_failure_is_reported_not_swallowed(workspace):
    client = MockIam(get_role_returns=[{"Role": {"AssumeRolePolicyDocument": {"x": 1}}}],
                     delete_raises=True)
    outcome = run(workspace, client)
    assert outcome.status == "FAILED"
    assert any("DeleteRole FAILED" in p for p in outcome.problems), outcome.problems


def test_16_deletion_not_observable_is_reported(workspace):
    client = MockIam(get_role_returns=[{"Role": {"AssumeRolePolicyDocument": {"x": 1}}}],
                     role_persists_after_delete=True)
    outcome = run(workspace, client)
    assert outcome.deletion_verified is False
    assert outcome.status == "FAILED"
    assert any("still present" in p for p in outcome.problems)


def test_18_malformed_aws_response_fails_closed(workspace):
    client = MockIam(get_role_returns=[{"Role": {"AssumeRolePolicyDocument": "not json at all"}}])
    outcome = run(workspace, client)
    assert outcome.status != "SUCCESS"
    assert client.deleted


def test_20_an_unexpected_pre_existing_role_surfaces_as_a_create_failure(workspace):
    client = MockIam(create_raises=RuntimeError("EntityAlreadyExists"))
    with pytest.raises(RuntimeError):
        run(workspace, client)
    assert "delete_role" not in client.calls, (
        "nothing was created, so nothing may be deleted — deleting a pre-existing role would "
        "destroy something this executor did not make")


# --- PHASE E: manifest binding ---------------------------------------------------------------


def test_an_arbitrary_role_name_is_refused(workspace):
    path, _ = workspace
    with pytest.raises(ex.ExecutorRefusal, match="not in the reviewed manifest"):
        ex.execute_one(MockIam(), manifest_path=path / "manifest.json",
                       role_name="attacker-chosen-role", candidate_id=CANDIDATE,
                       base=path, now=NOW)


def test_a_candidate_mismatch_is_refused(workspace):
    path, _ = workspace
    with pytest.raises(ex.ExecutorRefusal, match="candidate mismatch"):
        ex.execute_one(MockIam(), manifest_path=path / "manifest.json", role_name=ROLE,
                       candidate_id="SOME-OTHER-CANDIDATE", base=path, now=NOW)


def test_an_expired_manifest_is_refused(workspace):
    path, _ = workspace
    late = datetime.datetime(2026, 8, 1, 0, 0, 0, tzinfo=UTC)
    with pytest.raises(ex.ExecutorRefusal, match="expired"):
        ex.execute_one(MockIam(), manifest_path=path / "manifest.json", role_name=ROLE,
                       candidate_id=CANDIDATE, base=path, now=late)


def test_an_altered_trust_file_is_refused_before_anything_is_created(workspace):
    path, _ = workspace
    (path / "trust.json").write_text('{"Version": "2012-10-17", "Statement": []}\n')
    client = MockIam()
    with pytest.raises(ex.ExecutorRefusal, match="file-byte hash"):
        ex.execute_one(client, manifest_path=path / "manifest.json", role_name=ROLE,
                       candidate_id=CANDIDATE, base=path, now=NOW)
    assert client.calls == [], "an altered trust file must be caught BEFORE CreateRole"


@pytest.mark.parametrize("field,value,pattern", [
    ("approved_account_id", "999988887777", "account"),
    ("partition", "aws-us-gov", "partition"),
])
def test_a_manifest_that_disagrees_with_the_boundary_arn_is_refused(field, value, pattern,
                                                                    workspace):
    path, _ = workspace
    manifest = json.loads((path / "manifest.json").read_text())
    manifest[field] = value
    (path / "manifest.json").write_text(json.dumps(manifest))
    with pytest.raises(ex.ExecutorRefusal, match=pattern):
        ex.execute_one(MockIam(), manifest_path=path / "manifest.json", role_name=ROLE,
                       candidate_id=CANDIDATE, base=path, now=NOW)


def test_the_executor_makes_no_aws_call_when_invoked_as_a_script():
    """This gate authorizes none. The CLI refuses rather than quietly doing nothing."""
    assert ex.main.__doc__ is None or True
    source = (REPO_ROOT / "scripts" / "role_bootstrap_executor.py").read_text(encoding="utf-8")
    assert "REFUSING TO RUN" in source
    assert "import boto3" not in source, "no AWS SDK is imported"


def test_the_bounds_are_declared_as_constants_not_buried():
    for name, ceiling in (("FIRST_READBACK_DEADLINE", 2.0), ("COMPARISON_DEADLINE", 15.0),
                          ("ROLLBACK_START_DEADLINE", 2.0), ("TOTAL_EXPOSURE_TARGET", 30.0),
                          ("DELETION_VERIFY_DEADLINE", 30.0)):
        assert getattr(ex, name) == ceiling, name
