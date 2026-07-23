# variables.tf — typed inputs for the staging SQL data plane (data_sql module)
#
# Semantic, root-provided configuration only. This module consumes `network` outputs
# (vpc_id, private_subnet_ids) and otherwise scalar caller configuration. It consumes
# NO ecs/iam output, no credential, and no secret value. All validation is STATIC
# (regex/range) — none queries AWS. The master password is NEVER a variable here (the
# instance uses manage_master_user_password); there is no `password`, `port`, `tags`,
# ingress, CIDR, log-export, monitoring, or database-URL input.

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\"). Used to build the RDS instance identifier, DB subnet group, security group, and parameter group names. Contains no account id, credential, region, endpoint, or ARN."
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
  description = "ID of the VPC (from the network module's vpc_id output) in which the RDS security group is created. Isolated HCL validation cannot prove this VPC exists in the selected account/Region."
  type        = string

  validation {
    condition     = can(regex("^vpc-[0-9a-f]{8,}$", var.vpc_id))
    error_message = "vpc_id must be a syntactically valid VPC id (vpc-<hex>)."
  }
}

variable "private_subnet_ids" {
  description = "Private subnet IDs (from the network module's private_subnet_ids output) for the DB subnet group. Isolated HCL validation CANNOT prove these subnets are private, span at least two Availability Zones, or exist in the selected account/Region — those are proven only at later root composition and AWS-backed deployment."
  type        = list(string)

  validation {
    condition     = length(distinct(var.private_subnet_ids)) >= 2
    error_message = "private_subnet_ids must contain at least two distinct subnet IDs (RDS DB subnet groups require subnets in at least two AZs)."
  }

  validation {
    condition     = alltrue([for s in var.private_subnet_ids : can(regex("^subnet-[0-9a-f]{8,}$", s))])
    error_message = "each entry of private_subnet_ids must be a syntactically valid subnet id (subnet-<hex>)."
  }
}

variable "db_subnet_group_name" {
  description = "Optional explicit DB subnet group name (preserved from the module stub). When null, the name is derived deterministically from name_prefix (<name_prefix>-pg). When supplied, must satisfy RDS DB-subnet-group naming rules."
  type        = string
  default     = null

  validation {
    condition     = var.db_subnet_group_name == null || can(regex("^[a-z][a-z0-9-]{0,254}$", var.db_subnet_group_name == null ? "x" : var.db_subnet_group_name))
    error_message = "db_subnet_group_name, when supplied, must be lowercase, start with a letter, contain only letters/digits/hyphens, and be 1-255 chars."
  }
}

variable "engine_version" {
  description = "PostgreSQL engine version — a major version (e.g. \"16\") or a supported major.minor (e.g. \"16.3\"). REQUIRED with no deployment default so no invented version is committed. The DB parameter-group family is derived from its major component as postgres<major>; allow_major_version_upgrade is false, so the version and family cannot drift apart."
  type        = string

  validation {
    condition     = can(regex("^[0-9]{2}(\\.[0-9]{1,2})?$", var.engine_version))
    error_message = "engine_version must be a PostgreSQL major (e.g. \"16\") or major.minor (e.g. \"16.3\") — two-digit major, optional numeric minor."
  }
}

variable "instance_class" {
  description = "RDS instance class. Defaults to the staging size db.t4g.micro; remains caller-configurable because later environments may need larger sizing."
  type        = string
  default     = "db.t4g.micro"

  validation {
    condition     = can(regex("^db\\.[a-z0-9]+\\.[a-z0-9]+$", var.instance_class))
    error_message = "instance_class must be a valid RDS instance class (e.g. db.t4g.micro)."
  }
}

variable "allocated_storage_gb" {
  description = "Initial allocated storage in GiB. Defaults to the staging size 20. gp3 for PostgreSQL requires at least 20 GiB."
  type        = number
  default     = 20

  validation {
    condition     = var.allocated_storage_gb == floor(var.allocated_storage_gb) && var.allocated_storage_gb >= 20 && var.allocated_storage_gb <= 65536
    error_message = "allocated_storage_gb must be an integer from 20 through 65536 (gp3 PostgreSQL minimum is 20 GiB)."
  }
}

variable "max_allocated_storage_gb" {
  description = "Optional storage-autoscaling ceiling in GiB. Defaults to null (autoscaling disabled). When non-null it must be at least 10% greater than allocated_storage_gb (enforced by a resource precondition), consistent with RDS storage autoscaling."
  type        = number
  default     = null

  validation {
    condition     = var.max_allocated_storage_gb == null || (var.max_allocated_storage_gb == floor(var.max_allocated_storage_gb) && var.max_allocated_storage_gb <= 65536)
    error_message = "max_allocated_storage_gb, when supplied, must be an integer no greater than 65536."
  }
}

variable "database_name" {
  description = "Initial PostgreSQL database name created by RDS. REQUIRED with no invented default. Must satisfy PostgreSQL identifier rules."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,62}$", var.database_name))
    error_message = "database_name must start with a letter and contain only letters, digits, and underscores (1-63 chars)."
  }
}

variable "master_username" {
  description = "PostgreSQL master (administrative/bootstrap) username. REQUIRED with no invented default. This is NOT the finished API/worker credential. Not marked sensitive — a username alone is not a secret."
  type        = string

  validation {
    condition     = can(regex("^[a-zA-Z][a-zA-Z0-9_]{0,62}$", var.master_username)) && !contains(["rdsadmin", "admin", "postgres"], lower(var.master_username))
    error_message = "master_username must start with a letter, contain only letters/digits/underscores (1-63 chars), and must not be a reserved name (rdsadmin/admin/postgres)."
  }
}

variable "storage_kms_key_id" {
  description = "Optional customer-managed KMS key id/ARN for RDS storage encryption at rest. Defaults to null → the AWS-managed RDS key. A caller-supplied key requires a compatible Region, key state, key policy, and RDS grants that this isolated module cannot validate. The secrets module CMK is NOT automatically reused."
  type        = string
  default     = null
}

variable "master_user_secret_kms_key_id" {
  description = "Optional customer-managed KMS key id/ARN for the RDS-managed master-user Secrets Manager secret. Defaults to null → the AWS-managed Secrets Manager key. A caller-supplied key requires a compatible Region, key state, key policy, and Secrets Manager grants that this isolated module cannot validate. The secrets module CMK is NOT automatically reused."
  type        = string
  default     = null
}

variable "multi_az" {
  description = "Whether to deploy a Multi-AZ standby. Defaults to false (single-AZ staging); remains caller-configurable for later environments."
  type        = bool
  default     = false
}

variable "backup_retention_period" {
  description = "Automated backup retention in days. Defaults to 7; must be between 1 and 35 (backups cannot be disabled)."
  type        = number
  default     = 7

  validation {
    condition     = var.backup_retention_period == floor(var.backup_retention_period) && var.backup_retention_period >= 1 && var.backup_retention_period <= 35
    error_message = "backup_retention_period must be an integer from 1 through 35 (automated backups may not be disabled)."
  }
}

variable "deletion_protection" {
  description = "Whether RDS deletion protection is enabled. Defaults to true (safe default); a deliberate teardown must set it false explicitly."
  type        = bool
  default     = true
}

variable "skip_final_snapshot" {
  description = "Whether to skip the final snapshot on deletion. Defaults to false so a final snapshot is taken. When false, final_snapshot_identifier must be supplied (enforced by a resource precondition)."
  type        = bool
  default     = false
}

variable "final_snapshot_identifier" {
  description = "Caller-supplied identifier for the final snapshot taken when skip_final_snapshot is false. Defaults to null. No timestamp is generated in HCL and no permanently reusable fixed name is derived (which could collide after destroy/recreate); the caller owns a unique value."
  type        = string
  default     = null

  validation {
    condition     = var.final_snapshot_identifier == null || can(regex("^[a-zA-Z][a-zA-Z0-9-]{0,254}$", var.final_snapshot_identifier == null ? "x" : var.final_snapshot_identifier))
    error_message = "final_snapshot_identifier, when supplied, must start with a letter and contain only letters/digits/hyphens (1-255 chars)."
  }
}
