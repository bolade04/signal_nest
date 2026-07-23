# main.tf — staging SQL data plane (INFRA-4 data_sql module)
#
# Owns a private Amazon RDS for PostgreSQL DB instance for SIGNALNEST_STAGING, its DB
# subnet group, its (initially rule-free) RDS security group, and a TLS-enforcing DB
# parameter group. The instance is private (not publicly accessible), encrypted at
# rest (gp3), and forces TLS in transit via rds.force_ssl.
#
# CREDENTIAL BOUNDARY (do not blur): manage_master_user_password = true, so RDS
# generates and manages the MASTER (administrative/bootstrap) password in an
# RDS-owned Secrets Manager secret. No password is a variable, a local, a
# random_password, a secret version, or an output here, and the value never enters
# OpenTofu state. The master credential is NOT the finished API/worker credential:
# this module populates no application-facing DATABASE_URL, grants no ECS/IAM access,
# reads no secret value, and creates no least-privilege application/migration role.
# Those remain a separately authorized bootstrap/deployment concern.
#
# NETWORK/SG BOUNDARY (§26): this module CREATES and OWNS the RDS security group and
# OUTPUTS its id, but authors NO ingress/egress rules. `ecs` later owns the three
# standalone TCP 5432 ingress rules (from the API, worker, and migration task SGs) —
# one-way `data_sql -> ecs`, acyclic. There is no ECS dependency here, no CIDR input,
# and no 0.0.0.0/0 anywhere. pgvector is NOT preloaded via shared_preload_libraries;
# `CREATE EXTENSION vector` is a later database-bootstrap step, not part of this HCL.
#
# No `provider "aws"` block is declared; `versions.tf` declares only the provider
# SOURCE (no version constraint) per the sibling-module convention, so the root owns
# the sole provider config, version constraint, and committed lockfile, inherited
# here. Tagging is the root provider's `default_tags`; this module adds only the
# conventional per-resource `Name` tag. No account id, ARN, region, VPC/subnet/KMS id,
# password, or database URL is committed.

locals {
  # Derive the DB parameter-group family from the engine version's major component so
  # the version and family cannot drift apart (allow_major_version_upgrade is false).
  engine_major         = split(".", var.engine_version)[0]
  parameter_family     = "postgres${local.engine_major}"
  db_subnet_group_name = coalesce(var.db_subnet_group_name, "${var.name_prefix}-pg")
  db_instance_id       = "${var.name_prefix}-postgres"
}

# --- DB subnet group (private subnets only) ---------------------------------------
resource "aws_db_subnet_group" "this" {
  name       = local.db_subnet_group_name
  subnet_ids = var.private_subnet_ids

  tags = {
    Name = "${var.name_prefix}-pg-subnets"
  }
}

# --- RDS security group: created here, ZERO rules -----------------------------------
# Intentionally declares no ingress and no egress. ECS owns the standalone TCP 5432
# ingress rules (from API/worker/migration task SGs) in a later tranche. Declaring no
# egress block leaves the security group with no egress rule.
resource "aws_security_group" "rds" {
  name        = "${var.name_prefix}-pg-sg"
  description = "RDS PostgreSQL SG for ${var.name_prefix}; ingress rules are owned by the ecs module."
  vpc_id      = var.vpc_id

  tags = {
    Name = "${var.name_prefix}-pg-sg"
  }
}

# --- DB parameter group: TLS enforcement only --------------------------------------
resource "aws_db_parameter_group" "this" {
  name   = "${var.name_prefix}-pg-params"
  family = local.parameter_family

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  tags = {
    Name = "${var.name_prefix}-pg-params"
  }
}

# --- Private RDS for PostgreSQL DB instance ----------------------------------------
resource "aws_db_instance" "this" {
  identifier = local.db_instance_id

  engine                      = "postgres"
  engine_version              = var.engine_version
  allow_major_version_upgrade = false
  instance_class              = var.instance_class

  allocated_storage     = var.allocated_storage_gb
  max_allocated_storage = var.max_allocated_storage_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = var.storage_kms_key_id

  db_name  = var.database_name
  username = var.master_username

  # RDS generates and manages the master password in an RDS-owned Secrets Manager
  # secret; the value never enters OpenTofu state. No password/master_password is set.
  manage_master_user_password   = true
  master_user_secret_kms_key_id = var.master_user_secret_kms_key_id

  port                = 5432
  publicly_accessible = false

  db_subnet_group_name   = aws_db_subnet_group.this.name
  vpc_security_group_ids = [aws_security_group.rds.id]
  parameter_group_name   = aws_db_parameter_group.this.name

  multi_az                            = var.multi_az
  backup_retention_period             = var.backup_retention_period
  copy_tags_to_snapshot               = true
  auto_minor_version_upgrade          = true
  iam_database_authentication_enabled = false

  deletion_protection       = var.deletion_protection
  skip_final_snapshot       = var.skip_final_snapshot
  final_snapshot_identifier = var.final_snapshot_identifier

  tags = {
    Name = "${var.name_prefix}-postgres"
  }

  lifecycle {
    precondition {
      condition     = var.skip_final_snapshot || var.final_snapshot_identifier != null
      error_message = "final_snapshot_identifier must be supplied when skip_final_snapshot is false."
    }

    precondition {
      condition     = var.max_allocated_storage_gb == null || var.max_allocated_storage_gb >= var.allocated_storage_gb * 1.1
      error_message = "max_allocated_storage_gb must be at least 10% greater than allocated_storage_gb to enable RDS storage autoscaling."
    }
  }
}
