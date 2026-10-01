"""Staging window (docs/operations/staging-window.md) — root composition contract.

The module tests (alb/data_cache/network window.tftest.hcl) prove each module's open/closed
behaviour with a mocked provider. This module proves the ROOT wires the single window input
to those modules and refuses the workload stage outside a window — read from the HCL text
the way the repository's other structural guards do, never from a live plan.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra" / "aws"
MAIN = (INFRA / "main.tf").read_text(encoding="utf-8")
VARIABLES = (INFRA / "variables.tf").read_text(encoding="utf-8")


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


def test_the_window_input_is_required_with_no_default():
    block = _block(VARIABLES, 'variable "staging_window_active"')
    assert re.search(r"^\s*type\s*=\s*bool\s*$", block, re.M)
    assert not re.search(r"^\s*default\s*=", block, re.M), "a window that defaults open is how spend happens — no default attribute"


def test_the_workload_stage_is_refused_outside_a_window():
    block = _block(VARIABLES, 'variable "deploy_workload"')
    assert "!var.deploy_workload || var.staging_window_active" in block
    assert "error_message" in block


def test_the_root_wires_the_window_to_nat_alb_and_cache():
    network = _block(MAIN, 'module "network"')
    assert "enable_nat_gateway = var.enable_nat_gateway && var.staging_window_active" in network, (
        "the NAT gateway must be ANDed with the window (the window can only turn it off)")
    alb = _block(MAIN, 'module "alb"')
    assert re.search(r"enabled\s*=\s*var\.staging_window_active", alb), "alb.enabled must follow the window"
    cache = _block(MAIN, 'module "data_cache"')
    assert re.search(r"enabled\s*=\s*var\.staging_window_active", cache), "data_cache.enabled must follow the window"


def test_the_window_does_not_reach_data_or_identity_modules():
    for name in ("data_sql", "secrets", "iam", "storage", "registry", "edge", "cost", "observability", "revision_reader"):
        block = _block(MAIN, f'module "{name}"')
        assert "staging_window_active" not in block, f"module {name} must not be gated by the window (data/identity persist)"


def test_module_gates_exist_and_default_open():
    for module in ("alb", "data_cache"):
        text = (INFRA / "modules" / module / "variables.tf").read_text(encoding="utf-8")
        block = _block(text, 'variable "enabled"')
        assert re.search(r"^\s*type\s*=\s*bool\s*$", block, re.M) and "default     = true" in block
    alb_main = (INFRA / "modules" / "alb" / "main.tf").read_text(encoding="utf-8")
    for res in ('resource "aws_lb" "this"', 'resource "aws_lb_target_group" "api"', 'resource "aws_lb_listener" "https"'):
        assert "count = var.enabled ? 1 : 0" in _block(alb_main, res), res
    for res in ('resource "aws_security_group" "alb"', 'resource "aws_s3_bucket" "alb_logs"'):
        assert "count" not in _block(alb_main, res), f"{res} must persist across windows"
    cache_main = (INFRA / "modules" / "data_cache" / "main.tf").read_text(encoding="utf-8")
    assert "count = var.enabled ? 1 : 0" in _block(cache_main, 'resource "aws_elasticache_replication_group" "this"')
    for res in ('resource "aws_elasticache_subnet_group" "this"', 'resource "aws_security_group" "redis"', 'resource "aws_elasticache_parameter_group" "this"'):
        assert "count" not in _block(cache_main, res), f"{res} must persist across windows"


def test_examples_and_fixtures_state_the_window():
    example = (INFRA / "terraform.tfvars.example").read_text(encoding="utf-8")
    assert re.search(r"^staging_window_active = false$", example, re.M), "the tracked example must state the CLOSED state explicitly"
    fixture = (REPO_ROOT / "tests" / "fixtures" / "root-wiring-synthetic.tfvars.example").read_text(encoding="utf-8")
    assert re.search(r"^staging_window_active = true$", fixture, re.M), "the synthetic root fixture must supply the required input"


def test_the_runbook_exists_and_names_the_rds_restart_limit():
    doc = (REPO_ROOT / "docs" / "operations" / "staging-window.md").read_text(encoding="utf-8")
    for needle in ("staging_window_active", "StopDBInstance", "seven days", "ReleaseAddress", "2.46"):
        assert needle in doc, f"runbook must mention {needle!r}"
