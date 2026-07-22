# backend.tf — remote state backend declaration (INFRA-4 skeleton, no values)
#
# The design targets an S3 remote state backend with SSE-KMS encryption,
# versioning, blocked public access, and DynamoDB-based state locking
# (aws-staging-iac-plan.md §7).
#
# All backend configuration values — bucket, key, region, KMS key id, DynamoDB
# lock table, role ARN, credentials, and workspace prefix — are INTENTIONALLY
# OMITTED here. They are supplied only during a separately authorized
# remote-state bootstrap / CI-OIDC tranche via partial-backend configuration
# (`-backend-config`), never committed to this repository.
#
# This backend has NOT been initialized. No state bucket or lock table exists,
# and no `tofu init` was run in this tranche.

terraform {
  backend "s3" {
    # Deliberately empty. Do not add bucket/key/region/kms_key_id/
    # dynamodb_table/role_arn literals here. Supply them at bootstrap time via
    # partial backend configuration under separate authorization.
  }
}
