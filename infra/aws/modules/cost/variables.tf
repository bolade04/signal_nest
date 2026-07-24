# variables.tf — typed inputs for the staging cost-guardrail plane
#
# Interface per the cost contract (aws-staging-iac-plan.md §15, ADR-0001 §M,
# this module's README): a single monthly staging budget with fixed notification
# thresholds. The budget limit is REQUIRED (no invented default — the documented
# design estimates are context, not configuration) and statically bounded by the
# documented **$200/month hard ceiling** so a composition mistake cannot encode
# an over-ceiling budget. The former stub's `tags` input is deliberately absent
# per the locked sibling convention (root provider `default_tags`; §26.15
# recorded this cleanup). No account id, ARN, region, credential, or real
# notification address is committed — the subscriber address is caller-supplied.

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\"). Builds the budget name \"<name_prefix>-monthly\". Contains no account id, credential, region, endpoint, or ARN."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric and hyphens, 2-63 chars, no leading/trailing hyphen."
  }
}

variable "monthly_budget_limit" {
  description = "Monthly cost-budget limit in USD. REQUIRED with no invented default. Statically bounded by the documented $200/month staging hard ceiling (ADR-0001 §M) — a larger budget requires a fresh, separately authorized ceiling decision, never a silent input change."
  type        = number

  validation {
    condition     = var.monthly_budget_limit > 0 && var.monthly_budget_limit <= 200
    error_message = "monthly_budget_limit must be greater than 0 and at most 200 USD (the ADR-0001 §M staging hard ceiling; raising the ceiling is a separate authorization, not an input change)."
  }
}

variable "threshold_percentages" {
  description = "Notification thresholds as percentages of the budget limit (ACTUAL spend). The staging contract fixes these at 50/75/90/100 (§15); the default encodes that fixed set. Values must be unique, ascending, and within 1-100."
  type        = list(number)
  default     = [50, 75, 90, 100]

  validation {
    condition     = length(var.threshold_percentages) > 0 && alltrue([for p in var.threshold_percentages : p >= 1 && p <= 100])
    error_message = "threshold_percentages must be a non-empty list of values between 1 and 100."
  }

  validation {
    condition     = length(var.threshold_percentages) == length(distinct(var.threshold_percentages))
    error_message = "threshold_percentages must not contain duplicates."
  }
}

variable "notification_target" {
  description = "Email address that receives every budget notification (the §15 notification target). Caller-supplied at composition time — never committed. Notifications are OBSERVATIONAL: a budget alert never stops, caps, or remediates spending."
  type        = string

  validation {
    condition     = can(regex("^[^@[:space:]]+@[^@[:space:]]+\\.[^@[:space:]]+$", var.notification_target))
    error_message = "notification_target must be a plausible email address (local@domain.tld)."
  }
}
