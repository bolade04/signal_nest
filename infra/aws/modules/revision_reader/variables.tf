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

  # STAGE-A PRECONDITION (Gate 4N-I5, RE-KEYED at Gate 4N-I16 Defect 1).
  #
  # WHY THIS IS CODE AND NOT PROSE. The Stage-A graph creates the ECR repository and its
  # lifecycle policy BEFORE the publisher role. The temporary operator's iam:CreateRole
  # grant is conditioned on iam:PermissionsBoundary, so with no boundary ARN the provider
  # sends no boundary, the condition cannot match, and the apply fails AFTER both ECR
  # resources already exist — a partial apply. Gate 4N-I4 documented that requirement in
  # a JSON artifact no tool consults, which is why it did not prevent anything.
  #
  # GATE 4N-I16 DEFECT 1. This validation used to read `role_permissions_boundary_arn != null`.
  # That was the determinant BEFORE Gate 4N-I14 made the module consume a MODE. Since I14 the
  # value actually sent to the provider is `local.effective_permissions_boundary`, which is
  # null whenever the mode is not "required" — REGARDLESS of the ARN variable. So
  # bootstrap-enabled + mode "disabled" + a syntactically valid ARN passed every guard and
  # produced an UNBOUNDED publisher role: precisely the partial apply this message describes.
  # The check now reads the SAME determinant the resources read. A validation keyed to a
  # superseded signal is worse than none, because it reports safety it no longer measures.
  validation {
    condition     = !var.publication_bootstrap_enabled || var.role_boundary_mode == "required"
    error_message = "revision_reader publication_bootstrap_enabled requires role_boundary_mode = \"required\". Stage A creates protected IAM roles, and the roles consume the MODE-DERIVED boundary (local.effective_permissions_boundary), which is null in \"disabled\" mode no matter what role_permissions_boundary_arn holds. An unbounded create fails AFTER the ECR resources exist."
  }

  validation {
    condition     = !var.publication_bootstrap_enabled || var.role_permissions_boundary_arn != null
    error_message = "revision_reader publication_bootstrap_enabled requires a non-null role_permissions_boundary_arn — the bootstrap operator's iam:CreateRole grant is conditioned on iam:PermissionsBoundary, so an unbounded create fails AFTER the ECR resources are created."
  }

  # NOTE: a second validation requiring github_oidc_provider_arn was considered and
  # REJECTED. The module deliberately supports publication with the OIDC provider absent
  # (it then skips only the federated roles — see the oidc_absent_skips_only_the_federated_roles
  # test), so forbidding that configuration here would break a supported path. Asserting
  # that Stage A produces exactly the four intended addresses is a PLAN-ACCEPTANCE concern
  # and belongs in the execution contract, not in a module validation.
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

# Permissions boundary applied to EVERY IAM role this module creates (Gate 4N-I3).
# The boundary caps a created role's effective permissions to the intersection of its
# identity policy and the boundary, so a role minted here can never exceed it — closing
# the transitive escalation recorded in Gate 4N-I1/I2.
#
# DARK BY DEFAULT: null means no boundary is set, which is exactly today's deployed
# state, so this variable changes nothing until a later authorized gate creates the
# boundary policy and supplies its ARN. Applying it is deliberately NOT part of this gate.
variable "role_permissions_boundary_arn" {
  description = "ARN of the permissions boundary applied to every IAM role created by this module. Null (default) leaves roles unbounded, preserving the currently deployed state."
  type        = string
  default     = null

  validation {
    condition     = var.role_permissions_boundary_arn == null || can(regex("^arn:aws:iam::[0-9]{12}:policy/", var.role_permissions_boundary_arn))
    error_message = "role_permissions_boundary_arn must be null or a full IAM policy ARN (arn:aws:iam::<account>:policy/<name>)."
  }
}

# GATE 4N-I14 DEFECT 2. `role_boundary_mode` existed only inside variable validation — the
# resource expression consumed the ARN directly, so the mode changed no plan and was a
# validation side channel. It is now a real graph input: the boundary a role receives is
# DERIVED from the mode, not from the ARN's nullness.
variable "role_boundary_mode" {
  description = "Boundary enforcement mode. 'required' attaches the exact reviewed boundary to every role this module creates; 'disabled' is the pre-rollout dark state and MUST be stated deliberately."
  type        = string

  validation {
    condition     = contains(["disabled", "required"], var.role_boundary_mode)
    error_message = "role_boundary_mode must be exactly 'disabled' or 'required'."
  }
}
