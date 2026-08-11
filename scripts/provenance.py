#!/usr/bin/env python3
"""Value-bearing, type-aware provenance verification (Gate 4N-I16, Defect 2).

THE DEFECT THIS REPLACES. The Gate 4N-I15 version checked that a named field EXISTED. It
did not compare the claimed value to the source value, and it had no notion of a field being
RELEVANT to a claim. The consequence shipped: the row certifying the boundary policy ARN —
the most load-bearing identifier in this gate chain — named the field `_captured_utc`, a
capture DATE, carried no `value` key so no comparison ran, and returned verified=True with
`certifies_production: True`. The source file contained no boundary reference of any kind.

    A source timestamp cannot certify an ARN.

That sentence is now a mechanical rule, not a comment. Every record declares the SEMANTIC
TYPE of its claim and the SEMANTIC TYPE of the field it reads, both are detected from the
actual bytes, and a support matrix decides whether that type of evidence can bear that type
of claim. Presence was never evidence; it is now not accepted as evidence.

WHAT "VERIFIED" MEANS HERE. A row is verified when, and only when:
  1. the source exists and its bytes hash to what the record expects (when pinned);
  2. the named field exists;
  3. the field's OBSERVED type is a type permitted to support the CLAIMED type;
  4. a declared comparison method actually ran and returned equal.
A row that cannot meet all four is DOWNGRADED, never deleted — deleting a weakly-grounded
row destroys the information that it is weakly grounded.

CONSUMER SAFETY. Labels gate use. SYNTHETIC_TEST_FIXTURE cannot certify production,
CI_EQUIVALENT_LOCAL_REPRODUCTION cannot satisfy an actual-CI requirement, and UNKNOWN or
INFERRED cannot authorize a mutation.

Usage:
    python3 scripts/provenance.py [--json]
Exit: 0 iff every claim is SUPPORTED (not merely present) and no weak label certifies
production.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import signalnest_identity as identity  # noqa: E402

# --- labels ---------------------------------------------------------------------------------

LIVE_READ_EXACT = "LIVE_READ_EXACT"
CLOUDTRAIL_REQUEST_PARAMETER = "CLOUDTRAIL_REQUEST_PARAMETER"
IMMUTABLE_HISTORICAL_ARTIFACT = "IMMUTABLE_HISTORICAL_ARTIFACT"
REPOSITORY_EXPRESSION = "REPOSITORY_EXPRESSION"
EXTERNAL_OPERATOR_INPUT = "EXTERNAL_OPERATOR_INPUT"
DETERMINISTIC_NOT_YET_CREATED = "DETERMINISTIC_NOT_YET_CREATED"
SYNTHETIC_TEST_FIXTURE = "SYNTHETIC_TEST_FIXTURE"
CI_EQUIVALENT_LOCAL_REPRODUCTION = "CI_EQUIVALENT_LOCAL_REPRODUCTION"
PROVIDER_SOURCE = "PROVIDER_SOURCE"
AWS_SERVICE_AUTHORIZATION_METADATA = "AWS_SERVICE_AUTHORIZATION_METADATA"
INFERRED = "INFERRED"
UNKNOWN = "UNKNOWN"

LABELS = (LIVE_READ_EXACT, CLOUDTRAIL_REQUEST_PARAMETER, IMMUTABLE_HISTORICAL_ARTIFACT,
          REPOSITORY_EXPRESSION, EXTERNAL_OPERATOR_INPUT, DETERMINISTIC_NOT_YET_CREATED,
          SYNTHETIC_TEST_FIXTURE, CI_EQUIVALENT_LOCAL_REPRODUCTION, PROVIDER_SOURCE,
          AWS_SERVICE_AUTHORIZATION_METADATA, INFERRED, UNKNOWN)

AUTHORITATIVE = {LIVE_READ_EXACT, CLOUDTRAIL_REQUEST_PARAMETER, IMMUTABLE_HISTORICAL_ARTIFACT,
                 REPOSITORY_EXPRESSION, EXTERNAL_OPERATOR_INPUT, DETERMINISTIC_NOT_YET_CREATED,
                 PROVIDER_SOURCE, AWS_SERVICE_AUTHORIZATION_METADATA}
NON_CERTIFYING = {SYNTHETIC_TEST_FIXTURE, CI_EQUIVALENT_LOCAL_REPRODUCTION, INFERRED, UNKNOWN}

# --- Phase F: semantic types ----------------------------------------------------------------
#
# Types are DETECTED from the observed bytes, never taken from the record's own say-so. A
# record that declares its source field is an ARN, over a field that holds a date, must be
# rejected — so the detector, not the author, decides.

T_ARN = "ARN"
T_ACCOUNT_ID = "ACCOUNT_ID"
T_SHA256 = "SHA256"
T_TIMESTAMP = "TIMESTAMP"
T_BOOLEAN = "BOOLEAN"
T_VERSION = "VERSION"
T_NAME = "NAME"
T_HCL_EXPRESSION = "HCL_EXPRESSION"
T_ABSENT = "ABSENT"

TYPES = (T_ARN, T_ACCOUNT_ID, T_SHA256, T_TIMESTAMP, T_BOOLEAN, T_VERSION, T_NAME,
         T_HCL_EXPRESSION, T_ABSENT)

_ARN_RE = re.compile(r"^arn:[a-z0-9-]+:[a-z0-9-]*:[a-z0-9-]*:\d{0,12}:.+$")
_ACCOUNT_RE = re.compile(r"^\d{12}$")
_SHA_RE = re.compile(r"^(sha256:)?[0-9a-f]{64}$")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2}(:\d{2})?Z?)?$")
_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
_HCL_RE = re.compile(r"\$\{|^\s*(coalesce|join|format|try|lookup|merge)\s*\(")


def detect_type(value: object) -> str:
    """The observed semantic type of a value. ORDER MATTERS: ARN before ACCOUNT_ID."""
    if value is None:
        return T_ABSENT
    if isinstance(value, bool):
        return T_BOOLEAN
    text = str(value)
    if _ARN_RE.match(text):
        return T_ARN
    if _ACCOUNT_RE.match(text):
        return T_ACCOUNT_ID
    if _SHA_RE.match(text):
        return T_SHA256
    if _TS_RE.match(text):
        return T_TIMESTAMP
    if _VERSION_RE.match(text):
        return T_VERSION
    if _HCL_RE.search(text):
        return T_HCL_EXPRESSION
    return T_NAME


# --- Phase G/H: the support matrix ----------------------------------------------------------
#
# claimed type -> the set of OBSERVED source-field types that may support it.
#
# READ THE TIMESTAMP ROW. A TIMESTAMP source supports ONLY a TIMESTAMP claim. That single
# entry is what makes the Gate 4N-I15 boundary-ARN defect unrepresentable: `_captured_utc`
# detects as TIMESTAMP, an ARN claim admits only ARN evidence, so the row cannot verify.

SUPPORT = {
    T_ARN: {T_ARN},
    # An ARN carries its account in a fixed position, so it can bear an account claim.
    T_ACCOUNT_ID: {T_ACCOUNT_ID, T_ARN},
    T_SHA256: {T_SHA256},
    T_TIMESTAMP: {T_TIMESTAMP},
    T_BOOLEAN: {T_BOOLEAN},
    T_VERSION: {T_VERSION, T_NAME},
    # A NAME claim is the weakest; an HCL expression names a value the repository computes.
    T_NAME: {T_NAME, T_HCL_EXPRESSION, T_VERSION},
    T_HCL_EXPRESSION: {T_HCL_EXPRESSION, T_NAME},
}

# --- comparison methods ---------------------------------------------------------------------

C_EXACT_STRING = "EXACT_STRING"
C_ARN_COMPONENTWISE = "ARN_COMPONENTWISE"
C_ACCOUNT_OF_ARN = "ACCOUNT_OF_ARN"
C_SHA256_OF_FILE = "SHA256_OF_FILE"
C_DETERMINISTIC_CONSTRUCTION = "DETERMINISTIC_CONSTRUCTION"
C_SUBSTRING_OF_TEXT = "SUBSTRING_OF_TEXT"
C_STRUCTURAL_ONLY = "STRUCTURAL_ONLY"
# GATE 4N-I17 DEFECT 5. The expected side comes from a TRACKED FIXTURE, the observed side from the
# live inventory. Two files, two lineages. Gate 4N-I16 read both from the inventory, so the
# comparison could not fail whatever the inventory contained.
C_DIGEST_AGAINST_TRACKED_FIXTURE = "DIGEST_AGAINST_TRACKED_FIXTURE"

COMPARISONS = (C_EXACT_STRING, C_ARN_COMPONENTWISE, C_ACCOUNT_OF_ARN, C_SHA256_OF_FILE,
               C_DETERMINISTIC_CONSTRUCTION, C_SUBSTRING_OF_TEXT, C_STRUCTURAL_ONLY,
               C_DIGEST_AGAINST_TRACKED_FIXTURE)

DIGEST_ALGORITHM = "sha256(utf8(value))"
EXPECTED_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "expected-provenance-values.json"


def expected_digests(tier: str | None = None) -> dict:
    # GATE 4N-I24D. `algorithm` was authored and consumed by NOTHING, so the fixture could
    # declare one digest method while compare() used another and every row would still agree
    # with itself. The declared algorithm must match the one this module actually applies.
    _doc = json.loads(EXPECTED_FIXTURE.read_text(encoding="utf-8"))
    _declared = _doc.get("algorithm")
    if _declared != DIGEST_ALGORITHM:
        raise ValueError(
            f"{EXPECTED_FIXTURE.name} declares algorithm {_declared!r} but this module computes "
            f"{DIGEST_ALGORITHM!r}. A digest set computed by a different method than the one "
            "that verifies it proves nothing.")
    """The EXPECTED digest set for the declared tier.

    GATE 4N-I18, SEC-1. Both sets are tracked and reviewable, and neither is derived from the
    observed side. Tier 1 anchors the SYNTHETIC inventory's values, so ordinary CI exercises
    the comparison machinery end-to-end without a live identifier in the tree; Tier 2 anchors
    the real values. Selecting the set by tier is what keeps the mechanism exercisable after
    the live inventory left the repository — an unexercised control is the defect class this
    gate chain exists to eliminate, so containment must not buy safety with a dead check.
    """
    doc = json.loads(EXPECTED_FIXTURE.read_text(encoding="utf-8"))
    if tier is None:
        import protected_inventory
        tier = protected_inventory.load().tier
    key = "expected_digests" if tier == "TIER_2_PROTECTED" else "expected_digests_synthetic"
    if key not in doc:
        raise KeyError(
            f"the tracked provenance fixture has no {key!r}. A missing expected-digest set is "
            "an error, never a fallback to the other tier's anchors.")
    return doc[key]


ARN_FIELDS = ("partition", "service", "region", "account", "resource")


def split_arn(text: str) -> dict | None:
    parts = str(text).split(":", 5)
    if len(parts) != 6 or parts[0] != "arn":
        return None
    return dict(zip(ARN_FIELDS, parts[1:]))


def compare(method: str, claimed: object, observed: object, record: dict) -> tuple[bool, str]:
    """Run the DECLARED comparison. An unknown method is a failure, never a pass."""
    if method == C_EXACT_STRING:
        return (str(claimed) == str(observed),
                f"exact string {claimed!r} vs {observed!r}")

    if method == C_ARN_COMPONENTWISE:
        a, b = split_arn(claimed), split_arn(observed)
        if not a or not b:
            return False, "one side is not a parseable 6-part ARN"
        diffs = [f"{k}: {a[k]!r} != {b[k]!r}" for k in ARN_FIELDS if a[k] != b[k]]
        return not diffs, "all ARN components equal" if not diffs else f"differs at {diffs}"

    if method == C_ACCOUNT_OF_ARN:
        b = split_arn(observed)
        if b is None:
            return (str(claimed) == str(observed), f"account {claimed!r} vs {observed!r}")
        return b["account"] == str(claimed), f"account {claimed!r} vs ARN account {b['account']!r}"

    if method == C_SHA256_OF_FILE:
        target = REPO_ROOT / str(record["hashed_file"])
        if not target.exists():
            return False, f"hashed_file does not exist: {record['hashed_file']}"
        digest = hashlib.sha256(target.read_bytes()).hexdigest()
        want = str(claimed).removeprefix("sha256:")
        return digest == want, f"sha256({record['hashed_file']}) = {digest[:16]}… vs {want[:16]}…"

    if method == C_DETERMINISTIC_CONSTRUCTION:
        built = record["construct"]()
        ok, why = compare(C_ARN_COMPONENTWISE, claimed, built, record) \
            if str(built).startswith("arn:") else (str(built) == str(claimed),
                                                   f"constructed {built!r} vs {claimed!r}")
        return ok, f"{record['construction_rule']}: {why}"

    if method == C_DIGEST_AGAINST_TRACKED_FIXTURE:
        key = record["expected_digest_key"]
        digests = expected_digests()
        if key not in digests:
            return False, f"no expected digest tracked for {key!r}"
        actual = hashlib.sha256(str(observed).encode()).hexdigest()
        return actual == digests[key], (
            f"sha256(observed)={actual[:16]}… vs tracked expected={digests[key][:16]}…")

    if method == C_SUBSTRING_OF_TEXT:
        return str(claimed) in str(observed), f"{claimed!r} within the field text"

    if method == C_STRUCTURAL_ONLY:
        return False, ("STRUCTURAL_ONLY never certifies a value claim — it exists to be "
                       "named explicitly rather than achieved by omitting a comparison")

    return False, f"unknown comparison method {method!r}"


# --- source access --------------------------------------------------------------------------


def _dig(document: object, path: str):
    node = document
    for part in path.split("."):
        if isinstance(node, list):
            try:
                node = node[int(part)]
                continue
            except (ValueError, IndexError):
                return None, False
        if not isinstance(node, dict) or part not in node:
            return None, False
        node = node[part]
    return node, True


def _block_body(text: str, header: re.Pattern) -> str | None:
    """The brace-matched body of the first block whose header matches."""
    match = header.search(text)
    if not match:
        return None
    start = text.index("{", match.end() - 1) if "{" not in match.group(0) else \
        match.start() + match.group(0).index("{")
    depth, i = 0, start
    while i < len(text):
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return text[start + 1:i]
        i += 1
    return None


def _attr(body: str, name: str) -> tuple[object, bool]:
    """A top-level attribute of a block body, brace-aware for multi-line values."""
    for match in re.finditer(rf'^\s*{re.escape(name)}\s*=\s*(.+)$', body, re.MULTILINE):
        # Only accept an assignment at nesting depth 0 within this body.
        prefix = body[:match.start()]
        if prefix.count("{") != prefix.count("}"):
            continue
        raw = match.group(1).strip()
        if raw.startswith('"') and raw.endswith('"') and len(raw) >= 2:
            raw = raw[1:-1]
        return raw, True
    return None, False


def _hcl_field(text: str, field: str) -> tuple[object, bool]:
    """Resolve a STRUCTURED HCL address, not a bare attribute name.

    THE GATE 4N-I15 LESSON, APPLIED TO ITS REPLACEMENT. My first draft of this function
    matched on the LEAF name only — so `aws_db_parameter_group.this.name` resolved to
    whichever `name =` appeared first anywhere in the file, and two records silently read a
    different resource's attribute than the one they named. Reading the wrong field and
    calling it the named field is the same class of error as accepting a field's mere
    presence: in both cases the address in the record is decorative. Addresses are resolved
    structurally here, and an address that does not resolve is ABSENT rather than guessed.

    Supported address forms:
        local.<name>                     -> locals { <name> = ... }
        <type>.<label>.<attr>            -> resource "<type>" "<label>" { <attr> = ... }
        <block>.<sub>.<attr>             -> nested plain blocks / object attributes
    """
    parts = field.split(".")

    if parts[0] == "local" and len(parts) == 2:
        body = _block_body(text, re.compile(r"locals\s*\{"))
        return _attr(body, parts[1]) if body is not None else (None, False)

    if len(parts) == 3:
        body = _block_body(text, re.compile(
            rf'resource\s+"{re.escape(parts[0])}"\s+"{re.escape(parts[1])}"\s*\{{'))
        if body is not None:
            return _attr(body, parts[2])

    if parts[0] == "provider" and len(parts) >= 3:
        # provider."<source>".<attr> — the lockfile form, whose label contains dots.
        source, attr = ".".join(parts[1:-1]), parts[-1]
        body = _block_body(text, re.compile(rf'provider\s+"{re.escape(source)}"\s*\{{'))
        return _attr(body, attr) if body is not None else (None, False)

    # Generic descent through nested blocks: a { b = { c = ... } } or a { b { c = ... } }
    body = text
    for part in parts[:-1]:
        nested = _block_body(body, re.compile(rf'(^|\n)\s*{re.escape(part)}\s*=?\s*\{{'))
        if nested is None:
            return None, False
        body = nested
    return _attr(body, parts[-1])


def _sha(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


# --- verification -----------------------------------------------------------------------------


def verify(record: dict) -> dict:
    label = record.get("label")
    out = {
        "claim_id": record.get("id"),
        "label": label,
        "normalized_label": label,
        "claimed_type": record.get("claimed_type"),
        "claimed_value": record.get("value"),
        "source_label": label,
        "source_path": record.get("source"),
        "source_sha256": None,
        "field": record.get("field"),
        "observed_field_type": None,
        "observed_field_value": None,
        "comparison_method": record.get("comparison"),
        "comparison_result": None,
        "confidence": record.get("confidence", "unstated"),
        "certifies_production": bool(record.get("certifies_production")),
        "verified": False,
        "reason": "",
    }

    if label not in LABELS:
        out.update(normalized_label=UNKNOWN, reason=f"unknown label {label!r}")
        return out

    if label in (INFERRED, UNKNOWN):
        if not record.get("explanation"):
            out.update(normalized_label=UNKNOWN,
                       reason=f"{label} requires an explanation of the uncertainty")
            return out
        if out["certifies_production"]:
            out.update(normalized_label=UNKNOWN,
                       reason=f"{label} may never certify production")
            return out
        out.update(verified=True, comparison_result="n/a",
                   reason="uncertainty stated explicitly; certifies nothing")
        return out

    source = record.get("source")
    if not source:
        out.update(normalized_label=UNKNOWN, reason=f"{label} claims no source")
        return out
    path = Path(source) if Path(source).is_absolute() else REPO_ROOT / source
    if not path.exists():
        out.update(normalized_label=UNKNOWN,
                   reason=f"{label} names a source that does not exist: {source}")
        return out
    out["source_sha256"] = _sha(path)
    if record.get("source_sha256") and record["source_sha256"] != out["source_sha256"]:
        out.update(normalized_label=UNKNOWN,
                   reason=f"{label} source hash mismatch — the evidence changed")
        return out

    # --- labels whose meaning is structural, not value-bearing ---------------------------
    if label == SYNTHETIC_TEST_FIXTURE:
        if "NON_PRODUCTION_TEST_FIXTURE" not in path.read_text(encoding="utf-8"):
            out.update(normalized_label=UNKNOWN,
                       reason="synthetic label on a source not marked non-production")
            return out
        if out["certifies_production"]:
            out.update(normalized_label=UNKNOWN,
                       reason="a synthetic fixture claimed production certification")
            return out
        out.update(verified=True, comparison_result="n/a",
                   reason="marked non-production; certifies nothing")
        return out

    if label == CI_EQUIVALENT_LOCAL_REPRODUCTION:
        if out["certifies_production"]:
            out.update(normalized_label=UNKNOWN,
                       reason="a local reproduction claimed production certification")
            return out
        claims = _actions_claim_problems(record)
        if claims:
            out.update(normalized_label=UNKNOWN, reason="; ".join(claims))
            return out
        out.update(verified=True, comparison_result="n/a",
                   reason="local reproduction; no affirmative GitHub Actions claim")
        return out

    # --- every remaining label is VALUE-BEARING ------------------------------------------
    claimed_type = record.get("claimed_type")
    if claimed_type not in TYPES:
        out.update(normalized_label=UNKNOWN,
                   reason=f"record declares no valid claimed_type (got {claimed_type!r})")
        return out
    if "value" not in record:
        out.update(normalized_label=UNKNOWN,
                   reason=f"{label} is value-bearing but the record carries no claimed value. "
                          "Presence of a field is not evidence — this is the Gate 4N-I15 defect.")
        return out
    method = record.get("comparison")
    if method not in COMPARISONS:
        out.update(normalized_label=UNKNOWN,
                   reason=f"no valid comparison method declared (got {method!r})")
        return out

    # A tracked-fixture digest comparison reads no field from the source either: the OBSERVED
    # value is what the repository produces today, and the EXPECTED value is a digest in a
    # separately tracked file. Requiring a `field` here would force the observed side back into
    # the same document as the expected side, which is the defect.
    if method == C_DIGEST_AGAINST_TRACKED_FIXTURE and not record.get("field"):
        observed_type = detect_type(record["value"])
        out.update(observed_field_type=observed_type, observed_field_value=record["value"])
        if observed_type not in SUPPORT.get(claimed_type, set()):
            out.update(normalized_label=INFERRED,
                       reason=f"TYPE MISMATCH: a {claimed_type} claim cannot be supported by a "
                              f"{observed_type} value")
            return out
        ok, why = compare(method, record["value"], record["value"], record)
        out["comparison_result"] = why
        if not ok:
            out.update(normalized_label=INFERRED,
                       reason=f"{label} DOWNGRADED: the observed value does not match the "
                              f"tracked expected digest — {why}")
            return out
        out.update(verified=True,
                   reason=f"{claimed_type} claim matches the digest tracked independently "
                          f"in tests/fixtures/expected-provenance-values.json — {why}")
        return out

    # A deterministic construction reads no field: its evidence is the RULE plus its inputs.
    if method == C_DETERMINISTIC_CONSTRUCTION:
        ok, why = compare(method, record["value"], None, record)
        out.update(observed_field_type=T_ABSENT, comparison_result=why)
        if not ok:
            out.update(normalized_label=INFERRED, reason=f"construction did not reproduce: {why}")
            return out
        out.update(verified=True, reason=f"reconstructed deterministically: {why}")
        return out

    field = record.get("field")
    if not field:
        out.update(normalized_label=UNKNOWN, reason=f"{label} names no exact field")
        return out

    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        observed, present = _dig(document, field)
    except (json.JSONDecodeError, UnicodeDecodeError):
        observed, present = _hcl_field(path.read_text(encoding="utf-8", errors="ignore"), field)

    if not present:
        out.update(normalized_label=INFERRED,
                   reason=f"{label} DOWNGRADED: field {field!r} does not exist in {source}")
        return out

    observed_type = detect_type(observed)
    out.update(observed_field_type=observed_type, observed_field_value=observed)

    permitted = SUPPORT.get(claimed_type, set())
    if observed_type not in permitted:
        out.update(
            normalized_label=INFERRED,
            reason=(f"TYPE MISMATCH: a {claimed_type} claim cannot be supported by a "
                    f"{observed_type} field. {source}:{field} holds {observed!r}. "
                    f"Permitted supporting types: {sorted(permitted)}."))
        return out

    ok, why = compare(method, record["value"], observed, record)
    out["comparison_result"] = why
    if not ok:
        out.update(normalized_label=INFERRED,
                   reason=f"{label} DOWNGRADED: comparison failed — {why}")
        return out
    out.update(verified=True, reason=f"{claimed_type} claim supported by {observed_type} "
                                     f"evidence at {source}:{field} — {why}")
    return out


def _actions_claim_problems(record: dict) -> list[str]:
    """Decide whether a local reproduction is claiming an actual CI run — STRUCTURALLY.

    THE HISTORY, AND WHY THE APPROACH CHANGED. Two previous versions tried to read prose:
      v1  `"github actions" in blob and "not" not in blob` — "not" matched inside the KEY
          NAME "note", so a record literally saying "this ran on GitHub Actions" passed.
      v2  sentence splitting with negation markers — the Gate 4N-I15 adversarial lane broke
          it six ways (an unrelated " not " in the same sentence, an unrelated "never", a
          nested dict, a list, a "." between "GitHub" and "Actions", and "GH Actions").

    A third heuristic was drafted for this gate and abandoned. It could not separate
        "GitHub Actions has never run"                        (negated)
    from
        "executed on GitHub Actions and the result is not in dispute"   (affirmative)
    by any window or word-distance rule, because the difference is grammatical rather than
    positional. Shipping v3 of a control that two reviewers have already broken would be
    repeating the mistake with more confidence, not fixing it.

    SO PROSE IS NO LONGER PARSED, AND NO LONGER TRUSTED. The control is a DECLARED BOOLEAN:
    a CI_EQUIVALENT_LOCAL_REPRODUCTION record must carry `github_actions_run` as an explicit
    bool, and it must be False. That is mechanically checkable, cannot be evaded by wording,
    and cannot be satisfied by omission. Commentary in the record is documentation; it is
    not evidence, and this function no longer pretends to adjudicate it.
    """
    problems = []
    if "github_actions_run" not in record:
        problems.append(
            "a CI_EQUIVALENT_LOCAL_REPRODUCTION record must declare `github_actions_run` "
            "as an explicit boolean. Prose is not parsed and is not evidence.")
        return problems
    declared = record["github_actions_run"]
    if not isinstance(declared, bool):
        problems.append(f"`github_actions_run` must be a bool, got {type(declared).__name__}")
    elif declared:
        problems.append(
            "`github_actions_run` is True under a CI_EQUIVALENT_LOCAL_REPRODUCTION label. "
            "A local reproduction is not a CI run; if CI genuinely ran, the record needs a "
            "different label and different evidence.")
    return problems


# --- Phase I: the record set ------------------------------------------------------------------


def records() -> list[dict]:
    # GATE 4N-I18, SEC-1. The OBSERVED side no longer reads a repository path. It comes from
    # the tier-resolved inventory (scripts/protected_inventory.py): the tracked SYNTHETIC
    # fixture under Tier 1, the real inventory under Tier 2 via an explicit external path with
    # a separately-supplied hash. The EXPECTED side remains a digest in the tracked fixture,
    # so the two lineages stay disjoint — which is the whole point of this module.
    import protected_inventory
    loaded = protected_inventory.load()
    inventory = loaded.source_path

    def live(field):
        value, _ = loaded.dig(field)
        return value

    return [
        {"id": "trail_arn", "label": LIVE_READ_EXACT, "source": inventory,
         "field": "trails.0.1", "claimed_type": T_ARN, "value": live("trails.0.1"),
         "expected_digest_key": "trail_arn",
         "comparison": C_DIGEST_AGAINST_TRACKED_FIXTURE, "certifies_production": True,
         "confidence": "observed from the retained inventory; EXPECTED digest from the tracked fixture — two lineages"},
        {"id": "db_arn", "label": LIVE_READ_EXACT, "source": inventory,
         "field": "db.0.1", "claimed_type": T_ARN, "value": live("db.0.1"),
         "expected_digest_key": "db_arn",
         "comparison": C_DIGEST_AGAINST_TRACKED_FIXTURE, "certifies_production": True,
         "confidence": "observed from the retained inventory; EXPECTED digest from the tracked fixture — two lineages"},
        {"id": "state_bucket_name", "label": LIVE_READ_EXACT, "source": inventory,
         "field": "buckets_by_role.tfstate", "claimed_type": T_NAME, "value": live("buckets_by_role.tfstate"),
         "expected_digest_key": "state_bucket_name",
         "comparison": C_DIGEST_AGAINST_TRACKED_FIXTURE, "certifies_production": True,
         "confidence": "observed from the retained inventory; EXPECTED digest from the tracked fixture — two lineages"},
        {"id": "audit_bucket_name", "label": LIVE_READ_EXACT, "source": inventory,
         "field": "buckets_by_role.audit", "claimed_type": T_NAME, "value": live("buckets_by_role.audit"),
         "expected_digest_key": "audit_bucket_name",
         "comparison": C_DIGEST_AGAINST_TRACKED_FIXTURE, "certifies_production": True,
         "confidence": "observed from the retained inventory; EXPECTED digest from the tracked fixture — two lineages"},
        # GATE 4N-I18, ARCH-C1. This row was the Gate 4N-I17 architect lane's CRITICAL finding
        # and the mandatory "common decisive ancestor = YES" answer. It read BOTH sides from
        # the same file and the same field — `value` was live("aliases.0.1") and verify() then
        # re-read that identical field as the observed side under C_EXACT_STRING, so the
        # decisive comparison was str(x) == str(x) and could not fail. Its `claimed_type` was
        # detect_type() OF THE OBSERVED VALUE, so the type-support gate was self-satisfied too,
        # and the production-rows-without-a-comparison guard did not catch it because a
        # self-comparison still yields a non-"n/a" result. Rewriting the inventory to an
        # attacker-chosen KMS key id left this row verified, certifying and CI green.
        #
        # The anchor already existed and was unused: tests/fixtures/expected-provenance-values
        # .json has tracked `secrets_cmk_key_id`. This row now uses it, exactly like its five
        # sibling rows — expected from the tracked fixture, observed from the inventory.
        {"id": "secrets_cmk_key_id", "label": LIVE_READ_EXACT, "source": inventory,
         "field": "aliases.0.1", "claimed_type": T_NAME, "value": live("aliases.0.1"),
         "expected_digest_key": "secrets_cmk_key_id",
         "comparison": C_DIGEST_AGAINST_TRACKED_FIXTURE, "certifies_production": True,
         "confidence": "observed from the retained inventory; EXPECTED digest from the tracked fixture — two lineages"},
        {"id": "lock_table_name", "label": EXTERNAL_OPERATOR_INPUT, "source": inventory,
         "field": "lock_table_name", "claimed_type": T_NAME, "value": live("lock_table_name"),
         "expected_digest_key": "lock_table_name",
         "comparison": C_DIGEST_AGAINST_TRACKED_FIXTURE, "certifies_production": True,
         "confidence": "observed from the retained inventory; EXPECTED digest from the tracked fixture — two lineages"},

        # The two rows the Gate 4N-I10 architect lane caught. The inventory contains no
        # parameter group and no subnet group; these are repository EXPRESSIONS and are both
        # labelled and TYPED as such — an HCL expression is not a live value.
        {"id": "rds_parameter_group", "label": REPOSITORY_EXPRESSION,
         "source": "infra/aws/modules/data_sql/main.tf",
         "field": "aws_db_parameter_group.this.name", "claimed_type": T_HCL_EXPRESSION,
         "value": "${var.name_prefix}-pg-params", "comparison": C_EXACT_STRING,
         "certifies_production": True,
         "confidence": "repository expression; the deployed value is not read here"},
        {"id": "rds_subnet_group", "label": REPOSITORY_EXPRESSION,
         "source": "infra/aws/modules/data_sql/main.tf",
         "field": "local.db_subnet_group_name", "claimed_type": T_HCL_EXPRESSION,
         "value": "coalesce(var.db_subnet_group_name, \"${var.name_prefix}-pg\")",
         "comparison": C_EXACT_STRING, "certifies_production": True,
         "confidence": "repository expression; the deployed value is not read here"},

        # ============================================================================
        # PHASE I — THE REPAIRED ROW.
        #
        # WAS: label DETERMINISTIC_NOT_YET_CREATED, field `_captured_utc`, no value, no
        # comparison, certifies_production True. A capture DATE was standing in as evidence
        # for an ARN, and the checker said verified.
        #
        # NOW: the claim is reconstructed from the AWS managed-policy ARN rule applied to
        # its declared inputs, and compared to the claimed ARN component by component. The
        # boundary does not exist in AWS, so there is no live evidence to read — that is
        # precisely why the evidence must be the RULE plus its inputs, and why the account
        # input is broken out as its own separately-graded claim below rather than being
        # silently absorbed into this one.
        # ============================================================================
        {"id": "boundary_policy_arn", "label": DETERMINISTIC_NOT_YET_CREATED,
         "source": "scripts/signalnest_identity.py",
         "claimed_type": T_ARN, "value": identity.BOUNDARY_POLICY_ARN,
         "expected_digest_key": "boundary_policy_arn",
         "comparison": C_DIGEST_AGAINST_TRACKED_FIXTURE,
         "certifies_production": True,
         "confidence": "OBSERVED = the ARN the identity module constructs today; EXPECTED = the "
                       "digest tracked in tests/fixtures/expected-provenance-values.json",
         "explanation": ("GATE 4N-I17 DEFECT 5. The Gate 4N-I16 repair compared this ARN to a "
                         "deterministic construction built from the SAME three constants "
                         "(PARTITION, ACCOUNT, BOUNDARY_POLICY_NAME), so falsifying the boundary "
                         "name changed both sides together and provenance stayed clean — a "
                         "timestamp certifying an ARN was replaced by an ARN certifying itself. "
                         "The expected side is now a digest in a TRACKED fixture, which does not "
                         "move when the identity module does.")},

        # The account that construction depends on, graded on its own merits against a
        # retained live read rather than assumed.
        {"id": "approved_account_id", "label": LIVE_READ_EXACT, "source": inventory,
         "field": "trails.0.1", "claimed_type": T_ACCOUNT_ID, "value": identity.ACCOUNT,
         "comparison": C_ACCOUNT_OF_ARN, "certifies_production": True,
         "confidence": "the account is read out of a retained live ARN"},

        {"id": "synthetic_anchor", "label": SYNTHETIC_TEST_FIXTURE,
         "source": "tests/fixtures/synthetic-anchor.json", "certifies_production": False},
        {"id": "synthetic_ledger", "label": SYNTHETIC_TEST_FIXTURE,
         "source": "tests/fixtures/synthetic-ledger.json", "certifies_production": False},
        {"id": "empty_home_run", "label": CI_EQUIVALENT_LOCAL_REPRODUCTION,
         "source": "scripts/empty_home_ci.sh", "certifies_production": False,
         "github_actions_run": False,
         "explanation": "local reproduction with an empty HOME, via the committed harness "
                        "scripts/empty_home_ci.sh"},
        # The EXACT selection lives in the lockfile. versions.tf carries only a RANGE
        # (">= 6.55.0, < 6.56.0"), so sourcing an exact version from it would be another
        # presence-standing-in-for-support: the range contains the string but does not
        # assert it. The lockfile is tracked, so this is a real repository pin.
        {"id": "provider_pin", "label": PROVIDER_SOURCE,
         "source": "infra/aws/.terraform.lock.hcl",
         "field": "provider.registry.opentofu.org/hashicorp/aws.version",
         "claimed_type": T_VERSION, "value": "6.55.0", "comparison": C_EXACT_STRING,
         "certifies_production": True,
         "confidence": "exact selection pinned in the tracked lockfile"},
        {"id": "provider_constraint", "label": REPOSITORY_EXPRESSION,
         "source": "infra/aws/versions.tf", "field": "required_providers.aws.version",
         "claimed_type": T_NAME, "value": ">= 6.55.0, < 6.56.0",
         "comparison": C_EXACT_STRING, "certifies_production": True,
         "confidence": "the declared RANGE, stated as a range rather than as a pin"},
        {"id": "identity_centre_instance", "label": UNKNOWN,
         "explanation": "no pre-branch artifact retained the Identity Center instance ARN; "
                        "recovering it needs a live sso-admin:ListInstances call that no "
                        "gate in this chain has authorized"},
    ]


def run() -> dict:
    rows = [verify(r) for r in records()]
    downgraded = [r for r in rows if r["normalized_label"] != r["label"]]
    unsupported = [r for r in rows if not r["verified"]]
    unsafe = [r for r in rows
              if r.get("certifies_production") and r["normalized_label"] in NON_CERTIFYING]
    uncompared = [r for r in rows
                  if r["verified"] and r["certifies_production"]
                  and r["comparison_result"] in (None, "n/a")]
    return {
        "records": len(rows), "rows": rows,
        "downgraded": downgraded, "unsupported": unsupported,
        "weak_labels_authorizing_production": unsafe,
        "production_rows_without_a_comparison": uncompared,
        "clean": not unsupported and not unsafe and not uncompared,
    }


ROW_INVENTORY_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "provenance-row-inventory.json"


class RowCoverageError(RuntimeError):
    """The authored provenance-row inventory is absent, malformed, or does not account
    for every production-certifying row."""


# GATE 4N-I26B. The independently authored FLOOR of rows this mechanism must always guarantee.
# Not derived from records(), not derived from the inventory fixture — either would make it
# agree with whatever it is checking. See row_coverage_report() for why a floor is safe where a
# scope list is not.
REQUIRED_TWO_LINEAGE_ROWS = frozenset({"secrets_cmk_key_id"})


def row_coverage_report() -> dict:
    """COMPLETE two-way accounting of every production-certifying provenance row.

    GATE 4N-I23, BLOCKER 3 (I22 finding F2). The two-lineage guard was parametrized over a
    hand-written list of six ids while seven rows carried DIGEST_AGAINST_TRACKED_FIXTURE. The
    omitted one was `secrets_cmk_key_id` — the row the whole two-lineage mechanism was built
    for. Reverting it to a self-comparison left the suite green and this script printing
    "PROVENANCE: clean".

    The accounting runs in BOTH directions on purpose:
      * every certifying row discovered in records() must appear in exactly one inventory
        group, so a row cannot be quietly dropped from the inventory; and
      * every id the inventory names must exist in records() with the comparison the
        inventory declares, so a row cannot be quietly weakened in the schema.

    Selecting rows *by* their comparison would be the same defect wearing a different hat: a
    reverted row would fall out of the selection and the guard would pass by shrinking.
    """
    if not ROW_INVENTORY_FIXTURE.exists():
        raise RowCoverageError(
            f"the authored provenance-row inventory is absent: {ROW_INVENTORY_FIXTURE}. "
            "Absence must never be read as 'every row is covered'.")
    doc = json.loads(ROW_INVENTORY_FIXTURE.read_text(encoding="utf-8"))

    def _ids(key: str) -> list[str]:
        raw = doc.get(key)
        if not isinstance(raw, list):
            raise RowCoverageError(f"{ROW_INVENTORY_FIXTURE.name}: '{key}' must be a list")
        out = []
        for entry in raw:
            rid = entry.get("id") if isinstance(entry, dict) else entry
            if not isinstance(rid, str) or not rid:
                raise RowCoverageError(f"{ROW_INVENTORY_FIXTURE.name}: malformed entry in '{key}'")
            out.append(rid)
        return out

    two_lineage = _ids("two_lineage_required")
    other = _ids("other_certifying_comparisons")
    declared_other = {e["id"]: e.get("comparison")
                      for e in doc.get("other_certifying_comparisons", [])
                      if isinstance(e, dict)}

    rows = {r["id"]: r for r in records()}
    certifying = {rid for rid, r in rows.items() if r.get("certifies_production")}

    problems: list[str] = []
    for group, name in ((two_lineage, "two_lineage_required"),
                        (other, "other_certifying_comparisons")):
        dupes = sorted({i for i in group if group.count(i) > 1})
        for d in dupes:
            problems.append(f"{d}: duplicated in '{name}'")
    overlap = sorted(set(two_lineage) & set(other))
    for o in overlap:
        problems.append(f"{o}: appears in BOTH inventory groups")

    inventoried = set(two_lineage) | set(other)

    # GATE 4N-I26B, closing I26B-06 (I23's X7, still open at I25 as ADV-Y6).
    #
    # `secrets_cmk_key_id_guarded` was COMPUTED and PRINTED and never appended to problems, so
    # dropping the row from records() AND the inventory gave "guarded: False" immediately
    # followed by "PROVENANCE: clean", exit 0. Every check above derives `certifying` FROM
    # records(), so deleting the row shrinks the denominator and the row stops being missing by
    # ceasing to exist. A control whose scope comes from the thing it checks cannot see a
    # deletion from both sides.
    #
    # REQUIRED_ROWS is a FLOOR, not a scope. The distinction matters and is the reason this is
    # not the hand-authored-list defect: a scope list silently under-covers when something new
    # is added, which is unbounded; a floor can only fail to demand enough, and everything it
    # does demand is checked. It is the minimum this mechanism exists to guarantee, authored
    # independently of records(), and it is asserted in BOTH directions below.
    for rid in sorted(REQUIRED_TWO_LINEAGE_ROWS - set(rows)):
        problems.append(
            f"{rid}: REQUIRED production-certifying row is ABSENT from records(). This is the "
            "row the two-lineage mechanism was built for; its absence is a failure, not a "
            "smaller denominator.")
    for rid in sorted(REQUIRED_TWO_LINEAGE_ROWS - set(two_lineage)):
        problems.append(
            f"{rid}: REQUIRED row is not in the two-lineage inventory group, so nothing holds "
            "its expected and observed sides to independent sources.")

    for rid in sorted(certifying - inventoried):
        problems.append(f"{rid}: certifies production but is in NO inventory group — "
                        "coverage is incomplete and incomplete coverage is a failure")
    for rid in sorted(inventoried - set(rows)):
        problems.append(f"{rid}: named by the inventory but absent from records()")
    for rid in sorted(inventoried & set(rows) - certifying):
        problems.append(f"{rid}: inventoried as production-certifying but the row is not")

    for rid in two_lineage:
        row = rows.get(rid)
        if row is None:
            continue
        if row.get("comparison") != C_DIGEST_AGAINST_TRACKED_FIXTURE:
            problems.append(
                f"{rid}: the inventory requires {C_DIGEST_AGAINST_TRACKED_FIXTURE} but the row "
                f"uses {row.get('comparison')!r} — the expected and observed sides are no "
                "longer two independent lineages")
        if not row.get("expected_digest_key"):
            problems.append(f"{rid}: two-lineage row carries no expected_digest_key, so it has "
                            "no independent expected source")
    for rid, declared in declared_other.items():
        row = rows.get(rid)
        if row is not None and declared and row.get("comparison") != declared:
            problems.append(f"{rid}: inventory declares {declared!r} but the row uses "
                            f"{row.get('comparison')!r}")

    return {
        "inventory_source": str(ROW_INVENTORY_FIXTURE),
        "rows_discovered": len(rows),
        "certifying_rows": sorted(certifying),
        "two_lineage_required": sorted(two_lineage),
        "other_certifying": sorted(other),
        "expected_rows": len(certifying),
        "guarded_rows": len(inventoried & certifying),
        "missing": sorted(certifying - inventoried),
        "duplicate": sorted({i for i in two_lineage + other
                             if (two_lineage + other).count(i) > 1}),
        "unknown": sorted(inventoried - set(rows)),
        "secrets_cmk_key_id_guarded": "secrets_cmk_key_id" in two_lineage,
        "problems": problems,
        "complete": not problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = run()
    coverage = row_coverage_report()
    result["row_coverage"] = coverage
    # The aggregate guard's exit code depends on coverage. A provenance run that compared
    # every row it happened to look at, while silently not looking at the decisive one, is
    # exactly the I22 F2 defect and must not be reported as clean.
    result["clean"] = bool(result["clean"]) and coverage["complete"]
    if args.json:
        printable = {k: v for k, v in result.items()}
        print(json.dumps(printable, indent=2, ensure_ascii=True, default=str))
    else:
        for row in result["rows"]:
            mark = "OK  " if row["verified"] else "FAIL"
            print(f"  {mark} {row['normalized_label']:32s} {row['claim_id']:24s} "
                  f"{row['claimed_type'] or '-':14s} <- {row['observed_field_type'] or '-'}")
        for row in result["unsupported"]:
            print(f"    {row['claim_id']}: {row['reason']}", file=sys.stderr)
        for row in result["weak_labels_authorizing_production"]:
            print(f"    UNSAFE {row['claim_id']}: {row['normalized_label']} cannot certify "
                  "production", file=sys.stderr)
        for row in result["production_rows_without_a_comparison"]:
            print(f"    UNCOMPARED {row['claim_id']}: certifies production with no value "
                  "comparison", file=sys.stderr)
        cov = result["row_coverage"]
        print(f"  ROW COVERAGE {cov['guarded_rows']}/{cov['expected_rows']} certifying rows; "
              f"secrets_cmk_key_id guarded: {cov['secrets_cmk_key_id_guarded']}")
        for problem in cov["problems"]:
            print(f"    COVERAGE {problem}", file=sys.stderr)
        print("PROVENANCE: clean" if result["clean"] else "PROVENANCE: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
