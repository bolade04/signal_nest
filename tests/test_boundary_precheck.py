"""Offline tests for the Stage-A permissions-boundary precheck (P6-INF-15).

Never calls AWS. `evaluate()` is pure; the AWS CLI client is exercised only through an
injected runner that records the command it would have run, and `main()` only through an
injected client. IAM responses are STUBS in the documented GetPolicy / GetPolicyVersion
response shapes (IAM API Reference), not recordings: no live read has been made. Expected
identity comes from the declared (synthetic, Tier 1) anchor through scripts/signalnest_identity.py,
never from a literal here.
"""

from __future__ import annotations

import copy
import json
import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import action_classifier  # noqa: E402
import boundary_precheck as bp  # noqa: E402
import gen_boundary_policy as gb  # noqa: E402
import signalnest_identity  # noqa: E402

EXPECTED = bp.expected_identity()
ARN = EXPECTED["boundary_policy_arn"]
DOCUMENT = gb.boundary_policy()

GET_POLICY = {"Policy": {
    "PolicyName": EXPECTED["boundary_policy_name"], "Arn": ARN,
    "Path": EXPECTED["boundary_policy_path"], "DefaultVersionId": "v1", "AttachmentCount": 0,
    "PermissionsBoundaryUsageCount": 8, "IsAttachable": True,
    "CreateDate": "2026-07-31T00:00:00+00:00", "UpdateDate": "2026-07-31T00:00:00+00:00"}}
GET_POLICY_VERSION = {"PolicyVersion": {
    "Document": copy.deepcopy(DOCUMENT), "VersionId": "v1", "IsDefaultVersion": True,
    "CreateDate": "2026-07-31T00:00:00+00:00"}}
GOOD_INVENTORY = {"policy": GET_POLICY["Policy"], "policy_version": GET_POLICY_VERSION["PolicyVersion"],
                  "errors": {}}


def _role_change(address: str, actions: list[str], boundary, unknown: bool = False) -> dict:
    return {"address": address, "mode": "managed", "type": "aws_iam_role",
            "change": {"actions": actions, "after": {"permissions_boundary": boundary},
                       "after_unknown": {"permissions_boundary": True} if unknown else {}}}


GOOD_PLAN = {
    "format_version": "1.2",
    "variables": {
        "role_boundary_mode": {"value": "required"},
        "role_permissions_boundary_arn": {"value": ARN},
        "enable_revision_reader_publication_bootstrap": {"value": True},
    },
    "resource_changes": [
        {"address": "module.revision_reader.aws_ecr_repository.reader[0]", "mode": "managed",
         "type": "aws_ecr_repository", "change": {"actions": ["create"], "after": {}}},
        _role_change("module.revision_reader.aws_iam_role.reader_publisher[0]", ["create"], ARN),
        _role_change("module.iam.aws_iam_role.api_task", ["no-op"], ARN),
    ],
}


def _write(tmp_path: Path, name: str, data) -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


class StubClient:
    """An injected client: returns stubbed responses and records every call."""

    def __init__(self, policy=GET_POLICY, version=GET_POLICY_VERSION, fail=None):
        self.calls: list[tuple] = []
        self._policy, self._version, self._fail = policy, version, fail or {}

    def get_policy(self, arn):
        self.calls.append(("iam:GetPolicy", arn))
        if "iam:GetPolicy" in self._fail:
            raise bp.ReadFailed(self._fail["iam:GetPolicy"])
        return copy.deepcopy(self._policy)

    def get_policy_version(self, arn, version_id):
        self.calls.append(("iam:GetPolicyVersion", arn, version_id))
        if "iam:GetPolicyVersion" in self._fail:
            raise bp.ReadFailed(self._fail["iam:GetPolicyVersion"])
        return copy.deepcopy(self._version)


# --- the clean case ----------------------------------------------------------------------

def test_the_reviewed_plan_and_an_existing_reviewed_boundary_pass():
    result = bp.evaluate(GOOD_PLAN, GOOD_INVENTORY, EXPECTED)
    assert result["clean"], result["findings"]
    assert result["observed"]["plan"]["iam_role_changes_checked"] == 1
    assert result["observed"]["iam"]["document_sha256"] == EXPECTED["repository_render_sha256"]


def test_expected_identity_comes_from_the_single_identity_source():
    assert EXPECTED["boundary_policy_arn"] == signalnest_identity.BOUNDARY_POLICY_ARN
    assert EXPECTED["boundary_policy_name"] == signalnest_identity.BOUNDARY_POLICY_NAME
    assert EXPECTED["boundary_policy_path"] == signalnest_identity.BOUNDARY_POLICY_PATH
    assert EXPECTED["document_sha256"] == bp.canonical_sha256(gb.boundary_policy())
    source = (REPO_ROOT / "scripts" / "boundary_precheck.py").read_text(encoding="utf-8")
    assert "role-boundary" not in source, "the precheck must not rebuild the boundary name"


def test_inventory_reads_are_read_only_by_the_independent_classifier():
    assert bp.INVENTORY_READS == ("iam:GetPolicy", "iam:GetPolicyVersion")
    for action in bp.INVENTORY_READS:
        assert action_classifier.is_read_only(action), action


# --- the plan: every deviation fails closed ----------------------------------------------

def _plan(mutate) -> dict:
    plan = copy.deepcopy(GOOD_PLAN)
    mutate(plan)
    return plan


@pytest.mark.parametrize("mutate,expect", [
    (lambda p: p["variables"]["role_boundary_mode"].__setitem__("value", "disabled"),
     "role_boundary_mode is 'disabled'"),
    (lambda p: p["variables"].pop("role_boundary_mode"), "role_boundary_mode is absent"),
    (lambda p: p["variables"]["role_boundary_mode"].pop("value"), "role_boundary_mode is absent"),
    (lambda p: p["variables"]["role_permissions_boundary_arn"].__setitem__("value", None),
     "is null"),
    (lambda p: p["variables"].pop("role_permissions_boundary_arn"),
     "role_permissions_boundary_arn is absent"),
    (lambda p: p["variables"]["role_permissions_boundary_arn"].__setitem__(
        "value", ARN.replace(":policy/", ":policy/other/")), "is not the reviewed boundary ARN"),
    (lambda p: p["variables"]["role_permissions_boundary_arn"].__setitem__(
        "value", ARN + "-v2"), "is not the reviewed boundary ARN"),
    (lambda p: p.pop("variables"), "no variables block"),
    (lambda p: p.pop("resource_changes"), "no resource_changes list"),
    (lambda p: p.__setitem__("errored", True), "marked errored"),
    (lambda p: p["resource_changes"].append(
        _role_change("module.iam.aws_iam_role.worker_task", ["update"], None)),
     "worker_task: planned permissions_boundary None"),
    (lambda p: p["resource_changes"].append(
        _role_change("module.x.aws_iam_role.y", ["create"], ARN + "x")), "is not the reviewed boundary"),
    (lambda p: p["resource_changes"].append(
        _role_change("module.x.aws_iam_role.y", ["delete", "create"], None, unknown=True)),
     "unknown at plan time"),
    # W21-REV-OPSPREP r1 MINOR-1: a malformed entry is refused, never skipped.
    (lambda p: p["resource_changes"].append(
        {"address": "module.x.aws_iam_role.no_actions", "mode": "managed", "type": "aws_iam_role",
         "change": {"after": {"permissions_boundary": None}}}),
     "module.x.aws_iam_role.no_actions: change/actions unreadable: refusing"),
    (lambda p: p["resource_changes"].append(
        {"address": "module.x.aws_iam_role.string_actions", "mode": "managed", "type": "aws_iam_role",
         "change": {"actions": "create", "after": {"permissions_boundary": None}}}),
     "module.x.aws_iam_role.string_actions: change/actions unreadable: refusing"),
    (lambda p: p["resource_changes"].append(
        {"address": "module.x.aws_iam_role.no_change", "mode": "managed", "type": "aws_iam_role"}),
     "module.x.aws_iam_role.no_change: change/actions unreadable: refusing"),
    (lambda p: p["resource_changes"].append("aws_iam_role.y"),
     "resource_changes[3] is not an object: refusing an unreadable plan"),
    (lambda p: p["resource_changes"].append(
        {"address": "module.x.aws_iam_role.empty_actions", "mode": "managed", "type": "aws_iam_role",
         "change": {"actions": [], "after": {}}}),
     "module.x.aws_iam_role.empty_actions: change/actions unreadable: refusing"),
    (lambda p: p["resource_changes"].append(
        {"address": "module.x.aws_iam_role.odd_actions", "mode": "managed", "type": "aws_iam_role",
         "change": {"actions": ["create", None], "after": {}}}),
     "module.x.aws_iam_role.odd_actions: change/actions unreadable: refusing"),
])
def test_each_plan_deviation_fails_closed(mutate, expect):
    result = bp.evaluate(_plan(mutate), GOOD_INVENTORY, EXPECTED)
    assert not result["clean"]
    assert any(expect in f for f in result["findings"]), result["findings"]


def test_a_non_object_plan_fails():
    assert not bp.evaluate([], GOOD_INVENTORY, EXPECTED)["clean"]


def test_role_deletions_and_no_ops_are_not_boundary_carriers():
    plan = _plan(lambda p: p["resource_changes"].append(
        _role_change("module.x.aws_iam_role.gone", ["delete"], None)))
    result = bp.evaluate(plan, GOOD_INVENTORY, EXPECTED)
    assert result["clean"], result["findings"]
    assert result["observed"]["plan"]["iam_role_changes_checked"] == 1


# --- the IAM inventory: every deviation fails closed -------------------------------------

def _inventory(mutate) -> dict:
    inventory = copy.deepcopy(GOOD_INVENTORY)
    mutate(inventory)
    return inventory


@pytest.mark.parametrize("mutate,expect", [
    (lambda i: i.update(policy=None, policy_version=None,
                        errors={"iam:GetPolicy": "An error occurred (NoSuchEntity) when calling "
                                                 "the GetPolicy operation: Policy not found"}),
     "does not exist (NoSuchEntity)"),
    (lambda i: i.update(policy=None, policy_version=None,
                        errors={"iam:GetPolicy": "An error occurred (AccessDenied)"}),
     "existence cannot be established"),
    (lambda i: i.update(policy_version=None,
                        errors={"iam:GetPolicyVersion": "An error occurred (AccessDenied)"}),
     "iam:GetPolicyVersion failed or was denied"),
    (lambda i: i.update(policy=None, policy_version=None), "no policy record"),
    (lambda i: i.update(policy_version=None), "no default-version record"),
    (lambda i: i["policy"].__setitem__("Arn", ARN + "-copy"), "policy Arn"),
    (lambda i: i["policy"].__setitem__("PolicyName", "other"), "policy name"),
    (lambda i: i["policy"].__setitem__("Path", "/other/"), "policy path"),
    (lambda i: i["policy"].__setitem__("IsAttachable", False), "IsAttachable"),
    (lambda i: i["policy"].pop("DefaultVersionId"), "no default version"),
    (lambda i: i["policy_version"].__setitem__("VersionId", "v2"), "is not the default version"),
    (lambda i: i["policy_version"].__setitem__("IsDefaultVersion", False), "IsDefaultVersion"),
    (lambda i: i["policy_version"]["Document"]["Statement"].pop(),
     "default version document sha256"),
    (lambda i: i["policy_version"].__setitem__("Document", "%7Bnot-json"), "not a JSON object"),
    (lambda i: i["policy_version"].pop("Document"), "not a JSON object"),
    (lambda i: i.__setitem__("errors", ["x"]), "errors field is not an object"),
])
def test_each_inventory_deviation_fails_closed(mutate, expect):
    result = bp.evaluate(GOOD_PLAN, _inventory(mutate), EXPECTED)
    assert not result["clean"]
    assert any(expect in f for f in result["findings"]), result["findings"]


def test_the_api_url_encoded_document_form_is_decoded_and_compared():
    encoded = urllib.parse.quote(json.dumps(DOCUMENT, indent=1))
    inventory = _inventory(lambda i: i["policy_version"].__setitem__("Document", encoded))
    assert bp.evaluate(GOOD_PLAN, inventory, EXPECTED)["clean"]
    changed = copy.deepcopy(DOCUMENT)
    changed["Statement"][0]["Sid"] = "Renamed"
    inventory["policy_version"]["Document"] = urllib.parse.quote(json.dumps(changed))
    assert not bp.evaluate(GOOD_PLAN, inventory, EXPECTED)["clean"]


def test_an_explicit_reviewed_digest_replaces_the_comparison_and_never_skips_it():
    other = "0" * 64
    expected = bp.expected_identity(other)
    assert expected["document_sha256"] == other
    result = bp.evaluate(GOOD_PLAN, GOOD_INVENTORY, expected)
    assert not result["clean"] and any("operator-supplied" in f for f in result["findings"])
    for bad in ("ABC", "0" * 63, "0" * 65, "g" * 64, "A" * 64):
        with pytest.raises(bp.PrecheckInputError):
            bp.expected_identity(bad)


# --- the client: read-only by construction ------------------------------------------------

class RecordingRunner:
    def __init__(self, responses):
        self.commands: list[list[str]] = []
        self._responses = list(responses)

    def __call__(self, command, **kwargs):
        assert kwargs.get("capture_output") is True and kwargs.get("timeout")
        self.commands.append(command)
        returncode, stdout, stderr = self._responses.pop(0)
        return subprocess.CompletedProcess(command, returncode, stdout, stderr)


def test_the_cli_client_issues_exactly_the_two_reads_in_order():
    runner = RecordingRunner([(0, json.dumps(GET_POLICY), ""), (0, json.dumps(GET_POLICY_VERSION), "")])
    inventory = bp.collect(bp.AwsCliIamReader("readonly", runner=runner), ARN)
    assert runner.commands == [
        ["aws", "iam", "get-policy", "--policy-arn", ARN, "--profile", "readonly", "--output", "json"],
        ["aws", "iam", "get-policy-version", "--policy-arn", ARN, "--version-id", "v1",
         "--profile", "readonly", "--output", "json"],
    ]
    # The report's INVENTORY_READS must name exactly what the client issued, in order.
    issued = tuple("iam:" + "".join(w.capitalize() for w in c[2].split("-")) for c in runner.commands)
    assert issued == bp.INVENTORY_READS
    assert bp.evaluate(GOOD_PLAN, inventory, EXPECTED)["clean"]


def test_the_client_has_no_call_path_but_the_two_reads():
    public = {name for name in vars(bp.AwsCliIamReader) if not name.startswith("_")}
    assert public == {"get_policy", "get_policy_version"}, public
    source = (REPO_ROOT / "scripts" / "boundary_precheck.py").read_text(encoding="utf-8")
    operations = set(re.findall(r'self\._run\("([a-z-]+)"', source))
    assert operations == {"get-policy", "get-policy-version"}, operations


@pytest.mark.parametrize("response,expect", [
    ((254, "", "An error occurred (NoSuchEntity) when calling the GetPolicy operation"),
     "does not exist (NoSuchEntity)"),
    ((255, "", "An error occurred (AccessDenied) when calling the GetPolicy operation"),
     "existence cannot be established"),
    ((0, "not json", ""), "(MalformedResponse (GetPolicy))"),
    ((0, "[]", ""), "(MalformedResponse (GetPolicy))"),
    ((0, '{"Policy": {}, "Policy": {}}', ""), "(MalformedResponse (GetPolicy))"),
    ((255, "", "Unable to locate credentials. You can configure credentials by running aws configure."),
     "(CliExit255 (GetPolicy))"),
    ((-9, "", ""), "(CliSignal9 (GetPolicy))"),
])
def test_a_failed_or_unusable_first_read_stops_before_the_second(response, expect):
    runner = RecordingRunner([response])
    inventory = bp.collect(bp.AwsCliIamReader("readonly", runner=runner), ARN)
    assert len(runner.commands) == 1, "GetPolicyVersion must not follow a failed GetPolicy"
    result = bp.evaluate(GOOD_PLAN, inventory, EXPECTED)
    assert not result["clean"]
    assert any(expect in f for f in result["findings"]), result["findings"]


@pytest.mark.parametrize("exc", [OSError("no aws"), subprocess.TimeoutExpired(["aws"], 90)])
def test_a_runner_exception_is_a_failed_read(exc):
    def runner(command, **kwargs):
        raise exc
    inventory = bp.collect(bp.AwsCliIamReader("readonly", runner=runner), ARN)
    assert "iam:GetPolicy" in inventory["errors"]
    assert not bp.evaluate(GOOD_PLAN, inventory, EXPECTED)["clean"]


def test_a_policy_without_a_default_version_makes_no_second_read():
    client = StubClient(policy={"Policy": {k: v for k, v in GET_POLICY["Policy"].items()
                                           if k != "DefaultVersionId"}})
    inventory = bp.collect(client, ARN)
    assert [c[0] for c in client.calls] == ["iam:GetPolicy"]
    assert not bp.evaluate(GOOD_PLAN, inventory, EXPECTED)["clean"]


# --- main(): inputs, tier gate, output ----------------------------------------------------

def test_main_offline_fixture_pass_and_fail(tmp_path, capsys):
    plan = _write(tmp_path, "plan.json", GOOD_PLAN)
    good = _write(tmp_path, "good.json", GOOD_INVENTORY)
    assert bp.main(["--plan-json", str(plan), "--fixture", str(good)]) == 0
    assert "BOUNDARY PRECHECK: PASS" in capsys.readouterr().out
    bad = _write(tmp_path, "bad.json", _inventory(lambda i: i.update(policy=None, policy_version=None)))
    assert bp.main(["--plan-json", str(plan), "--fixture", str(bad), "--json"]) == 1
    result = json.loads(capsys.readouterr().out)
    assert result["clean"] is False and result["inputs"]["inventory_source"] == "fixture"
    assert re.fullmatch(r"[0-9a-f]{64}", result["inputs"]["plan_json_sha256"])


def test_main_refuses_a_live_read_under_the_synthetic_tier_without_any_call(tmp_path, capsys):
    plan = _write(tmp_path, "plan.json", GOOD_PLAN)

    def factory(profile):
        raise AssertionError("no client may be built under the synthetic tier")
    assert EXPECTED["anchor_tier"] != bp.PROTECTED_TIER
    assert bp.main(["--plan-json", str(plan), "--profile", "readonly"], client_factory=factory) == 1
    assert "requires SIGNALNEST_ANCHOR_TIER=TIER_2_PROTECTED" in capsys.readouterr().err


def test_main_live_path_uses_the_injected_client(tmp_path, monkeypatch, capsys):
    plan = _write(tmp_path, "plan.json", GOOD_PLAN)
    protected = dict(EXPECTED, anchor_tier=bp.PROTECTED_TIER)
    monkeypatch.setattr(bp, "expected_identity", lambda digest=None: protected)
    client = StubClient()
    assert bp.main(["--plan-json", str(plan), "--profile", "readonly"],
                   client_factory=lambda profile: client) == 0
    assert [c[0] for c in client.calls] == ["iam:GetPolicy", "iam:GetPolicyVersion"]
    assert "PASS" in capsys.readouterr().out
    failing = StubClient(fail={"iam:GetPolicy": "NoSuchEntity (GetPolicy)"})
    assert bp.main(["--plan-json", str(plan), "--profile", "readonly"],
                   client_factory=lambda profile: failing) == 1
    assert "NoSuchEntity" in capsys.readouterr().out


@pytest.mark.parametrize("content", ["{", '{"variables": {}, "variables": {}}', "\udcff"])
def test_main_refuses_unreadable_or_ambiguous_plan_input(tmp_path, capsys, content):
    plan = tmp_path / "plan.json"
    plan.write_bytes(content.encode("utf-8", "surrogateescape"))
    good = _write(tmp_path, "good.json", GOOD_INVENTORY)
    assert bp.main(["--plan-json", str(plan), "--fixture", str(good)]) == 1
    assert "refused" in capsys.readouterr().err


def test_main_refuses_a_missing_input_file(tmp_path, capsys):
    good = _write(tmp_path, "good.json", GOOD_INVENTORY)
    assert bp.main(["--plan-json", str(tmp_path / "absent.json"), "--fixture", str(good)]) == 1
    assert "refused" in capsys.readouterr().err


def test_main_refuses_an_undeclared_anchor_tier(tmp_path, monkeypatch, capsys):
    plan = _write(tmp_path, "plan.json", GOOD_PLAN)
    good = _write(tmp_path, "good.json", GOOD_INVENTORY)

    def undeclared(digest=None):
        raise RuntimeError("SIGNALNEST_ANCHOR_TIER is not set")
    monkeypatch.setattr(bp, "expected_identity", undeclared)
    assert bp.main(["--plan-json", str(plan), "--fixture", str(good)]) == 1
    assert "expected identity unavailable" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["--plan-json", "p.json"],
                                  ["--plan-json", "p", "--profile", "x", "--fixture", "f"]])
def test_main_requires_a_plan_and_exactly_one_inventory_source(argv):
    with pytest.raises(SystemExit) as raised:
        bp.main(argv, client_factory=StubClient)
    assert raised.value.code != 0


def test_output_never_prints_an_account_id(tmp_path, capsys):
    account = signalnest_identity.ACCOUNT
    assert re.fullmatch(r"[0-9]{12}", account)
    plan = _write(tmp_path, "plan.json", _plan(
        lambda p: p["variables"]["role_permissions_boundary_arn"].__setitem__("value", ARN + "x")))
    bad = _write(tmp_path, "bad.json", _inventory(lambda i: i["policy"].__setitem__("Arn", ARN + "x")))
    for extra in ([], ["--json"]):
        assert bp.main(["--plan-json", str(plan), "--fixture", str(bad), *extra]) == 1
        out = capsys.readouterr()
        assert account not in out.out + out.err
        assert "<account>" in out.out
    assert bp.redact(f"arn:aws:iam::{account}:policy/p") == "arn:aws:iam::<account>:policy/p"
    digest = "a" + account + "b" * 51
    assert bp.redact(digest) == digest, "a digest that contains twelve digits is not an account id"


# --- W21-REV-OPSPREP r1 MINOR-2: a failed read never prints the caller's identity -----------

SSO_ROLE = "AWSReservedSSO_ReadOnly_0123456789abcdef"
SESSION = "first.last@example.invalid"


def _main_with_cli_stderr(tmp_path, monkeypatch, stderr_text, extra=()):
    """Drive main() through the REAL client with a runner that fails the first read."""
    plan = _write(tmp_path, "plan.json", GOOD_PLAN)
    protected = dict(EXPECTED, anchor_tier=bp.PROTECTED_TIER)
    monkeypatch.setattr(bp, "expected_identity", lambda digest=None: protected)
    runner = RecordingRunner([(255, "", stderr_text)])
    code = bp.main(["--plan-json", str(plan), "--profile", "readonly", *extra],
                   client_factory=lambda profile: bp.AwsCliIamReader(profile, runner=runner))
    return code, runner


@pytest.mark.parametrize("extra", [[], ["--json"]])
def test_an_access_denied_read_prints_only_its_code_and_operation(tmp_path, monkeypatch, capsys, extra):
    account = signalnest_identity.ACCOUNT
    denied = (f"An error occurred (AccessDenied) when calling the GetPolicy operation: User: "
              f"arn:aws:sts::{account}:assumed-role/{SSO_ROLE}/{SESSION} is not authorized to "
              f"perform: iam:GetPolicy on resource: policy {ARN} because no identity-based policy "
              f"allows the iam:GetPolicy action")
    code, runner = _main_with_cli_stderr(tmp_path, monkeypatch, denied, extra)
    out = capsys.readouterr()
    text = out.out + out.err
    assert code == 1 and len(runner.commands) == 1
    assert "AccessDenied (GetPolicy)" in text
    for leaked in (SESSION, "0123456789abcdef", SSO_ROLE, "assumed-role", "@", account):
        assert leaked not in text, leaked


def test_an_account_id_straddling_a_400_character_cut_leaves_no_digits(tmp_path, monkeypatch, capsys):
    account = signalnest_identity.ACCOUNT
    lead = "An error occurred (AccessDenied) when calling the GetPolicy operation: "
    arn_head = "arn:aws:iam::"
    straddle = lead + "y" * (393 - len(lead) - len(arn_head)) + arn_head + account + ":policy/p more"
    assert straddle.index(account) == 393  # its first 7 digits fall before a 400-character cut
    code, _ = _main_with_cli_stderr(tmp_path, monkeypatch, straddle, ["--json"])
    out = capsys.readouterr()
    text = out.out + out.err
    assert code == 1
    for start in range(len(account) - 6):
        assert account[start:start + 7] not in text, "a 7-digit run of the account survived"


def test_the_client_itself_keeps_no_cli_text():
    """The first layer: the inventory the client builds holds only "<Code> (<Operation>)".

    evaluate_inventory() re-summarises as a second layer, so the printed output alone cannot
    show that the client discards the CLI text; this pins the client on its own.
    """
    denied = (f"An error occurred (AccessDenied) when calling the GetPolicy operation: User: "
              f"arn:aws:sts::{signalnest_identity.ACCOUNT}:assumed-role/{SSO_ROLE}/{SESSION} "
              "is not authorized")
    inventory = bp.collect(bp.AwsCliIamReader("readonly", runner=RecordingRunner([(255, "", denied)])), ARN)
    assert inventory["errors"] == {"iam:GetPolicy": "AccessDenied (GetPolicy)"}


@pytest.mark.parametrize("message,expect", [
    ("AccessDenied (GetPolicy)", "AccessDenied (GetPolicy)"),
    ("An error occurred (NoSuchEntity) when calling the GetPolicyVersion operation: x@y.example",
     "NoSuchEntity (GetPolicyVersion)"),
    (f"User: arn:aws:sts::{signalnest_identity.ACCOUNT}:assumed-role/{SSO_ROLE}/{SESSION} denied",
     "unrecognised error text withheld"),
    ("AccessDenied (GetPolicy) for " + SESSION, "unrecognised error text withheld"),
    ("", "unrecognised error text withheld"),
])
def test_error_summary_admits_only_code_and_operation(message, expect):
    assert bp.error_summary(message) == expect


def test_a_fixture_carrying_raw_cli_text_cannot_leak_it(tmp_path, capsys):
    account = signalnest_identity.ACCOUNT
    raw = (f"An error occurred (AccessDenied) when calling the GetPolicy operation: User: "
           f"arn:aws:sts::{account}:assumed-role/{SSO_ROLE}/{SESSION} is not authorized")
    plan = _write(tmp_path, "plan.json", GOOD_PLAN)
    bad = _write(tmp_path, "bad.json", {"policy": None, "policy_version": None,
                                        "errors": {"iam:GetPolicy": raw, SESSION: "x"}})
    assert bp.main(["--plan-json", str(plan), "--fixture", str(bad), "--json"]) == 1
    text = capsys.readouterr().out
    assert "AccessDenied (GetPolicy)" in text and "an unexpected error entry" in text
    for leaked in (SESSION, "0123456789abcdef", "assumed-role", "@", account):
        assert leaked not in text, leaked
