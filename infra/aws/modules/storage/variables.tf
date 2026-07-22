# variables.tf — typed inputs for the staging application-storage module
#
# Semantic, root-provided configuration only. This module has ZERO upstream module
# dependencies: it consumes no other module output, no role/registry/account id, no
# KMS ARN, and no credential. Access permissions are owned downstream by the future
# `iam` module (producer -> consumer: storage -> iam), never by this module. All
# validation is STATIC (regex/range) — none queries AWS.

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
