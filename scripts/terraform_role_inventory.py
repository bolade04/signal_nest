#!/usr/bin/env python3
"""Role inventory derived from the ACTUAL Terraform declarations (Gate 4N-I17, Defect 6).

THE DEFECT. `gen_operator_policies.INLINE_POLICY_ROLE_ARNS` was built from
`identity.ALL_ROLE_NAMES` — all EIGHT repository-managed roles — and used as the resource scope
for the Stage-A inline-policy write grant. The composition declares inline policies for SEVEN.
The odd one out is `migration-task`, whose own module comment reads:

    # The two application task roles that hold the S3 workload policy. The
    # migration role is deliberately absent (empty role, no policy).

So the write grant reached a role the design documents as deliberately empty. Worse, the grant was
invisible to every check: the allow-model exemption proof probes ARNs OUTSIDE the declared set, and
the containment test used the policy's own Resource list as its definition of "the declared roles".
Allow, deny-fence and test expectation all moved together, so appending an arbitrary ARN to that
list left the entire suite and nine guard scripts green.

A parser that reads the .tf declarations already existed — `putrolepolicy_classification.
declared_inline_policy_resources()` — and was never joined to the grant scope. This module closes
that gap by resolving each declaration to the ROLE it actually targets.

THREE SETS, DERIVED FROM THREE DIFFERENT PLACES (Phase S):
    DECLARED_ROLE_SET            every `resource "aws_iam_role"` in the composition
    EXPECTED_WRITABLE_ROLE_SET   roles an `aws_iam_role_policy` actually binds to
    GENERATED_WRITABLE_ROLE_SET  what the policy generator emits  (supplied by the caller)

The first two are parsed here from HCL. The third is the observed value. Reconciling them is the
point: the expected set must NOT be derivable from the generated one, or widening the grant would
widen its own expectation.

Usage:
    python3 scripts/terraform_role_inventory.py [--json]
Exit: 0 iff declared and writable sets parse and the generated scope matches the expected set.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import signalnest_identity as identity  # noqa: E402

INFRA = REPO_ROOT / "infra" / "aws"

_ROLE_RESOURCE = re.compile(r'resource\s+"aws_iam_role"\s+"([A-Za-z0-9_]+)"\s*\{')
_ROLE_POLICY = re.compile(r'resource\s+"aws_iam_role_policy"\s+"([A-Za-z0-9_]+)"\s*\{')
_ROLE_REF = re.compile(r'aws_iam_role\.([A-Za-z0-9_]+)')
_NAME_ATTR = re.compile(r'^\s*name\s*=\s*"([^"]+)"', re.MULTILINE)


def _block(text: str, start: int) -> str:
    """Brace-matched body starting at the '{' at or after `start`."""
    i = text.index("{", start)
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j]
    return ""


def _tf_files() -> list[Path]:
    return sorted(p for p in INFRA.rglob("*.tf") if ".terraform" not in p.parts)


def declared_roles() -> dict:
    """Every aws_iam_role resource, mapped to the concrete role NAME it creates.

    The name attribute is an interpolation like "${var.name_prefix}-api-task"; the prefix is
    resolved from the single identity source so the result is comparable to real ARNs.
    """
    out = {}
    for path in _tf_files():
        text = path.read_text(encoding="utf-8")
        for match in _ROLE_RESOURCE.finditer(text):
            label = match.group(1)
            body = _block(text, match.end() - 1)
            name_match = _NAME_ATTR.search(body)
            resolved = None
            if name_match:
                resolved = name_match.group(1).replace("${var.name_prefix}", identity.PREFIX)
            out[label] = {"terraform_label": label,
                          "module": str(path.relative_to(REPO_ROOT)),
                          "role_name": resolved}
    return out


def _locals_maps(text: str) -> dict:
    """Resolve `local.<name> = { k = aws_iam_role.X.id, ... }` maps used by for_each."""
    maps = {}
    for match in re.finditer(r'^\s*([A-Za-z0-9_]+)\s*=\s*\{', text, re.MULTILINE):
        label = match.group(1)
        body = _block(text, match.end() - 1)
        refs = _ROLE_REF.findall(body)
        if refs:
            maps[label] = refs
    return maps


def writable_roles() -> dict:
    """Roles that an aws_iam_role_policy actually binds to.

    Handles both the direct form (`role = aws_iam_role.X.id`) and the for_each form
    (`for_each = local.M` with `role = each.value`), because the second is exactly where
    migration-task is EXCLUDED and a naive parser would miss the exclusion.
    """
    out = {}
    for path in _tf_files():
        text = path.read_text(encoding="utf-8")
        maps = _locals_maps(text)
        for match in _ROLE_POLICY.finditer(text):
            label = match.group(1)
            body = _block(text, match.end() - 1)
            targets = []
            for_each = re.search(r'for_each\s*=\s*local\.([A-Za-z0-9_]+)', body)
            if for_each and re.search(r'role\s*=\s*each\.value', body):
                targets = maps.get(for_each.group(1), [])
            else:
                direct = re.search(r'role\s*=\s*aws_iam_role\.([A-Za-z0-9_]+)', body)
                if direct:
                    targets = [direct.group(1)]
            for target in targets:
                out.setdefault(target, []).append(
                    {"policy_label": label, "module": str(path.relative_to(REPO_ROOT))})
    return out


def role_arns(labels) -> list:
    declared = declared_roles()
    arns = []
    for label in sorted(labels):
        name = declared.get(label, {}).get("role_name")
        if name:
            arns.append(identity.iam_role_arn(name))
    return sorted(arns)



# --- GATE 4N-I20, ARCH-H1/ADV-C + ARCH-H2 ---------------------------------------------------
#
# THE TWO DEFECTS, and why one fix closes both.
#
# ARCH-H1/ADV-C: `gen_operator_policies.INLINE_POLICY_ROLE_ARNS` is literally
# `_tf_roles.role_arns(_tf_roles.writable_roles())`, so the "three independent sets" the widening
# suite claims — EXPECTED (authored fixture), DECLARED (.tf parse), GENERATED (policy) — were
# only two: GENERATED was DECLARED, computed by calling it.
#
# ARCH-H2: `main()` then passed that same constant into `reconcile()` as the GENERATED side,
# while `reconcile()` computed its EXPECTED side from the identical expression. `is_expected`
# and `is_generated` were therefore equal for every role, and OVER_GRANTED / UNDER_GRANTED were
# unreachable — the classification engine could not emit either of the two findings it exists to
# emit.
#
# THE FIX. The GENERATED side is now read back out of the EMITTED POLICY DOCUMENT. That is a
# genuinely different derivation: the constant is assembled into statements (grouped by action,
# paired with conditions, rendered) and then parsed back out by matching on the inline-policy
# lifecycle ACTIONS rather than on a Sid or a position. A statement-assembly bug, a Sid rename,
# a mis-scoped resource list or a grant that reaches the document by another path all move this
# set without moving the .tf parse — which is exactly the divergence the reconciler exists to
# catch and previously could not.

# The actions that constitute writable inline-policy lifecycle authority over a role. Matching
# on ACTIONS rather than on a Sid means a renamed or newly added statement carrying the same
# authority is still found.
INLINE_POLICY_LIFECYCLE_ACTIONS = frozenset({
    "iam:PutRolePolicy",
    "iam:DeleteRolePolicy",
})


def generated_writable_arns_from_policy(document=None) -> list[str]:
    """The GENERATED set, read back out of the emitted policy rather than from the constant."""
    if document is None:
        import expiry_authorization as _ea
        import gen_operator_policies as _gen

        document = _gen.bootstrap_temp_policy(_ea.ACTIVE_EXPIRY_UTC)

    found: list[str] = []
    for statement in document.get("Statement", []):
        if statement.get("Effect") != "Allow":
            continue
        actions = statement.get("Action") or []
        actions = [actions] if isinstance(actions, str) else list(actions)
        if not (INLINE_POLICY_LIFECYCLE_ACTIONS & set(actions)):
            continue
        resources = statement.get("Resource") or []
        resources = [resources] if isinstance(resources, str) else list(resources)
        # GATE 4N-I23: keep DUPLICATES. Collapsing to a set here would hide a resource list
        # that names the same role twice, and reconcile() can no longer report what it cannot
        # see. Callers that want uniqueness can take a set of the result.
        found.extend(r for r in resources if ":role/" in r or "*" in r)
    return sorted(found)


REQUIREMENT_FIXTURE = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / \
    "expected-writable-roles.json"

_ARN_RE = re.compile(r"^arn:aws:iam::\d{12}:role/[A-Za-z0-9+=,.@_-]+$")


class RequirementError(RuntimeError):
    """The authored writable-role requirement is missing, malformed, or ambiguous."""


def required_writable_scope() -> dict:
    """The INDEPENDENTLY AUTHORED requirement for which roles may carry an inline policy.

    GATE 4N-I23, BLOCKER 2. Until now `reconcile()` built its expected side from
    `role_arns(writable_roles())` — and `gen_operator_policies.INLINE_POLICY_ROLE_ARNS`,
    whose Resource list the observed side reads back, is *that same expression*. Both sides
    therefore descended from `writable_roles()`. Reading the value back out of the emitted
    policy changed its REPRESENTATION and not its LINEAGE, so any error in `writable_roles()`
    moved both sides together and OVER_GRANTED was unreachable for exactly the defect class
    this module was written to catch: poison `writable_roles()` to call migration-task
    writable and the generated policy grants it while reconcile() reports CORRECTLY_WRITABLE
    and clean.

    This fixture is authored from the architecture requirement. It is not parsed from any
    .tf file and not read from any generated policy.

    INDEPENDENCE, STATED PER COMPONENT — GATE 4N-I26B, closing I26B-09 (I25's AWS-F1). The
    earlier wording here said flatly that the fixture "shares no ancestor with the observed
    side". That is true for the property this control exists to decide — WHICH ROLES ARE
    WRITABLE, i.e. set MEMBERSHIP, which the fixture asserts and the emitted policy is read back
    for. It is FALSE for the ACCOUNT component of the ARNs: both sides reach
    `signalnest_identity.ACCOUNT` through `iam_role_arn()`, so a wrong account id would appear
    identically on both sides and this comparison could not see it.

    That residual is not fail-open — the account is carried on an independent limb by
    scripts/provenance.py, whose two-lineage rows compare it against a separately tracked
    fixture. But an unqualified independence claim is the kind of sentence a later reader trusts
    instead of re-deriving, so it is qualified here rather than left to be discovered again.

    The .tf parse (`writable_roles()`) is retained as a THIRD cross-check, reported separately —
    it is no longer the expectation.
    """
    if not REQUIREMENT_FIXTURE.exists():
        raise RequirementError(
            f"the authored writable-role requirement is absent: {REQUIREMENT_FIXTURE}. "
            "Absence must never be read as 'nothing is required'.")
    doc = json.loads(REQUIREMENT_FIXTURE.read_text(encoding="utf-8"))

    def _suffixes(key: str) -> list[str]:
        raw = doc.get(key)
        if not isinstance(raw, list) or not raw:
            raise RequirementError(f"{REQUIREMENT_FIXTURE.name}: '{key}' must be a non-empty list")
        out = []
        for entry in raw:
            suffix = entry.get("suffix") if isinstance(entry, dict) else entry
            if not isinstance(suffix, str) or not suffix:
                raise RequirementError(f"{REQUIREMENT_FIXTURE.name}: malformed entry in '{key}'")
            out.append(suffix)
        return out

    writable = _suffixes("role_name_suffixes_writable")
    never = _suffixes("role_name_suffixes_never_writable")

    # GATE 4N-I24C, finding I24C-04. EXACT identities, not just spellings. A suffix rule
    # cannot distinguish "<prefix>-api-task" from "<prefix>-evil-api-task", and it cannot
    # notice a role the parser failed to discover at all.
    writable_names = list(doc.get("writable_role_names") or [])
    never_names = list(doc.get("never_writable_role_names") or [])
    if not writable_names:
        raise RequirementError(
            f"{REQUIREMENT_FIXTURE.name}: 'writable_role_names' must enumerate the EXACT role "
            "identities that may be written. Suffixes alone let a role name itself into scope.")
    overlap_names = sorted(set(writable_names) & set(never_names))
    if overlap_names:
        raise RequirementError(f"role declared BOTH writable and never-writable: {overlap_names}")

    for group, name in ((writable, "writable"), (never, "never_writable")):
        dupes = sorted({s for s in group if group.count(s) > 1})
        if dupes:
            raise RequirementError(f"duplicate {name} suffixes: {dupes}")
    overlap = sorted(set(writable) & set(never))
    if overlap:
        raise RequirementError(
            f"suffix declared BOTH writable and never-writable: {overlap}. "
            "An ambiguous requirement cannot adjudicate anything.")
    return {"writable_suffixes": writable, "never_writable_suffixes": never,
            "writable_role_names": writable_names, "never_writable_role_names": never_names,
            "source": str(REQUIREMENT_FIXTURE)}


def classify_role_name(name: str, scope: dict | None = None) -> str:
    """REQUIRED_WRITABLE / REQUIRED_EMPTY / UNKNOWN, from the authored requirement alone.

    UNKNOWN is a FINDING, never a pass: a role the requirement does not mention has not been
    reviewed, and an unreviewed role must not be silently treated as either.
    """
    scope = scope or required_writable_scope()
    # GATE 4N-I24C: EXACT identity decides first and decides alone. The suffix lists remain as
    # a documentation-grade cross-check; they never admit a name the identity list omits.
    if name in scope.get("never_writable_role_names", []):
        return "REQUIRED_EMPTY"
    if name in scope.get("writable_role_names", []):
        return "REQUIRED_WRITABLE"
    if scope.get("writable_role_names"):
        return "UNKNOWN"
    writable = [s for s in scope["writable_suffixes"] if name.endswith(s)]
    never = [s for s in scope["never_writable_suffixes"] if name.endswith(s)]
    if writable and never:
        return "UNKNOWN"
    if writable:
        return "REQUIRED_WRITABLE"
    if never:
        return "REQUIRED_EMPTY"
    return "UNKNOWN"


def required_writable_arns(role_names=None, scope: dict | None = None) -> set:
    """ARNs the AUTHORED requirement says may be written, for the given role names."""
    scope = scope or required_writable_scope()
    # GATE 4N-I24C, finding I24C-04 claim B. The DOMAIN must come from the requirement, not
    # from the .tf parse. Enumerating `declared_roles()` meant a role the parser FAILED TO
    # DISCOVER dropped out of the expected side too, so it vanished from both sides with zero
    # problems — fail-OPEN, needing no attacker, only a parser that misses a form.
    authored = scope.get("writable_role_names")
    if authored:
        return {identity.iam_role_arn(n) for n in authored}
    if role_names is None:
        role_names = [i["role_name"] for i in declared_roles().values() if i.get("role_name")]
    return {identity.iam_role_arn(n) for n in role_names
            if classify_role_name(n, scope) == "REQUIRED_WRITABLE"}


def assert_expected_lineage(source_override: str | None = None) -> None:
    """Structural self-check: reconcile() must build its expected side from the AUTHORED
    requirement, not from the .tf parse or the emitted policy.

    GATE 4N-I23, closing the I22 AWS-lane LOW finding. On an unpoisoned repository the
    authored requirement and the .tf parse AGREE, so rewiring the expected side back to
    `role_arns(writable_roles())` left this guard exiting 0 — the standalone script was blind
    to precisely the regression it exists to prevent, and only the pytest suite caught it. A
    guard that cannot fail on the defect it names is documentation.

    The check is AST-based, not substring: a mention in a comment or a docstring is not a
    call, and the I22 F5 finding was a wiring test defeated by a bare `in` test.
    """
    import ast as _ast

    source = source_override if source_override is not None else \
        Path(__file__).read_text(encoding="utf-8")
    tree = _ast.parse(source)
    fn = next((n for n in _ast.walk(tree)
               if isinstance(n, _ast.FunctionDef) and n.name == "reconcile"), None)
    if fn is None:
        raise RequirementError("reconcile() is absent; the lineage cannot be verified")

    forbidden = {"role_arns", "generated_writable_arns_from_policy"}

    # GATE 4N-I24C, finding I24C-04 (adversarial X5). This walked ONLY the `expected_arns`
    # assignment inside reconcile(), so relocating the coupling one function away —
    # `required_writable_arns()` returning `set(role_arns(writable_roles()))` — left this
    # check PASSING and the standalone guard exiting 0. Follow the callees reachable from the
    # expected side, so the coupling cannot simply move outward.
    module = _ast.parse(source)
    defs = {n.name: n for n in _ast.walk(module) if isinstance(n, _ast.FunctionDef)}

    def _calls(node):
        out = set()
        for c in _ast.walk(node):
            if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Name):
                out.add(c.func.id)
        return out

    def _reachable(start, seen=None):
        seen = seen if seen is not None else set()
        for name in _calls(defs[start]) if start in defs else set():
            if name in seen:
                continue
            seen.add(name)
            if name in defs:
                _reachable(name, seen)
        return seen

    for helper in ("required_writable_arns", "required_writable_scope", "classify_role_name"):
        if helper not in defs:
            continue
        bad = _reachable(helper) & forbidden
        if bad:
            raise RequirementError(
                f"{helper}() reaches {sorted(bad)} on the EXPECTED side. The coupling was "
                "relocated one function away from reconcile(); the expected and observed "
                "sides would share a decisive ancestor again (Gate 4N-I23 adversarial X5).")

    for node in _ast.walk(fn):
        if not (isinstance(node, _ast.Assign) and any(
                isinstance(t, _ast.Name) and t.id == "expected_arns" for t in node.targets)):
            continue
        called = {c.func.id for c in _ast.walk(node.value)
                  if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Name)}
        attrs = {c.func.attr for c in _ast.walk(node.value)
                 if isinstance(c, _ast.Call) and isinstance(c.func, _ast.Attribute)}
        names = {n.id for n in _ast.walk(node.value) if isinstance(n, _ast.Name)}
        if called & forbidden or attrs & forbidden or "INLINE_POLICY_ROLE_ARNS" in names:
            raise RequirementError(
                "reconcile() builds `expected_arns` from the generated side "
                f"({sorted((called | attrs) & forbidden) or 'INLINE_POLICY_ROLE_ARNS'}). "
                "The expected and observed sides would share a decisive ancestor again — this "
                "is Gate 4N-I22 blocker 2 reintroduced.")


def reconcile(generated_writable_arns=None, expected_writable_arns=None) -> dict:
    """Phase S/W — reconcile the sets and classify every role.

    GATE 4N-I23: the expected side now defaults to the INDEPENDENTLY AUTHORED requirement
    (`required_writable_arns()`), not to `role_arns(writable_roles())`. The observed side is
    still read back out of the emitted policy. The two now share no decisive ancestor, so
    poisoning `writable_roles()` moves ONLY the generated side and surfaces as OVER_GRANTED.
    The .tf parse is kept as a third cross-check under `declared_vs_required`.
    """
    if generated_writable_arns is None:
        generated_writable_arns = generated_writable_arns_from_policy()
    declared = declared_roles()
    writable = writable_roles()
    scope = required_writable_scope()
    if expected_writable_arns is None:
        expected_arns = required_writable_arns(
            [i["role_name"] for i in declared.values() if i.get("role_name")], scope)
    else:
        expected_arns = set(expected_writable_arns)
    generated = set(generated_writable_arns)

    rows, problems = [], []
    # GATE 4N-I24C: iterate the union of PARSED roles and AUTHORED-REQUIRED roles, so a role
    # the parser missed is still adjudicated instead of silently disappearing.
    _seen_names = {i.get("role_name") for i in declared.values() if i.get("role_name")}
    for _req in sorted(scope.get("writable_role_names", []) + scope.get("never_writable_role_names", [])):
        if _req not in _seen_names:
            problems.append(f"{_req}: the authored requirement names this role but the .tf "
                            "parser did not discover it — a parser blind spot must fail closed")
    for label, info in sorted(declared.items()):
        name = info["role_name"]
        if not name:
            rows.append({"role": label, "classification": "UNRESOLVED",
                         "detail": "no literal name attribute could be resolved"})
            problems.append(f"{label}: role name unresolved")
            continue
        arn = identity.iam_role_arn(name)
        is_expected = arn in expected_arns
        is_generated = arn in generated
        if is_expected and is_generated:
            cls = "CORRECTLY_WRITABLE"
        elif not is_expected and not is_generated:
            cls = "CORRECTLY_EMPTY"
        elif is_generated and not is_expected:
            cls = "OVER_GRANTED"
            problems.append(f"{name}: writable in the generated policy but NO aws_iam_role_policy "
                            "declares it — over-granted")
        else:
            cls = "UNDER_GRANTED"
            problems.append(f"{name}: an aws_iam_role_policy binds it but the generated policy "
                            "does not grant it — under-granted")
        rows.append({"role": label, "role_name": name, "arn": arn,
                     "declared_inline_policies": [p["policy_label"] for p in writable.get(label, [])],
                     "expected_writable": is_expected, "generated_writable": is_generated,
                     "classification": cls})

    undeclared = generated - {r["arn"] for r in rows if "arn" in r}
    for arn in sorted(undeclared):
        rows.append({"arn": arn, "classification": "UNDECLARED"})
        problems.append(f"{arn}: granted but no aws_iam_role resource declares it")

    # GATE 4N-I23. A role the AUTHORED requirement does not mention has not been reviewed.
    # Fail closed: neither "writable" nor "empty" may be inferred for it.
    unknown_roles = sorted(n for n in (i.get("role_name") for i in declared.values()) if n
                           and classify_role_name(n, scope) == "UNKNOWN")
    for name in unknown_roles:
        problems.append(f"{name}: the authored requirement classifies this role neither writable "
                        "nor never-writable — UNKNOWN roles fail closed")

    # Shape of every generated resource: a wildcard or malformed ARN is never an acceptable
    # role scope, and a duplicate hides how wide the grant really is.
    raw_generated = list(generated_writable_arns) if not isinstance(generated_writable_arns, set) \
        else sorted(generated_writable_arns)
    for arn in sorted(set(raw_generated)):
        if "*" in arn or "?" in arn:
            problems.append(f"{arn}: the generated policy scopes a role grant with a WILDCARD")
        elif not _ARN_RE.match(arn):
            problems.append(f"{arn}: malformed role ARN in the generated policy")
    duplicates = sorted({a for a in raw_generated if raw_generated.count(a) > 1})
    for arn in duplicates:
        problems.append(f"{arn}: duplicated in the generated policy resource list")

    # THIRD cross-check, reported not conflated: the .tf parse must agree with the authored
    # requirement. Disagreement means the composition and the requirement have diverged —
    # a real finding, but a DIFFERENT one from an over-grant.
    tf_parsed_arns = set(role_arns(writable))
    declared_vs_required = {
        "tf_parsed_writable": sorted(tf_parsed_arns),
        "required_writable": sorted(expected_arns),
        "in_tf_not_required": sorted(tf_parsed_arns - expected_arns),
        "in_required_not_tf": sorted(expected_arns - tf_parsed_arns),
        "agree": tf_parsed_arns == expected_arns,
    }
    for arn in declared_vs_required["in_tf_not_required"]:
        problems.append(f"{arn}: an aws_iam_role_policy binds it but the AUTHORED requirement "
                        "does not permit it to be writable")
    for arn in declared_vs_required["in_required_not_tf"]:
        problems.append(f"{arn}: the authored requirement expects it writable but no "
                        "aws_iam_role_policy declares it")

    return {
        "declared_role_count": len(declared),
        "expected_writable_count": len(expected_arns),
        "generated_writable_count": len(generated),
        "declared_role_set": sorted(i["role_name"] for i in declared.values() if i["role_name"]),
        "expected_writable_role_set": sorted(expected_arns),
        "generated_writable_role_set": sorted(generated),
        "expected_source": scope["source"],
        "expected_source_kind": "INDEPENDENTLY_AUTHORED_REQUIREMENT",
        "declared_vs_required": declared_vs_required,
        "unknown_roles": unknown_roles,
        "duplicate_generated_resources": duplicates,
        "rows": rows,
        "problems": problems,
        "clean": not problems,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    # GATE 4N-I20, ARCH-H2: the GENERATED side comes from the emitted policy document. Passing
    # gen.INLINE_POLICY_ROLE_ARNS here compared the expected set against itself.
    # GATE 4N-I23: and the EXPECTED side must come from the authored requirement. Verified
    # structurally here so the standalone guard is not blind to a lineage regression on a
    # repository where the two derivations happen to agree.
    try:
        assert_expected_lineage()
    except RequirementError as exc:
        print(f"LINEAGE: {exc}", file=sys.stderr)
        print("ROLE INVENTORY: fail-closed")
        return 1
    result = reconcile()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=True))
    else:
        for row in result["rows"]:
            print(f"  {row['classification']:20s} {row.get('role_name') or row.get('arn')}")
        for problem in result["problems"]:
            print(f"    {problem}", file=sys.stderr)
        print("ROLE INVENTORY: clean" if result["clean"] else "ROLE INVENTORY: findings")
    return 0 if result["clean"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
