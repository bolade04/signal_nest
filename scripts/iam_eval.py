"""A fail-closed, condition-aware IAM policy evaluator (Gate 4N-I4).

This replaces the Gate 4N-I3 evaluator, which never inspected `Condition`. Five injected
defects passed silently against it — including a permissions-boundary ARN changed to a
value that can never be satisfied, which is precisely the Gate 4N-I2 defect the test
suite existed to prevent.

SCOPE — READ THIS BEFORE TRUSTING A RESULT
This is NOT a general IAM simulator. It implements a defined subset, listed in
SUPPORTED_SEMANTICS, sufficient for the policies this repository generates. Anything
outside that subset raises `UnsupportedPolicyFeature` rather than being ignored: an
evaluator that silently skips what it does not understand is worse than no evaluator,
because it produces confident wrong answers. Callers must let that exception propagate.

Evaluation model (identity-policy only):
  * explicit Deny anywhere wins;
  * otherwise an applicable Allow grants;
  * otherwise implicit deny.
Resource policies, permissions boundaries, SCPs and session policies are NOT modelled —
`effective_permission` reasons about a single identity document only.
"""

from __future__ import annotations

import dataclasses
import enum
import datetime
import fnmatch
import re
from typing import Any, Iterable

__all__ = [
    "Decision",
    "Evaluation",
    "decide",
    "UnsupportedPolicyFeature",
    "SUPPORTED_SEMANTICS",
    "ACTION_CONDITION_KEYS",
    "effect",
    "is_allowed",
    "is_denied",
    "validate_policy",
    "require_explicit_deny",
]


class UnsupportedPolicyFeature(Exception):
    """Raised when the policy uses something this evaluator does not model."""


class Decision(str, enum.Enum):
    """Authorization outcomes, kept DISTINCT on purpose.

    Gate 4N-I5 shipped a suite whose safety assertions were all `not allowed(...)`, which
    IMPLICIT_DENY satisfies. Removing iam:PassRole from the permanent deny — the exact
    4N-H regression — still left 166/166 green. An explicit Deny is the only control that
    survives an added attachment, so the harness must be able to tell the two apart.
    """

    EXPLICIT_ALLOW = "EXPLICIT_ALLOW"
    EXPLICIT_DENY = "EXPLICIT_DENY"
    IMPLICIT_DENY = "IMPLICIT_DENY"
    INVALID_POLICY = "INVALID_POLICY"
    MISSING_CONTEXT = "MISSING_CONTEXT"
    UNSUPPORTED_SEMANTICS = "UNSUPPORTED_SEMANTICS"
    # A decision that depends on a condition key whose runtime population is DISPUTED.
    # Never EXPLICIT_ALLOW, so nothing can be authorized on an unsettled behaviour.
    UNKNOWN_RUNTIME_CONTEXT = "UNKNOWN_RUNTIME_CONTEXT"


@dataclasses.dataclass(frozen=True)
class Evaluation:
    """The full decision record, not just the verdict."""

    decision: Decision
    reason: str
    matching_allow_sids: tuple[str, ...] = ()
    matching_deny_sids: tuple[str, ...] = ()
    nonmatching_candidate_sids: tuple[str, ...] = ()
    failed_conditions: tuple[str, ...] = ()
    invalid_elements: tuple[str, ...] = ()

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.EXPLICIT_ALLOW


SUPPORTED_SEMANTICS = {
    "elements": ["Version", "Statement", "Sid", "Effect", "Action", "NotAction",
                 "Resource", "NotResource", "Condition"],
    "condition_operators": [
        "StringEquals", "StringNotEquals", "StringLike", "StringNotLike",
        "StringEqualsIgnoreCase",
        "ArnEquals", "ArnLike", "ArnNotEquals", "ArnNotLike",
        "Bool", "Null",
        "DateLessThan", "DateLessThanEquals", "DateGreaterThan", "DateGreaterThanEquals",
        "ForAllValues:StringEquals", "ForAnyValue:StringEquals",
        "ForAllValues:StringLike", "ForAnyValue:StringLike",
    ],
    "not_modelled": [
        "resource-based policies", "permissions boundaries", "SCPs", "session policies",
        "Principal / NotPrincipal", "IfExists operator suffix", "policy variables (${...})",
        "IP/VPC condition operators", "numeric operators",
    ],
    "evaluation": "identity policy only; explicit Deny > Allow > implicit deny",
}

_SET_OPERATORS = ("ForAllValues:", "ForAnyValue:")
_NEGATED = ("StringNotEquals", "StringNotLike", "ArnNotEquals", "ArnNotLike")

# Condition keys AWS actually supports per action, for the actions this repository
# conditions. Sourced from the AWS Service Reference. Used to reject a policy that
# conditions an action on a key AWS will never populate — the request context would
# lack the key, the statement would never match, and the grant would be silently dead.

# Action/key pairings whose RUNTIME POPULATION is unproven (Gate 4N-I11 Defect 14).
# Distinct from "unsupported": AWS may document the key for the action while never populating
# it in the request context, and a StringEquals against an absent key evaluates FALSE — a
# grant that is dead exactly when it is needed. Settling one of these needs a policy-simulator
# call this gate chain has never been authorized to make, so the honest encoding is UNKNOWN
# rather than a guess in either direction.
DISPUTED_RUNTIME_CONTEXT = {
    ("iam:DeleteRolePermissionsBoundary", "iam:PermissionsBoundary"):
        "Gate 4N-I7 security lane read the service authorization reference as NOT populating "
        "this key for the Delete action; the lead read it as populated with the currently "
        "attached boundary per the permissions-boundary delegation pattern. Unproven either "
        "way, so critical rollback must not depend on it.",
}

ACTION_CONDITION_KEYS: dict[str, set[str]] = {
    "iam:CreateRole": {"aws:RequestTag/${TagKey}", "aws:TagKeys", "iam:PermissionsBoundary"},
    "iam:PutRolePolicy": {"iam:PermissionsBoundary"},
    "iam:TagRole": {"aws:RequestTag/${TagKey}", "aws:TagKeys"},
    "iam:UntagRole": {"aws:TagKeys"},
    "iam:DeleteRole": set(),
    # Verified against the AWS Service Authorization Reference in Gate 4N-I5:
    # DeleteRolePolicy DOES support iam:PermissionsBoundary. The Gate 4N-I4 table
    # mapped it to the empty set, which would have wrongly reported a correct
    # boundary-conditioned statement as a dead grant.
    "iam:DeleteRolePolicy": {"iam:PermissionsBoundary"},
    "iam:PutRolePermissionsBoundary": {"iam:PermissionsBoundary"},
    # GATE 4N-I11 DEFECT 14. This previously asserted the DISPUTED reading as fact: the
    # table's own docstring says it encodes "what AWS actually supports ... used to reject a
    # policy that conditions an action on a key AWS will never populate", while
    # gen_bootstrap_operator_policy.py says of this exact pairing "neither reading was
    # proven". The dead-grant detector was calibrated to the reading the generator refuses to
    # rely on, so re-adding the condition to a removal statement would have been called
    # healthy. It is now recorded as DISPUTED and handled fail-closed.
}

# Keys that are always available in a request context and therefore never action-specific.
_GLOBAL_KEYS = {
    "aws:CurrentTime", "aws:RequestedRegion", "aws:PrincipalAccount", "aws:PrincipalArn",
    "aws:PrincipalOrgID", "aws:ResourceAccount", "aws:SecureTransport", "aws:SourceArn",
    "aws:SourceAccount", "aws:UserAgent", "aws:TokenIssueTime", "aws:MultiFactorAuthPresent",
}


def _as_list(value: Any) -> list:
    if value is None:
        return []
    return list(value) if isinstance(value, list) else [value]


def _glob(patterns: Iterable[str], value: str) -> bool:
    return any(fnmatch.fnmatchcase(value, p) for p in patterns)


def _base_operator(operator: str) -> str:
    for prefix in _SET_OPERATORS:
        if operator.startswith(prefix):
            return operator[len(prefix):]
    return operator


def _compare_one(operator: str, expected: str, actual: Any) -> bool:
    base = _base_operator(operator)
    if base in ("StringEquals", "ArnEquals"):
        return actual == expected
    if base == "StringEqualsIgnoreCase":
        return isinstance(actual, str) and actual.lower() == str(expected).lower()
    if base in ("StringNotEquals", "ArnNotEquals"):
        return actual != expected
    if base in ("StringLike", "ArnLike"):
        return isinstance(actual, str) and fnmatch.fnmatchcase(actual, str(expected))
    if base in ("StringNotLike", "ArnNotLike"):
        return not (isinstance(actual, str) and fnmatch.fnmatchcase(actual, str(expected)))
    if base == "Bool":
        return str(actual).lower() == str(expected).lower()
    if base in DATE_OPERATORS:
        return _compare_dates(base, actual, expected)
    raise UnsupportedPolicyFeature(f"condition operator not modelled: {operator}")


class MalformedDateValue(UnsupportedPolicyFeature):
    """A Date condition value that is not a parseable RFC 3339 / ISO 8601 instant.

    A subclass of UnsupportedPolicyFeature so it fails CLOSED through every existing path:
    decide() reports UNSUPPORTED_SEMANTICS, which is never EXPLICIT_ALLOW.
    """


DATE_OPERATORS = {
    "DateLessThan": lambda a, b: a < b,
    "DateLessThanEquals": lambda a, b: a <= b,
    "DateGreaterThan": lambda a, b: a > b,
    "DateGreaterThanEquals": lambda a, b: a >= b,
}

# Rejected explicitly rather than by parse failure, so the error names the real problem.
_PLACEHOLDER_MARKERS = ("<", ">", "{", "}", "$", "PLACEHOLDER", "EXPIRY-ISO")


def parse_iam_date(value: object, *, what: str) -> datetime.datetime:
    """Parse an IAM Date value into a timezone-aware UTC instant.

    GATE 4N-I8 DEFECT 4. This was `str(actual) < str(expected)`. Lexicographic comparison of
    timestamps is wrong in general, and here it was catastrophic: the reviewed artifacts
    carried the literal placeholder `<EXPIRY-ISO8601>`, and `<` is ASCII 0x3C — above every
    digit — so EVERY clock compared "less than" it. A request in 2099 evaluated
    EXPLICIT_ALLOW against a policy whose entire purpose was to expire. Five layers agreed it
    was fine, including validate_policy.

    Requirements, all fail-closed:
      - RFC 3339 / ISO 8601 only
      - an explicit timezone is REQUIRED; a naive timestamp is ambiguous, and guessing UTC
        would silently shift every boundary by the offset
      - normalized to UTC so offsets compare correctly
      - placeholders and non-dates rejected by name
    """
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            raise MalformedDateValue(f"{what}: timezone-naive datetime is not accepted")
        return value.astimezone(datetime.timezone.utc)
    if not isinstance(value, str):
        raise MalformedDateValue(f"{what}: expected an ISO-8601 string, got {type(value).__name__}")

    text = value.strip()
    if not text:
        raise MalformedDateValue(f"{what}: empty date value")
    for marker in _PLACEHOLDER_MARKERS:
        if marker in text.upper():
            raise MalformedDateValue(
                f"{what}: {value!r} is a PLACEHOLDER, not a timestamp. This is the exact "
                "Gate 4N-I7 defect: an unstamped policy compared as valid and never expired."
            )
    candidate = text[:-1] + "+00:00" if text.endswith(("Z", "z")) else text
    try:
        parsed = datetime.datetime.fromisoformat(candidate)
    except ValueError as exc:
        raise MalformedDateValue(f"{what}: {value!r} is not a valid RFC 3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise MalformedDateValue(
            f"{what}: {value!r} has no timezone. IAM values are UTC-qualified; accepting a "
            "naive value would silently shift the comparison by the local offset."
        )
    return parsed.astimezone(datetime.timezone.utc)


def _compare_dates(base: str, actual: object, expected: object) -> bool:
    """Compare parsed INSTANTS. Lexicographic comparison is prohibited here."""
    left = parse_iam_date(actual, what=f"{base} request value")
    right = parse_iam_date(expected, what=f"{base} policy value")
    return DATE_OPERATORS[base](left, right)


def _evaluate_condition(condition: dict, context: dict) -> bool:
    """Return True only if every condition block is satisfied by `context`.

    Fails CLOSED: a key absent from the context makes the statement not match, exactly as
    AWS behaves for non-IfExists operators. `Null` is handled separately because it is a
    presence test rather than a value test.
    """
    for operator, mapping in condition.items():
        if operator.endswith("IfExists"):
            raise UnsupportedPolicyFeature(f"IfExists is not modelled: {operator}")
        if operator not in SUPPORTED_SEMANTICS["condition_operators"] and operator != "Null":
            raise UnsupportedPolicyFeature(f"condition operator not modelled: {operator}")
        if not isinstance(mapping, dict):
            raise UnsupportedPolicyFeature(f"malformed condition block for {operator}")

        for key, expected in mapping.items():
            if operator == "Null":
                present = key in context and context[key] is not None
                want_absent = str(expected).lower() == "true"
                if present == want_absent:
                    return False
                continue

            absent = key not in context or context[key] is None
            expected_values = _as_list(expected)

            # Set operators are NOT symmetric on an absent or empty multivalued key.
            # AWS: ForAllValues is VACUOUSLY TRUE when there are no values to check;
            # ForAnyValue is false. Treating both as "fail closed" — the Gate 4N-I4
            # defect — makes a ForAllValues-conditioned Deny silently stop applying.
            # Presence, where it matters, is required by a separate Null condition.
            if operator.startswith("ForAllValues:"):
                actuals = [] if absent else _as_list(context[key])
                if not all(any(_compare_one(operator, e, a) for e in expected_values) for a in actuals):
                    return False
                continue
            if operator.startswith("ForAnyValue:"):
                actuals = [] if absent else _as_list(context[key])
                if not any(any(_compare_one(operator, e, a) for e in expected_values) for a in actuals):
                    return False
                continue

            if absent:
                return False  # single-valued, non-IfExists: fail closed on missing context
            actual = context[key]

            if _base_operator(operator) in _NEGATED:
                # A negated operator must hold against EVERY listed value.
                if not all(_compare_one(operator, e, actual) for e in expected_values):
                    return False
            else:
                if not any(_compare_one(operator, e, actual) for e in expected_values):
                    return False
    return True


def _statement_applies_explained(stmt: dict, action: str, resource: str,
                                 context: dict) -> tuple[bool, str]:
    """Like _statement_applies, but reports WHY a candidate statement did not match."""
    if "NotAction" in stmt:
        if _glob(_as_list(stmt["NotAction"]), action):
            return False, "excluded by NotAction"
    elif not _glob(_as_list(stmt.get("Action")), action):
        return False, ""
    if "NotResource" in stmt:
        if _glob(_as_list(stmt["NotResource"]), resource):
            return False, "excluded by NotResource"
    elif not _glob(_as_list(stmt.get("Resource")), resource):
        return False, "resource did not match"
    condition = stmt.get("Condition")
    if condition:
        missing = _missing_context_keys(condition, context)
        if not _evaluate_condition(condition, context):
            if missing:
                return False, f"MISSING_CONTEXT: {sorted(missing)}"
            return False, f"condition not satisfied: {sorted(condition)}"
    return True, ""


def _missing_context_keys(condition: dict, context: dict) -> set[str]:
    """Keys a non-Null condition needs that the request context does not supply.

    Reported separately from a value mismatch: "the key was absent" and "the value was
    wrong" are different diagnoses, and conflating them hides a context-modelling bug.
    ForAllValues is excluded because an absent key is VACUOUSLY SATISFIED for it.
    """
    missing: set[str] = set()
    for operator, mapping in condition.items():
        if operator == "Null" or operator.startswith("ForAllValues:"):
            continue
        if not isinstance(mapping, dict):
            continue
        for key in mapping:
            if key not in context or context[key] is None:
                missing.add(key)
    return missing


def _statement_applies(stmt: dict, action: str, resource: str, context: dict) -> bool:
    unknown = set(stmt) - set(SUPPORTED_SEMANTICS["elements"])
    if unknown:
        raise UnsupportedPolicyFeature(f"policy element(s) not modelled: {sorted(unknown)}")
    if "Action" in stmt and "NotAction" in stmt:
        raise UnsupportedPolicyFeature("statement has both Action and NotAction")
    if "Resource" in stmt and "NotResource" in stmt:
        raise UnsupportedPolicyFeature("statement has both Resource and NotResource")

    if "NotAction" in stmt:
        if _glob(_as_list(stmt["NotAction"]), action):
            return False
    elif not _glob(_as_list(stmt.get("Action")), action):
        return False

    if "NotResource" in stmt:
        if _glob(_as_list(stmt["NotResource"]), resource):
            return False
    elif not _glob(_as_list(stmt.get("Resource")), resource):
        return False

    condition = stmt.get("Condition")
    if condition:
        return _evaluate_condition(condition, context)
    return True


def _assert_modelled(policy: dict) -> None:
    """Reject the WHOLE policy before evaluating any of it.

    Gate 4N-I4 evaluated statement by statement and returned on the first matching
    Deny, so an unmodelled element in a LATER statement never raised — the fail-closed
    guarantee depended on statement order. Validation is now a separate, complete pass.
    """
    for stmt in policy["Statement"]:
        unknown = set(stmt) - set(SUPPORTED_SEMANTICS["elements"])
        if unknown:
            raise UnsupportedPolicyFeature(f"policy element(s) not modelled: {sorted(unknown)}")
        if stmt.get("Effect") not in ("Allow", "Deny"):
            raise UnsupportedPolicyFeature(f"unknown Effect: {stmt.get('Effect')!r}")
        if "Action" in stmt and "NotAction" in stmt:
            raise UnsupportedPolicyFeature("statement has both Action and NotAction")
        if "Resource" in stmt and "NotResource" in stmt:
            raise UnsupportedPolicyFeature("statement has both Resource and NotResource")
        for operator, mapping in (stmt.get("Condition") or {}).items():
            if operator.endswith("IfExists"):
                raise UnsupportedPolicyFeature(f"IfExists is not modelled: {operator}")
            if operator not in SUPPORTED_SEMANTICS["condition_operators"] and operator != "Null":
                raise UnsupportedPolicyFeature(f"condition operator not modelled: {operator}")
            if not isinstance(mapping, dict):
                raise UnsupportedPolicyFeature(f"malformed condition block for {operator}")


def decide(policy: dict, action: str, resource: str, context: dict | None = None) -> Evaluation:
    """Full authorization record for (action, resource) under `context`.

    Order-independent: every statement is validated first, then ALL statements are
    evaluated. The result is invariant under any permutation of Statement.
    """
    try:
        _assert_modelled(policy)
    except UnsupportedPolicyFeature as exc:
        text = str(exc)
        kind = (Decision.UNSUPPORTED_SEMANTICS
                if "not modelled" in text or "IfExists" in text
                else Decision.INVALID_POLICY)
        return Evaluation(kind, text, invalid_elements=(text,))

    ctx = dict(context or {})
    allow_sids: list[str] = []
    deny_sids: list[str] = []
    nonmatching: list[str] = []
    failed: list[str] = []

    for index, stmt in enumerate(policy["Statement"]):
        sid = stmt.get("Sid") or f"<statement {index}>"
        try:
            applies, why = _statement_applies_explained(stmt, action, resource, ctx)
        except UnsupportedPolicyFeature as exc:
            # FAIL CLOSED. A malformed or placeholder Date value raises here, and the whole
            # evaluation becomes UNSUPPORTED_SEMANTICS rather than escaping as a traceback or
            # — far worse — being silently skipped. UNSUPPORTED_SEMANTICS is never
            # EXPLICIT_ALLOW, so nothing can be authorised on a condition we cannot evaluate.
            return Evaluation(Decision.UNSUPPORTED_SEMANTICS, f"{sid}: {exc}",
                              invalid_elements=(f"{sid}: {exc}",))
        if not applies:
            nonmatching.append(sid)
            if why:
                failed.append(f"{sid}: {why}")
            continue
        (deny_sids if stmt["Effect"] == "Deny" else allow_sids).append(sid)

    if deny_sids:
        decision, reason = Decision.EXPLICIT_DENY, f"explicit Deny in {deny_sids}"
    elif allow_sids:
        decision, reason = Decision.EXPLICIT_ALLOW, f"explicit Allow in {allow_sids}"
    elif any("MISSING_CONTEXT" in f for f in failed):
        decision = Decision.MISSING_CONTEXT
        reason = "a candidate statement required context the request did not supply"
    else:
        decision, reason = Decision.IMPLICIT_DENY, "no statement matched"

    return Evaluation(decision, reason, tuple(allow_sids), tuple(deny_sids),
                      tuple(nonmatching), tuple(failed))


def effect(policy: dict, action: str, resource: str, context: dict | None = None) -> str:
    """Legacy string API. Prefer `decide()`, which distinguishes explicit from implicit.

    NOTE: 'Deny' here collapses EXPLICIT_DENY and nothing else; IMPLICIT_DENY returns
    'ImplicitDeny'. Callers asserting a SAFETY deny must use decide() — that conflation
    is what made the Gate 4N-I5 suite vacuous.
    """
    result = decide(policy, action, resource, context)
    if result.decision in (Decision.INVALID_POLICY, Decision.UNSUPPORTED_SEMANTICS):
        raise UnsupportedPolicyFeature(result.reason)
    return {Decision.EXPLICIT_DENY: "Deny",
            Decision.EXPLICIT_ALLOW: "Allow"}.get(result.decision, "ImplicitDeny")


def require_explicit_deny(policy: dict, action: str, resource: str,
                          context: dict | None = None, sid: str | None = None) -> Evaluation:
    """Assert an EXPLICIT Deny — never satisfied by IMPLICIT_DENY.

    This is the helper safety tests must use. `not is_allowed(...)` passes on implicit
    denial and therefore proves nothing about the presence of a safety control.
    """
    result = decide(policy, action, resource, context)
    if result.decision is not Decision.EXPLICIT_DENY:
        raise AssertionError(
            f"expected EXPLICIT_DENY for {action} on {resource}, got {result.decision.value} "
            f"({result.reason}). An implicit deny is not a safety control."
        )
    if sid is not None and sid not in result.matching_deny_sids:
        raise AssertionError(
            f"expected Deny Sid {sid!r} to match {action} on {resource}; "
            f"matched {list(result.matching_deny_sids)}"
        )
    return result


def is_allowed(policy: dict, action: str, resource: str, context: dict | None = None) -> bool:
    return effect(policy, action, resource, context) == "Allow"


def is_denied(policy: dict, action: str, resource: str, context: dict | None = None) -> bool:
    return not is_allowed(policy, action, resource, context)


def disputed_pairings(policy: dict) -> list[str]:
    """Statements relying on an action/key pairing whose runtime population is UNPROVEN.

    Reported separately from "unsupported": an unsupported key is a definite dead grant, a
    disputed one is an unsettled risk. Critical execution must not depend on either.
    """
    out = []
    for index, statement in enumerate(policy.get("Statement", [])):
        sid = statement.get("Sid") or f"<statement {index}>"
        if statement.get("Effect") != "Allow":
            continue
        keys = {k for pairs in (statement.get("Condition") or {}).values() for k in pairs}
        for action in _as_list(statement.get("Action")):
            for key in keys:
                why = DISPUTED_RUNTIME_CONTEXT.get((action, key))
                if why:
                    out.append(f"{sid}: {action} conditioned on {key} — DISPUTED runtime "
                               f"population. {why}")
    return out


def validate_policy(policy: dict, kind: str = "identity") -> list[str]:
    """Return structural problems that would make a statement silently dead or unsafe.

    Catches the class of defect that reached production in Gate 4N-I2: an action
    conditioned on a key AWS never populates for it, so the Allow can never match.

    `kind` distinguishes an IDENTITY policy from a permissions BOUNDARY. A bare wildcard
    Allow is a real defect in an identity policy and the correct idiom in a boundary,
    where it is the ceiling the Denies carve out of and grants nothing on its own.
    """
    if kind not in ("identity", "boundary"):
        raise ValueError(f"unknown policy kind: {kind!r}")
    problems: list[str] = []
    if policy.get("Version") != "2012-10-17":
        problems.append(f"unexpected policy Version: {policy.get('Version')!r}")

    for stmt in policy.get("Statement", []):
        sid = stmt.get("Sid", "<no-sid>")
        if stmt.get("Effect") not in ("Allow", "Deny"):
            problems.append(f"{sid}: invalid Effect {stmt.get('Effect')!r}")
        if kind == "identity" and "*" in _as_list(stmt.get("Action")) and stmt.get("Effect") == "Allow":
            problems.append(f"{sid}: bare wildcard Action in an Allow")

        condition = stmt.get("Condition") or {}
        for operator, mapping in condition.items():
            if operator.endswith("IfExists"):
                problems.append(f"{sid}: IfExists operator {operator} weakens the condition")
            if not isinstance(mapping, dict):
                problems.append(f"{sid}: malformed condition block for {operator}")
                continue
            for key in mapping:
                if key in _GLOBAL_KEYS:
                    continue
                for act in _as_list(stmt.get("Action")):
                    supported = ACTION_CONDITION_KEYS.get(act)
                    if supported is None:
                        continue  # unknown action: no claim either way
                    if key not in supported:
                        problems.append(
                            f"{sid}: {act} does not support condition key {key} — the key is "
                            "absent from the request context, so this statement can never match"
                        )
        # A Deny that carries an expiry stops protecting when it lapses.
        if stmt.get("Effect") == "Deny" and any(
            op.startswith("Date") for op in condition
        ):
            problems.append(f"{sid}: Deny carries a date condition and would stop applying")
    return problems
