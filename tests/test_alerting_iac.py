"""Alerting and retention IaC (P6-INF-4/-5/-6/-13) — root composition contract.

The mocked suite infra/aws/alerting.tftest.hcl proves the observability and alb modules'
behaviour and the root's window wiring through its outputs. What a root run cannot address
— the workload environment that makes the application export metrics, and the absence of a
default for the operator values — is pinned here from the HCL text, the way the
repository's other structural guards read it; nothing here runs OpenTofu or calls AWS.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
INFRA = REPO_ROOT / "infra" / "aws"
MAIN = (INFRA / "main.tf").read_text(encoding="utf-8")
VARIABLES = (INFRA / "variables.tf").read_text(encoding="utf-8")
SUITE = (INFRA / "alerting.tftest.hcl").read_text(encoding="utf-8")


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


def test_the_operator_values_are_required_with_no_default():
    for name in ("alarm_notification_email", "audit_log_retention_days", "alb_log_retention_days"):
        block = _block(VARIABLES, f'variable "{name}"')
        assert not re.search(r"^\s*default\s*=", block, re.M), f"{name}: an operator value must never be assumed"
        assert "validation" in block, name


def test_the_workloads_export_emf_metrics_under_the_namespace_the_alarms_read():
    common = _block(MAIN, "workload_env_common = {")
    assert 'METRICS_ENABLED   = "true"' in common and 'METRICS_EXPORTER  = "emf"' in common
    assert "METRICS_NAMESPACE = local.app_metrics_namespace" in common
    assert 'app_metrics_namespace = "${local.name_prefix}/app"' in MAIN
    assert "SERVICE_NAME        = local.app_service_names.api" in _block(MAIN, "workload_env_api = merge(")
    assert "SERVICE_NAME = local.app_service_names.worker" in _block(MAIN, "workload_env_worker = merge(")
    assert "worker_environment      = local.workload_env_worker" in _block(MAIN, 'module "ecs"')
    assert "METRICS" not in _block(MAIN, "workload_env_migration = {"), "the one-shot migration actor exports nothing"
    obs = _block(MAIN, 'module "observability"')
    assert "namespace   = local.app_metrics_namespace" in obs
    assert "service     = local.app_service_names.api" in obs


def test_only_the_alb_alarms_follow_the_window_through_the_alb_module():
    obs = _block(MAIN, 'module "observability"')
    assert "staging_window_active" not in obs, "the window input must not reach the observability module"
    assert "alb_alarms_enabled          = module.alb.alb_enabled" in obs
    assert "alarm_notification_email = var.alarm_notification_email" in obs
    alb = _block(MAIN, 'module "alb"')
    assert "log_retention_days = var.alb_log_retention_days" in alb


def test_the_suite_is_fully_mocked_and_states_its_runs():
    assert 'mock_provider "aws"' in SUITE and 'alias = "revision_reader"' in SUITE
    assert len(re.findall(r'^run "', SUITE, re.M)) == 13
    for needle in ("expect_failures = [var.alarm_notification_email]",
                   "expect_failures = [var.audit_log_retention_days]",
                   "expect_failures = [aws_cloudwatch_metric_alarm.alb]"):
        assert needle in SUITE, needle
