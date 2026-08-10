"""No implicit HOME anchor resolution (Gate 4N-I13, Defect 1, Phase D).

THE DEFECT. Every anchor resolved through `Path.home() / ".signalnest" / "anchor"`. On a CI
runner with an empty HOME that path does not exist, so five guard scripts exited non-zero and
nine test files failed at collection — while the Gate 4N-I10 "clean checkout" reported
933 passed, because it was a fresh clone that INHERITED $HOME and silently read my machine.

An implicit fallback to a developer home directory is not a convenience; it is the mechanism
by which a portability claim became false without anything failing. This test is AST-based
and prevents a new one from appearing.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

# Modules allowed to call Path.home(), each for a stated non-anchor reason.
ALLOWED_HOME_CALLERS = {
    "check_toolchain_integrity.py":
        "reads ~/.terraformrc and ~/.tofurc to DETECT a CLI config that could redirect the "
        "toolchain — it is looking for the file's presence as a hazard, not resolving an "
        "anchor from it",
    # GATE 4N-I28AS: npm_authority.py joins for exactly the reason above, not as an exception to
    # it. It reads ~/.npmrc to DETECT a user configuration that could set registry, script-shell or
    # prefix and thereby substitute the tool — the file's presence and CONTENT are the hazard being
    # measured. No anchor, requirement or approved path is resolved from the home directory: the
    # npm installation is located from PATH and adjudicated against explicit installation roots in
    # tests/fixtures/npm-authority-policy.json.
    "npm_authority.py":
        "reads ~/.npmrc to DETECT a user npm configuration that could redirect the toolchain — "
        "it is looking for the file's presence and content as a hazard, not resolving an anchor "
        "from it",
    # GATE 4N-I28AT: docker_boundary.py joins for the same reason, not as an exception to it. It
    # reads ~/.docker to DETECT a Docker configuration directory and context store that could
    # redirect execution — their presence and CONTENT are the hazard being measured. No anchor,
    # requirement or approved path is resolved from the home directory.
    "docker_boundary.py":
        "reads ~/.docker/config.json and the context store to DETECT Docker configuration that "
        "could redirect the client to another daemon — it is looking for the files' presence and "
        "content as a hazard, not resolving an anchor from them",
    # GATE 4N-I16 DEFECT 4: verify_artifacts.py was removed from this allowlist. It no
    # longer calls Path.home() at all — its target directory now comes from the explicit
    # candidate manifest rather than a home-relative gate constant. This test failing on a
    # stale entry is exactly what it is for.
}


def _home_calls(path: Path) -> list[int]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    lines = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr == "home"
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "Path"):
            lines.append(node.lineno)
    return lines


@pytest.mark.parametrize("script", sorted(p.name for p in (REPO_ROOT / "scripts").glob("*.py")))
def test_no_script_resolves_an_anchor_from_the_home_directory(script):
    path = REPO_ROOT / "scripts" / script
    calls = _home_calls(path)
    if not calls:
        return
    assert script in ALLOWED_HOME_CALLERS, (
        f"{script} calls Path.home() at line(s) {calls} and is not on the allowlist. Anchor "
        "and requirement paths must be EXPLICIT — an implicit home fallback is how the "
        "Gate 4N-I10 portability evidence became false without anything failing.")


def test_every_allowlist_entry_is_still_needed_and_justified():
    for script, why in ALLOWED_HOME_CALLERS.items():
        path = REPO_ROOT / "scripts" / script
        assert path.exists(), f"stale allowlist entry: {script}"
        assert _home_calls(path), f"{script} no longer calls Path.home(); remove the entry"
        assert len(why.split()) >= 10, f"the exemption for {script} is not justified"


def test_the_anchor_loader_itself_never_touches_home():
    """The one module whose whole job is anchor resolution must be the strictest."""
    assert not _home_calls(REPO_ROOT / "scripts" / "anchor_loader.py")


def test_the_requirement_sources_resolve_explicitly():
    import deny_requirements

    assert deny_requirements.LEDGER_PATH is None
    assert deny_requirements.REQUIREMENTS_V2_PATH is None
    source = (REPO_ROOT / "scripts" / "deny_requirements.py").read_text(encoding="utf-8")
    assert "SIGNALNEST_LEDGER_PATH" in source
    assert "SIGNALNEST_REQUIREMENTS_PATH" in source


def test_the_detector_can_actually_fail(tmp_path):
    probe = tmp_path / "gen_probe.py"
    probe.write_text("from pathlib import Path\nA = Path.home() / '.signalnest' / 'x'\n")
    assert _home_calls(probe), "the detector missed a Path.home() anchor resolution"
