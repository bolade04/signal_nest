"""Assertions over security/vuln_policy_controls.py (P6-CI-6 closure, operator order 2026-10-19).

The case lists are enumerated twice — in the driver and in the completeness steps of
.github/workflows/ci.yml — so they are pinned against each other here in BOTH directions, per job.
The driver's gate arguments are pinned against the REAL gate steps, so a control cannot exercise a
weaker invocation than the one CI gates on. Every case is then built and run on inert synthetic
reports of the scanner's shape: each must reach its stated outcome under the tracked policy, and under
the superseded policy (CRITICAL treated like HIGH) exactly the assessment's pass-through cases must
fail — the controls detect the regression they exist for. No scanner runs.
Run: python3 -m unittest discover -s security
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import pathlib
import re
import shlex
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402
import vuln_policy_controls as ctl  # noqa: E402

WORKFLOW = HERE.parent / ".github" / "workflows" / "ci.yml"
JOBS = {"container": "container-build", "reader": "revision-reader"}
# REPORTS key -> {--name of a real gate step whose arguments the driver must reuse: the inventory that step must read}
REAL_STEPS = {"locks": {"locks-vuln": None}, "api-image": {"image-api-vuln": None}, "reader-image": {"image-reader-vuln": None},
              "reader-tooling": {"reader-tooling-venv": "reader-tooling-venv-installed.json",
                                 "reader-tooling-venv-final": "reader-tooling-venv-installed-final.json"},
              "reader-syspy": {"reader-tooling-syspy": "reader-tooling-syspy-installed.json",
                               "reader-tooling-syspy-final": "reader-tooling-syspy-installed-final.json"},
              "security-tooling": {"security-tooling-venv": "security-tooling-venv-installed.json",
                                   "security-tooling-venv-final": "security-tooling-venv-installed-final.json"}}
# the inventory commands, in workflow order, per tooling report: (first inventory, final inventory)
INVENTORY_COMMANDS = {"reader-tooling": ".reader-venv/bin/python -m pip inspect > security-reports/reader-tooling-venv-installed{}.json",
                      "reader-syspy": "python3 -m pip inspect > security-reports/reader-tooling-syspy-installed{}.json",
                      "security-tooling": ".security-venv/bin/python -m pip inspect > security-reports/security-tooling-venv-installed{}.json"}
# The assessment's pass-through classes: under the superseded policy these, and only these, go BAD.
PASS_THROUGH = {
    "critical-nofix-api-image-os", "critical-nofix-api-image-python", "critical-nofix-runtime-lock",
    "critical-nofix-npm-runtime", "critical-fix-npm-dev", "critical-nofix-npm-dev",
    "critical-fix-security-tool-lock", "critical-nofix-security-tool-lock", "critical-nofix-reader-tooling-lock",
    "critical-nofix-security-tooling", "critical-nofix-reader-image", "critical-fix-reader-tooling",
    "critical-nofix-reader-tooling", "critical-status-fixed-no-version-runtime-lock", "critical-wontfix-api-image-os",
    "critical-fixdeferred-api-image-os", "critical-eol-runtime-lock", "critical-ghsa-npm-dev", "critical-unmatched-uid-runtime-lock",
    "critical-nofix-api-image-gobinary", "critical-nofix-reader-tooling-vendored", "critical-nofix-reader-tooling-node-pkg",
    "critical-wontfix-reader-image-os", "critical-nofix-reader-syspy"}
SITE = "lib/python3.12/site-packages"
PIP_TARGETS = ["apps/api/requirements-runtime.txt", "apps/api/requirements-dev.txt", "scripts/requirements-gate.txt",
               "scripts/requirements-gate-hashes.txt", "security/requirements-semgrep.txt",
               "apps/revision-reader/requirements-dev.txt", "apps/revision-reader/requirements-toolcache-pip.txt"]


def job_text(text: str, job: str) -> str:
    start = text.index(f"\n  {job}:\n")
    nxt = re.search(r"\n  [a-z0-9-]+:\n", text[start + 1:])
    return text[start: start + 1 + nxt.start()] if nxt else text[start:]


def gate_steps(text: str) -> dict:
    """--name -> (report path, argument tokens) for every `security/gate.py vuln` command in the text."""
    out, lines = {}, text.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^\s+python3 security/gate\.py vuln (\S+) --name (\S+)(.*)$", line)
        if not m:
            continue
        toks = shlex.split(m.group(3))
        j = i + 1
        while j < len(lines) and lines[j].strip().startswith("--"):   # folded-scalar continuation lines
            toks += shlex.split(lines[j]); j += 1
        out[m.group(2)] = (m.group(1), toks)
    return out


def tooling(env: pathlib.Path, artifact: str, count: int) -> tuple[dict, dict]:
    dists = [(f"tool-pkg-{i}", f"1.{i}") for i in range(count)]
    inv = {"version": "1", "pip_version": "26.2.1", "installed": [
        {"metadata": {"name": n, "version": v}, "metadata_location": str(env / SITE / f"{n.replace('-', '_')}-{v}.dist-info")} for n, v in dists]}
    rep = {"SchemaVersion": 2, "ArtifactName": artifact, "ArtifactType": "filesystem", "Results": [
        {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg", "Packages": [
            {"Name": n, "Version": v, "FilePath": f"{SITE}/{n.replace('-', '_')}-{v}.dist-info/METADATA", "Identifier": {"UID": f"t{i}"}}
            for i, (n, v) in enumerate(dists)] + [{"Name": "vendored", "Version": "1.0", "FilePath": "", "Identifier": {"UID": "vend"}}]}]}
    return rep, inv


def image(artifact: str, lang: int) -> dict:
    return {"SchemaVersion": 2, "ArtifactName": artifact, "ArtifactType": "container_image",
            "Metadata": {"ImageID": "sha256:" + "cd" * 32, "OS": {"Family": "debian", "Name": "13.7"}},
            "Results": [{"Target": f"{artifact} (debian 13.7)", "Class": "os-pkgs", "Type": "debian",
                         "Packages": [{"Name": "libc6", "Version": "2.41", "Identifier": {"UID": "os1"}}]},
                        {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg",
                         "Packages": [{"Name": f"p{i}", "Version": "1.0", "Identifier": {"UID": f"l{i}"}} for i in range(lang)]}]}


def locks() -> dict:
    results = [{"Target": "package-lock.json", "Class": "lang-pkgs", "Type": "npm",
                "Packages": [{"Name": "react", "Version": "19.0.0", "Identifier": {"UID": "rt"}},
                             {"Name": "vite", "Version": "7.0.0", "Dev": True, "Identifier": {"UID": "dv"}}]}]
    results += [{"Target": t, "Class": "lang-pkgs", "Type": "pip",
                 "Packages": [{"Name": f"pkg{i}", "Version": "1.0", "Identifier": {"UID": f"pip{i}"}}]} for i, t in enumerate(PIP_TARGETS)]
    return {"SchemaVersion": 2, "ArtifactName": ".", "ArtifactType": "filesystem", "Results": results}


class Pinning(unittest.TestCase):
    def setUp(self):
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_each_jobs_case_list_is_exactly_the_one_its_completeness_step_requires(self):
        everywhere = set(re.findall(r"--expect-control " + re.escape(ctl.SUMMARY_PREFIX) + r"([a-z0-9-]+)", self.text))
        seen = set()
        for group, job in JOBS.items():
            with self.subTest(group=group):
                required = set(re.findall(r"--expect-control " + re.escape(ctl.SUMMARY_PREFIX) + r"([a-z0-9-]+)", job_text(self.text, job)))
                cases = {n for n, c in ctl.CASES.items() if c[0] == group}
                self.assertEqual(required, cases, f"{job}: completeness --expect-control list differs from the driver's {group} cases")
                self.assertIn(f"python3 security/vuln_policy_controls.py check {group} security-reports", job_text(self.text, job))
                seen |= required
        self.assertEqual(everywhere, seen, "a vulnerability-policy control is required outside its job")
        self.assertEqual(set(ctl.CASES), seen)
        self.assertEqual({c[0] for c in ctl.CASES.values()}, set(ctl.GROUPS))

    def test_the_driver_reuses_the_real_gate_steps_arguments(self):
        for key, (rep_name, args, inv_name) in ctl.REPORTS.items():
            group_jobs = {JOBS[c[0]] for c in ctl.CASES.values() if c[1] == key}
            self.assertEqual(len(group_jobs), 1, key)
            steps = gate_steps(job_text(self.text, group_jobs.pop()))
            for name, step_inv in REAL_STEPS[key].items():
                with self.subTest(report=key, step=name):
                    self.assertIn(name, steps, f"no real gate step --name {name}")
                    path, toks = steps[name]
                    self.assertEqual(path, f"security-reports/{rep_name}")
                    if inv_name is None:
                        self.assertEqual(toks, args)
                    else:
                        k = toks.index("--installed-inventory")
                        self.assertEqual(toks[k + 1], f"security-reports/{step_inv}", "a tooling gate reads another step's inventory")
                        self.assertEqual(toks[:k] + toks[k + 2:], args)
            if inv_name is not None:
                self.assertEqual(list(REAL_STEPS[key].values())[0], inv_name)

    def test_tooling_inventories_are_taken_scanned_gated_and_retaken_in_that_order(self):
        for key, cmd in INVENTORY_COMMANDS.items():
            rep_name, _, inv_name = ctl.REPORTS[key]
            group = {c[0] for c in ctl.CASES.values() if c[1] == key}.pop()
            job = job_text(self.text, JOBS[group])
            first, final = list(REAL_STEPS[key])
            with self.subTest(key):
                def at(text):
                    m = re.search(re.escape(text) + r"(?=\s)", job)
                    self.assertIsNotNone(m, f"{text!r} is absent from {JOBS[group]}")
                    return m.start()
                order = [at(cmd.format("")), at(f"output: security-reports/{rep_name}"), at(f"--name {first}"),
                         at(f"python3 security/vuln_policy_controls.py check {group}"), at(cmd.format("-final")),
                         at(f"--name {final}"), at("python3 security/gate.py complete")]
                self.assertEqual(order, sorted(order), "inventory, scan, gate, controls, final inventory, final gate and completeness are out of order")
                self.assertEqual(job.count(cmd.format("")), 1); self.assertEqual(job.count(cmd.format("-final")), 1)
        self.assertNotIn("pip inspect --local", self.text, "--local hides distributions a path entry adds from outside the environment")

    def test_the_rootfs_scanner_control_is_required_in_both_jobs(self):
        for job in JOBS.values():
            with self.subTest(job):
                text = job_text(self.text, job)
                self.assertIn("--expect-control control-tooling-rootfs", text)
                self.assertIn("scan-ref: security/controls/tooling-env", text)
                self.assertIn("--name control-tooling-rootfs", text)

    def test_every_lock_target_the_policy_declares_is_expected_by_the_locks_gate(self):
        policy = json.loads((HERE / "policy.json").read_text())["vulnerabilities"]
        declared = set(policy["runtime_targets"]) | set(policy["development_targets"])
        expected = {ctl.LOCKS_ARGS[i + 1] for i, t in enumerate(ctl.LOCKS_ARGS) if t == "--expect-target"}
        self.assertEqual(declared, expected)
        self.assertEqual(set(PIP_TARGETS) | {"package-lock.json"}, expected)


class Cases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name).resolve()
        self.cwd = os.getcwd()
        os.chdir(self.root)            # the tooling gates take cwd-relative environment roots, as in CI
        self.reports = self.root / "security-reports"
        self.reports.mkdir()
        docs = {"trivy-locks-vuln.json": locks(), "trivy-image-api-vuln.json": image("signalnest-api:ci", 45),
                "trivy-image-reader-vuln.json": image("signalnest-revision-reader:ci", 3)}
        self.pyloc = str(self.root / "toolcache" / "Python" / "3.12.15" / "x64")   # an interpreter prefix, absolute as in CI
        for env, count, rep_name, inv_name in ((".reader-venv", 17, "trivy-reader-tooling-venv-vuln.json", "reader-tooling-venv-installed.json"),
                                               (".security-venv", 67, "trivy-security-tooling-venv-vuln.json", "security-tooling-venv-installed.json"),
                                               (self.pyloc, 2, "trivy-reader-tooling-syspy-vuln.json", "reader-tooling-syspy-installed.json")):
            (self.root / env).mkdir(parents=True)
            docs[rep_name], docs[inv_name] = tooling(self.root / env, env, count)
        for name, doc in docs.items():
            (self.reports / name).write_text(json.dumps(doc), encoding="utf-8")
        self.saved_load_policy = gate.load_policy
        self.saved_pyloc = os.environ.get("pythonLocation")
        os.environ["pythonLocation"] = self.pyloc

    def tearDown(self):
        gate.load_policy = self.saved_load_policy
        if self.saved_pyloc is None:
            os.environ.pop("pythonLocation", None)
        else:
            os.environ["pythonLocation"] = self.saved_pyloc
        os.chdir(self.cwd)
        self.tmp.cleanup()

    def test_the_interpreter_cases_need_python_location(self):
        os.environ.pop("pythonLocation")
        with self.assertRaises(ctl.ControlError):
            ctl.build_case("baseline-reader-syspy", self.reports, self.root / "w")
        self.assertEqual(self.main("check", "reader", str(self.reports)), gate.EXIT_REPORT)

    def main(self, *argv) -> int:
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return ctl.main(list(argv))

    def run_all(self) -> dict:
        out = {}
        for group in ctl.GROUPS:
            out.update(dict(ctl.check(group, self.reports)))
        return out

    def test_every_case_reaches_its_stated_outcome_under_the_tracked_policy(self):
        results = self.run_all()
        self.assertEqual(set(results), set(ctl.CASES))
        self.assertEqual([n for n, ok in results.items() if not ok], [])
        for name in ctl.CASES:
            text = (self.reports / f"{ctl.SUMMARY_PREFIX}{name}.summary.txt").read_text()
            self.assertTrue(text.rstrip().endswith(gate.RESULT_CONTROL_OK), name)

    def test_under_the_superseded_policy_exactly_the_pass_through_cases_fail(self):
        real = gate.load_policy

        def superseded(path):
            doc = real(path)
            doc["vulnerabilities"]["severity_rules"]["CRITICAL"] = {"fail_scopes": ["image", "runtime"], "fail_requires_fix_available": True}
            return doc
        gate.load_policy = superseded
        results = self.run_all()
        self.assertEqual({n for n, ok in results.items() if not ok}, PASS_THROUGH)
        self.assertEqual(self.main(*["check", "container", str(self.reports)]), gate.EXIT_CONTROL)

    def test_the_driver_exits_zero_only_when_every_case_behaves(self):
        self.assertEqual(self.main(*["check", "container", str(self.reports)]), 0)
        self.assertEqual(self.main(*["check", "reader", str(self.reports)]), 0)
        self.assertEqual(self.main(*["check", "elsewhere", str(self.reports)]), gate.EXIT_USAGE)

    def test_a_missing_report_or_an_unbuildable_case_stops_the_run(self):
        (self.reports / "trivy-security-tooling-venv-vuln.json").unlink()
        self.assertEqual(self.main(*["check", "container", str(self.reports)]), gate.EXIT_REPORT)
        with self.assertRaises(ctl.ControlError):
            ctl.build_case("critical-nofix-security-tooling", self.reports, self.root / "w")
        doc = json.loads((self.reports / "trivy-locks-vuln.json").read_text())
        doc["Results"] = [r for r in doc["Results"] if r["Target"] != "security/requirements-semgrep.txt"]
        (self.reports / "trivy-locks-vuln.json").write_text(json.dumps(doc))
        with self.assertRaises(ctl.ControlError):
            ctl.build_case("critical-fix-security-tool-lock", self.reports, self.root / "w")

    def test_a_mutation_that_changes_nothing_stops_the_run(self):
        saved = ctl._inject
        try:
            ctl._inject = lambda *a, **k: None
            with self.assertRaises(ctl.ControlError):
                ctl.build_case("critical-nofix-runtime-lock", self.reports, self.root / "w")
        finally:
            ctl._inject = saved

    def test_dropping_a_vendored_copy_is_not_what_the_incompleteness_case_does(self):
        ctl.build_case("reader-tooling-package-not-scanned", self.reports, self.root / "w")
        built = json.loads((self.root / "w" / "reader-tooling-package-not-scanned.report.json").read_text())
        names = {p["Name"] for r in built["Results"] for p in r["Packages"]}
        self.assertIn("vendored", names)
        self.assertEqual(len(names), 17)          # 17 installed + 1 vendored, one installed distribution removed


if __name__ == "__main__":
    unittest.main()
