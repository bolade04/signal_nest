# variables.tf — typed inputs for the staging Redis cache plane (data_cache module)
#
# Semantic, root-provided configuration only. This module consumes `network` outputs
# (vpc_id, private_subnet_ids) and otherwise scalar caller configuration. It consumes
# NO ecs/iam output, no credential, and no secret value. All validation is STATIC
# (regex/range) — none queries AWS. There is NO auth_token, password, port, tags,
# ingress, CIDR, source-security-group, or Redis-URL input (Option A: no Redis auth
# secret in HCL/state; TLS + at-rest encryption + private SG are the controls).

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\"). Builds the replication-group id, cache subnet group, security group, and parameter group names. Contains no account id, credential, region, endpoint, or ARN."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric and hyphens, 2-63 chars, no leading/trailing hyphen."
  }

  validation {
    condition     = !can(regex("--", var.name_prefix))
    error_message = "name_prefix must not contain consecutive hyphens."
  }
}

variable "vpc_id" {
  description = "ID of the VPC (from the network module's vpc_id output) in which the Redis security group is created. Isolated HCL validation cannot prove this VPC exists in the selected account/Region."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,}$", var.vpc_id))
    error_message = "vpc_id must be a syntactically valid VPC id (vpc-<hex>)."
  }
}

variable "private_subnet_ids" {
  description = "Private subnet IDs (from the network module's private_subnet_ids output) for the ElastiCache subnet group. Isolated HCL validation CANNOT prove these subnets are private, span at least two Availability Zones, or exist in the selected account/Region — those are proven only at later root composition and AWS-backed deployment."
  type        = list(string)

  validation {
    condition     = length(distinct(var.private_subnet_ids)) >= 2
    error_message = "private_subnet_ids must contain at least two distinct subnet IDs (spanning multiple AZs)."
  }

  validation {
    condition     = alltrue([for s in var.private_subnet_ids : can(regex("^subnet-[0-9a-f]{8,}$", s))])
    error_message = "each entry of private_subnet_ids must be a syntactically valid subnet id (subnet-<hex>)."
  }
}

variable "engine_version" {
  description = "Redis engine version — a major (e.g. \"7\") or major.minor (e.g. \"7.1\"). REQUIRED with no deployment default so no invented version is committed. The parameter-group family is derived from the major component (Redis uses \"redis6.x\" for the 6 line and \"redis<major>\" for 7+)."
  type        = string

  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]+)?$", var.engine_version))
    error_message = "engine_version must be a Redis major (e.g. \"7\") or major.minor (e.g. \"7.1\")."
  }

  validation {
    condition     = tonumber(split(".", var.engine_version)[0]) >= 6
    error_message = "engine_version major must be >= 6 (encryption-capable ElastiCache Redis)."
  }
}

variable "node_type" {
  description = "ElastiCache node type. Defaults to the staging size cache.t4g.micro; remains caller-configurable for later environments."
  type        = string
  default     = "cache.t4g.micro"

  validation {
    condition     = can(regex("^cache\\.[a-z0-9]+\\.[a-z0-9]+$", var.node_type))
    error_message = "node_type must be a valid ElastiCache node type (e.g. cache.t4g.micro)."
  }
}

variable "num_cache_clusters" {
  description = "Number of nodes in the single-shard replication group (primary + read replicas). Defaults to 1 (single-node staging). Automatic failover / Multi-AZ require at least 2 (enforced by a resource precondition)."
  type        = number
  default     = 1

  validation {
    condition     = var.num_cache_clusters == floor(var.num_cache_clusters) && var.num_cache_clusters >= 1 && var.num_cache_clusters <= 6
    error_message = "num_cache_clusters must be an integer from 1 through 6."
  }
}

variable "automatic_failover_enabled" {
  description = "Whether automatic failover is enabled. Defaults to false (single-node staging). Requires num_cache_clusters >= 2 (enforced by a resource precondition)."
  type        = bool
  default     = false
}

variable "multi_az_enabled" {
  description = "Whether Multi-AZ is enabled. Defaults to false (single-AZ staging). Requires num_cache_clusters >= 2 and automatic failover (enforced by a resource precondition)."
  type        = bool
  default     = false
}

variable "snapshot_retention_limit" {
  description = "Days to retain automatic Redis snapshots. Defaults to 1. Redis here is a cache / queue-coordination store (not the system of record — PostgreSQL is), so minimal retention is acceptable; 0 disables automatic snapshots. Range 0-35."
  type        = number
  default     = 1

  validation {
    condition     = var.snapshot_retention_limit == floor(var.snapshot_retention_limit) && var.snapshot_retention_limit >= 0 && var.snapshot_retention_limit <= 35
    error_message = "snapshot_retention_limit must be an integer from 0 through 35."
  }
}

variable "kms_key_id" {
  description = "Optional customer-managed KMS key id/ARN for at-rest encryption. Defaults to null → the AWS-managed ElastiCache key. A caller-supplied key requires a compatible Region, key state, key policy, and ElastiCache grants that this isolated module cannot validate. FIXED AT CREATION: the at-rest KMS key selection is set when the replication group is created and is not an in-place update; finalize before the first apply. The secrets module CMK is NOT automatically reused."
  type        = string
  default     = null
}
