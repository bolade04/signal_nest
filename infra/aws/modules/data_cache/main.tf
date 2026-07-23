# main.tf — staging Redis cache plane (INFRA-4 data_cache module)
#
# Owns a private Amazon ElastiCache for Redis replication group for SIGNALNEST_STAGING
# (cache, queue coordination, and notify channel), its cache subnet group, its
# (initially rule-free) Redis security group, and a Redis parameter group. The
# replication group is private (private subnets only, no public access), encrypted at
# rest, and TLS-required in transit.
#
# OPTION A — no Redis auth secret in HCL or state. `transit_encryption_enabled = true`
# with `transit_encryption_mode = "required"` enforces TLS, and `at_rest_encryption_enabled`
# is true. There is deliberately NO `auth_token`: no Redis password is generated, read,
# stored, output, or committed, and none enters OpenTofu state. Access control is the
# private Redis security group (below) plus subnet isolation. Application connections use
# `rediss://` via the out-of-band `REDIS_URL` (composed and injected through Secrets
# Manager under a separate step); this module builds and outputs NO `REDIS_URL`.
#
# NETWORK/SG BOUNDARY (aws-staging-iac-plan.md §26.2-26.3): this module CREATES and OWNS
# the Redis security group and OUTPUTS its id, but authors NO ingress/egress rules. `ecs`
# later owns the two standalone TCP 6379 ingress rules — from the API and worker task SGs
# ONLY. The MIGRATION task is pinned to non-Redis backends and receives NO Redis access
# (§26.3). One-way `data_cache -> ecs`, acyclic. No CIDR input, no 0.0.0.0/0 anywhere,
# no ECS dependency here, no AWS-querying data source.
#
# No `provider "aws"` block is declared; `versions.tf` declares only the provider SOURCE
# (no version constraint) per the sibling-module convention, so the root owns the sole
# provider config, version constraint, and committed lockfile, inherited here. Tagging is
# the root provider's `default_tags`; this module adds only the conventional per-resource
# `Name` tag. No account id, ARN, region, VPC/subnet/KMS id, password, or Redis URL is
# committed.

locals {
  engine_major = split(".", var.engine_version)[0]
  # Redis parameter-group families: the 6 line is "redis6.x"; 7+ is "redis<major>".
  parameter_family     = local.engine_major == "6" ? "redis6.x" : "redis${local.engine_major}"
  replication_group_id = "${var.name_prefix}-redis"
}

# --- Cache subnet group (private subnets only) ------------------------------------
resource "aws_elasticache_subnet_group" "this" {
  name       = "${var.name_prefix}-redis-subnets"
  subnet_ids = var.private_subnet_ids

  tags = {
    Name = "${var.name_prefix}-redis-subnets"
  }
}

# --- Redis security group: created here, ZERO rules -------------------------------
# Intentionally declares no ingress and no egress. ECS owns the standalone TCP 6379
# ingress rules (from API and worker task SGs only; migration gets none) in a later
# tranche. Declaring no egress block leaves the security group with no egress rule.
resource "aws_security_group" "redis" {
  name        = "${var.name_prefix}-redis-sg"
  description = "ElastiCache Redis SG for ${var.name_prefix}; ingress rules are owned by the ecs module (API/worker only, no migration)."
  vpc_id      = var.vpc_id

  tags = {
    Name = "${var.name_prefix}-redis-sg"
  }
}

# --- Redis parameter group --------------------------------------------------------
# Custom (empty) group pinned to the engine's family so future tuning has a home; no
# custom parameters are set in this tranche.
resource "aws_elasticache_parameter_group" "this" {
  name        = "${var.name_prefix}-redis-params"
  family      = local.parameter_family
  description = "Redis parameter group for ${var.name_prefix} (family ${local.parameter_family})."

  tags = {
    Name = "${var.name_prefix}-redis-params"
  }
}

# --- Private ElastiCache for Redis replication group ------------------------------
resource "aws_elasticache_replication_group" "this" {
  replication_group_id = local.replication_group_id
  description          = "${var.name_prefix} staging Redis (cache/queue/notify)"

  engine         = "redis"
  engine_version = var.engine_version
  node_type      = var.node_type
  port           = 6379

  num_cache_clusters         = var.num_cache_clusters
  automatic_failover_enabled = var.automatic_failover_enabled
  multi_az_enabled           = var.multi_az_enabled

  subnet_group_name    = aws_elasticache_subnet_group.this.name
  security_group_ids   = [aws_security_group.redis.id]
  parameter_group_name = aws_elasticache_parameter_group.this.name

  # Option A: at-rest + TLS-required in transit, NO auth_token.
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
  transit_encryption_mode    = "required"
  kms_key_id                 = var.kms_key_id

  snapshot_retention_limit = var.snapshot_retention_limit

  tags = {
    Name = "${var.name_prefix}-redis"
  }

  lifecycle {
    precondition {
      condition     = var.automatic_failover_enabled == false || var.num_cache_clusters >= 2
      error_message = "automatic_failover_enabled requires num_cache_clusters >= 2."
    }

    precondition {
      condition     = var.multi_az_enabled == false || (var.automatic_failover_enabled && var.num_cache_clusters >= 2)
      error_message = "multi_az_enabled requires automatic_failover_enabled and num_cache_clusters >= 2."
    }
  }
}
