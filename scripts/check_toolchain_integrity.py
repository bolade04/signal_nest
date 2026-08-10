#!/usr/bin/env python3
"""Fail-closed toolchain verification for the Gate 4N execution contract.

Gate 4N-I4. Replaces scripts/check-toolchain-integrity.sh, which had two defects:

  1. It failed merely because a module-local `.terraform` directory EXISTED — but CI
     legitimately creates one when it runs the offline module contract tests. The check
     was therefore deterministically red, and because CI steps are sequential it also
     suppressed the policy tests that ran after it.
  2. Checks 4b and 10 passed vacuously ("nothing to verify") whenever no cache was
     present, which is exactly the CI condition.

The fix is to classify each cache by CONTENTS AND PROVENANCE rather than by existence:

  EXPECTED_VERIFIED  a cache at a known path whose provider version matches the lockfile
                     and whose extracted binary matches the recorded per-platform pin.
  FORBIDDEN          wrong version, unpinned/unknown binary, a `dev` build, a symlink
                     escaping the cache root, or a cache at an unexpected path.
  ABSENT_OPTIONAL    no cache at a known path; legitimate when the selected test path
                     does not need one.

Never touches a backend: no init, no plan, no apply, no AWS call. Read-only on disk.

Usage:
    python3 scripts/check_toolchain_integrity.py [--infra-dir DIR] [--json]

Exit: 0 all checks pass; 1 one or more fail.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

EXPECTED_TOFU_VERSION = "1.12.5"
EXPECTED_PROVIDER_VERSION = "6.55.0"
PROVIDER_SOURCE = "registry.opentofu.org/hashicorp/aws"

# Paths where a provider cache is legitimate. The root is the plan/apply surface; the
# module paths are offline contract-test surfaces CI initialises deliberately.
EXPECTED_CACHE_ROOTS = (
    Path("infra/aws"),
    Path("infra/aws/modules/revision_reader"),
    # Gate 4N-I8: modules/iam gained boundary_durability.tftest.hcl, so CI must `tofu init
    # -backend=false` there to run it. Same shape as revision_reader — offline, mocked
    # provider, no backend. Adding the path deliberately rather than deleting the cache,
    # because CI needs it to exist.
    Path("infra/aws/modules/iam"),
)

# Environment variables that can silently redefine what runs or where it reads from.
FORBIDDEN_ENV = (
    "TF_CLI_CONFIG_FILE",
    "TF_DATA_DIR",
    "TF_WORKSPACE",
    "AWS_CONFIG_FILE",
    "AWS_SHARED_CREDENTIALS_FILE",
    "TOFU_ENFORCE_GPG_VALIDATION",
)
FORBIDDEN_ENV_PREFIXES = ("TF_LOG", "TF_CLI_ARGS", "TF_VAR_", "TF_TEMP_LOG")

CLI_CONFIGS = (
    Path.home() / ".terraformrc",
    Path.home() / ".tofurc",
)
OVERRIDE_PATTERN = re.compile(r"dev_overrides|provider_installation|filesystem_mirror|network_mirror")


class Report:
    def __init__(self) -> None:
        self.checks: list[dict] = []

    def add(self, cid: str, ok: bool, detail: str, classification: str | None = None) -> None:
        self.checks.append(
            {"id": cid, "ok": ok, "detail": detail, "classification": classification}
        )

    @property
    def failures(self) -> list[dict]:
        return [c for c in self.checks if not c["ok"]]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_pin(repo_root: Path) -> dict:
    pin_path = repo_root / "infra" / "aws" / "provider-binary-pin.json"
    if not pin_path.exists():
        return {}
    return json.loads(pin_path.read_text(encoding="utf-8"))


def check_executable(report: Report) -> None:
    tofu = shutil.which("tofu")
    if not tofu:
        report.add("exe", False, "'tofu' not found; the contract requires OpenTofu, not terraform")
        return
    report.add("exe", True, f"tofu present at {tofu}")

    if shutil.which("terraform"):
        # Presence alone is not a failure, but it must be visible: a script or muscle
        # memory invoking `terraform` would silently bypass every pin below.
        report.add("exe.terraform-also-present", True, "terraform is also on PATH — never invoke it for this repo")

    try:
        raw = subprocess.run([tofu, "version", "-json"], capture_output=True, text=True, timeout=30)
        version = json.loads(raw.stdout).get("terraform_version", "")
    except Exception as exc:  # noqa: BLE001 - fail closed on any probe failure
        report.add("exe.version", False, f"could not determine tofu version: {exc}")
        return
    report.add(
        "exe.version",
        version == EXPECTED_TOFU_VERSION,
        f"tofu version {version or 'unknown'} (contract requires exactly {EXPECTED_TOFU_VERSION})",
    )


def check_lockfile(report: Report, infra_dir: Path) -> None:
    lockfile = infra_dir / ".terraform.lock.hcl"
    if not lockfile.exists():
        report.add("lock", False, f"lockfile not found at {lockfile}")
        return
    text = lockfile.read_text(encoding="utf-8")

    # Correlated match: the version must appear INSIDE the aws provider block, not merely
    # somewhere in the file. Two uncorrelated greps would pass on a different provider.
    block = re.search(
        r'provider\s+"' + re.escape(PROVIDER_SOURCE) + r'"\s*\{(.*?)\n\}',
        text,
        re.DOTALL,
    )
    if not block:
        report.add("lock.provider", False, f"no provider block for {PROVIDER_SOURCE}")
        return
    body = block.group(1)
    pinned = re.search(r'^\s*version\s*=\s*"([^"]+)"', body, re.MULTILINE)
    actual = pinned.group(1) if pinned else None
    report.add(
        "lock.version",
        actual == EXPECTED_PROVIDER_VERSION,
        f"{PROVIDER_SOURCE} pinned to {actual or 'unknown'} inside its own block",
    )
    report.add(
        "lock.checksums",
        bool(re.search(r'"(h1|zh):', body)),
        "lockfile records provider checksums for the aws block",
    )


def classify_cache(cache_root: Path, pin: dict) -> tuple[str, str]:
    """Return (classification, detail) for one provider-cache location."""
    providers = cache_root / ".terraform" / "providers" / PROVIDER_SOURCE
    if not (cache_root / ".terraform").exists():
        return "ABSENT_OPTIONAL", "no .terraform directory"
    if not providers.exists():
        # A .terraform with no provider dir is a backend-only or empty init.
        return "EXPECTED_VERIFIED", ".terraform present with no provider cache"

    versions = sorted(p.name for p in providers.iterdir() if p.is_dir())
    unexpected = [v for v in versions if v != EXPECTED_PROVIDER_VERSION]
    if unexpected:
        return "FORBIDDEN", f"unexpected provider version(s) cached: {', '.join(unexpected)}"
    if not versions:
        return "EXPECTED_VERIFIED", "provider directory present but empty"

    version_dir = providers / EXPECTED_PROVIDER_VERSION
    binaries = [p for p in version_dir.rglob("terraform-provider-aws*") if p.is_file()]
    if not binaries:
        return "FORBIDDEN", f"{EXPECTED_PROVIDER_VERSION} cached but no provider binary present"

    for binary in binaries:
        # A symlink escaping the cache root would let an arbitrary binary masquerade.
        resolved = binary.resolve()
        try:
            resolved.relative_to(cache_root.resolve())
        except ValueError:
            return "FORBIDDEN", f"provider binary escapes the cache root via symlink: {binary}"

        if "dev" in binary.name.replace("terraform-provider-aws", ""):
            return "FORBIDDEN", f"provider binary looks like a dev build: {binary.name}"

        platform = binary.parent.name
        expected = (pin.get("binaries") or {}).get(platform)
        if expected is not None:
            actual = sha256_file(binary)
            if expected != actual:
                return "FORBIDDEN", f"binary SHA-256 mismatch for {platform}: expected {expected[:16]}…, got {actual[:16]}…"
            continue
        if platform in (pin.get("lockfile_verified_platforms") or []):
            # Assurance rests on the verification `tofu init` performs against the
            # lockfile. Explicitly allowlisted, so the reduction is reviewable.
            continue
        return (
            "FORBIDDEN",
            f"platform '{platform}' is neither binary-pinned nor lockfile-verified-allowlisted; "
            "add it to infra/aws/provider-binary-pin.json after verifying provenance",
        )

    return "EXPECTED_VERIFIED", f"{EXPECTED_PROVIDER_VERSION} cached, provenance verified for every binary"


def check_caches(report: Report, repo_root: Path, pin: dict) -> None:
    expected_paths = {(repo_root / p).resolve() for p in EXPECTED_CACHE_ROOTS}

    for rel in EXPECTED_CACHE_ROOTS:
        cache_root = repo_root / rel
        classification, detail = classify_cache(cache_root, pin)
        report.add(
            f"cache[{rel}]",
            classification != "FORBIDDEN",
            f"{classification}: {detail}",
            classification,
        )

    # Any .terraform outside the expected set is unreviewed by definition.
    for found in repo_root.rglob(".terraform"):
        if not found.is_dir() or ".git" in found.parts:
            continue
        owner = found.parent.resolve()
        if owner not in expected_paths:
            report.add(
                f"cache.unexpected[{found.relative_to(repo_root)}]",
                False,
                "FORBIDDEN: provider cache at an unexpected path",
                "FORBIDDEN",
            )


def check_environment(report: Report) -> None:
    offenders = [name for name in FORBIDDEN_ENV if os.environ.get(name)]
    offenders += [
        name
        for name in os.environ
        if any(name.startswith(prefix) for prefix in FORBIDDEN_ENV_PREFIXES)
    ]
    report.add(
        "env",
        not offenders,
        "no toolchain-redirecting environment variables set"
        if not offenders
        else f"set: {', '.join(sorted(set(offenders)))}",
    )


def check_cli_config(report: Report, infra_dir: Path) -> None:
    offenders = []
    for cfg in (*CLI_CONFIGS, infra_dir / ".terraformrc", infra_dir / ".tofurc"):
        if cfg.exists() and OVERRIDE_PATTERN.search(cfg.read_text(encoding="utf-8", errors="replace")):
            offenders.append(str(cfg))
    report.add(
        "cli-config",
        not offenders,
        "no dev_overrides / provider_installation / mirror directives"
        if not offenders
        else f"provider-installation override present in: {', '.join(offenders)}",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--infra-dir", default=str(REPO_ROOT / "infra" / "aws"))
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    infra_dir = Path(args.infra_dir).resolve()
    pin = load_pin(repo_root)

    report = Report()
    if not pin:
        report.add("pin", False, "infra/aws/provider-binary-pin.json missing — binaries unverifiable")
    else:
        report.add("pin", True, f"binary pin present for {len(pin.get('binaries', {}))} platform(s)")

    check_executable(report)
    check_lockfile(report, infra_dir)
    check_caches(report, repo_root, pin)
    check_environment(report)
    check_cli_config(report, infra_dir)

    if args.json:
        print(json.dumps({"checks": report.checks, "failed": len(report.failures)}, indent=2))
    else:
        for check in report.checks:
            print(f"  {'OK  ' if check['ok'] else 'FAIL'}  {check['id']}: {check['detail']}")
        print()
        if report.failures:
            print(f"TOOLCHAIN INTEGRITY: {len(report.failures)} check(s) FAILED.", file=sys.stderr)
        else:
            print("TOOLCHAIN INTEGRITY: all checks passed.")

    return 1 if report.failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
