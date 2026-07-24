# main.tf — staging cost guardrails (INFRA-4 cost module)
#
# Owns the structural cost controls locked by aws-staging-iac-plan.md §15 and
# ADR-0001 §M: ONE monthly AWS Budget (COST type, USD) with an ACTUAL-spend
# notification at each fixed threshold percentage (50/75/90/100 by default) so
# overspend alerts exist structurally before any spend begins. The module is
# INDEPENDENT (§26.12: `cost` has no producer edge and no consumer) — it takes
# no sibling-module input, uses no data source, and nothing downstream consumes
# its outputs.
#
# SAFETY MODEL: an AWS Budget is OBSERVATIONAL — it notifies, it never stops,
# caps, or remediates spending. This module deliberately creates NO budget
# action, NO automated remediation, NO SNS topic (the subscriber is a
# caller-supplied email address), and NO IAM resource. The $200/month hard
# ceiling (ADR-0001 §M) is enforced STATICALLY at the input boundary: the
# monthly_budget_limit validation rejects values above 200 so an over-ceiling
# budget cannot even validate; raising the ceiling is a separate authorization.
#
# No `provider "aws"` block; `versions.tf` declares only the provider SOURCE per
# the sibling convention. Tagging is the root provider's `default_tags`; the
# budget adds only the conventional `Name` tag. No account id, ARN, region,
# credential, or notification address is committed — offline-validated HCL only;
# no budget exists in AWS.

resource "aws_budgets_budget" "monthly" {
  name        = "${var.name_prefix}-monthly"
  budget_type = "COST"
  time_unit   = "MONTHLY"

  limit_amount = tostring(var.monthly_budget_limit)
  limit_unit   = "USD"

  # One ACTUAL-spend notification per fixed threshold (§15: 50/75/90/100).
  # Sorted for deterministic block ordering; each notifies the single
  # caller-supplied email subscriber. Observational only — no action.
  dynamic "notification" {
    for_each = toset([for p in var.threshold_percentages : tostring(p)])

    content {
      comparison_operator        = "GREATER_THAN"
      threshold                  = tonumber(notification.value)
      threshold_type             = "PERCENTAGE"
      notification_type          = "ACTUAL"
      subscriber_email_addresses = [var.notification_target]
    }
  }

  tags = {
    Name = "${var.name_prefix}-monthly"
  }
}
