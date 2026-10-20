"""No scanner-side suppression on the merge path (P6-CI-6 closure, operator order 2026-10-19).

Trivy reads a `.trivyignore` and a `trivy.yaml` from its working directory, and aquasecurity/trivy-action
forwards ignore files, severity filters and `ignore-unfixed` through its inputs and TRIVY_* variables.
Each of those removes findings BEFORE security/gate.py reads the report, where neither the gate nor a
control could notice. These assertions pin what the merge-path scans run with: no ignore or
configuration file at the repository root (the scans' working directory), the two tracked Trivy
configurations exactly as reviewed, and every trivy-action step of ci.yml in one reviewed line shape with
reviewed inputs only. Every trivy-action step must pass one of the reviewed configurations, and each of those
pins `ignorefile` to the tracked, empty security/empty-ignorefile: with an explicit configuration and ignore
file, Trivy reads no ignore or configuration file from its working directory, so such a file created there
by ANY code the jobs run (a script, a test, a background process) cannot filter a scan, whatever its timing. The step check reads LINE SHAPES, not YAML meaning, so it fails closed: a quoted
key, a flow mapping, another indentation or any unrecognised line is rejected rather than interpreted.
No suppression file name may appear in the workflow outside the two reviewed configuration paths. In every
job of ci.yml (all six run Trivy since the P6-CI-6 coverage-gap tranche of 2026-10-20: the four API-venv jobs
scan their installed environments, the two security-scanned jobs also the binaries they execute), Trivy may run
ONLY through the reviewed trivy-action steps: their number is
pinned, every `uses:` reference is from a reviewed set (no local actions), no run line invokes `trivy`,
and no scanning or gating step changes its working directory away from the one the run-time refusal
checks. What text cannot see (a file or variable created at run time) is refused by the gate itself
(gate.scanner_suppression), whose list of file names is pinned here literally. Each check is also run on
a deliberately suppressed copy, which it must reject. What no mechanical check sees, such as a step that
rewrites a report by other means or an obfuscated command, is left to review of the workflow change.
Run: python3 -m unittest discover -s security
"""
from __future__ import annotations

import pathlib
import re
import sys
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402

ROOT = HERE.parent
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"
# The run-time refusal's file names, pinned literally (a shrunk tuple in gate.py must fail here, not shrink the test).
SUPPRESSION_FILES = (".trivyignore", ".trivyignore.yaml", ".trivyignore.yml", "trivy.yaml", "trivy.yml", ".trivy.yaml", ".trivy.yml")
TRIVY_STEPS = {"frontend-quality": 2, "backend-quality": 3, "migration-and-contract": 2, "container-build": 17,
               "integration-smoke": 2, "revision-reader": 9}
REVIEWED_USES = {
    "frontend-quality": {"actions/checkout@v7", "actions/setup-node@v6", "actions/setup-python@v6", "actions/upload-artifact@v7",
                         "aquasecurity/trivy-action@v0.36.0"},
    "backend-quality": {"actions/checkout@v7", "actions/setup-python@v6", "actions/upload-artifact@v7", "aquasecurity/trivy-action@v0.36.0"},
    "migration-and-contract": {"actions/checkout@v7", "actions/setup-node@v6", "actions/setup-python@v6", "actions/upload-artifact@v7",
                               "aquasecurity/trivy-action@v0.36.0"},
    "integration-smoke": {"actions/checkout@v7", "actions/setup-python@v6", "actions/upload-artifact@v7", "aquasecurity/trivy-action@v0.36.0"},
    "container-build": {"actions/checkout@v7", "actions/setup-python@v6", "actions/upload-artifact@v7", "aquasecurity/trivy-action@v0.36.0",
                        "docker/build-push-action@v6", "docker/setup-buildx-action@v3"},
    "revision-reader": {"actions/checkout@v7", "actions/setup-python@v6", "actions/upload-artifact@v7", "aquasecurity/trivy-action@v0.36.0",
                        "docker/build-push-action@v6", "opentofu/setup-opentofu@v1"},
}
TRIVY_WORD = re.compile(r"(?:^|[\s;&|(`$\"'=/])trivy(?=[\s)\"';&|]|$)")
SCAN_OR_GATE = re.compile(r"trivy-action|security/gate\.py|security/vuln_policy_controls\.py|security/iac_exception_controls\.py")
ALLOWED_INPUTS = {"version", "scan-type", "scan-ref", "image-ref", "scanners", "format", "output", "exit-code", "list-all-pkgs",
                  "skip-dirs", "trivy-config"}
ALLOWED_VALUES = {"exit-code": {"'0'"}, "skip-dirs": {"security/controls"}, "version": {"v0.76.0"},
                  "trivy-config": {"security/trivy.yaml", "security/controls/trivy-control.yaml", "security/trivy-scan.yaml"}}
EMPTY_IGNOREFILE = "security/empty-ignorefile"
# The effective (non-comment, non-blank) lines of the two tracked Trivy configurations, as reviewed.
CONFIGS = {
    "security/trivy.yaml": ["scan:", "  file-patterns:", '    - "pip:requirements-.*\\\\.txt"', "  include-dev-deps: true",
                            "pkg:", "  include-dev-deps: true", "ignorefile: security/empty-ignorefile"],
    "security/controls/trivy-control.yaml": ["scan:", "  file-patterns:", '    - "pip:pinned-vulnerable\\\\.txt"',
                                             "ignorefile: security/empty-ignorefile"],
    "security/trivy-scan.yaml": ["ignorefile: security/empty-ignorefile"],
}


def effective_lines(text: str) -> list[str]:
    return [l.rstrip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]


# The only line shapes a trivy-action step may have (after its `      - name:` line).
STEP_LINES = (r"        if: always\(\)", r"        uses: aquasecurity/trivy-action@v0\.36\.0", r"        with:", r"\s*", r"      #.*")
INPUT_LINE = r"          ([a-z][a-z0-9-]*): ([A-Za-z0-9_./:'-]+|\$\{\{ env\.pythonLocation \}\})"


def trivy_step_problems(text: str) -> tuple[int, list[str]]:
    """(number of trivy-action steps, problems) for a workflow text."""
    lines, problems, steps = text.splitlines(), [], 0
    starts = [i for i, l in enumerate(lines) if re.match(r"^\s*- ", l) and len(l) - len(l.lstrip()) <= 6]
    for k, start in enumerate(starts):
        block = lines[start: starts[k + 1] if k + 1 < len(starts) else len(lines)]
        if not any("trivy-action" in l for l in block):
            continue
        steps += 1
        name = block[0].strip()
        if not any(re.fullmatch(r"          trivy-config: \S+", l) for l in block):
            problems.append(f"{name}: a trivy-action step passes no reviewed configuration (trivy-config)")
        if not re.fullmatch(r"      - name: \S.*", block[0]):
            problems.append(f"{name}: a trivy-action step must start with its `- name:` line")
        for l in block[1:]:
            m = re.fullmatch(INPUT_LINE, l)
            if m:
                key, value = m.group(1), m.group(2)
                if key not in ALLOWED_INPUTS:
                    problems.append(f"{name}: input {key!r} is not a reviewed trivy-action input")
                elif key in ALLOWED_VALUES and value not in ALLOWED_VALUES[key]:
                    problems.append(f"{name}: {key} {value!r} is not a reviewed value")
            elif not any(re.fullmatch(shape, l) for shape in STEP_LINES):
                problems.append(f"{name}: line {l!r} is not in a reviewed trivy-action step shape")
    if "TRIVY_" in text.upper():
        problems.append("a TRIVY_* variable appears in the workflow")
    for m in re.finditer(r"[\w./-]*(?:trivyignore|trivy\.ya?ml|trivy-control\.ya?ml)\b", text, re.IGNORECASE):
        if m.group(0) not in ("security/trivy.yaml", "security/controls/trivy-control.yaml"):
            problems.append(f"the workflow names {m.group(0)!r}, a Trivy ignore or configuration file outside the reviewed paths")
    return steps, problems


def job_text(text: str, job: str) -> str:
    start = text.index(f"\n  {job}:\n")
    nxt = re.search(r"\n  [a-z0-9-]+:\n", text[start + 1:])
    return text[start: start + 1 + nxt.start()] if nxt else text[start:]


def step_blocks(body: str) -> list[list[str]]:
    lines = body.splitlines()
    starts = [i for i, l in enumerate(lines) if re.match(r"^      - ", l)]
    return [lines[s: starts[k + 1] if k + 1 < len(starts) else len(lines)] for k, s in enumerate(starts)]


def trivy_route_problems(text: str) -> list[str]:
    """Problems with HOW Trivy runs in the two security-scanned jobs (only through the reviewed steps)."""
    problems = []
    for job, expected in TRIVY_STEPS.items():
        body = job_text(text, job)
        count = sum(1 for b in step_blocks(body) if any(re.fullmatch(r"        uses: aquasecurity/trivy-action@v0\.36\.0", l) for l in b))
        if count != expected:
            problems.append(f"{job}: {count} trivy-action steps, expected {expected}")
        uses = {m.group(1) for m in re.finditer(r"(?:^|[\s{,])['\"]?uses['\"]?\s*:\s*['\"]?([^\s,}'\"]+)", body, re.M)}
        if uses != REVIEWED_USES[job]:
            problems.append(f"{job}: uses {sorted(uses ^ REVIEWED_USES[job])} differ from the reviewed set")
        for l in body.splitlines():
            stripped = l.strip()
            if stripped.startswith(("#", "- name:")):
                continue
            if TRIVY_WORD.search(l):
                problems.append(f"{job}: a run line invokes trivy directly: {stripped!r}")
        for b in step_blocks(body):
            if SCAN_OR_GATE.search("\n".join(b)) and any(re.match(r"^        working-directory:", l) for l in b):
                problems.append(f"{job}: {b[0].strip()} runs a scan or gate in another working directory")
    if re.search(r"uses['\"]?\s*:\s*['\"]?\.{1,2}/", text):
        problems.append("a local action is used")
    for m in re.finditer(r"defaults['\"]?\s*:\s*\{[^\n]*working-directory", text):
        problems.append(f"a flow-style defaults block sets a working directory: {m.group(0)!r}")
    lines = text.splitlines()
    for i, l in enumerate(lines):
        m = re.match(r"^(\s*)defaults:\s*$", l)
        if not m:
            continue
        for nxt in lines[i + 1:]:
            if nxt.strip() and len(nxt) - len(nxt.lstrip()) <= len(m.group(1)):
                break
            if "working-directory" in nxt:
                problems.append(f"a defaults block sets a working directory: {nxt.strip()!r}")
    return problems


class ScannerConfiguration(unittest.TestCase):
    def test_no_ignore_or_configuration_file_where_the_scans_run(self):
        for name in SUPPRESSION_FILES:
            with self.subTest(name):
                self.assertFalse((ROOT / name).exists(), f"{name} at the repository root would filter the merge-path scans")

    def test_the_tracked_trivy_configurations_hold_only_the_reviewed_settings(self):
        for rel, expected in CONFIGS.items():
            with self.subTest(rel):
                self.assertEqual(effective_lines((ROOT / rel).read_text(encoding="utf-8")), expected)

    def test_every_trivy_step_uses_only_reviewed_inputs(self):
        steps, problems = trivy_step_problems(WORKFLOW.read_text(encoding="utf-8"))
        self.assertEqual(steps, sum(TRIVY_STEPS.values()), "the trivy-action steps were not all found; the check would be vacuous")
        self.assertEqual(problems, [])

    def test_the_step_check_rejects_a_suppressed_workflow(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        anchor = "          list-all-pkgs: true\n"
        self.assertIn(anchor, text)
        for extra in ("          ignore-unfixed: true\n", "          severity: CRITICAL\n", "          trivyignores: .trivyignore\n",
                      "          vuln-severity-source: nvd\n",
                      "          skip-files: package-lock.json\n", "          skip-dirs: apps\n", "          exit-code: '1'\n"):
            with self.subTest(extra.strip()):
                _, problems = trivy_step_problems(text.replace(anchor, anchor + extra, 1))
                self.assertTrue(problems)
        _, problems = trivy_step_problems(text.replace("        uses: aquasecurity/trivy-action@v0.36.0\n",
                                                       "        uses: aquasecurity/trivy-action@v0.36.0\n        env:\n          TRIVY_SEVERITY: CRITICAL\n", 1))
        self.assertTrue(problems)

    def test_the_step_check_rejects_equivalent_yaml_spellings_and_named_files(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        anchor = "          list-all-pkgs: true\n"
        variants = {
            "quoted key": text.replace(anchor, anchor + "          'ignore-unfixed': true\n", 1),
            "double-quoted key": text.replace(anchor, anchor + '          "severity": CRITICAL\n', 1),
            "deeper indentation": text.replace(anchor, anchor + "            ignore-unfixed: true\n", 1),
            "flow mapping": text.replace("        with:\n          version: v0.76.0\n", "        with: {version: v0.76.0, ignore-unfixed: true}\n", 1),
            "an id or continue-on-error line": text.replace(anchor, anchor + "        continue-on-error: true\n", 1),
            "a run step writes .trivyignore": text.replace("        run: mkdir -p security-reports\n",
                                                           "        run: mkdir -p security-reports && printf 'CVE-2020-14343\\n' > .trivyignore\n", 1),
            "a run step writes trivy.yaml": text.replace("        run: mkdir -p security-reports\n",
                                                         "        run: mkdir -p security-reports && printf 'severity: [LOW]\\n' > trivy.yaml\n", 1),
            "lower-case variable": text + "\n# trivy_severity=CRITICAL\n",
        }
        for label, mutated in variants.items():
            with self.subTest(label):
                self.assertNotEqual(mutated, text, "the variant did not apply")
                _, problems = trivy_step_problems(mutated)
                self.assertTrue(problems)

    def test_every_reviewed_configuration_pins_the_empty_ignore_file(self):
        self.assertEqual((ROOT / EMPTY_IGNOREFILE).read_bytes(), b"", "the pinned ignore file must stay empty")
        self.assertEqual(set(CONFIGS), ALLOWED_VALUES["trivy-config"])
        for rel, expected in CONFIGS.items():
            with self.subTest(rel):
                self.assertIn(f"ignorefile: {EMPTY_IGNOREFILE}", expected)

    def test_a_trivy_step_without_a_reviewed_configuration_is_rejected(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        line = "          trivy-config: security/trivy-scan.yaml\n"
        self.assertIn(line, text)
        _, problems = trivy_step_problems(text.replace(line, "", 1))
        self.assertTrue(any("passes no reviewed configuration" in p for p in problems))
        _, problems = trivy_step_problems(text.replace(line, "          trivy-config: security/other.yaml\n", 1))
        self.assertTrue(problems)

    def test_the_run_time_refusal_covers_the_pinned_file_names(self):
        self.assertEqual(tuple(gate.SCANNER_SUPPRESSION_FILES), SUPPRESSION_FILES)

    def test_trivy_runs_only_through_the_reviewed_steps(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        jobs = set(re.findall(r"(?m)^  ([a-z0-9-]+):$", text.split("\njobs:\n", 1)[1]))
        self.assertEqual(jobs, set(TRIVY_STEPS), "every job of ci.yml is checked")
        self.assertEqual(set(REVIEWED_USES), set(TRIVY_STEPS))
        self.assertEqual(trivy_route_problems(text), [])

    def test_the_binary_pins_follow_the_versions_the_workflow_installs(self):
        import json
        pins = json.loads((ROOT / "security" / "scanner-binaries.json").read_text(encoding="utf-8"))
        text = WORKFLOW.read_text(encoding="utf-8")
        versions = set(re.findall(r"(?m)^          version: v(\S+)$", text))
        self.assertEqual(versions, {pins["binaries"]["trivy"]["version"]})
        self.assertEqual(set(re.findall(r"(?m)^          tofu_version: (\S+)$", text)), {pins["binaries"]["tofu"]["version"]})
        self.assertIn(f'EXPECTED_TOFU_VERSION = "{pins["binaries"]["tofu"]["version"]}"',
                      (ROOT / "scripts" / "check_toolchain_integrity.py").read_text(encoding="utf-8"))
        for name, pin in pins["binaries"].items():
            with self.subTest(name):
                self.assertEqual(pin["module_version"], "v" + pin["version"])
                self.assertRegex(pin["sha256"], r"^[0-9a-f]{64}$")
                self.assertIn(pin["version"], pin["release_asset"])
        self.assertEqual(pins["jobs"], {"container-build": ["trivy"], "revision-reader": ["trivy", "tofu"]})
        for job, names in pins["jobs"].items():   # each job that executes a pinned binary gates it, by that job's list
            with self.subTest(job):
                body = job_text(text, job)
                self.assertIn(f"--kind binary --artifact .scanner-binaries --binary-job {job} --binary-pins security/scanner-binaries.json", body)
                self.assertIn(f"python3 security/stage_inputs.py binaries --pins security/scanner-binaries.json --job {job} ", body)
                self.assertIn("--expect-pass scanner-binaries", body)
        self.assertEqual(text.count("opentofu/setup-opentofu@"), 1, "OpenTofu runs only in the job that analyses its binary")

    def test_the_route_check_rejects_other_ways_of_running_trivy(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        anchor = "        run: mkdir -p security-reports\n"
        self.assertEqual(text.count(anchor), 1)
        variants = {
            "a direct trivy run": anchor + "      - name: Rescan\n        run: trivy image --ignore-unfixed -o security-reports/trivy-image-reader-vuln.json x\n",
            "trivy behind a path": anchor + "      - name: Rescan\n        run: /usr/local/bin/trivy fs .\n",
            "a local composite action": anchor + "      - name: Rescan\n        uses: ./.github/actions/rescan\n",
            "another action": anchor + "      - name: Rescan\n        uses: someone/scan-action@v1\n",
            "a gate in another directory": anchor + "      - name: Gate\n        working-directory: apps\n        run: python3 ../security/gate.py vuln x --name y\n",
            "an extra trivy-action step": anchor + "      - name: Extra\n        uses: aquasecurity/trivy-action@v0.36.0\n",
            "a flow-style step": anchor + "      - {name: Rescan, uses: someone/scanner-action@v1}\n",
            "a quoted uses key": anchor + "      - name: Rescan\n        'uses': someone/scanner-action@v1\n",
        }
        for label, z in variants.items():
            with self.subTest(label):
                self.assertTrue(trivy_route_problems(text.replace(anchor, z, 1)))
        for label, mutated in (("a job-level default working directory", text.replace("    runs-on: ubuntu-latest\n",
                                    "    runs-on: ubuntu-latest\n    defaults:\n      run:\n        working-directory: apps\n", 1)),
                               ("a workflow-level default working directory", text.replace("defaults:\n  run:\n",
                                    "defaults:\n  run:\n    working-directory: apps\n", 1)),
                               ("a flow-style job-level default working directory", text.replace("    runs-on: ubuntu-latest\n",
                                    "    runs-on: ubuntu-latest\n    defaults: {run: {working-directory: w}}\n", 1)),
                               ("a flow-style workflow-level default working directory", text.replace("defaults:\n  run:\n    shell:",
                                    "defaults: {run: {working-directory: w}}\nx-old-defaults:\n  run:\n    shell:", 1))):
            with self.subTest(label):
                self.assertNotEqual(mutated, text)
                self.assertTrue(trivy_route_problems(mutated))

    def test_the_configuration_check_rejects_a_suppressed_configuration(self):
        text = (ROOT / "security/trivy.yaml").read_text(encoding="utf-8") + "vulnerability:\n  ignore-unfixed: true\n"
        self.assertNotEqual(effective_lines(text), CONFIGS["security/trivy.yaml"])
        for rel in CONFIGS:  # no severity-source override can be added to a reviewed configuration (operator decision 1, 2026-10-19)
            with self.subTest(rel):
                text = (ROOT / rel).read_text(encoding="utf-8") + "vulnerability:\n  severity-source:\n    - nvd\n"
                self.assertNotEqual(effective_lines(text), CONFIGS[rel])


if __name__ == "__main__":
    unittest.main()
