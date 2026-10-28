"""The CI tooling installs are hash-locked and agree with each other (P6-CI-6 closure, operator order 2026-10-19).

The reader job installs its venv from apps/revision-reader/requirements-dev.txt with --require-hashes,
the system interpreter's Gate 4N PyYAML from scripts/requirements-gate-hashes.txt, that interpreter's own pip
from apps/revision-reader/requirements-toolcache-pip.txt (GHSA-6q48-6c7w-9gw2: the toolcache's bundled pip
vendors its own certifi, so it is replaced from a lock before anything else enters the prefix), and keeps one
unhashed line (`.reader-venv/bin/pip install -r scripts/requirements-gate.txt`, pinned byte-exactly by
tests/test_i28bh_e2_ci_guard_repairs.py) that is a no-op only while every PyYAML pin agrees. The four jobs
that create the API venv install pip and the setuptools build backend from apps/api/requirements-bootstrap.txt,
and Backend quality installs its lock toolchain (.locktools) from apps/api/requirements-locktools.txt (P6-CI-6
coverage gaps, operator order 2026-10-20). These assertions keep that true, keep every requirement hashed, keep
the locks covering what the reader and the API declare and build with, keep the bootstrap pins equal to the
reader tooling lock's and the lock toolchain equal to the documented one, and pin every installer-shaped line
of EVERY job of ci.yml (all six run security gates since the coverage-gap tranche): any line
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
API_BOOTSTRAP_LOCK = ROOT / "apps" / "api" / "requirements-bootstrap.txt"
API_LOCKTOOLS_LOCK = ROOT / "apps" / "api" / "requirements-locktools.txt"
TOOLCACHE_LOCK = ROOT / "apps" / "revision-reader" / "requirements-toolcache-pip.txt"
TOOLCACHE_IN = ROOT / "apps" / "revision-reader" / "requirements-toolcache-pip.in"
# Every installer-shaped line of every job, exactly as written in ci.yml (stripped).
INSTALLER_TOOLS = (r"pip[0-9.]*|pipx|uv|uvx|conda|mamba|micromamba|poetry|pdm|easy_install|hatch|rye|npm|npx|yarn|pnpm|corepack|gem|cargo|"
                   r"apt-get|apt|brew|snap|dpkg")
INSTALLER_LINE = re.compile(rf"(?:^|[\s;&|(`$\"'=]|/bin/)(?:{INSTALLER_TOOLS})(?=[\s)\"';&|]|$)"
                            r"|\s-m\s*(?:pip|ensurepip|venv|virtualenv|pipx|uv)\b|\bpip\._internal\b|\b(?:import|from)\s+pip\b"
                            r"|--upgrade-deps|setup\.py\s+(?:install|develop)|get-pip|\bgo\s+install\b|\bpip-install\s*:"
                            r"|\b(?:curl|wget)\b[^|\n]*\|\s*(?:ba|z)?sh", re.I)
API_VENV_INVENTORY = {
    "run: apps/api/.venv/bin/python -m pip inspect > security-reports/api-venv-installed-final.json",
}
INSTALL_PREFIX_PIP = "python3 -m pip install --isolated --require-hashes --only-binary :all: --no-deps -r apps/revision-reader/requirements-toolcache-pip.txt"
ALLOWED_INSTALLER_LINES = {
    "frontend-quality": {
        "cache: npm",
        "cache: pip",
        "run: npm run test:ci-pipefail",
        "npm ci 2>&1 | tee ci-logs/npm-ci.log",
        "run: npm run lint 2>&1 | tee ci-logs/frontend-lint.log",
        "run: npm run type-check 2>&1 | tee ci-logs/frontend-typecheck.log",
        "npm run test 2>&1 | tee ci-logs/frontend-test.log",
        "run: npm run build 2>&1 | tee ci-logs/frontend-build.log",
        "python -m venv --clear apps/api/.venv",
        "apps/api/.venv/bin/python -m pip install --isolated --require-hashes --only-binary :all: --no-deps -r apps/api/requirements-bootstrap.txt",
        "apps/api/.venv/bin/pip install --isolated --require-hashes --only-binary :all: -r apps/api/requirements-dev.txt",
        "apps/api/.venv/bin/pip install --no-deps --no-build-isolation -e apps/api",
        "apps/api/.venv/bin/python -m pip inspect > security-reports/api-venv-installed.json",
        "run: npm run e2e:install --workspace apps/web 2>&1 | tee ci-logs/frontend-e2e-install.log",
    } | API_VENV_INVENTORY,
    "backend-quality": {
        "cache: pip",
        "python -m venv --clear apps/api/.venv",
        "apps/api/.venv/bin/python -m pip install --isolated --require-hashes --only-binary :all: --no-deps -r apps/api/requirements-bootstrap.txt 2>&1 | tee ci-logs/pip-install.log",
        "apps/api/.venv/bin/pip install --isolated --require-hashes --only-binary :all: -r apps/api/requirements-dev.txt 2>&1 | tee -a ci-logs/pip-install.log",
        "apps/api/.venv/bin/pip install --no-deps --no-build-isolation -e apps/api 2>&1 | tee -a ci-logs/pip-install.log",
        "python -m venv --clear .locktools",
        ".locktools/bin/python -m pip install --isolated --quiet --require-hashes --only-binary :all: --no-deps -r apps/api/requirements-locktools.txt",
        ".locktools/bin/python -m pip check",
        ".locktools/bin/python -m pip --version",
        "apps/api/.venv/bin/python -m pip check",
        "apps/api/.venv/bin/python -m pip inspect > security-reports/api-venv-installed.json",
        ".locktools/bin/python -m pip inspect > security-reports/locktools-installed.json",
        "apps/api/.venv/bin/python -m pip inspect > security-reports/api-venv-installed-final.json",
        ".locktools/bin/python -m pip inspect > security-reports/locktools-installed-final.json",
    },
    "migration-and-contract": {
        "cache: npm",
        "cache: pip",
        "npm ci 2>&1 | tee ci-logs/npm-ci.log",
        "python -m venv --clear apps/api/.venv",
        "apps/api/.venv/bin/python -m pip install --isolated --require-hashes --only-binary :all: --no-deps -r apps/api/requirements-bootstrap.txt 2>&1 | tee ci-logs/pip-install.log",
        "apps/api/.venv/bin/pip install --isolated --require-hashes --only-binary :all: -r apps/api/requirements-dev.txt 2>&1 | tee -a ci-logs/pip-install.log",
        "apps/api/.venv/bin/pip install --no-deps --no-build-isolation -e apps/api 2>&1 | tee -a ci-logs/pip-install.log",
        "apps/api/.venv/bin/python -m pip inspect > security-reports/api-venv-installed.json",
        "npm run gen:types 2>&1 | tee ci-logs/gen-types.log",
    } | API_VENV_INVENTORY,
    "integration-smoke": {
        "cache: pip",
        "python -m venv --clear apps/api/.venv",
        "apps/api/.venv/bin/python -m pip install --isolated --require-hashes --only-binary :all: --no-deps -r apps/api/requirements-bootstrap.txt",
        "apps/api/.venv/bin/pip install --isolated --require-hashes --only-binary :all: -r apps/api/requirements-dev.txt",
        "apps/api/.venv/bin/pip install --no-deps --no-build-isolation -e apps/api",
        "apps/api/.venv/bin/python -m pip inspect > security-reports/api-venv-installed.json",
    } | API_VENV_INVENTORY,
    "container-build": {
        "- name: Inventory the security tooling venv as installed (pip inspect)",
        "cache: pip",
        "python3 -m venv --clear .security-venv",
        ".security-venv/bin/python -m pip install --isolated --quiet --require-hashes --only-binary :all: -r security/requirements-semgrep.txt",
        "run: .security-venv/bin/python -m pip inspect > security-reports/security-tooling-venv-installed.json",
        "run: .security-venv/bin/python -m pip inspect > security-reports/security-tooling-venv-installed-final.json",
    },
    "revision-reader": {
        "- name: Inventory the reader tooling environments as installed (pip inspect)",
        "- name: Install the locked pip into the job's interpreter prefix (replaces the toolcache's bundled pip)",
        "cache: pip",
        "python -m venv --clear .reader-venv",
        ".reader-venv/bin/python -m pip install --isolated --require-hashes --only-binary :all: --no-deps -r apps/revision-reader/requirements-dev.txt",
        ".reader-venv/bin/python -m pip install --no-deps --no-build-isolation -e apps/revision-reader",
        ".reader-venv/bin/python -m pip check",
        ".reader-venv/bin/pip install -r scripts/requirements-gate.txt",
        "python3 -m pip install --isolated --require-hashes --only-binary :all: --no-deps -r apps/revision-reader/requirements-toolcache-pip.txt",
        "python3 -m pip install --isolated --require-hashes --only-binary :all: --no-deps -r scripts/requirements-gate-hashes.txt",
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
        for path in (READER_LOCK, GATE_LOCK, API_BOOTSTRAP_LOCK, API_LOCKTOOLS_LOCK, TOOLCACHE_LOCK):
            with self.subTest(str(path.relative_to(ROOT))):
                entries = lock_entries(path)
                self.assertTrue(entries)
                for name, (version, hashes) in entries.items():
                    self.assertTrue(hashes, f"{name}=={version} has no hash")

    def test_the_gate_lock_carries_the_reader_locks_pyyaml_hashes(self):
        self.assertEqual(sorted(lock_entries(GATE_LOCK)["pyyaml"][1]), sorted(lock_entries(READER_LOCK)["pyyaml"][1]))
        self.assertEqual(set(lock_entries(GATE_LOCK)), {"pyyaml"})

    def test_the_toolcache_lock_is_pip_alone_at_the_reader_locks_pin_and_hashes(self):
        toolcache, reader = lock_entries(TOOLCACHE_LOCK), lock_entries(READER_LOCK)
        self.assertEqual(set(toolcache), {"pip"})
        self.assertEqual(toolcache["pip"][0], reader["pip"][0])
        self.assertEqual(sorted(toolcache["pip"][1]), sorted(reader["pip"][1]))
        self.assertEqual(pins(TOOLCACHE_IN), {"pip": toolcache["pip"][0]})

    def test_the_prefix_pip_is_installed_once_before_the_gate_pyyaml_and_the_first_prefix_inventory(self):
        body = job_text(WORKFLOW.read_text(encoding="utf-8"), "revision-reader")
        pip_install = INSTALL_PREFIX_PIP
        gate_install = "python3 -m pip install --isolated --require-hashes --only-binary :all: --no-deps -r scripts/requirements-gate-hashes.txt"   # the #233 form (block scalar, --isolated --only-binary :all:)
        inventory = "python3 -m pip inspect > security-reports/reader-tooling-syspy-installed.json"
        self.assertEqual(body.count(pip_install), 1)
        self.assertEqual(body.count("-r " + TOOLCACHE_LOCK.relative_to(ROOT).as_posix()), 1)
        self.assertLess(body.index(pip_install), body.index(gate_install))
        self.assertLess(body.index(gate_install), body.index(inventory))

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

    def test_the_api_bootstrap_lock_is_pip_and_the_build_backend_at_the_reader_locks_pins(self):
        boot, reader = lock_entries(API_BOOTSTRAP_LOCK), lock_entries(READER_LOCK)
        self.assertEqual(set(boot), {"pip", "setuptools"})
        for name in boot:
            with self.subTest(name):
                self.assertEqual(boot[name][0], reader[name][0])
                self.assertEqual(sorted(boot[name][1]), sorted(reader[name][1]))
        project = tomllib.loads((ROOT / "apps" / "api" / "pyproject.toml").read_text(encoding="utf-8"))
        for req in project["build-system"]["requires"]:
            self.assertIn(norm(re.match(r"[A-Za-z0-9._-]+", req).group(0)), boot, f"{req} (build-system) is not in the bootstrap lock")
        self.assertEqual(pins(ROOT / "apps" / "api" / "requirements-bootstrap.in"), {n: v for n, (v, _) in boot.items()})

    def test_the_lock_toolchain_lock_holds_the_documented_toolchain(self):
        tools = lock_entries(API_LOCKTOOLS_LOCK)
        documented = {"pip": "26.2.2", "pip-tools": "7.6.2"}           # README-deps.md and tools/verify_locks.py TOOLCHAIN (CERT-2)
        self.assertIn('TOOLCHAIN = {"pip": "26.2.2", "pip-tools": "7.6.2"}', (ROOT / "apps" / "api" / "tools" / "verify_locks.py").read_text(encoding="utf-8"))
        for name, version in documented.items():
            self.assertEqual(tools[name][0], version)
        self.assertEqual(pins(ROOT / "apps" / "api" / "requirements-locktools.in"), documented)
        self.assertIn("setuptools", tools, "verify_locks.py builds metadata with --no-build-isolation, so the build backend must be in the lock")

    def test_the_api_venv_and_lock_toolchain_installs_are_hash_locked_and_never_upgrade_unpinned(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("pip install --upgrade pip", text)
        self.assertNotIn('"pip==25.1.1" "pip-tools==7.5.2"', text)
        self.assertNotIn('"pip==26.2.2" "pip-tools==7.6.2"', text)
        self.assertNotIn("--no-deps -e apps/api\n", text)
        for job in ("frontend-quality", "backend-quality", "migration-and-contract", "integration-smoke"):
            with self.subTest(job):
                body = job_text(text, job)
                self.assertEqual(body.count("-r apps/api/requirements-bootstrap.txt"), 1)
                self.assertEqual(body.count("--no-deps --no-build-isolation -e apps/api"), 1)

    def test_every_hash_locked_install_refuses_source_builds(self):
        # A source build of a locked dependency fetches its build requirements unpinned and unhashed, runs them and deletes them,
        # where no inventory or scan sees them (round-3 finding A3R-1); --only-binary :all: makes such an install fail instead.
        text = WORKFLOW.read_text(encoding="utf-8")
        lines = [l.strip() for l in re.sub(r"\\\n\s*", " ", text).splitlines() if not l.strip().startswith("#")]
        hashed = [l for l in lines if "--require-hashes" in l]
        self.assertGreaterEqual(len(hashed), 12)
        for line in hashed:
            with self.subTest(line):
                self.assertIn("--only-binary :all:", line)
                self.assertIn("pip install --isolated ", line)   # no PIP_* variable or user configuration (round-4 A4R-2)
        for line in lines:     # no install from a lock or the index escapes the rule by dropping --require-hashes
            if re.search(r"\bpip install\b.*\s-r\s", line) and "--require-hashes" not in line:
                self.assertEqual(line, ".reader-venv/bin/pip install -r scripts/requirements-gate.txt")   # pinned no-op (module docstring)

    def test_every_lock_an_install_reads_is_only_pins_hashes_and_comments(self):
        # pip applies option lines of a requirements file after its command line, so a `--no-binary=x` continuation,
        # `--only-binary=:none:`, `-c`, `--index-url` and the like inside a lock would undo `--only-binary :all:` or
        # `--require-hashes` for the whole install (round-4 finding A4R-1). Every lock ci.yml or the API Dockerfile installs
        # from is read with gate.parse_lock's strict grammar; the set is derived from the files, so a new lock is covered too.
        import sys
        sys.path.insert(0, str(ROOT / "security"))
        import gate
        text = WORKFLOW.read_text(encoding="utf-8") + (ROOT / "apps" / "api" / "Dockerfile").read_text(encoding="utf-8")
        named = set(re.findall(r"(?<!\S)-r\s+(\S+\.txt)", text))
        locks = set()
        for rel in named:
            path = ROOT / rel if (ROOT / rel).is_file() else ROOT / "apps" / "api" / rel      # the Dockerfile's context is apps/api
            self.assertTrue(path.is_file(), rel)
            locks.add(path)
        self.assertEqual({str(p.relative_to(ROOT)) for p in locks}, {
            "apps/api/requirements-bootstrap.txt", "apps/api/requirements-dev.txt", "apps/api/requirements-runtime.txt",
            "apps/api/requirements-locktools.txt", "security/requirements-semgrep.txt", "apps/revision-reader/requirements-dev.txt",
            "scripts/requirements-gate-hashes.txt", "scripts/requirements-gate.txt", "apps/revision-reader/requirements-toolcache-pip.txt"})
        for path in sorted(locks):
            with self.subTest(str(path.relative_to(ROOT))):
                self.assertTrue(gate.parse_lock(path, hashed=path.name != "requirements-gate.txt"))

    def test_the_strict_lock_grammar_refuses_every_option_shape(self):
        import sys, tempfile
        sys.path.insert(0, str(ROOT / "security"))
        import gate
        h = "    --hash=sha256:" + "ab" * 32
        good = f"# header\nfoo==1.0 \\\n{h} \\\n{h}\n    # via bar\n"
        shapes = {
            "option continuation": f"foo==1.0 \\\n    --no-binary=foo \\\n{h}\n",
            "indented option line": f"foo==1.0 \\\n{h}\n    --no-binary foo\n",
            "top-level option line": f"--only-binary=:none:\nfoo==1.0 \\\n{h}\n",
            "constraint line": f"-c other.txt\nfoo==1.0 \\\n{h}\n",
            "index line": f"--index-url https://example.invalid/simple\nfoo==1.0 \\\n{h}\n",
            "url requirement": f"foo @ https://example.invalid/foo.tar.gz \\\n{h}\n",
            "option after a hash": f"foo==1.0 \\\n{h} --no-binary=foo\n",
            "unhashed pin": "foo==1.0\n",
            "unterminated": f"foo==1.0 \\\n{h} \\\n",
            "duplicate": f"foo==1.0 \\\n{h}\nfoo==1.0 \\\n{h}\n",
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = pathlib.Path(tmp) / "lock.txt"
            path.write_text(good, encoding="utf-8")
            self.assertEqual(gate.parse_lock(path), {"foo": "1.0"})
            for label, text in shapes.items():
                with self.subTest(label):
                    path.write_text(text, encoding="utf-8")
                    with self.assertRaises(gate.ReportError):
                        gate.parse_lock(path)

    def test_the_lock_toolchain_lock_holds_exactly_the_toolchain_closure(self):
        # An extra name in the lock toolchain lock would be installed into .locktools from a wheel and scanned, but nothing else
        # needs it there; keep the set to pip-tools' closure (round-4 finding A4R-1, the extra-pin part).
        self.assertEqual(set(lock_entries(API_LOCKTOOLS_LOCK)),
                         {"build", "click", "packaging", "pip", "pip-tools", "pyproject-hooks", "setuptools", "wheel"})

    def test_no_repository_module_shadows_what_the_installs_run_with_python_m(self):
        # `python -m venv`, `-m ensurepip` (which venv runs) and `-m pip` run at the workspace root, where `-m` puts the working
        # directory first on sys.path: a tracked top-level pip, venv or ensurepip would run instead (round-4 finding A4R-3).
        for name in ("pip", "venv", "ensurepip", "piptools"):
            for entry in (name, f"{name}.py"):
                with self.subTest(entry):
                    self.assertFalse((ROOT / entry).exists(), f"a top-level {entry} would shadow `python -m {name}`")

    def test_the_api_project_builds_only_with_the_locked_setuptools_backend(self):
        # The editable install runs the project's build backend inside the install step, before `readonly` (round-3 finding A3R-3):
        # keep it the locked setuptools, with no in-tree backend and no setup.py/setup.cfg that would run repository code there.
        project = tomllib.loads((ROOT / "apps" / "api" / "pyproject.toml").read_text(encoding="utf-8"))
        self.assertEqual(project["build-system"]["build-backend"], "setuptools.build_meta")
        self.assertNotIn("backend-path", project["build-system"])
        for name in ("setup.py", "setup.cfg"):
            self.assertFalse((ROOT / "apps" / "api" / name).exists(), f"apps/api/{name} would run inside the editable install")
        # pyproject-only routes to repository code inside the build (round-4 finding A4R-3): setuptools imports a cmdclass
        # module, and resolves `attr:`/`file:` dynamic fields from the project's own files.
        setuptools_table = project.get("tool", {}).get("setuptools", {})
        self.assertNotIn("cmdclass", setuptools_table)
        self.assertNotIn("dynamic", setuptools_table)
        self.assertNotIn("dynamic", project["project"])

    def test_the_scanned_jobs_run_only_the_reviewed_installer_lines(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        jobs = set(re.findall(r"(?m)^  ([a-z0-9-]+):$", text.split("\njobs:\n", 1)[1]))
        self.assertEqual(jobs, set(ALLOWED_INSTALLER_LINES), "every job of ci.yml has its installer lines pinned")
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
