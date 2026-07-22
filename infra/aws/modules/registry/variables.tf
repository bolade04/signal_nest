# variables.tf — typed inputs for the staging registry module
#
# Scalar, root-provided configuration only. This module has ZERO upstream module
# dependencies (§26.12): it consumes no other module output, no role/registry/account
# id, no KMS ARN, no image tag/digest/URI, and no Docker/AWS credential. It takes no
# `tags` input — the authoritative eight-tag set is applied by the root provider's
# `default_tags` (providers.tf). The two logical repositories are FIXED to `api` and
# `worker` inside the module (see locals in main.tf); callers cannot add a third
# repository. All validation is STATIC (regex/range) — none queries AWS.

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\"). Builds the two ECR repository paths <name_prefix>/api and <name_prefix>/worker. Contains no account id, credential, region, endpoint, ARN, image tag, or digest."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric and hyphens, 2-63 chars, no leading/trailing hyphen (valid as an ECR repository path prefix; remains valid when /api or /worker is appended)."
  }

  validation {
    condition     = !can(regex("--", var.name_prefix))
    error_message = "name_prefix must not contain consecutive hyphens (no double separators)."
  }
}

variable "untagged_image_retention_days" {
  description = "Number of days after which UNTAGGED images expire via the per-repository lifecycle policy. Tagged (immutable release) images are never expired by this policy. Applies independently to both repositories."
  type        = number
  default     = 14

  validation {
    condition     = var.untagged_image_retention_days == floor(var.untagged_image_retention_days) && var.untagged_image_retention_days >= 1 && var.untagged_image_retention_days <= 365
    error_message = "untagged_image_retention_days must be an integer from 1 through 365."
  }
}
