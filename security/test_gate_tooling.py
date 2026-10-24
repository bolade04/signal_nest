"""Assertions over `security/gate.py vuln --kind tooling` (P6-CI-6 closure, operator order 2026-10-19).

An installed tooling environment (a CI virtual environment or interpreter prefix) is scanned AS
INSTALLED with Trivy rootfs and reconciled against the environment's own `pip inspect` inventory:
every installed distribution must appear in the scan at its own metadata path with the same name
and version. These tests build inert, synthetic inventories and reports of the shapes `pip inspect`
and Trivy emit; no scanner or installer runs. The coverage-gap tranche (operator order 2026-10-20) adds the
vendored-pin accounting of an installed pip, the reconciliation against hash locks, the refusal of pip in a final
image and the binary kind (Go binary analysis of the Trivy and OpenTofu binaries a job executes); each is asserted
here, including the cases where the analysis is unsupported, which must never read as clean.
Run: python3 -m unittest discover -s security
"""
from __future__ import annotations

import hashlib
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
VENDOR = [("urllib3", "1.26.20"), ("requests", "2.32.3")]
VTARGET = "reader-venv/requirements-vendored-pip.txt"


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


def vendored(pins=VENDOR, vulns=(), target=VTARGET) -> dict:
    return {"SchemaVersion": 2, "ArtifactName": ".vendored-pins", "ArtifactType": "filesystem", "Results": [
        {"Target": target, "Class": "lang-pkgs", "Type": "pip", "Packages": [{"Name": n, "Version": v, "Identifier": {"UID": f"u-{n}"}} for n, v in pins],
         "Vulnerabilities": list(vulns)}]}


def write_vendor_txt(env: pathlib.Path, text: str) -> None:
    d = env / SITE / "pip" / "_vendor"
    d.mkdir(parents=True, exist_ok=True)
    (d / "vendor.txt").write_text(text, encoding="utf-8")


def vuln(vid: str, name: str, fixed: str, severity: str) -> dict:
    return {"VulnerabilityID": vid, "PkgName": name, "InstalledVersion": "x", "FixedVersion": fixed, "Severity": severity,
            "Status": "fixed" if fixed else "affected", "PkgIdentifier": {"UID": f"u-{name}"}}


class ToolingCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.env = self.dir / ".reader-venv"
        self.env.mkdir()
        write_vendor_txt(self.env, "# pip's own list\nurllib3==1.26.20\nrequests==2.32.3\n    certifi==2024.8.30  # indented sub-pin\n")
        self.vendor = VENDOR + [("certifi", "2024.8.30")]
        self.reports = self.dir / "security-reports"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, name, doc):
        p = self.dir / name
        p.write_text(json.dumps(doc), encoding="utf-8")
        return str(p)

    def gate(self, rep, inv, *extra, env=None, floor="3", vrep="default"):
        argv = ["--policy", str(POLICY), "--reports-dir", str(self.reports), "vuln", self.write("r.json", rep), "--name", "t",
                "--kind", "tooling", "--artifact", ".reader-venv", "--min-lang-packages", floor,
                "--installed-inventory", self.write("inv.json", inv), "--env-root", str(env or self.env), *extra]
        if vrep is not None:
            argv += ["--vendored-report", self.write("v.json", vendored(self.vendor) if vrep == "default" else vrep),
                     "--vendored-artifact", ".vendored-pins", "--vendored-target", VTARGET]
        code = gate.main(argv)
        text = (self.reports / "t.summary.txt").read_text(encoding="utf-8")
        return code, [l for l in text.splitlines() if l.startswith("RESULT:")][-1], text

    def test_every_installed_distribution_scanned_passes(self):
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env))
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("4 installed distributions", text)
        self.assertIn("pip 26.2.1 declares 3 vendored packages in lib/python3.12/site-packages/pip/_vendor/vendor.txt; all 3 are in the vendored-pin scan", text)
        self.assertNotIn("vendored copies included", text)
        self.assertIn("the scan lists 4 language packages: 4 at an installed distribution's metadata, 0 at other metadata paths", text)

    # ---------------------------------------------------------------- vendored copies of an installed pip (2026-10-20)
    def test_pip_without_a_vendored_pin_scan_is_not_accounted_for(self):
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env), vrep=None)
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("they are not accounted for", text)

    def test_pip_without_a_readable_vendor_list_cannot_be_accounted_for(self):
        (self.env / SITE / "pip" / "_vendor" / "vendor.txt").unlink()
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("vendor.txt is absent", text)
        for bad in ("requests>=2\n", "requests==2.32.3; python_version<'3.9'\n", "-r other.txt\n", "# nothing\n"):
            with self.subTest(bad):
                write_vendor_txt(self.env, bad)
                code, result, _ = self.gate(report(".reader-venv"), inventory(self.env))
                self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))

    def test_a_vendored_pin_the_scan_omitted_or_added_is_incomplete(self):
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env), vrep=vendored(self.vendor[1:]))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("absent from the vendored-pin scan", text)
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env), vrep=vendored(self.vendor + [("idna", "3.10")]))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("no installed pip declares", text)
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env), vrep=vendored(self.vendor, target="other/requirements-vendored-pip.txt"))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("staged pin list was not analysed", text)
        for name in ("/some/other/dir", None):     # a report of another directory, or of none (A-4)
            doc = vendored(self.vendor)
            if name is None:
                del doc["ArtifactName"]
            else:
                doc["ArtifactName"] = name
            code, result, text = self.gate(report(".reader-venv"), inventory(self.env), vrep=doc)
            self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("the report describes something else", text)

    def test_findings_in_vendored_copies_are_evaluated_at_the_tooling_scope(self):
        crit = dict(vuln("C-VP", "urllib3", "", "CRITICAL"), PkgIdentifier={"UID": "u-urllib3"})
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env), vrep=vendored(self.vendor, vulns=[crit]))
        self.assertEqual((code, result), (3, gate.RESULT_POLICY))
        self.assertIn(f"VIOLATION C-VP urllib3 x -> no fix [CRITICAL] scope=development status=affected target={VTARGET}", text)
        high = dict(vuln("H-VP", "requests", "2.33.0", "HIGH"), PkgIdentifier={"UID": "u-requests"})
        code, result, text = self.gate(report(".reader-venv"), inventory(self.env), vrep=vendored(self.vendor, vulns=[high]))
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("REPORTED  H-VP", text)

    def test_an_environment_without_pip_needs_no_vendored_pin_scan(self):
        dists = DISTS[1:]
        code, result, text = self.gate(report(".reader-venv", dists=dists), inventory(self.env, dists), vrep=None)
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("vendored: no pip is installed in this environment", text)
        code, result, _ = self.gate(report(".reader-venv", dists=dists), inventory(self.env, dists))
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE), "a vendored-pin scan listing packages no pip declares")

    # ---------------------------------------------------------------- reconciliation against hash locks (2026-10-20)
    def locks(self, *docs):
        out = []
        for pins in docs:
            self.lock_count = getattr(self, "lock_count", 0) + 1
            path = self.dir / f"lock-{self.lock_count}.txt"
            path.write_text("".join(f"{n}=={v} \\\n    --hash=sha256:{'ab' * 32}\n    # via x\n" for n, v in pins), encoding="utf-8")
            out += ["--locked-by", str(path)]
        return out

    def editable(self, inv, name, url):
        for it in inv["installed"]:
            if it["metadata"]["name"] == name:
                it["direct_url"] = {"url": url, "dir_info": {"editable": True}}
        return inv

    def test_an_installation_that_is_exactly_the_locks_passes(self):
        inv = self.editable(inventory(self.env), "typing_extensions", "file://" + str(pathlib.Path.cwd() / "apps" / "api"))
        code, result, text = self.gate(report(".reader-venv"), inv, *self.locks(DISTS[:1], DISTS[1:3]), "--locked-extra", "typing-extensions")
        self.assertEqual((code, result), (0, gate.RESULT_PASS), text)
        self.assertIn("the 4 installed distributions are exactly the 3 pins of", text)

    def test_a_locked_extra_must_be_an_editable_install_from_the_working_directory(self):
        locks = self.locks(DISTS[:1], DISTS[1:3])
        for label, inv in (("no direct_url", inventory(self.env)),
                           ("from an index", self.editable(inventory(self.env), "typing_extensions", "https://files.example.invalid/x.whl")),
                           ("outside the working directory", self.editable(inventory(self.env), "typing_extensions", "file:///somewhere/else"))):
            with self.subTest(label):
                code, result, text = self.gate(report(".reader-venv"), inv, *locks, "--locked-extra", "typing-extensions")
                self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("not installed from the checkout", text)
        inv = self.editable(inventory(self.env), "typing_extensions", "file://" + str(pathlib.Path.cwd()))
        inv["installed"][-1]["direct_url"]["dir_info"] = {}
        code, result, _ = self.gate(report(".reader-venv"), inv, *locks, "--locked-extra", "typing-extensions")
        self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE), "a directory install that is not editable")

    # ---------------------------------------------------------------- read-only between inventories (2026-10-20, A-2)
    def test_an_environment_that_is_not_read_only_fails_the_read_only_requirement(self):
        import os, stat
        def set_mode(writable):
            for here, dirs, files in os.walk(self.env, topdown=False):
                for n in dirs + files:
                    p = os.path.join(here, n)
                    if not os.path.islink(p):
                        os.chmod(p, (os.lstat(p).st_mode | 0o200) if writable else (os.lstat(p).st_mode & ~0o222))
            os.chmod(self.env, (os.lstat(self.env).st_mode | 0o200) if writable else (os.lstat(self.env).st_mode & ~0o222))
        try:
            (self.env / "lib64").symlink_to("lib")              # a venv's lib64 link: symbolic links are not judged
            set_mode(False)
            code, result, text = self.gate(report(".reader-venv"), inventory(self.env), "--require-read-only")
            self.assertEqual((code, result), (0, gate.RESULT_PASS), text); self.assertIn("read-only: every file and directory", text)
            target = self.env / SITE / "pip" / "_vendor" / "vendor.txt"
            os.chmod(target, os.lstat(target).st_mode | stat.S_IWUSR)
            code, result, text = self.gate(report(".reader-venv"), inventory(self.env), "--require-read-only")
            self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn("lib/python3.12/site-packages/pip/_vendor/vendor.txt", text)
            os.chmod(target, os.lstat(target).st_mode & ~0o222)
            os.chmod(self.env, os.lstat(self.env).st_mode | stat.S_IWUSR)     # the root directory itself
            code, result, _ = self.gate(report(".reader-venv"), inventory(self.env), "--require-read-only")
            self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE))
        finally:
            set_mode(True)

    def test_an_installation_outside_or_against_the_locks_is_incomplete(self):
        for label, locks, needle in (("unlocked", self.locks(DISTS[:3]), "not in the lock(s)"),
                                     ("version", self.locks(DISTS[:3] + [("typing_extensions", "4.15.0")]), "differ from the locked version"),
                                     ("absent", self.locks(DISTS + [("idna", "3.10")]), "are not installed")):
            with self.subTest(label):
                code, result, text = self.gate(report(".reader-venv"), inventory(self.env), *locks)
                self.assertEqual((code, result), (4, gate.RESULT_INCOMPLETE)); self.assertIn(needle, text)
        code, result, _ = self.gate(report(".reader-venv"), inventory(self.env), *self.locks(DISTS, [("pip", "25.1.1")]))
        self.assertEqual((code, result), (2, gate.RESULT_REPORT), "one distribution pinned at two versions")

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

    def test_a_valid_unknown_in_tooling_is_reported_and_a_later_critical_fails(self):
        # Operator decision 2 (2026-10-19): a valid UNKNOWN stays reported for triage in an installed tooling scan too.
        code, result, text = self.gate(report(".reader-venv", vulns=[vuln("U-W", "pytest", "", "UNKNOWN")]), inventory(self.env))
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("lower     U-W pytest x -> no fix [UNKNOWN] scope=development", text)
        code, result, text = self.gate(report(".reader-venv", vulns=[vuln("U-W", "pytest", "", "CRITICAL")]), inventory(self.env))
        self.assertEqual((code, result), (3, gate.RESULT_POLICY)); self.assertIn("VIOLATION U-W", text)

    def test_high_in_tooling_is_reported_or_listed_not_gating(self):
        code, result, text = self.gate(report(".reader-venv", vulns=[vuln("H-T", "pytest", "9.0.3", "HIGH"), vuln("H-U", "pip", "", "HIGH")]), inventory(self.env))
        self.assertEqual((code, result), (0, gate.RESULT_PASS)); self.assertIn("REPORTED  H-T", text); self.assertIn("EXCLUDED  H-U", text)

    def test_tooling_options_are_bound_to_the_tooling_kind(self):
        rep = self.write("r.json", report(".reader-venv")); inv = self.write("inv.json", inventory(self.env))
        base = ["--policy", str(POLICY), "--reports-dir", str(self.reports), "vuln", rep, "--name", "t"]
        self.assertEqual(gate.main(base + ["--kind", "tooling", "--artifact", ".reader-venv", "--env-root", str(self.env)]), gate.EXIT_USAGE)
        self.assertEqual(gate.main(base + ["--kind", "tooling", "--artifact", ".reader-venv", "--installed-inventory", inv]), gate.EXIT_USAGE)
        self.assertEqual(gate.main(base + ["--kind", "image", "--artifact", "x", "--installed-inventory", inv, "--env-root", str(self.env)]), gate.EXIT_USAGE)
        full = base + ["--kind", "tooling", "--artifact", ".reader-venv", "--installed-inventory", inv, "--env-root", str(self.env)]
        self.assertEqual(gate.main(full + ["--vendored-report", rep]), gate.EXIT_USAGE)
        self.assertEqual(gate.main(full + ["--vendored-target", VTARGET]), gate.EXIT_USAGE)
        self.assertEqual(gate.main(full + ["--vendored-report", rep, "--vendored-target", VTARGET]), gate.EXIT_USAGE, "without --vendored-artifact")
        self.assertEqual(gate.main(full + ["--locked-extra", "x"]), gate.EXIT_USAGE)
        for extra in (["--vendored-report", rep, "--vendored-target", VTARGET, "--vendored-artifact", "x"], ["--locked-by", rep],
                      ["--binary-job", "j"], ["--require-read-only"]):
            with self.subTest(extra):
                self.assertEqual(gate.main(base + ["--kind", "image", "--artifact", "x"] + extra), gate.EXIT_USAGE)


class ImageInstallerCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def run_gate(self, lang):
        doc = {"SchemaVersion": 2, "ArtifactName": "img:ci", "ArtifactType": "container_image",
               "Metadata": {"ImageID": "sha256:" + "ab" * 32, "OS": {"Family": "debian", "Name": "13"}},
               "Results": [{"Target": "img:ci (debian 13)", "Class": "os-pkgs", "Type": "debian", "Packages": [{"Name": "libc6", "Version": "2", "Identifier": {"UID": "o"}}]},
                           {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg", "Packages": lang}]}
        path = self.dir / "img.json"
        path.write_text(json.dumps(doc), encoding="utf-8")
        code = gate.main(["--policy", str(POLICY), "--reports-dir", str(self.dir), "vuln", str(path), "--name", "i",
                          "--kind", "image", "--artifact", "img:ci", "--min-lang-packages", "1"])
        return code, (self.dir / "i.summary.txt").read_text(encoding="utf-8")

    def test_a_final_image_with_pip_is_incomplete_and_without_it_passes(self):
        app = {"Name": "fastapi", "Version": "1", "FilePath": "opt/venv/lib/python3.12/site-packages/fastapi-1.dist-info/METADATA", "Identifier": {"UID": "a"}}
        code, text = self.run_gate([app])
        self.assertEqual(code, gate.EXIT_OK); self.assertIn("installers: no pip in the image's analysed Python population", text)
        pip = {"Name": "pip", "Version": "25.0.1", "FilePath": "usr/local/lib/python3.12/site-packages/pip-25.0.1.dist-info/METADATA", "Identifier": {"UID": "p"}}
        code, text = self.run_gate([app, pip])
        self.assertEqual(code, gate.EXIT_INCOMPLETE); self.assertIn("the image ships an installer (pip 25.0.1 at usr/local/lib", text)


class BinaryCase(unittest.TestCase):
    """--kind binary over synthetic staged files, pins, a staging manifest and Go binary results."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.stage = self.dir / ".scanner-binaries"
        self.stage.mkdir()
        self.origin = self.dir / "toolcache"
        self.origin.mkdir()
        self.pins = {"jobs": {"j": ["trivy", "tofu"]}, "binaries": {}}
        self.manifest = {"version": 1, "binaries": {}}
        for name in ("trivy", "tofu"):
            data = f"synthetic {name}".encode()
            (self.stage / name).write_bytes(data); (self.origin / name).write_bytes(data)
            digest = hashlib.sha256(data).hexdigest()
            self.pins["binaries"][name] = {"version": "1.0.0", "module": f"example.invalid/{name}", "module_version": "v1.0.0", "sha256": digest,
                                           "release_asset": f"{name}.tar.gz"}
            self.manifest["binaries"][name] = {"origin": str(self.origin / name), "sha256": digest}

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def result(name, *, root=True, stdlib="v1.26.5", vulns=()):
        pkgs = [{"Name": "golang.org/x/net", "Version": "v0.1.0", "Relationship": "direct", "Identifier": {"UID": f"{name}-net"}}]
        if root:
            pkgs.insert(0, {"Name": f"example.invalid/{name}", "Version": "v1.0.0", "Relationship": "root", "Identifier": {"UID": f"{name}-root"}})
        if stdlib:
            pkgs.append({"Name": "stdlib", "Version": stdlib, "Relationship": "direct", "Identifier": {"UID": f"{name}-std"}})
        return {"Target": name, "Class": "lang-pkgs", "Type": "gobinary", "Packages": pkgs, "Vulnerabilities": list(vulns)}

    def run_gate(self, results, job="j", policy=POLICY):
        rep = {"SchemaVersion": 2, "ArtifactName": str(self.stage), "ArtifactType": "filesystem", "Results": results}
        files = {}
        for name, doc in (("r", rep), ("pins", self.pins), ("manifest", self.manifest)):
            files[name] = self.dir / f"{name}.json"
            files[name].write_text(json.dumps(doc), encoding="utf-8")
        code = gate.main(["--policy", str(policy), "--reports-dir", str(self.dir), "vuln", str(files["r"]), "--name", "b",
                          "--kind", "binary", "--artifact", str(self.stage), "--binary-job", job,
                          "--binary-pins", str(files["pins"]), "--binary-manifest", str(files["manifest"])])
        return code, (self.dir / "b.summary.txt").read_text(encoding="utf-8")

    def test_both_binaries_analysed_at_their_pinned_digests_pass(self):
        code, text = self.run_gate([self.result("trivy"), self.result("tofu")])
        self.assertEqual(code, gate.EXIT_OK, text)
        self.assertIn("Go binary analysis: main module example.invalid/tofu v1.0.0, Go standard library v1.26.5, 3 modules", text)
        self.assertIn("not established by this analysis", text)

    def test_unsupported_or_incomplete_analysis_is_never_clean(self):
        cases = {
            "no result": [self.result("trivy")],
            "two results": [self.result("trivy"), self.result("tofu"), self.result("tofu")],
            "not a Go binary result": [self.result("trivy"), dict(self.result("tofu"), Type="rustbinary")],
            "no packages": [self.result("trivy"), dict(self.result("tofu"), Packages=[])],
            "no main module": [self.result("trivy"), self.result("tofu", root=False)],
            "no standard library version": [self.result("trivy"), self.result("tofu", stdlib="")],
            "another binary": [self.result("trivy"), self.result("tofu"), self.result("sh")],
        }
        for label, results in cases.items():
            with self.subTest(label):
                code, text = self.run_gate(results)
                self.assertEqual(code, gate.EXIT_INCOMPLETE, text)
        bad = self.result("tofu"); bad["Packages"][0]["Version"] = "v9.9.9"
        code, text = self.run_gate([self.result("trivy"), bad])
        self.assertEqual(code, gate.EXIT_INCOMPLETE); self.assertIn("unsupported or incomplete: main module", text)
        code, _ = self.run_gate(None)
        self.assertEqual(code, gate.EXIT_REPORT, "a scan with no Results at all (what Trivy writes for executables it cannot analyse)")

    def test_the_scanned_copy_and_the_executed_file_must_be_the_pinned_release(self):
        (self.stage / "tofu").write_bytes(b"tampered")
        code, text = self.run_gate([self.result("trivy"), self.result("tofu")])
        self.assertEqual(code, gate.EXIT_INCOMPLETE); self.assertIn("the staged copy of tofu hashes to", text)
        (self.stage / "tofu").write_bytes(b"synthetic tofu"); (self.origin / "tofu").write_bytes(b"replaced after staging")
        code, text = self.run_gate([self.result("trivy"), self.result("tofu")])
        self.assertEqual(code, gate.EXIT_INCOMPLETE); self.assertIn("tofu as resolved on PATH at staging time", text)
        (self.origin / "tofu").write_bytes(b"synthetic tofu"); self.manifest["binaries"]["tofu"]["sha256"] = "0" * 64
        code, text = self.run_gate([self.result("trivy"), self.result("tofu")])
        self.assertEqual(code, gate.EXIT_INCOMPLETE); self.assertIn("staging manifest does not record tofu", text)

    def test_findings_are_evaluated_at_the_policys_binary_scope(self):
        # binary_scope = development records the operator's bounded decision of 2026-10-21 (policy _operator_decisions): CRITICAL
        # fails at any scope and a HIGH is reported. The key alone, not tooling_scope, decides the binaries' scope.
        crit = {"VulnerabilityID": "C-GO", "PkgName": "stdlib", "InstalledVersion": "v1.26.5", "Severity": "CRITICAL", "Status": "affected",
                "PkgIdentifier": {"UID": "tofu-std"}}
        code, text = self.run_gate([self.result("trivy"), self.result("tofu", vulns=[crit])])
        self.assertEqual(code, gate.EXIT_POLICY); self.assertIn("VIOLATION C-GO stdlib v1.26.5 -> no fix [CRITICAL] scope=development", text)
        self.assertIn("scope: findings are evaluated at the policy's binary_scope (development;", text)
        high = dict(crit, VulnerabilityID="H-GO", Severity="HIGH", FixedVersion="v1.26.6", Status="fixed")
        code, text = self.run_gate([self.result("trivy"), self.result("tofu", vulns=[high])])
        self.assertEqual(code, gate.EXIT_OK); self.assertIn("REPORTED  H-GO", text)
        doc = json.loads(POLICY.read_text())
        doc["vulnerabilities"]["binary_scope"] = "runtime"          # the key, not the tooling scope, decides the binaries' scope
        runtime = self.dir / "policy-runtime.json"; runtime.write_text(json.dumps(doc), encoding="utf-8")
        code, text = self.run_gate([self.result("trivy"), self.result("tofu", vulns=[high])], policy=runtime)
        self.assertEqual(code, gate.EXIT_POLICY); self.assertIn("VIOLATION H-GO stdlib v1.26.5 -> v1.26.6 [HIGH] scope=runtime", text)

    def test_the_job_must_be_named_in_the_pins_and_the_options_belong_to_the_binary_kind(self):
        code, _ = self.run_gate([self.result("trivy"), self.result("tofu")], job="other")
        self.assertEqual(code, gate.EXIT_REPORT)
        rep = self.dir / "x.json"; rep.write_text("{}", encoding="utf-8")
        base = ["--policy", str(POLICY), "--reports-dir", str(self.dir), "vuln", str(rep), "--name", "u", "--kind", "binary", "--artifact", "s"]
        self.assertEqual(gate.main(base), gate.EXIT_USAGE)
        self.assertEqual(gate.main(base + ["--binary-job", "j", "--binary-pins", "p"]), gate.EXIT_USAGE)


if __name__ == "__main__":
    unittest.main()
