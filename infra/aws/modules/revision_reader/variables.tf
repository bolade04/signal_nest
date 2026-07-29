# Gate 4J — dedicated revision-reader prerequisite module.

# TWO INDEPENDENT LIFECYCLES (Gate 4M). Publication bootstrap and reader runtime are
# separately gated so the image can be published before any runtime resource — and, in
# particular, before the execution role that holds the DATABASE_URL secret grant — exists.
#
# Both DELIBERATELY INDEPENDENT OF `deploy_workload`: the reader verifies the live schema
# revision BEFORE the workload plan, so it must not be gated by the flag it is meant to
# gate. Neither consumes anything `deploy_workload` gates.

variable "publication_bootstrap_enabled" {
  type        = bool
  default     = false
  description = <<-EOT
    STAGE A. Creates ONLY the publication-bootstrap resources: the dedicated reader ECR
    repository, its lifecycle policy, and the publisher OIDC role + policy (the last only
    when github_oidc_provider_arn is set). Creates NO runtime resource — no log group,
    security group, execution role, runner role, RDS ingress, or task definition, and in
    particular no DATABASE_URL secret grant. Requires no image digest.

    Default false: nothing is provisioned until a later authorized apply.
  EOT
}

variable "runtime_enabled" {
  type        = bool
  default     = false
  description = <<-EOT
    STAGE B. Creates the reader runtime resources (log group, security group, egress rules,
    the reader->RDS ingress rule, execution role + policy, runner role + policy, and the
    task definition). Runtime MUST NOT be enabled by publication alone.

    INVARIANT: runtime_enabled = true requires publication_bootstrap_enabled = true (the
    task definition's image and the execution role's ECR pull both reference the
    bootstrap-owned ECR repository) AND a non-null immutable revision_reader_image_digest.
    Enforced at plan time by the validations below, so an invalid combination errors
    cleanly rather than crashing on an empty resource tuple.
  EOT

  validation {
    condition     = !var.runtime_enabled || var.publication_bootstrap_enabled
    error_message = "revision_reader runtime_enabled requires publication_bootstrap_enabled = true — publish the image via the bootstrap stage before provisioning runtime."
  }

  validation {
    condition     = !var.runtime_enabled || var.revision_reader_image_digest != null
    error_message = "revision_reader runtime_enabled requires a non-null revision_reader_image_digest (immutable sha256) — the task definition must reference an exact published image, never a mutable tag."
  }
}

variable "name_prefix" {
  type        = string
  description = "Deterministic staging name prefix (e.g. signalnest-staging)."
}

variable "vpc_id" {
  type        = string
  description = "VPC for the reader security group."
}

variable "rds_security_group_id" {
  type        = string
  description = "The RDS security group the reader is permitted to reach on 5432."
}

variable "database_url_secret_arn" {
  type        = string
  description = <<-EOT
    ARN of the DATABASE_URL secret, the ONLY secret injected into the reader task.

    HONEST LIMIT (do not remove this note): while this is the application DSN, the role it
    authenticates as OWNS the database and can therefore write and perform DDL. The image
    controls bound what CODE exists and what can RUN; they do not bound the CREDENTIAL. A
    dedicated PostgreSQL role with SELECT on alembic_version and nothing else — delivered
    as its own secret — is the only unconditional control, and it requires separate
    authorization. Until it exists, "read-only" describes this reader's BEHAVIOUR, not the
    identity it connects as.
  EOT
}

variable "secrets_kms_key_arn" {
  type        = string
  description = "CMK used by Secrets Manager, for the execution role's ViaService-scoped Decrypt."
}

variable "revision_reader_image_digest" {
  type        = string
  default     = null
  description = "Immutable sha256 digest of the published reader image. Null until published."

  validation {
    condition     = var.revision_reader_image_digest == null || can(regex("^sha256:[0-9a-f]{64}$", var.revision_reader_image_digest))
    error_message = "revision_reader_image_digest must be a real immutable sha256 digest (never a mutable tag)."
  }
}

variable "github_oidc_provider_arn" {
  type        = string
  default     = null
  description = "Account-wide GitHub OIDC provider ARN. CONSUMED, never created here."
}

variable "github_repository" {
  type        = string
  default     = "bolade04/signal_nest"
  description = "owner/name pinned in the OIDC trust subject claims."
}

variable "log_retention_days" {
  type        = number
  default     = 30
  description = "Retention for the dedicated reader log group."
}

variable "task_cpu" {
  type        = number
  default     = 256
  description = "Minimal CPU. The reader opens one connection and runs one SELECT."
}

variable "task_memory" {
  type        = number
  default     = 512
  description = "Minimal memory."
}

variable "aws_region" {
  type        = string
  description = "Region, for log configuration and condition keys."
}

variable "ecs_cluster_arn" {
  type        = string
  description = <<-EOT
    Staging cluster ARN. Used ONLY as an IAM condition value (ecs:cluster) — this module
    creates no ECS resource that lives in the cluster except the task definition, which
    references no cluster at all.

    The root wires this from `module.ecs.cluster_id`, so there IS a module-level dependency
    on ecs. That is deliberate and is not the circularity that matters: `aws_ecs_cluster` is
    ungated and exists in the foundation stage today. What this module must never consume is
    anything gated by `deploy_workload` — task definitions, services, the workload digests —
    and it consumes none of them.
  EOT
}

variable "tags" {
  type        = map(string)
  default     = {}
  description = "Additional tags."
}
