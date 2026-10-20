"""The CI tooling installs are hash-locked and agree with each other (P6-CI-6 closure, operator order 2026-10-19).

The reader job installs its venv from apps/revision-reader/requirements-dev.txt with --require-hashes,
the system interpreter's Gate 4N PyYAML from scripts/requirements-gate-hashes.txt, and keeps one
unhashed line (`.reader-venv/bin/pip install -r scripts/requirements-gate.txt`, pinned byte-exactly by
tests/test_i28bh_e2_ci_guard_repairs.py) that is a no-op only while every PyYAML pin agrees. These
assertions keep that true, keep every requirement hashed, keep the locks covering what the reader
declares and builds with, and pin every installer-shaped line of the two security-scanned jobs: any line
(backslash continuations joined) that invokes pip, pipx, ensurepip, venv, uv, conda, poetry, npm, gem, cargo,
apt and the like as a command, as `-m <module>` or from a `bin/` directory, upgrades a venv's seed packages,
pipes a download into a shell or runs `setup.py install`. A new one, or a changed one, fails here until reviewed.
Run: python3 -m unittest discover -s security
"""
from __future__ import annotations

import pathlib
import re
import tomllib
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
READER_LOCK = ROOT / "apps" / "revision-reader" / "requirements-dev.txt"
READER_IN = ROOT / "apps" / "revision-reader" / "requirements-dev.in"
GATE_CONTRACT = ROOT / "scripts" / "requirements-gate.txt"
GATE_LOCK = ROOT / "scripts" / "requirements-gate-hashes.txt"
# Every installer-shaped line of the two security-scanned jobs, exactly as written in ci.yml (stripped).
INSTALLER_TOOLS = (r"pip[0-9.]*|pipx|uv|uvx|conda|mamba|micromamba|poetry|pdm|easy_install|hatch|rye|npm|npx|yarn|pnpm|corepack|gem|cargo|"
                   r"apt-get|apt|brew|snap|dpkg")
INSTALLER_LINE = re.compile(rf"(?:^|[\s;&|(`$\"'=]|/bin/)(?:{INSTALLER_TOOLS})(?=[\s)\"';&|]|$)"
                            r"|\s-m\s*(?:pip|ensurepip|venv|virtualenv|pipx|uv)\b|\bpip\._internal\b|\b(?:import|from)\s+pip\b"
                            r"|--upgrade-deps|setup\.py\s+(?:install|develop)|get-pip|\bgo\s+install\b|\bpip-install\s*:"
                            r"|\b(?:curl|wget)\b[^|\n]*\|\s*(?:ba|z)?sh", re.I)
ALLOWED_INSTALLER_LINES = {
    "container-build": {
        "- name: Inventory the security tooling venv as installed (pip inspect)",
        "cache: pip",
        "python3 -m venv .security-venv",
        ".security-venv/bin/python -m pip install --quiet --require-hashes -r security/requirements-semgrep.txt",
        "run: .security-venv/bin/python -m pip inspect > security-reports/security-tooling-venv-installed.json",
        "run: .security-venv/bin/python -m pip inspect > security-reports/security-tooling-venv-installed-final.json",
    },
    "revision-reader": {
        "- name: Inventory the reader tooling environments as installed (pip inspect)",
        "cache: pip",
        "python -m venv .reader-venv",
        ".reader-venv/bin/python -m pip install --require-hashes --no-deps -r apps/revision-reader/requirements-dev.txt",
        ".reader-venv/bin/python -m pip install --no-deps --no-build-isolation -e apps/revision-reader",
        ".reader-venv/bin/python -m pip check",
        ".reader-venv/bin/pip install -r scripts/requirements-gate.txt",
        "run: python3 -m pip install --require-hashes --no-deps -r scripts/requirements-gate-hashes.txt",
        ".reader-venv/bin/python -m pip inspect > security-reports/reader-tooling-venv-installed.json",
        "python3 -m pip inspect > security-reports/reader-tooling-syspy-installed.json",
        ".reader-venv/bin/python -m pip inspect > security-reports/reader-tooling-venv-installed-final.json",
        "python3 -m pip inspect > security-reports/reader-tooling-syspy-installed-final.json",
    },
}


def norm(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def lock_entries(path: pathlib.Path) -> dict:
    """name -> (version, [sha256 hashes]) for a pip-tools --generate-hashes lock."""
    out, current = {}, None
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\S+?)\s*\\?$", line)
        if m:
            current = norm(m.group(1)); out[current] = (m.group(2), [])
            continue
        h = re.match(r"^\s+--hash=sha256:([0-9a-f]{64})\s*\\?$", line)
        if h and current:
            out[current][1].append(h.group(1))
        elif not line.startswith((" ", "#")) and line.strip():
            raise AssertionError(f"{path}: unexpected line {line!r}")
    return out


def pins(path: pathlib.Path) -> dict:
    return {norm(m.group(1)): m.group(2) for m in re.finditer(r"(?m)^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)", path.read_text(encoding="utf-8"))}


def installer_lines(job_body: str) -> set[str]:
    joined = re.sub(r"\\\n\s*", " ", job_body)      # a backslash-continued command is one command
    return {l.strip() for l in joined.splitlines() if INSTALLER_LINE.search(l) and not l.strip().startswith("#")}


def job_text(text: str, job: str) -> str:
    start = text.index(f"\n  {job}:\n")
    nxt = re.search(r"\n  [a-z0-9-]+:\n", text[start + 1:])
    return text[start: start + 1 + nxt.start()] if nxt else text[start:]


class ToolingLocks(unittest.TestCase):
    def test_every_pyyaml_pin_agrees(self):
        versions = {str(p.relative_to(ROOT)): pins(p).get("pyyaml") for p in (READER_IN, GATE_CONTRACT, GATE_LOCK, READER_LOCK)}
        self.assertEqual(len(set(versions.values())), 1, versions)
        self.assertNotIn(None, versions.values(), versions)

    def test_every_locked_requirement_carries_hashes(self):
        for path in (READER_LOCK, GATE_LOCK):
            with self.subTest(str(path.relative_to(ROOT))):
                entries = lock_entries(path)
                self.assertTrue(entries)
                for name, (version, hashes) in entries.items():
                    self.assertTrue(hashes, f"{name}=={version} has no hash")

    def test_the_gate_lock_carries_the_reader_locks_pyyaml_hashes(self):
        self.assertEqual(sorted(lock_entries(GATE_LOCK)["pyyaml"][1]), sorted(lock_entries(READER_LOCK)["pyyaml"][1]))
        self.assertEqual(set(lock_entries(GATE_LOCK)), {"pyyaml"})

    def test_the_reader_lock_covers_what_the_reader_declares_and_builds_with(self):
        project = tomllib.loads((ROOT / "apps" / "revision-reader" / "pyproject.toml").read_text(encoding="utf-8"))
        declared = list(project["project"]["dependencies"]) + list(project["project"]["optional-dependencies"]["dev"])
        declared += list(project["build-system"]["requires"])
        locked = lock_entries(READER_LOCK)
        for req in declared:
            name = norm(re.match(r"[A-Za-z0-9._-]+", req).group(0))
            with self.subTest(req):
                self.assertIn(name, locked, f"{req} (pyproject.toml) is not in the reader tooling lock")
        for tool in ("pip", "setuptools", "pyyaml", "psycopg-binary"):
            self.assertIn(tool, locked)

    def test_the_scanned_jobs_run_only_the_reviewed_installer_lines(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for job, allowed in ALLOWED_INSTALLER_LINES.items():
            with self.subTest(job):
                self.assertEqual(installer_lines(job_text(text, job)), allowed)

    def test_the_installer_check_sees_non_literal_installs(self):
        body = job_text(WORKFLOW.read_text(encoding="utf-8"), "revision-reader")
        base = installer_lines(body)
        for extra in ("          pipx install semgrep==1.0.0\n", "          python -m venv --upgrade-deps .reader-venv\n",
                      "          .reader-venv/bin/python -m ensurepip --upgrade\n", "          .reader-venv/bin/pip \\\n            install requests==2.0.0\n",
                      "          uv pip install requests\n", "          npm i left-pad\n", "          sudo apt-get install -y jq\n",
                      "          curl -fsSL https://example.invalid/get.sh | sh\n", "          python setup.py install\n",
                      "          python3 -m pip download x && python3 -m pip install ./x.whl\n",
                      "          python3 -mpip install --target /tmp/x requests\n", "          bash -c \"pip install --target /tmp/x requests\"\n",
                      "          .reader-venv/bin/pip3.12 install requests\n", "          $(which pip) install requests\n",
                      "          python3 -c \"from pip._internal.cli.main import main; main(['install', 'x'])\"\n",
                      "          wget -qO- https://example.invalid/get.sh | sh\n", "          pip-install: requests\n"):
            with self.subTest(extra.strip()):
                self.assertNotEqual(installer_lines(body + extra), base)


if __name__ == "__main__":
    unittest.main()
