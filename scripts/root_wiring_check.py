#!/usr/bin/env python3
"""INFRA-9 B-3. OpenTofu-native root-wiring regression guard for the revision reader.

THE DEFECT CLASS THIS CLOSES. Deleting `module.revision_reader`'s
`providers = { aws = aws.revision_reader }` map — or adding `default_tags`,
`assume_role`, `ignore_tags`, `profile`, credential, account or endpoint routing to the
aliased provider block — is valid HCL that `tofu validate` accepts, and it silently
reintroduces the TagRole drift (or a credential/tag reroute) that the B-2 Stage-A
barrier refused on 2026-08-15. A prior hand-rolled HCL-scanner guard was removed after
six review rounds found successive fail-open evasions, so this guard parses NO HCL:
OpenTofu itself is the configuration oracle, and the checker consumes only
OpenTofu-generated artifacts — `tofu show -json` plan output (primary) and `tofu graph`
DOT (secondary independent witness).

HOW IT RUNS WITHOUT AWS. The pinned provider (aws 6.55.0) eagerly calls STS
GetCallerIdentity at configure time, so an offline plan needs an answer: the checker
binds a synthetic STS stub to 127.0.0.1 on an ephemeral port and points the provider at
it via AWS_ENDPOINT_URL and AWS_ENDPOINT_URL_STS. The stub accepts EXACTLY one request shape — POST / with
Action=GetCallerIdentity from a localhost Host — answers a canned synthetic identity
(account 000000000000), and rejects everything else with HTTP 400 while recording the
violation. The OpenTofu subprocess environment is CONSTRUCTED from an explicit
allowlist, never inherited: no profile, credential file, config file, proxy, or
metadata source can leak in (AWS_EC2_METADATA_DISABLED=true; synthetic static keys;
localhost-only endpoint; CHECKPOINT_DISABLE=1).

WHAT IT NEVER TOUCHES. The committed S3 backend is never initialized: the checker
copies the git-TRACKED files of infra/aws (excluding bootstrap/) into a disposable work
directory outside the repository, overrides the backend to `local` inside the copy
only, and plans with -refresh=false against empty local state. Provider installation is
filesystem-mirror/cache-only: the mirror is built from an already-present verified
provider cache (CI: the module-test cache this job created earlier; local: the repo
cache), the unpacked directory's h1 dirhash MUST appear in the committed root
.terraform.lock.hcl, the binary pin policy of infra/aws/provider-binary-pin.json is
enforced, and the generated CLI config excludes `direct` installation entirely — there
is no registry download and no network fallback.

FIXTURE-SUBSTITUTION CONTROL. Every copied file is sha256-bound to the repository
working tree at copy time AND re-verified after the plan, immediately before the JSON
assertions — so the checker provably inspected the real root configuration, and a copy
doctored after binding (battery row fixture_substitution) is caught.

ERRORED-PLAN QUIRK. OpenTofu 1.12.5 writes the plan FILE even when `plan` exits 1
(observed with a rogue `profile`). The pipeline gates on the plan EXIT CODE before any
`show -json`, so an errored plan file is never parsed as a verdict.

GRADING. A single run is never trusted bare: `--mode full` (the CI invocation) runs the
positive control P1 (the unmutated copy must PASS every assertion — JSON/DOT format
drift therefore fails closed, never silently green) plus the complete negative mutation
battery on doctored copies, each of which must fail AT its designated stage WITH its
expected signature. A battery row that passes, or fails elsewhere, fails the guard.

Usage:
    python3 scripts/root_wiring_check.py [--mode full|positive|contract] [--work-dir DIR] [--json]

    full     = positive control + complete negative battery (the graded CI invocation)
    positive = P1 only
    contract = strict fixture validation, no OpenTofu (the DEFAULT; what the
               site-coverage matrix executes)

Exit: 0 all controls hold; 1 any assertion, battery row, or environment failure.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import http.server
import json
import os
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA_REL = Path("infra") / "aws"
CONTRACT_PATH = REPO_ROOT / "tests" / "fixtures" / "root-wiring-contract.json"
# .tfvars.example: the repository convention for tracked SYNTHETIC variable templates
# (.gitignore blocks bare *.tfvars so no real inputs can ever be committed).
TFVARS_PATH = REPO_ROOT / "tests" / "fixtures" / "root-wiring-synthetic.tfvars.example"
PIN_PATH = REPO_ROOT / INFRA_REL / "provider-binary-pin.json"
LOCK_REL = ".terraform.lock.hcl"
OVERRIDE_NAME = "backend_override.tf"
OVERRIDE_CONTENT = 'terraform {\n  backend "local" {}\n}\n'

# Synthetic static credentials. They authenticate nothing anywhere: the constructed
# environment points the SDK at the localhost stub, and even a committed `endpoints`
# reroute (a battery defect class) would carry only these fake keys, which no real
# service accepts. Deliberately NOT shaped like a
# real key id (no AKIA/ASIA prefix), so the repository leak scan never has to allowlist
# a credential-shaped literal.
SYNTHETIC_ACCESS_KEY = "SYNTHETICAKIA00000000"
SYNTHETIC_SECRET_KEY = "SyntheticSecretKey0000000000000000000000"

_STS_RESPONSE_TEMPLATE = """<GetCallerIdentityResponse xmlns="https://sts.amazonaws.com/doc/2011-06-15/">
  <GetCallerIdentityResult>
    <Arn>arn:aws:iam::{account}:user/synthetic-root-wiring-probe</Arn>
    <UserId>AIDASYNTHETIC000000000</UserId>
    <Account>{account}</Account>
  </GetCallerIdentityResult>
  <ResponseMetadata>
    <RequestId>00000000-0000-0000-0000-000000000000</RequestId>
  </ResponseMetadata>
</GetCallerIdentityResponse>"""


class StageFailure(Exception):
    """A pipeline stage failed; carries the stage id and the exact message.

    No __init__ override on purpose: Exception stores (stage, message) in .args
    itself, and site_taxonomy's call-graph resolver fails closed on any explicit
    __init__ delegation among this module's classes.
    """

    @property
    def stage(self) -> str:
        return self.args[0]

    @property
    def message(self) -> str:
        return self.args[1]

    def __str__(self) -> str:
        return f"[{self.args[0]}] {self.args[1]}"


# --------------------------------------------------------------------------- #
# contract
# --------------------------------------------------------------------------- #

_CONTRACT_REQUIRED_KEYS = (
    "provider_source",
    "provider_version",
    "alias_key",
    "alias_name",
    "alias_allowed_expression_keys",
    "alias_region_references",
    "default_provider_expression_keys",
    "aws_provider_config_universe",
    "reader_module_name",
    "reader_module_source",
    "reader_resource_roster",
    "plan_json_format_versions",
    "stub",
    "forbidden_alias_arguments_documented",
)


def validate_contract(contract: dict, repo_root: Path) -> None:
    """Strict per-key validation of the authored contract, fail-closed.

    Every failure NAMES the offending key: this function is the shipping guard's
    catch for a corrupted contract fixture (site-coverage requirement_key rows execute
    exactly these refusals), and it cross-checks the repository-authoritative sources
    (committed lockfile, provider-binary pin, module tree) wherever one exists — so a
    contract that drifts from the repository is refused before any OpenTofu runs.
    Value-level drift that stays shape-valid (for example a permitted-surface widening)
    is caught by the positive control P1 against the real configuration instead.
    """
    def refuse(key: str, why: str) -> None:
        raise StageFailure("contract", f"contract key {key}: {why}")

    missing = [k for k in _CONTRACT_REQUIRED_KEYS if k not in contract]
    if missing:
        raise StageFailure("contract", f"contract fixture missing keys: {', '.join(missing)}")

    pin = json.loads(PIN_PATH.read_text(encoding="utf-8")) if PIN_PATH.is_file() else {}
    source = contract["provider_source"]
    version = contract["provider_version"]
    if not isinstance(source, str) or source != pin.get("provider"):
        refuse("provider_source", f"{source!r} != provider-binary-pin.json provider {pin.get('provider')!r}")
    if not isinstance(version, str) or version != pin.get("version"):
        refuse("provider_version", f"{version!r} != provider-binary-pin.json version {pin.get('version')!r}")
    lock = repo_root / INFRA_REL / LOCK_REL
    if not lock.is_file() or f'provider "{source}"' not in lock.read_text(encoding="utf-8"):
        refuse("provider_source", f"no block for {source!r} in the committed {LOCK_REL}")

    alias_name = contract["alias_name"]
    if not isinstance(alias_name, str) or not alias_name:
        refuse("alias_name", "must be a non-empty string")
    if contract["alias_key"] != f"aws.{alias_name}":
        refuse("alias_key", f"{contract['alias_key']!r} != 'aws.' + alias_name ({alias_name!r})")

    allowed = contract["alias_allowed_expression_keys"]
    if (not isinstance(allowed, list) or not allowed
            or not all(isinstance(k, str) for k in allowed) or "region" not in allowed):
        refuse("alias_allowed_expression_keys",
               "must be a non-empty list of strings containing 'region' (the alias cannot configure without one)")
    refs = contract["alias_region_references"]
    if not isinstance(refs, list) or not refs or not all(isinstance(r, str) and r for r in refs):
        refuse("alias_region_references", "must be a non-empty list of reference strings")
    default_keys = contract["default_provider_expression_keys"]
    if not isinstance(default_keys, list) or not {"default_tags", "region"} <= set(default_keys):
        refuse("default_provider_expression_keys",
               "must include 'default_tags' and 'region' — the eight-tag set stays on the DEFAULT provider")
    universe = contract["aws_provider_config_universe"]
    if (not isinstance(universe, list)
            or not {"aws", contract["alias_key"]} <= set(universe)):
        refuse("aws_provider_config_universe", "must contain 'aws' and the alias key")

    module_name = contract["reader_module_name"]
    if not isinstance(module_name, str) or not module_name:
        refuse("reader_module_name", "must be a non-empty string")
    module_source = contract["reader_module_source"]
    if (not isinstance(module_source, str) or not module_source.startswith("./")
            or not (repo_root / INFRA_REL / module_source).is_dir()):
        refuse("reader_module_source", f"{module_source!r} is not a module directory under {INFRA_REL}")

    roster = contract["reader_resource_roster"]
    if not roster or not isinstance(roster, list):
        refuse("reader_resource_roster", "empty — refusing a vacuous roster")
    prefix = f"module.{module_name}."
    for row in roster:
        address = row.get("address") if isinstance(row, dict) else None
        if (not address or not address.startswith(prefix)
                or row.get("mode") not in ("managed", "data")
                or "aws_" not in address):
            # Names BOTH keys: a corrupted roster row and a reader_module_name that no
            # longer matches the roster land here, and each must be attributable.
            refuse("reader_resource_roster",
                   f"row {row!r} is malformed or outside reader_module_name {module_name!r}")
    addresses = [row["address"] for row in roster]
    if len(set(addresses)) != len(addresses):
        refuse("reader_resource_roster", "duplicate addresses")

    versions = contract["plan_json_format_versions"]
    if not isinstance(versions, list) or not versions or not all(isinstance(v, str) and v for v in versions):
        refuse("plan_json_format_versions", "must be a non-empty list of version strings")
    stub = contract["stub"]
    if (not isinstance(stub, dict) or not stub.get("allowed_action")
            or not re.fullmatch(r"\d{12}", str(stub.get("synthetic_account", "")))
            or int(stub["synthetic_account"]) > 1):
        refuse("stub", "must carry allowed_action and a synthetic (zero-run) 12-digit account")
    documented = contract["forbidden_alias_arguments_documented"]
    if (not isinstance(documented, list) or not documented
            or not all(isinstance(d, str) for d in documented)
            or set(documented) & set(allowed)):
        refuse("forbidden_alias_arguments_documented",
               "must be a non-empty list of argument names disjoint from the permitted surface")


def load_contract() -> dict:
    if not CONTRACT_PATH.is_file():
        raise StageFailure("contract", f"contract fixture missing: {CONTRACT_PATH}")
    contract = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    validate_contract(contract, REPO_ROOT)
    if not TFVARS_PATH.is_file():
        raise StageFailure("contract", f"synthetic tfvars fixture missing: {TFVARS_PATH}")
    return contract


# --------------------------------------------------------------------------- #
# repository universe and digests
# --------------------------------------------------------------------------- #

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tracked_infra_files(repo_root: Path) -> list[str]:
    """The copy universe: git-TRACKED files under infra/aws, excluding bootstrap/.

    Tracked-only is what makes the probe inspect the repository rather than the
    operator's machine: untracked local files (real *.tfvars, backend.hcl, module
    lockfiles, caches) never enter the copy, so no real value can steer the plan and
    `terraform.tfvars` auto-loading has nothing to load.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo_root), "ls-files", "-z", "--", str(INFRA_REL)],
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0:
        raise StageFailure("copy", f"git ls-files failed: {proc.stderr.decode(errors='replace').strip()}")
    rels = [r for r in proc.stdout.decode().split("\0") if r]
    prefix = str(INFRA_REL) + "/"
    bootstrap = prefix + "bootstrap/"
    out = sorted(r[len(prefix):] for r in rels if r.startswith(prefix) and not r.startswith(bootstrap))
    if not out:
        raise StageFailure("copy", "git reports no tracked files under infra/aws — refusing")
    for required in ("main.tf", "providers.tf", LOCK_REL):
        if required not in out:
            raise StageFailure("copy", f"tracked universe lacks {required} — refusing")
    return out


def make_copy(repo_root: Path, dest: Path) -> dict[str, str]:
    """Copy the tracked universe into dest; return {relpath: sha256} of the source."""
    digests: dict[str, str] = {}
    for rel in tracked_infra_files(repo_root):
        src = repo_root / INFRA_REL / rel
        if not src.is_file():
            raise StageFailure("copy", f"tracked file missing from working tree: {rel}")
        dst = dest / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        digests[rel] = sha256_file(src)
    return digests


def bind_copy(copy_dir: Path, repo_root: Path, stage: str) -> None:
    """Prove the copy is byte-identical to the repository working tree, both ways.

    Runs at copy time AND again after the plan (immediately before the JSON
    assertions), so a copy doctored mid-run — the checker inspecting a fixture instead
    of the actual root — is caught. The checker-authored backend override is the single
    permitted extra file and must carry exactly the expected bytes.
    """
    expected = set(tracked_infra_files(repo_root))
    actual = {
        str(p.relative_to(copy_dir))
        for p in copy_dir.rglob("*")
        if p.is_file()
    }
    override_present = OVERRIDE_NAME in actual
    actual.discard(OVERRIDE_NAME)
    if actual - expected:
        extra = sorted(actual - expected)[0]
        raise StageFailure(stage, f"unexpected file in the execution copy: {extra}")
    if expected - actual:
        missing = sorted(expected - actual)[0]
        raise StageFailure(stage, f"copy digest mismatch vs repo working tree: {missing} (missing)")
    for rel in sorted(expected):
        if sha256_file(copy_dir / rel) != sha256_file(repo_root / INFRA_REL / rel):
            raise StageFailure(stage, f"copy digest mismatch vs repo working tree: {rel}")
    if override_present:
        if (copy_dir / OVERRIDE_NAME).read_text(encoding="utf-8") != OVERRIDE_CONTENT:
            raise StageFailure(stage, f"{OVERRIDE_NAME} does not carry the expected local-backend bytes")


# --------------------------------------------------------------------------- #
# provider mirror (filesystem-mirror/cache-only; fail closed)
# --------------------------------------------------------------------------- #

def h1_dirhash(directory: Path) -> str:
    """The h1 dirhash OpenTofu records for an unpacked provider directory.

    Validated against the committed lockfile: the local darwin_arm64 cache directory
    hashes to an entry of infra/aws/.terraform.lock.hcl with exactly this procedure.
    """
    entries = []
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        rel = path.relative_to(directory).as_posix()
        entries.append(f"{sha256_file(path)}  {rel}\n")
    entries.sort(key=lambda line: line.split("  ", 1)[1])
    summary = "".join(entries).encode()
    return "h1:" + base64.b64encode(hashlib.sha256(summary).digest()).decode()


def lock_h1_set(lock_text: str, provider_source: str) -> set[str]:
    block = re.search(
        r'provider\s+"' + re.escape(provider_source) + r'"\s*\{(.*?)\n\}',
        lock_text,
        re.DOTALL,
    )
    if not block:
        raise StageFailure("mirror", f"committed lockfile has no block for {provider_source}")
    return set(re.findall(r'"(h1:[^"]+)"', block.group(1)))


# Cache roots where a verified provider may legitimately already exist — the same
# closed set scripts/check_toolchain_integrity.py classifies. CI's revision_reader /
# iam module-test inits populate the module caches before this guard's step runs.
CACHE_ROOTS = (
    INFRA_REL,
    INFRA_REL / "modules" / "revision_reader",
    INFRA_REL / "modules" / "iam",
)


def build_mirror(repo_root: Path, workdir: Path, contract: dict) -> Path:
    """Build a filesystem mirror from an already-present verified cache. Fail closed.

    Assurance chain: the unpacked directory's h1 MUST appear in the COMMITTED root
    lockfile (so the mirror can only serve bytes the repository already pinned), and
    the extracted binary must satisfy infra/aws/provider-binary-pin.json — a sha256
    match where a pin exists, otherwise the platform must be explicitly
    lockfile-verified-allowlisted. `tofu init` then verifies the same h1 again itself.
    """
    source = contract["provider_source"]
    version = contract["provider_version"]
    lock_text = (repo_root / INFRA_REL / LOCK_REL).read_text(encoding="utf-8")
    permitted_h1 = lock_h1_set(lock_text, source)
    if not permitted_h1:
        raise StageFailure("mirror", "committed lockfile records no h1 hashes — cannot verify a mirror")
    pin = json.loads(PIN_PATH.read_text(encoding="utf-8")) if PIN_PATH.is_file() else {}
    if not pin:
        raise StageFailure("mirror", f"{PIN_PATH} missing — binaries unverifiable")

    rejected: list[str] = []
    for cache_root in CACHE_ROOTS:
        version_dir = repo_root / cache_root / ".terraform" / "providers" / source / version
        if not version_dir.is_dir():
            continue
        for platform_dir in sorted(p for p in version_dir.iterdir() if p.is_dir()):
            platform = platform_dir.name
            actual_h1 = h1_dirhash(platform_dir)
            if actual_h1 not in permitted_h1:
                rejected.append(f"{platform_dir}: h1 {actual_h1} not in the committed lockfile")
                continue
            binaries = [p for p in platform_dir.rglob("terraform-provider-aws*") if p.is_file()]
            if not binaries:
                rejected.append(f"{platform_dir}: no provider binary")
                continue
            pinned = (pin.get("binaries") or {}).get(platform)
            if pinned is not None:
                if any(sha256_file(b) != pinned for b in binaries):
                    rejected.append(f"{platform_dir}: binary sha256 does not match provider-binary-pin.json")
                    continue
            elif platform not in (pin.get("lockfile_verified_platforms") or []):
                rejected.append(f"{platform_dir}: platform neither binary-pinned nor allowlisted")
                continue
            mirror = workdir / "mirror"
            target = mirror / source / version / platform
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(platform_dir, target)
            return mirror

    detail = "; ".join(rejected) if rejected else "no provider cache present at any expected root"
    raise StageFailure(
        "mirror",
        f"no verified {source} {version} cache to mirror from — refusing any network fallback ({detail})",
    )


def write_tofurc(workdir: Path, mirror: Path, contract: dict) -> Path:
    """CLI config for the CONSTRUCTED subprocess env only: mirror-only, no direct."""
    tofurc = workdir / "root-wiring.tofurc"
    namespace = "/".join(contract["provider_source"].split("/")[:2])  # host/namespace
    tofurc.write_text(
        "provider_installation {\n"
        "  filesystem_mirror {\n"
        f'    path    = "{mirror.as_posix()}"\n'
        f'    include = ["{namespace}/*"]\n'
        "  }\n"
        "  # No direct {} block: registry/network installation is not a fallback here.\n"
        "}\n",
        encoding="utf-8",
    )
    return tofurc


# --------------------------------------------------------------------------- #
# synthetic STS stub
# --------------------------------------------------------------------------- #

class _StubState:
    def __init__(self, allowed_action: str, account: str) -> None:
        self.allowed_action = allowed_action
        self.response = _STS_RESPONSE_TEMPLATE.format(account=account).encode()
        self.accepted: list[str] = []
        self.violations: list[str] = []
        self.lock = threading.Lock()


class _StubHandler(http.server.BaseHTTPRequestHandler):
    state: _StubState  # injected per server

    def _reject(self, reason: str) -> None:
        with self.state.lock:
            self.state.violations.append(reason)
        body = b"<Error>synthetic root-wiring stub: unexpected request</Error>"
        self.send_response(400)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        import urllib.parse

        host = (self.headers.get("Host") or "").split(":")[0]
        try:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length).decode(errors="replace")
        except (ValueError, OSError) as exc:
            # ADV-B3-F6: a malformed request body must be a RECORDED rejection, not an
            # unlogged handler exception.
            self._reject(f"malformed request body: {exc}")
            return
        action = urllib.parse.parse_qs(body).get("Action", [""])[0]
        if self.path != "/":
            self._reject(f"unexpected path {self.path!r} (action {action!r})")
            return
        if host not in ("127.0.0.1", "localhost"):
            self._reject(f"unexpected host {host!r} (action {action!r})")
            return
        if action != self.state.allowed_action:
            self._reject(f"unexpected action {action!r}")
            return
        with self.state.lock:
            self.state.accepted.append(action)
        self.send_response(200)
        self.send_header("Content-Type", "text/xml")
        self.send_header("Content-Length", str(len(self.state.response)))
        self.end_headers()
        self.wfile.write(self.state.response)

    def _reject_method(self) -> None:
        self._reject(f"unexpected {self.command} {self.path!r}")

    # ADV-B3-F6: every non-POST method is a recorded 400 — BaseHTTPRequestHandler's
    # default 501 for an undefined do_* would reject WITHOUT recording the violation.
    do_GET = _reject_method     # noqa: N815 - http.server dispatches on these names
    do_PUT = _reject_method     # noqa: N815
    do_DELETE = _reject_method  # noqa: N815
    do_PATCH = _reject_method   # noqa: N815
    do_OPTIONS = _reject_method  # noqa: N815
    do_HEAD = _reject_method    # noqa: N815
    do_TRACE = _reject_method   # noqa: N815
    do_CONNECT = _reject_method  # noqa: N815

    def log_message(self, *_args) -> None:  # silence default stderr logging
        return


class StsStub:
    """Localhost-only synthetic STS. Ephemeral port; accepts ONLY GetCallerIdentity."""

    def __init__(self, allowed_action: str, account: str) -> None:
        self.state = _StubState(allowed_action, account)
        handler = type("BoundHandler", (_StubHandler,), {"state": self.state})
        self._server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
        self._server.daemon_threads = True
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "StsStub":
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()


# --------------------------------------------------------------------------- #
# constructed subprocess environment
# --------------------------------------------------------------------------- #

_SHORT_TMP: Path | None = None


def short_tmpdir() -> Path:
    """A dedicated SHORT scratch directory for the OpenTofu subprocess.

    go-plugin rendezvous uses an AF_UNIX socket under TMPDIR, and unix socket paths
    are capped (~104 bytes on macOS, 108 on Linux). A TMPDIR nested under the work
    directory overruns the cap when RUNNER_TEMP is itself deep — the provider then
    dies at launch with 'Failed to read any lines from plugin's stdout'. So the
    subprocess TMPDIR is created directly under the system temp root, independent of
    work-directory depth, and removed by main()'s cleanup.
    """
    global _SHORT_TMP
    if _SHORT_TMP is None:
        _SHORT_TMP = Path(tempfile.mkdtemp(prefix="rw-tmp-"))
    return _SHORT_TMP


def constructed_env(workdir: Path, run_dir: Path, port: int, tofurc: Path) -> dict[str, str]:
    """The ONLY environment OpenTofu sees. Built from scratch — never inherited.

    No AWS_PROFILE / AWS_CONFIG_FILE / AWS_SHARED_CREDENTIALS_FILE / proxy variables
    exist here at all; HOME is an empty work-scoped directory, so no dotfile is
    reachable; the metadata source is disabled; the environment points every SDK
    client at the localhost stub (globally AND service-specifically for STS). Honest
    limit: a provider-block `endpoints` argument in the CONFIGURATION outranks the
    environment — that is a battery defect class (rogue_endpoint_routing), and the
    credentials here authenticate nowhere real regardless. TF_CLI_CONFIG_FILE / TF_DATA_DIR live exclusively inside this dict, so the
    step-scoped FORBIDDEN_ENV contract of check_toolchain_integrity is never violated
    in the ambient environment.
    """
    tofu = shutil.which("tofu")
    if not tofu:
        raise StageFailure("env", "'tofu' not found on PATH")
    home = workdir / "home"
    home.mkdir(exist_ok=True)
    return {
        "PATH": f"{Path(tofu).parent}:/usr/bin:/bin",
        "HOME": str(home),
        "TMPDIR": str(short_tmpdir()),
        "LANG": "C",
        "TF_CLI_CONFIG_FILE": str(tofurc),
        "TF_DATA_DIR": str(run_dir / "tfdata"),
        "TF_IN_AUTOMATION": "1",
        "TF_PLUGIN_CACHE_DIR": str(workdir / "plugin-cache"),
        "CHECKPOINT_DISABLE": "1",
        "NO_COLOR": "1",
        "AWS_ACCESS_KEY_ID": SYNTHETIC_ACCESS_KEY,
        "AWS_SECRET_ACCESS_KEY": SYNTHETIC_SECRET_KEY,
        "AWS_REGION": "us-east-1",
        "AWS_DEFAULT_REGION": "us-east-1",
        "AWS_ENDPOINT_URL": f"http://127.0.0.1:{port}",
        # Belt and braces for the one service actually called: the service-specific
        # override outranks everything else in the SDK's endpoint resolution, so STS
        # containment does not rest on the global variable alone.
        "AWS_ENDPOINT_URL_STS": f"http://127.0.0.1:{port}",
        "AWS_EC2_METADATA_DISABLED": "true",
        "AWS_MAX_ATTEMPTS": "2",
    }


def run_tofu(args: list[str], cwd: Path, env: dict[str, str], timeout: int = 600) -> subprocess.CompletedProcess:
    (Path(env["TF_PLUGIN_CACHE_DIR"])).mkdir(exist_ok=True)
    return subprocess.run(
        ["tofu", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# --------------------------------------------------------------------------- #
# plan-JSON assertions (primary control)
# --------------------------------------------------------------------------- #

def _walk_resources(module: dict, prefix: str = "") -> list[tuple[str, str | None, str, str]]:
    out = []
    for resource in module.get("resources", []) or []:
        out.append(
            (
                prefix + resource.get("address", "?"),
                resource.get("provider_config_key"),
                resource.get("mode", "?"),
                resource.get("type", "?"),
            )
        )
    for name, call in (module.get("module_calls") or {}).items():
        out.extend(_walk_resources(call.get("module") or {}, f"{prefix}module.{name}."))
    return out


def assert_plan_json(plan: dict, contract: dict) -> list[str]:
    """A1–A7 over OpenTofu's own `configuration` representation. Fail closed."""
    failures: list[str] = []
    fmt = plan.get("format_version")
    if fmt not in contract["plan_json_format_versions"]:
        return [f"plan JSON format_version {fmt!r} outside the proven set "
                f"{contract['plan_json_format_versions']} — refusing to interpret drifted output"]
    cfg = plan.get("configuration")
    if not isinstance(cfg, dict) or "root_module" not in cfg:
        return ["plan JSON carries no configuration.root_module — refusing"]

    alias_key = contract["alias_key"]
    provider_config = cfg.get("provider_config") or {}

    # A6: the aws-named provider-config universe is closed (shadow-provider control).
    aws_universe = sorted(k for k, v in provider_config.items() if (v or {}).get("name") == "aws")
    if aws_universe != sorted(contract["aws_provider_config_universe"]):
        failures.append(
            f"aws provider-config universe {aws_universe} != expected "
            f"{sorted(contract['aws_provider_config_universe'])}"
        )

    # A1: the aliased provider exists and its expression-key surface is exact.
    alias = provider_config.get(alias_key)
    if not alias:
        failures.append(f"provider_config[{alias_key!r}] is absent")
    else:
        if alias.get("alias") != contract["alias_name"] or alias.get("name") != "aws":
            failures.append(f"provider_config[{alias_key!r}] is not aws alias {contract['alias_name']!r}")
        surface = sorted((alias.get("expressions") or {}).keys())
        permitted = sorted(contract["alias_allowed_expression_keys"])
        if surface != permitted:
            failures.append(
                f"aliased provider argument surface {surface} != permitted {permitted}"
            )
        region = (alias.get("expressions") or {}).get("region") or {}
        if sorted(region.get("references") or []) != sorted(contract["alias_region_references"]):
            failures.append(
                f"aliased provider region references {region.get('references')} != "
                f"{contract['alias_region_references']}"
            )

    # A4: the default provider keeps its region + default_tags contract (the eight-tag
    # set must stay on the DEFAULT provider — the inverse regression).
    default = provider_config.get("aws")
    if not default:
        failures.append("default aws provider_config is absent")
    else:
        default_surface = sorted((default.get("expressions") or {}).keys())
        if default_surface != sorted(contract["default_provider_expression_keys"]):
            failures.append(
                f"default provider argument surface {default_surface} != expected "
                f"{sorted(contract['default_provider_expression_keys'])}"
            )

    # A5: exactly one root module call sources the governed reader module.
    module_calls = (cfg.get("root_module") or {}).get("module_calls") or {}
    reader_calls = sorted(
        name for name, call in module_calls.items()
        if (call or {}).get("source") == contract["reader_module_source"]
    )
    if reader_calls != [contract["reader_module_name"]]:
        failures.append(
            f"{len(reader_calls)} module calls source {contract['reader_module_source']} "
            f"(expected 1): {reader_calls}"
        )

    # A2: the exact governed roster, every row on the aliased provider key.
    resources = _walk_resources(cfg.get("root_module") or {})
    reader_prefix = f"module.{contract['reader_module_name']}."
    reader_aws = {
        addr: key for addr, key, _mode, rtype in resources
        if addr.startswith(reader_prefix) and rtype.startswith("aws_")
    }
    roster = {row["address"] for row in contract["reader_resource_roster"]}
    if set(reader_aws) != roster:
        extra = sorted(set(reader_aws) - roster)
        missing = sorted(roster - set(reader_aws))
        failures.append(
            f"reader aws resource roster mismatch — unexpected: {extra or 'none'}; "
            f"missing: {missing or 'none'}"
        )
    misbound = {addr: key for addr, key in reader_aws.items() if key != alias_key}
    if misbound:
        by_key: dict[str, int] = {}
        for key in misbound.values():
            by_key[str(key)] = by_key.get(str(key), 0) + 1
        detail = ", ".join(f"'{k}': {n}/{len(reader_aws)}" for k, n in sorted(by_key.items()))
        failures.append(
            f"reader resources bound to provider key {detail} (expected {alias_key!r})"
        )

    # A3: the untagged alias must not leak outside the reader module.
    leaked = sorted(
        addr for addr, key, _mode, _rtype in resources
        if not addr.startswith(reader_prefix) and key == alias_key
    )
    if leaked:
        failures.append(f"{alias_key} consumed outside the reader module: {leaked}")

    # A7: every non-reader aws resource stays on the DEFAULT provider key.
    stray = sorted(
        f"{addr}->{key}" for addr, key, _mode, rtype in resources
        if not addr.startswith(reader_prefix) and rtype.startswith("aws_") and key != "aws"
    )
    if stray:
        failures.append(f"non-reader aws resources not on the default provider: {stray}")

    return failures


# --------------------------------------------------------------------------- #
# graph witness (secondary independent control)
# --------------------------------------------------------------------------- #

_DOT_EDGE = re.compile(r'"((?:[^"\\]|\\.)+)"\s*->\s*"((?:[^"\\]|\\.)+)"')
_NODE_SUFFIX = re.compile(r"\s*\((expand|close|expand, reference)\)$")


def _dot_node(raw: str) -> tuple[str, str]:
    """(address, kind). kind: 'plain', 'expand', 'close', or 'expand, reference'.

    ADV-B3-F3: the kinds must be KEPT. OpenTofu's graph carries both a provider
    CONFIGURE node and a provider CLOSE node; collapsing them merges the close node's
    outgoing resource edges into the configure node and drowns the configure node's
    own reference edges, which is exactly the set G2 asserts over.
    """
    addr = raw.replace('\\"', '"')
    if addr.startswith("[root] "):
        addr = addr[len("[root] "):]
    match = _NODE_SUFFIX.search(addr)
    kind = match.group(1) if match else "plain"
    return _NODE_SUFFIX.sub("", addr), kind


def graph_edges(dot_text: str) -> list[tuple[tuple[str, str], tuple[str, str]]]:
    return [(_dot_node(a), _dot_node(b)) for a, b in _DOT_EDGE.findall(dot_text)]


def assert_graph(dot_text: str, contract: dict) -> tuple[list[str], set[str]]:
    """G1–G3 over the DOT witness. Honest scope: the DOT is transitively REDUCED
    (proven 2026-08-15 — only a subset of the reader→provider edges survives), so G1
    asserts the SET of referenced provider targets, never an edge roster, and the DOT
    is blind to literal provider arguments — plan-JSON A1 is the PRIMARY control for
    the argument surface. What the DOT does contribute independently: the provider
    routing signal (G1/G3) and every reference-valued expression on the alias's
    configure node (G2) — variables, locals, data sources, resource attributes alike.
    Returns (failures, reader→aws-provider target set) for the JSON cross-check.
    """
    failures: list[str] = []
    edges = graph_edges(dot_text)
    if not edges:
        return (["graph DOT parsed to zero edges — refusing drifted output"], set())

    provider_prefix = f'provider["{contract["provider_source"]}"]'
    alias_node = f'{provider_prefix}.{contract["alias_name"]}'
    reader_prefix = f"module.{contract['reader_module_name']}."

    aws_provider_nodes = {
        addr for edge in edges for addr, _kind in edge if addr.startswith(provider_prefix)
    }
    if alias_node not in aws_provider_nodes:
        failures.append(f"graph: aliased provider node {alias_node} is absent")

    # G1: every aws-provider CONFIGURE node a reader node references is the alias.
    reader_targets = {
        b for (a, _ka), (b, kb) in edges
        if a.startswith(reader_prefix) and b.startswith(provider_prefix) and kb != "close"
    }
    if not reader_targets:
        failures.append("graph: no reader→aws-provider reference at all — refusing drifted output")
    elif reader_targets != {alias_node}:
        failures.append(
            f"graph: reader resources reference aws provider nodes {sorted(reader_targets)} "
            f"(expected exactly {{{alias_node}}})"
        )

    # G2: the alias CONFIGURE node's complete outgoing reference set is exactly the
    # contract's. With the close node separated, this covers EVERY reference-valued
    # expression on the alias — var, local, data source, resource attribute, module
    # output — not just var./local. prefixes.
    alias_refs = {
        b for (a, ka), (b, _kb) in edges if a == alias_node and ka != "close"
    }
    if alias_refs != set(contract["alias_region_references"]):
        failures.append(
            f"graph: aliased provider references {sorted(alias_refs)} != "
            f"{sorted(contract['alias_region_references'])}"
        )

    # G3: nothing outside the reader module (bar the synthetic root node) points at
    # the alias CONFIGURE node.
    outside = {
        a for (a, _ka), (b, kb) in edges
        if b == alias_node and kb != "close" and a != "root" and not a.startswith(reader_prefix)
    }
    if outside:
        failures.append(f"graph: non-reader nodes reference the alias: {sorted(outside)}")

    return failures, reader_targets


# --------------------------------------------------------------------------- #
# pipeline
# --------------------------------------------------------------------------- #

class RunRecord:
    def __init__(self, name: str) -> None:
        self.name = name
        self.failure: StageFailure | None = None
        self.plan_file_written_on_failure = False
        self.stub_violations: list[str] = []
        self.stub_accepted: list[str] = []


def run_pipeline(
    name: str,
    workdir: Path,
    tofurc: Path,
    contract: dict,
    repo_root: Path,
    mutation=None,
    doctor_after_bind=None,
    run_fmt: bool = True,
) -> RunRecord:
    """copy → bind → [mutate] → fmt → override → init → validate → stub-plan →
    re-bind → JSON assertions → graph assertions → stub audit. First failure wins."""
    record = RunRecord(name)
    run_dir = workdir / "runs" / name
    copy_dir = run_dir / "root"
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        make_copy(repo_root, copy_dir)
        pristine = mutation is None and doctor_after_bind is None
        if pristine or doctor_after_bind is not None:
            bind_copy(copy_dir, repo_root, "bind")
        if mutation is not None:
            mutation(copy_dir)
        if doctor_after_bind is not None:
            doctor_after_bind(copy_dir)

        env_probe = constructed_env(workdir, run_dir, port=1, tofurc=tofurc)
        if run_fmt and pristine:
            # BEFORE the override exists, so only repository files are judged.
            fmt = run_tofu(["fmt", "-check", "-diff", "."], copy_dir, env_probe)
            if fmt.returncode != 0:
                raise StageFailure("fmt", f"tofu fmt -check failed:\n{fmt.stdout}{fmt.stderr}")

        (copy_dir / OVERRIDE_NAME).write_text(OVERRIDE_CONTENT, encoding="utf-8")

        init = run_tofu(["init", "-input=false"], copy_dir, env_probe)
        if init.returncode != 0:
            raise StageFailure("init", f"offline init failed (mirror-only, no fallback):\n{init.stderr.strip()}")

        validate = run_tofu(["validate"], copy_dir, env_probe)
        if validate.returncode != 0:
            raise StageFailure("validate", f"tofu validate failed:\n{validate.stderr.strip()}")

        stub_cfg = contract["stub"]
        plan_path = run_dir / "probe.tfplan"
        with StsStub(stub_cfg["allowed_action"], stub_cfg["synthetic_account"]) as stub:
            env = constructed_env(workdir, run_dir, port=stub.port, tofurc=tofurc)
            plan = run_tofu(
                [
                    "plan",
                    "-input=false",
                    "-refresh=false",
                    f"-var-file={TFVARS_PATH}",
                    f"-out={plan_path}",
                ],
                copy_dir,
                env,
            )
        record.stub_violations = list(stub.state.violations)
        record.stub_accepted = list(stub.state.accepted)
        if plan.returncode != 0:
            # OpenTofu 1.12.5 writes the errored plan FILE even at exit 1; record that
            # so the battery can assert the quirk, and NEVER parse it.
            record.plan_file_written_on_failure = plan_path.is_file()
            raise StageFailure("plan", f"tofu plan exited {plan.returncode}:\n{plan.stderr.strip()}")

        # Fixture-substitution control: the tree the plan just consumed must STILL be
        # the repository working tree, byte for byte.
        if pristine or doctor_after_bind is not None:
            bind_copy(copy_dir, repo_root, "rebind")

        show = run_tofu(["show", "-json", str(plan_path)], copy_dir, env_probe)
        if show.returncode != 0:
            raise StageFailure("show", f"tofu show -json failed:\n{show.stderr.strip()}")
        plan_json = json.loads(show.stdout)
        json_failures = assert_plan_json(plan_json, contract)
        if json_failures:
            raise StageFailure("assert_json", "; ".join(json_failures))

        graph = run_tofu(["graph"], copy_dir, env_probe)
        if graph.returncode != 0:
            raise StageFailure("graph", f"tofu graph failed:\n{graph.stderr.strip()}")
        graph_failures, reader_targets = assert_graph(graph.stdout, contract)
        if graph_failures:
            raise StageFailure("assert_graph", "; ".join(graph_failures))

        # Cross-witness agreement: the independent DOT witness must name the same
        # provider the plan JSON bound every reader resource to.
        alias_node = f'provider["{contract["provider_source"]}"].{contract["alias_name"]}'
        if reader_targets != {alias_node}:
            raise StageFailure("cross_check", "graph witness disagrees with the plan-JSON provider mapping")

        if record.stub_violations:
            raise StageFailure("stub_audit", f"stub observed unexpected requests: {record.stub_violations}")
        if not record.stub_accepted:
            raise StageFailure("stub_audit", "stub observed zero GetCallerIdentity calls — the plan cannot have configured the provider")
    except StageFailure as failure:
        record.failure = failure
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)
    return record


# --------------------------------------------------------------------------- #
# negative mutation battery
# --------------------------------------------------------------------------- #

def _replace_in(copy_dir: Path, rel: str, old: str, new: str) -> None:
    path = copy_dir / rel
    text = path.read_text(encoding="utf-8")
    if old not in text:
        raise StageFailure("mutation", f"mutation anchor not found in {rel} — the repository drifted; re-pin the battery")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


_PROVIDERS_MAP = "  providers = {\n    aws = aws.revision_reader\n  }\n"
_ALIAS_BLOCK = 'provider "aws" {\n  alias  = "revision_reader"\n  region = var.aws_region\n}'


def _inject_alias_argument(copy_dir: Path, argument_lines: str) -> None:
    """Inject argument lines into the aliased provider block."""
    _replace_in(
        copy_dir,
        "providers.tf",
        _ALIAS_BLOCK,
        _ALIAS_BLOCK[:-1] + argument_lines + "}",
    )


def _mut_map_removed(copy_dir: Path) -> None:
    _replace_in(copy_dir, "main.tf", _PROVIDERS_MAP, "")


def _mut_map_default(copy_dir: Path) -> None:
    _replace_in(copy_dir, "main.tf", "aws = aws.revision_reader", "aws = aws")


def _mut_map_other_alias(copy_dir: Path) -> None:
    _replace_in(copy_dir, "main.tf", "aws = aws.revision_reader", "aws = aws.other")
    _replace_in(
        copy_dir,
        "providers.tf",
        _ALIAS_BLOCK,
        _ALIAS_BLOCK + '\n\nprovider "aws" {\n  alias  = "other"\n  region = var.aws_region\n}',
    )


def _mut_default_tags(copy_dir: Path) -> None:
    _inject_alias_argument(copy_dir, '  default_tags {\n    tags = { Synthetic = "probe" }\n  }\n')


def _mut_ignore_tags(copy_dir: Path) -> None:
    _inject_alias_argument(copy_dir, '  ignore_tags {\n    keys = ["Synthetic"]\n  }\n')


def _mut_assume_role(copy_dir: Path) -> None:
    _inject_alias_argument(
        copy_dir,
        '  assume_role {\n    role_arn = "arn:aws:iam::000000000000:role/synthetic-rogue"\n  }\n',
    )


def _mut_profile(copy_dir: Path) -> None:
    _inject_alias_argument(copy_dir, '  profile = "synthetic-rogue"\n')


def _mut_static_credentials(copy_dir: Path) -> None:
    _inject_alias_argument(
        copy_dir,
        f'  access_key = "{SYNTHETIC_ACCESS_KEY}"\n  secret_key = "{SYNTHETIC_SECRET_KEY}"\n',
    )


def _mut_account_override(copy_dir: Path) -> None:
    # 111122223333 is registered in approved-account-registry.json (class
    # REPOSITORY_SYNTHETIC_ANCHOR; its provenance note records it as the AWS
    # documentation placeholder); any value other than the stub's synthetic
    # 000000000000 exercises the override.
    _inject_alias_argument(copy_dir, '  allowed_account_ids = ["111122223333"]\n')


def _mut_endpoint_routing(copy_dir: Path) -> None:
    _inject_alias_argument(copy_dir, '  endpoints {\n    sts = "http://127.0.0.1:1"\n  }\n')


def _mut_duplicate_alias(copy_dir: Path) -> None:
    _replace_in(copy_dir, "providers.tf", _ALIAS_BLOCK, _ALIAS_BLOCK + "\n\n" + _ALIAS_BLOCK)


def _mut_shadow_module(copy_dir: Path) -> None:
    main = copy_dir / "main.tf"
    text = main.read_text(encoding="utf-8")
    block = re.search(r'module "revision_reader" \{.*?\n\}\n', text, re.DOTALL)
    if not block:
        raise StageFailure("mutation", "reader module call not found for the shadow-module mutation")
    shadow = block.group(0).replace(
        'module "revision_reader" {', 'module "revision_reader_shadow" {', 1
    ).replace("local.name_prefix", '"synthetic-shadow"')
    main.write_text(text + "\n" + shadow, encoding="utf-8")


def _mut_new_reader_resource(copy_dir: Path) -> None:
    module_main = copy_dir / "modules" / "revision_reader" / "main.tf"
    with module_main.open("a", encoding="utf-8") as handle:
        handle.write(
            '\nresource "aws_cloudwatch_log_group" "synthetic_unrostered" {\n'
            '  name              = "synthetic-unrostered-probe"\n'
            "  retention_in_days = 1\n"
            "}\n"
        )


def _mut_missing_reader_resource(copy_dir: Path) -> None:
    module_main = copy_dir / "modules" / "revision_reader" / "main.tf"
    text = module_main.read_text(encoding="utf-8")
    block = re.search(r'resource "aws_ecr_lifecycle_policy" "reader" \{.*?\n\}\n', text, re.DOTALL)
    if not block:
        raise StageFailure("mutation", "aws_ecr_lifecycle_policy.reader not found for the roster-shrink mutation")
    module_main.write_text(text.replace(block.group(0), "", 1), encoding="utf-8")


def _mut_module_local_provider(copy_dir: Path) -> None:
    # A provider block declared INSIDE the reader module (the in-module shadow-provider
    # evasion). OpenTofu refuses to pass a providers map to a module that carries its
    # own provider configuration, so this dies at the native gates.
    module_main = copy_dir / "modules" / "revision_reader" / "main.tf"
    with module_main.open("a", encoding="utf-8") as handle:
        handle.write('\nprovider "aws" {\n  region = var.aws_region\n}\n')


def _mut_module_alias_passthrough(copy_dir: Path) -> None:
    # configuration_aliases passthrough: the module declares an extra alias, the root
    # maps it to the DEFAULT (tagged) provider, and one reader resource escapes to it.
    _replace_in(
        copy_dir,
        "modules/revision_reader/versions.tf",
        '    aws = {\n      source  = "hashicorp/aws"\n      version = ">= 6.55.0, < 6.56.0"\n    }',
        '    aws = {\n      source                = "hashicorp/aws"\n      version               = ">= 6.55.0, < 6.56.0"\n      configuration_aliases = [aws.rogue]\n    }',
    )
    _replace_in(
        copy_dir,
        "main.tf",
        "  providers = {\n    aws = aws.revision_reader\n  }\n",
        "  providers = {\n    aws       = aws.revision_reader\n    aws.rogue = aws\n  }\n",
    )
    _replace_in(
        copy_dir,
        "modules/revision_reader/main.tf",
        'resource "aws_ecr_lifecycle_policy" "reader" {\n  count = local.create_bootstrap\n',
        'resource "aws_ecr_lifecycle_policy" "reader" {\n  provider = aws.rogue\n  count    = local.create_bootstrap\n',
    )


def _mut_unexpected_action(copy_dir: Path) -> None:
    with (copy_dir / "main.tf").open("a", encoding="utf-8") as handle:
        handle.write('\ndata "aws_availability_zones" "synthetic_probe" {}\n')


def _doctor_comment(copy_dir: Path) -> None:
    with (copy_dir / "main.tf").open("a", encoding="utf-8") as handle:
        handle.write("# doctored after digest binding — the checker must catch this\n")


# Each row must fail AT expect_stage WITH expect_pattern. `expect_stub_violation`
# additionally requires the stub to have rejected a request; `expect_plan_file`
# additionally requires the errored-plan-file quirk to have been observed.
#
# PURE DATA on purpose: `mutation` / `doctor_after_bind` are FUNCTION NAMES resolved
# fail-closed by _resolve_mutation (a `_mut_*` / `_doctor_*` def in this module), never
# function objects — so this SECURITY_CRITICAL table stays review-pinnable by content
# (review_pin_control canonical digest) without binding interpreter-version-sensitive
# bytecode. Shrinking or reshaping the battery moves BOTH the reviewed digest and the
# independent id-set pin in tests/test_root_wiring_check.py.
BATTERY: list[dict] = [
    {"id": "providers_map_removed", "mutation": "_mut_map_removed",
     "expect_stage": "assert_json", "expect_pattern": r"bound to provider key 'aws':"},
    {"id": "providers_map_default_aws", "mutation": "_mut_map_default",
     "expect_stage": "assert_json", "expect_pattern": r"bound to provider key 'aws':"},
    {"id": "providers_map_other_alias", "mutation": "_mut_map_other_alias",
     "expect_stage": "assert_json", "expect_pattern": r"aws\.other"},
    {"id": "rogue_default_tags", "mutation": "_mut_default_tags",
     "expect_stage": "assert_json", "expect_pattern": r"argument surface \['default_tags', 'region'\] != permitted"},
    {"id": "rogue_ignore_tags", "mutation": "_mut_ignore_tags",
     "expect_stage": "assert_json", "expect_pattern": r"argument surface \['ignore_tags', 'region'\] != permitted"},
    {"id": "rogue_assume_role", "mutation": "_mut_assume_role",
     "expect_stage": "plan", "expect_pattern": r".", "expect_stub_violation": True},
    {"id": "rogue_profile", "mutation": "_mut_profile",
     "expect_stage": "plan", "expect_pattern": r"failed to get shared config profile", "expect_plan_file": True},
    {"id": "rogue_static_credentials", "mutation": "_mut_static_credentials",
     "expect_stage": "assert_json", "expect_pattern": r"argument surface \['access_key', 'region', 'secret_key'\] != permitted"},
    # Signatures observed live against tofu 1.12.5 / provider 6.55.0 (2026-08-15):
    # the stub's synthetic account is rejected by the override, and the rerouted STS
    # endpoint (127.0.0.1:1) refuses the connection. ADV-B3-F5: stage-only rows were
    # not decisive; these are.
    {"id": "rogue_account_override", "mutation": "_mut_account_override",
     "expect_stage": "plan", "expect_pattern": r"AWS account ID not allowed: 000000000000"},
    {"id": "rogue_endpoint_routing", "mutation": "_mut_endpoint_routing",
     "expect_stage": "plan", "expect_pattern": r"dial tcp 127\.0\.0\.1:1: connect: connection refused"},
    # The design lab observed this at `validate` (its work dir was initialised before
    # the mutation); a fresh per-copy pipeline hits the same native backstop one stage
    # earlier, at `init`, which refuses an invalid configuration outright.
    {"id": "shadow_provider_duplicate_alias", "mutation": "_mut_duplicate_alias",
     "expect_stage": "init", "expect_pattern": r"Duplicate provider configuration"},
    # ADV-B3-F4: the two in-module evasions proven by the ORACLE, not by shape analogy.
    {"id": "module_local_provider", "mutation": "_mut_module_local_provider",
     "expect_stage": "init", "expect_pattern": r"Cannot override provider configuration"},
    {"id": "module_alias_passthrough", "mutation": "_mut_module_alias_passthrough",
     "expect_stage": "assert_json", "expect_pattern": r"bound to provider key 'aws': 1/15"},
    {"id": "shadow_module", "mutation": "_mut_shadow_module",
     "expect_stage": "assert_json", "expect_pattern": r"2 module calls source \./modules/revision_reader \(expected 1\)"},
    {"id": "new_reader_resource_unrostered", "mutation": "_mut_new_reader_resource",
     "expect_stage": "assert_json", "expect_pattern": r"roster mismatch — unexpected: \['module\.revision_reader\.aws_cloudwatch_log_group\.synthetic_unrostered'\]"},
    {"id": "missing_reader_resource", "mutation": "_mut_missing_reader_resource",
     "expect_stage": "assert_json", "expect_pattern": r"roster mismatch — unexpected: none; missing: \['module\.revision_reader\.aws_ecr_lifecycle_policy\.reader'\]"},
    {"id": "unexpected_stub_action", "mutation": "_mut_unexpected_action",
     "expect_stage": "plan", "expect_pattern": r".", "expect_stub_violation": True},
    {"id": "fixture_substitution", "doctor_after_bind": "_doctor_comment",
     "expect_stage": "rebind", "expect_pattern": r"copy digest mismatch vs repo working tree: main\.tf"},
]


def _resolve_mutation(name: str):
    """Resolve a battery row's function NAME to this module's def. Fail closed: only
    `_mut_*` / `_doctor_*` names defined here resolve; anything else is a battery
    authoring error, never a silent no-op."""
    if not isinstance(name, str) or not name.startswith(("_mut_", "_doctor_")):
        raise StageFailure("mutation", f"battery names a non-battery symbol {name!r}")
    fn = globals().get(name)
    if not callable(fn):
        raise StageFailure("mutation", f"battery names unknown mutation {name!r}")
    return fn


def judge_battery_row(row: dict, record: RunRecord) -> str | None:
    """None when the row behaved exactly as expected; otherwise the defect."""
    if record.failure is None:
        return f"{row['id']}: the mutation was NOT detected (pipeline passed)"
    if record.failure.stage != row["expect_stage"]:
        return (
            f"{row['id']}: failed at stage '{record.failure.stage}' "
            f"(expected '{row['expect_stage']}'): {record.failure.message}"
        )
    if not re.search(row["expect_pattern"], record.failure.message):
        return (
            f"{row['id']}: failure signature mismatch at '{record.failure.stage}': "
            f"{record.failure.message}"
        )
    if row.get("expect_stub_violation") and not record.stub_violations:
        return f"{row['id']}: expected the stub to reject a request, but it recorded none"
    if row.get("expect_plan_file") and not record.plan_file_written_on_failure:
        return (
            f"{row['id']}: the errored-plan-file quirk was not observed — "
            "re-prove the plan-exit-code gate against this OpenTofu version"
        )
    return None


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #

def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("full", "positive", "contract"), default="contract",
                        help="full = positive control + complete negative battery (the graded "
                             "CI invocation, pinned to '--mode full' by the unit suite); "
                             "positive = P1 only; contract (default) = strict fixture "
                             "validation against the repository-authoritative sources, no "
                             "OpenTofu — the mode the site-coverage matrix executes")
    parser.add_argument("--work-dir", default=None,
                        help="working directory (default: $RUNNER_TEMP or a mkdtemp, never the repo)")
    parser.add_argument("--repo-root", default=str(REPO_ROOT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    repo_root = Path(args.repo_root).resolve()
    base = args.work_dir or os.environ.get("RUNNER_TEMP") or tempfile.gettempdir()
    workdir = Path(tempfile.mkdtemp(prefix="root-wiring-", dir=base))
    try:
        workdir.relative_to(repo_root)
        print(f"refusing to work inside the repository: {workdir}", file=sys.stderr)
        return 1
    except ValueError:
        pass

    results: list[dict] = []
    ok = True
    try:
        contract = load_contract()
        if args.mode == "contract":
            results.append({"row": "contract-validation", "ok": True,
                            "detail": f"{len(_CONTRACT_REQUIRED_KEYS)} contract keys validated "
                                      "against the repository-authoritative sources"})
        else:
            mirror = build_mirror(repo_root, workdir, contract)
            tofurc = write_tofurc(workdir, mirror, contract)

            p1 = run_pipeline("P1-positive-control", workdir, tofurc, contract, repo_root)
            if p1.failure is not None:
                ok = False
                results.append({"row": "P1-positive-control", "ok": False,
                                "detail": f"stage {p1.failure.stage}: {p1.failure.message}"})
            else:
                results.append({"row": "P1-positive-control", "ok": True,
                                "detail": f"all assertions hold; stub accepted "
                                          f"{len(p1.stub_accepted)}x GetCallerIdentity, 0 violations"})

        if args.mode == "full":
            for row in BATTERY:
                record = run_pipeline(
                    f"battery-{row['id']}", workdir, tofurc, contract, repo_root,
                    mutation=_resolve_mutation(row["mutation"]) if "mutation" in row else None,
                    doctor_after_bind=(
                        _resolve_mutation(row["doctor_after_bind"])
                        if "doctor_after_bind" in row else None
                    ),
                )
                verdict = judge_battery_row(row, record)
                if verdict is not None:
                    ok = False
                    results.append({"row": row["id"], "ok": False, "detail": verdict})
                else:
                    results.append({"row": row["id"], "ok": True,
                                    "detail": f"detected at '{record.failure.stage}' as expected"})
    except StageFailure as failure:
        ok = False
        results.append({"row": "environment", "ok": False,
                        "detail": f"stage {failure.stage}: {failure.message}"})
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
        if _SHORT_TMP is not None:
            shutil.rmtree(_SHORT_TMP, ignore_errors=True)

    if args.json:
        print(json.dumps({"ok": ok, "mode": args.mode, "results": results}, indent=2))
    else:
        for row in results:
            print(f"  {'OK  ' if row['ok'] else 'FAIL'}  {row['row']}: {row['detail']}")
        print()
        if ok:
            held = {"full": "positive control and battery",
                    "positive": "positive control P1",
                    "contract": "contract validation"}[args.mode]
            print(f"ROOT WIRING: {held} hold(s).")
        else:
            print("ROOT WIRING: one or more controls FAILED.", file=sys.stderr)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
