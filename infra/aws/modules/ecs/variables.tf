# variables.tf — typed inputs for the staging ECS/Fargate compute plane
#
# Inputs are configuration REFERENCES produced by sibling modules plus the
# deterministic root `name_prefix` (aws-staging-iac-plan.md §26.10-§26.12,
# §26.15). No secret value, account id, region, credential, CIDR, or real
# ARN/identifier is committed — ARN/id-typed inputs are wired from module
# outputs at composition time, never from literals. There is deliberately NO
# task-security-group input (this module CREATES the three task SGs, §26.2), NO
# storage input (§26.12 has no `storage -> ecs` edge; `S3_BUCKET` arrives via
# the ordinary environment maps, §26.11), and NO `tags` input (root provider
# `default_tags`).

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\"). Builds the cluster/service/task-family/SG names and the three deterministic /ecs/<name_prefix>-{api,worker,migration} log-group names (§26.9). Contains no account id, credential, region, endpoint, or ARN."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric and hyphens, 2-63 chars, no leading/trailing hyphen."
  }
}

variable "cluster_name" {
  description = "Optional ECS cluster name override. Defaults (null) to the deterministic \"<name_prefix>-cluster\". Exposed per the ecs README planned-interface contract."
  type        = string
  default     = null
  nullable    = true
}

# --- Network (network -> ecs) ------------------------------------------------------
variable "vpc_id" {
  description = "VPC id from the network module. Hosts the three task security groups."
  type        = string
}

variable "private_subnet_ids" {
  description = "Private subnet ids from the network module. All tasks run here with no public IP (§26.10)."
  type        = list(string)

  validation {
    condition     = length(var.private_subnet_ids) > 0
    error_message = "private_subnet_ids must contain at least one private subnet id."
  }
}

# --- ALB boundary (alb -> ecs) -----------------------------------------------------
variable "alb_security_group_id" {
  description = "ALB security-group id from the alb module. This module authors BOTH locked ALB↔API TCP 8000 cross-SG rules (§26.3/§26.13); the alb module never consumes an ECS output."
  type        = string
}

variable "api_target_group_arn" {
  description = "API target-group ARN from the alb module (target type ip, port 8000, health path /health). Attached to the API service; no ALB/listener/target-group resource is created or modified here."
  type        = string
}

# --- Data-tier boundaries (data_sql/data_cache -> ecs) -----------------------------
variable "rds_security_group_id" {
  description = "Rule-free PostgreSQL security-group id from the data_sql module. This module authors the three locked TCP 5432 ingress rules (from the api, worker, AND migration task SGs — §26.3) plus the matching task-SG egress rules."
  type        = string
}

variable "redis_security_group_id" {
  description = "Rule-free Redis security-group id from the data_cache module. This module authors the two locked TCP 6379 ingress rules (from the api and worker task SGs ONLY — the migration task is Redis-excluded, §26.3) plus the matching task-SG egress rules."
  type        = string
}

# --- Image contract (registry -> ecs; §26.5) ---------------------------------------
variable "repository_urls" {
  description = "Map of logical repository key (api|worker) -> ECR repository URL, from the registry module output of the same name. Combined with the immutable digests below to pin task-definition images. Exactly two application images exist; the migration task reuses the worker image."
  type        = map(string)

  validation {
    condition     = contains(keys(var.repository_urls), "api") && contains(keys(var.repository_urls), "worker")
    error_message = "repository_urls must contain the \"api\" and \"worker\" keys (the fixed two-image contract, §26.5)."
  }
}

variable "api_image_digest" {
  description = "Immutable sha256 digest of the verified API image (\"sha256:<64 hex>\"). Digest-pinned per §26.5 — no mutable tag and no \"latest\" is accepted."
  type        = string

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.api_image_digest))
    error_message = "api_image_digest must be an immutable digest of the exact form sha256:<64 lowercase hex chars>; mutable tags (including latest) are rejected (§26.5)."
  }
}

variable "worker_image_digest" {
  description = "Immutable sha256 digest of the verified worker image (\"sha256:<64 hex>\"). Also used by the one-shot migration task definition with the locked command override (§26.5); no third image exists."
  type        = string

  validation {
    condition     = can(regex("^sha256:[0-9a-f]{64}$", var.worker_image_digest))
    error_message = "worker_image_digest must be an immutable digest of the exact form sha256:<64 lowercase hex chars>; mutable tags (including latest) are rejected (§26.5)."
  }
}

# --- IAM boundary (iam -> ecs; §26.8) ----------------------------------------------
variable "execution_role_arn" {
  description = "ARN of the shared ECS task execution role from the iam module. Used by all three task definitions; owns ECR pull, log delivery, and secret injection."
  type        = string
}

variable "api_task_role_arn" {
  description = "ARN of the API application task role from the iam module."
  type        = string
}

variable "worker_task_role_arn" {
  description = "ARN of the worker application task role from the iam module."
  type        = string
}

variable "migration_task_role_arn" {
  description = "ARN of the intentionally empty migration task role from the iam module."
  type        = string
}

# --- Secret containers (secrets -> ecs; §26.6/§26.7) -------------------------------
variable "secret_arns" {
  description = "Map of the four logical secret keys (SECRET_KEY/DATABASE_URL/REDIS_URL/LLM_API_KEY) -> Secrets Manager container ARN, from the secrets module output of the same name. This module injects the locked per-workload SUBSETS as task-definition `secrets` valueFrom references (api/worker: all four; migration: three — REDIS_URL excluded, §26.7). References only — never secret values."
  type        = map(string)

  validation {
    condition = alltrue([
      for k in ["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY"] :
      contains(keys(var.secret_arns), k)
    ])
    error_message = "secret_arns must contain the four locked keys SECRET_KEY, DATABASE_URL, REDIS_URL, and LLM_API_KEY (§26.7)."
  }

  validation {
    condition     = alltrue([for a in values(var.secret_arns) : can(regex("^arn:[^:]+:secretsmanager:", a))])
    error_message = "Every secret_arns value must be a Secrets Manager ARN (arn:<partition>:secretsmanager:...), never a secret value."
  }
}

# --- Ordinary (non-secret) environment (§26.11) ------------------------------------
# The denylist precondition below is MANDATED by §26.11: secret-bearing names must
# never enter plaintext env. S3_BUCKET/S3_REGION/ENVIRONMENT/APP_MODE etc. arrive
# through these maps at composition time.
variable "api_environment" {
  description = "Ordinary non-secret environment for the API container (name -> value). Only values that must differ from safe application defaults (§26.11). Secret-bearing names are rejected."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k in keys(var.api_environment) :
      !contains(["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"], k)
    ])
    error_message = "api_environment must not contain secret-bearing names (SECRET_KEY, DATABASE_URL, REDIS_URL, LLM_API_KEY, S3_ACCESS_KEY_ID, S3_SECRET_ACCESS_KEY, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_SESSION_TOKEN) — §26.11 denylist."
  }
}

variable "worker_environment" {
  description = "Ordinary non-secret environment for the worker container (name -> value). Same §26.11 denylist as api_environment."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k in keys(var.worker_environment) :
      !contains(["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"], k)
    ])
    error_message = "worker_environment must not contain secret-bearing names — §26.11 denylist."
  }
}

variable "migration_environment" {
  description = "Ordinary non-secret environment for the one-shot migration container (name -> value). Must pin the non-Redis backends (QUEUE_BACKEND/CACHE_BACKEND, §26.3) at composition time. Same §26.11 denylist; REDIS_URL is additionally never injected as a secret for this workload."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for k in keys(var.migration_environment) :
      !contains(["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY", "S3_ACCESS_KEY_ID", "S3_SECRET_ACCESS_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"], k)
    ])
    error_message = "migration_environment must not contain secret-bearing names — §26.11 denylist."
  }
}

# --- Sizing and deployment (locked staging baseline, §26.10) -----------------------
variable "task_cpu" {
  description = "Fargate task CPU units for every workload (locked staging baseline 256)."
  type        = number
  default     = 256

  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.task_cpu)
    error_message = "task_cpu must be a valid Fargate CPU value (256, 512, 1024, 2048, or 4096)."
  }
}

variable "task_memory" {
  description = "Fargate task memory (MiB) for every workload (locked staging baseline 512). Must be a valid Fargate pairing with task_cpu."
  type        = number
  default     = 512

  validation {
    condition     = var.task_memory >= 512 && var.task_memory <= 30720
    error_message = "task_memory must be between 512 and 30720 MiB (Fargate bounds)."
  }
}

variable "api_desired_count" {
  description = "Desired count of the API service (locked staging baseline 1)."
  type        = number
  default     = 1

  validation {
    condition     = var.api_desired_count >= 0
    error_message = "api_desired_count must be >= 0."
  }
}

variable "worker_desired_count" {
  description = "Desired count of the worker service (locked staging baseline 1)."
  type        = number
  default     = 1

  validation {
    condition     = var.worker_desired_count >= 0
    error_message = "worker_desired_count must be >= 0."
  }
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention for the three ecs-owned workload log groups (locked staging baseline 30 days, §26.9)."
  type        = number
  default     = 30

  validation {
    condition     = contains([1, 3, 5, 7, 14, 30, 60, 90, 120, 150, 180, 365], var.log_retention_days)
    error_message = "log_retention_days must be a CloudWatch-supported retention value."
  }
}

# --- Graceful shutdown (locked to application behavior, §26.10) --------------------
variable "worker_shutdown_grace_seconds" {
  description = "The application worker's in-flight-job drain grace (mirrors apps/api/app/core/config.py worker_shutdown_grace_seconds, default 10.0). The worker container stopTimeout must be >= this value (enforced by precondition)."
  type        = number
  default     = 10

  validation {
    condition     = var.worker_shutdown_grace_seconds >= 0 && var.worker_shutdown_grace_seconds <= 120
    error_message = "worker_shutdown_grace_seconds must be between 0 and 120 (ECS stopTimeout ceiling)."
  }
}

variable "worker_stop_timeout_seconds" {
  description = "SIGTERM->SIGKILL window for the worker container. Must be >= worker_shutdown_grace_seconds so the exec-form PID 1 can drain in-flight jobs (§26.10); <= 120 (Fargate ceiling)."
  type        = number
  default     = 30

  validation {
    condition     = var.worker_stop_timeout_seconds >= 1 && var.worker_stop_timeout_seconds <= 120
    error_message = "worker_stop_timeout_seconds must be between 1 and 120 seconds (Fargate ceiling)."
  }
}

variable "api_stop_timeout_seconds" {
  description = "SIGTERM->SIGKILL window for the API container. Sized to accommodate the ALB 60s deregistration delay (§26.10); <= 120 (Fargate ceiling)."
  type        = number
  default     = 70

  validation {
    condition     = var.api_stop_timeout_seconds >= 60 && var.api_stop_timeout_seconds <= 120
    error_message = "api_stop_timeout_seconds must be between 60 (ALB deregistration delay, §26.10) and 120 seconds (Fargate ceiling)."
  }
}
