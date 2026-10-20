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
import hashlib
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
JOBS = {"container": "container-build", "reader": "revision-reader", "frontend": "frontend-quality", "backend": "backend-quality",
        "migrations": "migration-and-contract", "integration": "integration-smoke"}
# REPORTS key -> {--name of a real gate step whose arguments the driver must reuse: the inventory that step must read}
REAL_STEPS = {"locks": {"locks-vuln": None}, "api-image": {"image-api-vuln": None}, "reader-image": {"image-reader-vuln": None},
              "reader-tooling": {"reader-tooling-venv": "reader-tooling-venv-installed.json",
                                 "reader-tooling-venv-final": "reader-tooling-venv-installed-final.json"},
              "reader-syspy": {"reader-tooling-syspy": "reader-tooling-syspy-installed.json",
                               "reader-tooling-syspy-final": "reader-tooling-syspy-installed-final.json"},
              "security-tooling": {"security-tooling-venv": "security-tooling-venv-installed.json",
                                   "security-tooling-venv-final": "security-tooling-venv-installed-final.json"},
              "container-binaries": {"scanner-binaries": None}, "reader-binaries": {"scanner-binaries": None},
              "backend-locktools": {"locktools": "locktools-installed.json", "locktools-final": "locktools-installed-final.json"}}
for _g in ctl.API_JOBS:
    REAL_STEPS[f"{_g}-api-venv"] = {"api-venv": "api-venv-installed.json", "api-venv-final": "api-venv-installed-final.json"}
    REAL_STEPS[f"{_g}-binaries"] = {"scanner-binaries": None}
# the inventory commands, in workflow order, per tooling report: (first inventory, final inventory)
INVENTORY_COMMANDS = {"reader-tooling": ".reader-venv/bin/python -m pip inspect > security-reports/reader-tooling-venv-installed{}.json",
                      "reader-syspy": "python3 -m pip inspect > security-reports/reader-tooling-syspy-installed{}.json",
                      "security-tooling": ".security-venv/bin/python -m pip inspect > security-reports/security-tooling-venv-installed{}.json",
                      "backend-locktools": ".locktools/bin/python -m pip inspect > security-reports/locktools-installed{}.json"}
for _g in ctl.API_JOBS:
    INVENTORY_COMMANDS[f"{_g}-api-venv"] = "apps/api/.venv/bin/python -m pip inspect > security-reports/api-venv-installed{}.json"
# the copies the driver passes in place of the real step's files: flag -> the real step's file (a format for the inventory)
COPIED = {"--installed-inventory": None, "--vendored-report": "security-reports/trivy-vendored-pins-vuln.json",
          "--binary-manifest": "security-reports/scanner-binaries-manifest.json"}
# The assessment's pass-through classes: under the superseded policy these, and only these, go BAD.
PASS_THROUGH = {
    "critical-nofix-api-image-os", "critical-nofix-api-image-python", "critical-nofix-runtime-lock",
    "critical-nofix-npm-runtime", "critical-fix-npm-dev", "critical-nofix-npm-dev",
    "critical-fix-security-tool-lock", "critical-nofix-security-tool-lock", "critical-nofix-reader-tooling-lock",
    "critical-nofix-security-tooling", "critical-nofix-reader-image", "critical-fix-reader-tooling",
    "critical-nofix-reader-tooling", "critical-status-fixed-no-version-runtime-lock", "critical-wontfix-api-image-os",
    "critical-fixdeferred-api-image-os", "critical-eol-runtime-lock", "critical-ghsa-npm-dev", "critical-unmatched-uid-runtime-lock",
    "critical-nofix-api-image-gobinary", "critical-nofix-reader-tooling-vendored", "critical-nofix-reader-tooling-node-pkg",
    "critical-wontfix-reader-image-os", "critical-nofix-reader-syspy",
    "critical-nofix-security-tooling-vendored-pin", "critical-nofix-container-binary-trivy", "critical-nofix-reader-tooling-vendored-pin",
    "critical-nofix-reader-syspy-vendored-pin", "critical-nofix-reader-binary-trivy", "critical-nofix-reader-binary-tofu"}
ENV_KEYS = [f"{g}-api-venv" for g in ctl.API_JOBS] + ["backend-locktools"]
PASS_THROUGH |= {f"critical-nofix-{k}" for k in ENV_KEYS} | {f"critical-nofix-{k}-vendored-pin" for k in ENV_KEYS}
PASS_THROUGH |= {f"critical-nofix-{g}-binary-trivy" for g in ctl.API_JOBS}
READ_ONLY_ENVS = ("apps/api/.venv", ".locktools")       # the gates of these environments carry --require-read-only
SITE = "lib/python3.12/site-packages"
PIP_TARGETS = ["apps/api/requirements-runtime.txt", "apps/api/requirements-dev.txt", "apps/api/requirements-bootstrap.txt",
               "apps/api/requirements-locktools.txt", "scripts/requirements-gate.txt",
               "scripts/requirements-gate-hashes.txt", "security/requirements-semgrep.txt",
               "apps/revision-reader/requirements-dev.txt"]
VENDOR_PINS = [("vend-alpha", "1.0"), ("vend-beta", "2.0")]


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


def tooling(env: pathlib.Path, artifact: str, count: int, extra: tuple = ()) -> tuple[dict, dict]:
    """An installed environment of `count` distributions (pip among them, with a vendor.txt on disk), plus `extra`."""
    dists = [(f"tool-pkg-{i}", f"1.{i}") for i in range(count - 1 - len(extra))] + [("pip", "26.2.1")] + list(extra)
    inv = {"version": "1", "pip_version": "26.2.1", "installed": [
        {"metadata": {"name": n, "version": v}, "metadata_location": str(env / SITE / f"{n.replace('-', '_')}-{v}.dist-info")} for n, v in dists]}
    vendor = env / SITE / "pip" / "_vendor"
    vendor.mkdir(parents=True, exist_ok=True)
    (vendor / "vendor.txt").write_text("".join(f"{n}=={v}\n" for n, v in VENDOR_PINS), encoding="utf-8")
    rep = {"SchemaVersion": 2, "ArtifactName": artifact, "ArtifactType": "filesystem", "Results": [
        {"Target": "Python", "Class": "lang-pkgs", "Type": "python-pkg", "Packages": [
            {"Name": n, "Version": v, "FilePath": f"{SITE}/{n.replace('-', '_')}-{v}.dist-info/METADATA", "Identifier": {"UID": f"t{i}"}}
            for i, (n, v) in enumerate(dists)] + [{"Name": "vendored", "Version": "1.0", "FilePath": "", "Identifier": {"UID": "vend"}}]}]}
    return rep, inv


def vendored_result(label: str) -> dict:
    return {"Target": f"{label}/requirements-vendored-pip.txt", "Class": "lang-pkgs", "Type": "pip",
            "Packages": [{"Name": n, "Version": v, "Identifier": {"UID": f"{label}-{n}"}} for n, v in VENDOR_PINS]}


def lock_text(pins: list) -> str:
    return "".join(f"{n}=={v} \\\n    --hash=sha256:{'ab' * 32}\n" for n, v in pins)


def binaries(root: pathlib.Path, names: list) -> tuple[dict, dict, dict]:
    """Staged synthetic binaries, the pins naming their digests, the staging manifest and a Go binary report."""
    stage = root / ".scanner-binaries"
    stage.mkdir(exist_ok=True)
    pins, manifest, results = {}, {}, []
    for name in names:
        data = f"synthetic {name} binary".encode()
        (stage / name).write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        pins[name] = {"version": "1.0.0", "module": f"example.invalid/{name}", "module_version": "v1.0.0", "sha256": digest}
        manifest[name] = {"origin": str(stage / name), "sha256": digest}
        results.append({"Target": name, "Class": "lang-pkgs", "Type": "gobinary", "Packages": [
            {"Name": f"example.invalid/{name}", "Version": "v1.0.0", "Relationship": "root", "Identifier": {"UID": f"{name}-root"}},
            {"Name": "stdlib", "Version": "v1.26.5", "Relationship": "direct", "Identifier": {"UID": f"{name}-std"}},
            {"Name": "golang.org/x/net", "Version": "v0.1.0", "Relationship": "direct", "Identifier": {"UID": f"{name}-net"}}]})
    report = {"SchemaVersion": 2, "ArtifactName": ".scanner-binaries", "ArtifactType": "filesystem", "Results": results}
    return pins, {"version": 1, "binaries": manifest}, report


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
        self.assertEqual(set(REAL_STEPS), set(ctl.REPORTS))
        for key, (rep_name, args, inv_name, vend_name, man_name) in ctl.REPORTS.items():
            group_jobs = {JOBS[c[0]] for c in ctl.CASES.values() if c[1] == key}
            self.assertEqual(len(group_jobs), 1, key)
            steps = gate_steps(job_text(self.text, group_jobs.pop()))
            for name, step_inv in REAL_STEPS[key].items():
                with self.subTest(report=key, step=name):
                    self.assertIn(name, steps, f"no real gate step --name {name}")
                    path, toks = steps[name]
                    self.assertEqual(path, f"security-reports/{rep_name}")
                    rest = list(toks)
                    for flag, real in COPIED.items():
                        present = {"--installed-inventory": inv_name, "--vendored-report": vend_name, "--binary-manifest": man_name}[flag]
                        self.assertEqual(rest.count(flag), 1 if present else 0, f"{flag} in {name}")
                        if present:
                            k = rest.index(flag)
                            want = f"security-reports/{step_inv}" if flag == "--installed-inventory" else real
                            self.assertEqual(rest[k + 1], want, f"{name} reads another step's {flag[2:]}")
                            del rest[k:k + 2]
                    self.assertEqual(rest, args)
            if inv_name is not None:
                self.assertEqual(list(REAL_STEPS[key].values())[0], inv_name)

    def test_tooling_inventories_are_taken_scanned_gated_and_retaken_in_that_order(self):
        for key, cmd in INVENTORY_COMMANDS.items():
            rep_name, _, inv_name, _, _ = ctl.REPORTS[key]
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

    def test_the_rootfs_scanner_control_is_required_in_every_job(self):
        for job in JOBS.values():
            with self.subTest(job):
                text = job_text(self.text, job)
                self.assertIn("--expect-control control-tooling-rootfs", text)
                self.assertIn("scan-ref: security/controls/tooling-env", text)
                self.assertIn("--name control-tooling-rootfs", text)

    def test_the_vendored_pin_scanner_control_is_required_in_every_job(self):
        for job in JOBS.values():
            with self.subTest(job):
                text = job_text(self.text, job)
                self.assertIn("--expect-control control-vendored-pins", text)
                self.assertIn("--env control security/controls/vendored-env security/controls/vendored-env-installed.json", text)
                self.assertIn("scan-ref: .vendored-pins-control", text)
                self.assertIn("--expect-fail\n          --expect-violation target=control/requirements-vendored-pip.txt", text)
                self.assertIn("--vendored-artifact .vendored-pins-control --vendored-target control/requirements-vendored-pip.txt", text)

    def test_the_hash_locked_environments_are_made_read_only_at_the_end_of_their_install(self):
        # No repository code may run between an environment's hash-locked install and `readonly`: the command is the very
        # next line of the same step (round-2 finding A2R-1), and it precedes the first inventory and every gate.
        pairs = {"apps/api/.venv": "--no-deps --no-build-isolation -e apps/api",
                 ".locktools": ".locktools/bin/python -m pip check"}
        for group in ctl.API_JOBS:
            job = job_text(self.text, JOBS[group])
            for env, last_install_line in pairs.items():
                if env == ".locktools" and group != "backend":
                    continue
                with self.subTest(group=group, env=env):
                    cmd = f"python3 security/stage_inputs.py readonly {env}\n"
                    self.assertEqual(job.count(cmd), 1)
                    lines = [l.strip() for l in job.splitlines()]
                    k = lines.index(cmd.strip())
                    self.assertIn(last_install_line, lines[k - 1], "readonly must directly follow the install")
                    self.assertLess(job.index(cmd), job.index("security-reports/api-venv-installed.json"))
                    self.assertLess(job.index(cmd), job.index("--name api-venv\n"))
        for group in set(JOBS) - set(ctl.API_JOBS):
            self.assertNotIn("stage_inputs.py readonly", job_text(self.text, JOBS[group]))

    def test_every_installed_environment_is_staged_for_the_vendored_pin_scan_it_is_gated_on(self):
        for key, (_, args, inv_name, vend_name, _) in ctl.REPORTS.items():
            if vend_name is None:
                continue
            job = job_text(self.text, JOBS[{c[0] for c in ctl.CASES.values() if c[1] == key}.pop()])
            label = args[args.index("--vendored-target") + 1].split("/")[0]
            root = args[args.index("--env-root") + 1]
            root = '"' + root + '"' if root.startswith("${{") else root
            with self.subTest(key):
                self.assertRegex(job, re.escape(f"--env {label} {root} security-reports/{inv_name}") + r"(\s|$)")
                self.assertIn("python3 security/stage_inputs.py vendored --out .vendored-pins ", job)
                self.assertIn("scan-ref: .vendored-pins\n", job)

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
        docs = {"trivy-locks-vuln.json": locks(), "trivy-image-api-vuln.json": image("signalnest-api:ci", 45),
                "trivy-image-reader-vuln.json": image("signalnest-revision-reader:ci", 3)}
        self.pyloc = str(self.root / "toolcache" / "Python" / "3.12.15" / "x64")   # an interpreter prefix, absolute as in CI
        vendored = {"SchemaVersion": 2, "ArtifactName": ".vendored-pins", "ArtifactType": "filesystem", "Results": []}
        for env, count, rep_name, inv_name, label, extra in (
                (".reader-venv", 17, "trivy-reader-tooling-venv-vuln.json", "reader-tooling-venv-installed.json", "reader-venv", ()),
                (".security-venv", 67, "trivy-security-tooling-venv-vuln.json", "security-tooling-venv-installed.json", "security-venv", ()),
                (self.pyloc, 2, "trivy-reader-tooling-syspy-vuln.json", "reader-tooling-syspy-installed.json", "reader-syspy", ()),
                ("apps/api/.venv", 58, "trivy-api-venv-vuln.json", "api-venv-installed.json", "api-venv", (("signalnest_api", "0.1.0"),)),
                (".locktools", 8, "trivy-locktools-vuln.json", "locktools-installed.json", "locktools", ())):
            (self.root / env).mkdir(parents=True)
            docs[rep_name], docs[inv_name] = tooling(self.root / env, env, count, extra)
            vendored["Results"].append(vendored_result(label))
        docs[ctl.VENDORED] = vendored
        for it in docs["api-venv-installed.json"]["installed"]:     # the editable project, as pip inspect records it
            if it["metadata"]["name"] == "signalnest_api":
                it["direct_url"] = {"url": "file://" + str(self.root / "apps" / "api"), "dir_info": {"editable": True}}
        # the hash locks the API venv and .locktools reconcile against (cwd-relative, as in CI)
        api = [(it["metadata"]["name"], it["metadata"]["version"]) for it in docs["api-venv-installed.json"]["installed"]]
        (self.root / "apps" / "api").mkdir(parents=True, exist_ok=True)
        (self.root / "apps" / "api" / "requirements-bootstrap.txt").write_text(lock_text([x for x in api if x[0] == "pip"]), encoding="utf-8")
        (self.root / "apps" / "api" / "requirements-dev.txt").write_text(lock_text([x for x in api if x[0] not in ("pip", "signalnest_api")]), encoding="utf-8")
        tools = [(it["metadata"]["name"], it["metadata"]["version"]) for it in docs["locktools-installed.json"]["installed"]]
        (self.root / "apps" / "api" / "requirements-locktools.txt").write_text(lock_text(tools), encoding="utf-8")
        # the staged binaries: container-build analyses trivy, revision-reader trivy and tofu (each job its own reports)
        pins, manifest_r, report_r = binaries(self.root, ["trivy", "tofu"])
        (self.root / "security").mkdir()
        (self.root / "security" / "scanner-binaries.json").write_text(json.dumps(
            {"jobs": {"container-build": ["trivy"], "revision-reader": ["trivy", "tofu"],
                      **{ctl.API_JOB_IDS[g]: ["trivy"] for g in ctl.API_JOBS}}, "binaries": pins}), encoding="utf-8")
        manifest_c = {"version": 1, "binaries": {"trivy": manifest_r["binaries"]["trivy"]}}
        report_c = dict(report_r, Results=[r for r in report_r["Results"] if r["Target"] == "trivy"])
        self.dirs = {}
        for group in ctl.GROUPS:
            d = self.root / f"security-reports-{group}"
            d.mkdir()
            own = {"trivy-scanner-binaries-vuln.json": report_r if group == "reader" else report_c,
                   ctl.BINARY_MANIFEST: manifest_r if group == "reader" else manifest_c}
            for name, doc in {**docs, **own}.items():
                (d / name).write_text(json.dumps(doc), encoding="utf-8")
            self.dirs[group] = d
        self.reports = self.dirs["container"]
        self.saved_load_policy = gate.load_policy
        self.saved_pyloc = os.environ.get("pythonLocation")
        os.environ["pythonLocation"] = self.pyloc
        for env in READ_ONLY_ENVS:                       # made read-only after the first inventory, as in CI
            self.set_writable(self.root / env, False)

    @staticmethod
    def set_writable(top: pathlib.Path, writable: bool) -> None:
        for here, dirs, files in os.walk(top, topdown=False):
            for n in dirs + files:
                p = os.path.join(here, n)
                if not os.path.islink(p):
                    os.chmod(p, (os.lstat(p).st_mode | 0o200) if writable else (os.lstat(p).st_mode & ~0o222))
        os.chmod(top, (os.lstat(top).st_mode | 0o200) if writable else (os.lstat(top).st_mode & ~0o222))

    def tearDown(self):
        for env in READ_ONLY_ENVS:
            self.set_writable(self.root / env, True)
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
            out.update(dict(ctl.check(group, self.dirs[group])))
        return out

    def test_every_case_reaches_its_stated_outcome_under_the_tracked_policy(self):
        results = self.run_all()
        self.assertEqual(set(results), set(ctl.CASES))
        self.assertEqual([n for n, ok in results.items() if not ok], [])
        for name, case in ctl.CASES.items():
            text = (self.dirs[case[0]] / f"{ctl.SUMMARY_PREFIX}{name}.summary.txt").read_text()
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
        for group in ctl.GROUPS:
            self.assertEqual(self.main(*["check", group, str(self.dirs[group])]), 0, group)
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

    def test_the_vendored_and_binary_cases_change_only_their_own_target(self):
        ctl.build_case("reader-syspy-vendored-pin-not-scanned", self.reports, self.root / "w")
        built = json.loads((self.root / "w" / "reader-syspy-vendored-pin-not-scanned.vendored.json").read_text())
        counts = {r["Target"].split("/")[0]: len(r["Packages"]) for r in built["Results"]}
        self.assertEqual(counts, {"reader-venv": 2, "security-venv": 2, "reader-syspy": 1, "api-venv": 2, "locktools": 2})
        ctl.build_case("reader-binary-tofu-analysis-missing", self.dirs["reader"], self.root / "w")
        built = json.loads((self.root / "w" / "reader-binary-tofu-analysis-missing.report.json").read_text())
        self.assertEqual([r["Target"] for r in built["Results"]], ["trivy"])

    def test_a_tooling_gate_without_the_vendored_pin_scan_fails_while_pip_is_installed(self):
        args, rep, copies = ctl.build_case("baseline-backend-api-venv", self.dirs["backend"], self.root / "w")
        with contextlib.redirect_stdout(io.StringIO()):
            code = gate.main(["--policy", str(HERE / "policy.json"), "--reports-dir", str(self.root / "w"), "vuln", str(rep), "--name", "nov",
                              *[a for i, a in enumerate(args) if a not in ("--vendored-target", "--vendored-artifact")
                                and (i == 0 or args[i - 1] not in ("--vendored-target", "--vendored-artifact"))],
                              "--installed-inventory", str(copies["--installed-inventory"])])
        self.assertEqual(code, gate.EXIT_INCOMPLETE)
        self.assertIn("not accounted for", (self.root / "w" / "nov.summary.txt").read_text())


if __name__ == "__main__":
    unittest.main()
