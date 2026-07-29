"""Offline strict revision comparator tests (Phase 4 Gate 4F).

Anti-vacuity by construction: the exact-match positive case kills an
"always rejects" stub; the multi-line/truncated-second-line cases kill a
``readline()``-based stub that ignores everything after the first line; the
ancestor case (a real, well-formed non-head revision) proves the comparison is
exact equality against the code head, not format validation alone. The module
must stay strictly offline: no engine construction, no DB import.
"""

from __future__ import annotations

import inspect as pyinspect
import io

import app.db.revision_compare as revision_compare
from app.db.schema import code_head_revision


def _run(monkeypatch, capsys, stdin_text: str, argv: list[str] | None = None):
    monkeypatch.setattr("sys.stdin", io.StringIO(stdin_text))
    rc = revision_compare.main(argv if argv is not None else [])
    cap = capsys.readouterr()  # single capture per scenario
    return rc, cap


def test_exact_match_succeeds(monkeypatch, capsys) -> None:
    head = code_head_revision()
    rc, cap = _run(monkeypatch, capsys, f"{head}\n")
    assert rc == revision_compare.EXIT_OK
    assert cap.out == "revision-compare: match\n"
    assert cap.err == ""


def test_exact_match_without_trailing_newline_succeeds(monkeypatch, capsys) -> None:
    rc, cap = _run(monkeypatch, capsys, code_head_revision())
    assert rc == revision_compare.EXIT_OK
    assert cap.out == "revision-compare: match\n"


def test_ancestor_revision_rejected(monkeypatch, capsys) -> None:
    # A real, well-formed revision from this repository's graph that is NOT the
    # head: exact equality must reject it (ancestors are not "close enough").
    rc, cap = _run(monkeypatch, capsys, "9a7c614699d8\n")
    assert rc == revision_compare.EXIT_MISMATCH
    assert cap.err == "revision-compare: mismatch\n"
    assert cap.out == ""


def test_well_formed_foreign_revision_rejected(monkeypatch, capsys) -> None:
    rc, cap = _run(monkeypatch, capsys, "aaaaaaaaaaaa\n")
    assert rc == revision_compare.EXIT_MISMATCH


def test_prefix_of_head_rejected(monkeypatch, capsys) -> None:
    rc, cap = _run(monkeypatch, capsys, code_head_revision()[:11] + "\n")
    assert rc == revision_compare.EXIT_INPUT_INVALID  # 11 chars: malformed


def test_suffix_extended_head_rejected(monkeypatch, capsys) -> None:
    rc, cap = _run(monkeypatch, capsys, code_head_revision() + "0\n")
    assert rc == revision_compare.EXIT_INPUT_INVALID  # 13 chars: malformed


def test_case_mutation_rejected(monkeypatch, capsys) -> None:
    rc, cap = _run(monkeypatch, capsys, code_head_revision().upper() + "\n")
    assert rc == revision_compare.EXIT_INPUT_INVALID


def test_two_valid_lines_rejected(monkeypatch, capsys) -> None:
    head = code_head_revision()
    rc, cap = _run(monkeypatch, capsys, f"{head}\n{head}\n")
    assert rc == revision_compare.EXIT_INPUT_INVALID
    assert cap.err == "revision-compare: invalid-input\n"


def test_match_plus_truncated_second_line_rejected(monkeypatch, capsys) -> None:
    # A readline()-based comparator would accept this: the complete stream must
    # be read and the extra (truncated) line must reject.
    head = code_head_revision()
    rc, cap = _run(monkeypatch, capsys, f"{head}\n98289430a3e")
    assert rc == revision_compare.EXIT_INPUT_INVALID


def test_junk_then_match_rejected(monkeypatch, capsys) -> None:
    rc, cap = _run(monkeypatch, capsys, f"garbage\n{code_head_revision()}\n")
    assert rc == revision_compare.EXIT_INPUT_INVALID


def test_garbage_rejected(monkeypatch, capsys) -> None:
    rc, cap = _run(monkeypatch, capsys, "not-a-revision\n")
    assert rc == revision_compare.EXIT_INPUT_INVALID


def test_empty_input_rejected(monkeypatch, capsys) -> None:
    rc, cap = _run(monkeypatch, capsys, "")
    assert rc == revision_compare.EXIT_INPUT_INVALID
    assert cap.err == "revision-compare: invalid-input\n"


def test_multiple_repository_heads_rejected(monkeypatch, capsys) -> None:
    class _MultiHeadScript:
        def get_heads(self):
            return ["98289430a3ec", "aaaaaaaaaaaa"]

    class _ScriptDirectory:
        @classmethod
        def from_config(cls, cfg):
            return _MultiHeadScript()

    import alembic.script as alembic_script

    monkeypatch.setattr(alembic_script, "ScriptDirectory", _ScriptDirectory)
    rc, cap = _run(monkeypatch, capsys, "98289430a3ec\n")
    assert rc == revision_compare.EXIT_CODE_HEAD_UNRESOLVED
    assert cap.err == "revision-compare: code-head-unresolved\n"


def test_stray_argv_rejected(monkeypatch, capsys) -> None:
    rc, cap = _run(monkeypatch, capsys, "98289430a3ec\n", argv=["--force"])
    assert rc == revision_compare.EXIT_ARGS
    assert cap.err == "revision-compare: unexpected-arguments\n"


def test_unreadable_stdin_is_a_safe_failure(monkeypatch, capsys) -> None:
    class _BoomStdin:
        def read(self):
            raise OSError("stdin unreadable")

    monkeypatch.setattr("sys.stdin", _BoomStdin())
    rc = revision_compare.main([])
    cap = capsys.readouterr()
    assert rc == revision_compare.EXIT_UNEXPECTED
    assert cap.err == "revision-compare: unexpected-failure\n"
    assert "Traceback" not in cap.out + cap.err


def test_module_is_strictly_offline() -> None:
    # AST-level check (docstrings don't count): the comparator must never
    # import engine construction, the process engine module, or sqlalchemy.
    import ast

    tree = ast.parse(pyinspect.getsource(revision_compare))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            imported.add(module)
            imported.update(f"{module}.{alias.name}" for alias in node.names)
    assert not any(name.startswith("sqlalchemy") for name in imported)
    assert "app.db.session" not in imported
    assert not any(name.endswith("create_engine") for name in imported)
