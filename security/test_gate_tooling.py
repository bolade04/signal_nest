"""Assertions over `security/gate.py vuln --kind tooling` (P6-CI-6 closure, operator order 2026-10-19).

An installed tooling environment (a CI virtual environment or interpreter prefix) is scanned AS
INSTALLED with Trivy rootfs and reconciled against the environment's own `pip inspect` inventory:
every installed distribution must appear in the scan at its own metadata path with the same name
and version. These tests build inert, synthetic inventories and reports of the shapes `pip inspect`
and Trivy emit; no scanner or installer runs. Run: python3 -m unittest discover -s security
"""
from __future__ import annotations

import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402

POLICY = HERE / "policy.json"
SITE = "lib/python3.12/site-packages"
DISTS = [("pip", "26.2.1"), ("pytest", "8.3.3"), ("PyYAML", "6.0.3"), ("typing_extensions", "4.16.0")]


def info_dir(name: str, version: str) -> str:
    return f"{SITE}/{name.replace('-', '_')}-{version}.dist-info"


def inventory(env: pathlib.Path, dists=DISTS) -> dict:
    return {"version": "1", "pip_version": "26.2.1", "environment": {"sys_platform": "linux"},
            "installed": [{"metadata": {"name": n, "version": v}, "metadata_location": str(env / info_dir(n, v))} for n, v in dists]}


def report(artifact: str, dists=DISTS, vulns=(), extra=()) -> dict:
    pkgs = [{"Name": n, "Version": v, "FilePath": f"{info_dir(n, v)}/METADATA", "Identifier": {"UID": f"u-{n}"}} for n, v in dists]
    pkgs += list(extra)
    return {"SchemaVersion": 2, "ArtifactName": artifact, "ArtifactType": "filesystem",
            "Results": [{"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg", "Packages": pkgs, "Vulnerabilities": list(vulns)}]}


def vuln(vid: str, name: str, fixed: str, severity: str) -> dict:
    return {"VulnerabilityID": vid, "PkgName": name, "InstalledVersion": "x", "FixedVersion": fixed, "Severity": severity,
            "Status": "fixed" if fixed else "affected", "PkgIdentifier": {"UID": f"u-{name}"}}


class ToolingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.env = self.dir / ".reader-venv"
        self.env.mkdir()
        self.reports = self.dir / "security-reports"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, doc):
        p = self.dir / name
        p.write_text(json.dumps(doc), encoding="utf-8")
        return str(p)

    def gate(self, rep, inv, *extra, env=None, floor="3"):
        argv = ["--policy", str(POLICY), "--reports-dir", str(self.reports), "vuln", self.write("r.json", rep), "--name", "t",
                "--kind", "tooling", "--artifact", ".reader-venv", "--min-lang-packages", floor,
                "--installed-inventory", self.write("inv.json", inv), "--env-root", str(env or self.env), *extra]
        code = gate.main(argv)
        text = (self.reports / "t.summary.txt").read_text(encoding="utf-8")
        return code, [l for l in text.splitlines() if l.startswith("RESULT:")][-1], text

    def test_every_installed_distribution_scanned_passes(self):
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env))
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("4 installed distributions", text)

    def test_vendored_copies_in_the_scan_are_allowed(self):
        extra = [{"Name": "urllib3", "Version": "2.7.0", "FilePath": "", "Identifier": {"UID": "v"}}]
        code, result, _ = self.gate(report(".reader-venv", extra=extra), inventory(self.env))
        self.assertEqual((code, result), (0, gate.RESULT_PASS))

    def test_a_distribution_the_scan_omitted_is_incomplete(self):
        code, result, text = self.gate(report(".reader-venv", dists=DISTS[:-1]), inventory(self.env))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("typing-extensions 4.16.0", text)

    def test_a_distribution_installed_after_the_scan_is_incomplete(self):
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env, DISTS + [("requests", "2.34.2")]))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("installed after the scan", text)

    def test_a_version_mismatch_is_incomplete(self):
        code, result, _ = self.gate(report(".reader-venv", dists=[("pip", "26.2.1"), ("pytest", "9.0.3"), ("PyYAML", "6.0.3"), ("typing_extensions", "4.16.0")]), inventory(self.env))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_the_same_name_and_version_at_another_path_is_not_coverage(self):
        rep = report(".reader-venv", dists=DISTS[1:], extra=[{"Name": "pip", "Version": "26.2.1", "FilePath": "elsewhere/pip-26.2.1.dist-info/METADATA", "Identifier": {"UID": "w"}}])
        code, result, _ = self.gate(rep, inventory(self.env))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_an_inventory_of_another_environment_is_incomplete(self):
        other = self.dir / "other-venv"; other.mkdir()
        code, result, text = self.gate(report(".reader-venv"), inventory(other))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("outside the scanned environment", text)

    def test_an_inventory_below_the_floor_is_incomplete(self):
        code, result, _ = self.gate(report(".reader-venv"), inventory(self.env), floor="10")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_a_malformed_inventory_is_a_report_failure(self):
        for doc in ({"version": "2", "installed": []}, {"version": "1"}, {"version": "1", "installed": [{"metadata": {"name": "pip"}}]}):
            with self.subTest(doc):
                code, result, _ = self.gate(report(".reader-venv"), doc)
                self.assertEqual((code, result), (2, gate.RESULT_REPORT))

    def test_a_report_of_another_artifact_or_type_is_incomplete(self):
        code, result, _ = self.gate(report("/somewhere/else"), inventory(self.env))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))
        rep = report(".reader-venv"); rep["ArtifactType"] = "repository"
        code, result, _ = self.gate(rep, inventory(self.env))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_critical_in_tooling_violates_with_or_without_a_fix(self):
        for fixed in ("9.0.3", ""):
            with self.subTest(fixed=fixed):
                code, result, text = self.gate(report(".reader-venv", vulns=[vuln("C-T", "pytest", fixed, "CRITICAL")]), inventory(self.env))
                self.assertEqual((code, result), (3, gate.RESULT_POLICY)); self.assertIn("scope=development", text)

    def test_critical_on_a_vendored_copy_without_a_filepath_violates(self):
        extra = [{"Name": "urllib3", "Version": "2.7.0", "FilePath": "", "Identifier": {"UID": "vend"}}]
        v = vuln("C-V", "urllib3", "", "CRITICAL"); v["PkgIdentifier"] = {"UID": "vend"}
        code, result, text = self.gate(report(".reader-venv", vulns=[v], extra=extra), inventory(self.env))
        self.assertEqual((code, result), (3, gate.RESULT_POLICY)); self.assertIn("VIOLATION C-V", text)

    def test_critical_in_a_non_python_result_of_a_tooling_scan_violates(self):
        rep = report(".reader-venv")
        rep["Results"].append({"Target": "Node.js", "Class": "lang-pkgs", "Type": "node-pkg",
                               "Packages": [{"Name": "x", "Version": "1", "FilePath": "lib/node_modules/x/package.json", "Identifier": {"UID": "n1"}}],
                               "Vulnerabilities": [dict(vuln("C-N", "x", "", "CRITICAL"), PkgIdentifier={"UID": "n1"})]})
        code, result, text = self.gate(rep, inventory(self.env))
        self.assertEqual((code, result), (3, gate.RESULT_POLICY)); self.assertIn("VIOLATION C-N", text)

    def test_high_in_tooling_is_reported_or_listed_not_gating(self):
        code, result, text = self.gate(report(".reader-venv", vulns=[vuln("H-T", "pytest", "9.0.3", "HIGH"), vuln("H-U", "pip", "", "HIGH")]), inventory(self.env))
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("REPORTED  H-T", text); self.assertIn("EXCLUDED  H-U", text)

    def test_tooling_options_are_bound_to_the_tooling_kind(self):
        rep = self.write("r.json", report(".reader-venv")); inv = self.write("inv.json", inventory(self.env))
        base = ["--policy", str(POLICY), "--reports-dir", str(self.reports), "vuln", rep, "--name", "t"]
        self.assertEqual(gate.main(base + ["--kind", "tooling", "--artifact", ".reader-venv", "--env-root", str(self.env)]), gate.EXIT_USAGE)
        self.assertEqual(gate.main(base + ["--kind", "tooling", "--artifact", ".reader-venv", "--installed-inventory", inv]), gate.EXIT_USAGE)
        self.assertEqual(gate.main(base + ["--kind", "image", "--artifact", "x", "--installed-inventory", inv, "--env-root", str(self.env)]), gate.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
