#!/usr/bin/env python3
"""Independent Deny requirements and triangulation (Gate 4N-I8, Defect 2).

THE DEFECT. `scripts/must_not_contract.py` both DEFINED the forbidden set and supplied the
expectation the tests measured against. `test_the_score_denominator_is_the_whole_contract`
compared the score to `len(FORBIDDEN_CAPABILITIES)` — the contract measured against itself.
Deleting one line shrank the policy and the expectation together: the suite stayed green,
`allow_model` reported "clean at 44/44", and two principals silently dropped from
EXPLICIT_DENY to IMPLICIT_DENY. The adversarial lane swept all 135 Deny actions and found
~80 individually removable with a green suite, including the complete boundary-neutering
triad. A requirement that lives in the thing it constrains is not a requirement.

THREE INDEPENDENT SOURCES, and no policy generator among them:

  SOURCE 1  INCIDENT LEDGER — ~/.signalnest/anchor/signalnest-deny-incident-ledger.json,
            OUTSIDE the repository, mode 400, each entry tied to the prior gate and the
            retained artifact that established it. A repository edit cannot reach it.
  SOURCE 2  ARCHITECTURE INVARIANTS — declarative statements about what each principal must
            not be able to do, expanded to actions here. In-repo, but it does not define the
            same rows SOURCE 1 does, so deleting from one leaves the other demanding it.
  SOURCE 3  RESOURCE INVENTORY — the exact protected resources a Deny must actually cover,
            so a Deny scoped to the wrong ARN is CONFLICTING_SCOPE rather than a pass.

The authoritative requirement is SOURCE 1 ∪ SOURCE 2. The generated policy is the SUBJECT,
never a source. Triangulation classifies every requirement and anything other than
REQUIRED_AND_PRESENT fails.

Usage:
    python3 scripts/deny_requirements.py [--json]
Exit: 0 iff every requirement is REQUIRED_AND_PRESENT.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import json
import re
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from external_anchor import AnchorUnavailable  # noqa: E402

# GATE 4N-I13 DEFECT 1: no HOME resolution. SIGNALNEST_REQUIREMENTS_PATH names the retained
# external source explicitly; ordinary CI runs the Tier-1 synthetic set instead.
LEDGER_PATH = None
# Gate 4N-I11 Defect 1: v2, organised by OUTCOME rather than by principal, so it enumerates
# genuinely different rows from the in-repo architecture invariants.
REQUIREMENTS_V2_PATH = None
SYNTHETIC_REQUIREMENTS = REPO_ROOT / "tests" / "fixtures" / "synthetic-requirements.json"
# Operator ruling 2026-10-23 (O11 option 1): requirement AMENDMENTS live in their own record, beside — never inside —
# the requirement source, so the original requirement and any sealed external copy stay byte-for-byte unchanged.
SYNTHETIC_REQUIREMENT_AMENDMENTS = REPO_ROOT / "tests" / "fixtures" / "synthetic-requirement-amendments.json"
SINGLE_GROUNDED = "SINGLE_GROUNDED"

REQUIRED_AND_PRESENT = "REQUIRED_AND_PRESENT"
REQUIRED_BUT_MISSING = "REQUIRED_BUT_MISSING"
PRESENT_BUT_UNJUSTIFIED = "PRESENT_BUT_UNJUSTIFIED"
RESOURCE_UNRESOLVED = "RESOURCE_UNRESOLVED"
CONFLICTING_SCOPE = "CONFLICTING_SCOPE"



# --- GATE 4N-I24D: DECLARED-COUNT INTEGRITY --------------------------------------------------
#
# A file that DECLARES how much it contains, and is never checked against what it actually
# contains, is a field nothing reads. Gate 4N-I24C's independent site discovery found exactly
# that: `ledger_version`, `created_utc`, `entry_count`, `outcome_count` and `action_count` were
# authored, hashed, and consumed by nothing. A truncated or padded SOURCE 1 would have loaded
# clean. These checks make the declaration load-bearing: the file must be internally honest
# before it is allowed to act as an external requirement source.

_UTC_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _declared_utc(doc: dict, field: str, path) -> None:
    value = doc.get(field)
    if not isinstance(value, str) or not _UTC_RE.match(value):
        raise AnchorUnavailable(
            f"{path}: {field!r} is {value!r}; an external requirement source must carry a "
            "canonical UTC stamp (YYYY-MM-DDTHH:MM:SSZ) so its age is reviewable.")


def _declared_count(doc: dict, field: str, actual: int, path) -> None:
    declared = doc.get(field)
    if not isinstance(declared, int):
        raise AnchorUnavailable(
            f"{path}: {field!r} is {declared!r}; a source that does not declare its own size "
            "cannot be checked for truncation.")
    if declared != actual:
        raise AnchorUnavailable(
            f"{path}: {field!r} declares {declared} but the file actually contains {actual}. "
            "A truncated or padded requirement source must never load.")


def _declared_version(doc: dict, field: str, permitted: tuple, path) -> None:
    value = doc.get(field)
    if str(value) not in permitted:
        raise AnchorUnavailable(
            f"{path}: {field!r} is {value!r}; expected one of {permitted}. An unknown schema "
            "version must fail closed rather than be parsed on assumption.")


# --- SOURCE 1: the external incident ledger --------------------------------------------


def _explicit(env_name: str, fallback: Path) -> Path:
    """Explicit environment path, else the tracked synthetic fixture. Never HOME."""
    value = os.environ.get(env_name)
    return Path(value) if value else fallback


def incident_ledger(path: Path | None = None) -> dict:
    path = Path(path) if path is not None else _explicit(
        "SIGNALNEST_LEDGER_PATH", REPO_ROOT / "tests" / "fixtures" / "synthetic-ledger.json")
    if not path.exists():
        raise AnchorUnavailable(
            f"Deny incident ledger missing at {path}. FAIL-CLOSED: without SOURCE 1 the "
            "requirement set collapses to repository-controlled files, which is exactly the "
            "Gate 4N-I7 defect. Restore it from retained gate evidence."
        )
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not doc.get("entries"):
        raise AnchorUnavailable(f"incident ledger at {path} has no entries")
    _declared_version(doc, "ledger_version", ("1",), path)
    _declared_utc(doc, "created_utc", path)
    _declared_count(doc, "entry_count", len(doc["entries"]), path)
    return doc


def external_requirements(path: Path | None = None) -> dict:
    path = Path(path) if path is not None else _explicit(
        "SIGNALNEST_REQUIREMENTS_PATH", SYNTHETIC_REQUIREMENTS)
    if not path.exists():
        raise AnchorUnavailable(
            f"external security requirements missing at {path}. FAIL-CLOSED: without SOURCE 1 "
            "the requirement set collapses to repository-controlled files.")
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not doc.get("entries"):
        raise AnchorUnavailable(f"{path} has no entries")
    _declared_version(doc, "version", ("1", "2"), path)
    _declared_utc(doc, "created_utc", path)
    _declared_count(doc, "outcome_count", len(doc["entries"]), path)
    _declared_count(doc, "action_count",
                    sum(len(e.get("actions", [])) for e in doc["entries"]), path)
    return doc


def source1_actions() -> dict[str, str]:
    """SOURCE 1 = the v1 incident ledger UNION the v2 outcome-organised requirements.

    v1 alone was a strict subset of the repository invariants (Gate 4N-I11 Defect 1). v2 is
    written from the attacker OUTCOME, which enumerates a different set, so SOURCE 1 now
    contributes rows SOURCE 2 does not have.
    """
    out: dict[str, str] = {}
    for e in incident_ledger()["entries"]:
        out[e["action"]] = (f"SOURCE 1 incident ledger ({e['established_by_gate']}): "
                            f"{e['consequence']}")
    for e in external_requirements()["entries"]:
        for action in e["actions"]:
            out.setdefault(action, f"SOURCE 1 outcome {e['outcome_id']} "
                                   f"({e['established_by_gate']}): {e['outcome']}")
    return out



# --- GATE 4N-I21, ADV-B: the requirement metadata is now LOAD-BEARING ------------------------
#
# THE DEFECT. Gate 4N-I16's security lane found `requirement_kind` and `principal` in the
# requirements fixture were read by no code, and Gate 4N-I17's adversarial lane confirmed the
# state was unchanged: blanking all three of `requirement_kind`, `principal` and
# `established_by_gate` on every row left the suite byte-identically green. `established_by_gate`
# was interpolated into a human-readable message string, which is not an assertion — a message
# nobody compares is documentation.
#
# Worse, the enforcing-consumer test that was supposed to catch exactly this had been applied to
# a DIFFERENT fixture (the widening ceiling), so the requirements fixture had no coverage at all.
#
# Each field now DECIDES something:
#
#   requirement_kind    a `permanent` requirement's actions must be denied with NO date
#                       condition. A Deny that expires stops protecting precisely when the
#                       window closes, so "permanent" has to mean something mechanical.
#   principal           the named principal's own policy must actually deny those actions. This
#                       binds an abstract requirement to an evaluated document.
#   established_by_gate must be a well-formed gate identifier and is ASSERTED, not interpolated.
#   outcome_id          must be unique; a duplicated id silently merges two requirements.
#   evidence_artifact   must be named, and evidence_sha256 must be a real digest.

# The authored vocabulary. NOT derived from the fixture's own values — a set read out of the
# document it validates would accept whatever that document happened to say.
_KINDS = frozenset({"permanent", "temporary", "workflow"})
_GATE_RE = re.compile(r"^4N-[A-Z0-9]+[0-9A-Za-z.\-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")

# requirement_kind -> the policy documents whose Deny statements must carry no expiry.
PERMANENT_KIND = "permanent"


def requirement_metadata_problems() -> list[str]:
    """Every authored field decides something. Absence, malformation or a blank fails."""
    problems: list[str] = []
    entries = external_requirements()["entries"]
    if not entries:
        return ["the external requirements fixture declares no entries"]

    seen_ids: set[str] = set()
    for entry in entries:
        oid = entry.get("outcome_id")
        if not oid:
            problems.append("an entry declares no outcome_id")
            continue
        if oid in seen_ids:
            problems.append(f"{oid}: duplicate outcome_id — two requirements would merge")
        seen_ids.add(oid)

        kind = entry.get("requirement_kind")
        if kind not in _KINDS:
            problems.append(f"{oid}: requirement_kind {kind!r} is not one of {sorted(_KINDS)}")

        principal = entry.get("principal")
        if not principal or not str(principal).strip():
            problems.append(f"{oid}: principal is blank — the requirement binds to nobody")

        gate = entry.get("established_by_gate")
        if not gate or not _GATE_RE.match(str(gate)):
            problems.append(f"{oid}: established_by_gate {gate!r} is not a gate identifier")

        if not entry.get("evidence_artifact"):
            problems.append(f"{oid}: no evidence_artifact is named")
        digest = entry.get("evidence_sha256")
        if not digest or not _SHA_RE.match(str(digest)):
            problems.append(f"{oid}: evidence_sha256 {str(digest)[:12]!r} is not a sha256 digest")

        if not entry.get("actions"):
            problems.append(f"{oid}: declares no actions")
    return problems


def permanent_requirements_never_expire() -> list[str]:
    """A `permanent` requirement's actions must be denied WITHOUT a date condition.

    This is what makes requirement_kind decide something: flipping a row to `temporary`, or
    letting a permanent Deny acquire an expiry, changes the outcome of this check.
    """
    import iam_eval

    problems: list[str] = []
    documents = _reviewed_documents()
    for entry in external_requirements()["entries"]:
        if entry.get("requirement_kind") != PERMANENT_KIND:
            continue
        for action in entry.get("actions", []):
            for name, document in documents.items():
                for statement in document.get("Statement", []):
                    if statement.get("Effect") != "Deny":
                        continue
                    declared = statement.get("Action") or []
                    declared = [declared] if isinstance(declared, str) else declared
                    if action not in declared:
                        continue
                    condition = statement.get("Condition") or {}
                    if any(op.startswith("Date") for op in condition):
                        problems.append(
                            f"{entry['outcome_id']}: {action} is a PERMANENT requirement but "
                            f"{name}/{statement.get('Sid')} denies it with a date condition — a "
                            "Deny that expires stops protecting when the window closes")
    return problems


def principals_actually_deny_their_requirements() -> list[str]:
    """`principal` names the SCOPE a requirement binds to, and that scope must hold.

    DESIGN NOTE, recorded because the first version of this check was wrong. It compared each
    requirement against every generated document and demanded EXPLICIT_DENY from all of them,
    including the permissions BOUNDARY. A boundary is a ceiling: it Allows broadly and carves
    with Denies, so it legitimately resolves EXPLICIT_ALLOW for actions it does not carve, and
    the check produced sixteen false findings. A control that misfires on correct policy is
    worse than none — it gets suppressed.

    What `principal` actually decides is SCOPE. Every row here carries the universal scope, and
    the universal scope means the action must be in the repository's mandatory deny set, which
    is the contract the rest of this module already enforces per principal. A blank principal,
    or one naming a scope this module does not recognise, fails.
    """
    import iam_eval

    problems: list[str] = []
    amended, amendment_findings = _active_amendments()
    problems.extend(amendment_findings)
    for entry in external_requirements()["entries"]:
        principal = str(entry.get("principal") or "").strip()
        scope = _PRINCIPAL_SCOPES.get(principal)
        if scope is None:
            problems.append(
                f"{entry['outcome_id']}: principal {principal!r} names no scope this module "
                f"recognises; known scopes are {sorted(_PRINCIPAL_SCOPES)}")
            continue
        if scope != "ALL_DESIGNED_PRINCIPALS":
            continue
        for action in entry.get("actions", []):
            for name, document in _principal_documents().items():
                amendment_id = amended.get((entry["outcome_id"], name, action))
                if amendment_id is not None:
                    # The ruled amendment REPLACES the universal "*" probe for this one pair with the
                    # independently grounded expectations of the ruling; it never skips the pair.
                    problems.extend(_amended_pair_problems(entry["outcome_id"], amendment_id, name,
                                                           document, action))
                    continue
                decision = iam_eval.decide(document, action, "*", _PROBE_CONTEXT).decision
                if decision is not iam_eval.Decision.EXPLICIT_DENY:
                    problems.append(
                        f"{entry['outcome_id']}: principal scope is every designed principal, "
                        f"but {name} resolves {decision.value} for {action} rather than an "
                        "explicit Deny")
    return problems


def _principal_documents() -> dict:
    """The generated PRINCIPAL policies.

    The permissions BOUNDARY is deliberately excluded: it is a ceiling that Allows broadly and
    carves with Denies, so it legitimately resolves EXPLICIT_ALLOW for anything it does not
    carve. Demanding a Deny from it produced sixteen false findings in the first draft of this
    check, and a control that misfires on correct policy is one that gets suppressed.
    """
    return {name: document for name, document in _reviewed_documents().items()
            if name != "boundary"}


# Authored scope vocabulary. A phrase absent from this table is a finding, so a new or blanked
# principal cannot pass by being unrecognised.
_PRINCIPAL_SCOPES = {
    "any principal in the SignalNest design": "ALL_DESIGNED_PRINCIPALS",
}


_PROBE_CONTEXT = {"aws:CurrentTime": "2026-08-01T18:00:00Z", "aws:RequestedRegion": "us-east-1"}


# --- OPERATOR RULING 2026-10-23: the O11 amendment for the key-apply principal's kms:PutKeyPolicy ---------------------
#
# O11 (SOURCE 1 v2, gate 4N-H1, permanent; "the keys protecting state and secrets are used, delegated, or destroyed")
# binds "any principal in the SignalNest design", and principals_actually_deny_their_requirements() enforced it as an
# EXPLICIT_DENY of every O11 action at Resource "*" for every designed principal. The operator decided D-K1-3 = (b-wide)
# (the key-apply principal holds an expiring, region-conditioned Allow of kms:PutKeyPolicy on "*" for the CreateKey
# policy-lockout check) and then, on 2026-10-23, authorized "option 1": a NARROWLY SCOPED REQUIREMENT AMENDMENT for that
# one principal and that one action. THIS CHANGES THE PREVIOUS UNIVERSAL Resource "*" ENFORCEMENT for exactly the pair
# (key_apply, kms:PutKeyPolicy); every other principal and every other O11 action keeps the "*" check unchanged.
#
# Two keys must agree before the exception applies (fail-closed in both directions):
#   1. this AUTHORED ruling table (code, review-pinned), written from the ruling text — not from any fixture value and
#      not from the generated policy;
#   2. an ACTIVE, provenance-carrying amendment record (tests/fixtures/synthetic-requirement-amendments.json, or the
#      external record named by SIGNALNEST_REQUIREMENT_AMENDMENTS_PATH) whose every provenance field equals this table.
# An amendment for any other (outcome, principal, action), or one with missing or mismatching provenance, is a finding
# and is not applied. A ruling with no matching record applies nothing, so the "*" probe — and its finding — stands.
_REQUIREMENT_AMENDMENT_RULINGS = {
  "rulings": {
    "O11-AMEND-2026-10-23-KEY-APPLY-PUTKEYPOLICY": {
        "amends_outcome_id": "O11",
        "principal": "key_apply",
        "action": "kms:PutKeyPolicy",
        "ruling_date": "2026-10-23",
        "ruled_by": "operator",
        "ruling_record": "P6-W6-INFRABRIEF-01/decision/OPERATOR-REPLY-2026-10-23-2-verbatim.md section 1",
        "ruling_sha256": "e2a963e32d1aac0f1424c7749a29aa6511be8b38cf6d7f4a87d7356b11fa80a5",
        # The ruling's "two protected state/secrets keys", named by their identity-module role (never by a policy Sid).
        "protected_keys": ("state_cmk", "secrets_cmk"),
    },
  },
  # Every amendment record must carry all of these (non-blank; `retains` a non-empty list) or it is refused.
  "provenance_fields": ("amendment_id", "amends_outcome_id", "amends_action_set_sha256", "principal", "action",
                        "ruling_date", "ruled_by", "ruling_record", "ruling_sha256", "status", "replaces", "retains",
                        "context"),
  "statuses": ("ACTIVE", "SUPERSEDED"),
}
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def requirement_amendments(path: Path | None = None) -> dict:
    """The requirement-amendment record. Explicit SIGNALNEST_REQUIREMENT_AMENDMENTS_PATH, else — only while the
    requirement source itself is the tracked synthetic set — the tracked synthetic amendments fixture. An EXTERNAL
    requirement source never inherits a repository-controlled amendment (that would let a repository file relax SOURCE
    1): without an explicit external amendment record it carries no amendment at all, so the original check applies."""
    if path is None:
        explicit = os.environ.get("SIGNALNEST_REQUIREMENT_AMENDMENTS_PATH")
        if explicit:
            path = Path(explicit)
        elif os.environ.get("SIGNALNEST_REQUIREMENTS_PATH"):
            return {"version": "1", "amendments": []}
        else:
            path = SYNTHETIC_REQUIREMENT_AMENDMENTS
    path = Path(path)
    if not path.exists():
        raise AnchorUnavailable(f"requirement amendment record missing at {path}. FAIL-CLOSED.")
    doc = json.loads(path.read_text(encoding="utf-8"))
    _declared_version(doc, "version", ("1",), path)
    _declared_utc(doc, "created_utc", path)
    _declared_count(doc, "amendment_count", len(doc.get("amendments") or []), path)
    return doc


def _action_set_sha256(actions) -> str:
    return hashlib.sha256(json.dumps(sorted(actions), separators=(",", ":")).encode("utf-8")).hexdigest()


def _active_amendments() -> tuple[dict, list[str]]:
    """{(outcome_id, principal, action): amendment_id} for every VALID, ACTIVE amendment, plus the findings."""
    problems: list[str] = []
    active: dict = {}
    base = {e.get("outcome_id"): e for e in external_requirements()["entries"]}
    principals = set(_principal_documents())
    seen: set = set()
    for record in requirement_amendments().get("amendments") or []:
        aid = str(record.get("amendment_id") or "").strip() or "<no amendment_id>"
        missing = [f for f in _REQUIREMENT_AMENDMENT_RULINGS["provenance_fields"] if f != "retains"
                   and not str(record.get(f) or "").strip()]
        retains = record.get("retains")
        if not isinstance(retains, list) or not retains or not all(str(r).strip() for r in retains):
            missing.append("retains")
        if missing:
            problems.append(f"amendment {aid}: missing provenance field(s) {missing}; not applied")
            continue
        if aid in seen:
            problems.append(f"amendment {aid}: duplicate amendment_id; not applied")
            continue
        seen.add(aid)
        ruling = _REQUIREMENT_AMENDMENT_RULINGS["rulings"].get(aid)
        if ruling is None:
            problems.append(f"amendment {aid}: no operator ruling authorizes it (authored rulings: "
                            f"{sorted(_REQUIREMENT_AMENDMENT_RULINGS['rulings'])}); not applied")
            continue
        wrong = [f for f in ("amends_outcome_id", "principal", "action", "ruling_date", "ruled_by", "ruling_record",
                             "ruling_sha256") if record.get(f) != ruling[f]]
        if wrong:
            problems.append(f"amendment {aid}: field(s) {wrong} differ from the authored ruling — the exception "
                            "cannot be moved to another principal/action or re-attributed; not applied")
            continue
        if not _DATE_RE.match(str(record["ruling_date"])) or not _SHA_RE.match(str(record["ruling_sha256"])):
            problems.append(f"amendment {aid}: malformed ruling_date or ruling_sha256; not applied")
            continue
        if record["status"] not in _REQUIREMENT_AMENDMENT_RULINGS["statuses"]:
            problems.append(f"amendment {aid}: status {record['status']!r} not in "
                            f"{_REQUIREMENT_AMENDMENT_RULINGS['statuses']}; not applied")
            continue
        entry = base.get(record["amends_outcome_id"])
        if entry is None or record["action"] not in entry.get("actions", []):
            problems.append(f"amendment {aid}: {record['amends_outcome_id']}/{record['action']} is not a requirement "
                            "of the loaded source; not applied")
            continue
        if record["amends_action_set_sha256"] != _action_set_sha256(entry.get("actions", [])):
            problems.append(f"amendment {aid}: the amended requirement {record['amends_outcome_id']} changed since the "
                            "ruling (action-set digest differs); not applied")
            continue
        if record["principal"] not in principals:
            problems.append(f"amendment {aid}: principal {record['principal']!r} is not a designed principal; not applied")
            continue
        if record["status"] == "ACTIVE":
            active[(record["amends_outcome_id"], record["principal"], record["action"])] = aid
    return active, problems


def _amended_pair_problems(outcome_id: str, amendment_id: str, name: str, document: dict, action: str) -> list[str]:
    """The ruled replacement of the "*" probe, from the ruling text (operator reply 2026-10-23 §1) — the protected keys
    from the identity module, the expiry from expiry_authorization, the region from the identity anchor:
      (a) kms:PutKeyPolicy on the state CMK and on the secrets CMK is EXPLICIT_DENY unconditionally (in or out of the
          window, in or out of the region, with or without the bypass flag);
      (b) with kms:BypassPolicyLockoutSafetyCheck = true it is EXPLICIT_DENY on any key (the existing bypass Deny);
      (c) the reviewed b-wide Allow keeps its expiry and Region conditions: on another key it is IMPLICIT_DENY after
          the expiry and outside the region, and EXPLICIT_ALLOW only inside both (so a protected-key Deny widened to
          every key, or a dropped condition, is visible)."""
    import expiry_authorization as _ea
    import iam_eval
    import signalnest_identity as identity

    D = iam_eval.Decision
    ruling = _REQUIREMENT_AMENDMENT_RULINGS["rulings"][amendment_id]
    critical = identity.critical_resources()
    protected = [critical[k] for k in ruling["protected_keys"]]
    other_key = identity.kms_key_arn("00000000-0000-4000-8000-0000000a3e11")
    region = identity.REGION
    other_region = "eu-west-1" if region != "eu-west-1" else "us-west-2"
    expiry = _ea.ACTIVE_EXPIRY_UTC
    after = (datetime.datetime.strptime(expiry, "%Y-%m-%dT%H:%M:%SZ")
             + datetime.timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    within = _PROBE_CONTEXT["aws:CurrentTime"]
    problems: list[str] = []
    if not within < expiry or other_key in protected:
        return [f"{outcome_id} [{amendment_id}]: the amendment's probe inputs are inconsistent (in-window instant "
                f"{within} vs expiry {expiry}); fail-closed"]
    base = {"aws:CurrentTime": within, "aws:RequestedRegion": region, "kms:BypassPolicyLockoutSafetyCheck": "false"}
    variants = {"in window, in region": {}, "bypass flag": {"kms:BypassPolicyLockoutSafetyCheck": "true"},
                "after expiry": {"aws:CurrentTime": after}, "outside the region": {"aws:RequestedRegion": other_region}}
    expected = [(key, label, over, D.EXPLICIT_DENY) for key in protected for label, over in variants.items()]
    expected += [(other_key, "bypass flag", variants["bypass flag"], D.EXPLICIT_DENY),
                 (other_key, "in window, in region", {}, D.EXPLICIT_ALLOW),
                 (other_key, "after expiry", variants["after expiry"], D.IMPLICIT_DENY),
                 (other_key, "outside the region", variants["outside the region"], D.IMPLICIT_DENY)]
    for resource, label, over, want in expected:
        got = iam_eval.decide(document, action, resource, {**base, **over}).decision
        if got is not want:
            problems.append(f"{outcome_id} [{amendment_id}]: {name} resolves {got.value} for {action} on {resource} "
                            f"({label}); the ruled amendment requires {want.value}")
    return problems


def _principal_matches(principal: str, document_name: str) -> bool:
    """Map a requirement's principal phrase onto the generated documents it constrains."""
    lowered = principal.lower()
    if "any principal" in lowered or "every" in lowered or "all " in lowered:
        return True
    return document_name.replace("_", " ") in lowered or document_name in lowered


def _reviewed_documents() -> dict:
    import expiry_authorization as _ea
    import gen_bootstrap_operator_policy as boot
    import gen_boundary_policy as gb
    import gen_operator_policies as gen
    import gen_role_bootstrap_policy as rb

    expiry = _ea.ACTIVE_EXPIRY_UTC
    return {"permanent_w0": gen.permanent_w0_policy(),
            "stage_a": gen.bootstrap_temp_policy(expiry),
            "role_bootstrap": rb.role_bootstrap_policy(expiry),
            "boundary": gb.boundary_policy(),
            "boundary_bootstrap": boot.bootstrap_operator_policy(expiry),
            # P6-W0-TRANSITION: the window principal's EFFECTIVE policy (inline + both customer managed).
            "window_transition": gen.window_transition_effective_policy(expiry, "api.synthetic.example.com"),
            # P6-CI-6 K0-2: the key-apply principal's EFFECTIVE policy (inline + both customer managed).
            "key_apply": gen.key_apply_effective_policy(expiry, "api.synthetic.example.com")}

def independence() -> dict:
    """Phase B: independence must be MEANINGFUL, not merely file separation."""
    s1, s2 = set(source1_actions()), set(source2_actions())
    only_external, only_repository = sorted(s1 - s2), sorted(s2 - s1)
    return {
        "external_count": len(s1), "repository_count": len(s2),
        "only_in_external": only_external, "only_in_repository": only_repository,
        "both": len(s1 & s2),
        "external_minus_repository_nonempty": bool(only_external),
        "repository_minus_external_nonempty": bool(only_repository),
        "independent": bool(only_external) and bool(only_repository),
    }


def grounds_for(action: str) -> list[str]:
    return [label for label, table in (("SOURCE_1_EXTERNAL", source1_actions()),
                                       ("SOURCE_2_INVARIANT", source2_actions()),
                                       ("SOURCE_3_AWS_SAFETY", source3_actions()))
            if action in table]


def single_grounded() -> list[dict]:
    """Phase C: a mandatory capability with exactly one ground is a DEFECT, not a warning."""
    return [{"action": a, "grounds": g}
            for a in sorted(required_denies())
            if len(g := grounds_for(a)) < 2]


# --- SOURCE 2: architecture invariants --------------------------------------------------
#
# Stated as PROPERTIES of the design, then expanded to actions. The expansion is the part a
# reviewer should attack: an invariant whose expansion is too narrow silently permits the
# thing the invariant forbids.

ARCHITECTURE_INVARIANTS = {
    "permanent_w0_must_not_mutate_iam": [
        "iam:AttachRolePolicy", "iam:AttachUserPolicy", "iam:CreateAccessKey",
        "iam:CreateInstanceProfile", "iam:CreateOpenIDConnectProvider", "iam:CreatePolicy",
        "iam:CreatePolicyVersion", "iam:CreateRole", "iam:CreateServiceLinkedRole",
        "iam:CreateUser", "iam:DeletePolicy", "iam:DeletePolicyVersion", "iam:DeleteRole",
        "iam:DeleteRolePermissionsBoundary", "iam:DeleteRolePolicy", "iam:DetachRolePolicy",
        "iam:PassRole", "iam:PutRolePermissionsBoundary", "iam:PutRolePolicy",
        "iam:SetDefaultPolicyVersion", "iam:UpdateAssumeRolePolicy", "iam:UpdateRole",
    ],
    "no_principal_may_read_or_write_terraform_state": [
        "s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:DeleteObjectVersion",
        # dynamodb:UpdateItem forges or corrupts a lock item exactly as PutItem does and was
        # absent (Gate 4N-I11 Defect 16).
        "dynamodb:GetItem", "dynamodb:PutItem", "dynamodb:DeleteItem", "dynamodb:UpdateItem",
    ],
    "no_principal_may_disable_or_erase_logging": [
        # GATE 4N-I11 DEFECT 16. Suspending versioning on the audit bucket defeats the
        # protection s3:DeleteObjectVersion exists for, without deleting anything.
        # Disabling access logging removes the record of who touched it. Both were absent.
        "s3:PutBucketVersioning", "s3:PutBucketLogging", "s3:PutBucketNotification",
        "cloudtrail:StopLogging", "cloudtrail:DeleteTrail", "cloudtrail:UpdateTrail",
        "cloudtrail:PutEventSelectors", "cloudtrail:PutInsightSelectors",
        "logs:DeleteLogGroup", "logs:DeleteLogStream", "logs:PutRetentionPolicy",
        "s3:PutBucketPolicy", "s3:PutBucketAcl", "s3:PutObjectAcl",
        "s3:PutLifecycleConfiguration",
    ],
    "no_principal_may_retrieve_or_alter_secret_material": [
        "secretsmanager:GetSecretValue", "secretsmanager:PutSecretValue",
        "secretsmanager:DeleteSecret", "secretsmanager:UpdateSecret",
        "secretsmanager:PutResourcePolicy",
        "kms:Decrypt", "kms:CreateGrant", "kms:PutKeyPolicy", "kms:ScheduleKeyDeletion",
        "kms:ReEncryptFrom", "kms:ReEncryptTo",
    ],
    "no_principal_may_run_workloads": [
        "ecs:RegisterTaskDefinition", "ecs:RunTask", "ecs:CreateService",
        "ecs:UpdateService", "ecs:ExecuteCommand",
    ],
    "no_principal_may_administer_identity_center_or_the_organization": [
        "sso:PutInlinePolicyToPermissionSet", "sso:ProvisionPermissionSet",
        "sso:CreateAccountAssignment", "sso:DeleteAccountAssignment",
        "sso:CreatePermissionSet", "sso:DeletePermissionSet",
        "organizations:LeaveOrganization",
    ],
    "no_principal_may_destroy_or_expose_the_database": [
        "rds:DeleteDBInstance", "rds:ModifyDBInstance",
        "rds:RestoreDBInstanceFromDBSnapshot", "rds:ModifyDBSnapshotAttribute",
    ],
    "no_principal_may_chain_out_of_the_account": [
        "sts:AssumeRole", "sts:GetFederationToken",
    ],
    # Found by the Gate 4N-I7 architect lane. The bootstrap operator could rewrite the
    # reviewed boundary to Allow * and set it default.
    "no_principal_may_rewrite_the_security_ceiling": [
        "iam:CreatePolicyVersion", "iam:SetDefaultPolicyVersion", "iam:DeletePolicyVersion",
        "iam:DeletePolicy",
    ],
}


# --- SOURCE 3: AWS service safety rules -------------------------------------------------
#
# GATE 4N-I11 PHASE C. Independence (S1 and S2 enumerating different rows) and grounding
# (every capability having >= 2 grounds) pull in opposite directions: closing every
# single-ground gap by copying rows between S1 and S2 would collapse them back into one
# source, which is Defect 1 again.
#
# SOURCE 3 resolves it on a THIRD axis the gate explicitly permits ("architecture invariant +
# AWS-service safety rule"): EQUIVALENT EFFECT. If AWS offers two APIs that reach the same
# end state, denying one and not the other is an accident of enumeration, not a decision.
# Each rule names the anchor action already grounded elsewhere and the siblings that reach
# the same outcome through a different call.
AWS_SERVICE_SAFETY = {
    "dynamodb:PutItem": ["dynamodb:UpdateItem", "dynamodb:BatchWriteItem"],
    "dynamodb:DeleteItem": ["dynamodb:DeleteTable", "dynamodb:BatchWriteItem"],
    "ecs:RunTask": ["ecs:StartTask"],
    "iam:CreateOpenIDConnectProvider": ["iam:AddClientIDToOpenIDConnectProvider",
                                        "iam:UpdateOpenIDConnectProviderThumbprint"],
    "iam:CreateInstanceProfile": ["iam:AddRoleToInstanceProfile"],
    "iam:PutRolePolicy": ["iam:PutUserPolicy"],
    "iam:CreateRole": ["iam:DeleteRole"],
    "s3:PutBucketPolicy": ["s3:PutBucketOwnershipControls", "s3:PutBucketPublicAccessBlock",
                           "s3:DeleteBucketPolicy"],
    "s3:DeleteObject": ["s3:DeleteBucket"],
    "s3:PutBucketVersioning": ["s3:PutBucketNotification"],
    "secretsmanager:PutSecretValue": ["secretsmanager:RestoreSecret"],
    "kms:ScheduleKeyDeletion": ["kms:DisableKey", "kms:RetireGrant", "kms:RevokeGrant"],
    "rds:RestoreDBInstanceFromDBSnapshot": ["rds:CreateDBSnapshot", "rds:DeleteDBSnapshot"],
    "sts:AssumeRole": ["sts:AssumeRoleWithSAML", "sts:AssumeRoleWithWebIdentity"],
    "logs:PutRetentionPolicy": ["logs:DeleteRetentionPolicy"],
}


def source3_actions() -> dict[str, str]:
    out: dict[str, str] = {}
    for anchor_action, siblings in AWS_SERVICE_SAFETY.items():
        for sibling in siblings:
            out.setdefault(sibling, f"SOURCE 3 AWS service safety: reaches the same end state "
                                    f"as {anchor_action}, which is independently required")
    return out


def source2_actions() -> dict[str, str]:
    out: dict[str, str] = {}
    for invariant, actions in ARCHITECTURE_INVARIANTS.items():
        for action in actions:
            out.setdefault(action, f"SOURCE 2 architecture invariant: {invariant}")
    return out


# --- the authoritative requirement -------------------------------------------------------


def required_denies() -> dict[str, dict]:
    """SOURCE 1 union SOURCE 2. The generated policy is NOT consulted."""
    s1, s2, s3 = source1_actions(), source2_actions(), source3_actions()
    out: dict[str, dict] = {}
    for action in sorted(set(s1) | set(s2) | set(s3)):
        out[action] = {
            "action": action,
            "in_source_1": action in s1,
            "in_source_2": action in s2,
            "in_source_3": action in s3,
            "ground_count": sum(action in t for t in (s1, s2, s3)),
            "justification": [j for j in (s1.get(action), s2.get(action), s3.get(action)) if j],
        }
    return out


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        required = required_denies()
    except AnchorUnavailable as exc:
        print(f"  {exc}", file=sys.stderr)
        print("DENY REQUIREMENTS: fail-closed")
        return 2
    s1 = sum(1 for r in required.values() if r["in_source_1"])
    s2 = sum(1 for r in required.values() if r["in_source_2"])
    both = sum(1 for r in required.values() if r["in_source_1"] and r["in_source_2"])
    if args.json:
        print(json.dumps({"required": required, "counts":
                          {"total": len(required), "source_1": s1, "source_2": s2,
                           "both": both}}, indent=2))
    else:
        print(f"  required Deny actions {len(required)}  "
              f"(SOURCE 1 {s1}, SOURCE 2 {s2}, corroborated by both {both})")

    # GATE 4N-I21, ADV-B. The authored requirement metadata now DECIDES the exit code. Before
    # this, `requirement_kind` and `principal` were read by nothing and `established_by_gate`
    # was interpolated into a message string, so blanking all three left the suite green.
    problems = (requirement_metadata_problems()
                + permanent_requirements_never_expire()
                + principals_actually_deny_their_requirements())
    if problems:
        for problem in problems:
            print(f"    {problem}", file=sys.stderr)
        print("DENY REQUIREMENTS: findings")
        return 1
    if not args.json:
        print("DENY REQUIREMENTS: loaded")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
