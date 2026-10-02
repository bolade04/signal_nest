"""P6-WINDOW-DNS-BUDGET — windowed API alias (P6-INF-3) and the operator budget bound, root contract.

The mocked suites prove runtime behaviour: `infra/aws/modules/alb/api_alias.tftest.hcl` (the record
exists only while enabled, is one A alias bound to the planned ALB, rejects malformed inputs) and
`infra/aws/input_boundary.tftest.hcl` (the ROOT rejects monthly_budget_limit > 20 and api_fqdn ==
web_fqdn, and wires the window to the alias while the web alias persists). This module pins the
STRUCTURE those suites rely on — read from the HCL/text the way the repository's other structural
guards do, never from a live plan — so a silent re-wiring is caught by pytest as well as by CI.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra" / "aws"
MAIN = (INFRA / "main.tf").read_text(encoding="utf-8")
VARIABLES = (INFRA / "variables.tf").read_text(encoding="utf-8")
OUTPUTS = (INFRA / "outputs.tf").read_text(encoding="utf-8")
ALB_MAIN = (INFRA / "modules" / "alb" / "main.tf").read_text(encoding="utf-8")
ALB_VARIABLES = (INFRA / "modules" / "alb" / "variables.tf").read_text(encoding="utf-8")
EDGE_MAIN = (INFRA / "modules" / "edge" / "main.tf").read_text(encoding="utf-8")
COST_VARIABLES = (INFRA / "modules" / "cost" / "variables.tf").read_text(encoding="utf-8")
EXAMPLE = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
FIXTURE = (REPO_ROOT / "tests" / "fixtures" / "root-wiring-synthetic.tfvars.example").read_text(encoding="utf-8")
ROOT_SUITE = (INFRA / "input_boundary.tftest.hcl").read_text(encoding="utf-8")


def _block(text: str, header: str) -> str:
    start = text.index(header)
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[start:j + 1]
    raise AssertionError(f"unterminated block {header}")


# --------------------------------------------------------------------------- API alias: ownership and gate
def test_the_alias_record_is_owned_by_the_alb_module_and_shares_the_window_gate():
    record = _block(ALB_MAIN, 'resource "aws_route53_record" "api"')
    assert "count = var.enabled ? 1 : 0" in record, "the alias must share the ALB's window gate"
    assert re.search(r'type\s*=\s*"A"', record), "one A alias (the ALB is IPv4-only)"
    assert "aws_lb.this[0].dns_name" in record and "aws_lb.this[0].zone_id" in record, \
        "the alias target must be the planned ALB's attributes, so a re-created ALB re-points the record"
    assert "evaluate_target_health = false" in record
    assert re.search(r"zone_id\s*=\s*var\.hosted_zone_id", record) and re.search(r"name\s*=\s*lower\(var\.api_fqdn\)", record)
    # exactly one Route 53 resource in the module, and no zone/certificate creation anywhere in alb
    assert ALB_MAIN.count('resource "aws_route53_record"') == 1
    assert "aws_route53_zone" not in ALB_MAIN and "aws_acm_certificate" not in ALB_MAIN
    assert 'type    = "AAAA"' not in ALB_MAIN and '"AAAA"' not in ALB_MAIN, "no AAAA alias for an IPv4-only ALB"
    assert 'ip_address_type    = "ipv4"' in _block(ALB_MAIN, 'resource "aws_lb" "this"')


def test_the_alb_module_validates_api_fqdn_and_hosted_zone_id_statically():
    fqdn = _block(ALB_VARIABLES, 'variable "api_fqdn"')
    assert fqdn.count("validation {") == 3 and not re.search(r"^\s*default\s*=", fqdn, re.M)
    assert '[/?#:@[:space:]]' in fqdn, "scheme/port/path/query/fragment/@/whitespace are rejected"
    assert "lower(var.api_fqdn)" in fqdn and "length(var.api_fqdn) <= 253" in fqdn
    zone = _block(ALB_VARIABLES, 'variable "hosted_zone_id"')
    assert '"^Z[A-Z0-9]{1,32}$"' in zone and not re.search(r"^\s*default\s*=", zone, re.M)
    declared = re.findall(r'^data "([a-z0-9_]+)"', ALB_MAIN, re.M)
    assert sorted(declared) == ["aws_caller_identity", "aws_elb_service_account"], \
        "no new data source: the zone is consumed by value, never looked up (aws_route53_zone must not appear)"
    assert re.search(r"name\s*=\s*lower\(var\.api_fqdn\)", _block(ALB_MAIN, 'resource "aws_route53_record" "api"')), \
        "the record name is lower()ed to match Route 53's case folding and the root's collision rule"


def test_the_root_requires_api_fqdn_and_rejects_collision_with_web_fqdn():
    block = _block(VARIABLES, 'variable "api_fqdn"')
    assert not re.search(r"^\s*default\s*=", block, re.M), "no committed hostname default"
    assert block.count("validation {") == 4
    assert "lower(var.api_fqdn) != lower(var.web_fqdn)" in block, \
        "the cross-variable rule is the ONLY place both record names are known (same hosted zone)"
    alb = _block(MAIN, 'module "alb"')
    assert re.search(r"hosted_zone_id\s*=\s*var\.hosted_zone_id", alb) and re.search(r"api_fqdn\s*=\s*var\.api_fqdn", alb)
    assert re.search(r"enabled\s*=\s*var\.staging_window_active", alb)
    assert "module.edge" not in alb, "alb must consume root values only — no edge -> alb graph edge (§26.12 unchanged)"


def test_the_web_dns_records_are_unchanged_and_not_windowed():
    for name in ("web_a", "web_aaaa"):
        rec = _block(EDGE_MAIN, f'resource "aws_route53_record" "{name}"')
        assert "count" not in rec and "for_each" not in rec, f"{name} must persist across windows"
        assert "aws_cloudfront_distribution.spa.domain_name" in rec and "aws_cloudfront_distribution.spa.hosted_zone_id" in rec
        assert re.search(r"zone_id\s*=\s*var\.hosted_zone_id", rec) and re.search(r"name\s*=\s*var\.web_fqdn", rec)
    assert EDGE_MAIN.count('resource "aws_route53_record"') == 2, "edge owns exactly the two web aliases (§23: no API record)"
    edge = _block(MAIN, 'module "edge"')
    assert "staging_window_active" not in edge and "api_fqdn" not in edge


def test_the_root_exports_the_windowed_alias_name_and_a_configuration_echo_url():
    assert "module.alb.api_alias_record_name" in _block(OUTPUTS, 'output "api_alias_record_name"')
    assert 'value       = "https://${lower(var.api_fqdn)}"' in _block(OUTPUTS, 'output "api_url"')


# --------------------------------------------------------------------------- budget: operator limit at the root
def test_the_root_bounds_the_budget_at_the_operator_limit_while_the_module_keeps_its_interface():
    root = _block(VARIABLES, 'variable "monthly_budget_limit"')
    assert "var.monthly_budget_limit > 0 && var.monthly_budget_limit <= 20" in root
    assert not re.search(r"^\s*default\s*=", root, re.M), "the limit stays an explicit input"
    assert "20 TOTAL" in root and "200" in root, "the description must distinguish the operator limit from the historical ceiling"
    module = _block(COST_VARIABLES, 'variable "monthly_budget_limit"')
    assert "var.monthly_budget_limit > 0 && var.monthly_budget_limit <= 200" in module, \
        "the generic cost module's interface is preserved (the stricter bound lives at the staging root)"
    cost = _block(MAIN, 'module "cost"')
    assert re.search(r"monthly_budget_limit\s*=\s*var\.monthly_budget_limit", cost)
    assert "threshold_percentages" not in cost, "notification thresholds keep the module default (50/75/90/100)"
    assert re.search(r"notification_target\s*=\s*var\.budget_notification_email", cost), "recipient configuration preserved"


def test_examples_and_fixtures_carry_the_new_inputs_at_valid_values():
    assert re.search(r"^monthly_budget_limit = 20$", EXAMPLE, re.M), "the tracked example states the operator limit explicitly"
    assert re.search(r"^# api_fqdn\s+= \"<API_FQDN>\"", EXAMPLE, re.M), "the example names api_fqdn with a placeholder token"
    assert re.search(r"^monthly_budget_limit = 20\b", FIXTURE, re.M)
    assert re.search(r'^api_fqdn\s+= "api\.synthetic\.example\.com"', FIXTURE, re.M)
    web = re.search(r'^web_fqdn\s+= "([^"]+)"', FIXTURE, re.M).group(1)
    api = re.search(r'^api_fqdn\s+= "([^"]+)"', FIXTURE, re.M).group(1)
    assert api.lower() != web.lower(), "the fixture must satisfy the collision rule"
    # the root suite's variables block mirrors the fixture's shared values
    for key, value in (("web_fqdn", web), ("api_fqdn", api), ("monthly_budget_limit", "20")):
        assert re.search(r"^\s*%s\s*=\s*\"?%s\"?\s*$" % (re.escape(key), re.escape(value)), ROOT_SUITE, re.M), key


def test_no_budget_action_or_enforcement_claim_was_added():
    cost_main = (INFRA / "modules" / "cost" / "main.tf").read_text(encoding="utf-8")
    assert "aws_budgets_budget_action" not in cost_main and "aws_budgets_budget_action" not in MAIN
    assert cost_main.count('resource "') == 1, "the cost module still declares exactly one resource"


# --------------------------------------------------------------------------- docs
def test_the_runbook_and_plan_record_the_alias_lifecycle_and_the_budget_distinction():
    runbook = (REPO_ROOT / "docs" / "operations" / "staging-window.md").read_text(encoding="utf-8")
    for needle in ("aws_route53_record", "route53:ChangeResourceRecordSets", "route53:GetChange", "budgets:ModifyBudget", "TTL",
                   "create_before_destroy", "api_ses_send", "test_the_web_dns_records_are_unchanged_and_not_windowed"):
        assert needle in runbook, f"staging-window.md must mention {needle!r}"
    plan = (REPO_ROOT / "docs" / "operations" / "aws-staging-iac-plan.md").read_text(encoding="utf-8")
    assert "windowed" in plan and "api_fqdn" in plan, "§24.7 must record the lifted alias deferral as a windowed record"
    contract = (REPO_ROOT / "docs" / "operations" / "aws-staging-runtime-contract.md").read_text(encoding="utf-8")
    assert "<= 20" in contract or "at most 20" in contract, "§M must state the root's stricter bound"
