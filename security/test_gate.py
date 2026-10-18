"""Independent assertions over security/gate.py with inert synthetic reports.

These are the gate's own negative controls: they do not run a scanner and do not depend on
one. Each test builds a minimal report of the shape the scanner emits, hands it to the gate
and asserts the exit code and the RESULT line. Run: python3 -m unittest discover -s security
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
IMAGE_ID = "sha256:" + "ab" * 32


def vuln(vid, pkg, installed, fixed, severity, uid="u1"):
    return {"VulnerabilityID": vid, "PkgName": pkg, "InstalledVersion": installed, "FixedVersion": fixed,
            "Severity": severity, "Status": "fixed" if fixed else "affected", "PkgIdentifier": {"UID": uid}}


def image_report(vulns=(), artifact="signalnest-api:ci", image_id=IMAGE_ID, lang_packages=45):
    return {"SchemaVersion": 2, "ArtifactName": artifact, "ArtifactType": "container_image",
            "Metadata": {"ImageID": image_id, "OS": {"Family": "debian", "Name": "13.7"}},
            "Results": [
                {"Target": f"{artifact} (debian 13.7)", "Class": "os-pkgs", "Type": "debian",
                 "Packages": [{"Name": "libc6", "Identifier": {"UID": "u1"}}], "Vulnerabilities": list(vulns)},
                {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg",
                 "Packages": [{"Name": f"p{i}", "Identifier": {"UID": f"l{i}"}} for i in range(lang_packages)]},
            ]}


def fs_report(npm_vulns=(), dev_vulns=(), runtime_py_vulns=()):
    return {"SchemaVersion": 2, "ArtifactName": ".", "ArtifactType": "filesystem", "Results": [
        {"Target": "package-lock.json", "Class": "lang-pkgs", "Type": "npm",
         "Packages": [{"Name": "react", "Identifier": {"UID": "rt"}}, {"Name": "vite", "Dev": True, "Identifier": {"UID": "dv"}}],
         "Vulnerabilities": list(npm_vulns) + list(dev_vulns)},
        {"Target": "apps/api/requirements-runtime.txt", "Class": "lang-pkgs", "Type": "pip",
         "Packages": [{"Name": "fastapi", "Identifier": {"UID": "py"}}], "Vulnerabilities": list(runtime_py_vulns)},
        {"Target": "apps/api/requirements-dev.txt", "Class": "lang-pkgs", "Type": "pip",
         "Packages": [{"Name": "pytest", "Identifier": {"UID": "pd"}}]},
    ]}


def misconfig_report(failures=(), types=("dockerfile", "terraform")):
    results = []
    for t in types:
        results.append({"Target": f"x.{t}", "Class": "config", "Type": t,
                        "MisconfSummary": {"Successes": 3, "Failures": len(failures) if t == "terraform" else 0},
                        "Misconfigurations": [dict(f, Status="FAIL") for f in failures] if t == "terraform" else []})
    return {"SchemaVersion": 2, "ArtifactName": ".", "ArtifactType": "filesystem", "Results": results}


def sast_report(results=(), errors=(), scanned=("apps/api/app/main.py", "apps/web/src/App.tsx", "scripts/x.py")):
    return {"version": "1.181.0", "results": list(results), "errors": list(errors),
            "paths": {"scanned": list(scanned), "skipped": []}}


def finding(severity, check="python.lang.security.audit.subprocess-shell-true", path="apps/api/app/x.py"):
    return {"check_id": check, "path": path, "start": {"line": 7}, "extra": {"severity": severity, "metadata": {"confidence": "MEDIUM"}}}


def sbom(kind="image", artifact="signalnest-api:ci", components=3, image_id=IMAGE_ID, spec="1.7"):
    comp = {"type": "container" if kind == "image" else "application", "name": artifact,
            "properties": ([{"name": gate.IMAGE_ID_PROPERTY, "value": image_id}] if image_id else [])}
    comps = [{"type": "operating-system", "name": "debian", "version": "13.7"}] if kind == "image" else []
    comps += [{"type": "library", "name": f"lib{i}", "version": "1.0"} for i in range(components)]
    return {"bomFormat": "CycloneDX", "specVersion": spec, "serialNumber": "urn:uuid:11111111-2222-3333-4444-555555555555",
            "version": 1, "metadata": {"component": comp, "tools": {"components": [{"name": "trivy", "version": "0.76.0"}]}},
            "components": comps[:components] if kind != "image" else comps}


class GateCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.reports = self.dir / "security-reports"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, doc):
        p = self.dir / name
        p.write_text(json.dumps(doc) if not isinstance(doc, str) else doc, encoding="utf-8")
        return str(p)

    def run_gate(self, *argv):
        code = gate.main(["--policy", str(POLICY), "--reports-dir", str(self.reports), *argv])
        name = argv[argv.index("--name") + 1]
        text = (self.reports / f"{name}.summary.txt").read_text(encoding="utf-8")
        result = [l for l in text.splitlines() if l.startswith("RESULT:")][-1]
        return code, result, text

    # ---- vulnerabilities, image
    def test_image_clean_passes(self):
        code, result, text = self.run_gate("vuln", self.write("r.json", image_report()), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci", "--min-lang-packages", "40")
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("population: 1 OS packages, 45 language packages", text)

    def test_image_critical_with_fix_violates(self):
        code, result, text = self.run_gate("vuln", self.write("r.json", image_report([vuln("CVE-1", "libc6", "1", "2", "CRITICAL")])), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        self.assertEqual((code, result), (3, gate.RESULT_POLICY)); self.assertIn("VIOLATION CVE-1", text)

    def test_image_high_without_fix_is_listed_as_excluded_not_hidden(self):
        code, result, text = self.run_gate("vuln", self.write("r.json", image_report([vuln("CVE-2", "libc6", "1", "", "HIGH")])), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("EXCLUDED  CVE-2", text)

    def test_image_medium_is_reported_at_lower_severity(self):
        code, result, text = self.run_gate("vuln", self.write("r.json", image_report([vuln("CVE-3", "libc6", "1", "2", "MEDIUM")])), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        self.assertEqual(code, 0); self.assertIn("lower     CVE-3", text)

    def test_image_wrong_artifact_is_incomplete(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", image_report(artifact="python:3.12-slim")), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_image_without_image_id_is_incomplete(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", image_report(image_id=None)), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_image_with_too_few_language_packages_is_incomplete(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", image_report(lang_packages=2)), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci", "--min-lang-packages", "40")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_image_with_empty_results_is_incomplete(self):
        doc = image_report(); doc["Results"] = []
        code, result, _ = self.run_gate("vuln", self.write("r.json", doc), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_filesystem_report_passed_as_image_is_incomplete(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", fs_report()), "--name", "a", "--kind", "image", "--artifact", ".")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    # ---- vulnerabilities, filesystem
    def test_fs_runtime_high_with_fix_violates(self):
        code, result, text = self.run_gate("vuln", self.write("r.json", fs_report(npm_vulns=[vuln("CVE-9", "react", "1", "2", "HIGH", uid="rt")])), "--name", "a", "--kind", "filesystem", "--artifact", ".", "--expect-target", "package-lock.json")
        self.assertEqual((code, result), (3, gate.RESULT_POLICY)); self.assertIn("scope=runtime", text)

    def test_fs_development_high_is_reported_not_gating(self):
        code, result, text = self.run_gate("vuln", self.write("r.json", fs_report(dev_vulns=[vuln("CVE-8", "vite", "1", "2", "HIGH", uid="dv")])), "--name", "a", "--kind", "filesystem", "--artifact", ".", "--expect-target", "package-lock.json")
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("REPORTED  CVE-8", text); self.assertIn("scope=development", text)

    def test_fs_dev_lock_target_is_development_scope(self):
        doc = fs_report(); doc["Results"][2]["Vulnerabilities"] = [vuln("CVE-7", "pytest", "1", "2", "CRITICAL", uid="pd")]
        code, result, text = self.run_gate("vuln", self.write("r.json", doc), "--name", "a", "--kind", "filesystem", "--artifact", ".", "--expect-target", "apps/api/requirements-dev.txt")
        self.assertEqual(code, 0); self.assertIn("REPORTED  CVE-7", text)

    def test_fs_runtime_python_critical_violates(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", fs_report(runtime_py_vulns=[vuln("CVE-6", "fastapi", "1", "2", "CRITICAL", uid="py")])), "--name", "a", "--kind", "filesystem", "--artifact", ".", "--expect-target", "apps/api/requirements-runtime.txt")
        self.assertEqual((code, result), (3, gate.RESULT_POLICY))

    def test_fs_missing_expected_target_is_incomplete(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", fs_report()), "--name", "a", "--kind", "filesystem", "--artifact", ".", "--expect-target", "scripts/requirements-gate.txt")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_fs_target_with_no_packages_is_incomplete(self):
        doc = fs_report(); doc["Results"][1]["Packages"] = []
        code, result, _ = self.run_gate("vuln", self.write("r.json", doc), "--name", "a", "--kind", "filesystem", "--artifact", ".", "--expect-target", "apps/api/requirements-runtime.txt")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    # ---- report integrity
    def test_missing_report_fails_as_report(self):
        code, result, _ = self.run_gate("vuln", str(self.dir / "nope.json"), "--name", "a", "--kind", "image", "--artifact", "x")
        self.assertEqual((code, result), (2, gate.RESULT_REPORT))

    def test_empty_report_fails_as_report(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", ""), "--name", "a", "--kind", "image", "--artifact", "x")
        self.assertEqual((code, result), (2, gate.RESULT_REPORT))

    def test_malformed_json_fails_as_report(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", "{not json"), "--name", "a", "--kind", "image", "--artifact", "x")
        self.assertEqual((code, result), (2, gate.RESULT_REPORT))

    def test_wrong_schema_version_fails_as_report(self):
        doc = image_report(); doc["SchemaVersion"] = 1
        code, result, _ = self.run_gate("vuln", self.write("r.json", doc), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        self.assertEqual((code, result), (2, gate.RESULT_REPORT))

    def test_unreadable_policy_fails_closed(self):
        code = gate.main(["--policy", str(self.dir / "missing-policy.json"), "--reports-dir", str(self.reports), "vuln", self.write("r.json", image_report()), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci"])
        self.assertEqual(code, 2)

    # ---- misconfigurations
    def test_misconfig_high_violates(self):
        code, result, text = self.run_gate("misconfig", self.write("m.json", misconfig_report([{"ID": "AVD-AWS-0107", "Severity": "HIGH", "Title": "open ssh"}])), "--name", "m", "--expect-type", "terraform")
        self.assertEqual((code, result), (3, gate.RESULT_POLICY)); self.assertIn("VIOLATION AVD-AWS-0107", text)

    def test_misconfig_low_only_passes_and_is_listed(self):
        code, result, text = self.run_gate("misconfig", self.write("m.json", misconfig_report([{"ID": "AVD-AWS-0124", "Severity": "LOW", "Title": "desc"}])), "--name", "m", "--expect-type", "terraform")
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("lower     AVD-AWS-0124", text)

    def test_misconfig_missing_type_is_incomplete(self):
        code, result, _ = self.run_gate("misconfig", self.write("m.json", misconfig_report(types=("dockerfile",))), "--name", "m", "--expect-type", "terraform")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_misconfig_no_results_is_incomplete(self):
        code, result, _ = self.run_gate("misconfig", self.write("m.json", misconfig_report(types=())), "--name", "m")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    # ---- SAST
    def test_sast_error_finding_violates(self):
        code, result, text = self.run_gate("sast", self.write("s.json", sast_report([finding("ERROR")])), "--name", "s", "--expect-prefix", "apps/api/app")
        self.assertEqual((code, result), (3, gate.RESULT_POLICY)); self.assertIn("VIOLATION python.lang", text)

    def test_sast_warning_only_passes_and_is_listed(self):
        code, result, text = self.run_gate("sast", self.write("s.json", sast_report([finding("WARNING")])), "--name", "s", "--expect-prefix", "apps/api/app")
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("reported  python.lang", text)

    def test_sast_scanner_error_is_incomplete_even_with_no_findings(self):
        code, result, text = self.run_gate("sast", self.write("s.json", sast_report(errors=[{"type": "Timeout", "level": "warn", "rule_id": "r", "path": "scripts/x.py"}])), "--name", "s")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("SCANNER ERROR Timeout", text)

    def test_sast_missing_tree_is_incomplete(self):
        code, result, _ = self.run_gate("sast", self.write("s.json", sast_report(scanned=["scripts/x.py"])), "--name", "s", "--expect-prefix", "apps/web/src")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_sast_missing_language_is_incomplete(self):
        code, result, _ = self.run_gate("sast", self.write("s.json", sast_report(scanned=["scripts/x.py"])), "--name", "s", "--expect-extension", ".tsx")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_sast_empty_scan_is_incomplete(self):
        code, result, _ = self.run_gate("sast", self.write("s.json", sast_report(scanned=[])), "--name", "s", "--expect-prefix", "scripts")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    # ---- SBOM
    def test_sbom_image_passes_and_writes_identity(self):
        rep = self.write("r.json", image_report())
        ident = self.dir / "id.json"
        code, result, text = self.run_gate("sbom", self.write("b.json", sbom()), "--name", "b", "--kind", "image", "--artifact", "signalnest-api:ci", "--min-components", "3", "--commit", "deadbeef", "--image-id-from", rep, "--identity-out", str(ident))
        self.assertEqual((code, result), (0, gate.RESULT_PASS))
        rec = json.loads(ident.read_text()); self.assertEqual(rec["commit"], "deadbeef"); self.assertEqual(rec["image_id"], IMAGE_ID); self.assertEqual(rec["components"], 4)

    def test_sbom_describing_a_different_image_is_incomplete(self):
        rep = self.write("r.json", image_report(image_id="sha256:" + "cd" * 32))
        code, result, _ = self.run_gate("sbom", self.write("b.json", sbom()), "--name", "b", "--kind", "image", "--artifact", "signalnest-api:ci", "--min-components", "1", "--commit", "c", "--image-id-from", rep)
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_sbom_name_substitution_is_incomplete(self):
        code, result, _ = self.run_gate("sbom", self.write("b.json", sbom(artifact="signalnest-worker:ci")), "--name", "b", "--kind", "image", "--artifact", "signalnest-api:ci", "--min-components", "1", "--commit", "c")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_sbom_without_image_id_is_incomplete(self):
        code, result, _ = self.run_gate("sbom", self.write("b.json", sbom(image_id=None)), "--name", "b", "--kind", "image", "--artifact", "signalnest-api:ci", "--min-components", "1", "--commit", "c")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_sbom_too_few_components_is_incomplete(self):
        code, result, _ = self.run_gate("sbom", self.write("b.json", sbom(kind="filesystem", artifact=".", components=2)), "--name", "b", "--kind", "filesystem", "--artifact", ".", "--min-components", "100", "--commit", "c")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_sbom_empty_placeholder_is_refused(self):
        code, result, _ = self.run_gate("sbom", self.write("b.json", {"bomFormat": "CycloneDX", "specVersion": "1.7", "components": []}), "--name", "b", "--kind", "filesystem", "--artifact", ".", "--min-components", "1", "--commit", "c")
        self.assertEqual((code, result), (2, gate.RESULT_REPORT))

    def test_sbom_wrong_format_is_refused(self):
        doc = sbom(); doc["bomFormat"] = "SPDX"
        code, result, _ = self.run_gate("sbom", self.write("b.json", doc), "--name", "b", "--kind", "image", "--artifact", "signalnest-api:ci", "--min-components", "1", "--commit", "c")
        self.assertEqual((code, result), (2, gate.RESULT_REPORT))

    def test_sbom_filesystem_kind_rejects_container_component(self):
        code, result, _ = self.run_gate("sbom", self.write("b.json", sbom()), "--name", "b", "--kind", "filesystem", "--artifact", "signalnest-api:ci", "--min-components", "1", "--commit", "c")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    # ---- negative controls and completeness
    def test_expect_fail_passes_only_on_a_policy_violation(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", image_report([vuln("CVE-1", "libc6", "1", "2", "CRITICAL")])), "--name", "c", "--kind", "image", "--artifact", "signalnest-api:ci", "--expect-fail")
        self.assertEqual((code, result), (0, gate.RESULT_CONTROL_OK))

    def test_expect_fail_rejects_a_clean_result(self):
        code, result, _ = self.run_gate("vuln", self.write("r.json", image_report()), "--name", "c", "--kind", "image", "--artifact", "signalnest-api:ci", "--expect-fail")
        self.assertEqual((code, result), (6, gate.RESULT_CONTROL_BAD))

    def test_expect_fail_rejects_a_missing_report(self):
        code, result, _ = self.run_gate("vuln", str(self.dir / "nope.json"), "--name", "c", "--kind", "image", "--artifact", "x", "--expect-fail")
        self.assertEqual((code, result), (6, gate.RESULT_CONTROL_BAD))

    def test_expect_fail_rejects_an_incomplete_analysis(self):
        code, result, _ = self.run_gate("sast", self.write("s.json", sast_report(results=[finding("ERROR")], errors=[{"type": "Timeout"}])), "--name", "c", "--expect-fail")
        self.assertEqual((code, result), (6, gate.RESULT_CONTROL_BAD))

    def test_complete_requires_every_summary(self):
        self.run_gate("vuln", self.write("r.json", image_report()), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        code, result, _ = self.run_gate("complete", "--name", "done", "--expect-pass", "a", "--expect-pass", "never-ran")
        self.assertEqual((code, result), (2, gate.RESULT_REPORT))

    def test_complete_rejects_a_failed_summary(self):
        self.run_gate("vuln", self.write("r.json", image_report([vuln("CVE-1", "libc6", "1", "2", "CRITICAL")])), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        code, result, _ = self.run_gate("complete", "--name", "done", "--expect-pass", "a")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_complete_passes_and_digests_the_reports(self):
        self.run_gate("vuln", self.write("r.json", image_report()), "--name", "a", "--kind", "image", "--artifact", "signalnest-api:ci")
        self.run_gate("vuln", self.write("c.json", image_report([vuln("CVE-1", "libc6", "1", "2", "CRITICAL")])), "--name", "ctl", "--kind", "image", "--artifact", "signalnest-api:ci", "--expect-fail")
        code, result, text = self.run_gate("complete", "--name", "done", "--expect-pass", "a", "--expect-control", "ctl")
        self.assertEqual((code, result), (0, gate.RESULT_PASS))
        manifest = (self.reports / "MANIFEST.txt").read_text()
        self.assertIn("a.summary.txt", manifest); self.assertIn("ctl.summary.txt", manifest); self.assertNotIn("MANIFEST.txt", manifest)

    def test_complete_rejects_a_control_that_passed_cleanly(self):
        self.run_gate("vuln", self.write("r.json", image_report()), "--name", "ctl", "--kind", "image", "--artifact", "signalnest-api:ci")
        code, result, _ = self.run_gate("complete", "--name", "done", "--expect-control", "ctl")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))


if __name__ == "__main__":
    unittest.main()
