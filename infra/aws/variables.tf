# variables.tf — typed root inputs (INFRA-4 skeleton)
#
# Only inputs supported by the authoritative plans are declared. No credential,
# secret-value, account-id, ARN, domain, certificate, CIDR, resource-id, or
# password variable exists in this tranche. The full 87-field application
# settings inventory is NOT reproduced here as IaC variables.

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
  description = "Deployment environment. This staging-only skeleton accepts exactly 'staging'; production is out of scope for INFRA-4."
  type        = string
  default     = "staging"

  validation {
    condition     = var.environment == "staging"
    error_message = "environment must be 'staging'. This INFRA-4 skeleton is staging-only; production is a separate, later phase."
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
