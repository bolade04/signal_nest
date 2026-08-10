#!/usr/bin/env python3
"""Production-certification state machine, eligibility, generator and gate (Gate 4N-I20B).

WHAT GATE 4N-I20A FOUND IN THE I20 VERSION OF THIS FILE. `production_gate()` compared the
candidate, manifest, HEAD, index-tree, commit-tree and repository-diff bindings ONLY when the
caller passed `expected_*` arguments. The shipping CLI passed none. So an artifact naming a
candidate that does not exist, a HEAD that does not exist, a commit tree that does not exist
and an EMPTY production-check list was PERMITTED, exit code 0, printing
"PRODUCTION GATE: permitted". The bindings existed as parameters, not as controls.

That is the same defect this chain found in `terraform_role_inventory.reconcile()` one gate
earlier — a control that works on the path the tests exercise and not on the path that ships —
reproduced here while writing the fix for it.

THE MODEL NOW (Phase C, MODEL B). Expected values are resolved by the VERIFIER, never read from
the artifact under validation:

  repository bindings  resolved LIVE from scripts/tracked_state.py at verification time, so
                       drift after certification is detected automatically rather than needing a
                       caller to remember to ask for it
  candidate identity   from an explicitly supplied candidate manifest
  external evidence    from an explicitly supplied protected external-binding manifest
  required checks      from tests/fixtures/required-production-checks.json, an independently
                       authored set the artifact cannot name for itself

Every one is MANDATORY. There is no artifact-only verification mode that can return production
permission: `verify` requires all three inputs, and the informational `state` command is
structurally incapable of permitting anything.

THE PIPELINE, three separate executed commands:

  state        derive the current state from evidence (never hardcoded)
  eligibility  Tier-2 evidence in -> PRODUCTION_CERTIFICATION_ELIGIBLE, flag still FALSE
  certify      a valid eligibility result in -> the separate immutable certification artifact
  verify       artifact + bindings -> permit, or exit non-zero

`certify` refuses anything that is not a valid eligibility result, so eligibility cannot become
certification by editing a state string, and `verify` re-derives everything anyway.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

UTC = datetime.timezone.utc

MECHANISM_ONLY = "MECHANISM_ONLY"
CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED = "CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED"
PRODUCTION_CERTIFICATION_ELIGIBLE = "PRODUCTION_CERTIFICATION_ELIGIBLE"
PRODUCTION_CERTIFIED = "PRODUCTION_CERTIFIED"
INVALID_CERTIFICATION = "INVALID_CERTIFICATION"

STATES = (MECHANISM_ONLY, CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED,
          PRODUCTION_CERTIFICATION_ELIGIBLE, PRODUCTION_CERTIFIED, INVALID_CERTIFICATION)

ALLOWED_TRANSITIONS = {
    (MECHANISM_ONLY, CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED),
    (CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED, PRODUCTION_CERTIFICATION_ELIGIBLE),
    (PRODUCTION_CERTIFICATION_ELIGIBLE, PRODUCTION_CERTIFIED),
}

REQUIRED_FLAG = {
    MECHANISM_ONLY: False,
    CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED: False,
    PRODUCTION_CERTIFICATION_ELIGIBLE: False,
    PRODUCTION_CERTIFIED: True,
    INVALID_CERTIFICATION: False,
}

SCHEMA_VERSION = "4n-i20b.1"
SYNTHETIC_MARKER = "NON_PRODUCTION_TEST_FIXTURE"
# GATE 4N-I23: a candidate id carrying this prefix can NEVER certify production. The
# disqualification is structural, so unlike the opt-in SYNTHETIC_MARKER it cannot be left
# out of a fixture to make a pipeline succeed (I22 architect finding H3).
NON_CERTIFYING_ID_PREFIX = "SYNTHETIC-"
TIER_PROTECTED = "TIER_2_PROTECTED"
TIER_SYNTHETIC = "TIER_1_SYNTHETIC"
DEFAULT_VALIDITY = datetime.timedelta(hours=24)

REQUIRED_CHECKS_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "required-production-checks.json"

REQUIRED_ARTIFACT_FIELDS = (
    "schema_version", "certification_state", "certifies_production", "candidate_id",
    "candidate_manifest_sha256", "head", "index_tree_hash", "commit_tree_hash",
    "repository_diff_sha256", "external_anchor_sha256", "external_inventory_sha256",
    "protected_tier", "certified_at_utc", "valid_until_utc", "checks", "certifier_provenance",
    "eligibility_sha256",
)

# Fields an artifact may never carry: they would let the document supply the value it is
# verified against. `required_checks` is included because an artifact naming its own required
# set is the Gate 4N-I20A empty-checks defect with extra steps.
SELF_ATTESTING_FIELDS = ("canonical_sha256", "expected_sha256", "artifact_sha256", "digest",
                         "required_checks", "expected_bindings")

PASSED = "PASSED"


class CertificationError(ValueError):
    """Fail-closed. Never downgraded, never satisfied by a default."""


def canonical_sha256(document: dict) -> str:
    return hashlib.sha256(
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode()
    ).hexdigest()


# --- the independently authored required-check set ------------------------------------------


def required_checks() -> list[dict]:
    """The required set. Read from the tracked fixture — never from any artifact."""
    if not REQUIRED_CHECKS_FIXTURE.exists():
        raise CertificationError(
            f"the required production-check contract is missing: {REQUIRED_CHECKS_FIXTURE}. "
            "Absence is never success.")
    doc = json.loads(REQUIRED_CHECKS_FIXTURE.read_text(encoding="utf-8"))
    checks = doc.get("required_checks") or []
    if not checks:
        raise CertificationError("the required production-check set is empty; refusing to certify")
    return checks


def validate_checks(reported) -> list[str]:
    """Every required check must be present, unique and PASSED. Absence never implies success."""
    problems: list[str] = []
    if reported is None:
        return ["no `checks` field: absence never implies success"]
    if not isinstance(reported, list):
        return [f"`checks` must be a list, got {type(reported).__name__}"]
    if not reported:
        return ["`checks` is EMPTY: a certification must prove the required checks ran and passed"]

    seen: dict[str, str] = {}
    for entry in reported:
        if not isinstance(entry, dict) or "check_id" not in entry or "status" not in entry:
            problems.append(f"malformed check entry: {entry!r}")
            continue
        cid, status = entry["check_id"], entry["status"]
        if cid in seen:
            problems.append(f"duplicate check_id {cid!r}")
        seen[cid] = status

    for required in required_checks():
        cid = required["check_id"]
        want = required["required_status"]
        if cid not in seen:
            problems.append(f"required check {cid!r} is MISSING")
        elif seen[cid] != want:
            problems.append(f"required check {cid!r} is {seen[cid]!r}, required {want!r}")
    return problems


# --- state machine, called by production code -----------------------------------------------


def transition(current: str, target: str) -> str:
    if current not in STATES or target not in STATES:
        raise CertificationError(f"unknown state in transition {current!r} -> {target!r}")
    if (current, target) not in ALLOWED_TRANSITIONS:
        raise CertificationError(
            f"prohibited transition {current} -> {target}. There is deliberately no direct path "
            "to PRODUCTION_CERTIFIED.")
    return target


def classify(*, tier: str, has_protected_anchor: bool, anchor_hash_verified: bool,
             synthetic_source: bool, candidate_bound: bool, repository_bound: bool,
             production_checks_passed: bool, artifact_present: bool) -> str:
    """Derive the state from EVIDENCE. Never from a declared label."""
    if tier != TIER_PROTECTED:
        return MECHANISM_ONLY if synthetic_source else CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED
    if synthetic_source:
        return INVALID_CERTIFICATION
    if not (has_protected_anchor and anchor_hash_verified and candidate_bound
            and repository_bound and production_checks_passed):
        return INVALID_CERTIFICATION
    return PRODUCTION_CERTIFIED if artifact_present else PRODUCTION_CERTIFICATION_ELIGIBLE


# --- expected bindings, resolved by the VERIFIER --------------------------------------------


def resolve_repository_binding() -> dict:
    """Expected repository bindings, resolved LIVE. Drift is therefore detected automatically."""
    import tracked_state

    record = tracked_state.repository_state_record()
    return {"head": record["head"],
            # GATE 4N-I23: was record["head_tree_hash"] — a field named for the index
            # carrying HEAD's tree. Three I22 lanes found it independently; the
            # adversarial lane showed staging a file left it unchanged.
            "index_tree_hash": record["index_tree_hash"],
            "commit_tree_hash": record["predicted_commit_tree_hash"],
            "repository_diff_sha256": record["full_tracked_diff_sha256"]}


def load_json(path: str | Path, *, what: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise CertificationError(f"{what} is REQUIRED and missing: {p}")
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise CertificationError(f"{what} is not valid JSON: {exc}") from exc


AUTHORIZATION_CONTRACT = REPO_ROOT / "tests" / "fixtures" / "external-authorization-contract.json"

# What the verifier CHECKS. Held beside the contract's required_fields so the two can be
# differenced in BOTH directions — a required field nobody validates and a validated field
# nobody requires are each a defect, and only checking one direction hides half of them.
VALIDATED_AUTHORIZATION_FIELDS = (
    "authorization_version", "protected_tier", "approved_anchor_sha256",
    "approved_inventory_sha256", "approved_evidence_classification", "non_synthetic_required",
    "eligibility_lineage_sha256", "required_production_checks", "issued_utc",
    "valid_until_utc", "authorized_candidate_scope")


def authorization_contract() -> dict:
    """The repository's independent authority over external expectations. Never caller-supplied."""
    if not AUTHORIZATION_CONTRACT.exists():
        raise CertificationError(
            f"the external-authorization contract is ABSENT: {AUTHORIZATION_CONTRACT}. Without "
            "it there is no independent source for external expectations and nothing may be "
            "certified.")
    return json.loads(AUTHORIZATION_CONTRACT.read_text(encoding="utf-8"))


def authorization_field_coverage() -> dict:
    """Both differences between REQUIRED and VALIDATED authorization fields."""
    required = set(authorization_contract().get("required_fields") or ())
    validated = set(VALIDATED_AUTHORIZATION_FIELDS)
    return {"required": sorted(required), "validated": sorted(validated),
            "required_not_validated": sorted(required - validated),
            "validated_not_required": sorted(validated - required),
            "complete": not (required - validated) and not (validated - required)}


def resolve_external_authorization(path: Path, *, tier: str) -> dict:
    """Load the caller's authorization document and AUTHENTICATE it against the repository pin.

    GATE 4N-I26B, closing I26B-02. The previous contract compared the artifact's external
    digests against a manifest the CALLER passed on the command line. Both sides were
    caller-supplied, so Gate 4N-I25's security lane invented a digest, echoed the same value
    into `--external-binding`, and the comparison agreed with itself: exit 0, "PRODUCTION GATE:
    permitted", on a wholly fabricated artifact.

    The repository cannot re-derive Tier-2 evidence. It can, and now does, decide WHICH
    DOCUMENT COUNTS: the authorized document's digest is pinned in a tracked fixture, and a
    document whose digest is not the pinned one is refused before any field is read. Forging
    one now means editing tracked repository content, which moves the index tree — already
    bound and already verified.
    """
    contract = authorization_contract()
    coverage = authorization_field_coverage()
    if not coverage["complete"]:
        raise CertificationError(
            "the authorization contract and the verifier disagree about which fields exist: "
            f"required-but-unvalidated {coverage['required_not_validated']}, "
            f"validated-but-unrequired {coverage['validated_not_required']}. A field nobody "
            "checks is a field an attacker may set freely.")

    pins = contract.get("pinned_authorization_digests") or {}
    pinned = pins.get(tier)
    if not pinned:
        raise CertificationError(
            f"no authorization digest is pinned for tier {tier!r}. An unpinned tier has no "
            "independent authority behind it, and the correct answer is refusal, not trust.")

    if not path.exists():
        raise CertificationError(f"the external authorization document is missing: {path}")
    actual = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != pinned:
        raise CertificationError(
            f"the external authorization document does not match the digest this repository "
            f"pins for {tier}: supplied {actual[:16]}… != pinned {pinned[:16]}…. A "
            "caller-supplied expectation is not an expectation.")

    document = json.loads(path.read_text(encoding="utf-8"))
    missing = [f for f in contract.get("required_fields", []) if f not in document]
    if missing:
        raise CertificationError(
            f"the authorization document omits required field(s) {missing}")
    return document


def _external_problems(artifact: dict, external: dict) -> list[str]:
    """Compare the artifact against an AUTHENTICATED authorization document.

    `external` must come from resolve_external_authorization(), never straight from a caller
    path — that is the whole of I26B-02.
    """
    problems = []
    if external.get("protected_tier") != TIER_PROTECTED:
        problems.append(
            f"the external authorization speaks for {external.get('protected_tier')!r}, not "
            f"{TIER_PROTECTED}; only a protected-tier authorization may certify production")
    if SYNTHETIC_MARKER in json.dumps(external) or external.get("non_synthetic_required") is False:
        problems.append("the external authorization admits synthetic evidence, which cannot "
                        "certify production")
    for authorized, claimed in (("approved_anchor_sha256", "external_anchor_sha256"),
                                ("approved_inventory_sha256", "external_inventory_sha256")):
        expected, actual = external.get(authorized), artifact.get(claimed)
        if not expected:
            problems.append(f"the authorization supplies no {authorized}")
        elif expected != actual:
            problems.append(f"{claimed} MISMATCH: artifact {str(actual)[:12]}… != authorized "
                            f"{str(expected)[:12]}…")
    lineage = external.get("eligibility_lineage_sha256")
    if lineage and artifact.get("eligibility_sha256") != lineage:
        problems.append(
            f"eligibility lineage MISMATCH: the artifact descends from "
            f"{str(artifact.get('eligibility_sha256'))[:12]}… but the authorization names "
            f"{str(lineage)[:12]}…")
    scope = external.get("authorized_candidate_scope")
    if scope and artifact.get("candidate_id") != scope:
        problems.append(f"the authorization covers candidate {scope!r}, not "
                        f"{artifact.get('candidate_id')!r}")
    required_checks = set(external.get("required_production_checks") or ())
    satisfied = {c.get("id") for c in (artifact.get("checks") or []) if isinstance(c, dict)
                 and c.get("passed") is True}
    for missing in sorted(required_checks - satisfied):
        problems.append(f"required production check {missing!r} is not present AND passed")
    return problems


# --- validation ------------------------------------------------------------------------------


def validate_artifact(artifact: dict, *, candidate_manifest: dict, external_binding: dict,
                      repository_binding: dict | None = None,
                      now: datetime.datetime | None = None) -> dict:
    """Validate against expectations the CALLER RESOLVED. All three inputs are mandatory."""
    problems: list[str] = []
    now = now or datetime.datetime.now(UTC)
    if repository_binding is None:
        repository_binding = resolve_repository_binding()

    for field in REQUIRED_ARTIFACT_FIELDS:
        if field not in artifact:
            problems.append(f"missing required field {field!r}")
    for field in SELF_ATTESTING_FIELDS:
        if field in artifact:
            problems.append(
                f"artifact carries its own {field!r}: a document may not supply the value it is "
                "verified against")
    if problems:
        return {"state": INVALID_CERTIFICATION, "certifies_production": False,
                "problems": problems, "clean": False}

    state, flag = artifact["certification_state"], artifact["certifies_production"]
    if state not in STATES:
        problems.append(f"unknown certification_state {state!r}")
    if not isinstance(flag, bool):
        problems.append(f"certifies_production must be a boolean, got {type(flag).__name__}")
    elif state in REQUIRED_FLAG and flag != REQUIRED_FLAG[state]:
        problems.append(f"state {state} requires certifies_production={REQUIRED_FLAG[state]}")

    if artifact.get("protected_tier") != TIER_PROTECTED:
        problems.append("certification without TIER_2_PROTECTED evidence")
    if SYNTHETIC_MARKER in json.dumps(artifact):
        problems.append("synthetic evidence cannot support production certification")

    # MANDATORY bindings. Every expectation comes from a source the artifact does not control.
    expected_candidate = candidate_manifest.get("candidate_id")
    expected_manifest_hash = candidate_manifest.get("candidate_manifest_sha256")
    if not expected_candidate or not expected_manifest_hash:
        problems.append("the candidate manifest supplies no candidate_id / manifest digest")
    if expected_candidate and expected_candidate != artifact.get("candidate_id"):
        problems.append(f"candidate_id MISMATCH: artifact {artifact.get('candidate_id')!r} != "
                        f"expected {expected_candidate!r}")
    if expected_manifest_hash and expected_manifest_hash != artifact.get("candidate_manifest_sha256"):
        problems.append("candidate_manifest_sha256 MISMATCH")

    for field, expected in repository_binding.items():
        actual = artifact.get(field)
        if expected != actual:
            problems.append(f"{field} MISMATCH: artifact {str(actual)[:12]}… != repository "
                            f"{str(expected)[:12]}… (repository drift, or a certification "
                            "issued for a different tree)")

    problems.extend(_external_problems(artifact, external_binding))
    problems.extend(validate_checks(artifact.get("checks")))

    if not artifact.get("eligibility_sha256"):
        problems.append("no eligibility_sha256: certification must descend from an eligibility result")

    try:
        valid_until = datetime.datetime.fromisoformat(
            str(artifact["valid_until_utc"]).replace("Z", "+00:00"))
        if valid_until <= now:
            problems.append(f"certification expired at {artifact['valid_until_utc']}")
    except Exception as exc:  # noqa: BLE001
        problems.append(f"unreadable valid_until_utc: {exc}")

    resolved = state if not problems else INVALID_CERTIFICATION
    return {"state": resolved, "certifies_production": bool(flag) and not problems,
            "problems": problems, "clean": not problems}


def production_gate(artifact: dict | None, *, candidate_manifest: dict | None = None,
                    external_binding: dict | None = None,
                    repository_binding: dict | None = None, **kw) -> dict:
    """THE decisive consumer. Every binding input is MANDATORY; absence is refusal."""
    if artifact is None:
        return {"permitted": False, "exit_code": 2, "state": INVALID_CERTIFICATION,
                "reason": "no certification artifact was supplied"}
    if candidate_manifest is None or external_binding is None:
        return {"permitted": False, "exit_code": 2, "state": INVALID_CERTIFICATION,
                "reason": ("a candidate manifest AND an external binding manifest are REQUIRED. "
                           "Gate 4N-I20A: making these optional is what let a fabricated "
                           "artifact through.")}
    result = validate_artifact(artifact, candidate_manifest=candidate_manifest,
                               external_binding=external_binding,
                               repository_binding=repository_binding, **kw)
    permitted = result["state"] == PRODUCTION_CERTIFIED and result["certifies_production"]
    return {"permitted": permitted, "exit_code": 0 if permitted else 2,
            "state": result["state"], "certifies_production": result["certifies_production"],
            "reason": "certified" if permitted else "; ".join(result["problems"])}


# --- eligibility ------------------------------------------------------------------------------


def establish_eligibility(*, tier: str, anchor_path: str, anchor_sha256: str,
                          inventory_sha256: str, candidate_manifest: dict,
                          checks: list, repository_binding: dict | None = None,
                          now: datetime.datetime | None = None) -> dict:
    """Tier-2 evidence in -> an eligibility result. NEVER produces PRODUCTION_CERTIFIED."""
    now = now or datetime.datetime.now(UTC)
    if repository_binding is None:
        repository_binding = resolve_repository_binding()

    if tier != TIER_PROTECTED:
        raise CertificationError(f"eligibility requires {TIER_PROTECTED}, got {tier!r}")

    evidence = load_json(anchor_path, what="the protected Tier-2 evidence")
    raw = Path(anchor_path).read_bytes()
    actual = hashlib.sha256(raw).hexdigest()
    if actual != anchor_sha256:
        raise CertificationError(
            "protected evidence hash MISMATCH: the expected digest must be supplied separately "
            "and must match")
    synthetic = evidence.get("_classification") == SYNTHETIC_MARKER
    if synthetic:
        raise CertificationError("synthetic evidence cannot establish production eligibility")
    if not inventory_sha256:
        raise CertificationError("an external inventory digest is REQUIRED")

    check_problems = validate_checks(checks)
    if check_problems:
        raise CertificationError("production checks incomplete: " + "; ".join(check_problems))
    if not candidate_manifest.get("candidate_id"):
        raise CertificationError("the candidate manifest supplies no candidate_id")

    state = classify(tier=tier, has_protected_anchor=True, anchor_hash_verified=True,
                     synthetic_source=synthetic, candidate_bound=True, repository_bound=True,
                     production_checks_passed=True, artifact_present=False)
    if state != PRODUCTION_CERTIFICATION_ELIGIBLE:
        raise CertificationError(f"evidence did not establish eligibility: derived {state}")
    transition(CANDIDATE_VALIDATED_NOT_PRODUCTION_CERTIFIED, state)

    result = {
        "schema_version": SCHEMA_VERSION, "certification_state": state,
        "certifies_production": False,          # eligibility is NOT certification
        "candidate_id": candidate_manifest["candidate_id"],
        "candidate_manifest_sha256": candidate_manifest.get("candidate_manifest_sha256"),
        **repository_binding,
        "external_anchor_sha256": anchor_sha256,
        "external_inventory_sha256": inventory_sha256,
        "protected_tier": tier, "checks": checks,
        "established_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    return result


def generate_certification(eligibility: dict, *, certifier_provenance: str,
                           validity: datetime.timedelta | None = None,
                           now: datetime.datetime | None = None) -> dict:
    """A valid eligibility result in -> the separate immutable certification artifact."""
    now = now or datetime.datetime.now(UTC)
    validity = validity or DEFAULT_VALIDITY

    # GATE 4N-I23, closing the I22 OPERATOR-RULING qualification (architect H3). Every CI run
    # executed eligibility -> certify -> verify and produced an artifact declaring
    # PRODUCTION_CERTIFIED / certifies_production: true, which the gate then permitted. The
    # only thing between that and a real certification was a fixture that OMITTED the opt-in
    # synthetic marker, so the mechanism could not reach the conclusion the workflow comment
    # asserted ("it certifies nothing"). candidate_manifest.py already had the right shape: a
    # SYNTHETIC- id CANNOT claim production certification because the disqualification is
    # derived from the identifier itself and therefore cannot be left out.
    #
    # It runs FIRST, deliberately. A structural disqualification must not depend on the input
    # first passing every other validation — otherwise its guarantee is really a claim about
    # statement ordering.
    candidate_id = eligibility.get("candidate_id", "")
    if NON_CERTIFYING_ID_PREFIX and str(candidate_id).startswith(NON_CERTIFYING_ID_PREFIX):
        raise CertificationError(
            f"candidate {candidate_id!r} carries the {NON_CERTIFYING_ID_PREFIX!r} prefix and "
            "can never certify production. Non-certification is derived from the identifier "
            "so it cannot be omitted from a fixture.")

    state = eligibility.get("certification_state")
    if state != PRODUCTION_CERTIFICATION_ELIGIBLE:
        raise CertificationError(
            f"the generator accepts ONLY a {PRODUCTION_CERTIFICATION_ELIGIBLE} result, got "
            f"{state!r}. Certification cannot be manufactured from a candidate-only, "
            "mechanism-only, invalid or already-certified input.")
    if eligibility.get("certifies_production") is not False:
        raise CertificationError("an eligibility result must carry certifies_production=False")
    problems = validate_checks(eligibility.get("checks"))
    if problems:
        raise CertificationError("eligibility checks incomplete: " + "; ".join(problems))
    if eligibility.get("protected_tier") != TIER_PROTECTED:
        raise CertificationError("eligibility did not come from protected Tier-2 evidence")

    expected_repo = resolve_repository_binding()
    for field, expected in expected_repo.items():
        if eligibility.get(field) != expected:
            raise CertificationError(
                f"{field} has drifted since eligibility was established; refusing to certify a "
                "tree that no longer exists")

    transition(PRODUCTION_CERTIFICATION_ELIGIBLE, PRODUCTION_CERTIFIED)
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "certification_state": PRODUCTION_CERTIFIED,
        "certifies_production": True,
        "candidate_id": eligibility["candidate_id"],
        "candidate_manifest_sha256": eligibility["candidate_manifest_sha256"],
        "head": eligibility["head"], "index_tree_hash": eligibility["index_tree_hash"],
        "commit_tree_hash": eligibility["commit_tree_hash"],
        "repository_diff_sha256": eligibility["repository_diff_sha256"],
        "external_anchor_sha256": eligibility["external_anchor_sha256"],
        "external_inventory_sha256": eligibility["external_inventory_sha256"],
        "protected_tier": TIER_PROTECTED,
        "certified_at_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "valid_until_utc": (now + validity).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "checks": eligibility["checks"],
        "certifier_provenance": certifier_provenance,
        "eligibility_sha256": canonical_sha256(eligibility),
    }
    return artifact


def derive_current_state(*, tier: str | None = None) -> dict:
    """The current state, DERIVED through classify() rather than hardcoded."""
    import os

    tier = tier or os.environ.get("SIGNALNEST_ANCHOR_TIER") or TIER_SYNTHETIC
    synthetic = tier != TIER_PROTECTED
    state = classify(tier=tier, has_protected_anchor=False, anchor_hash_verified=False,
                     synthetic_source=synthetic, candidate_bound=False, repository_bound=False,
                     production_checks_passed=False, artifact_present=False)
    binding = resolve_repository_binding()
    return {"certification_state": state, "certifies_production": REQUIRED_FLAG[state],
            **binding,
            "why": ("derived from the declared tier and the absence of protected evidence; local "
                    "and Tier-1 runs cannot certify production, which is correct and is not a "
                    "failure")}


# --- CLI ---------------------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="production certification")
    sub = parser.add_subparsers(dest="command")

    p_state = sub.add_parser("state", help="derive the current state (INFORMATIONAL, never permits)")
    p_state.add_argument("--json", action="store_true")

    p_elig = sub.add_parser("eligibility", help="establish Tier-2 eligibility (never certifies)")
    for flag in ("--tier2-evidence", "--evidence-sha256", "--inventory-sha256",
                 "--candidate-manifest", "--checks"):
        p_elig.add_argument(flag, required=True)
    p_elig.add_argument("--out", required=True)

    p_cert = sub.add_parser("certify", help="generate the separate certification artifact")
    p_cert.add_argument("--eligibility", required=True)
    p_cert.add_argument("--certifier", required=True)
    p_cert.add_argument("--out", required=True)

    p_verify = sub.add_parser("verify", help="THE production gate; all bindings mandatory")
    for flag in ("--artifact", "--candidate-manifest", "--external-binding"):
        p_verify.add_argument(flag, required=True)
    p_verify.add_argument("--json", action="store_true")

    args = parser.parse_args(argv)
    command = args.command or "state"

    try:
        if command == "state":
            state = derive_current_state()
            print(json.dumps(state, indent=2, ensure_ascii=True) if getattr(args, "json", False)
                  else f"  state {state['certification_state']}  "
                       f"certifies_production={state['certifies_production']}\n  {state['why']}")
            print("CERTIFICATION: candidate-validated, not production-certified")
            return 0

        if command == "eligibility":
            result = establish_eligibility(
                tier=TIER_PROTECTED, anchor_path=args.tier2_evidence,
                anchor_sha256=args.evidence_sha256, inventory_sha256=args.inventory_sha256,
                candidate_manifest=load_json(args.candidate_manifest, what="the candidate manifest"),
                checks=load_json(args.checks, what="the production check results")["checks"])
            Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=True) + "\n",
                                      encoding="utf-8")
            print(f"  state {result['certification_state']}  certifies_production=False")
            print("ELIGIBILITY: established (NOT certification)")
            return 0

        if command == "certify":
            artifact = generate_certification(
                load_json(args.eligibility, what="the eligibility result"),
                certifier_provenance=args.certifier)
            Path(args.out).write_text(json.dumps(artifact, indent=2, ensure_ascii=True) + "\n",
                                      encoding="utf-8")
            print(f"  candidate {artifact['candidate_id']}  valid_until {artifact['valid_until_utc']}")
            print("CERTIFICATION ARTIFACT: generated")
            return 0

        if command == "verify":
            verdict = production_gate(
                load_json(args.artifact, what="the certification artifact"),
                candidate_manifest=load_json(args.candidate_manifest, what="the candidate manifest"),
                # GATE 4N-I26B, closing I26B-02. The document is no longer TRUSTED because the
                # caller named it — it is AUTHENTICATED against the digest this repository pins
                # for the declared tier, and refused outright if it does not match. The caller
                # still says WHERE the authorization is; the repository says WHICH one counts.
                external_binding=resolve_external_authorization(
                    Path(args.external_binding),
                    tier=load_json(args.external_binding,
                                   what="the external authorization")["protected_tier"]))
            if getattr(args, "json", False):
                print(json.dumps(verdict, indent=2, ensure_ascii=True))
            else:
                print(f"  {verdict['state']}: {verdict['reason']}")
            print("PRODUCTION GATE: permitted" if verdict["permitted"]
                  else "PRODUCTION GATE: refused")
            return verdict["exit_code"]

    except CertificationError as exc:
        print(f"  {exc}", file=sys.stderr)
        print(f"{command.upper()}: refused")
        return 2

    parser.print_help()
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
