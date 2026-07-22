# main.tf — application object-storage plane (INFRA-4 storage module)
#
# Owns exactly ONE private, durable S3 bucket for SIGNALNEST_STAGING application
# object storage (uploads/artifacts; presigned-URL access at runtime). The bucket is
# private: all four public-access-block controls are enabled, object ownership is
# bucket-owner-enforced (ACLs disabled), server-side encryption uses S3-managed keys
# (SSE-S3/AES256, no KMS dependency), versioning is enabled, and a bucket policy
# denies any request not made over TLS.
#
# This module performs ONLY declarative bucket creation. It creates no IAM user,
# role, policy, access key, or credential; uploads no object or folder marker;
# stores no secret value; and declares no ACL resource. Successful creation does NOT
# mean the application is wired to S3 or that S3-backed storage is active.
#
# Dependency boundary (aws-staging-iac-plan.md §26): ZERO upstream module
# dependencies. Downstream only, one-way: `storage -> iam` — the future `iam` module
# consumes this bucket's ARN to scope the task-role S3 identity policy. This module
# consumes no `iam` output (access is granted by the IAM identity policy, not by a
# bucket policy that references a role), so no storage<->iam cycle is created. The
# future ECS/root-composition tranche passes the bucket name to the application.
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

# Server-side encryption at rest (SSE-S3/AES256; no committed KMS key reference).
resource "aws_s3_bucket_server_side_encryption_configuration" "this" {
  bucket = aws_s3_bucket.this.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
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
