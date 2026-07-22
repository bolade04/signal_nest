# main.tf — staging secrets plane (INFRA-4 secrets module)
#
# Owns exactly the secret CONTAINERS and their encryption key for SIGNALNEST_STAGING:
# one customer-managed KMS key (+ deterministic alias) and four EMPTY AWS Secrets
# Manager secret containers (SECRET_KEY, DATABASE_URL, REDIS_URL, LLM_API_KEY). It
# creates NO secret version and NO value — an empty container is intentional and is
# NOT ready for ECS startup until a separately authorized operational procedure
# (INFRA-6) populates and a fail-closed G5 check verifies each value out-of-band.
#
# Dependency boundary (§26.1/§26.12): ZERO upstream module dependencies. Downstream
# only, one-way: `secrets -> iam` (iam consumes the secret + KMS ARNs to build
# identity policies) and `secrets -> ecs` (ecs consumes the secret ARNs for
# task-definition `secrets` valueFrom injection). This module consumes no role ARN,
# endpoint, or ECS/IAM output, so it creates no iam->secrets->iam or ecs->secrets->ecs
# cycle.
#
# No `provider "aws"` block is declared here; `versions.tf` declares only the AWS
# provider SOURCE (no version constraint) per the network/edge/alb convention, so the
# root module (infra/aws/providers.tf + versions.tf) owns the sole provider
# configuration, version constraint, and committed `.terraform.lock.hcl`, all inherited
# by this child module. Tagging is the root provider's `default_tags`; this module adds
# only the conventional per-resource `Name` tag. No secret value, account id, ARN,
# region, or endpoint is committed.

locals {
  # The exact four logical secret keys (UPPERCASED model field names, matching the
  # ECS task-definition `secrets` injection contract §26.7). Used as stable map keys
  # so `secret_arns`/`secret_names` retain these exact keys for iam/ecs wiring.
  secret_keys = toset(["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY"])
}

# --- Customer-managed KMS key for the four application secret containers -----------
# Symmetric ENCRYPT_DECRYPT, rotation on, single-region, enabled, recoverable delete.
# No `policy` is declared: AWS applies its DEFAULT key policy, which grants the key's
# OWN account root full administrative access and thereby delegates access control to
# that account's IAM. This is deliberate (see README "KMS policy / delegation"): it
# enables the account without binding any IAM-module role ARN, so the one-way
# `secrets -> iam` dependency is preserved and no iam->secrets cycle is created.
# `bypass_policy_lockout_safety_check = false` keeps the account-lockout safety active.
resource "aws_kms_key" "secrets" {
  description                        = "SignalNest ${var.name_prefix} secrets CMK — encrypts the four Secrets Manager containers."
  key_usage                          = "ENCRYPT_DECRYPT"
  customer_master_key_spec           = "SYMMETRIC_DEFAULT"
  enable_key_rotation                = true
  multi_region                       = false
  is_enabled                         = true
  deletion_window_in_days            = var.kms_deletion_window_in_days
  bypass_policy_lockout_safety_check = false

  tags = {
    Name = "${var.name_prefix}-secrets-cmk"
  }
}

# Deterministic alias for the secrets CMK. No secret value in the alias name.
resource "aws_kms_alias" "secrets" {
  name          = "alias/${var.name_prefix}/secrets"
  target_key_id = aws_kms_key.secrets.key_id
}

# --- Four EMPTY Secrets Manager containers (no versions, no values) ----------------
# Deterministic names ${name_prefix}/<LOGICAL_KEY>. Each is encrypted by the module
# KMS key and has a recoverable deletion window. NO aws_secretsmanager_secret_version,
# secret_string, or secret_binary is declared anywhere — values are populated
# out-of-band (INFRA-6) and never enter Git/HCL/tfvars/vars/locals/outputs/plan/state.
resource "aws_secretsmanager_secret" "app" {
  for_each = local.secret_keys

  name                    = "${var.name_prefix}/${each.key}"
  description             = "SignalNest ${var.name_prefix} ${each.key} container — EMPTY; value populated out-of-band (INFRA-6), never by IaC."
  kms_key_id              = aws_kms_key.secrets.arn
  recovery_window_in_days = var.secret_recovery_window_in_days

  tags = {
    Name = "${var.name_prefix}-${lower(each.key)}"
  }
}
