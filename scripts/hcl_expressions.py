#!/usr/bin/env python3
"""Repository EXPRESSION resolution for resource names (Gate 4N-I10, Defect 6).

THE DEFECT. The resource oracle's `rds:pg`, `rds:subgrp` and `dynamodb:lock` rows were
NAMING-CONVENTION RESTATEMENTS, not derivations: they rebuilt `<prefix>-pg-params` and
friends from the prefix and compared that to a generator that rebuilt the same string the
same way. The Gate 4N-I9 adversarial lane renamed the parameter group in the repository to a
value that did not even contain the prefix, and the oracle still reported MATCH. A
convention restatement is two copies of the same assumption, not an independent witness.

This module resolves the ACTUAL HCL expression: it finds the resource block, reads the
`name` attribute expression, and resolves variables, locals and `coalesce` against their
declared defaults. If it cannot reach a literal, it says UNRESOLVED and the caller fails —
there is no prefix fallback, because the fallback IS the defect.

ONE HONEST OUTCOME. The DynamoDB lock table name resolves to `var.lock_table_name`, which is
REQUIRED and supplied at bootstrap time through a git-ignored tfvars. It is genuinely not
derivable from the repository at all. That is reported as
EXTERNAL_INPUT_NOT_REPOSITORY_DERIVABLE with live corroboration, NOT as a MATCH — inventing a
convention for it is precisely what this module exists to stop.
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra" / "aws"

RESOLVED = "RESOLVED"
UNRESOLVED = "UNRESOLVED"
EXTERNAL_INPUT = "EXTERNAL_INPUT_NOT_REPOSITORY_DERIVABLE"


@dataclass
class Resolution:
    key: str
    status: str
    value: str | None
    hcl_file: str
    block: str
    expression: str
    steps: list[str]
    note: str = ""


def _read(rel: str) -> str:
    return (INFRA / rel).read_text(encoding="utf-8")


def _block(text: str, kind: str, label: str) -> str | None:
    m = re.search(r'resource "%s" "%s" \{(.*?)\n\}' % (kind, label), text, re.DOTALL)
    return m.group(1) if m else None


def _attribute(body: str, attr: str) -> str | None:
    m = re.search(r'^\s*%s\s*=\s*(.+)$' % attr, body, re.MULTILINE)
    return m.group(1).strip() if m else None


def _locals(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for block in re.findall(r'locals \{(.*?)\n\}', text, re.DOTALL):
        for name, value in re.findall(r'^\s*(\w+)\s*=\s*(.+)$', block, re.MULTILINE):
            out.setdefault(name, value.strip())
    return out


def _variable_default(text: str, name: str) -> tuple[bool, str | None]:
    m = re.search(r'variable "%s" \{(.*?)\n\}' % name, text, re.DOTALL)
    if not m:
        return False, None
    d = re.search(r'^\s*default\s*=\s*(.+)$', m.group(1), re.MULTILINE)
    if not d:
        return True, None  # declared, but REQUIRED
    value = d.group(1).strip()
    return True, None if value == "null" else value.strip('"')


def _resolve(expression: str, *, module_text: str, variables_text: str,
             name_prefix: str, steps: list[str]) -> str | None:
    """Resolve one expression to a literal, or None."""
    expr = expression.strip()
    steps.append(f"expression: {expr}")

    coalesce = re.match(r'coalesce\((.+)\)$', expr)
    if coalesce:
        args = [a.strip() for a in re.split(r',\s*(?![^"]*"\s*\))', coalesce.group(1))]
        steps.append(f"coalesce with {len(args)} argument(s)")
        for arg in args:
            resolved = _resolve(arg, module_text=module_text, variables_text=variables_text,
                                name_prefix=name_prefix, steps=steps)
            if resolved is not None:
                return resolved
        return None

    local = re.match(r'local\.(\w+)$', expr)
    if local:
        table = _locals(module_text)
        if local.group(1) not in table:
            steps.append(f"local.{local.group(1)} NOT FOUND")
            return None
        steps.append(f"-> local.{local.group(1)}")
        return _resolve(table[local.group(1)], module_text=module_text,
                        variables_text=variables_text, name_prefix=name_prefix, steps=steps)

    variable = re.match(r'var\.(\w+)$', expr)
    if variable:
        declared, default = _variable_default(variables_text, variable.group(1))
        if not declared:
            steps.append(f"var.{variable.group(1)} NOT DECLARED")
            return None
        if default is None:
            steps.append(f"var.{variable.group(1)} is REQUIRED with no default — "
                         "supplied outside the repository")
            return None
        steps.append(f"-> var.{variable.group(1)} default {default!r}")
        return default

    if expr.startswith('"') and expr.endswith('"'):
        literal = expr[1:-1]
        resolved = literal.replace("${var.name_prefix}", name_prefix)
        if "${" in resolved:
            steps.append(f"unresolved interpolation remains in {resolved!r}")
            return None
        steps.append(f"-> literal {resolved!r}")
        return resolved

    steps.append(f"unsupported expression form {expr!r}")
    return None


def name_prefix() -> str:
    locals_src = _read("locals.tf")
    expr = re.search(r'name_prefix\s*=\s*"([^"]+)"', locals_src).group(1)
    variables = _read("variables.tf")

    def default(var: str) -> str:
        block = re.search(r'variable "%s" \{(.*?)\n\}' % var, variables, re.DOTALL).group(1)
        return re.search(r'default\s*=\s*"([^"]+)"', block).group(1)

    return (expr.replace("${lower(var.project_name)}", default("project_name").lower())
                .replace("${var.environment}", default("environment")))


TARGETS = [
    ("rds:pg", "modules/data_sql/main.tf", "modules/data_sql/variables.tf",
     "aws_db_parameter_group", "this"),
    ("rds:subgrp", "modules/data_sql/main.tf", "modules/data_sql/variables.tf",
     "aws_db_subnet_group", "this"),
    ("dynamodb:lock", "bootstrap/main.tf", "bootstrap/variables.tf",
     "aws_dynamodb_table", "lock"),
]


def resolve_all() -> dict[str, Resolution]:
    prefix = name_prefix()
    out: dict[str, Resolution] = {}
    for key, module_rel, variables_rel, kind, label in TARGETS:
        module_text = _read(module_rel)
        variables_text = _read(variables_rel)
        body = _block(module_text, kind, label)
        if body is None:
            out[key] = Resolution(key, UNRESOLVED, None, module_rel, f"{kind}.{label}",
                                  "", [f"{kind}.{label} not found"])
            continue
        expression = _attribute(body, "name")
        if expression is None:
            out[key] = Resolution(key, UNRESOLVED, None, module_rel, f"{kind}.{label}",
                                  "", ["no name attribute"])
            continue
        steps: list[str] = []
        value = _resolve(expression, module_text=module_text, variables_text=variables_text,
                         name_prefix=prefix, steps=steps)
        if value is not None:
            out[key] = Resolution(key, RESOLVED, value, module_rel, f"{kind}.{label}",
                                  expression, steps)
        elif any("REQUIRED with no default" in s for s in steps):
            out[key] = Resolution(
                key, EXTERNAL_INPUT, None, module_rel, f"{kind}.{label}", expression, steps,
                note="supplied outside the repository (git-ignored tfvars). NOT derivable, "
                     "and deliberately NOT guessed from a naming convention — inventing one "
                     "is the defect this module closes. Corroborated by SOURCE B only.")
        else:
            out[key] = Resolution(key, UNRESOLVED, None, module_rel, f"{kind}.{label}",
                                  expression, steps)
    return out


def main() -> int:
    results = resolve_all()
    payload = {k: vars(v) for k, v in results.items()}
    if "--json" in sys.argv:
        print(json.dumps(payload, indent=2, ensure_ascii=True))
    else:
        for key, r in results.items():
            print(f"  {r.status:38s} {key:16s} {r.value or ''}")
    bad = [k for k, v in results.items() if v.status == UNRESOLVED]
    if bad:
        print(f"EXPRESSION RESOLUTION: unresolved {bad}", file=sys.stderr)
    print("EXPRESSION RESOLUTION: clean" if not bad else "EXPRESSION RESOLUTION: findings")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
