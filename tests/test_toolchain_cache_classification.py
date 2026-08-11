"""Cache-classification tests for the toolchain integrity check (Gate 4N-I4).

The Gate 4N-I3 shell version failed merely because a module-local `.terraform` existed —
which CI legitimately creates — so it was deterministically red and, because CI steps run
in sequence, it suppressed the policy tests that followed. These tests pin the corrected
behaviour: classification is by CONTENTS AND PROVENANCE, never by existence.

Every fixture is built in a temporary directory. Nothing in the repository is touched.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import check_toolchain_integrity as tc  # noqa: E402

PIN_HASH = "c4e6254291d61b558312e49af143d262234831b114c7187dd17d14579fad086e"


def make_cache(root: Path, *, version: str = tc.EXPECTED_PROVIDER_VERSION,
               platform: str = "darwin_arm64", content: bytes | None = None,
               binary_name: str = "terraform-provider-aws") -> Path:
    """Create a provider cache under `root` and return the binary path."""
    target = root / ".terraform" / "providers" / tc.PROVIDER_SOURCE / version / platform
    target.mkdir(parents=True, exist_ok=True)
    binary = target / binary_name
    binary.write_bytes(content if content is not None else b"pinned-provider-bytes")
    return binary


@pytest.fixture()
def pin(tmp_path: Path) -> dict:
    """A pin whose recorded hash matches the default fixture bytes."""
    import hashlib

    digest = hashlib.sha256(b"pinned-provider-bytes").hexdigest()
    return {"binaries": {"darwin_arm64": digest}}


def classify(root: Path, pin: dict) -> tuple[str, str]:
    return tc.classify_cache(root, pin)


# --- EXPECTED_VERIFIED ---------------------------------------------------------------


def test_absent_cache_is_absent_optional(tmp_path: Path, pin: dict):
    assert classify(tmp_path, pin)[0] == "ABSENT_OPTIONAL"


def test_valid_module_cache_is_expected_verified(tmp_path: Path, pin: dict):
    """THE CI CONDITION. The old check failed here; failing here again would re-break CI."""
    make_cache(tmp_path)
    classification, detail = classify(tmp_path, pin)
    assert classification == "EXPECTED_VERIFIED", detail


def test_terraform_dir_without_provider_cache_is_accepted(tmp_path: Path, pin: dict):
    (tmp_path / ".terraform").mkdir()
    assert classify(tmp_path, pin)[0] == "EXPECTED_VERIFIED"


# --- FORBIDDEN -----------------------------------------------------------------------


def test_wrong_provider_version_is_forbidden(tmp_path: Path, pin: dict):
    make_cache(tmp_path, version="6.56.0")
    classification, detail = classify(tmp_path, pin)
    assert classification == "FORBIDDEN"
    assert "6.56.0" in detail


def test_wrong_binary_checksum_is_forbidden(tmp_path: Path, pin: dict):
    make_cache(tmp_path, content=b"tampered")
    classification, detail = classify(tmp_path, pin)
    assert classification == "FORBIDDEN"
    assert "mismatch" in detail


def test_provider_dev_build_is_forbidden(tmp_path: Path, pin: dict):
    make_cache(tmp_path, binary_name="terraform-provider-aws_dev")
    classification, detail = classify(tmp_path, pin)
    assert classification == "FORBIDDEN"
    assert "dev build" in detail


def test_unrecorded_platform_is_forbidden_not_skipped(tmp_path: Path, pin: dict):
    """The old check passed vacuously when it could not verify. This must fail closed."""
    make_cache(tmp_path, platform="solaris_sparc")
    classification, detail = classify(tmp_path, pin)
    assert classification == "FORBIDDEN"
    assert "neither binary-pinned nor lockfile-verified" in detail


def test_lockfile_verified_platform_is_accepted_only_when_allowlisted(tmp_path: Path, pin: dict):
    """CI runs linux_amd64 with no binary pin. Accepting it must be EXPLICIT, not a fallback."""
    make_cache(tmp_path, platform="linux_amd64", content=b"whatever-ci-downloaded")
    assert classify(tmp_path, pin)[0] == "FORBIDDEN", "not allowlisted -> forbidden"
    allowlisted = dict(pin, lockfile_verified_platforms=["linux_amd64"])
    assert classify(tmp_path, allowlisted)[0] == "EXPECTED_VERIFIED"


def test_real_pin_file_allowlists_the_ci_platform():
    """Guards against re-shipping a deterministically red CI, this time via the platform rule."""
    pin = tc.load_pin(REPO_ROOT)
    assert "linux_amd64" in pin.get("lockfile_verified_platforms", []), (
        "CI runs linux_amd64; without an allowlist entry the post-init check would fail"
    )


def test_binary_pin_still_wins_over_the_allowlist(tmp_path: Path):
    """A platform with BOTH a pin and an allowlist entry must still be hash-checked."""
    import hashlib
    good = hashlib.sha256(b"pinned-provider-bytes").hexdigest()
    pin = {"binaries": {"darwin_arm64": good}, "lockfile_verified_platforms": ["darwin_arm64"]}
    make_cache(tmp_path, content=b"tampered")
    assert classify(tmp_path, pin)[0] == "FORBIDDEN"


def test_missing_binary_is_forbidden(tmp_path: Path, pin: dict):
    (tmp_path / ".terraform" / "providers" / tc.PROVIDER_SOURCE / tc.EXPECTED_PROVIDER_VERSION).mkdir(parents=True)
    classification, detail = classify(tmp_path, pin)
    assert classification == "FORBIDDEN"
    assert "no provider binary" in detail


def test_symlink_escaping_the_cache_root_is_forbidden(tmp_path: Path, pin: dict):
    outside = tmp_path.parent / "outside-provider"
    outside.write_bytes(b"pinned-provider-bytes")
    target = tmp_path / ".terraform" / "providers" / tc.PROVIDER_SOURCE / tc.EXPECTED_PROVIDER_VERSION / "darwin_arm64"
    target.mkdir(parents=True)
    (target / "terraform-provider-aws").symlink_to(outside)
    classification, detail = classify(tmp_path, pin)
    assert classification == "FORBIDDEN"
    assert "symlink" in detail


def test_cache_at_an_unexpected_path_is_forbidden(tmp_path: Path, pin: dict):
    """A cache outside the reviewed set is unreviewed by definition."""
    (tmp_path / "infra" / "aws").mkdir(parents=True)
    stray = tmp_path / "some" / "other" / "place"
    stray.mkdir(parents=True)
    make_cache(stray)
    report = tc.Report()
    tc.check_caches(report, tmp_path, pin)
    unexpected = [c for c in report.checks if "unexpected" in c["id"]]
    assert unexpected and not unexpected[0]["ok"]


# --- environment and CLI config ------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["TF_CLI_CONFIG_FILE", "TF_DATA_DIR", "TF_LOG", "TF_CLI_ARGS_plan", "TF_VAR_foo",
     "TF_WORKSPACE", "AWS_CONFIG_FILE", "TOFU_ENFORCE_GPG_VALIDATION", "TF_TEMP_LOG_PATH"],
)
def test_toolchain_redirecting_env_vars_fail_closed(monkeypatch, name):
    monkeypatch.setenv(name, "x")
    report = tc.Report()
    tc.check_environment(report)
    assert report.failures, f"{name} must be rejected"


@pytest.mark.parametrize("directive", ["dev_overrides", "provider_installation", "filesystem_mirror", "network_mirror"])
def test_provider_installation_overrides_fail_closed(tmp_path: Path, directive: str):
    cfg = tmp_path / ".terraformrc"
    cfg.write_text(f'{directive} {{\n  "hashicorp/aws" = "/tmp/x"\n}}\n', encoding="utf-8")
    report = tc.Report()
    tc.check_cli_config(report, tmp_path)
    assert report.failures


# --- lockfile correlation ------------------------------------------------------------


def test_lockfile_version_must_be_inside_the_aws_block(tmp_path: Path):
    """Two uncorrelated greps would pass here; a correlated match must not."""
    (tmp_path / ".terraform.lock.hcl").write_text(
        'provider "registry.opentofu.org/hashicorp/aws" {\n'
        '  version = "6.99.0"\n'
        '  hashes = ["h1:x"]\n'
        "}\n\n"
        'provider "registry.opentofu.org/hashicorp/random" {\n'
        f'  version = "{tc.EXPECTED_PROVIDER_VERSION}"\n'
        "}\n",
        encoding="utf-8",
    )
    report = tc.Report()
    tc.check_lockfile(report, tmp_path)
    version_check = next(c for c in report.checks if c["id"] == "lock.version")
    assert not version_check["ok"], "the aws block pins 6.99.0; a different provider at 6.55.0 must not satisfy it"


def test_lockfile_missing_checksums_fails(tmp_path: Path):
    (tmp_path / ".terraform.lock.hcl").write_text(
        'provider "registry.opentofu.org/hashicorp/aws" {\n'
        f'  version = "{tc.EXPECTED_PROVIDER_VERSION}"\n'
        "}\n",
        encoding="utf-8",
    )
    report = tc.Report()
    tc.check_lockfile(report, tmp_path)
    assert not next(c for c in report.checks if c["id"] == "lock.checksums")["ok"]


def test_real_repository_passes_and_pin_matches_the_real_binary():
    """End-to-end on the actual repository, and the recorded pin must be the real hash."""
    pin = tc.load_pin(REPO_ROOT)
    assert pin["binaries"]["darwin_arm64"] == PIN_HASH
    report = tc.Report()
    tc.check_caches(report, REPO_ROOT, pin)
    assert not report.failures, [c for c in report.failures]
