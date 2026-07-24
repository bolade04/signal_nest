# main.tf — one-time OpenTofu remote-state backend bootstrap (CONFIGURATION ONLY)
#
# Authors exactly the state-backend objects fixed by aws-staging-iac-plan.md §7:
# one SSE-KMS-encrypted, versioned, public-access-blocked S3 state bucket, its
# dedicated customer-managed KMS key (+ alias), and one DynamoDB lock table.
# The plan's §7 parenthetical also permits "the tool-native equivalent" (the S3
# backend's `use_lockfile` conditional-write locking, supported by the pinned
# OpenTofu range); the DynamoDB table is authored here because DynamoDB is the
# mechanism §7 names primarily — the live-bootstrap authorizer may revisit that
# choice before executing.
#
# EXECUTION IS NOT AUTHORIZED BY THIS FILE'S EXISTENCE. Per §7 "the state
# backend itself is provisioned once under a later authorized step"; the repo's
# status ledger (infra/aws/README.md §12) bundles that live bootstrap with the
# INFRA-9 fresh-authorization gate. This tranche validated the configuration
# OFFLINE ONLY (`fmt`, `init -backend=false`, `validate`) — no plan, no apply,
# no AWS API call, and nothing exists in AWS.
#
# This root keeps its own one-time state LOCALLY (no backend block): the bucket
# it creates cannot host its creator's state before existing. State access for
# the MAIN root is least-privilege and CI-OIDC-scoped per §7; this bootstrap
# root's tiny local state (bucket/table/key identifiers only, no secrets) is
# retained by the operator and never committed (*.tfstate* is git-ignored).

# --- Customer-managed KMS key for the state bucket (SSE-KMS, §7) --------------------
# Symmetric ENCRYPT_DECRYPT, rotation on, single-region, recoverable delete. No
# `policy` is declared: AWS applies its default key policy (account-root
# administrative access, delegating to IAM) — the same deliberate delegation the
# secrets module documents. State access control lives on the CI-OIDC role
# (INFRA-5), not in this key policy.
resource "aws_kms_key" "state" {
  description                        = "SignalNest ${local.name_prefix} OpenTofu state CMK — encrypts the remote-state S3 bucket."
  key_usage                          = "ENCRYPT_DECRYPT"
  customer_master_key_spec           = "SYMMETRIC_DEFAULT"
  enable_key_rotation                = true
  multi_region                       = false
  is_enabled                         = true
  deletion_window_in_days            = var.kms_deletion_window_in_days
  bypass_policy_lockout_safety_check = false

  tags = {
    Name = "${local.name_prefix}-state-cmk"
  }
}

# Deterministic alias for the state CMK.
resource "aws_kms_alias" "state" {
  name          = "alias/${local.name_prefix}/state"
  target_key_id = aws_kms_key.state.key_id
}

# --- Remote-state S3 bucket (SSE-KMS, versioned, private, §7) -----------------------
# The globally unique name is caller-supplied (git-ignored *.tfvars) — never
# committed. State contains sensitive resolved attributes, so the bucket is
# private, TLS-only, versioned (state history/recovery), and CMK-encrypted.
resource "aws_s3_bucket" "state" {
  bucket        = var.state_bucket_name
  force_destroy = false

  tags = {
    Name = "${local.name_prefix}-state"
  }
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket = aws_s3_bucket.state.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# SSE-KMS with the dedicated CMK (§7). `bucket_key_enabled` is meaningful under
# SSE-KMS (unlike SSE-S3) and reduces per-object KMS request cost.
resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.state.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Deny-only policy: reject any non-TLS request (encryption in transit, §7).
# Access ALLOWS are identity-based (the CI-OIDC deployment role, INFRA-5) —
# no principal is granted anything here.
resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "DenyInsecureTransport"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource = [
          aws_s3_bucket.state.arn,
          "${aws_s3_bucket.state.arn}/*",
        ]
        Condition = {
          Bool = { "aws:SecureTransport" = "false" }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.state]
}

# --- DynamoDB state-lock table (§7) -------------------------------------------------
# The S3 backend's lock schema: a single string hash key named exactly "LockID".
# On-demand billing (lock traffic is tiny and bursty); server-side encryption
# with the same CMK. Lock rows hold coordination metadata only — never state.
resource "aws_dynamodb_table" "lock" {
  name         = var.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  server_side_encryption {
    enabled     = true
    kms_key_arn = aws_kms_key.state.arn
  }

  tags = {
    Name = "${local.name_prefix}-state-lock"
  }
}
