"""Regression tests for tools/verify_locks.py, run by CI with the lock toolchain interpreter::

    cd apps/api && ../../.locktools/bin/python -m unittest discover -s tools -p "test_*.py" -v

Every case runs the real entry point (``verify_locks.py`` as a subprocess) against a fixture
project whose locks were produced by pip-compile itself, resolving offline from a local
directory of generated wheels and sdists (``--no-index --find-links``). Nothing reaches a
network index, and the repository's real locks are never read or written here.
"""

from __future__ import annotations

import gzip
import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERIFY = HERE / "verify_locks.py"
sys.path.insert(0, str(HERE))
import verify_locks as vl  # noqa: E402

FIXED_TIME = (2020, 1, 1, 0, 0, 0)

PYPROJECT = """\
[project]
name = "fixture-api"
version = "0.0.0"
requires-python = ">=3.11"
dependencies = ["alpha>=1.0"]

[project.optional-dependencies]
full = ["delta>=1.0"]
dev = ["gamma>=1.0", "delta>=1.0"]

[build-system]
requires = ["setuptools>=68"]
build-backend = "setuptools.build_meta"

[tool.setuptools]
py-modules = []
"""


def make_wheel(index: Path, name: str, version: str, requires: tuple[str, ...] = ()) -> None:
    dist = f"{name}-{version}.dist-info"
    files = {
        f"{name}/__init__.py": f'__version__ = "{version}"\n',
        f"{dist}/METADATA": (f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n"
                             + "".join(f"Requires-Dist: {r}\n" for r in requires)),
        f"{dist}/WHEEL": ("Wheel-Version: 1.0\nGenerator: verify-locks-tests\n"
                          "Root-Is-Purelib: true\nTag: py3-none-any\n"),
    }
    files[f"{dist}/RECORD"] = "".join(f"{p},,\n" for p in files) + f"{dist}/RECORD,,\n"
    with zipfile.ZipFile(index / f"{name}-{version}-py3-none-any.whl", "w") as archive:
        for path, text in files.items():
            archive.writestr(zipfile.ZipInfo(path, FIXED_TIME), text)


def make_sdist(index: Path, name: str, version: str) -> None:
    """A second artifact for the same version, so that pin carries two hashes."""
    payload = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    raw = io.BytesIO()
    with tarfile.open(fileobj=raw, mode="w") as archive:
        info = tarfile.TarInfo(f"{name}-{version}/PKG-INFO")
        info.size, info.mtime = len(payload), 0
        archive.addfile(info, io.BytesIO(payload))
    with gzip.GzipFile(index / f"{name}-{version}.tar.gz", "wb", mtime=0) as out:
        out.write(raw.getvalue())


def offline_args(index: Path, cache: Path) -> list[str]:
    return ["--no-index", f"--find-links={index}", "--no-emit-find-links",
            f"--cache-dir={cache}"]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def piptools_compile(project: Path, args: list[str], env: dict[str, str] | None = None) -> None:
    """Plain pip-compile, as maintenance would run it (fixture generation and controls)."""
    subprocess.run([sys.executable, "-m", "piptools", "compile", "--quiet", *args],
                   cwd=project, check=True, capture_output=True, text=True,
                   env=vl.compile_environment() if env is None else env)


class VerifyLocksTests(unittest.TestCase):
    """Committed locks pin alpha 1.0 (+ beta 1.0), delta 1.0 and, in the dev lock, gamma 1.0."""

    @classmethod
    def setUpClass(cls) -> None:
        problems = vl.check_toolchain()
        if problems:
            raise RuntimeError("run these tests with the pinned lock toolchain: "
                               + "; ".join(problems))
        cls._root = Path(tempfile.mkdtemp(prefix="verify-locks-tests-"))
        index, project = cls._root / "index", cls._root / "project"
        index.mkdir()
        project.mkdir()
        make_wheel(index, "alpha", "1.0", ("beta>=1.0",))
        make_wheel(index, "beta", "1.0")
        make_sdist(index, "beta", "1.0")
        make_wheel(index, "gamma", "1.0")
        make_wheel(index, "delta", "1.0")
        make_wheel(index, "epsilon", "1.0")
        (project / "pyproject.toml").write_text(PYPROJECT, encoding="utf-8")
        args = ["--no-config", *offline_args(index, cls._root / "cache-generate")]
        # Generate the "committed" locks exactly as maintenance would (fresh, no existing pins).
        piptools_compile(project, ["--generate-hashes", "--strip-extras", "--extra", "full",
                                   *args, "--output-file", vl.RUNTIME, "pyproject.toml"])
        piptools_compile(project, ["--generate-hashes", "--strip-extras", "--extra", "full",
                                   "--extra", "dev", "--constraint", vl.RUNTIME, *args,
                                   "--output-file", vl.DEV, "pyproject.toml"])

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls._root, ignore_errors=True)

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="case-", dir=self._root))
        self.index = self.tmp / "index"
        self.project = self.tmp / "project"
        shutil.copytree(self._root / "index", self.index)
        shutil.copytree(self._root / "project", self.project)
        self.cache = self.tmp / "cache"

    # ------------------------------------------------------------------ helpers
    def lock(self, name: str) -> Path:
        return self.project / name

    def run_verifier(self, *extra: str,
                     env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        command = [sys.executable, str(VERIFY), "--project-dir", str(self.project)]
        command += [f"--compile-arg={a}" for a in offline_args(self.index, self.cache)]
        command += list(extra)
        return subprocess.run(command, capture_output=True, text=True, check=False, env=env)

    def replace_entry(self, path: Path, name: str, version: str, digests: list[str]) -> None:
        """Replace one package's pin and hash lines, keeping its annotation lines."""
        out, skipping = [], False
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            if line.startswith(f"{name}=="):
                skipping = True
                out.append(f"{name}=={version} \\\n")
                tails = [" \\"] * (len(digests) - 1) + [""]
                out += [f"    --hash=sha256:{d}{tail}\n"
                        for d, tail in zip(digests, tails, strict=True)]
                continue
            if skipping and line.startswith("    --hash="):
                continue
            skipping = False
            out.append(line)
        path.write_text("".join(out), encoding="utf-8")

    def edit(self, path: Path, old: str, new: str) -> None:
        text = path.read_text(encoding="utf-8")
        self.assertEqual(text.count(old), 1, f"fixture edit anchor {old!r} in {path.name}")
        path.write_text(text.replace(old, new), encoding="utf-8")

    def drop_block(self, path: Path, name: str) -> None:
        """Remove one package's pin, hash and annotation lines from a lock."""
        out, skipping = [], False
        for line in path.read_text(encoding="utf-8").splitlines(keepends=True):
            if line.startswith(f"{name}=="):
                skipping = True
                continue
            if skipping and (line.startswith(" ") or not line.strip()):
                continue
            skipping = False
            out.append(line)
        path.write_text("".join(out), encoding="utf-8")

    def hashes_of(self, path: Path, name: str) -> list[str]:
        return next(p.hashes for p in vl.parse_lock(path) if p.name == name)

    def assert_fails(self, proc: subprocess.CompletedProcess[str], *needles: str) -> None:
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        for needle in needles:
            self.assertIn(needle, proc.stdout)

    # ------------------------------------------------------------------ fixture sanity
    def test_fixture_locks_are_what_the_verifier_expects(self) -> None:
        runtime = {p.name: p for p in vl.parse_lock(self.lock(vl.RUNTIME))}
        dev = {p.name: p for p in vl.parse_lock(self.lock(vl.DEV))}
        self.assertEqual({n: p.version for n, p in runtime.items()},
                         {"alpha": "1.0", "beta": "1.0", "delta": "1.0"})
        self.assertEqual({n: p.version for n, p in dev.items()},
                         {"alpha": "1.0", "beta": "1.0", "delta": "1.0", "gamma": "1.0"})
        self.assertEqual(len(runtime["beta"].hashes), 2)  # wheel + sdist
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    # ------------------------------------------------------------------ the incident
    def test_newer_compatible_release_does_not_change_the_verified_graph(self) -> None:
        make_wheel(self.index, "alpha", "1.1", ("beta>=1.0",))
        make_wheel(self.index, "gamma", "1.1")
        before = {n: sha256(self.lock(n)) for n in (vl.RUNTIME, vl.DEV)}
        # Positive control: an unconstrained fresh resolve (the predecessor check) now picks
        # the newer release, so this fixture really contains the incident's trigger.
        fresh = self.tmp / "fresh-runtime.txt"
        proc = vl.compile_lock(self.project, fresh, ("full",), None,
                               offline_args(self.index, self.cache))
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("alpha==1.1", fresh.read_text(encoding="utf-8"))
        # The repaired check keeps the committed pins and passes.
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn(f"{vl.RUNTIME}: verified", proc.stdout)
        self.assertIn(f"{vl.DEV}: verified", proc.stdout)
        self.assertEqual(before, {n: sha256(self.lock(n)) for n in (vl.RUNTIME, vl.DEV)})

    # ------------------------------------------------------------------ real defects
    def test_incompatible_declared_requirement_fails(self) -> None:
        make_wheel(self.index, "alpha", "1.1", ("beta>=1.0",))
        self.edit(self.project / "pyproject.toml", '"alpha>=1.0"', '"alpha>=1.1"')
        self.assert_fails(self.run_verifier(), f"LOCK MISMATCH: {vl.RUNTIME}",
                          "version: alpha committed 1.0, resolved 1.1")

    def test_incompatible_dev_requirement_fails(self) -> None:
        make_wheel(self.index, "gamma", "1.1")
        self.edit(self.project / "pyproject.toml", '"gamma>=1.0"', '"gamma>=1.1"')
        proc = self.run_verifier()
        self.assert_fails(proc, f"LOCK MISMATCH: {vl.DEV}",
                          "version: gamma committed 1.0, resolved 1.1")
        self.assertIn(f"{vl.RUNTIME}: verified", proc.stdout)

    def test_unsatisfiable_declared_requirement_fails(self) -> None:
        self.edit(self.project / "pyproject.toml", '"alpha>=1.0"', '"alpha>=9"')
        self.assert_fails(self.run_verifier(), f"LOCK UNRESOLVABLE: {vl.RUNTIME}",
                          f"{vl.DEV}: not verified")

    def test_missing_required_dependency_fails(self) -> None:
        for name in (vl.RUNTIME, vl.DEV):
            self.drop_block(self.lock(name), "beta")
        self.assert_fails(self.run_verifier(), "missing from the committed lock: beta==1.0")

    def test_corrupted_hash_fails(self) -> None:
        digest = self.hashes_of(self.lock(vl.RUNTIME), "alpha")[0]
        flipped = digest[:-1] + ("0" if digest[-1] != "0" else "1")
        self.edit(self.lock(vl.RUNTIME), digest, flipped)
        self.assert_fails(self.run_verifier(), f"LOCK MISMATCH: {vl.RUNTIME}",
                          "hashes: alpha==1.0 committed-only 1, index-only 1")

    def test_missing_one_of_several_hashes_fails(self) -> None:
        lock = self.lock(vl.RUNTIME)
        first, second = sorted(self.hashes_of(lock, "beta"))
        self.edit(lock, f"    --hash=sha256:{first} \\\n    --hash=sha256:{second}\n",
                  f"    --hash=sha256:{second}\n")
        self.assertEqual(self.hashes_of(lock, "beta"), [second])
        self.assert_fails(self.run_verifier(), "hashes: beta==1.0 committed-only 0, index-only 1")

    def test_pin_without_any_hash_fails(self) -> None:
        lock = self.lock(vl.RUNTIME)
        (digest,) = self.hashes_of(lock, "delta")
        self.edit(lock, f"delta==1.0 \\\n    --hash=sha256:{digest}\n", "delta==1.0\n")
        self.assert_fails(self.run_verifier(), "LOCK INVALID", "'delta==1.0'")

    # ------------------------------------------------------------------ the dev lock path
    def test_dev_lock_diverging_from_runtime_fails(self) -> None:
        # CI installs the dev lock and production the runtime lock: the dev lock is compiled
        # against the runtime pins, so a dev pin that differs from the runtime pin must fail.
        make_wheel(self.index, "alpha", "1.1", ("beta>=1.0",))
        digest = sha256(self.index / "alpha-1.1-py3-none-any.whl")
        self.replace_entry(self.lock(vl.DEV), "alpha", "1.1", [digest])
        proc = self.run_verifier()
        self.assert_fails(proc, f"LOCK MISMATCH: {vl.DEV}",
                          "version: alpha committed 1.1, resolved 1.0")
        self.assertIn(f"{vl.RUNTIME}: verified", proc.stdout)

    def test_dev_only_unsatisfiable_requirement_fails(self) -> None:
        self.edit(self.project / "pyproject.toml", '"gamma>=1.0"', '"gamma>=9"')
        proc = self.run_verifier()
        self.assert_fails(proc, f"LOCK UNRESOLVABLE: {vl.DEV}")
        self.assertIn(f"{vl.RUNTIME}: verified", proc.stdout)

    def test_dev_only_pin_without_hash_fails(self) -> None:
        lock = self.lock(vl.DEV)
        (digest,) = self.hashes_of(lock, "gamma")
        self.edit(lock, f"gamma==1.0 \\\n    --hash=sha256:{digest}\n", "gamma==1.0\n")
        proc = self.run_verifier()
        self.assert_fails(proc, "LOCK INVALID", "'gamma==1.0'")
        self.assertIn(f"{vl.RUNTIME}: verified", proc.stdout)

    def test_cli_refuses_a_different_toolchain(self) -> None:
        # setUpClass checks the toolchain itself, so this drives the CLI check directly.
        code = ("import sys; sys.path.insert(0, sys.argv[1]); import verify_locks as v; "
                "v.TOOLCHAIN['pip'] = '0.0.0'; sys.exit(v.main(['--project-dir', sys.argv[2]]))")
        proc = subprocess.run([sys.executable, "-c", code, str(HERE), str(self.project)],
                              capture_output=True, text=True, check=False)
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("TOOLCHAIN MISMATCH: pip 25.1.1 (required 0.0.0)", proc.stdout)

    # ------------------------------------------------------------------ configuration isolation
    def test_project_pip_tools_config_cannot_turn_verification_into_an_upgrade(self) -> None:
        make_wheel(self.index, "alpha", "1.1", ("beta>=1.0",))
        (self.project / ".pip-tools.toml").write_text("[tool.pip-tools]\nupgrade = true\n",
                                                      encoding="utf-8")
        # Positive control: pip-compile WITH config reading, given the committed pins, upgrades.
        seeded = self.tmp / "seeded-runtime.txt"
        seeded.write_text(vl.pin_list(vl.parse_lock(self.lock(vl.RUNTIME))), encoding="utf-8")
        piptools_compile(self.project, ["--generate-hashes", "--strip-extras", "--extra", "full",
                                        *offline_args(self.index, self.cache),
                                        "--output-file", str(seeded), "pyproject.toml"])
        self.assertIn("alpha==1.1", seeded.read_text(encoding="utf-8"))
        proc = self.run_verifier()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_pip_environment_cannot_add_a_package_source(self) -> None:
        vendor = self.tmp / "vendor"
        vendor.mkdir()
        make_wheel(vendor, "alpha", "1.0", ("beta>=1.0", "beta<9"))  # different bytes, same version
        extra = sha256(vendor / "alpha-1.0-py3-none-any.whl")
        lock = self.lock(vl.RUNTIME)
        self.replace_entry(lock, "alpha", "1.0", sorted([*self.hashes_of(lock, "alpha"), extra]))
        conf = self.tmp / "pip.conf"
        conf.write_text(f"[global]\nfind-links = {vendor}\n", encoding="utf-8")
        for variable, value in (("PIP_FIND_LINKS", str(vendor)), ("PIP_CONFIG_FILE", str(conf))):
            with self.subTest(variable=variable):
                env = {**os.environ, variable: value}
                # Positive control: with that environment, plain pip-compile sees both files.
                out = self.tmp / f"control-{variable}.txt"
                piptools_compile(self.project, [
                    "--no-config", "--generate-hashes", "--strip-extras", "--extra", "full",
                    *offline_args(self.index, self.cache), "--output-file", str(out),
                    "pyproject.toml"], env=env)
                self.assertIn(extra, out.read_text(encoding="utf-8"))
                self.assert_fails(self.run_verifier(env=env),
                                  "hashes: alpha==1.0 committed-only 1, index-only 0")

    def test_removed_dependency_is_not_retained_by_its_old_pin(self) -> None:
        self.edit(self.project / "pyproject.toml", 'full = ["delta>=1.0"]', "full = []")
        self.edit(self.project / "pyproject.toml", 'dev = ["gamma>=1.0", "delta>=1.0"]',
                  'dev = ["gamma>=1.0"]')
        proc = self.run_verifier()
        self.assert_fails(proc, f"LOCK MISMATCH: {vl.RUNTIME}", f"LOCK MISMATCH: {vl.DEV}",
                          "only in the committed lock: delta==1.0")

    def test_unrequired_extra_pin_is_not_retained(self) -> None:
        # A correctly hashed pin that nothing requires: supplying it as an existing pin must
        # not make pip-tools emit it.
        digest = sha256(self.index / "epsilon-1.0-py3-none-any.whl")
        lock = self.lock(vl.RUNTIME)
        lock.write_text(lock.read_text(encoding="utf-8")
                        + f"epsilon==1.0 \\\n    --hash=sha256:{digest}\n", encoding="utf-8")
        self.assert_fails(self.run_verifier(), "only in the committed lock: epsilon==1.0")

    # ------------------------------------------------------------------ the check itself
    def test_upgrade_and_hash_reuse_options_are_refused(self) -> None:
        for arg in ("--upgrade", "-U", "-qU", "--upgrade-package=alpha", "-Palpha",
                    "--reuse-hashes", "--output-file=x.txt", "-ox.txt", "--config=x.toml",
                    "--pip-args=--isolated", "--pre", "--index-url=https://example.invalid",
                    "--no-build-isolation", "--find-links", "--cache-dir=", "--no-config"):
            with self.subTest(arg=arg):
                proc = self.run_verifier(f"--compile-arg={arg}")
                self.assertEqual(proc.returncode, 2, proc.stdout)
                self.assertIn("REFUSED", proc.stdout)

    def test_committed_hashes_never_reach_pip_tools(self) -> None:
        pins = vl.parse_lock(self.lock(vl.DEV))
        self.assertNotIn("--hash", vl.pin_list(pins))
        self.assertEqual(vl.pin_list(pins).splitlines(),
                         [f"{p.name}=={p.version}" for p in pins])

    def test_committed_locks_are_never_written(self) -> None:
        self.edit(self.project / "pyproject.toml", '"alpha>=1.0"', '"alpha>=9"')
        before = {n: sha256(self.lock(n)) for n in (vl.RUNTIME, vl.DEV)}
        self.run_verifier()
        self.assertEqual(before, {n: sha256(self.lock(n)) for n in (vl.RUNTIME, vl.DEV)})


class ParseLockTests(unittest.TestCase):
    def parse(self, text: str) -> list[vl.Pin]:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lock.txt"
            path.write_text(text, encoding="utf-8")
            return vl.parse_lock(path)

    def test_unpinned_requirement_is_refused(self) -> None:
        with self.assertRaisesRegex(vl.LockError, "not an exact pin"):
            self.parse("alpha>=1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")

    def test_option_line_is_refused(self) -> None:
        with self.assertRaisesRegex(vl.LockError, "not an exact pin"):
            self.parse("--index-url https://example.invalid/simple\n")

    def test_duplicate_pin_is_refused(self) -> None:
        entry = "alpha==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n"
        with self.assertRaisesRegex(vl.LockError, "more than once"):
            self.parse(entry + entry)

    def test_comment_inside_an_entry_is_refused(self) -> None:
        h = "    --hash=sha256:"
        for text in ("alpha==1.0 \\\n" + h + "a" * 64 + " \\\n    # via x\n" + h + "b" * 64 + "\n",
                     "alpha==1.0 \\\n    # via x\n" + h + "a" * 64 + "\n",
                     "alpha==1.0 \\\n\n" + h + "a" * 64 + "\n"):
            with self.subTest(text=text), self.assertRaisesRegex(vl.LockError, "expected a sha256"):
                self.parse(text)

    def test_unclosed_or_hashless_entries_are_refused(self) -> None:
        with self.assertRaisesRegex(vl.LockError, "never closed"):
            self.parse("alpha==1.0 \\\n    --hash=sha256:" + "a" * 64 + " \\\n")
        with self.assertRaisesRegex(vl.LockError, "expected a sha256 hash line continuing alpha"):
            self.parse("alpha==1.0 \\\nbeta==1.0 \\\n    --hash=sha256:" + "a" * 64 + "\n")
        with self.assertRaisesRegex(vl.LockError, "not an exact pin"):
            self.parse("alpha==1.0\n")

    def test_pre_release_dev_and_inexact_versions_are_refused(self) -> None:
        tail = " \\\n    --hash=sha256:" + "a" * 64 + "\n"
        for pin, message in (("alpha==1.2rc1", "pre-release"),
                             ("alpha==1.2.dev3", "pre-release"),
                             ("alpha==1.*", "not an exact pin"),
                             ("alpha===1.0", "not an exact pin"),
                             ("alpha==1.0,<2", "not an exact pin"),
                             ('alpha==1.0 ; python_version >= "3.0"', "not an exact pin")):
            with self.subTest(pin=pin), self.assertRaisesRegex(vl.LockError, message):
                self.parse(pin + tail)

    def test_comments_and_hashes_are_parsed(self) -> None:
        pins = self.parse("# header\nalpha==1.0 \\\n    --hash=sha256:" + "a" * 64
                          + " \\\n    --hash=sha256:" + "b" * 64 + "\n    # via x\n")
        self.assertEqual([(p.name, p.version, len(p.hashes)) for p in pins], [("alpha", "1.0", 2)])


if __name__ == "__main__":
    unittest.main()
