"""Assertions over security/stage_inputs.py (P6-CI-6 coverage gaps, operator order 2026-10-20).

stage_inputs.py stages what two scans cannot read in place: the pins an installed pip declares it vendors
(pip/_vendor/vendor.txt), for a Trivy filesystem scan, and copies of the binaries a job executes, with their
origin and digest, for a Go binary analysis. Inert synthetic environments only; no scanner or installer runs.
Run: python3 -m unittest discover -s security
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402
import stage_inputs  # noqa: E402

SITE = "lib/python3.12/site-packages"


def env_with(root: pathlib.Path, dists: list, vendor: str | None) -> pathlib.Path:
    """An environment holding the dist-info dirs of `dists`, pip's vendor.txt when given, and its inventory."""
    for name, version in dists:
        (root / SITE / f"{name}-{version}.dist-info").mkdir(parents=True)
    if vendor is not None:
        (root / SITE / "pip" / "_vendor").mkdir(parents=True)
        (root / SITE / "pip" / "_vendor" / "vendor.txt").write_text(vendor, encoding="utf-8")
    inv = root.parent / f"{root.name}-installed.json"
    inv.write_text(json.dumps({"version": "1", "installed": [
        {"metadata": {"name": n, "version": v}, "metadata_location": str(root / SITE / f"{n}-{v}.dist-info")} for n, v in dists]}), encoding="utf-8")
    return inv


class Staging(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, *argv):
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            code = stage_inputs.main(list(argv))
        return code, out.getvalue() + err.getvalue()

    def test_vendored_pins_are_staged_per_environment_and_an_environment_without_pip_gets_none(self):
        a = env_with(self.dir / "a", [("pip", "25.1.1"), ("click", "8.5.0")], "CacheControl==0.14.2\n    certifi==2025.1.31\n# c\n")
        b = env_with(self.dir / "b", [("click", "8.5.0")], None)
        out = self.dir / "staged"
        code, text = self.run_main("vendored", "--out", str(out), "--env", "a", str(self.dir / "a"), str(a), "--env", "b", str(self.dir / "b"), str(b))
        self.assertEqual(code, 0, text)
        staged = (out / "a" / stage_inputs.VENDORED_FILE).read_text(encoding="utf-8").splitlines()
        self.assertEqual([l for l in staged if not l.startswith("#")], ["cachecontrol==0.14.2", "certifi==2025.1.31"])
        self.assertFalse((out / "b").exists())
        self.assertIn("b: no pip installed", text)

    def test_staging_fails_closed(self):
        a = env_with(self.dir / "a", [("pip", "25.1.1")], None)                     # pip without vendor.txt
        code, text = self.run_main("vendored", "--out", str(self.dir / "s1"), "--env", "a", str(self.dir / "a"), str(a))
        self.assertEqual(code, gate.EXIT_REPORT); self.assertIn("vendor.txt is absent", text)
        c = env_with(self.dir / "c", [("pip", "26.2.1")], "requests>=2\n")         # not an exact pin
        code, _ = self.run_main("vendored", "--out", str(self.dir / "s2"), "--env", "c", str(self.dir / "c"), str(c))
        self.assertEqual(code, gate.EXIT_REPORT)
        d = env_with(self.dir / "d", [("pip", "26.2.1")], "requests==2.32.3\n")
        (self.dir / "s3").mkdir()                                                    # an existing (possibly stale) directory
        code, text = self.run_main("vendored", "--out", str(self.dir / "s3"), "--env", "d", str(self.dir / "d"), str(d))
        self.assertEqual(code, gate.EXIT_REPORT); self.assertIn("already exists", text)
        for label in ("d", "../d", ".d"):
            with self.subTest(label):
                code, _ = self.run_main("vendored", "--out", str(self.dir / f"s4{len(label)}"), "--env", label, str(self.dir / "d"), str(d),
                                        "--env", "d", str(self.dir / "d"), str(d))
                self.assertEqual(code, gate.EXIT_REPORT)
        code, _ = self.run_main("vendored", "--out", str(self.dir / "s5"))
        self.assertEqual(code, gate.EXIT_USAGE)

    def test_binaries_are_copied_from_path_with_their_origin_and_digest(self):
        bindir = self.dir / "bin"; bindir.mkdir()
        for name in ("trivy", "tofu"):
            (bindir / name).write_bytes(f"#!/bin/sh\necho {name}\n".encode()); (bindir / name).chmod(0o755)
        pins = self.dir / "pins.json"
        pins.write_text(json.dumps({"jobs": {"j": ["trivy", "tofu"], "k": ["nothere"]}, "binaries": {
            n: {"sha256": "ab" * 32, "module": "m", "module_version": "v1"} for n in ("trivy", "tofu", "nothere")}}), encoding="utf-8")
        saved = os.environ["PATH"]
        os.environ["PATH"] = f"{bindir}{os.pathsep}{saved}"
        try:
            out, manifest = self.dir / "stage", self.dir / "m.json"
            code, text = self.run_main("binaries", "--pins", str(pins), "--job", "j", "--out", str(out), "--manifest", str(manifest))
            self.assertEqual(code, 0, text)
            record = json.loads(manifest.read_text(encoding="utf-8"))["binaries"]
            for name in ("trivy", "tofu"):
                self.assertEqual(record[name]["origin"], str((bindir / name).resolve()))
                self.assertEqual(record[name]["sha256"], hashlib.sha256((bindir / name).read_bytes()).hexdigest())
                self.assertEqual((out / name).read_bytes(), (bindir / name).read_bytes())
                self.assertTrue(os.access(out / name, os.X_OK), "the copy keeps the executable mode Trivy's binary analysers require")
            code, text = self.run_main("binaries", "--pins", str(pins), "--job", "k", "--out", str(self.dir / "stage2"), "--manifest", str(manifest))
            self.assertEqual(code, gate.EXIT_REPORT); self.assertIn("not on PATH", text)
            code, _ = self.run_main("binaries", "--pins", str(pins), "--job", "unknown", "--out", str(self.dir / "stage3"), "--manifest", str(manifest))
            self.assertEqual(code, gate.EXIT_REPORT)
        finally:
            os.environ["PATH"] = saved

    def test_readonly_removes_every_write_permission_and_leaves_links_alone(self):
        env = self.dir / "venv"
        env_with(env, [("pip", "26.2.1")], "requests==2.32.3\n")
        (env / "lib64").symlink_to("lib")
        (env / "bin").mkdir(); (env / "bin" / "python").write_text("x", encoding="utf-8")
        try:
            code, text = self.run_main("readonly", str(env))
            self.assertEqual(code, 0, text)
            for here, dirs, files in os.walk(env):
                for name in dirs + files:
                    path = os.path.join(here, name)
                    if not os.path.islink(path):
                        self.assertEqual(os.lstat(path).st_mode & 0o222, 0, path)
            self.assertEqual(os.lstat(env).st_mode & 0o222, 0)
            self.assertTrue((env / "lib64").is_symlink())
            with self.assertRaises(PermissionError):           # an ordinary later write fails
                (env / "lib" / "late-install.txt").write_text("x", encoding="utf-8")
        finally:
            for here, dirs, files in os.walk(env):
                os.chmod(here, 0o755)
                for name in files:
                    if not os.path.islink(os.path.join(here, name)):
                        os.chmod(os.path.join(here, name), 0o644)
        code, _ = self.run_main("readonly", str(self.dir / "missing"))
        self.assertEqual(code, gate.EXIT_REPORT)

    def test_the_real_pins_name_each_jobs_binaries(self):
        doc = json.loads((HERE / "scanner-binaries.json").read_text(encoding="utf-8"))
        for job in doc["jobs"]:
            with self.subTest(job):
                self.assertEqual(list(gate.load_binary_pins(HERE / "scanner-binaries.json", job)), doc["jobs"][job])


if __name__ == "__main__":
    unittest.main()
