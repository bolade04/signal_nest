#!/usr/bin/env python3
"""Negative controls for the ADOPTED vulnerability policy (P6-CI-6 closure, operator order 2026-10-19).

The P6-CI-6 closure assessment showed a CRITICAL finding merging clean in four classes: no fix
published, development scope (including the Semgrep tool lock and npm development packages), the
reader image without a fix, and the reader's installed tooling, which no scan covered (the Container
build job's installed security-tool venv is the same kind of population and is controlled the same
way). Each CASE here
takes ONE real report that THIS CI run produced (copied; never edited in place), applies ONE synthetic
change — an injected finding, a removed scanned package or an altered installed inventory — and runs
the real gate (security/gate.py, the same CLI and the same arguments as the gate steps in
.github/workflows/ci.yml) on the copy. Each case states its expected outcome, and its control summary
ends in NEGATIVE CONTROL DETECTED only when the gate produced exactly that outcome:

    violation        exit 3 and a VIOLATION line naming the injected finding
    excluded         exit 0 and an EXCLUDED line naming it (HIGH without a fix: listed, not failing)
    reported         exit 0 and a REPORTED line naming it (HIGH in development scope)
    incomplete       exit 4 and the expected incompleteness reason
    malformed        exit 2 and the expected reason
    pass             exit 0 (the unmutated baseline: copying a report is not what fails the cases)

    python3 security/vuln_policy_controls.py check <group> <reports-dir>

with <group> one of GROUPS: container and reader (the two security-scanned jobs) and, since the coverage-gap
tranche (operator order 2026-10-20), frontend, backend, migrations and integration — the four jobs that
install the API venv (backend also its lock toolchain, .locktools). The tooling cases there also cover the
reconciliation of an environment against its hash locks, the vendored-pin scan of its installed pip and the
truncation of its inventory below the gate's floor; the binary cases cover the Go binary analysis of the
Trivy and OpenTofu binaries the job executes; two image cases cover an installer left in a final image and a
language population truncated below the floor.

The case lists are pinned against the completeness steps of ci.yml by
security/test_vuln_policy_controls.py, in both directions. Every mutation asserts that it changed the
report or inventory; a mutation that changes nothing stops the run (exit 2) instead of testing nothing.
Injected findings are synthetic (VulnerabilityID CONTROL-<case>, or an advisory-style id where the
case is about the id); they exercise the gate's decision over the real report's own structure,
targets and packages, in the shapes real reports carry: every no-fix Status Trivy emits
(affected, will_not_fix, fix_deferred, end_of_life), a fix recorded without a FixedVersion, a
package identifier that matches no package, a vendored copy without a FilePath, and language
results other than Python (a Go binary in an image, node packages in a tooling scan). The reader
job's interpreter prefix is located through the `pythonLocation` environment variable that
actions/setup-python exports, exactly as the gate steps read `${{ env.pythonLocation }}`.
Stdlib only; no network; no scanner runs.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import sys

HERE = pathlib.Path(__file__).resolve().parent
SUMMARY_PREFIX = "control-vulnpol-"
WORK_DIR = "vulnpol-controls"


class ControlError(Exception):
    """A case could not be built faithfully (missing report, no package to inject into, a no-op mutation)."""


# The gate arguments of the REAL gate steps in .github/workflows/ci.yml (pinned there by the test).
LOCKS_ARGS = ["--kind", "filesystem", "--artifact", ".",
              "--expect-target", "package-lock.json", "--expect-target", "apps/api/requirements-runtime.txt",
              "--expect-target", "apps/api/requirements-dev.txt", "--expect-target", "apps/api/requirements-bootstrap.txt",
              "--expect-target", "apps/api/requirements-locktools.txt", "--expect-target", "scripts/requirements-gate.txt",
              "--expect-target", "scripts/requirements-gate-hashes.txt", "--expect-target", "security/requirements-semgrep.txt",
              "--expect-target", "apps/revision-reader/requirements-dev.txt"]
API_IMAGE_ARGS = ["--kind", "image", "--artifact", "signalnest-api:ci", "--min-lang-packages", "40"]
READER_IMAGE_ARGS = ["--kind", "image", "--artifact", "signalnest-revision-reader:ci", "--min-lang-packages", "1"]
VENDORED = "trivy-vendored-pins-vuln.json"      # each job's filesystem scan of its staged vendored-pin lists
VENDORED_ARTIFACT = ".vendored-pins"          # the staging directory each vendored-pin report must describe
READER_TOOLING_ARGS = ["--kind", "tooling", "--artifact", ".reader-venv", "--env-root", ".reader-venv", "--min-lang-packages", "15",
                       "--vendored-artifact", VENDORED_ARTIFACT, "--vendored-target", "reader-venv/requirements-vendored-pip.txt"]
SECURITY_TOOLING_ARGS = ["--kind", "tooling", "--artifact", ".security-venv", "--env-root", ".security-venv", "--min-lang-packages", "60",
                         "--vendored-artifact", VENDORED_ARTIFACT, "--vendored-target", "security-venv/requirements-vendored-pip.txt"]
PYTHON_LOCATION = "${{ env.pythonLocation }}"   # resolved from the environment at run time, as the workflow resolves it
READER_SYSPY_ARGS = ["--kind", "tooling", "--artifact", PYTHON_LOCATION, "--env-root", PYTHON_LOCATION, "--min-lang-packages", "2",
                     "--vendored-artifact", VENDORED_ARTIFACT, "--vendored-target", "reader-syspy/requirements-vendored-pip.txt"]
API_VENV_ARGS = ["--kind", "tooling", "--artifact", "apps/api/.venv", "--env-root", "apps/api/.venv", "--min-lang-packages", "52",
                 "--locked-by", "apps/api/requirements-bootstrap.txt", "--locked-by", "apps/api/requirements-dev.txt",
                 "--locked-extra", "signalnest-api", "--vendored-artifact", VENDORED_ARTIFACT,
                 "--vendored-target", "api-venv/requirements-vendored-pip.txt", "--require-read-only"]
LOCKTOOLS_ARGS = ["--kind", "tooling", "--artifact", ".locktools", "--env-root", ".locktools", "--min-lang-packages", "7",
                  "--locked-by", "apps/api/requirements-locktools.txt", "--vendored-artifact", VENDORED_ARTIFACT,
                  "--vendored-target", "locktools/requirements-vendored-pip.txt", "--require-read-only"]


def binary_args(job: str) -> list[str]:
    return ["--kind", "binary", "--artifact", ".scanner-binaries", "--binary-job", job, "--binary-pins", "security/scanner-binaries.json"]


BINARY_MANIFEST = "scanner-binaries-manifest.json"
API_JOBS = ("frontend", "backend", "migrations", "integration")
API_JOB_IDS = {"frontend": "frontend-quality", "backend": "backend-quality", "migrations": "migration-and-contract",
               "integration": "integration-smoke"}

# key: (report, gate arguments, inventory, vendored-pin report, binary staging manifest). The driver passes the
# inventory (--installed-inventory), the vendored-pin report (--vendored-report) and the manifest (--binary-manifest)
# as COPIES, so a case can change them; every other argument is exactly the real gate step's.
REPORTS = {
    "locks": ("trivy-locks-vuln.json", LOCKS_ARGS, None, None, None),
    "api-image": ("trivy-image-api-vuln.json", API_IMAGE_ARGS, None, None, None),
    "reader-image": ("trivy-image-reader-vuln.json", READER_IMAGE_ARGS, None, None, None),
    "reader-tooling": ("trivy-reader-tooling-venv-vuln.json", READER_TOOLING_ARGS, "reader-tooling-venv-installed.json", VENDORED, None),
    "security-tooling": ("trivy-security-tooling-venv-vuln.json", SECURITY_TOOLING_ARGS, "security-tooling-venv-installed.json", VENDORED, None),
    "reader-syspy": ("trivy-reader-tooling-syspy-vuln.json", READER_SYSPY_ARGS, "reader-tooling-syspy-installed.json", VENDORED, None),
    "container-binaries": ("trivy-scanner-binaries-vuln.json", binary_args("container-build"), None, None, BINARY_MANIFEST),
    "reader-binaries": ("trivy-scanner-binaries-vuln.json", binary_args("revision-reader"), None, None, BINARY_MANIFEST),
    "backend-locktools": ("trivy-locktools-vuln.json", LOCKTOOLS_ARGS, "locktools-installed.json", VENDORED, None),
}
for _job in API_JOBS:
    REPORTS[f"{_job}-api-venv"] = ("trivy-api-venv-vuln.json", API_VENV_ARGS, "api-venv-installed.json", VENDORED, None)
    REPORTS[f"{_job}-binaries"] = ("trivy-scanner-binaries-vuln.json", binary_args(API_JOB_IDS[_job]), None, None, BINARY_MANIFEST)

# Package selectors: (result predicate, package predicate) over the real report.
OS_PKG = (lambda r: r.get("Class") == "os-pkgs", lambda p: True)
IMAGE_PY = (lambda r: r.get("Class") == "lang-pkgs", lambda p: True)
RUNTIME_LOCK = (lambda r: r.get("Target") == "apps/api/requirements-runtime.txt", lambda p: True)
NPM_RUNTIME = (lambda r: r.get("Target") == "package-lock.json", lambda p: not p.get("Dev"))
NPM_DEV = (lambda r: r.get("Target") == "package-lock.json", lambda p: bool(p.get("Dev")))
TOOL_LOCK = (lambda r: r.get("Target") == "security/requirements-semgrep.txt", lambda p: True)
READER_LOCK = (lambda r: r.get("Target") == "apps/revision-reader/requirements-dev.txt", lambda p: True)
TOOLING_PKG = (lambda r: r.get("Type") == "python-pkg", lambda p: bool(p.get("FilePath")))
TOOLING_VENDORED = (lambda r: r.get("Type") == "python-pkg", lambda p: not p.get("FilePath"))


def GOBINARY(name: str):
    return (lambda r: r.get("Type") == "gobinary" and r.get("Target") == name, lambda p: p.get("Name") != "stdlib")

# name: (group, report key, change, expectation, token)
#   change: ("inject", selector, severity, fixed[, uid]) | ("append-result", type, severity) | ("baseline",) | ("drop-scanned",)
#           | ("inventory", how) | ("target",) | ("no-severity", selector)
#           | ("inject-vendored", severity, fixed) | ("drop-vendored",)       the environment's result in the vendored-pin report
#           | ("truncate-inventory",) | ("truncate-lang",)                   one below the gate's --min-lang-packages floor
#           | ("unlocked", how)   a distribution installed AND scanned outside the locks ("add") or at another version ("version")
#           | ("append-pip",)     an installed pip in a final image's Python population
#           | ("vendored-artifact",)   the vendored-pin report renamed to describe another directory
#           | ("extra-not-editable",)  the --locked-extra project's direct_url removed from the inventory (not from the checkout)
#           | ("extra-url", url)       the --locked-extra project's direct_url pointed at another URL (an index, or outside the workspace)
#           | ("drop-binary", name) | ("drop-stdlib", name) | ("manifest-digest", name)    the binary analysis and its staging record
#   fixed:  True (FixedVersion + Status fixed) | False (Status affected) | "status-only" (Status fixed, no FixedVersion)
#           | "status:<s>" (Status <s>, no FixedVersion);  uid: a PkgIdentifier UID matching no package
#   token:  the injected VulnerabilityID (violation / excluded / reported) or the expected reason text
CASES: dict = {
    # --- container group: the locks scan, the API image and the installed security-tool venv of the
    #     Container build and security job
    "baseline-locks": ("container", "locks", ("baseline",), "pass", None),
    "baseline-api-image": ("container", "api-image", ("baseline",), "pass", None),
    "baseline-security-tooling": ("container", "security-tooling", ("baseline",), "pass", None),
    "critical-fix-api-image-os": ("container", "api-image", ("inject", OS_PKG, "CRITICAL", True), "violation", "CONTROL-critical-fix-api-image-os"),
    "critical-nofix-api-image-os": ("container", "api-image", ("inject", OS_PKG, "CRITICAL", False), "violation", "CONTROL-critical-nofix-api-image-os"),
    "critical-nofix-api-image-python": ("container", "api-image", ("inject", IMAGE_PY, "CRITICAL", False), "violation", "CONTROL-critical-nofix-api-image-python"),
    "critical-fix-runtime-lock": ("container", "locks", ("inject", RUNTIME_LOCK, "CRITICAL", True), "violation", "CONTROL-critical-fix-runtime-lock"),
    "critical-nofix-runtime-lock": ("container", "locks", ("inject", RUNTIME_LOCK, "CRITICAL", False), "violation", "CONTROL-critical-nofix-runtime-lock"),
    "critical-nofix-npm-runtime": ("container", "locks", ("inject", NPM_RUNTIME, "CRITICAL", False), "violation", "CONTROL-critical-nofix-npm-runtime"),
    "critical-fix-npm-dev": ("container", "locks", ("inject", NPM_DEV, "CRITICAL", True), "violation", "CONTROL-critical-fix-npm-dev"),
    "critical-nofix-npm-dev": ("container", "locks", ("inject", NPM_DEV, "CRITICAL", False), "violation", "CONTROL-critical-nofix-npm-dev"),
    "critical-fix-security-tool-lock": ("container", "locks", ("inject", TOOL_LOCK, "CRITICAL", True), "violation", "CONTROL-critical-fix-security-tool-lock"),
    "critical-nofix-security-tool-lock": ("container", "locks", ("inject", TOOL_LOCK, "CRITICAL", False), "violation", "CONTROL-critical-nofix-security-tool-lock"),
    "critical-nofix-reader-tooling-lock": ("container", "locks", ("inject", READER_LOCK, "CRITICAL", False), "violation", "CONTROL-critical-nofix-reader-tooling-lock"),
    "critical-nofix-security-tooling": ("container", "security-tooling", ("inject", TOOLING_PKG, "CRITICAL", False), "violation", "CONTROL-critical-nofix-security-tooling"),
    "security-tooling-package-not-scanned": ("container", "security-tooling", ("drop-scanned",), "incomplete", "absent from the scan"),
    "critical-status-fixed-no-version-runtime-lock": ("container", "locks", ("inject", RUNTIME_LOCK, "CRITICAL", "status-only"), "violation", "CONTROL-critical-status-fixed-no-version-runtime-lock"),
    "critical-wontfix-api-image-os": ("container", "api-image", ("inject", OS_PKG, "CRITICAL", "status:will_not_fix"), "violation", "CONTROL-critical-wontfix-api-image-os"),
    "critical-fixdeferred-api-image-os": ("container", "api-image", ("inject", OS_PKG, "CRITICAL", "status:fix_deferred"), "violation", "CONTROL-critical-fixdeferred-api-image-os"),
    "critical-eol-runtime-lock": ("container", "locks", ("inject", RUNTIME_LOCK, "CRITICAL", "status:end_of_life"), "violation", "CONTROL-critical-eol-runtime-lock"),
    "critical-ghsa-npm-dev": ("container", "locks", ("inject", NPM_DEV, "CRITICAL", False), "violation", "GHSA-c0n7-r0l5-npmd"),
    "critical-unmatched-uid-runtime-lock": ("container", "locks", ("inject", RUNTIME_LOCK, "CRITICAL", False, "control-no-such-package"), "violation", "CONTROL-critical-unmatched-uid-runtime-lock"),
    "critical-nofix-api-image-gobinary": ("container", "api-image", ("append-result", "gobinary", "CRITICAL"), "violation", "CONTROL-critical-nofix-api-image-gobinary"),
    "high-fix-api-image": ("container", "api-image", ("inject", OS_PKG, "HIGH", True), "violation", "CONTROL-high-fix-api-image"),
    "high-nofix-api-image": ("container", "api-image", ("inject", OS_PKG, "HIGH", False), "excluded", "CONTROL-high-nofix-api-image"),
    "high-fix-runtime-lock": ("container", "locks", ("inject", RUNTIME_LOCK, "HIGH", True), "violation", "CONTROL-high-fix-runtime-lock"),
    "high-nofix-runtime-lock": ("container", "locks", ("inject", RUNTIME_LOCK, "HIGH", False), "excluded", "CONTROL-high-nofix-runtime-lock"),
    "high-status-fixed-no-version-runtime-lock": ("container", "locks", ("inject", RUNTIME_LOCK, "HIGH", "status-only"), "excluded", "CONTROL-high-status-fixed-no-version-runtime-lock"),
    "high-fix-npm-dev": ("container", "locks", ("inject", NPM_DEV, "HIGH", True), "reported", "CONTROL-high-fix-npm-dev"),
    "high-fix-security-tool-lock": ("container", "locks", ("inject", TOOL_LOCK, "HIGH", True), "reported", "CONTROL-high-fix-security-tool-lock"),
    "undeclared-lock-target": ("container", "locks", ("target",), "incomplete", "no declared scope"),
    "missing-severity-runtime-lock": ("container", "locks", ("no-severity", RUNTIME_LOCK), "malformed", "has severity None"),
    # --- reader group: the reader image and the reader's installed tooling environment (.reader-venv)
    "baseline-reader-image": ("reader", "reader-image", ("baseline",), "pass", None),
    "baseline-reader-tooling": ("reader", "reader-tooling", ("baseline",), "pass", None),
    "critical-fix-reader-image": ("reader", "reader-image", ("inject", IMAGE_PY, "CRITICAL", True), "violation", "CONTROL-critical-fix-reader-image"),
    "critical-nofix-reader-image": ("reader", "reader-image", ("inject", OS_PKG, "CRITICAL", False), "violation", "CONTROL-critical-nofix-reader-image"),
    "critical-fix-reader-tooling": ("reader", "reader-tooling", ("inject", TOOLING_PKG, "CRITICAL", True), "violation", "CONTROL-critical-fix-reader-tooling"),
    "critical-nofix-reader-tooling": ("reader", "reader-tooling", ("inject", TOOLING_PKG, "CRITICAL", False), "violation", "CONTROL-critical-nofix-reader-tooling"),
    "critical-nofix-reader-tooling-vendored": ("reader", "reader-tooling", ("inject", TOOLING_VENDORED, "CRITICAL", False), "violation", "CONTROL-critical-nofix-reader-tooling-vendored"),
    "critical-nofix-reader-tooling-node-pkg": ("reader", "reader-tooling", ("append-result", "node-pkg", "CRITICAL"), "violation", "CONTROL-critical-nofix-reader-tooling-node-pkg"),
    "critical-wontfix-reader-image-os": ("reader", "reader-image", ("inject", OS_PKG, "CRITICAL", "status:will_not_fix"), "violation", "CONTROL-critical-wontfix-reader-image-os"),
    "high-fix-reader-tooling": ("reader", "reader-tooling", ("inject", TOOLING_PKG, "HIGH", True), "reported", "CONTROL-high-fix-reader-tooling"),
    "high-nofix-reader-image": ("reader", "reader-image", ("inject", OS_PKG, "HIGH", False), "excluded", "CONTROL-high-nofix-reader-image"),
    "reader-tooling-package-not-scanned": ("reader", "reader-tooling", ("drop-scanned",), "incomplete", "absent from the scan"),
    "reader-tooling-installed-after-scan": ("reader", "reader-tooling", ("inventory", "add"), "incomplete", "installed after the scan"),
    "reader-tooling-version-differs": ("reader", "reader-tooling", ("inventory", "version"), "incomplete", "absent from the scan"),
    "reader-tooling-inventory-of-another-environment": ("reader", "reader-tooling", ("inventory", "elsewhere"), "incomplete", "outside the scanned environment"),
    # the reader job's interpreter prefix (its pip and the Gate 4N PyYAML), located through pythonLocation
    "baseline-reader-syspy": ("reader", "reader-syspy", ("baseline",), "pass", None),
    "critical-nofix-reader-syspy": ("reader", "reader-syspy", ("inject", TOOLING_PKG, "CRITICAL", False), "violation", "CONTROL-critical-nofix-reader-syspy"),
    "reader-syspy-package-not-scanned": ("reader", "reader-syspy", ("drop-scanned",), "incomplete", "absent from the scan"),
    # --- coverage gaps (operator order 2026-10-20): vendored pins, floors, installers in images, scanner/tool binaries
    "critical-nofix-security-tooling-vendored-pin": ("container", "security-tooling", ("inject-vendored", "CRITICAL", False), "violation", "CONTROL-critical-nofix-security-tooling-vendored-pin"),
    "security-tooling-vendored-pin-not-scanned": ("container", "security-tooling", ("drop-vendored",), "incomplete", "absent from the vendored-pin scan"),
    "security-tooling-truncated-inventory": ("container", "security-tooling", ("truncate-inventory",), "incomplete", "expected at least"),
    "security-tooling-vendored-report-of-another-directory": ("container", "security-tooling", ("vendored-artifact",), "incomplete", "the report describes something else"),
    "api-image-installer-present": ("container", "api-image", ("append-pip",), "incomplete", "ships an installer"),
    "api-image-truncated-lang-packages": ("container", "api-image", ("truncate-lang",), "incomplete", "expected at least"),
    "baseline-container-binaries": ("container", "container-binaries", ("baseline",), "pass", None),
    "critical-nofix-container-binary-trivy": ("container", "container-binaries", ("inject", GOBINARY("trivy"), "CRITICAL", False), "violation", "CONTROL-critical-nofix-container-binary-trivy"),
    "high-fix-container-binary-trivy": ("container", "container-binaries", ("inject", GOBINARY("trivy"), "HIGH", True), "reported", "CONTROL-high-fix-container-binary-trivy"),
    "container-binary-trivy-analysis-missing": ("container", "container-binaries", ("drop-binary", "trivy"), "incomplete", "unsupported or incomplete"),
    "container-binary-trivy-stdlib-missing": ("container", "container-binaries", ("drop-stdlib", "trivy"), "incomplete", "standard library version was not recovered"),
    "container-binary-trivy-manifest-digest-differs": ("container", "container-binaries", ("manifest-digest", "trivy"), "incomplete", "staging manifest does not record"),
    "critical-nofix-reader-tooling-vendored-pin": ("reader", "reader-tooling", ("inject-vendored", "CRITICAL", False), "violation", "CONTROL-critical-nofix-reader-tooling-vendored-pin"),
    "reader-tooling-vendored-pin-not-scanned": ("reader", "reader-tooling", ("drop-vendored",), "incomplete", "absent from the vendored-pin scan"),
    "reader-tooling-truncated-inventory": ("reader", "reader-tooling", ("truncate-inventory",), "incomplete", "expected at least"),
    "reader-tooling-vendored-report-of-another-directory": ("reader", "reader-tooling", ("vendored-artifact",), "incomplete", "the report describes something else"),
    "critical-nofix-reader-syspy-vendored-pin": ("reader", "reader-syspy", ("inject-vendored", "CRITICAL", False), "violation", "CONTROL-critical-nofix-reader-syspy-vendored-pin"),
    "reader-syspy-vendored-pin-not-scanned": ("reader", "reader-syspy", ("drop-vendored",), "incomplete", "absent from the vendored-pin scan"),
    "reader-syspy-truncated-inventory": ("reader", "reader-syspy", ("truncate-inventory",), "incomplete", "expected at least"),
    "reader-syspy-vendored-report-of-another-directory": ("reader", "reader-syspy", ("vendored-artifact",), "incomplete", "the report describes something else"),
    "reader-image-installer-present": ("reader", "reader-image", ("append-pip",), "incomplete", "ships an installer"),
    "reader-image-truncated-lang-packages": ("reader", "reader-image", ("truncate-lang",), "incomplete", "expected at least"),
    "baseline-reader-binaries": ("reader", "reader-binaries", ("baseline",), "pass", None),
    "critical-nofix-reader-binary-trivy": ("reader", "reader-binaries", ("inject", GOBINARY("trivy"), "CRITICAL", False), "violation", "CONTROL-critical-nofix-reader-binary-trivy"),
    "critical-nofix-reader-binary-tofu": ("reader", "reader-binaries", ("inject", GOBINARY("tofu"), "CRITICAL", False), "violation", "CONTROL-critical-nofix-reader-binary-tofu"),
    "high-fix-reader-binary-tofu": ("reader", "reader-binaries", ("inject", GOBINARY("tofu"), "HIGH", True), "reported", "CONTROL-high-fix-reader-binary-tofu"),
    "reader-binary-tofu-analysis-missing": ("reader", "reader-binaries", ("drop-binary", "tofu"), "incomplete", "unsupported or incomplete"),
    "reader-binary-trivy-stdlib-missing": ("reader", "reader-binaries", ("drop-stdlib", "trivy"), "incomplete", "standard library version was not recovered"),
    "reader-binary-tofu-manifest-digest-differs": ("reader", "reader-binaries", ("manifest-digest", "tofu"), "incomplete", "staging manifest does not record"),
}


def _environment_cases(group: str, key: str) -> dict:
    """The cases of one hash-locked installed environment (an API venv, or .locktools)."""
    return {
        f"baseline-{key}": (group, key, ("baseline",), "pass", None),
        f"critical-nofix-{key}": (group, key, ("inject", TOOLING_PKG, "CRITICAL", False), "violation", f"CONTROL-critical-nofix-{key}"),
        f"critical-nofix-{key}-vendored-pin": (group, key, ("inject-vendored", "CRITICAL", False), "violation", f"CONTROL-critical-nofix-{key}-vendored-pin"),
        f"high-fix-{key}": (group, key, ("inject", TOOLING_PKG, "HIGH", True), "reported", f"CONTROL-high-fix-{key}"),
        f"{key}-package-not-scanned": (group, key, ("drop-scanned",), "incomplete", "absent from the scan"),
        f"{key}-installed-after-scan": (group, key, ("inventory", "add"), "incomplete", "installed after the scan"),
        f"{key}-installed-outside-the-locks": (group, key, ("unlocked", "add"), "incomplete", "not in the lock(s)"),
        f"{key}-version-differs-from-the-lock": (group, key, ("unlocked", "version"), "incomplete", "differ from the locked version"),
        f"{key}-vendored-pin-not-scanned": (group, key, ("drop-vendored",), "incomplete", "absent from the vendored-pin scan"),
        f"{key}-vendored-report-of-another-directory": (group, key, ("vendored-artifact",), "incomplete", "the report describes something else"),
        f"{key}-truncated-inventory": (group, key, ("truncate-inventory",), "incomplete", "expected at least"),
        **({f"{key}-project-not-from-the-checkout": (group, key, ("extra-not-editable",), "incomplete", "not installed from the checkout"),
            f"{key}-project-from-an-index-url": (group, key, ("extra-url", "https://files.example.invalid/signalnest_api.whl"), "incomplete", "not a local directory"),
            f"{key}-project-from-outside-the-workspace": (group, key, ("extra-url", "file:///control/outside/apps/api"), "incomplete", "outside the working directory")}
           if "--locked-extra" in REPORTS[key][1] else {}),
    }


def _binary_cases(group: str) -> dict:
    """The Trivy binary cases of one API job (it runs Trivy, so it analyses the Trivy it executes)."""
    key = f"{group}-binaries"
    return {
        f"baseline-{key}": (group, key, ("baseline",), "pass", None),
        f"critical-nofix-{group}-binary-trivy": (group, key, ("inject", GOBINARY("trivy"), "CRITICAL", False), "violation", f"CONTROL-critical-nofix-{group}-binary-trivy"),
        f"high-fix-{group}-binary-trivy": (group, key, ("inject", GOBINARY("trivy"), "HIGH", True), "reported", f"CONTROL-high-fix-{group}-binary-trivy"),
        f"{group}-binary-trivy-analysis-missing": (group, key, ("drop-binary", "trivy"), "incomplete", "unsupported or incomplete"),
        f"{group}-binary-trivy-stdlib-missing": (group, key, ("drop-stdlib", "trivy"), "incomplete", "standard library version was not recovered"),
        f"{group}-binary-trivy-manifest-digest-differs": (group, key, ("manifest-digest", "trivy"), "incomplete", "staging manifest does not record"),
    }


for _job in API_JOBS:
    CASES.update(_environment_cases(_job, f"{_job}-api-venv"))
    CASES.update(_binary_cases(_job))
CASES.update(_environment_cases("backend", "backend-locktools"))
GROUPS = ("container", "reader") + API_JOBS


def _digest(doc: object) -> str:
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


def _pick(report: dict, selector) -> tuple[dict, dict]:
    rpred, ppred = selector
    for res in report.get("Results") or []:
        if rpred(res):
            for pkg in res.get("Packages") or []:
                if isinstance(pkg, dict) and ppred(pkg) and (pkg.get("Identifier") or {}).get("UID"):
                    return res, pkg
    raise ControlError("no package matches the case's selector in the real report")


def _inject(report: dict, selector, severity: str, fixed, vid: str, uid: str | None = None) -> None:
    res, pkg = _pick(report, selector)
    v = {"VulnerabilityID": vid, "PkgID": pkg.get("ID"), "PkgName": pkg.get("Name"), "PkgIdentifier": copy.deepcopy(pkg["Identifier"]),
         "InstalledVersion": pkg.get("Version"), "Severity": severity, "Title": "synthetic negative control (security/vuln_policy_controls.py)"}
    if uid is not None:
        if any((p.get("Identifier") or {}).get("UID") == uid for p in res.get("Packages") or []):
            raise ControlError(f"the unmatched-identifier case's UID {uid!r} matches a real package")
        v["PkgIdentifier"] = {"UID": uid}          # an identifier that matches no package of the result
    if fixed == "status-only":
        v["Status"] = "fixed"                     # a fix is recorded, but the FixedVersion field is absent
    elif isinstance(fixed, str) and fixed.startswith("status:"):
        v["Status"] = fixed[len("status:"):]      # a no-fix status Trivy emits (will_not_fix, fix_deferred, end_of_life, ...)
    elif fixed:
        v["Status"], v["FixedVersion"] = "fixed", "999.0.0"
    else:
        v["Status"] = "affected"                  # no fix published
    res.setdefault("Vulnerabilities", []).append(v)


def _arg(args: list[str], flag: str) -> str:
    if flag not in args:
        raise ControlError(f"the case's gate arguments carry no {flag}")
    return args[args.index(flag) + 1]


def _result(doc: dict, target: str) -> dict:
    hits = [r for r in doc.get("Results") or [] if r.get("Target") == target]
    if len(hits) != 1:
        raise ControlError(f"the real report carries {len(hits)} results for {target!r}, expected one")
    return hits[0]


def build_case(name: str, reports_dir: pathlib.Path, out_dir: pathlib.Path) -> tuple[list[str], pathlib.Path, dict]:
    group, key, change, _, token = CASES[name]
    rep_name, args, inv_name, vend_name, man_name = REPORTS[key]
    if PYTHON_LOCATION in args:
        loc = os.environ.get("pythonLocation")
        if not loc:
            raise ControlError(f"{name}: the pythonLocation environment variable (exported by actions/setup-python) is not set")
        args = [loc if a == PYTHON_LOCATION else a for a in args]
    src = reports_dir / rep_name
    if not src.is_file():
        raise ControlError(f"{name}: the real report {src} is missing")
    report = json.loads(src.read_text(encoding="utf-8"))
    inventory = json.loads((reports_dir / inv_name).read_text(encoding="utf-8")) if inv_name else None
    vendored = json.loads((reports_dir / vend_name).read_text(encoding="utf-8")) if vend_name else None
    manifest = json.loads((reports_dir / man_name).read_text(encoding="utf-8")) if man_name else None
    before = (_digest(report), _digest(inventory), _digest(vendored), _digest(manifest))
    kind = change[0]
    if kind == "inject":
        _inject(report, change[1], change[2], change[3], token, *change[4:])
    elif kind == "append-result":
        rtype, severity = change[1], change[2]
        pkg = {"Name": f"control-{rtype}", "Version": "1.0.0", "Identifier": {"UID": f"control-{rtype}-uid"}}
        if key != "api-image" and key != "reader-image":
            pkg["FilePath"] = f"control/{rtype}/package.json"
        report["Results"].append({"Target": f"control ({rtype})", "Class": "lang-pkgs", "Type": rtype, "Packages": [pkg],
                                  "Vulnerabilities": [{"VulnerabilityID": token, "PkgName": pkg["Name"], "PkgIdentifier": {"UID": pkg["Identifier"]["UID"]},
                                                       "InstalledVersion": "1.0.0", "Severity": severity, "Status": "affected",
                                                       "Title": "synthetic negative control (security/vuln_policy_controls.py)"}]})
    elif kind == "no-severity":
        _inject(report, change[1], "CRITICAL", True, f"CONTROL-{name}")
        del next(v for r in report["Results"] for v in r.get("Vulnerabilities") or [] if v.get("VulnerabilityID") == f"CONTROL-{name}")["Severity"]
    elif kind == "target":
        report["Results"].append({"Target": "tools/requirements-control.txt", "Class": "lang-pkgs", "Type": "pip",
                                  "Packages": [{"Name": "control", "Version": "1.0", "Identifier": {"UID": "control-uid"}}]})
    elif kind == "drop-scanned":
        # Remove the scanned entry of an INSTALLED distribution (matched by name, version and its own
        # dist-info path) — removing a vendored copy pip never inventoried would test nothing.
        norm = lambda s: re.sub(r"[-_.]+", "-", str(s)).lower()  # the gate's own normalisation (gate._norm_dist)
        wanted = {(norm(it["metadata"]["name"]), it["metadata"]["version"], pathlib.Path(it["metadata_location"]).name) for it in inventory["installed"]}
        dropped = False
        for res in report.get("Results") or []:
            if res.get("Type") != "python-pkg" or dropped:
                continue
            for pkg in list(res.get("Packages") or []):
                fp = str(pkg.get("FilePath") or "")
                if any((norm(pkg.get("Name")), pkg.get("Version")) == (n, v) and fp.endswith(f"/{d}/METADATA") for n, v, d in wanted):
                    res["Packages"].remove(pkg); dropped = True
                    break
        if not dropped:
            raise ControlError(f"{name}: no scanned package corresponds to an installed distribution")
    elif kind == "inventory":
        items = inventory["installed"]
        if change[1] == "add":
            loc = pathlib.Path(items[0]["metadata_location"]).parent / "control_late_install-1.0.dist-info"
            items.append({"metadata": {"name": "control-late-install", "version": "1.0"}, "metadata_location": str(loc)})
        elif change[1] == "version":
            items[0]["metadata"]["version"] = items[0]["metadata"]["version"] + ".post999"
        elif change[1] == "elsewhere":
            items[0]["metadata_location"] = "/control/elsewhere/" + pathlib.Path(items[0]["metadata_location"]).name
    elif kind in ("inject-vendored", "drop-vendored"):
        target = _arg(args, "--vendored-target")
        if vendored is None:
            raise ControlError(f"{name}: the case has no vendored-pin report")
        if kind == "inject-vendored":
            _inject(vendored, (lambda r: r.get("Target") == target, lambda p: True), change[1], change[2], token)
        else:
            res = _result(vendored, target)
            if not res.get("Packages"):
                raise ControlError(f"{name}: the vendored-pin result {target!r} lists no package to drop")
            res["Packages"].pop(0)
    elif kind == "vendored-artifact":
        if vendored is None:
            raise ControlError(f"{name}: the case has no vendored-pin report")
        vendored["ArtifactName"] = "control/another-directory"
    elif kind == "extra-not-editable":
        norm = lambda s: re.sub(r"[-_.]+", "-", str(s)).lower()
        extras = {norm(args[i + 1]) for i, a in enumerate(args) if a == "--locked-extra"}
        hit = [it for it in inventory["installed"] if norm(it["metadata"]["name"]) in extras and "direct_url" in it]
        if not hit:
            raise ControlError(f"{name}: no --locked-extra entry with a direct_url in the real inventory")
        for it in hit:
            del it["direct_url"]
    elif kind == "extra-url":
        norm = lambda s: re.sub(r"[-_.]+", "-", str(s)).lower()
        extras = {norm(args[i + 1]) for i, a in enumerate(args) if a == "--locked-extra"}
        hit = [it for it in inventory["installed"] if norm(it["metadata"]["name"]) in extras and "direct_url" in it]
        if not hit:
            raise ControlError(f"{name}: no --locked-extra entry with a direct_url in the real inventory")
        for it in hit:
            it["direct_url"]["url"] = change[1]
    elif kind == "truncate-inventory":
        floor = int(_arg(args, "--min-lang-packages"))
        if floor < 1 or len(inventory["installed"]) < floor:
            raise ControlError(f"{name}: the real inventory is already below the floor {floor}")
        inventory["installed"] = inventory["installed"][:floor - 1]
    elif kind == "truncate-lang":
        floor = int(_arg(args, "--min-lang-packages"))
        keep = floor - 1
        for res in report.get("Results") or []:
            if res.get("Class") == "lang-pkgs":
                pkgs = res.get("Packages") or []
                res["Packages"], keep = pkgs[:keep], max(0, keep - len(pkgs))
    elif kind == "append-pip":
        res = next((r for r in report.get("Results") or [] if r.get("Class") == "lang-pkgs" and r.get("Type") == "python-pkg"), None)
        if res is None:
            raise ControlError(f"{name}: the real image report has no Python result to place pip in")
        res.setdefault("Packages", []).append({"Name": "pip", "Version": "25.0.1", "Identifier": {"UID": "control-pip-uid"},
                                               "FilePath": "usr/local/lib/python3.12/site-packages/pip-25.0.1.dist-info/METADATA"})
    elif kind == "unlocked":
        norm = lambda s: re.sub(r"[-_.]+", "-", str(s)).lower()  # the gate's own normalisation (gate._norm_dist)
        root = os.path.realpath(_arg(args, "--env-root"))
        extras = {norm(args[i + 1]) for i, a in enumerate(args) if a == "--locked-extra"}
        res = next((r for r in report.get("Results") or [] if r.get("Type") == "python-pkg"), None)
        if res is None:
            raise ControlError(f"{name}: the real report has no Python result")
        items = inventory["installed"]
        if change[1] == "add":
            loc = pathlib.Path(items[0]["metadata_location"]).parent / "control_unlocked-1.0.dist-info"
            items.append({"metadata": {"name": "control-unlocked", "version": "1.0"}, "metadata_location": str(loc)})
            rel = os.path.relpath(os.path.realpath(loc), root).replace(os.sep, "/")
            res.setdefault("Packages", []).append({"Name": "control-unlocked", "Version": "1.0", "FilePath": f"{rel}/METADATA",
                                                   "Identifier": {"UID": "control-unlocked-uid"}})
        else:
            it = next((x for x in items if norm(x["metadata"]["name"]) not in extras | {"pip"}), None)
            if it is None:
                raise ControlError(f"{name}: no locked distribution to change")
            old = it["metadata"]["version"]
            it["metadata"]["version"] = old + ".post999"
            dist = pathlib.Path(it["metadata_location"]).name
            hit = [p for p in res.get("Packages") or [] if norm(p.get("Name")) == norm(it["metadata"]["name"]) and p.get("Version") == old
                   and str(p.get("FilePath") or "").endswith(f"/{dist}/METADATA")]
            if len(hit) != 1:
                raise ControlError(f"{name}: the scanned entry of {it['metadata']['name']} {old} was not found exactly once")
            hit[0]["Version"] = old + ".post999"
    elif kind in ("drop-binary", "drop-stdlib"):
        res = _result(report, change[1])
        if kind == "drop-binary":
            report["Results"].remove(res)
        else:
            res["Packages"] = [p for p in res.get("Packages") or [] if p.get("Name") != "stdlib"]
    elif kind == "manifest-digest":
        if manifest is None or change[1] not in (manifest.get("binaries") or {}):
            raise ControlError(f"{name}: the staging manifest does not record {change[1]}")
        manifest["binaries"][change[1]]["sha256"] = "0" * 64
    elif kind != "baseline":
        raise ControlError(f"{name}: unknown change {kind!r}")
    if kind != "baseline" and (_digest(report), _digest(inventory), _digest(vendored), _digest(manifest)) == before:
        raise ControlError(f"{name}: the mutation changed nothing — the control would test nothing")
    out_dir.mkdir(parents=True, exist_ok=True)
    rep_out = out_dir / f"{name}.report.json"
    rep_out.write_text(json.dumps(report), encoding="utf-8")
    copies = {}
    for flag, doc, suffix in (("--installed-inventory", inventory, "installed"), ("--vendored-report", vendored, "vendored"),
                              ("--binary-manifest", manifest, "manifest")):
        if doc is not None:
            path = out_dir / f"{name}.{suffix}.json"
            path.write_text(json.dumps(doc), encoding="utf-8")
            copies[flag] = path
    return args, rep_out, copies


def _verdict(expect: str, token, code: int, text: str) -> bool:
    lines = text.splitlines()
    if expect == "violation":
        return code == 3 and any(l.startswith(f"  VIOLATION {token} ") for l in lines)
    if expect == "excluded":
        return code == 0 and any(l.startswith(f"  EXCLUDED  {token} ") for l in lines)
    if expect == "reported":
        return code == 0 and any(l.startswith(f"  REPORTED  {token} ") for l in lines)
    if expect == "incomplete":
        return code == 4 and any(l.startswith("incomplete:") and token in l for l in lines)
    if expect == "malformed":
        return code == 2 and any(l.startswith("report:") and token in l for l in lines)
    if expect == "pass":
        return code == 0
    raise ControlError(f"unknown expectation {expect!r}")


def check(group: str, reports_dir: pathlib.Path) -> list[tuple[str, bool]]:
    sys.path.insert(0, str(HERE))
    import gate  # noqa: E402 — the same module and CLI the gate steps run
    out_dir = reports_dir / WORK_DIR
    results = []
    for name, (g, key, change, expect, token) in CASES.items():
        if g != group:
            continue
        args, rep, copies = build_case(name, reports_dir, out_dir)
        argv = ["--policy", str(HERE / "policy.json"), "--reports-dir", str(out_dir), "vuln", str(rep), "--name", f"probe-{name}"] + args
        for flag, path in copies.items():
            argv += [flag, str(path)]
        code = gate.main(argv)
        text = (out_dir / f"probe-{name}.summary.txt").read_text(encoding="utf-8")
        ok = _verdict(expect, token, code, text)
        summary = [f"SECURITY GATE CONTROL {SUMMARY_PREFIX}{name} (vulnerability policy, group {group})",
                   f"base report: {REPORTS[key][0]}; change: {change[0]}{' ' + '/'.join(str(x) for x in change[2:]) if change[0] == 'inject' else ''}"
                   + (f" {'/'.join(str(x) for x in change[1:])}" if change[0] != 'inject' and len(change) > 1 else ""),
                   f"case report: {rep} sha256 {hashlib.sha256(rep.read_bytes()).hexdigest()}",
                   f"expected: {expect}" + (f" carrying {token!r}" if token else "") + f"; gate exit {code}; gate summary probe-{name}.summary.txt",
                   gate.RESULT_CONTROL_OK if ok else gate.RESULT_CONTROL_BAD]
        (reports_dir / f"{SUMMARY_PREFIX}{name}.summary.txt").write_text("\n".join(summary) + "\n", encoding="utf-8")
        results.append((name, ok))
    return results


def main(argv: list[str]) -> int:
    try:
        if len(argv) == 3 and argv[0] == "check" and argv[1] in GROUPS:
            results = check(argv[1], pathlib.Path(argv[2]))
            bad = [n for n, ok in results if not ok]
            for n, ok in results:
                print(f"{'ok ' if ok else 'BAD'} {SUMMARY_PREFIX}{n}: expected {CASES[n][3]}")
            print(f"RESULT: {'every vulnerability-policy control behaved as intended' if not bad else f'{len(bad)} control(s) did NOT behave as intended'} ({len(results)} cases, group {argv[1]})")
            return 0 if not bad and results else 6
    except (ControlError, OSError, json.JSONDecodeError, KeyError, IndexError) as exc:
        sys.stderr.write(f"control could not be built: {exc}\n")
        return 2
    sys.stderr.write(__doc__ + "\n")
    return 5


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
