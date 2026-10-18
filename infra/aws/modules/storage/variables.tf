# variables.tf — typed inputs for the staging application-storage module
#
# Semantic, root-provided configuration plus ONE producer output since P6-CI-6: the
# secrets CMK ARN (`kms_key_arn`, from `secrets`). It consumes no role/registry/account
# id and no credential. Access permissions are owned downstream by the `iam` module
# (producer -> consumer: storage -> iam), never by this module. All validation is
# STATIC (regex/range) — none queries AWS.

variable "bucket_name" {
  description = "Explicit, globally-unique lowercase S3 bucket name for the staging application object store. Supplied by the caller so a later, separately authorized root-composition tranche can set an environment-specific name. Contains no account id, credential, region, endpoint, or ARN."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$", var.bucket_name))
    error_message = "bucket_name must be 3-63 characters, lowercase letters/numbers/hyphens/dots only, and start and end with a letter or number (S3 bucket naming rules)."
  }

  validation {
    condition     = !can(regex("\\.\\.", var.bucket_name))
    error_message = "bucket_name must not contain two adjacent periods (S3 bucket naming rules)."
  }

  validation {
    condition     = !can(regex("(\\.-|-\\.)", var.bucket_name))
    error_message = "bucket_name must not contain a period adjacent to a hyphen (S3 bucket naming rules)."
  }

  validation {
    condition     = !can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+\\.[0-9]+$", var.bucket_name))
    error_message = "bucket_name must not be formatted as an IPv4 address (S3 bucket naming rules)."
  }
}

variable "force_destroy" {
  description = "When true, allows the bucket to be destroyed even if it still contains objects. Defaults to false so a non-empty staging bucket is never silently deleted; a later operational tranche may override it deliberately."
  type        = bool
  default     = false
}

variable "tags" {
  description = "Additional caller-provided resource tags merged onto the bucket. Defaults to an empty map. The authoritative common tag set is applied separately by the root provider default_tags; this input only supplements it and does not replace it."
  type        = map(string)
  default     = {}
}

variable "kms_key_arn" {
  description = "ARN of the secrets customer-managed KMS key (the secrets module's `kms_key_arn` output). The bucket's default encryption is SSE-KMS with this key (P6-CI-6): the live role permissions boundary permits the task roles KMS use on this key only. Required; no default."
  type        = string

  validation {
    condition     = can(regex("^arn:[^:]+:kms:[^:]+:[0-9]{12}:key/[0-9a-f-]+$", var.kms_key_arn))
    error_message = "kms_key_arn must be a KMS key ARN (arn:<partition>:kms:<region>:<account>:key/<key-id>), not an alias."
  }
}
