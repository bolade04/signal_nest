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

# --- Network module inputs (INFRA-4 network tranche) ---
# Addressing is variable-driven; vpc_cidr and availability_zones are REQUIRED and
# have no committed defaults, so no real CIDR or AZ name is embedded in the repo.

variable "vpc_cidr" {
  description = "IPv4 CIDR block for the staging VPC (passed to the network module). Supplied at plan time via a git-ignored *.tfvars; no real CIDR is committed. Prefix /16–/24."
  type        = string

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block (e.g. an RFC1918 /16)."
  }
}

variable "availability_zones" {
  description = "Explicit list of AWS availability zone names for subnet placement (passed to the network module). Supplied explicitly, never discovered via an AWS data source; no real AZ name is committed. Single-AZ is acceptable for staging (contract §N); >= 2 recommended for later ALB/RDS subnet groups."
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 1
    error_message = "availability_zones must contain at least one AZ name."
  }
}

variable "subnet_newbits" {
  description = "Additional prefix bits used to carve per-AZ subnets from vpc_cidr (passed to the network module). Supports up to 2^(subnet_newbits-1) AZs per subnet class."
  type        = number
  default     = 4

  validation {
    condition     = var.subnet_newbits >= 2 && var.subnet_newbits <= 8
    error_message = "subnet_newbits must be between 2 and 8."
  }
}

variable "enable_nat_gateway" {
  description = "Provision a single NAT gateway for private-subnet egress (passed to the network module). A single NAT gateway is the cost-minimized staging choice (contract §M)."
  type        = bool
  default     = true
}

# --- Edge module inputs (INFRA-4 edge tranche; web/SPA only) ---
# The web FQDN, hosted-zone id, and certificate ARN are REQUIRED and have no
# committed defaults, so no real domain, zone id, or ARN is embedded in the repo.
# The ACM certificate and Route 53 hosted zone are CONSUMED by value (§23), never
# created. All validation is STATIC (regex/string) — none queries AWS.

variable "web_fqdn" {
  description = "One complete web FQDN for the SPA (passed to the edge module; e.g. \"app.staging.example.com\"). Supplied at plan time via a git-ignored *.tfvars; no real domain is committed. Bare hostname only — no scheme, port, path, query, or fragment."
  type        = string

  validation {
    condition     = !can(regex("[/?#:]", var.web_fqdn))
    error_message = "web_fqdn must be a bare hostname: no scheme (https://), port, path, query string, or fragment."
  }

  validation {
    condition     = can(regex("^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$", lower(var.web_fqdn)))
    error_message = "web_fqdn must be a valid multi-label FQDN (lowercase labels 1-63 chars, no empty labels, no trailing dot), e.g. app.staging.example.com."
  }

  validation {
    condition     = length(var.web_fqdn) <= 253
    error_message = "web_fqdn must be <= 253 characters."
  }
}

variable "hosted_zone_id" {
  description = "Existing Route 53 hosted-zone id that owns web_fqdn (passed to the edge module; CONSUMED, never created). Supplied at plan time via a git-ignored *.tfvars; no real id is committed. Statically validated only — never queried against AWS."
  type        = string

  validation {
    condition     = can(regex("^Z[A-Z0-9]{1,32}$", var.hosted_zone_id))
    error_message = "hosted_zone_id must be a plausible Route 53 hosted-zone id (starts with 'Z', uppercase alphanumeric)."
  }
}

variable "acm_certificate_arn" {
  description = "Existing CloudFront ACM certificate ARN (passed to the edge module; CONSUMED, never created/validated). MUST be in us-east-1. Supplied at plan time via a git-ignored *.tfvars; no real ARN is committed. Statically validated only — never queried against AWS."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-zA-Z-]*:acm:us-east-1:[0-9]{12}:certificate/.+$", var.acm_certificate_arn))
    error_message = "acm_certificate_arn must be an ACM certificate ARN in us-east-1 (arn:aws:acm:us-east-1:<account>:certificate/<id>)."
  }
}

variable "price_class" {
  description = "CloudFront distribution price class (passed to the edge module). PriceClass_100 (cheapest, NA+EU) is the cost-minimized staging default."
  type        = string
  default     = "PriceClass_100"

  validation {
    condition     = contains(["PriceClass_100", "PriceClass_200", "PriceClass_All"], var.price_class)
    error_message = "price_class must be one of PriceClass_100, PriceClass_200, PriceClass_All."
  }
}

# --- ALB module inputs (INFRA-4 alb tranche) ---
# The regional ACM certificate ARN for the ALB HTTPS listener is REQUIRED and has no
# committed default, so no real ARN is embedded in the repo. The certificate is
# CONSUMED by value (§24.4), never created/queried. It must be in us-east-1 to match
# the ALB's provider region (ADR-0001). Validation is STATIC (regex) — never queries AWS.

variable "api_certificate_arn" {
  description = "Existing REGIONAL ACM certificate ARN for the ALB HTTPS listener (passed to the alb module; CONSUMED, never created/validated). MUST be in us-east-1 to match the ALB provider region (ADR-0001). Supplied at plan time via a git-ignored *.tfvars; no real ARN is committed. Statically validated only — never queried against AWS."
  type        = string

  validation {
    condition     = can(regex("^arn:aws[a-zA-Z-]*:acm:us-east-1:[0-9]{12}:certificate/.+$", var.api_certificate_arn))
    error_message = "api_certificate_arn must be an ACM certificate ARN in us-east-1 (arn:aws:acm:us-east-1:<account>:certificate/<id>)."
  }
}
