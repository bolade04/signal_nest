# variables.tf — typed inputs for the staging IAM identity plane
#
# Inputs are configuration REFERENCES produced by sibling modules (`secrets`,
# `registry`, `storage`) plus the deterministic root `name_prefix`
# (aws-staging-iac-plan.md §26.8, §26.15). No secret value, account id, region,
# password, or real ARN is committed here — ARN-typed inputs are wired from
# module outputs at composition time, never from literals. Per §26.8 there is
# deliberately NO `log_group_arns` input (the execution-role Logs policy is
# scoped to a deterministic name prefix, breaking `iam -> ecs -> iam`) and NO
# `data_sql`/`data_cache` input (RDS/Redis socket access is a network/credential
# matter, not an IAM permission). No `tags` input (root provider `default_tags`).

variable "name_prefix" {
  description = "Deterministic, lowercase resource name prefix from the root (e.g. \"signalnest-staging\"). Builds the four role names and the deterministic /ecs/<name_prefix>-* CloudWatch Logs policy scope. Contains no account id, credential, region, endpoint, or ARN."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,61}[a-z0-9]$", var.name_prefix))
    error_message = "name_prefix must be lowercase alphanumeric and hyphens, 2-63 chars, no leading/trailing hyphen."
  }

  validation {
    condition     = length(var.name_prefix) <= 48
    error_message = "name_prefix must be at most 48 characters so every derived IAM role name (longest suffix: -migration-task) stays within the 64-character IAM role-name limit."
  }
}

variable "secret_arns" {
  description = "Map of the four logical secret keys (SECRET_KEY/DATABASE_URL/REDIS_URL/LLM_API_KEY) -> Secrets Manager container ARN, from the secrets module output of the same name. Scopes the execution role's secretsmanager:GetSecretValue to exactly the referenced containers. References only — never secret values."
  type        = map(string)

  validation {
    condition     = length(var.secret_arns) > 0 && alltrue([for a in values(var.secret_arns) : can(regex("^arn:[^:]+:secretsmanager:", a))])
    error_message = "secret_arns must be a non-empty map of Secrets Manager ARNs (arn:<partition>:secretsmanager:...)."
  }
}

variable "kms_key_arn" {
  description = "ARN of the customer-managed KMS key that encrypts the four secret containers, from the secrets module `kms_key_arn` output. Scopes the execution role's kms:Decrypt to exactly this key (via Secrets Manager only)."
  type        = string

  validation {
    condition     = can(regex("^arn:[^:]+:kms:", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:<partition>:kms:...)."
  }
}

variable "bucket_arn" {
  description = "ARN of the single private application S3 bucket, from the storage module `bucket_arn` output (singular — storage exposes exactly one bucket; §26.15). Scopes the API and worker task-role S3 policies."
  type        = string

  validation {
    condition     = can(regex("^arn:[^:]+:s3:::", var.bucket_arn))
    error_message = "bucket_arn must be an S3 bucket ARN (arn:<partition>:s3:::<bucket>)."
  }
}

variable "repository_arns" {
  description = "Map of logical repository key (api|worker) -> ECR repository ARN, from the registry module output of the same name. Scopes the execution role's image-pull permissions to exactly the two application repositories (and the CI publisher role's push permissions to the same two)."
  type        = map(string)

  validation {
    condition     = length(var.repository_arns) > 0 && alltrue([for a in values(var.repository_arns) : can(regex("^arn:[^:]+:ecr:", a))])
    error_message = "repository_arns must be a non-empty map of ECR repository ARNs (arn:<partition>:ecr:...)."
  }
}

# CI image-publisher role trust (staging-publish-workflow.md §4). The account-wide
# GitHub Actions OIDC provider is CONSUMED, never created here — it is a shared
# account resource whose ownership this module does not establish, so it remains
# an external prerequisite. Null (default) leaves the publisher role UNCREATED so
# offline validation passes with no real ARN committed.
variable "github_oidc_provider_arn" {
  description = "ARN of the pre-existing GitHub Actions OIDC provider (token.actions.githubusercontent.com) used by the CI image-publisher role trust policy. Null leaves the publisher role uncreated. Consumed, never created; no real ARN is committed."
  type        = string
  default     = null

  validation {
    condition     = var.github_oidc_provider_arn == null || can(regex("^arn:aws[a-zA-Z-]*:iam::[0-9]{12}:oidc-provider/token\\.actions\\.githubusercontent\\.com$", var.github_oidc_provider_arn))
    error_message = "github_oidc_provider_arn, when set, must be a GitHub Actions OIDC provider ARN (arn:aws:iam::<account>:oidc-provider/token.actions.githubusercontent.com)."
  }
}
