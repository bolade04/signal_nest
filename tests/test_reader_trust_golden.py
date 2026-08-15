"""The python half of the reader trust-parity control (B-2B).

The reader IAM roles are created out-of-band by the role-bootstrap executor from
scripts/trust_policies.py and later ADOPTED by modules/revision_reader. The B-2 Stage-A
barrier (2026-08-15) refused an import because the module rendered the same trust
documents without their Sids — two unreconciled renderings of one reviewed contract.

Both renderers now bind to ONE golden fixture, tests/fixtures/reader-trust-golden.json:
  - THIS test proves trust_policies.py still renders exactly the fixture's documents;
  - modules/revision_reader/trust_binding.tftest.hcl proves the module renders
    structurally identical documents under the same synthetic inputs (run by CI's
    revision-reader job via `tofu test`).
Either side drifting fails its own half; the two cannot diverge from each other while
both halves pass. Regenerate the fixture ONLY from this module under TIER_1_SYNTHETIC —
never by hand, and never from the HCL side (that would invert the authority).
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import trust_policies

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE = REPO_ROOT / "tests" / "fixtures" / "reader-trust-golden.json"
TFTEST = (REPO_ROOT / "infra" / "aws" / "modules" / "revision_reader"
          / "trust_binding.tftest.hcl")

EXPECTED_SIDS = {
    "execution": "EcsTasksInThisAccountOnly",
    "publisher": "GitHubOidcExactRepositoryAndEnvironment",
    "runner": "GitHubOidcExactRepositoryAndEnvironment",
}


def _fixture() -> dict:
    assert FIXTURE.exists(), (
        "tests/fixtures/reader-trust-golden.json is MISSING. The trust-parity control "
        "fails closed: without the fixture neither renderer is pinned.")
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_fixture_is_synthetic_and_complete():
    data = _fixture()
    assert data.get("_classification") == "NON_PRODUCTION_TEST_FIXTURE"
    assert sorted(data["trust"]) == ["execution", "publisher", "runner"], (
        "the fixture must carry exactly the three reader trust documents")


def test_trust_policies_renders_exactly_the_golden_documents():
    data = _fixture()
    manifest = trust_policies.trust_manifest()
    entries = manifest.values() if isinstance(manifest, dict) else manifest
    by_suffix = {e["role_name"].rsplit("-", 1)[-1]: e["trust_policy"] for e in entries}
    assert sorted(by_suffix) == sorted(data["trust"])
    for suffix, expected in data["trust"].items():
        assert by_suffix[suffix] == expected, (
            f"trust_policies.py no longer renders the golden {suffix} trust document. "
            "If the change is intentional, regenerate the fixture from THIS side under "
            "TIER_1_SYNTHETIC and re-run the module's trust_binding.tftest.hcl.")


def test_golden_documents_carry_the_reviewed_sids():
    data = _fixture()
    for suffix, sid in EXPECTED_SIDS.items():
        statements = data["trust"][suffix]["Statement"]
        assert len(statements) == 1
        assert statements[0].get("Sid") == sid, (
            f"the {suffix} golden trust lost its reviewed Sid {sid!r} — the exact "
            "defect class the B-2 barrier refused")


def test_fixture_account_is_the_synthetic_anchor_account():
    """A fixture regenerated under TIER_2_PROTECTED would commit the REAL account id —
    the SEC-1 disclosure class Gate 4N-I18 removed from the tree. Bind the fixture's
    embedded account to the synthetic anchor's, independent of the ambient tier."""
    import anchor_loader

    synthetic = anchor_loader.load(anchor_loader.TIER_SYNTHETIC)
    approved = synthetic.anchor["approved_account_id"]
    data = _fixture()
    source_account = (data["trust"]["execution"]["Statement"][0]
                      ["Condition"]["StringEquals"]["aws:SourceAccount"])
    assert source_account == approved, (
        "the golden fixture's aws:SourceAccount is not the SYNTHETIC anchor account — "
        "it was regenerated under the wrong tier; never commit a Tier-2 rendering")
    for suffix in ("publisher", "runner"):
        federated = data["trust"][suffix]["Statement"][0]["Principal"]["Federated"]
        assert f":iam::{approved}:" in federated, (
            f"the {suffix} OIDC provider ARN does not carry the synthetic anchor account")


def test_module_taggable_resource_census_is_pinned():
    """Drift-pin for the tag-probe run's enumeration (round-3 adversarial finding):
    the tftest's "passed_tag_set_reaches_every_non_role_resource" run enumerates
    resources BY NAME, so a new taggable reader resource added without extending the
    probe would ship genuinely untagged now that default_tags no longer backstops the
    module. This census turns silent growth into a failing test: adding any resource
    to the module requires updating BOTH this pin AND the probe run."""
    module_dir = REPO_ROOT / "infra" / "aws" / "modules" / "revision_reader"
    assert not list(module_dir.glob("*.tf.json")), (
        "the reader module acquired a .tf.json file — native-JSON resources evade "
        "this census; convert to HCL or extend the census (round-4 adversarial F4)")
    found = set()
    for tf in sorted(module_dir.glob("*.tf")):
        # Indent- and charset-tolerant (round-4 adversarial F4): hyphens and upper
        # case are legal in labels, and an indented top-level block is valid HCL.
        for m in re.finditer(
                r'^[ \t]*resource\s+"([A-Za-z0-9_-]+)"\s+"([A-Za-z0-9_-]+)"',
                tf.read_text(), flags=re.MULTILINE):
            found.add(f"{m.group(1)}.{m.group(2)}")
    EXPECTED = {
        "aws_ecr_repository.reader",
        "aws_ecr_lifecycle_policy.reader",
        "aws_cloudwatch_log_group.reader",
        "aws_security_group.reader",
        "aws_vpc_security_group_egress_rule.reader_to_postgres",
        "aws_vpc_security_group_egress_rule.reader_https",
        "aws_vpc_security_group_ingress_rule.rds_from_reader",
        "aws_ecs_task_definition.reader",
        "aws_iam_role.reader_publisher",
        "aws_iam_role.reader_execution",
        "aws_iam_role.reader_runner",
        "aws_iam_role_policy.reader_publisher",
        "aws_iam_role_policy.reader_execution",
        "aws_iam_role_policy.reader_runner",
        "terraform_data.boundary_state_coherence",
        "terraform_data.boundary_mode_precondition",
    }
    assert found == EXPECTED, (
        "the reader module's resource census changed — update this pin AND extend "
        "trust_binding.tftest.hcl's tag-probe run if the new resource is taggable: "
        f"added={sorted(found - EXPECTED)} removed={sorted(EXPECTED - found)}")


def test_hcl_half_of_the_control_exists_and_binds_the_same_fixture():
    assert TFTEST.exists(), (
        "modules/revision_reader/trust_binding.tftest.hcl is MISSING — the HCL half of "
        "the trust-parity control is gone and module drift would ship silently.")
    text = TFTEST.read_text(encoding="utf-8")
    assert "reader-trust-golden.json" in text, (
        "the tftest no longer reads the golden fixture; the two halves are unbound")
    for sub in ("trust.publisher", "trust.runner", "trust.execution"):
        assert sub in text, f"the tftest no longer compares {sub}"
