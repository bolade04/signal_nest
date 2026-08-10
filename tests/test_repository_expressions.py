"""Repository EXPRESSION resolution and rename mutations (Gate 4N-I10, Defect 6).

THE DEFECT, demonstrated by the Gate 4N-I9 adversarial lane. The oracle's `rds:pg`,
`rds:subgrp` and `dynamodb:lock` rows were naming-CONVENTION restatements: they rebuilt
`<prefix>-pg-params` from the prefix and compared it to a generator that rebuilt the same
string the same way. The lane renamed the parameter group in the repository to a value that
did not even contain the prefix, and the oracle still printed MATCH. Two copies of one
assumption is not a witness.

`scripts/hcl_expressions.py` now resolves the ACTUAL HCL: it finds the resource block, reads
the `name` expression, and follows locals, variables and `coalesce` to a literal. The rename
tests below are the proof that it is load-bearing — each one edits the repository expression
in an isolated copy and requires the resolver to follow it or to say UNRESOLVED. What it may
never do is keep reporting the old convention value.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import hcl_expressions as hx  # noqa: E402


@pytest.fixture
def infra(tmp_path, monkeypatch):
    dst = tmp_path / "aws"
    shutil.copytree(REPO_ROOT / "infra" / "aws", dst,
                    ignore=shutil.ignore_patterns(".terraform", "*.tfstate*", "__pycache__"))
    monkeypatch.setattr(hx, "INFRA", dst)
    return dst


def test_the_resolver_derives_the_real_values():
    results = hx.resolve_all()
    assert results["rds:pg"].status == hx.RESOLVED
    assert results["rds:pg"].value == "signalnest-staging-pg-params"
    assert results["rds:subgrp"].status == hx.RESOLVED
    assert results["rds:subgrp"].value == "signalnest-staging-pg"


def test_the_resolver_records_its_steps():
    """A derivation nobody can follow is a convention with extra words."""
    for key in ("rds:pg", "rds:subgrp"):
        resolution = hx.resolve_all()[key]
        assert resolution.steps, key
        assert resolution.expression, key
        assert resolution.hcl_file.endswith(".tf"), key


def test_the_lock_table_is_honestly_classified_as_external_not_guessed():
    """aws_dynamodb_table.lock takes var.lock_table_name, REQUIRED, git-ignored tfvars.

    The previous row guessed `<prefix>-tf-lock` and reported MATCH. Guessing is the defect.
    """
    resolution = hx.resolve_all()["dynamodb:lock"]
    assert resolution.status == hx.EXTERNAL_INPUT
    assert resolution.value is None, "it must NOT invent a value"
    assert "not derivable" in resolution.note.lower()
    assert "var.lock_table_name" in resolution.expression


# --- PHASE N: rename mutations ------------------------------------------------------------


def _edit(path: Path, old: str, new: str):
    text = path.read_text(encoding="utf-8")
    assert old in text, f"anchor not found in {path.name}: {old!r}"
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


def test_renaming_the_parameter_group_is_followed(infra):
    _edit(infra / "modules/data_sql/main.tf",
          '"${var.name_prefix}-pg-params"', '"totally-different-params"')
    resolution = hx.resolve_all()["rds:pg"]
    assert resolution.status == hx.RESOLVED
    assert resolution.value == "totally-different-params", (
        "the resolver kept the old convention value instead of following the expression")


def test_renaming_the_subnet_group_local_is_followed(infra):
    _edit(infra / "modules/data_sql/main.tf",
          'coalesce(var.db_subnet_group_name, "${var.name_prefix}-pg")',
          'coalesce(var.db_subnet_group_name, "renamed-subnet-group")')
    resolution = hx.resolve_all()["rds:subgrp"]
    assert resolution.status == hx.RESOLVED
    assert resolution.value == "renamed-subnet-group"


def test_supplying_the_optional_subnet_group_default_is_followed(infra):
    """coalesce must prefer the variable when it stops being null."""
    _edit(infra / "modules/data_sql/variables.tf",
          "default     = null", 'default     = "explicit-subnet-group"')
    resolution = hx.resolve_all()["rds:subgrp"]
    assert resolution.status == hx.RESOLVED
    assert resolution.value == "explicit-subnet-group", resolution.steps


def test_a_name_with_no_prefix_at_all_is_still_followed(infra):
    """The adversarial lane's exact mutation: a name that breaks the convention entirely."""
    _edit(infra / "modules/data_sql/main.tf",
          '"${var.name_prefix}-pg-params"', '"zzz-nothing-like-the-prefix"')
    resolution = hx.resolve_all()["rds:pg"]
    assert resolution.value == "zzz-nothing-like-the-prefix"
    assert "signalnest" not in (resolution.value or "")


def test_an_unresolvable_expression_returns_UNRESOLVED_not_a_convention(infra):
    _edit(infra / "modules/data_sql/main.tf",
          '"${var.name_prefix}-pg-params"', 'some_module.output.name')
    resolution = hx.resolve_all()["rds:pg"]
    assert resolution.status == hx.UNRESOLVED
    assert resolution.value is None


def test_a_missing_resource_block_is_UNRESOLVED(infra):
    _edit(infra / "modules/data_sql/main.tf",
          'resource "aws_db_parameter_group" "this"',
          'resource "aws_db_parameter_group" "renamed_label"')
    resolution = hx.resolve_all()["rds:pg"]
    assert resolution.status == hx.UNRESOLVED


def test_giving_the_lock_table_a_default_makes_it_resolvable(infra):
    """The classification must follow the repository, not be hardcoded."""
    text = (infra / "bootstrap/variables.tf").read_text(encoding="utf-8")
    marker = 'variable "lock_table_name" {'
    assert marker in text
    patched = text.replace(marker, marker + '\n  default = "now-derivable-lock"', 1)
    (infra / "bootstrap/variables.tf").write_text(patched, encoding="utf-8")
    resolution = hx.resolve_all()["dynamodb:lock"]
    assert resolution.status == hx.RESOLVED
    assert resolution.value == "now-derivable-lock"


def test_a_rename_makes_the_oracle_disagree_with_the_generator(infra, monkeypatch):
    """End to end: the resolver feeding the oracle must expose the generator mismatch."""
    import resource_oracle

    monkeypatch.setattr(resource_oracle, "INFRA", infra)
    assert resource_oracle.compare()["clean"], "control: unmutated copy must be clean"

    _edit(infra / "modules/data_sql/main.tf",
          '"${var.name_prefix}-pg-params"', '"renamed-in-the-repository"')
    result = resource_oracle.compare()
    row = next(r for r in result["rows"] if r["key"] == "rds:pg")
    assert row["result"] == resource_oracle.Status.MISMATCH, row
    assert not result["clean"]


def test_the_resolver_has_no_prefix_fallback():
    """Static guard: the fallback IS the defect."""
    source = (REPO_ROOT / "scripts" / "hcl_expressions.py").read_text(encoding="utf-8")
    body = source.split('"""', 2)[-1]
    assert "-pg-params" not in body, "a convention literal is back in the resolver"
    assert "-tf-lock" not in body, "a convention literal is back in the resolver"
