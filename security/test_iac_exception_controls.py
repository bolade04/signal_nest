"""Assertions over security/iac_exception_controls.py (P6-CI-6): the case list is the one CI requires,
and every mutation still applies to the CURRENT tree.

The case list is enumerated twice — in the driver and in the completeness step of
.github/workflows/ci.yml — so this pins the two against each other in BOTH directions: a case the
driver runs but CI does not require, or one CI requires but the driver no longer builds, fails here
before any scanner runs. Building every case against the real tree proves no mutation anchor has
drifted (the driver refuses a drifted anchor, which would otherwise test nothing).
Run: python3 -m unittest discover -s security
"""
from __future__ import annotations

import json
import pathlib
import re
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import iac_exception_controls as ctl  # noqa: E402

WORKFLOW = HERE.parent / ".github" / "workflows" / "ci.yml"


class ControlCases(unittest.TestCase):
    def test_the_case_list_is_exactly_the_one_ci_requires(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        controls = set(re.findall(r"--expect-control " + re.escape(ctl.SUMMARY_PREFIX) + r"([a-z0-9-]+)", text))
        passes = set(re.findall(r"--expect-pass " + re.escape(ctl.SUMMARY_PREFIX) + r"([a-z0-9-]+)", text))
        mutations = {n for n, (_, token, _, _) in ctl.CASES.items() if token is not None}
        baselines = {n for n, (_, token, _, _) in ctl.CASES.items() if token is None}
        self.assertEqual(controls, mutations, "ci.yml completeness --expect-control list differs from the driver's mutation cases")
        self.assertEqual(passes, baselines, "ci.yml completeness --expect-pass list differs from the driver's baseline")
        self.assertEqual(baselines, {"baseline"})

    def test_every_case_builds_against_the_current_tree(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d) / "cases"
            built = ctl.build(root)
            self.assertEqual(built, list(ctl.CASES))
            def snapshot(case):
                return {str(p.relative_to(root / case)): p.read_bytes() for p in (root / case).rglob("*") if p.is_file()}
            base = snapshot("baseline")
            for name in ctl.CASES:
                if name == "baseline":
                    continue
                self.assertNotEqual(snapshot(name), base, f"case {name} is identical to the baseline — its mutation did nothing")
            # the baseline is a faithful copy: every configuration file and the exceptions file, byte for byte
            exc = json.loads((root / "baseline" / ctl.EXCEPTIONS).read_text())
            self.assertEqual(exc, json.loads((ctl.REPO / ctl.EXCEPTIONS).read_text()))
            for rel, data in base.items():
                if rel != ctl.EXCEPTIONS:
                    self.assertEqual((ctl.REPO / rel).read_bytes(), data, rel)

    def test_a_drifted_anchor_stops_the_build(self):
        saved = dict(ctl.CASES)
        try:
            ctl.CASES["drift-probe"] = ("probe", "X", [("replace", ctl.ECS, "this text is not in the file", "x")], None)
            with tempfile.TemporaryDirectory() as d:
                with self.assertRaises(ctl.ControlError):
                    ctl.build(pathlib.Path(d) / "cases")
        finally:
            ctl.CASES.clear(); ctl.CASES.update(saved)

    def test_a_non_empty_cases_root_is_refused(self):
        with tempfile.TemporaryDirectory() as d:
            (pathlib.Path(d) / "left-over").write_text("x")
            with self.assertRaises(ctl.ControlError):
                ctl.build(pathlib.Path(d))


if __name__ == "__main__":
    unittest.main()
