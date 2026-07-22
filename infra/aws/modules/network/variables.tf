# variables.tf — typed inputs for the staging network module
#
# Addressing is entirely variable-driven. NO real CIDR, AZ name, account id, ARN,
# hostname, or secret has a committed default. `vpc_cidr` and `availability_zones`
# are REQUIRED (no default) so no real environment addressing is embedded here.

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\")."
  type        = string

  validation {
    condition     = length(trimspace(var.name_prefix)) > 0
    error_message = "name_prefix must be a non-empty string."
  }
}

variable "vpc_cidr" {
  description = "IPv4 CIDR block for the staging VPC. Supplied at plan time; no real CIDR is committed. Recommended a private RFC1918 range with prefix between /16 and /24."
  type        = string

  validation {
    condition     = can(cidrhost(var.vpc_cidr, 0))
    error_message = "vpc_cidr must be a valid IPv4 CIDR block (e.g. an RFC1918 /16)."
  }

  validation {
    condition     = tonumber(split("/", var.vpc_cidr)[1]) >= 16 && tonumber(split("/", var.vpc_cidr)[1]) <= 24
    error_message = "vpc_cidr prefix length must be between /16 and /24 to leave room for the derived subnet scheme."
  }
}

variable "availability_zones" {
  description = "Explicit list of AWS availability zone names to place subnets in. Supplied explicitly (not discovered via an AWS data source); no real AZ name is committed. Single-AZ is acceptable for staging (runtime contract §N); >= 2 is recommended so later ALB/RDS subnet groups (which require multiple AZs) can be created."
  type        = list(string)

  validation {
    condition     = length(var.availability_zones) >= 1
    error_message = "availability_zones must contain at least one AZ name."
  }

  validation {
    condition     = length(var.availability_zones) == length(distinct(var.availability_zones))
    error_message = "availability_zones must not contain duplicates."
  }
}

variable "subnet_newbits" {
  description = "Number of additional prefix bits used to carve per-AZ subnets from vpc_cidr via cidrsubnet(). The lower half of the resulting blocks is used for public subnets and the upper half for private subnets, so each class supports up to 2^(subnet_newbits-1) AZs."
  type        = number
  default     = 4

  validation {
    condition     = var.subnet_newbits >= 2 && var.subnet_newbits <= 8
    error_message = "subnet_newbits must be between 2 and 8."
  }
}

variable "enable_nat_gateway" {
  description = "When true, provision a single NAT gateway + EIP and a default private-subnet route through it for outbound egress (LLM/API calls and image pulls per runtime contract §D). When false, private subnets have no default route (fully isolated). A single NAT gateway is the cost-minimized staging choice (contract §M)."
  type        = bool
  default     = true
}

variable "enable_dns_support" {
  description = "Enable DNS resolution in the VPC (required for interface endpoints, service discovery, and RDS/Redis hostnames)."
  type        = bool
  default     = true
}

variable "enable_dns_hostnames" {
  description = "Enable DNS hostnames in the VPC (required for private DNS on future interface VPC endpoints)."
  type        = bool
  default     = true
}
