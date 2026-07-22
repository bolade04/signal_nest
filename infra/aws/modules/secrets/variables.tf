# variables.tf — typed inputs for the staging secrets module
#
# Scalar, root-provided configuration only. This module has ZERO upstream module
# dependencies (§26.1/§26.12): it consumes no network/edge/alb/registry/storage/
# data_sql/data_cache/iam/ecs/observability/cost output, no role ARN, no endpoint,
# and no secret value. It takes no `tags` input — the authoritative eight-tag set
# is applied by the root provider's `default_tags` (providers.tf). All validation
# is STATIC (regex/range) — none queries AWS.

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\"). Used to build the four Secrets Manager container names (<name_prefix>/<KEY>) and the KMS alias (alias/<name_prefix>/secrets). Contains no account id, credential, secret, endpoint, or ARN."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric and hyphens, 2-63 chars, no leading/trailing hyphen (valid for both Secrets Manager names and the KMS alias)."
  }
}

variable "secret_recovery_window_in_days" {
  description = "Secrets Manager recovery window (days) for the four containers. Recoverable deletion only; 0-day force deletion is forbidden."
  type        = number
  default     = 30

  validation {
    condition     = var.secret_recovery_window_in_days == floor(var.secret_recovery_window_in_days) && var.secret_recovery_window_in_days >= 7 && var.secret_recovery_window_in_days <= 30
    error_message = "secret_recovery_window_in_days must be an integer from 7 through 30 (no 0-day force deletion)."
  }
}

variable "kms_deletion_window_in_days" {
  description = "Deletion window (days) for the customer-managed KMS key that encrypts the secret containers."
  type        = number
  default     = 30

  validation {
    condition     = var.kms_deletion_window_in_days == floor(var.kms_deletion_window_in_days) && var.kms_deletion_window_in_days >= 7 && var.kms_deletion_window_in_days <= 30
    error_message = "kms_deletion_window_in_days must be an integer from 7 through 30."
  }
}
