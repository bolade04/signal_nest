"""Assertions over the IaC exception mechanism of security/gate.py (P6-CI-6), with synthetic trees.

Each test writes a small Terraform tree and a Trivy-shaped misconfiguration report into a temporary
directory and checks the verdict. Nothing here runs a scanner. The negative controls that run the REAL
scanner over mutated copies of the REAL tree are security/iac_exception_controls.py (CI); these tests pin
the gate's own logic, including the fail-closed edges a scanner run would rarely reach.
Run: python3 -m unittest discover -s security
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import pathlib
import sys
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate  # noqa: E402

POLICY = HERE / "policy.json"
TODAY = dt.date(2026, 10, 18)

EGRESS = '''# a comment with "quotes" and ${not_a_template}
resource "aws_security_group" "task" {
  name = "sg"
}

resource "aws_vpc_security_group_egress_rule" "task_https" {
  for_each = toset(["api", "worker"])

  security_group_id = aws_security_group.task[each.key].id
  description       = "${each.key} egress"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"

  tags = {
    Name = "x-${each.key}"
  }
}
'''
EGRESS_LINE = 14  # the cidr_ipv4 line, where Trivy anchors AWS-0104

ALB = '''resource "aws_lb" "this" {
  name               = "alb"
  internal           = false
  load_balancer_type = "application"
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.this.arn
  protocol          = "HTTPS"
  default_action { type = "forward" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = "b"

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}
'''


def tree(root: pathlib.Path, files: dict) -> None:
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)


def mfinding(check, target, line, root, severity="HIGH", resource=None, code=True):
    lines = (root / target).read_text().splitlines()
    cm = {"Resource": "module.x", "StartLine": line, "EndLine": line,
          "Code": {"Lines": [{"Number": line, "Content": lines[line - 1], "IsCause": True, "Truncated": False}] if code else []}}
    if resource:
        cm["Occurrences"] = [{"Resource": resource, "Filename": target, "Location": {"StartLine": 1, "EndLine": 99}}]
    return {"ID": check, "Title": f"{check} title", "Severity": severity, "Status": "FAIL", "CauseMetadata": cm}


def mreport(findings, extra_targets=()):
    by = {}
    for t, f in findings:
        by.setdefault(t, []).append(f)
    results = [{"Target": t, "Class": "config", "Type": "terraform", "MisconfSummary": {"Successes": 5, "Failures": len(fs)},
                "Misconfigurations": fs} for t, fs in by.items()]
    results += [{"Target": t, "Class": "config", "Type": "terraform", "MisconfSummary": {"Successes": 5, "Failures": 0},
                 "Misconfigurations": []} for t in extra_targets]
    return {"SchemaVersion": 2, "ArtifactName": ".", "ArtifactType": "repository", "Results": results}


def entry(**kw):
    base = {"id": "E1", "class": "intended_design", "check": "AWS-0104", "target": "infra/m/main.tf",
            "resource": "aws_vpc_security_group_egress_rule.task_https", "expected_instances": 2,
            "resource_conditions": [{"attribute": "ip_protocol", "equals": "tcp"}, {"attribute": "to_port", "equals": 443},
                                    {"attribute": "cidr_ipv6", "absent": True}],
            "tree_conditions": [], "expires": "2027-01-31", "review": "r", "authority": "a", "statement": "s"}
    base.update(kw)
    return base


def exceptions(*entries):
    return {"_note": "test", "schema_version": 1, "tree_scope": "infra", "exceptions": list(entries)}


class ExceptionCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = pathlib.Path(self.tmp.name) / "src"
        self.reports = pathlib.Path(self.tmp.name) / "reports"
        tree(self.root, {"infra/m/main.tf": EGRESS, "infra/alb/main.tf": ALB})
        self.policy = json.loads(POLICY.read_text())

    def tearDown(self):
        self.tmp.cleanup()

    def egress_findings(self, n=2):
        return [("infra/m/main.tf", mfinding("AWS-0104", "infra/m/main.tf", EGRESS_LINE, self.root, "CRITICAL",
                                              f'aws_vpc_security_group_egress_rule.task_https["k{i}"]')) for i in range(n)]

    def evaluate(self, findings, doc, today=TODAY):
        return gate.evaluate_misconfig(mreport(findings), self.policy, expect_types=["terraform"], exceptions=doc,
                                       source_root=self.root, today=today)

    def cli(self, findings, doc, *extra, name="iac"):
        rp = pathlib.Path(self.tmp.name) / "r.json"; rp.write_text(json.dumps(mreport(findings)))
        ep = pathlib.Path(self.tmp.name) / "e.json"; ep.write_text(json.dumps(doc))
        code = gate.main(["--policy", str(POLICY), "--reports-dir", str(self.reports), "misconfig", str(rp), "--name", name,
                          "--expect-type", "terraform", "--exceptions", str(ep), "--source-root", str(self.root), *extra])
        text = (self.reports / f"{name}.summary.txt").read_text() if (self.reports / f"{name}.summary.txt").exists() else ""
        return code, text

    # ------------------------------------------------------------------ HCL reader
    def test_reader_handles_comments_templates_heredocs_and_one_line_blocks(self):
        body = gate.parse_hcl('a = "x" # c\n/* b\n} */\nb = "${var.y}"\nc = <<EOT\n}{\nEOT\nd { e = 1 }\nf = "$${lit}"\n')
        attrs = {i.name: i for i in body if isinstance(i, gate.HclAttr)}
        self.assertEqual(gate.hcl_literal(attrs["a"].tokens), "x")
        self.assertIs(gate.hcl_literal(attrs["b"].tokens), gate.NON_LITERAL)
        self.assertIs(gate.hcl_literal(attrs["c"].tokens), gate.NON_LITERAL)
        self.assertEqual(gate.hcl_literal(attrs["f"].tokens), "${lit}")
        block = [i for i in body if isinstance(i, gate.HclBlock)][0]
        self.assertEqual((block.type, gate.hcl_literal(block.body[0].tokens)), ("d", 1))

    def test_reader_nested_template_strings_do_not_end_the_string(self):
        body = gate.parse_hcl('a = "${var.x == "}" ? "a" : "b"}"\nb = true\n')
        self.assertIs(gate.hcl_literal(body[0].tokens), gate.NON_LITERAL)
        self.assertIs(gate.hcl_literal(body[1].tokens), True)

    def test_reader_refuses_what_it_cannot_read(self):
        for bad in ('a = "unterminated\n', 'r "x" {\n', 'a = 1 @ 2\n', 'a = <<EOT\nno end\n'):
            with self.assertRaises(gate.HclError, msg=bad):
                gate.parse_hcl(bad)

    # ------------------------------------------------------------------ applied
    def test_matching_exception_excepts_and_lists_every_finding(self):
        lines, violations = self.evaluate(self.egress_findings(), exceptions(entry()))
        self.assertEqual(violations, [])
        self.assertEqual(sum(1 for l in lines if l.startswith("  EXCEPTED ")), 2)
        self.assertTrue(any("-> APPLIED" in l for l in lines))

    def test_cli_pass_with_exceptions(self):
        code, text = self.cli(self.egress_findings(), exceptions(entry()))
        self.assertEqual(code, gate.EXIT_OK); self.assertIn("exceptions:", text); self.assertTrue(text.rstrip().endswith(gate.RESULT_PASS))

    def test_without_exceptions_the_findings_violate(self):
        lines, violations = gate.evaluate_misconfig(mreport(self.egress_findings()), self.policy, expect_types=["terraform"])
        self.assertEqual(len(violations), 2)

    def test_an_unrelated_finding_is_never_excepted(self):
        other = ("infra/alb/main.tf", mfinding("AWS-0053", "infra/alb/main.tf", 3, self.root))
        _, violations = self.evaluate(self.egress_findings() + [other], exceptions(entry()))
        self.assertEqual(len(violations), 1); self.assertIn("AWS-0053", violations[0])

    # ------------------------------------------------------------------ each failure class
    def test_count_mismatch_fails_and_unexcepts_every_match(self):
        _, violations = self.evaluate(self.egress_findings(3), exceptions(entry()))
        self.assertTrue(any(v.startswith("EXCEPTION_COUNT_MISMATCH") for v in violations))
        self.assertEqual(sum("NOT applied" in v for v in violations), 3)

    def test_expiry_is_exclusive_of_the_date(self):
        doc = exceptions(entry(expires="2026-10-19"))
        self.assertEqual(self.evaluate(self.egress_findings(), doc, today=dt.date(2026, 10, 18))[1], [])
        _, violations = self.evaluate(self.egress_findings(), doc, today=dt.date(2026, 10, 19))
        self.assertTrue(any(v.startswith("EXCEPTION_EXPIRED") for v in violations))

    def test_stale_entry_fails(self):
        doc = exceptions(entry(), entry(id="E2", resource="aws_vpc_security_group_egress_rule.gone", expected_instances=1, resource_conditions=[]))
        _, violations = self.evaluate(self.egress_findings(), doc)
        self.assertEqual([v for v in violations if v.startswith("EXCEPTION_STALE")], ["EXCEPTION_STALE E2: matches no gating finding — remove it or correct it"])

    def test_condition_failure_absent_and_strict_types(self):
        tree(self.root, {"infra/m/main.tf": EGRESS.replace('to_port           = 443', 'to_port           = "443"')})
        _, violations = self.evaluate(self.egress_findings(), exceptions(entry()))
        self.assertTrue(any(v.startswith("EXCEPTION_CONDITION_FAILED") and "to_port" in v for v in violations))
        tree(self.root, {"infra/m/main.tf": EGRESS.replace('  tags = {', '  cidr_ipv6 = "::/0"\n  tags = {')})
        _, violations = self.evaluate(self.egress_findings(), exceptions(entry()))
        self.assertTrue(any("must be absent" in v for v in violations))

    def test_expressions_fail_closed(self):
        for old, new in (('ip_protocol       = "tcp"', 'ip_protocol       = var.proto'),
                         ('ip_protocol       = "tcp"', 'ip_protocol       = "${var.proto}"'),
                         ('ip_protocol       = "tcp"', 'ip_protocol       = <<EOT\ntcp\nEOT')):
            tree(self.root, {"infra/m/main.tf": EGRESS.replace(old, new)})
            findings = self.egress_findings()
            _, violations = self.evaluate(findings, exceptions(entry()))
            self.assertTrue(any(v.startswith("EXCEPTION_CONDITION_NONLITERAL") for v in violations), new)

    def test_dynamic_nested_block_fails_closed(self):
        tree(self.root, {"infra/alb/main.tf": ALB.replace("  rule {\n", '  dynamic "rule" {\n    for_each = [1]\n    content {}\n  }\n  rule {\n')})
        e = entry(id="C1", check="AWS-0132", target="infra/alb/main.tf", expected_instances=1,
                  resource="aws_s3_bucket_server_side_encryption_configuration.logs",
                  resource_conditions=[{"attribute": "rule.apply_server_side_encryption_by_default.sse_algorithm", "equals": "AES256"}])
        f = ("infra/alb/main.tf", mfinding("AWS-0132", "infra/alb/main.tf", 18, self.root))
        _, violations = self.evaluate([f], exceptions(e))
        self.assertTrue(any(v.startswith("EXCEPTION_CONDITION_NONLITERAL") and "dynamic" in v for v in violations))

    def test_nested_condition_holds(self):
        e = entry(id="C1", check="AWS-0132", target="infra/alb/main.tf", expected_instances=1,
                  resource="aws_s3_bucket_server_side_encryption_configuration.logs",
                  resource_conditions=[{"attribute": "rule.apply_server_side_encryption_by_default.sse_algorithm", "equals": "AES256"}])
        f = ("infra/alb/main.tf", mfinding("AWS-0132", "infra/alb/main.tf", 18, self.root))
        self.assertEqual(self.evaluate([f], exceptions(e))[1], [])

    def test_override_and_tofu_files_beside_the_block_fail_closed(self):
        for rel in ("infra/m/egress_override.tf", "infra/m/override.tf.json", "infra/m/main.tofu"):
            with tempfile.TemporaryDirectory() as d:
                self.root = pathlib.Path(d)
                tree(self.root, {"infra/m/main.tf": EGRESS, rel: "{}\n" if rel.endswith(".json") else 'resource "aws_vpc_security_group_egress_rule" "task_https" {\n  to_port = 65535\n}\n'})
                _, violations = self.evaluate(self.egress_findings(), exceptions(entry()))
                self.assertTrue(any(v.startswith("EXCEPTION_MODULE_OVERRIDE") for v in violations), rel)

    def test_a_second_declaration_in_the_module_fails_closed(self):
        tree(self.root, {"infra/m/other.tf": 'resource "aws_vpc_security_group_egress_rule" "task_https" {\n  to_port = 1\n}\n'})
        _, violations = self.evaluate(self.egress_findings(), exceptions(entry()))
        self.assertTrue(any("declared 2 times" in v for v in violations))

    # ------------------------------------------------------------------ tree-scoped conditions
    def a2(self):
        return entry(id="A2", check="AWS-0053", target="infra/alb/main.tf", resource="aws_lb.this", expected_instances=1,
                     resource_conditions=[{"attribute": "load_balancer_type", "equals": "application"}],
                     tree_conditions=[{"resource_types": ["aws_lb_listener", "aws_alb_listener"], "attribute": "protocol",
                                       "equals": "HTTPS", "min_blocks": 1}])

    def alb_finding(self):
        return ("infra/alb/main.tf", mfinding("AWS-0053", "infra/alb/main.tf", 3, self.root, resource="aws_lb.this"))

    def test_tree_condition_holds(self):
        self.assertEqual(self.evaluate([self.alb_finding()], exceptions(self.a2()))[1], [])

    def test_tree_condition_sees_listeners_anywhere_in_scope(self):
        cases = {
            "infra/root_listener.tf": 'resource "aws_lb_listener" "http" {\n  protocol = "HTTP"\n}\n',
            "infra/other/alias.tf": 'resource "aws_alb_listener" "http" {\n  protocol = "HTTP"\n}\n',
            "infra/other/listener.tf.json": json.dumps({"resource": {"aws_lb_listener": {"http": {"protocol": "HTTP"}}}}),
            "infra/other/missing.tf": 'resource "aws_lb_listener" "default_protocol" {\n  port = 80\n}\n',
            "infra/.terraform/mod/listener.tf": 'resource "aws_lb_listener" "hidden" {\n  protocol = "HTTP"\n}\n',
        }
        for rel, text in cases.items():
            with tempfile.TemporaryDirectory() as d:
                self.root = pathlib.Path(d)
                tree(self.root, {"infra/alb/main.tf": ALB, rel: text})
                _, violations = self.evaluate([self.alb_finding()], exceptions(self.a2()))
                self.assertTrue(any(v.startswith("EXCEPTION_CONDITION_FAILED") for v in violations), rel)

    def test_tree_condition_json_template_fails_closed(self):
        tree(self.root, {"infra/other/l.tf.json": json.dumps({"resource": {"aws_lb_listener": {"x": {"protocol": "${var.p}"}}}})})
        _, violations = self.evaluate([self.alb_finding()], exceptions(self.a2()))
        self.assertTrue(any(v.startswith("EXCEPTION_CONDITION_NONLITERAL") for v in violations))

    def test_tree_condition_is_not_satisfied_vacuously(self):
        tree(self.root, {"infra/alb/main.tf": ALB.replace('resource "aws_lb_listener" "https"', 'resource "aws_lb_target_group" "https"')})
        _, violations = self.evaluate([self.alb_finding()], exceptions(self.a2()))
        self.assertTrue(any("at least 1 required" in v for v in violations))

    def test_tree_integrity_refuses_opaque_constructs(self):
        cases = {
            "infra/remote.tf": 'module "r" {\n  source = "git::https://example.invalid/x.git"\n}\n',
            "infra/registry.tf": 'module "r" {\n  source = "acme/thing/aws"\n}\n',
            "infra/prov.tf": 'resource "terraform_data" "p" {\n  provisioner "local-exec" {\n    command = "true"\n  }\n}\n',
            "infra/cfn.tf": 'resource "aws_cloudformation_stack" "s" {\n  name = "s"\n}\n',
            "infra/cc.tf": 'resource "awscc_elasticloadbalancingv2_listener" "l" {\n  protocol = "HTTPS"\n}\n',
            "infra/ext.tf": 'data "external" "x" {\n  program = ["true"]\n}\n',
            "infra/x.tofu": 'locals {\n  a = 1\n}\n',
        }
        for rel, text in cases.items():
            with tempfile.TemporaryDirectory() as d:
                self.root = pathlib.Path(d)
                tree(self.root, {"infra/alb/main.tf": ALB, rel: text})
                _, violations = self.evaluate([self.alb_finding()], exceptions(self.a2()))
                self.assertTrue(any(v.startswith("EXCEPTION_TREE_OPAQUE") for v in violations), rel)

    def test_a_local_module_source_that_escapes_the_scope_fails_closed(self):
        # Review FREEZE-1, adversarial BLOCKER-1: "../" (or a symlink) loads blocks this reading never walks.
        tree(self.root, {"outside/listener.tf": 'resource "aws_lb_listener" "http" {\n  protocol = "HTTP"\n}\n',
                         "infra/root.tf": 'module "escape" {\n  source = "../outside"\n}\n'})
        _, violations = self.evaluate([self.alb_finding()], exceptions(self.a2()))
        self.assertTrue(any(v.startswith("EXCEPTION_TREE_OPAQUE") and "resolves to" in v for v in violations))
        with tempfile.TemporaryDirectory() as d:
            self.root = pathlib.Path(d)
            tree(self.root, {"infra/alb/main.tf": ALB, "elsewhere/listener.tf": 'resource "aws_lb_listener" "http" {\n  protocol = "HTTP"\n}\n',
                             "infra/root.tf": 'module "linked" {\n  source = "./linked"\n}\n'})
            (self.root / "infra/linked").symlink_to(self.root / "elsewhere", target_is_directory=True)
            _, violations = self.evaluate([self.alb_finding()], exceptions(self.a2()))
            self.assertTrue(any(v.startswith("EXCEPTION_TREE_OPAQUE") and "resolves to" in v for v in violations))
        with tempfile.TemporaryDirectory() as d:
            self.root = pathlib.Path(d)
            tree(self.root, {"infra/alb/main.tf": ALB, "infra/root.tf": 'module "missing" {\n  source = "./does-not-exist"\n}\n'})
            _, violations = self.evaluate([self.alb_finding()], exceptions(self.a2()))
            self.assertTrue(any(v.startswith("EXCEPTION_TREE_OPAQUE") for v in violations))

    def test_a_symlink_followed_by_dotdot_cannot_escape_the_scope(self):
        # Review FREEZE-2, permissions lane R2-1: resolve() follows "link" first and lands inside; OpenTofu cleans
        # "./link/../../modules" lexically and loads infra/modules, outside the scope. Both syntaxes; symlinks refused outright.
        for rel, text in (("infra/root.tf", 'module "x" {\n  source = "./link/../../modules"\n}\n'),
                          ("infra/root.tf.json", json.dumps({"module": {"x": {"source": "./link/../../modules"}}}))):
            with tempfile.TemporaryDirectory() as d:
                self.root = pathlib.Path(d)
                # The reviewer's geometry: link -> sub/alb (two levels), so PHYSICALLY ./link/../../modules is infra/modules
                # (inside, and it exists), while LEXICALLY it is modules/ (outside) — what OpenTofu loads.
                tree(self.root, {"infra/alb/main.tf": ALB, "infra/sub/alb/x.tf": "locals {\n  a = 1\n}\n",
                                 "infra/modules/placeholder.tf": "locals {\n  a = 1\n}\n",
                                 "modules/listener.tf": 'resource "aws_lb_listener" "http" {\n  protocol = "HTTP"\n}\n', rel: text})
                (self.root / "infra/link").symlink_to("sub/alb", target_is_directory=True)
                self.assertTrue((self.root / "infra/link/../../modules").resolve() == (self.root / "infra/modules").resolve())
                _, violations = self.evaluate([self.alb_finding()], exceptions(self.a2()))
                opaque = [v for v in violations if v.startswith("EXCEPTION_TREE_OPAQUE")]
                self.assertTrue(any("a symlink in infra" in v for v in opaque), rel)
                self.assertTrue(any("module.x" in v and "resolves to modules" in v for v in opaque), rel)

    def test_any_symlink_in_the_scope_fails_closed_even_one_pointing_inside(self):
        (self.root / "infra/alias-to-alb").symlink_to("alb", target_is_directory=True)
        _, violations = self.evaluate([self.alb_finding()], exceptions(self.a2()))
        self.assertTrue(any(v.startswith("EXCEPTION_TREE_OPAQUE") and "a symlink in infra" in v for v in violations))

    def test_a_symlinked_file_beside_the_matched_block_fails_closed(self):
        tree(self.root, {"shared/locals.tf": "locals {\n  shared = 1\n}\n"})
        (self.root / "infra/m/locals.tf").symlink_to("../../shared/locals.tf")
        _, violations = self.evaluate(self.egress_findings(), exceptions(entry()))
        self.assertTrue(any(v.startswith("EXCEPTION_MODULE_OVERRIDE") for v in violations))

    def test_a_local_module_source_is_accepted(self):
        tree(self.root, {"infra/root.tf": 'module "alb" {\n  source = "./alb"\n}\n'})
        self.assertEqual(self.evaluate([self.alb_finding()], exceptions(self.a2()))[1], [])

    # ------------------------------------------------------------------ report <-> source binding
    def test_a_report_that_does_not_describe_the_source_is_incomplete(self):
        findings = self.egress_findings()
        tree(self.root, {"infra/m/main.tf": EGRESS.replace('cidr_ipv4         = "0.0.0.0/0"', 'cidr_ipv4         = "10.0.0.0/8"')})
        with self.assertRaises(gate.IncompleteError):
            self.evaluate(findings, exceptions(entry()))

    def test_a_missing_target_file_is_incomplete(self):
        findings = self.egress_findings()
        (self.root / "infra/m/main.tf").unlink()
        with self.assertRaises(gate.IncompleteError):
            self.evaluate(findings, exceptions(entry()))

    def test_a_disagreeing_occurrence_is_a_violation(self):
        f = mfinding("AWS-0104", "infra/m/main.tf", EGRESS_LINE, self.root, "CRITICAL", 'aws_vpc_security_group_egress_rule.other["k"]')
        _, violations = self.evaluate([("infra/m/main.tf", f)] * 2, exceptions(entry()))
        self.assertTrue(any(v.startswith("EXCEPTION_RESOURCE_MISMATCH") for v in violations))

    def test_a_line_outside_any_resource_is_unresolved(self):
        f = mfinding("AWS-0104", "infra/m/main.tf", 1, self.root, "CRITICAL")
        _, violations = self.evaluate([("infra/m/main.tf", f)], exceptions(entry(expected_instances=1)))
        self.assertTrue(any(v.startswith("EXCEPTION_UNRESOLVED") for v in violations))

    # ------------------------------------------------------------------ schema
    def test_schema_refusals(self):
        bad = [
            dict(exceptions(entry()), extra=1),
            dict(exceptions(entry()), schema_version=2),
            exceptions(dict(entry(), expire="2027-01-01")),
            exceptions({k: v for k, v in entry().items() if k != "review"}),
            exceptions(entry(expires=None)),
            exceptions(entry(expires=None, no_expiry_reason="only a verified limitation may omit an expiry")),
            exceptions(entry(**{"class": "verified_limitation", "expires": "2027-01-01", "no_expiry_reason": "r"})),
            exceptions(entry(), entry()),
            exceptions(entry(), entry(id="E2")),
            exceptions(entry(check="0104")),
            exceptions(entry(target="infra/m/main.tf.json")),
            exceptions(entry(target="elsewhere/main.tf")),
            exceptions(entry(target="infra/../m/main.tf")),
            exceptions(entry(expected_instances=True)),
            exceptions(entry(expires="2027-02-30")),
            exceptions(entry(resource_conditions=[{"attribute": "a", "equals": 1, "absent": True}])),
            exceptions(entry(resource_conditions=[{"attribute": "a", "absent": False}])),
            exceptions(entry(tree_conditions=[{"resource_types": ["aws_lb_listener"], "attribute": "protocol", "equals": "HTTPS"}])),
            {"schema_version": 1, "tree_scope": "infra", "exceptions": []},
        ]
        for n, doc in enumerate(bad):
            code, _ = self.cli(self.egress_findings(), doc, name=f"bad{n}")
            self.assertEqual(code, gate.EXIT_REPORT, f"case {n}: {json.dumps(doc)[:200]}")

    def test_a_limitation_may_omit_expiry_with_a_reason(self):
        e = entry(**{"class": "verified_limitation", "expires": None, "no_expiry_reason": "AWS supports SSE-S3 only"})
        code, text = self.cli(self.egress_findings(), exceptions(e))
        self.assertEqual(code, gate.EXIT_OK); self.assertIn("no expiry (AWS supports SSE-S3 only)", text)

    # ------------------------------------------------------------------ control-only CLI options
    def test_control_options_are_refused_on_a_real_gate(self):
        doc = exceptions(entry())
        self.assertEqual(self.cli(self.egress_findings(), doc, "--report-prefix", "x/")[0], gate.EXIT_USAGE)
        self.assertEqual(self.cli(self.egress_findings(), doc, "--expect-violation", "X")[0], gate.EXIT_USAGE)
        self.assertEqual(self.cli(self.egress_findings(), doc, "--control-baseline")[0], gate.EXIT_USAGE)
        self.assertEqual(self.cli(self.egress_findings(), doc, "--control-baseline", "--report-prefix", "x/", "--expect-fail")[0], gate.EXIT_USAGE)

    def test_expect_violation_requires_the_intended_violation(self):
        doc = exceptions(entry(expected_instances=3))
        code, text = self.cli(self.egress_findings(), doc, "--expect-fail", "--expect-violation", "EXCEPTION_EXPIRED", name="c1")
        self.assertEqual(code, gate.EXIT_CONTROL); self.assertIn("no violation line carries", text)
        code, text = self.cli(self.egress_findings(), doc, "--expect-fail", "--expect-violation", "EXCEPTION_COUNT_MISMATCH", name="c2")
        self.assertEqual(code, gate.EXIT_OK); self.assertTrue(text.rstrip().endswith(gate.RESULT_CONTROL_OK))

    def test_report_prefix_selects_one_copy_and_strips_it(self):
        findings = [("case/" + t, copy.deepcopy(f)) for t, f in self.egress_findings()]
        for _, f in findings:
            for o in f["CauseMetadata"]["Occurrences"]:
                o["Filename"] = "case/" + o["Filename"]
        rp = pathlib.Path(self.tmp.name) / "r.json"
        rp.write_text(json.dumps(mreport(findings + [("other/infra/m/main.tf", self.egress_findings()[0][1])])))
        ep = pathlib.Path(self.tmp.name) / "e.json"; ep.write_text(json.dumps(exceptions(entry())))
        code = gate.main(["--policy", str(POLICY), "--reports-dir", str(self.reports), "misconfig", str(rp), "--name", "b",
                          "--expect-type", "terraform", "--exceptions", str(ep), "--source-root", str(self.root),
                          "--report-prefix", "case/", "--control-baseline"])
        self.assertEqual(code, gate.EXIT_OK)


if __name__ == "__main__":
    unittest.main()
