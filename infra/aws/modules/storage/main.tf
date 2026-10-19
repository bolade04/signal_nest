# main.tf — application object-storage plane (INFRA-4 storage module)
#
# Owns exactly ONE private, durable S3 bucket for SIGNALNEST_STAGING application
# object storage (uploads/artifacts; presigned-URL access at runtime). The bucket is
# private: all four public-access-block controls are enabled, object ownership is
# bucket-owner-enforced (ACLs disabled), server-side encryption is SSE-KMS with the
# environment's SECRETS customer-managed key (P6-CI-6, below), versioning is enabled,
# and a bucket policy denies any request not made over TLS.
#
# This module performs ONLY declarative bucket creation. It creates no IAM user,
# role, policy, access key, or credential; uploads no object or folder marker;
# stores no secret value; and declares no ACL resource. Successful creation does NOT
# mean the application is wired to S3 or that S3-backed storage is active.
#
# Dependency boundary (aws-staging-iac-plan.md §26): ONE upstream input since P6-CI-6 —
# `secrets -> storage` (kms_key_arn, the secrets CMK; operator decision 2026-10-18,
# see the encryption section below); until then ZERO upstream module dependencies.
# Downstream only, one-way: `storage -> iam` — the `iam` module consumes this bucket's
# ARN to scope the task-role S3 identity policy. This module consumes no `iam` output
# (access is granted by the IAM identity policy, not by a bucket policy that references
# a role), so no storage<->iam cycle is created; `secrets` consumes nothing, so the
# new edge adds no cycle either. The root composition passes the bucket name to the
# application.
#
# No `provider "aws"` block is declared; `versions.tf` declares only the provider
# SOURCE (no version constraint) per the network/edge/alb/secrets/registry
# convention, so the root owns the sole provider config, version constraint, and
# committed lockfile, inherited here. The authoritative common tag set is applied by
# the root provider's `default_tags`; this module adds the conventional per-resource
# `Name` tag and merges any caller-supplied `tags`. No secret value, account id, ARN,
# region, or bucket name is committed.

# --- One private application bucket -----------------------------------------------
resource "aws_s3_bucket" "this" {
  bucket        = var.bucket_name
  force_destroy = var.force_destroy

  tags = merge(var.tags, {
    Name = var.bucket_name
  })
}

# ACLs disabled; the bucket owner owns every object. Access is by the ECS task-role
# credential chain (no ACLs, no access keys).
resource "aws_s3_bucket_ownership_controls" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Block all public access — this is a private application bucket.
resource "aws_s3_bucket_public_access_block" "this" {
  bucket = aws_s3_bucket.this.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Server-side encryption at rest: SSE-KMS with the secrets CMK (P6-CI-6, IaC finding
# AWS-0132; was SSE-S3/AES256). WHY THE SECRETS KEY AND NOT A DEDICATED DATA KEY
# (operator decision 2026-10-18): the live role permissions boundary denies the bounded
# api/worker task roles every KMS data-key operation outside the secrets CMK
# (scripts/gen_boundary_policy.py, DenyKmsUseOutsideSecretsCmk), so a new key would make
# every application read and write fail at runtime. The task roles reach the key ONLY
# through S3 for this bucket (iam module: kms:ViaService = s3 AND an encryption context
# equal to this bucket's ARN). The S3 Bucket Key must stay ON: with it, S3 uses the
# BUCKET ARN as the encryption context, which is exactly what that grant requires —
# turning it off would fail closed (every object operation denied), not open.
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = var.kms_key_arn
    }
    bucket_key_enabled = true
  }
}

# Versioning enabled so overwritten/deleted objects retain recoverable history.
resource "aws_s3_bucket_versioning" "this" {
  bucket = aws_s3_bucket.this.id

  versioning_configuration {
    status = "Enabled"
  }
}

# --- Bucket policy: deny any request not sent over TLS ----------------------------
# A blanket transport guard; it references no IAM role/principal (application access
# is granted by the future iam module's identity policy, not here), so it introduces
# no storage<->iam dependency.
resource "aws_s3_bucket_policy" "this" {
  bucket = aws_s3_bucket.this.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyNonTLSTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.this.arn,
          "${aws_s3_bucket.this.arn}/*",
        ]
        Condition = {
          Bool = {
            "aws:SecureTransport" = "false"
          }
        }
      }
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.this]
}
