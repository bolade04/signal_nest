# variables.tf — typed inputs for the staging ALB module
#
# All addressing/identity is variable-driven. NO real VPC id, subnet id, account
# id, or certificate ARN has a committed default. `vpc_id`, `public_subnet_ids`,
# and `api_certificate_arn` are REQUIRED (no default). All validations are STATIC
# (regex/string/length) — none queries AWS.
#
# Ownership decisions (aws-staging-iac-plan.md §24): this module creates+owns ONLY
# the ALB security group (public 443 ingress) and exposes `alb_security_group_id`;
# the two ALB<->API cross-SG rules and the API task SG are owned by the `ecs`
# module, which consumes this module's outputs. This module never consumes an
# ECS/API security-group id (one-way `ecs -> alb`). No `tags` input: the eight-tag
# common set is applied by the root provider's `default_tags`.

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\")."
  type        = string

  validation {
    condition     = length(trimspace(var.name_prefix)) > 0
    error_message = "name_prefix must be a non-empty string."
  }
}

variable "vpc_id" {
  description = "ID of the existing staging VPC (consumed from the network module). Statically validated only — never queried against AWS."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,17}$", var.vpc_id))
    error_message = "vpc_id must be a valid VPC id (vpc- followed by 8-17 hex chars)."
  }
}

variable "public_subnet_ids" {
  description = "Public subnet IDs for the internet-facing ALB (consumed from the network module). At least two distinct subnets in different AZs are required so the ALB can be created. Statically validated only."
  type        = list(string)

  validation {
    condition     = length(var.public_subnet_ids) >= 2
    error_message = "public_subnet_ids must contain at least two subnet IDs (one per AZ) for an internet-facing ALB."
  }

  validation {
    condition     = length(var.public_subnet_ids) == length(distinct(var.public_subnet_ids))
    error_message = "public_subnet_ids must not contain duplicate subnet IDs."
  }

  validation {
    condition     = alltrue([for s in var.public_subnet_ids : can(regex("^subnet-[0-9a-f]{8,17}$", s))])
    error_message = "each public_subnet_ids entry must be a valid subnet id (subnet- followed by 8-17 hex chars)."
  }
}

variable "api_certificate_arn" {
  description = "Existing REGIONAL ACM certificate ARN for the ALB HTTPS listener (CONSUMED, never created/validated/queried). MUST be in us-east-1 to match the ALB's provider region (ADR-0001). Supplied at apply time; no real ARN is committed. Statically validated only — never queried against AWS."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-zA-Z-]*:acm:us-east-1:[0-9]{12}:certificate/.+$", var.api_certificate_arn))
    error_message = "api_certificate_arn must be an ACM certificate ARN in us-east-1 (arn:aws:acm:us-east-1:<account>:certificate/<id>) matching the ALB provider region."
  }
}

variable "api_target_port" {
  description = "TCP port the API container listens on and the ALB forwards to (contract-locked to 8000; apps/api/Dockerfile EXPOSE 8000). Exposed as an input per the alb README contract; the default is the locked staging value."
  type        = number
  default     = 8000

  validation {
    condition     = var.api_target_port > 0 && var.api_target_port <= 65535
    error_message = "api_target_port must be a valid TCP port (1-65535)."
  }
}

variable "health_check_path" {
  description = "Target-group health-check path (contract-locked to the shallow, dependency-free liveness endpoint /health; never the dependency-aware /readiness). Exposed as an input per the alb README contract; the default is the locked staging value."
  type        = string
  default     = "/health"

  validation {
    condition     = can(regex("^/", var.health_check_path))
    error_message = "health_check_path must be an absolute path beginning with '/'."
  }
}
