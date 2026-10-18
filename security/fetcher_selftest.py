"""Deterministic tests for security/fetch_semgrep_rules.py (no network, no live registry).

Run under the hash-locked Semgrep venv, which provides ruamel.yaml:
    .security-venv/bin/python -m unittest discover -s security -p "fetcher_selftest.py"
CI runs exactly that line before fetching, so a fetcher regression fails the job.
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile
import unittest
import urllib.error
from unittest import mock

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import fetch_semgrep_rules as fsr  # noqa: E402

RULES = [{"id": f"inert.fixture.rule-{i}", "message": "inert fixture", "severity": "INFO",
          "languages": ["python"], "pattern": f"never_called_fixture_{i}(...)"} for i in range(4)]


def as_json(rules=RULES) -> bytes:
    return json.dumps({"rules": rules}).encode()


def as_yaml(rules=RULES) -> bytes:
    lines = ["rules:"]
    for r in rules:
        lines += [f"- id: {r['id']}", f"  message: {r['message']}", f"  severity: {r['severity']}",
                  "  languages:", "  - python", f"  pattern: {r['pattern']}"]
    return ("\n".join(lines) + "\n").encode()


class ParseRulesetCase(unittest.TestCase):
    def test_equivalent_json_and_yaml_count_the_same_rules(self):
        self.assertEqual(fsr.parse_ruleset(as_json()), (4, "json"))
        self.assertEqual(fsr.parse_ruleset(as_yaml()), (4, "yaml"))

    def test_yaml_with_leading_comment_and_indentation_is_parsed_structurally(self):
        doc = b"# registry header\n" + as_yaml()
        self.assertEqual(fsr.parse_ruleset(doc), (4, "yaml"))

    def test_refused_documents(self):
        bad = {
            "empty": b"",
            "whitespace": b"  \n",
            "html": b"<html><body>502 Bad Gateway</body></html>",
            "malformed json": b'{"rules": [',
            "malformed yaml": b"rules:\n- id: a\n  message: [unclosed\n",
            "json error body": b'{"errors": [{"message": "not found"}]}',
            "json list": b'[{"id": "a"}]',
            "yaml scalar": b"just a string\n",
            "rules not a list": b'{"rules": {"id": "a"}}',
            "empty rules": b'{"rules": []}',
            "entry without id": json.dumps({"rules": [{"message": "x"}]}).encode(),
            "entry with empty id": json.dumps({"rules": [{"id": "  "}]}).encode(),
            "entry not a mapping": b'{"rules": ["a"]}',
            "not utf-8": b"\xff\xfe\x00r\x00u",
        }
        for label, doc in bad.items():
            with self.subTest(label), self.assertRaises(fsr.RulesetError):
                fsr.parse_ruleset(doc)


class MainCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)
        self.out = self.dir / "semgrep-rules"
        self.copy = self.dir / "reports" / "semgrep-rules-provenance.json"

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, responses, min_bytes=1):
        def fake_fetch(name):
            value = responses[name]
            if isinstance(value, Exception):
                raise value
            return value

        with mock.patch.object(fsr, "fetch", side_effect=fake_fetch), mock.patch.object(fsr, "MIN_BYTES", min_bytes):
            return fsr.main(["fetch_semgrep_rules.py", str(self.out), str(self.copy)])

    def responses(self, **override):
        base = {name: (as_yaml(), "text/yaml; charset=utf-8") for name in fsr.RULESETS}
        base["p/javascript"] = (as_json(), "application/json")
        base.update(override)
        return base

    def test_success_keeps_raw_bytes_and_records_provenance(self):
        resp = self.responses()
        self.assertEqual(self.run_main(resp), 0)
        self.assertFalse(self.out.with_name(self.out.name + ".partial").exists())
        prov = json.loads((self.out / "PROVENANCE.json").read_text())
        self.assertEqual(json.loads(self.copy.read_text()), prov)
        self.assertEqual([r["ruleset"] for r in prov["rulesets"]], list(fsr.RULESETS))
        for rec in prov["rulesets"]:
            raw, ctype = resp[rec["ruleset"]]
            written = (self.out / rec["file"]).read_bytes()
            self.assertEqual(written, raw)  # raw bytes, never normalised
            self.assertEqual(rec["sha256"], hashlib.sha256(raw).hexdigest())
            self.assertEqual((rec["bytes"], rec["rule_ids"], rec["content_type"]), (len(raw), 4, ctype))
            self.assertEqual(rec["format"], "json" if rec["ruleset"] == "p/javascript" else "yaml")
            self.assertEqual(rec["url"], fsr.REGISTRY + rec["ruleset"])
        self.assertEqual(prov["total_rule_ids"], 4 * len(fsr.RULESETS))
        # every file is .yml — Semgrep's directory loader skips .json files silently
        self.assertEqual(sorted(p.suffix for p in self.out.iterdir() if p.name != "PROVENANCE.json"),
                         [".yml"] * len(fsr.RULESETS))
        self.assertIn("fetched_at", prov)

    def assert_nothing_left(self):
        self.assertFalse(self.out.exists(), "an incomplete population must never be left at <out>")
        self.assertFalse(self.out.with_name(self.out.name + ".partial").exists())
        self.assertFalse(self.copy.exists())

    def test_acquisition_failure_leaves_no_rules(self):
        resp = self.responses(**{"p/react": urllib.error.URLError("network unreachable")})
        self.assertEqual(self.run_main(resp), 1)
        self.assert_nothing_left()

    def test_http_error_body_mid_run_leaves_no_rules(self):
        resp = self.responses(**{"p/javascript": (b"<html>503</html>", "text/html")})
        self.assertEqual(self.run_main(resp), 1)
        self.assert_nothing_left()

    def test_insufficient_rule_population_is_refused(self):
        resp = self.responses(**{"p/react": (as_yaml(RULES[:3]), "text/yaml")})
        self.assertEqual(self.run_main(resp), 1)
        self.assert_nothing_left()

    def test_minimum_size_floor_still_applies(self):
        self.assertGreater(fsr.MIN_BYTES, len(as_yaml()))
        with mock.patch.object(fsr, "fetch", return_value=(as_yaml(), "text/yaml")):
            self.assertEqual(fsr.main(["x", str(self.out)]), 1)
        self.assert_nothing_left()

    def test_existing_output_directory_is_refused_and_untouched(self):
        self.out.mkdir()
        stale = self.out / "p-javascript.yml"
        stale.write_text("rules: []\n")
        self.assertEqual(self.run_main(self.responses()), 1)
        self.assertEqual(stale.read_text(), "rules: []\n")
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["p-javascript.yml"])


if __name__ == "__main__":
    unittest.main()
