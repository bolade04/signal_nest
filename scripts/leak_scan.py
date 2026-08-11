#!/usr/bin/env python3
"""Live-identifier leak scanner (Gate 4N-I18, SEC-1 / Phase F).

WHY THIS EXISTS. Gate 4N-I17's security lane blocked the commit: the working tree carried the
real AWS account id, bucket names with AWS-assigned suffixes, CloudTrail and RDS ARNs, KMS key
ids and the state lock-table name, and `git log --all -S` proved none of them appears anywhere
in history. Committing the gate package would have been FIRST DISCLOSURE into permanent git
history — and deleting the file afterwards would not undo it.

Containment alone is not a control. A control is something that NOTICES when the containment
is undone. This module is that check, and it is deliberately written to satisfy two opposing
requirements at once:

  IT MUST CATCH a real account id, a real ARN or an AWS-assigned identifier reintroduced
  anywhere in the committable tree.

  IT MUST NOT FLAG ITSELF. A scanner whose own rule definitions and explanatory prose trip it
  is a scanner that gets weakened or deleted the first time it fires. So the rules are
  expressed as PATTERNS plus an allowlist of documentation placeholders — this file contains
  no live identifier to detect, and the pattern text is not itself a match.

WHY PATTERN-PLUS-ALLOWLIST RATHER THAN A DENYLIST OF KNOWN VALUES. A denylist would have to
contain the very identifiers it is protecting, which is the disclosure it is meant to prevent.
The allowlist holds only AWS documentation placeholders, which are public and non-sensitive.
Anything else that LOOKS like an account id is a finding — fail-closed by construction.

FALSE-POSITIVE DISCIPLINE. A 64-character hex digest can contain twelve consecutive digits by
chance, and a digest is exactly what the containment stores INSTEAD of a value. Digest-shaped
runs are therefore excluded before the account rule is applied; the exclusion is narrow (a
long hex run) and is itself tested.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# AWS documentation placeholder accounts, plus the all-zero form used for "not applicable".
# These are public, non-sensitive, and are what the synthetic fixtures legitimately contain.
ALLOWED_ACCOUNTS = frozenset({
    "111122223333", "000000000000", "444444444444", "555555555555",
    "123456789012", "999988887777", "111199998888",
})

# GATE 4N-I27O. The frozenset above is NO LONGER SELF-AUTHORISING.
#
# THE DEFECT THIS CLOSES. It was the SOLE authority for which 12-digit identifiers may appear
# in tracked content, so it could widen itself: Gate 4N-I27M planted an unreviewed account id
# in a tracked file, watched the scan catch it, added that id to this literal, and the finding
# disappeared while none of the other thirty-six graded controls objected. A scope list owned
# by the control it scopes cannot see its own broadening.
#
# The registry is a SEPARATE reviewed artifact in which every permitted account carries a
# classification and a stated provenance. The two are compared in BOTH directions before any
# scan runs, so adding an id here alone is refused, and removing one there alone is refused
# too. Adding an account now means authoring a reviewed justification for it, which is a
# visible act rather than a silent literal edit.
APPROVED_ACCOUNT_REGISTRY = REPO_ROOT / "tests" / "fixtures" / "approved-account-registry.json"

# Classifications a permitted account may carry. A class outside this set is refused, so a new
# category cannot be introduced in the fixture alone.
NON_LIVE_CLASSIFICATIONS = frozenset({
    "REPOSITORY_SYNTHETIC_ANCHOR", "AWS_DOCUMENTATION_PLACEHOLDER",
    "NOT_APPLICABLE_SENTINEL", "SYNTHETIC_FOREIGN_ACCOUNT",
})


class AccountRegistryError(RuntimeError):
    """Fail-closed. An unregistered permitted account is never scanned past."""


def approved_accounts() -> dict[str, dict]:
    """The independently authored permitted-account registry, keyed by account id.

    Deliberately does NOT read ALLOWED_ACCOUNTS: a list compared against a copy of itself
    agrees with anything, which is the defect this replaces.
    """
    if not APPROVED_ACCOUNT_REGISTRY.exists():
        raise AccountRegistryError(
            f"the approved-account registry is ABSENT: {APPROVED_ACCOUNT_REGISTRY}. Absence "
            "must never be read as 'every account is approved'.")
    doc = json.loads(APPROVED_ACCOUNT_REGISTRY.read_text(encoding="utf-8"))
    entries = doc.get("approved_accounts")
    if not isinstance(entries, list) or not entries:
        raise AccountRegistryError("the registry declares no approved accounts")
    out: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise AccountRegistryError(f"malformed registry entry: {entry!r}")
        account = entry.get("account_id")
        classification = entry.get("classification")
        provenance = entry.get("provenance")
        if not isinstance(account, str) or not re.fullmatch(r"\d{12}", account):
            raise AccountRegistryError(f"registry entry has no valid account_id: {entry!r}")
        if classification not in NON_LIVE_CLASSIFICATIONS:
            raise AccountRegistryError(
                f"account ...{account[-4:]} carries classification {classification!r}, which is "
                f"not one of the non-live classes {sorted(NON_LIVE_CLASSIFICATIONS)}")
        if not isinstance(provenance, str) or len(provenance.strip()) < 20:
            raise AccountRegistryError(
                f"account ...{account[-4:]} has no stated provenance; an account nobody can "
                "justify is not approved")
        if account in out:
            raise AccountRegistryError(f"account ...{account[-4:]} is registered twice")
        out[account] = entry
    return out


# Repository-relative paths a fixture may name. Anything matching must exist in the tree.
_REPO_PATH_REFERENCE = re.compile(r"\b(?:tests|scripts|infra|apps)/[A-Za-z0-9_./-]+"
                                  r"\.(?:py|json|sh|tf|tfvars|ya?ml|md)\b")


def require_registry_references_resolve() -> None:
    """GATE 4N-I27S. Every repository path the registry NAMES must exist.

    THE DEFECT THIS CLOSES. The registry's `_authority` field cited
    `tests/test_approved_account_registry.py` as the guard proving its independence. That file
    has never existed. Gate 4N-I27Q's architect and scope lanes both found it, and NO control
    refused it — leak_scan, package_requirements and commit_package_coherence all exited 0 —
    because nothing checked that a path named inside a fixture resolves. The substantive guard
    did exist under a different name, so no security property was missing; what was missing was
    any reason to believe the citation. A claim of provenance that points at nothing is
    indistinguishable from one that points at something, which is exactly what makes it
    dangerous in an evidence package.
    """
    text = APPROVED_ACCOUNT_REGISTRY.read_text(encoding="utf-8")
    missing = sorted({ref for ref in _REPO_PATH_REFERENCE.findall(text)
                      if not (REPO_ROOT / ref).is_file()})
    if missing:
        raise AccountRegistryError(
            "the approved-account registry names repository path(s) that do not exist: "
            + ", ".join(missing)
            + ". A cited guard that is not in the tree cannot be evidence of anything.")


def require_registered_allowed_accounts() -> None:
    """Both directions, before any scanning happens."""
    require_registry_references_resolve()
    registry = approved_accounts()
    unregistered = sorted(ALLOWED_ACCOUNTS - set(registry))
    unused = sorted(set(registry) - ALLOWED_ACCOUNTS)
    problems = []
    if unregistered:
        problems.append(
            "permitted account(s) with no registry entry: "
            + ", ".join(f"...{a[-4:]}" for a in unregistered)
            + ". Broadening the scanner's own literal does not approve an identifier.")
    if unused:
        problems.append(
            "registered account(s) the scanner does not permit: "
            + ", ".join(f"...{a[-4:]}" for a in unused)
            + ". The registry and the scanner must agree in both directions.")
    if problems:
        raise AccountRegistryError("approved-account registry mismatch:\n  "
                                   + "\n  ".join(problems))

# A long hex run — a sha256 digest or an image digest. Removed before the account scan so a
# digest that happens to contain twelve consecutive digits is not reported as an account.
_HEX_RUN = re.compile(r"\b[0-9a-fA-F]{32,}\b")

# A UUID. Its final group is exactly twelve hex characters, so an all-digit tail (a request id
# like 11111111-1111-1111-1111-111111111111) is indistinguishable from an account id once the
# surrounding context is discarded. UUIDs are removed before the account scan for the same
# reason as digests: the containment STORES these shapes, so mistaking one for a leak would
# make the scanner unusable.
_UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")

# A bare 12-digit run, the shape of an AWS account id.
_ACCOUNT = re.compile(r"(?<!\d)\d{12}(?!\d)")

# Widths of the digests this repository actually stores: md5, sha1/git, sha256, sha512.
_DIGEST_WIDTHS = frozenset({32, 40, 64, 128})

# ARNs that carry an account in the 5th field. A placeholder account is permitted; anything
# else is a live ARN.
_ARN_WITH_ACCOUNT = re.compile(r"arn:aws[a-z-]*:[a-z0-9-]*:[a-z0-9-]*:(\d{12}):")

# Credential material. Never permitted, in any file, under any classification.
# A credential is a VALUE, not a variable name and not a rule definition. Matching the bare
# identifier `aws_secret_access_key` flagged legitimate boto keyword arguments, Terraform
# variable declarations, prose in the operations runbook and this scanner's own siblings —
# and a scanner that cries wolf on its own vocabulary is a scanner that gets deleted. A key
# NAME is only a finding when it is assigned a non-empty literal.
# GATE 4N-I27R. An AWS access-key id is UPPERCASE by specification, so this half is matched
# CASE-SENSITIVELY. Under the previous single `re.I` pattern, `AKIA[0-9A-Z]{16}` matched any
# base64 run containing "akia"-in-any-case followed by sixteen alphanumerics — which is how
# widening the scan to every tracked text file immediately produced a false "credential
# material" finding on the PUBLIC RDS CA bundle (`AKiaRZatN8eiz9p0s0lu`, random certificate
# base64). A scanner that cries wolf on a public certificate is a scanner that gets switched
# off, so the fix is to match what AWS actually issues rather than to exempt the file.
# GATE 4N-I28B, FINDING I28A-02. Route53 hosted-zone identifiers.
#
# THE DEFECT THIS CLOSES. Gate 4N-I27Y's adversarial lane found a hosted-zone id hardcoded in
# three files and reported that `leak_scan` exited 0 — structurally blind to the class, because a
# hosted-zone ARN carries no account segment and the id is not credential-shaped. Gate 4N-I27Z
# removed the literal and added a pattern to the TEST-side critical-ARN audit, but that audit is
# parametrised over a five-entry `POLICY_GENERATORS` list. Gate 4N-I28A proved the consequence:
# the same literal placed in `scripts/gen_readonly_verifier_policy.py` — a policy generator in
# neither that list nor its EXEMPT set — was caught by nothing at all.
#
# A NAMED-FILE LIST CANNOT BE THE COVERAGE MECHANISM. Adding one filename to it would leave the
# next generator, script, fixture or document exactly as blind. The rule belongs where inclusion
# is already universal: this scan already visits EVERY tracked text file, with binaries, vendored
# and generated paths excluded structurally, so an unfamiliar suffix, a compound suffix or an
# extensionless file is covered the day it is added and no allow-list has to be maintained.
#
# CASE MATTERS. `Z[A-Za-z0-9]{20}` matches 98 base64 fragments in this repository — the public
# RDS CA bundle among them. The uppercase-only form matches exactly one thing: the marked
# synthetic fixture value. This is the same lesson Gate 4N-I27R learned when an `re.I` key-id
# rule read certificate base64 as credential material.
# GATE 4N-I28I, ROOT CAUSE RC-4. A conservative CANDIDATE range, not a fitted length.
#
# THE DEFECT THIS CLOSES — Gate 4N-I28G finding ADV-04, raised independently as non-blocking
# AWS-I28G-01. The bare rule was `Z[A-Z0-9]{20}` — exactly 21 characters, the length of the single
# literal Gate 4N-I28B happened to observe. A length sweep showed detection at 21 and NOWHERE else,
# so a genuine 14-character hosted-zone id written bare scanned clean. Fitting a detector to one
# observed sample is the same habit as fitting a corpus to one implementation.
#
# The exact length of the real identifier is NOT available without protected Tier-2 evidence or an
# AWS call, and this gate is authorised for neither. So the range below is justified by the PUBLIC
# shape of the identifier class — an uppercase `Z` followed by uppercase alphanumerics, in the
# 13-to-32 band AWS hosted-zone ids are publicly documented to occupy — and NOT by the protected
# value. Nothing here claims to know how long the real one is.
#
# The rule deliberately OVERMATCHES within that band: a candidate is something to explain, not
# proof of a leak. A false positive is classified for review; a false negative inside the supported
# band blocks release. Case sensitivity is retained — Gate 4N-I28C measured that a case-insensitive
# variant matches base64 fragments, and Gate 4N-I27R learned the same lesson with an `re.I` key rule.
_HOSTED_ZONE_MIN_SUFFIX = 12
_HOSTED_ZONE_MAX_SUFFIX = 31
_HOSTED_ZONE = re.compile(
    r"(?<![A-Za-z0-9])Z[A-Z0-9]{%d,%d}(?![A-Za-z0-9])"
    % (_HOSTED_ZONE_MIN_SUFFIX, _HOSTED_ZONE_MAX_SUFFIX))
_HOSTED_ZONE_IN_ARN = re.compile(r"hostedzone/([A-Za-z0-9]+)")

# A value is allowed only if it ANNOUNCES that it is invented. A protected-input placeholder
# (`${var}`, `<hosted_zone_id>`, null) is not identifier-shaped and never reaches these rules.
_SYNTHETIC_ZONE_PREFIX = "ZSYNTH"


def _zone_is_declared_synthetic(zone: str) -> bool:
    return zone.startswith(_SYNTHETIC_ZONE_PREFIX)


_CREDENTIAL_EXACT_CASE = re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")

# The rest genuinely varies in case: a PEM header and a named secret assigned a literal.
_CREDENTIAL_ANY_CASE = re.compile(
    r"BEGIN [A-Z ]*PRIVATE KEY"
    r"|aws_secret_access_key\s*[=:]\s*[\"\'][^\"\'\s{}$][^\"\']*[\"\']",
    re.I)


class _CredentialRule:
    """Both halves behind one `.search()`, so callers are unchanged."""

    @staticmethod
    def search(text: str):
        return _CREDENTIAL_EXACT_CASE.search(text) or _CREDENTIAL_ANY_CASE.search(text)


_CREDENTIAL = _CredentialRule

# Paths whose live identifiers were contained by this gate and which must never come back.
PROHIBITED_PATHS = (
    Path("infra/aws/live-resource-inventory.json"),
    Path("infra/aws/cloudfront-expected.json"),
)

# This module states the RULES. Excluding it is not a loophole: it is the "do not flag your own
# rule definitions" requirement, and it holds no identifier to find — a claim the test suite
# checks rather than takes on trust.
SELF = Path("scripts/leak_scan.py")

SCAN_SUFFIXES = {".py", ".json", ".tf", ".tfvars", ".hcl", ".yml", ".yaml", ".md", ".txt", ".sh"}

# GATE 4N-I27R. SCAN_SUFFIXES IS NO LONGER THE INCLUSION RULE.
#
# THE DEFECT THIS CLOSES. Inclusion was a ten-entry suffix allow-list, so a tracked text file
# whose suffix nobody listed was never scanned at all. Gate 4N-I27Q's adversarial lane put an
# unapproved account id and a production-shaped role ARN into
# `infra/aws/terraform.tfvars.example` — a MODIFIED path in the rejected candidate, suffix
# `.example` — and the scan reported clean over an identical 552-file count, because the file
# was never in scope. Sixteen tracked classes were invisible the same way.
#
# THE INVERSION. Every tracked file is scanned unless it is EXCLUDED for a stated reason:
# a known binary/vendor/generated form, or bytes that are not decodable text. Recognising the
# good suffixes meant the unrecognised suffix escaped; recognising the excluded forms means an
# unfamiliar or compound suffix (`.tfvars.example`, `.env.sample`, extensionless `Dockerfile`)
# is scanned by default. An undecodable file is skipped as binary and REPORTED, never silently.
BINARY_SUFFIXES = {
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".webp", ".pdf", ".zip", ".gz", ".tgz", ".bz2",
    ".xz", ".7z", ".jar", ".class", ".pyc", ".pyo", ".so", ".dylib", ".dll", ".exe", ".bin",
    ".woff", ".woff2", ".ttf", ".otf", ".eot", ".mp4", ".mov", ".mp3", ".wav", ".db",
    ".sqlite", ".sqlite3",
}

# Paths whose CONTENT is generated or vendored, where a match would be noise rather than a
# disclosure this repository controls. Each entry is a stated exclusion, not a silent gap.
EXCLUDED_PATH_PARTS = {
    "node_modules", "vendor", ".terraform", "__pycache__", ".pytest_cache", ".mypy_cache",
}

_MAX_SCAN_BYTES = 4 * 1024 * 1024

# Every file the scan declined, with the reason. Reported, so an exclusion is visible.
SKIPPED_WITH_REASON: dict[str, str] = {}


def is_scannable(path: Path) -> tuple[bool, str]:
    """Should this tracked file be scanned? Returns (decision, reason).

    Default is YES. A file is skipped only for a reason this function can name.
    """
    if any(part in EXCLUDED_PATH_PARTS for part in path.parts):
        return False, "generated or vendored path"
    if path.suffix.lower() in BINARY_SUFFIXES:
        return False, f"known binary suffix {path.suffix.lower()}"
    try:
        if path.stat().st_size > _MAX_SCAN_BYTES:
            return False, "larger than the scan ceiling"
        chunk = path.read_bytes()[:8192]
    except OSError as exc:
        return False, f"unreadable: {exc}"
    if b"\x00" in chunk:
        return False, "binary content (NUL byte)"
    try:
        chunk.decode("utf-8")
    except UnicodeDecodeError:
        return False, "not decodable as UTF-8 text"
    return True, "tracked text file"

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".terraform", ".venv", "venv",
             ".reader-venv", "dist", "build", ".pytest_cache", ".mypy_cache"}


def scan_text(text: str) -> list[str]:
    """Findings in one document. Empty means clean."""
    findings = []
    # GATE 4N-I27R. A hex run is no longer DELETED before the account scan.
    #
    # THE DEFECT THIS CLOSES. `_HEX_RUN.sub("", text)` removed any run of 32+ hex characters
    # so that a digest containing twelve consecutive digits would not be misread as an account.
    # But an account id is itself hex-shaped, so an unapproved id with ~20 adjacent hex
    # characters formed a single long run and was erased WHOLE — verified at Gate 4N-I27Q:
    # `scan_text("trace_id: <id>aaaaaaaaaaaaaaaaaaaa")` returned nothing while the bare id was
    # caught. The exclusion meant to prevent a false negative on digests was creating a true
    # negative on identifiers.
    #
    # The replacement keeps the digest protection and removes the hiding place: a hex run is
    # still excluded from the BARE-RUN rule, but every 12-digit sequence inside it is recovered
    # and checked. A genuine sha256 rarely contains an unapproved 12-digit run at a digit
    # boundary; when it does, the honest answer is a finding a human resolves, not silence.
    scrubbed = _UUID.sub("", _HEX_RUN.sub("", text))
    embedded = set()
    for run in _HEX_RUN.findall(_UUID.sub("", text)):
        # A run of EXACTLY a known digest width is a digest, and the twelve digits inside it
        # are incidental — `f71d58ce895329d848631650004ebfe1f6227b9b`, a real git sha in the
        # deployment runbook, contains `848631650004` by chance. Any OTHER length is not a
        # digest shape, so an identifier hiding there is recovered and checked. This is the
        # narrow form of the exclusion: bounded by what digests actually look like, rather
        # than by "long enough to look hexadecimal".
        if len(run) in _DIGEST_WIDTHS:
            continue
        embedded.update(_ACCOUNT.findall(run))

    for account in set(_ACCOUNT.findall(scrubbed)):
        if account not in ALLOWED_ACCOUNTS:
            findings.append(f"non-placeholder 12-digit account id ...{account[-4:]}")
    for account in sorted(embedded - ALLOWED_ACCOUNTS):
        findings.append(
            f"non-placeholder 12-digit account id ...{account[-4:]} embedded in a hex-shaped "
            "run; a long hexadecimal neighbour does not make an identifier safe")

    for account in set(_ARN_WITH_ACCOUNT.findall(text)):
        if account not in ALLOWED_ACCOUNTS:
            findings.append(f"ARN carrying a non-placeholder account ...{account[-4:]}")

    if _CREDENTIAL.search(text):
        findings.append("credential material")

    # GATE 4N-I28B. Hosted-zone identifiers, in both the bare and the ARN-qualified form. The
    # bare rule catches `ZONE = "Z..."` in any tracked text file; the ARN rule catches an id of
    # any length written into `hostedzone/...`, which is where a shorter real one would appear.
    for zone in sorted(set(_HOSTED_ZONE.findall(text))):
        if not _zone_is_declared_synthetic(zone):
            findings.append(
                f"Route53 hosted-zone identifier ...{zone[-4:]} with no declared provenance; "
                f"an environment-specific zone id must be resolved through the protected "
                f"inventory, or be explicitly synthetic ({_SYNTHETIC_ZONE_PREFIX}...)")
    for zone in sorted(set(_HOSTED_ZONE_IN_ARN.findall(text))):
        if _zone_is_declared_synthetic(zone) or not zone.startswith("Z") or len(zone) < 8:
            continue
        if _HOSTED_ZONE.search(zone):
            continue                     # already reported by the bare rule
        findings.append(
            f"Route53 hosted-zone ARN carrying identifier ...{zone[-4:]} with no declared "
            "provenance")

    return findings


def _ignored(paths: list[Path], root: Path) -> set[str]:
    """Ask git which paths THE REPOSITORY would refuse to commit. One process, not one per file.

    GATE 4N-I26B. `git check-ignore` normally also consults `core.excludesFile` — the developer's
    personal global ignore list, which lives outside the repository and differs on every machine.
    That made this scanner's SCOPE depend on whose laptop it ran on: a file excluded by one
    developer's global rules was silently never scanned, while on a CI runner with no such file
    the same path would be scanned. The I26B CI harness caught it by running with an empty HOME,
    where a local settings file carrying a real 12-digit account id became visible for the first
    time; every previous "LEAK SCAN: clean" was partly a statement about a personal config file.

    Pinning `core.excludesFile` to /dev/null makes the ignore decision a property of the
    REPOSITORY — its .gitignore files and .git/info/exclude — and therefore identical here and on
    the runner. Containment a repository does not declare for itself is not containment.
    """
    import subprocess
    if not paths:
        return set()
    proc = subprocess.run(
        ["git", "-c", "core.excludesFile=/dev/null", "check-ignore", "--stdin"],
        cwd=root, text=True, input="\n".join(str(p) for p in paths), capture_output=True)
    return {line.strip() for line in proc.stdout.splitlines() if line.strip()}


# GATE 4N-I28I, ROOT CAUSE RC-2. Every discovered path gets a DECISION, and every decision is
# recorded.
#
# THE DEFECT THIS CLOSES — Gate 4N-I28G finding ADV-01. `candidate_files()` used to `continue` on
# SKIP_DIRS *before* is_scannable() ran, so an outer-filtered file left no trace at all. Adding one
# entry to SKIP_DIRS removed 80 files from the scan with a planted identifier inside them:
# `clean=True`, 601 files instead of 681, and **zero** skip-report entries. The same narrowing
# applied to EXCLUDED_PATH_PARTS produced 80 visible entries — so the two filters differed in
# VISIBILITY, not only in coverage, and the invisible one was the unpinned one.
#
# This defeated the property Gate 4N-I27R introduced in this very module: "An undecodable file is
# skipped as binary and REPORTED, never silently." That promise only ever covered the inner filter.
#
# The decision set is closed. An unrecognised outcome is ERROR_OR_UNKNOWN, which fails closed
# rather than quietly dropping the path.
SCANNED = "SCANNED"
SKIPPED_BINARY = "SKIPPED_BINARY"
SKIPPED_VENDOR = "SKIPPED_VENDOR"
SKIPPED_GENERATED = "SKIPPED_GENERATED"
SKIPPED_CACHE = "SKIPPED_CACHE"
SKIPPED_EXPLICIT_POLICY = "SKIPPED_EXPLICIT_POLICY"
ERROR_OR_UNKNOWN = "ERROR_OR_UNKNOWN"

DECISIONS = (SCANNED, SKIPPED_BINARY, SKIPPED_VENDOR, SKIPPED_GENERATED, SKIPPED_CACHE,
             SKIPPED_EXPLICIT_POLICY, ERROR_OR_UNKNOWN)

#: Which category each excluded directory name belongs to. A name with no category is an
#: unclassified exclusion and resolves to ERROR_OR_UNKNOWN rather than a silent skip.
_DIRECTORY_CATEGORY = {
    "node_modules": SKIPPED_VENDOR, "vendor": SKIPPED_VENDOR,
    ".venv": SKIPPED_VENDOR, "venv": SKIPPED_VENDOR, ".reader-venv": SKIPPED_VENDOR,
    ".terraform": SKIPPED_GENERATED, "dist": SKIPPED_GENERATED, "build": SKIPPED_GENERATED,
    "__pycache__": SKIPPED_CACHE, ".pytest_cache": SKIPPED_CACHE, ".mypy_cache": SKIPPED_CACHE,
    ".git": SKIPPED_EXPLICIT_POLICY,
}

#: Every decision made during the last scan: {relative path: (decision, reason)}. Unlike the old
#: SKIPPED_WITH_REASON this records SCANNED paths too, so the totals can be reconciled.
SCAN_DECISIONS: dict[str, tuple[str, str]] = {}


def scan_decision(path: Path, root: Path) -> tuple[str, str]:
    """The single decision point for one discovered file. Returns (decision, reason).

    Both the outer directory filter and the inner file filter resolve here, so neither can
    remove a path from the scan without leaving a categorized record.
    """
    parts = set(path.parts)
    for name in sorted(parts & (set(SKIP_DIRS) | set(EXCLUDED_PATH_PARTS))):
        category = _DIRECTORY_CATEGORY.get(name)
        if category is None:
            return ERROR_OR_UNKNOWN, (
                f"directory component {name!r} is excluded but has no category; an exclusion "
                "this module cannot explain must not be treated as a safe skip")
        return category, f"excluded directory component {name!r}"
    scannable, reason = is_scannable(path)
    if not scannable:
        if "binary" in reason or "decodable" in reason:
            return SKIPPED_BINARY, reason
        if "generated or vendored" in reason:
            return SKIPPED_VENDOR, reason
        return SKIPPED_EXPLICIT_POLICY, reason
    return SCANNED, "tracked text file"


def candidate_files(root: Path | None = None):
    """Every file git would actually commit, with every rejection accounted for.

    SCOPE IS THE POINT. A gitignored file cannot enter history, so a finding in one is noise
    that trains reviewers to ignore the scanner. The live inventory itself is now gitignored,
    which is exactly why its PATH is checked separately in scan_repository() rather than by
    reading its contents.
    """
    root = root or REPO_ROOT
    SCAN_DECISIONS.clear()
    considered = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = str(path.relative_to(root))
        decision, reason = scan_decision(path, root)
        SCAN_DECISIONS[rel] = (decision, reason)
        if decision != SCANNED:
            SKIPPED_WITH_REASON[rel] = reason        # kept: existing consumers read this
            continue
        considered.append(path.relative_to(root))
    ignored = _ignored(considered, root)
    for rel in considered:
        if str(rel) in ignored:
            SCAN_DECISIONS[str(rel)] = (SKIPPED_EXPLICIT_POLICY, "gitignored: cannot enter history")
            SKIPPED_WITH_REASON[str(rel)] = "gitignored: cannot enter history"
            continue
        yield root / rel


def scan_accounting(root: Path | None = None) -> dict:
    """discovered == scanned + categorized skips + errors, with nothing unexplained.

    GATE 4N-I28I RC-2. The invariant is the point: a file can leave the scan only by being
    counted somewhere else. If these totals stop reconciling, a path is disappearing.
    """
    root = root or REPO_ROOT
    scanned = list(candidate_files(root))           # populates SCAN_DECISIONS
    discovered = sum(1 for p in sorted(root.rglob("*")) if p.is_file())
    counts = {d: 0 for d in DECISIONS}
    for decision, _reason in SCAN_DECISIONS.values():
        counts[decision] = counts.get(decision, 0) + 1
    categorized_skips = sum(v for k, v in counts.items()
                            if k not in (SCANNED, ERROR_OR_UNKNOWN))
    accounted = counts[SCANNED] + categorized_skips + counts[ERROR_OR_UNKNOWN]
    unexplained = [p for p, (d, _r) in SCAN_DECISIONS.items() if d not in DECISIONS]
    return {"discovered_candidate_paths": discovered,
            "recorded_decisions": len(SCAN_DECISIONS),
            "scanned_paths": counts[SCANNED],
            "yielded_for_scanning": len(scanned),
            "categorized_skipped_paths": categorized_skips,
            "explicit_error_paths": counts[ERROR_OR_UNKNOWN],
            "accounted": accounted,
            "counts": counts,
            "reconciles": accounted == discovered == len(SCAN_DECISIONS),
            "duplicates": len(SCAN_DECISIONS) - len(set(SCAN_DECISIONS)),
            "omissions": discovered - len(SCAN_DECISIONS),
            "unexplained": unexplained,
            "every_skip_has_a_reason": all(r for _d, r in SCAN_DECISIONS.values())}


def scan_repository(root: Path | None = None) -> dict:
    root = root or REPO_ROOT
    # GATE 4N-I27O. Establish that every permitted account is independently registered BEFORE
    # scanning. If the permitted set has been widened without a reviewed justification, the
    # correct answer is refusal, not a clean scan performed under a scope nobody approved.
    require_registered_allowed_accounts()
    findings: dict[str, list[str]] = {}

    for prohibited in PROHIBITED_PATHS:
        if (root / prohibited).exists():
            findings.setdefault(str(prohibited), []).append(
                "a path contained by Gate 4N-I18 is present again in the tree")

    for path in candidate_files(root):
        rel = path.relative_to(root)
        if rel == SELF:
            continue
        hits = scan_text(path.read_text(encoding="utf-8", errors="ignore"))
        if hits:
            findings.setdefault(str(rel), []).extend(hits)

    return {"findings": findings, "clean": not findings,
            "files_scanned": sum(1 for _ in candidate_files(root))}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = scan_repository()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        for path, hits in sorted(result["findings"].items()):
            for hit in hits:
                print(f"  {path}: {hit}", file=sys.stderr)
        print(f"  {result['files_scanned']} files scanned")
        print("LEAK SCAN: clean" if result["clean"] else "LEAK SCAN: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
