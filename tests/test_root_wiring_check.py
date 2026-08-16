"""INFRA-9 B-3: unit and behavioural-mutation tests for scripts/root_wiring_check.py.

THE DEFECT CLASS. Deleting `module.revision_reader`'s providers map, or adding
default_tags / assume_role / ignore_tags / profile / credential / account / endpoint
routing to the aliased `aws.revision_reader` provider block, is valid HCL that
`tofu validate` accepts — and it silently reintroduces the exact TagRole drift the B-2
Stage-A barrier refused (2026-08-15). The guard uses OpenTofu itself as the
configuration oracle; these tests prove the guard's own decision logic fails closed on
every defect class WITHOUT running OpenTofu.

Layering: everything here is hermetic — synthetic plan-JSON documents, synthetic DOT
text, a loopback-only stub exercise, and pure-function checks. The tofu-executing half
of the control (positive control P1 + the complete negative mutation battery against
the real configuration) runs in the graded CI step `root_wiring` via
`python3 scripts/root_wiring_check.py --mode full`; nothing in this file reads the
physical host's toolchain or ambient AWS state (Gate 4N-I28BH-E6 lesson).

No AWS access; the only socket is a 127.0.0.1 ephemeral-port loopback to the guard's
own synthetic stub.
"""

from __future__ import annotations

import copy
import json
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import root_wiring_check as rwc  # noqa: E402

CONTRACT = json.loads((REPO_ROOT / "tests" / "fixtures" / "root-wiring-contract.json").read_text(encoding="utf-8"))
TFVARS_TEXT = (REPO_ROOT / "tests" / "fixtures" / "root-wiring-synthetic.tfvars.example").read_text(encoding="utf-8")

ALIAS_KEY = CONTRACT["alias_key"]
READER_PREFIX = f"module.{CONTRACT['reader_module_name']}."


# --------------------------------------------------------------------------- #
# synthetic plan-JSON builder (shaped exactly like `tofu show -json` output,
# proven against tofu 1.12.5 / provider aws 6.55.0 on 2026-08-15)
# --------------------------------------------------------------------------- #

def make_plan(**overrides) -> dict:
    reader_resources = [
        {
            "address": row["address"][len(READER_PREFIX):],
            "mode": row["mode"],
            "type": row["address"].split(".")[-2],
            "provider_config_key": ALIAS_KEY,
        }
        for row in CONTRACT["reader_resource_roster"]
    ]
    plan = {
        "format_version": CONTRACT["plan_json_format_versions"][0],
        "configuration": {
            "provider_config": {
                "aws": {
                    "name": "aws",
                    "expressions": {
                        "default_tags": [{"tags": {"references": ["local.common_tags"]}}],
                        "region": {"references": ["var.aws_region"]},
                    },
                },
                ALIAS_KEY: {
                    "name": "aws",
                    "alias": CONTRACT["alias_name"],
                    "expressions": {"region": {"references": ["var.aws_region"]}},
                },
            },
            "root_module": {
                "resources": [],
                "module_calls": {
                    CONTRACT["reader_module_name"]: {
                        "source": CONTRACT["reader_module_source"],
                        "module": {"resources": reader_resources},
                    },
                    "network": {
                        "source": "./modules/network",
                        "module": {
                            "resources": [
                                {
                                    "address": "aws_vpc.this",
                                    "mode": "managed",
                                    "type": "aws_vpc",
                                    "provider_config_key": "aws",
                                }
                            ]
                        },
                    },
                },
            },
        },
    }
    plan.update(overrides)
    return plan


def failures_for(plan: dict) -> list[str]:
    return rwc.assert_plan_json(plan, CONTRACT)


def test_positive_plan_json_has_no_failures():
    assert failures_for(make_plan()) == []


# ---- behavioural mutations over the plan-JSON decision logic ---------------- #

def _alias_cfg(plan):
    return plan["configuration"]["provider_config"][ALIAS_KEY]


def _reader_resources(plan):
    return plan["configuration"]["root_module"]["module_calls"][
        CONTRACT["reader_module_name"]
    ]["module"]["resources"]


def test_providers_map_removed_every_reader_row_flips_to_default():
    plan = make_plan()
    for resource in _reader_resources(plan):
        resource["provider_config_key"] = "aws"
    fails = failures_for(plan)
    assert any("bound to provider key 'aws': 15/15" in f for f in fails)


def test_providers_map_rerouted_to_another_alias():
    plan = make_plan()
    for resource in _reader_resources(plan):
        resource["provider_config_key"] = "aws.other"
    fails = failures_for(plan)
    assert any("'aws.other': 15/15" in f for f in fails)


def test_single_resource_fallback_is_not_averaged_away():
    plan = make_plan()
    _reader_resources(plan)[0]["provider_config_key"] = "aws"
    fails = failures_for(plan)
    assert any("'aws': 1/15" in f for f in fails)


def test_rogue_default_tags_on_alias_surface():
    plan = make_plan()
    _alias_cfg(plan)["expressions"]["default_tags"] = [{"tags": {"constant_value": {"X": "y"}}}]
    fails = failures_for(plan)
    assert any("argument surface ['default_tags', 'region'] != permitted ['region']" in f for f in fails)


@pytest.mark.parametrize("argument", [
    "assume_role", "ignore_tags", "profile", "access_key", "secret_key", "token",
    "endpoints", "allowed_account_ids", "shared_config_files", "shared_credentials_files",
    "skip_credentials_validation", "custom_ca_bundle", "http_proxy", "sts_region",
])
def test_any_extra_alias_argument_fails_exact_set(argument):
    plan = make_plan()
    _alias_cfg(plan)["expressions"][argument] = {"constant_value": "synthetic"}
    fails = failures_for(plan)
    assert any("argument surface" in f and argument in f for f in fails)


def test_alias_region_must_reference_var_aws_region_only():
    plan = make_plan()
    _alias_cfg(plan)["expressions"]["region"] = {"constant_value": "us-west-2"}
    fails = failures_for(plan)
    assert any("region references" in f for f in fails)


def test_alias_absent_fails():
    plan = make_plan()
    del plan["configuration"]["provider_config"][ALIAS_KEY]
    fails = failures_for(plan)
    assert any("is absent" in f for f in fails)


def test_default_provider_losing_default_tags_fails_inverse_regression():
    plan = make_plan()
    del plan["configuration"]["provider_config"]["aws"]["expressions"]["default_tags"]
    fails = failures_for(plan)
    assert any("default provider argument surface ['region']" in f for f in fails)


def test_shadow_aws_provider_config_expands_the_universe():
    plan = make_plan()
    plan["configuration"]["provider_config"]["aws.other"] = {
        "name": "aws",
        "alias": "other",
        "expressions": {"region": {"references": ["var.aws_region"]}},
    }
    fails = failures_for(plan)
    assert any("aws provider-config universe" in f for f in fails)


def test_module_level_aws_provider_config_expands_the_universe():
    plan = make_plan()
    plan["configuration"]["provider_config"]["module.revision_reader:aws"] = {
        "name": "aws",
        "expressions": {"region": {"constant_value": "us-east-1"}},
    }
    fails = failures_for(plan)
    assert any("aws provider-config universe" in f for f in fails)


def test_shadow_module_call_fails_exactly_one_rule():
    plan = make_plan()
    calls = plan["configuration"]["root_module"]["module_calls"]
    calls["revision_reader_shadow"] = copy.deepcopy(calls[CONTRACT["reader_module_name"]])
    fails = failures_for(plan)
    assert any("2 module calls source ./modules/revision_reader (expected 1)" in f for f in fails)


def test_zero_reader_module_calls_fails():
    plan = make_plan()
    del plan["configuration"]["root_module"]["module_calls"][CONTRACT["reader_module_name"]]
    fails = failures_for(plan)
    assert any("0 module calls source" in f for f in fails)


def test_new_unrostered_reader_resource_fails():
    plan = make_plan()
    _reader_resources(plan).append({
        "address": "aws_cloudwatch_log_group.synthetic_extra",
        "mode": "managed",
        "type": "aws_cloudwatch_log_group",
        "provider_config_key": ALIAS_KEY,
    })
    fails = failures_for(plan)
    assert any("roster mismatch" in f and "synthetic_extra" in f for f in fails)


def test_missing_rostered_reader_resource_fails():
    plan = make_plan()
    _reader_resources(plan).pop(0)
    fails = failures_for(plan)
    assert any("roster mismatch" in f and "missing:" in f for f in fails)


def test_alias_leaking_outside_the_reader_module_fails():
    plan = make_plan()
    plan["configuration"]["root_module"]["module_calls"]["network"]["module"]["resources"][0][
        "provider_config_key"
    ] = ALIAS_KEY
    fails = failures_for(plan)
    assert any("consumed outside the reader module" in f for f in fails)


def test_root_level_resource_on_alias_fails():
    plan = make_plan()
    plan["configuration"]["root_module"]["resources"] = [{
        "address": "aws_iam_role.rogue",
        "mode": "managed",
        "type": "aws_iam_role",
        "provider_config_key": ALIAS_KEY,
    }]
    fails = failures_for(plan)
    assert any("consumed outside the reader module" in f for f in fails)


# ---- fail-closed on format drift ------------------------------------------- #

def test_unknown_format_version_refuses_interpretation():
    fails = failures_for(make_plan(format_version="9.9"))
    assert len(fails) == 1 and "refusing to interpret drifted output" in fails[0]


def test_missing_configuration_refuses():
    fails = failures_for({"format_version": CONTRACT["plan_json_format_versions"][0]})
    assert fails and "no configuration.root_module" in fails[0]


def test_resource_without_provider_config_key_is_misbound_not_ignored():
    plan = make_plan()
    del _reader_resources(plan)[0]["provider_config_key"]
    fails = failures_for(plan)
    assert any("bound to provider key 'None': 1/15" in f for f in fails)


# --------------------------------------------------------------------------- #
# graph witness (DOT) decision logic
# --------------------------------------------------------------------------- #

PROVIDER_NODE = 'provider[\\"registry.opentofu.org/hashicorp/aws\\"]'
ALIAS_NODE = PROVIDER_NODE + ".revision_reader"


def make_dot(edges: list[tuple[str, str]]) -> str:
    lines = ["digraph {", '\tsubgraph "root" {']
    lines += [f'\t\t"[root] {a}" -> "[root] {b}"' for a, b in edges]
    lines += ["\t}", "}"]
    return "\n".join(lines)


BASE_EDGES = [
    ("module.revision_reader.aws_ecr_repository.reader (expand)", ALIAS_NODE),
    ("module.revision_reader.aws_iam_role.reader_runner (expand)", ALIAS_NODE),
    ("module.revision_reader.data.aws_caller_identity.current (expand)", ALIAS_NODE),
    (ALIAS_NODE, "var.aws_region (expand, reference)"),
    # the provider CLOSE node's outgoing resource edges — real DOT shape; these must
    # NOT pollute the configure node's G2 reference set (ADV-B3-F3)
    (ALIAS_NODE + " (close)", "module.revision_reader.aws_ecr_repository.reader (expand)"),
    (ALIAS_NODE + " (close)", "module.revision_reader.aws_iam_role.reader_runner (expand)"),
    ("module.network.aws_vpc.this (expand)", PROVIDER_NODE),
    (PROVIDER_NODE, "var.aws_region (expand, reference)"),
    ("root", ALIAS_NODE + " (close)"),
]


def graph_failures(edges) -> list[str]:
    fails, _targets = rwc.assert_graph(make_dot(edges), CONTRACT)
    return fails


def test_graph_positive_control_passes():
    assert graph_failures(BASE_EDGES) == []


def test_graph_parser_handles_root_prefix_suffixes_escapes_and_kinds():
    edges = rwc.graph_edges(make_dot(BASE_EDGES))
    assert (("module.network.aws_vpc.this", "expand"),
            ('provider["registry.opentofu.org/hashicorp/aws"]', "plain")) in edges
    assert (('provider["registry.opentofu.org/hashicorp/aws"].revision_reader', "plain"),
            ("var.aws_region", "expand, reference")) in edges
    # the close node keeps a DISTINCT kind so its edges never merge into the
    # configure node's reference set
    assert (('provider["registry.opentofu.org/hashicorp/aws"].revision_reader', "close"),
            ("module.revision_reader.aws_ecr_repository.reader", "expand")) in edges


def test_graph_reader_rerouted_to_default_provider_fails():
    edges = [
        (a.replace(ALIAS_NODE, PROVIDER_NODE) if b == ALIAS_NODE else a,
         b.replace(ALIAS_NODE, PROVIDER_NODE) if b == ALIAS_NODE else b)
        for a, b in BASE_EDGES
    ]
    fails = graph_failures(edges)
    assert any("reader resources reference aws provider nodes" in f or "alias" in f for f in fails)


def test_graph_alias_node_absent_fails():
    edges = [(a, b) for a, b in BASE_EDGES if ALIAS_NODE not in a and ALIAS_NODE not in b]
    fails = graph_failures(edges)
    assert any("aliased provider node" in f for f in fails)


@pytest.mark.parametrize("injected", [
    "local.common_tags (expand, reference)",
    # ADV-B3-F3: with node kinds preserved, G2 sees EVERY reference-valued
    # expression on the alias configure node, not only var./local. prefixes
    "data.aws_caller_identity.rogue (expand, reference)",
    "module.secrets.output.kms_key_arn (expand, reference)",
    "aws_s3_bucket.rogue (expand)",
])
def test_graph_reference_valued_injection_on_alias_fails(injected):
    edges = BASE_EDGES + [(ALIAS_NODE, injected)]
    fails = graph_failures(edges)
    assert any("aliased provider references" in f for f in fails)


def test_graph_close_node_edges_do_not_pollute_the_reference_set():
    # the close-node resource edges present in BASE_EDGES must be invisible to G2
    assert graph_failures(BASE_EDGES) == []


def test_graph_non_reader_consumer_of_alias_fails():
    edges = BASE_EDGES + [("module.ecs.aws_ecs_cluster.this (expand)", ALIAS_NODE)]
    fails = graph_failures(edges)
    assert any("non-reader nodes reference the alias" in f for f in fails)


def test_graph_zero_edges_refuses_drifted_output():
    fails, targets = rwc.assert_graph("digraph {}", CONTRACT)
    assert fails == ["graph DOT parsed to zero edges — refusing drifted output"]
    assert targets == set()


def test_graph_zero_reader_provider_references_refuses():
    edges = [(a, b) for a, b in BASE_EDGES if not a.startswith("module.revision_reader")]
    fails = graph_failures(edges)
    assert any("no reader→aws-provider reference at all" in f for f in fails)


# --------------------------------------------------------------------------- #
# synthetic STS stub: loopback-only, single-action, fail-visible
# --------------------------------------------------------------------------- #

def _post(port: int, action: str, path: str = "/", host: str | None = None):
    body = urllib.parse.urlencode({"Action": action, "Version": "2011-06-15"}).encode()
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=body, method="POST")
    if host is not None:
        request.add_header("Host", host)
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode()


def test_stub_accepts_only_the_expected_get_caller_identity():
    with rwc.StsStub("GetCallerIdentity", "000000000000") as stub:
        status, body = _post(stub.port, "GetCallerIdentity")
        assert status == 200
        assert "<Account>000000000000</Account>" in body
        assert stub.state.accepted == ["GetCallerIdentity"]
        assert stub.state.violations == []


def test_stub_rejects_unexpected_action_and_records_the_violation():
    with rwc.StsStub("GetCallerIdentity", "000000000000") as stub:
        status, _ = _post(stub.port, "AssumeRole")
        assert status == 400
        assert stub.state.accepted == []
        assert any("AssumeRole" in v for v in stub.state.violations)


def test_stub_rejects_unexpected_path_method_and_host():
    with rwc.StsStub("GetCallerIdentity", "000000000000") as stub:
        assert _post(stub.port, "GetCallerIdentity", path="/rogue")[0] == 400
        assert _post(stub.port, "GetCallerIdentity", host="sts.amazonaws.com")[0] == 400
        status, _ = _post(stub.port, "GetCallerIdentity")  # still healthy afterwards
        assert status == 200
        request = urllib.request.Request(f"http://127.0.0.1:{stub.port}/", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                get_status = response.status
        except urllib.error.HTTPError as error:
            get_status = error.code
        assert get_status == 400
        assert len(stub.state.violations) == 3


@pytest.mark.parametrize("method", ["DELETE", "PATCH", "OPTIONS", "TRACE"])
def test_stub_records_a_violation_for_every_unexpected_method(method):
    # ADV-B3-F6: an undefined do_* would 501 WITHOUT recording — every common verb
    # must land in the recorded-400 path so the audit surface is total.
    with rwc.StsStub("GetCallerIdentity", "000000000000") as stub:
        request = urllib.request.Request(f"http://127.0.0.1:{stub.port}/", method=method)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                status = response.status
        except urllib.error.HTTPError as error:
            status = error.code
        assert status == 400
        assert len(stub.state.violations) == 1 and method in stub.state.violations[0]


def test_stub_binds_loopback_on_an_ephemeral_port():
    with rwc.StsStub("GetCallerIdentity", "000000000000") as stub:
        assert stub._server.server_address[0] == "127.0.0.1"
        assert stub.port != 0


# --------------------------------------------------------------------------- #
# constructed subprocess environment: allowlist, never inheritance
# --------------------------------------------------------------------------- #

EXPECTED_ENV_KEYS = {
    "PATH", "HOME", "TMPDIR", "LANG",
    "TF_CLI_CONFIG_FILE", "TF_DATA_DIR", "TF_IN_AUTOMATION", "TF_PLUGIN_CACHE_DIR",
    "CHECKPOINT_DISABLE", "NO_COLOR",
    "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_REGION", "AWS_DEFAULT_REGION",
    "AWS_ENDPOINT_URL", "AWS_ENDPOINT_URL_STS", "AWS_EC2_METADATA_DISABLED",
    "AWS_MAX_ATTEMPTS",
}


def _env(tmp_path, monkeypatch) -> dict[str, str]:
    monkeypatch.setattr(rwc.shutil, "which", lambda name: "/synthetic/bin/tofu")
    return rwc.constructed_env(tmp_path, tmp_path / "run", port=59999, tofurc=tmp_path / "rc")


def test_constructed_env_is_exactly_the_allowlist(tmp_path, monkeypatch):
    # A poisoned ambient environment must be invisible: the env is BUILT, not filtered.
    for poison in ("AWS_PROFILE", "AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE",
                   "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "TF_VAR_synthetic",
                   "TF_LOG", "TF_WORKSPACE", "AWS_SESSION_TOKEN", "AWS_ROLE_ARN",
                   "AWS_WEB_IDENTITY_TOKEN_FILE", "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI"):
        monkeypatch.setenv(poison, "synthetic-poison")
    env = _env(tmp_path, monkeypatch)
    assert set(env) == EXPECTED_ENV_KEYS
    assert "synthetic-poison" not in set(env.values())


def test_constructed_env_pins_localhost_endpoint_and_disables_metadata(tmp_path, monkeypatch):
    env = _env(tmp_path, monkeypatch)
    assert env["AWS_ENDPOINT_URL"] == "http://127.0.0.1:59999"
    assert env["AWS_ENDPOINT_URL_STS"] == "http://127.0.0.1:59999"
    assert env["AWS_EC2_METADATA_DISABLED"] == "true"
    assert env["AWS_ACCESS_KEY_ID"] == rwc.SYNTHETIC_ACCESS_KEY
    assert env["CHECKPOINT_DISABLE"] == "1"
    assert env["HOME"] == str(tmp_path / "home")
    assert (tmp_path / "home").is_dir()  # empty work-scoped HOME, no dotfiles reachable


def test_constructed_env_tmpdir_is_short_and_workdir_independent(tmp_path, monkeypatch):
    # go-plugin rendezvous sockets live under TMPDIR and unix socket paths are capped
    # (~104 bytes on macOS). A TMPDIR nested under a deep RUNNER_TEMP work directory
    # kills the provider at launch — proven 2026-08-15 — so it must not derive from
    # the work directory at all.
    env = _env(tmp_path, monkeypatch)
    assert not env["TMPDIR"].startswith(str(tmp_path))
    # ~30 bytes covers go-plugin's "plugin<random>" socket basename with margin.
    assert len(env["TMPDIR"]) + 30 <= 104, "TMPDIR leaves no headroom for a plugin socket name"


def test_constructed_env_fails_closed_without_tofu(tmp_path, monkeypatch):
    monkeypatch.setattr(rwc.shutil, "which", lambda name: None)
    with pytest.raises(rwc.StageFailure) as excinfo:
        rwc.constructed_env(tmp_path, tmp_path / "run", port=1, tofurc=tmp_path / "rc")
    assert excinfo.value.stage == "env"


# --------------------------------------------------------------------------- #
# provider-mirror provenance primitives
# --------------------------------------------------------------------------- #

def test_h1_dirhash_matches_golden_vector(tmp_path):
    # The ALGORITHM is validated externally: the repository's real darwin_arm64 cache
    # directory hashes to an entry of the committed infra/aws/.terraform.lock.hcl
    # (h1:hXkvjHUIlMo9Mx7wnQ/YnDCiFbMkZvP5l5d0napXEro=, proven 2026-08-15). This golden
    # pins the implementation hermetically against regression.
    (tmp_path / "terraform-provider-aws_v0.0.0").write_bytes(b"synthetic provider binary\n")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "LICENSE.txt").write_bytes(b"synthetic license\n")
    assert rwc.h1_dirhash(tmp_path) == "h1:Jbp+1Bn21guHjCj9oA08WoZCH5EXd1gsJqKbLE0VPMs="


def test_h1_dirhash_sees_renames_and_content(tmp_path):
    (tmp_path / "a.bin").write_bytes(b"one")
    first = rwc.h1_dirhash(tmp_path)
    (tmp_path / "a.bin").write_bytes(b"two")
    second = rwc.h1_dirhash(tmp_path)
    (tmp_path / "a.bin").rename(tmp_path / "b.bin")
    third = rwc.h1_dirhash(tmp_path)
    assert len({first, second, third}) == 3


def test_lock_h1_set_reads_only_the_named_provider_block():
    lock = (
        'provider "registry.opentofu.org/hashicorp/other" {\n'
        '  hashes = [\n    "h1:OTHERONLY0000000000000000000000000000000000=",\n  ]\n}\n'
        'provider "registry.opentofu.org/hashicorp/aws" {\n'
        '  version = "6.55.0"\n'
        '  hashes = [\n    "h1:SYNTHETICAAAA000000000000000000000000000000=",\n'
        '    "zh:0000000000000000000000000000000000000000000000000000000000000000",\n  ]\n}\n'
    )
    assert rwc.lock_h1_set(lock, "registry.opentofu.org/hashicorp/aws") == {
        "h1:SYNTHETICAAAA000000000000000000000000000000="
    }


def test_lock_h1_set_fails_closed_on_missing_block():
    with pytest.raises(rwc.StageFailure) as excinfo:
        rwc.lock_h1_set("", "registry.opentofu.org/hashicorp/aws")
    assert excinfo.value.stage == "mirror"


def test_tofurc_has_no_direct_installation_block(tmp_path):
    rc = rwc.write_tofurc(tmp_path, tmp_path / "mirror", CONTRACT)
    text = rc.read_text(encoding="utf-8")
    assert "filesystem_mirror" in text
    assert re.search(r"^\s*direct\s*\{", text, re.M) is None
    assert "network_mirror" not in text


# --------------------------------------------------------------------------- #
# battery grading: a row must fail AT its stage WITH its signature
# --------------------------------------------------------------------------- #

def _record(stage=None, message="", violations=(), plan_file=False):
    record = rwc.RunRecord("synthetic")
    if stage is not None:
        record.failure = rwc.StageFailure(stage, message)
    record.stub_violations = list(violations)
    record.plan_file_written_on_failure = plan_file
    return record


ROW = {"id": "synthetic_row", "expect_stage": "assert_json", "expect_pattern": r"argument surface"}


def test_battery_judges_an_undetected_mutation_as_a_guard_failure():
    verdict = rwc.judge_battery_row(ROW, _record())
    assert verdict is not None and "NOT detected" in verdict


def test_battery_judges_wrong_stage_as_a_guard_failure():
    verdict = rwc.judge_battery_row(ROW, _record("plan", "argument surface"))
    assert verdict is not None and "expected 'assert_json'" in verdict


def test_battery_judges_wrong_signature_as_a_guard_failure():
    verdict = rwc.judge_battery_row(ROW, _record("assert_json", "something unrelated"))
    assert verdict is not None and "signature mismatch" in verdict


def test_battery_accepts_the_exact_expected_failure():
    assert rwc.judge_battery_row(ROW, _record("assert_json", "aliased argument surface bad")) is None


def test_battery_requires_a_recorded_stub_violation_when_declared():
    row = dict(ROW, expect_stage="plan", expect_pattern=r".", expect_stub_violation=True)
    assert rwc.judge_battery_row(row, _record("plan", "x")) is not None
    assert rwc.judge_battery_row(row, _record("plan", "x", violations=["unexpected action 'AssumeRole'"])) is None


def test_battery_requires_the_errored_plan_file_quirk_when_declared():
    # OpenTofu 1.12.5 writes the plan FILE even at plan exit 1. The guard must gate on
    # the exit code; this row proves the quirk is still observable, so a version bump
    # that changes the behaviour is surfaced instead of silently absorbed.
    row = dict(ROW, expect_stage="plan", expect_pattern=r".", expect_plan_file=True)
    assert rwc.judge_battery_row(row, _record("plan", "x")) is not None
    assert rwc.judge_battery_row(row, _record("plan", "x", plan_file=True)) is None


REQUIRED_BATTERY_IDS = {
    # the complete defect battery the B-3 authorization names; shrinking this set is a
    # governed change to BOTH this pin and scripts/root_wiring_check.py
    "providers_map_removed",
    "providers_map_default_aws",
    "providers_map_other_alias",
    "rogue_default_tags",
    "rogue_ignore_tags",
    "rogue_assume_role",
    "rogue_profile",
    "rogue_static_credentials",
    "rogue_account_override",
    "rogue_endpoint_routing",
    "shadow_provider_duplicate_alias",
    "module_local_provider",
    "module_alias_passthrough",
    "shadow_module",
    "new_reader_resource_unrostered",
    "missing_reader_resource",
    "unexpected_stub_action",
    "fixture_substitution",
}


def test_battery_covers_every_required_defect_class_exactly():
    assert {row["id"] for row in rwc.BATTERY} == REQUIRED_BATTERY_IDS


def test_every_battery_row_declares_stage_and_signature():
    for row in rwc.BATTERY:
        assert row["expect_stage"] in {"copy", "bind", "mutation", "fmt", "init",
                                       "validate", "plan", "rebind", "show",
                                       "assert_json", "assert_graph", "cross_check",
                                       "stub_audit"}
        re.compile(row["expect_pattern"])  # every signature must be a valid regex
        assert ("mutation" in row) != ("doctor_after_bind" in row)


def test_battery_is_pure_data_and_every_name_resolves():
    # PURE DATA so the reviewed content digest never binds interpreter-version-sensitive
    # bytecode; every name must still resolve fail-closed to a real def.
    for row in rwc.BATTERY:
        for key, value in row.items():
            assert isinstance(value, (str, bool)), f"{row['id']}.{key} is not pure data"
        name = row.get("mutation") or row["doctor_after_bind"]
        assert callable(rwc._resolve_mutation(name))


def test_mutation_resolver_fails_closed_on_unknown_or_foreign_names():
    for bad in ("_mut_nonexistent", "os.system", "run_pipeline", "_replace_in", ""):
        with pytest.raises(rwc.StageFailure) as excinfo:
            rwc._resolve_mutation(bad)
        assert excinfo.value.stage == "mutation"


def test_fixture_substitution_row_bypasses_the_mutation_channel():
    row = next(r for r in rwc.BATTERY if r["id"] == "fixture_substitution")
    assert row["expect_stage"] == "rebind"
    assert "doctor_after_bind" in row


# --------------------------------------------------------------------------- #
# fixture safety: synthetic, non-production, no live identifiers
# --------------------------------------------------------------------------- #

def test_contract_is_classified_synthetic_non_production():
    classification = CONTRACT["_classification"]
    assert "SYNTHETIC" in classification and "non-production" in classification


def test_contract_roster_is_the_proven_fifteen():
    roster = CONTRACT["reader_resource_roster"]
    assert len(roster) == 15
    addresses = [row["address"] for row in roster]
    assert len(set(addresses)) == 15
    assert all(address.startswith(READER_PREFIX) for address in addresses)
    assert sum(1 for row in roster if row["mode"] == "data") == 1


def test_fixtures_contain_no_live_account_id_or_real_endpoint():
    for text in (json.dumps(CONTRACT), TFVARS_TEXT):
        for digits in re.findall(r"\b\d{12}\b", text):
            # zero-account placeholders and zero-run UUID segments only
            assert int(digits) <= 1, f"12-digit run {digits} is not a synthetic placeholder"
        for arn in re.findall(r"arn:aws[^\"\s]*", text):
            assert ":000000000000:" in arn
        assert "amazonaws.com" not in text.replace("sts.amazonaws.com", "")
    for fqdn in re.findall(r'"[a-z0-9.-]+\.(?:com|net|org|io)"', TFVARS_TEXT):
        assert "example.com" in fqdn


def test_synthetic_tfvars_covers_every_required_root_variable():
    # Parity with the REAL root variable surface: a new required variable must arrive
    # with a synthetic value here, or P1 dies with "No value for required variable" —
    # this test names the gap at unit speed instead.
    variables_tf = (REPO_ROOT / "infra" / "aws" / "variables.tf").read_text(encoding="utf-8")
    required = []
    for match in re.finditer(r'variable\s+"([^"]+)"\s*\{', variables_tf):
        depth, index = 1, match.end()
        while depth:
            depth += {"{": 1, "}": -1}.get(variables_tf[index], 0)
            index += 1
        body = variables_tf[match.end():index - 1]
        if not re.search(r"^\s*default\s*=", body, re.M):
            required.append(match.group(1))
    assigned = set(re.findall(r"^([a-z_0-9]+)\s*=", TFVARS_TEXT, re.M))
    assert set(required) <= assigned, f"synthetic tfvars missing: {sorted(set(required) - assigned)}"


def test_override_content_is_a_bare_local_backend():
    assert rwc.OVERRIDE_CONTENT == 'terraform {\n  backend "local" {}\n}\n'
    assert "s3" not in rwc.OVERRIDE_CONTENT


# --------------------------------------------------------------------------- #
# strict contract validation (the shipping guard's requirement_key catch)
# --------------------------------------------------------------------------- #

def _validation_error(mutated: dict) -> str:
    with pytest.raises(rwc.StageFailure) as excinfo:
        rwc.validate_contract(mutated, REPO_ROOT)
    assert excinfo.value.stage == "contract"
    return excinfo.value.message


def test_validate_contract_accepts_the_shipped_fixture():
    rwc.validate_contract(copy.deepcopy(CONTRACT), REPO_ROOT)


@pytest.mark.parametrize("key,bad", [
    ("provider_source", "registry.example.com/foo/bar"),
    ("provider_version", "0.0.0"),
    ("alias_key", "aws.other"),
    ("alias_name", ""),
    ("alias_allowed_expression_keys", []),
    ("alias_region_references", []),
    ("default_provider_expression_keys", []),
    ("aws_provider_config_universe", []),
    ("reader_module_name", "other"),
    ("reader_module_source", "./modules/nonexistent"),
    ("reader_resource_roster", []),
    ("plan_json_format_versions", []),
    ("stub", {}),
    ("forbidden_alias_arguments_documented", []),
])
def test_validate_contract_refuses_each_corrupted_key_attributably(key, bad):
    mutated = copy.deepcopy(CONTRACT)
    mutated[key] = bad
    assert key in _validation_error(mutated)


@pytest.mark.parametrize("key", sorted(rwc._CONTRACT_REQUIRED_KEYS))
def test_validate_contract_refuses_each_missing_key_attributably(key):
    # ADV-B3-F2: every key validate_contract READS must be in _CONTRACT_REQUIRED_KEYS,
    # or deleting it raises a bare KeyError instead of the attributable refusal.
    mutated = copy.deepcopy(CONTRACT)
    del mutated[key]
    assert "missing keys" in _validation_error(mutated)


def test_required_keys_cover_every_contract_key_the_module_reads():
    # ADV-B3 round-2 LOW: scan the WHOLE module, both quote styles — a contract[...]
    # read added anywhere (assert_plan_json, assert_graph, build_mirror, ...) without a
    # tuple entry reproduces the F2 defect one function over.
    source = Path(rwc.__file__).read_text(encoding="utf-8")
    read_keys = set(re.findall(r"""contract\[["']([A-Za-z0-9_]+)["']\]""", source))
    assert read_keys, "the scan found no contract reads — the pattern regressed"
    assert read_keys <= set(rwc._CONTRACT_REQUIRED_KEYS), (
        f"the module reads unrequired contract keys: "
        f"{sorted(read_keys - set(rwc._CONTRACT_REQUIRED_KEYS))}"
    )


def test_stage_failure_carries_exactly_stage_and_message():
    # The .stage/.message properties read args[0]/args[1]; this pins the two-argument
    # construction contract so a future one-arg raise cannot surface as an IndexError
    # inside the battery judge.
    failure = rwc.StageFailure("synthetic_stage", "synthetic message")
    assert failure.args == ("synthetic_stage", "synthetic message")
    assert failure.stage == "synthetic_stage"
    assert failure.message == "synthetic message"
    assert str(failure) == "[synthetic_stage] synthetic message"
    source = Path(rwc.__file__).read_text(encoding="utf-8")
    import ast as _ast
    for node in _ast.walk(_ast.parse(source)):
        if (isinstance(node, _ast.Call) and isinstance(node.func, _ast.Name)
                and node.func.id == "StageFailure"):
            assert len(node.args) == 2 and not node.keywords, (
                f"StageFailure constructed with {len(node.args)} positional args at "
                f"line {node.lineno}; the (stage, message) contract requires exactly two"
            )


def test_validate_contract_refuses_forbidden_list_overlapping_permitted_surface():
    mutated = copy.deepcopy(CONTRACT)
    mutated["forbidden_alias_arguments_documented"] = ["region"]
    assert "forbidden_alias_arguments_documented" in _validation_error(mutated)


def test_ci_graded_step_is_pinned_to_mode_full():
    # The default mode is the fast contract validation (what the site-coverage matrix
    # executes); the GRADED step must run the full positive-control + mutation battery.
    # ADV-B3-F1: a raw-text substring pin was defeatable by a YAML comment carrying the
    # expected text next to a downgraded bare invocation. This pin therefore goes
    # through the repository's own shell analyser: only EXECUTED argv counts, comments
    # never do, and EVERY invocation of the checker anywhere in the workflow must be
    # the exact graded form — a second bare invocation cannot hide either.
    import ci_invocation_model as cim

    steps = cim.parse_steps()
    graded = [s for s in steps if s.get("id") == "root_wiring"]
    assert len(graded) == 1, "exactly one graded root_wiring step must exist"

    pinned_argv = ["python3", "scripts/root_wiring_check.py", "--mode", "full"]
    step_invocations = [
        c for c in cim.analyse_shell(graded[0]["run"])
        if c["class"] == cim.INVOKED and "scripts/root_wiring_check.py" in c["argv"]
    ]
    assert len(step_invocations) == 1
    assert step_invocations[0]["argv"] == pinned_argv

    for step in steps:
        for command in cim.analyse_shell(step["run"]):
            if command["class"] == cim.INVOKED and "scripts/root_wiring_check.py" in command["argv"]:
                assert command["argv"] == pinned_argv, (
                    f"step {step.get('id')!r} invokes the checker as {command['argv']} — "
                    "every invocation across the id-carrying workflow steps (the parse "
                    "universe; an id-less step cannot be graded) must be the exact "
                    "graded '--mode full' form"
                )


def test_synthetic_credentials_are_structurally_fake():
    # Not credential-shaped: no AKIA/ASIA key-id prefix (the leak scan's exact-case
    # rule), and both values announce themselves as synthetic.
    assert rwc.SYNTHETIC_ACCESS_KEY.startswith("SYNTHETIC")
    assert not re.match(r"(AKIA|ASIA)", rwc.SYNTHETIC_ACCESS_KEY)
    assert "Synthetic" in rwc.SYNTHETIC_SECRET_KEY
