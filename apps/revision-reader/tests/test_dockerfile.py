"""Gate 4J — structural assertions on the reader Dockerfile.

These are cheap offline checks for properties whose violation is EXPENSIVE and QUIET.
The defect that motivated the file: the builder was pinned to python:3.12-slim while
gcr.io/distroless/python3-debian12 ships Python 3.11. Nothing in the repository failed.
The image would simply have been unable to import psycopg — discovered, at best, during
a live invocation gate, against a production database, with a stale expected head.

Scope discipline: this file tests the BUILD CONTRACT (entrypoint form, interpreter
coherence, absence of shell/migration surface). It does NOT claim the image works — only
building and running it can establish that, which is the CI in-image band's job.
"""

from __future__ import annotations

import ast
import json
import re
import shlex
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DOCKERFILE = ROOT / "Dockerfile"


@pytest.fixture(scope="module")
def text() -> str:
    return DOCKERFILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def code(directives: list[tuple[str, str]]) -> str:
    """Instructions only, with comments removed.

    Scans that look for forbidden tokens MUST run against this rather than the raw file.
    The Dockerfile's own header explains why alembic and the application package are
    excluded, and a scan that cannot tell prose from instructions would flag that
    explanation — which pushes authors toward deleting the explanation rather than
    toward safer code.
    """
    return "\n".join(f"{i} {a}" for i, a in directives)


@pytest.fixture(scope="module")
def directives(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, argument) pairs with comments and line continuations resolved."""
    joined, out = "", []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.endswith("\\"):
            joined += line[:-1].strip() + " "
            continue
        line, joined = joined + line, ""
        instruction, _, arg = line.partition(" ")
        out.append((instruction.upper(), arg.strip()))
    return out


def _entrypoint(directives: list[tuple[str, str]]) -> list[str]:
    args = [a for i, a in directives if i == "ENTRYPOINT"]
    assert len(args) == 1, f"expected exactly one ENTRYPOINT, found {len(args)}"
    return json.loads(args[0])


# --- the control itself --------------------------------------------------------------


def test_entrypoint_is_exec_form_json(directives):
    """Shell form (`ENTRYPOINT cmd`) would run under `/bin/sh -c` and re-expose a shell."""
    entry = _entrypoint(directives)
    assert isinstance(entry, list) and all(isinstance(x, str) for x in entry)


def test_entrypoint_invokes_no_shell(directives):
    entry = _entrypoint(directives)
    assert not any(Path(tok).name in {"sh", "bash", "dash", "ash", "busybox"} for tok in entry)
    assert "-c" not in entry, "a `-c` in the entrypoint would accept an arbitrary program"


def test_entrypoint_uses_absolute_interpreter_path(directives):
    """A bare `python` resolves through PATH, which an image rebuild could redirect."""
    assert _entrypoint(directives)[0].startswith("/")


def test_entrypoint_pins_the_module_and_ends_there(directives):
    """`-m <module>` must be the LAST entrypoint element.

    CPython stops parsing interpreter options at `-m <module>`; everything after is
    sys.argv for the module. That is what makes an override command like ["-c", "..."]
    arrive as rejectable argv rather than as an interpreter flag that runs code. A
    trailing entrypoint argument after the module name would be silently consumed by the
    reader's own argv rejection instead, so the position is load-bearing.
    """
    entry = _entrypoint(directives)
    assert entry[-2:] == ["-m", "revision_reader.reader"], entry


def test_cmd_is_empty(directives):
    """A non-empty CMD is default argv, which this program rejects — the image would
    never start successfully, and the failure would look like a reader bug."""
    cmds = [a for i, a in directives if i == "CMD"]
    assert cmds == ["[]"], cmds


def test_task_definition_must_not_set_entrypoint():
    """The IaC counterpart of the control: a task-definition entryPoint shadows the
    image's. Asserted here rather than only in Terraform because this file is where a
    future author edits the entrypoint."""
    module = ROOT.parents[1] / "infra" / "aws" / "modules" / "revision_reader"
    files = sorted(module.glob("*.tf"))
    assert files, "revision_reader module not found — the control has no IaC counterpart"
    # Match an ASSIGNMENT, not the word. The module header explains this very control by
    # name, and a bare substring scan would forbid documenting it.
    assignment = re.compile(r"\"?entryPoint\"?\s*[=:]")
    for path in files:
        hit = assignment.search(path.read_text(encoding="utf-8"))
        assert hit is None, f"{path.name} assigns entryPoint, shadowing the image's"


# --- interpreter coherence (the defect this file was written for) --------------------


def _minors(code: str) -> dict[str, str]:
    builder = re.search(r"FROM\s+python:(\d+\.\d+)[.-]", code)
    entry = re.search(r"ENTRYPOINT\s+\[\"/usr/bin/python(\d+\.\d+)\"", code)
    pypath = re.search(r"PYTHONPATH=/usr/local/lib/python(\d+\.\d+)/", code)
    assert builder and entry and pypath, "could not locate all three interpreter versions"
    return {
        "builder": builder.group(1),
        "entrypoint": entry.group(1),
        "pythonpath": pypath.group(1),
    }


def test_builder_entrypoint_and_pythonpath_minors_agree(code):
    """psycopg[binary] is an ABI-specific wheel. If the builder minor differs from the
    runtime interpreter, the image cannot import its only dependency; if PYTHONPATH's
    minor differs, it cannot find it at all. All three must be one number."""
    found = _minors(code)
    assert len(set(found.values())) == 1, found


def test_runtime_minor_matches_the_distroless_base(code):
    """gcr.io/distroless/python3-debian12 ships Python 3.11 at /usr/bin/python3.11.
    Bumping the base to a debian13 (or any other) tag changes that, so the base tag and
    the interpreter minor are pinned together here deliberately."""
    assert "gcr.io/distroless/python3-debian12" in code
    assert _minors(code)["entrypoint"] == "3.11"


# --- what must not be in the image ---------------------------------------------------


def test_application_package_is_never_installed(code):
    """`alembic` is a BASE dependency of apps/api, so installing that package at all
    would restore migration capability. The reader is a separate distribution precisely
    so this stays true. Scans `code`, never the raw text — see the fixture's docstring."""
    for forbidden in ("apps/api", "app.db", "alembic", "sqlalchemy", "../"):
        assert forbidden not in code, f"Dockerfile instruction references {forbidden!r}"


def test_only_the_reader_sources_are_copied(text, directives):
    """A wildcard `COPY . .` would sweep in whatever a future directory contains."""
    external = [a for i, a in directives if i == "COPY" and "--from=" not in a]
    assert external, "expected at least one source COPY"
    for arg in external:
        src = shlex.split(arg)[0]
        assert src in {"pyproject.toml", "revision_reader", "assets"}, (
            f"unexpected COPY source {src!r}"
        )


def test_runs_as_the_fleet_nonroot_uid(directives):
    users = [a for i, a in directives if i == "USER"]
    assert users and users[-1] == "10001:10001", users


def test_final_stage_is_distroless_not_the_builder(directives):
    """The last FROM decides what ships. Reordering the stages so the slim builder is
    final would silently restore a shell and a package manager."""
    froms = [a for i, a in directives if i == "FROM"]
    assert froms[-1].startswith("gcr.io/distroless/"), froms


# --- Gate 4J.1: base-image digest pinning ---------------------------------------------


def test_both_base_images_are_digest_pinned(directives):
    """A floating tag (python:3.11-slim) lets a registry retag change the bytes silently,
    including the exact 3.11.x patch on whose urlsplit behaviour the DSN parser depends.
    Both bases must be @sha256:-pinned."""
    froms = [a for i, a in directives if i == "FROM"]
    assert len(froms) == 2, froms
    for f in froms:
        assert "@sha256:" in f, f"base image not digest-pinned: {f}"


def test_final_base_digest_is_the_reviewed_distroless(code):
    assert (
        "gcr.io/distroless/python3-debian12@sha256:"
        "2fdb05402a2cf21cf78fdb3ba4c5db167241e9e498140f5bf689d7efb773731f"
    ) in code


# --- Gate 4J.1: baked destination pins and CA bundle ----------------------------------


def test_destination_pins_are_baked_from_build_args_and_reject_empty(text):
    """Host, database and role are baked from build args into a SOURCE constant (not ENV,
    which is caller-overridable), and empty args must fail the build so no placeholder
    image can ship."""
    for arg in ("EXPECTED_DB_HOST", "EXPECTED_DB_NAME", "EXPECTED_DB_USER"):
        assert f"ARG {arg}" in text
    assert "revision_reader/_pinned.py" in text
    assert "must be non-empty" in text


def test_ca_bundle_is_copied_to_the_fixed_reader_path(code):
    assert "/etc/ssl/rds/rds-global-bundle.pem" in code


def test_ca_bundle_checksum_is_verified_during_build(code):
    assert "sha256sum -c" in code
    assert "e5bb2084ccf45087bda1c9bffdea0eb15ee67f0b91646106e466714f9de3c7e3" in code


def test_reader_pinned_ca_path_matches_the_dockerfile(text):
    from revision_reader import _pinned

    assert _pinned.CA_BUNDLE_PATH == "/etc/ssl/rds/rds-global-bundle.pem"
    assert _pinned.CA_BUNDLE_PATH in text


# --- entry-point convergence ---------------------------------------------------------


def test_every_entry_point_resolves_to_the_same_main():
    """There are three ways in — the image entrypoint (`-m revision_reader.reader`), the
    package (`-m revision_reader`), and the console script — and a security artefact must
    not have two entries that can drift apart. Pinned by AST so the check needs no import
    side effects: `__main__.py` runs `main()` at import time by design.
    """
    pkg = ROOT / "revision_reader"
    main_src = (pkg / "__main__.py").read_text(encoding="utf-8")
    tree = ast.parse(main_src)

    imported = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    assert ("revision_reader.reader", "main") in imported, (
        "__main__.py must delegate to revision_reader.reader.main, not define its own entry"
    )

    # `SystemExit(main())` is the whole file; anything else called here would be a second
    # entry point's worth of behaviour living outside the tested program.
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert called == {"main", "SystemExit"}, (
        f"__main__.py calls something beyond SystemExit(main()): {sorted(called)}"
    )

    # And the console script names the shim in the same module.
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'revision-reader = "revision_reader.reader:_console_main"' in pyproject
