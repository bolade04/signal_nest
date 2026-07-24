# variables.tf — typed inputs for the one-time state-bootstrap root
#
# The eight identity/tag inputs mirror the main root exactly (same names, same
# defaults, same validations). The state bucket name, lock table name, and KMS
# deletion window are the only bootstrap-specific inputs. NO real bucket or
# table name is committed anywhere (aws-staging-iac-plan.md §7: "never
# committed") — both names are REQUIRED, arrive via a git-ignored *.tfvars at
# the later-authorized live bootstrap execution, and are statically validated
# only (never queried against AWS).

variable "project_name" {
  description = "Logical project name used for tagging and name prefixes (runtime contract §A: Project=SignalNest)."
  type        = string
  default     = "SignalNest"

  validation {
    condition     = length(trimspace(var.project_name)) > 0
    error_message = "project_name must be a non-empty string."
  }
}

variable "environment" {
  description = "Deployment environment. This staging-only bootstrap accepts exactly 'staging'; production is out of scope for INFRA-4."
  type        = string
  default     = "staging"

  validation {
    condition     = var.environment == "staging"
    error_message = "environment must be 'staging'. This bootstrap root is staging-only; production is a separate, later phase."
  }
}

variable "aws_region" {
  description = "AWS region for the staging environment (ADR-0001: us-east-1)."
  type        = string
  default     = "us-east-1"

  validation {
    condition     = var.aws_region == "us-east-1"
    error_message = "aws_region must be 'us-east-1' per ADR-0001 for SIGNALNEST_STAGING."
  }
}

variable "alias" {
  description = "Logical environment alias tag (runtime contract §A: Alias=SIGNALNEST_STAGING)."
  type        = string
  default     = "SIGNALNEST_STAGING"

  validation {
    condition     = length(trimspace(var.alias)) > 0
    error_message = "alias must be a non-empty string."
  }
}

variable "owner" {
  description = "Logical internal owning team for tagging (runtime contract §A: Owner). Non-sensitive logical label only; no individual identity."
  type        = string
  default     = "internal-platform-ops"

  validation {
    condition     = length(trimspace(var.owner)) > 0
    error_message = "owner must be a non-empty logical team label."
  }
}

variable "cost_center" {
  description = "Logical cost-center label for cost attribution (runtime contract §A: CostCenter). Non-sensitive logical label only."
  type        = string
  default     = "signalnest-staging"

  validation {
    condition     = length(trimspace(var.cost_center)) > 0
    error_message = "cost_center must be a non-empty logical label."
  }
}

variable "data_class" {
  description = "Data classification tag (runtime contract §A: DataClass=internal-no-customer)."
  type        = string
  default     = "internal-no-customer"

  validation {
    condition     = length(trimspace(var.data_class)) > 0
    error_message = "data_class must be a non-empty string."
  }
}

variable "phase" {
  description = "Delivery phase tag (runtime contract §A: Phase=4B-C)."
  type        = string
  default     = "4B-C"

  validation {
    condition     = length(trimspace(var.phase)) > 0
    error_message = "phase must be a non-empty string."
  }
}

# --- Bootstrap-specific inputs ------------------------------------------------------

variable "state_bucket_name" {
  description = "Globally unique name for the OpenTofu remote-state S3 bucket (§7). REQUIRED; supplied at live-bootstrap time via a git-ignored *.tfvars — never committed. Statically validated against S3 naming rules only."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.state_bucket_name))
    error_message = "state_bucket_name must be a valid S3 bucket name (3-63 chars; lowercase letters, digits, dots, hyphens; starts/ends alphanumeric)."
  }
}

variable "lock_table_name" {
  description = "Name for the DynamoDB state-lock table (§7). REQUIRED; supplied at live-bootstrap time via a git-ignored *.tfvars — never committed. Statically validated against DynamoDB naming rules only."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z0-9_.-]{3,255}$", var.lock_table_name))
    error_message = "lock_table_name must be a valid DynamoDB table name (3-255 chars; letters, digits, underscore, dot, hyphen)."
  }
}

variable "kms_deletion_window_in_days" {
  description = "Recovery window before the state-encryption CMK is destroyed after a scheduled deletion (7-30 days)."
  type        = number
  default     = 30

  validation {
    condition     = var.kms_deletion_window_in_days >= 7 && var.kms_deletion_window_in_days <= 30
    error_message = "kms_deletion_window_in_days must be between 7 and 30 (AWS KMS bounds)."
  }
}
