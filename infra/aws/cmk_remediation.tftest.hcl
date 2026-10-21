# P6-CI-6 customer-managed-key remediations (operator order 2026-10-18, IaC option (a)) —
# offline, fully mocked provider, no backend, no AWS call. Each run targets ONE module
# directly (a `module` block), so the assertions can read that module's resources; the
# root suite (input_boundary.tftest.hcl) cannot address module internals.
#
# WHAT THIS PROVES (the Trivy IaC gate proves only that a customer-managed key is SET):
#   D1 edge         — the SPA bucket is SSE-KMS with the module's own key, the Bucket Key is
#                     on, and the key policy is EXACTLY the account-root delegation statement
#                     plus kms:Decrypt for the CloudFront service principal bound to THIS
#                     distribution (AWS:SourceArn). No encrypt/data-key grant to CloudFront.
#   D2+D3 audit     — the trail and the audit bucket default both use the module's audit
#                     key; the key policy is EXACTLY the root statement plus the two
#                     CloudTrail statements AWS documents, each bound to THIS trail; the
#                     Bucket Key is OFF (with it, CloudTrail would also need kms:Decrypt).
#   D4 storage      — the application bucket is SSE-KMS with the SUPPLIED secrets CMK ARN
#                     and the Bucket Key is ON (the task-role grant in the iam module keys
#                     on the bucket-ARN encryption context that only the Bucket Key yields);
#                     an alias ARN is refused before any plan.
# Every key: rotation on, single-region, 30-day deletion window, lockout safety check on.

mock_provider "aws" {
  mock_data "aws_caller_identity" {
    defaults = {
      account_id = "111122223333"
    }
  }
  mock_data "aws_partition" {
    defaults = {
      partition = "aws"
    }
  }
  mock_data "aws_region" {
    defaults = {
      region = "us-east-1"
      name   = "us-east-1"
    }
  }
  mock_resource "aws_kms_key" {
    defaults = {
      arn    = "arn:aws:kms:us-east-1:111122223333:key/11111111-1111-1111-1111-111111111111"
      key_id = "11111111-1111-1111-1111-111111111111"
    }
  }
  mock_resource "aws_cloudfront_distribution" {
    defaults = {
      arn            = "arn:aws:cloudfront::111122223333:distribution/EDFDVBD6EXAMPLE"
      domain_name    = "d111111abcdef8.cloudfront.net"
      hosted_zone_id = "ZSYNTHCFCANONICAL00"
    }
  }
  mock_resource "aws_s3_bucket" {
    defaults = {
      id                          = "signalnest-staging-mock-bucket"
      arn                         = "arn:aws:s3:::signalnest-staging-mock-bucket"
      bucket_regional_domain_name = "signalnest-staging-mock-bucket.s3.us-east-1.amazonaws.com"
    }
  }
  # P6-INF-4 (mock only, no assertion changed): the observability module's alarms now name its
  # alarm topic in their actions, which the provider validates as an ARN.
  mock_resource "aws_sns_topic" {
    defaults = {
      arn = "arn:aws:sns:us-east-1:111122223333:signalnest-staging-alarms"
    }
  }
}

# --- D1: SPA origin bucket (modules/edge) ----------------------------------------------
run "spa_bucket_uses_its_own_cmk_and_only_this_distribution_may_decrypt" {
  command = apply

  module {
    source = "./modules/edge"
  }

  variables {
    name_prefix         = "signalnest-staging"
    web_fqdn            = "app.staging.example.com"
    hosted_zone_id      = "ZSYNTHETICZONE00001"
    acm_certificate_arn = "arn:aws:acm:us-east-1:111122223333:certificate/00000000-0000-0000-0000-000000000000"
  }

  assert {
    condition = jsonencode(jsondecode(aws_kms_key.spa.policy)) == jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid       = "EnableIamPoliciesInThisAccount"
          Effect    = "Allow"
          Principal = { AWS = "arn:aws:iam::111122223333:root" }
          Action    = "kms:*"
          Resource  = "*"
        },
        {
          Sid       = "AllowCloudFrontOacDecryptForThisDistributionOnly"
          Effect    = "Allow"
          Principal = { Service = "cloudfront.amazonaws.com" }
          Action    = "kms:Decrypt"
          Resource  = "*"
          Condition = {
            StringEquals = { "AWS:SourceArn" = "arn:aws:cloudfront::111122223333:distribution/EDFDVBD6EXAMPLE" }
          }
        },
      ]
    })
    error_message = "the SPA key policy is not exactly the account-root statement plus kms:Decrypt for this CloudFront distribution only"
  }

  assert {
    condition = [
      for r in aws_s3_bucket_server_side_encryption_configuration.spa.rule : [
        r.apply_server_side_encryption_by_default[0].sse_algorithm,
        r.apply_server_side_encryption_by_default[0].kms_master_key_id,
        tostring(r.bucket_key_enabled),
      ]
    ] == [["aws:kms", aws_kms_key.spa.arn, "true"]]
    error_message = "the SPA bucket must be SSE-KMS with the module's SPA key and the S3 Bucket Key on"
  }

  assert {
    condition     = aws_kms_key.spa.enable_key_rotation && !aws_kms_key.spa.multi_region && aws_kms_key.spa.deletion_window_in_days == 30 && !aws_kms_key.spa.bypass_policy_lockout_safety_check && aws_kms_key.spa.customer_master_key_spec == "SYMMETRIC_DEFAULT"
    error_message = "the SPA key must be symmetric, rotated, single-region, 30-day recoverable, with the lockout safety check on"
  }

  assert {
    condition     = aws_kms_alias.spa.name == "alias/signalnest-staging/spa" && aws_kms_alias.spa.target_key_id == aws_kms_key.spa.key_id
    error_message = "the SPA key alias must be alias/<name_prefix>/spa and target the SPA key"
  }
}

# --- D2 + D3: CloudTrail audit trail and audit bucket (modules/observability) ----------
run "audit_trail_and_bucket_use_the_audit_cmk_bound_to_this_trail" {
  command = apply

  module {
    source = "./modules/observability"
  }

  variables {
    name_prefix = "signalnest-staging"
    log_group_names = {
      api       = "/ecs/signalnest-staging-api"
      worker    = "/ecs/signalnest-staging-worker"
      migration = "/ecs/signalnest-staging-migration"
    }
    log_group_arns = {
      api       = "arn:aws:logs:us-east-1:111122223333:log-group:/ecs/signalnest-staging-api"
      worker    = "arn:aws:logs:us-east-1:111122223333:log-group:/ecs/signalnest-staging-worker"
      migration = "arn:aws:logs:us-east-1:111122223333:log-group:/ecs/signalnest-staging-migration"
    }
    api_service_name    = "signalnest-staging-api"
    worker_service_name = "signalnest-staging-worker"
    alarm_thresholds = {
      ecs_cpu_high_percent          = 80
      ecs_memory_high_percent       = 80
      log_error_count_per_period    = 5
      rds_cpu_high_percent          = 80
      rds_free_storage_low_bytes    = 1000000000
      rds_freeable_memory_low_bytes = 100000000
      redis_cpu_high_percent        = 80
      redis_memory_high_percent     = 80
    }
  }

  assert {
    condition = jsonencode(jsondecode(aws_kms_key.audit.policy)) == jsonencode({
      Version = "2012-10-17"
      Statement = [
        {
          Sid       = "EnableIamPoliciesInThisAccount"
          Effect    = "Allow"
          Principal = { AWS = "arn:aws:iam::111122223333:root" }
          Action    = "kms:*"
          Resource  = "*"
        },
        {
          Sid       = "AllowCloudTrailEncryptForThisTrailOnly"
          Effect    = "Allow"
          Principal = { Service = "cloudtrail.amazonaws.com" }
          Action    = "kms:GenerateDataKey*"
          Resource  = "*"
          Condition = {
            StringEquals = { "aws:SourceArn" = "arn:aws:cloudtrail:us-east-1:111122223333:trail/signalnest-staging-audit" }
            StringLike   = { "kms:EncryptionContext:aws:cloudtrail:arn" = "arn:aws:cloudtrail:*:111122223333:trail/*" }
          }
        },
        {
          Sid       = "AllowCloudTrailDescribeKeyForThisTrailOnly"
          Effect    = "Allow"
          Principal = { Service = "cloudtrail.amazonaws.com" }
          Action    = "kms:DescribeKey"
          Resource  = "*"
          Condition = {
            StringEquals = { "aws:SourceArn" = "arn:aws:cloudtrail:us-east-1:111122223333:trail/signalnest-staging-audit" }
          }
        },
      ]
    })
    error_message = "the audit key policy is not exactly the account-root statement plus the two CloudTrail statements bound to this trail"
  }

  assert {
    condition     = aws_cloudtrail.audit.kms_key_id == aws_kms_key.audit.arn && aws_cloudtrail.audit.enable_log_file_validation
    error_message = "the trail must encrypt its log and digest files with the audit key and keep log-file validation on"
  }

  assert {
    condition = [
      for r in aws_s3_bucket_server_side_encryption_configuration.audit.rule : [
        r.apply_server_side_encryption_by_default[0].sse_algorithm,
        r.apply_server_side_encryption_by_default[0].kms_master_key_id,
        tostring(r.bucket_key_enabled),
      ]
    ] == [["aws:kms", aws_kms_key.audit.arn, "false"]]
    error_message = "the audit bucket default must be SSE-KMS with the audit key and the S3 Bucket Key OFF"
  }

  assert {
    condition     = aws_kms_key.audit.enable_key_rotation && !aws_kms_key.audit.multi_region && aws_kms_key.audit.deletion_window_in_days == 30 && !aws_kms_key.audit.bypass_policy_lockout_safety_check && aws_kms_key.audit.customer_master_key_spec == "SYMMETRIC_DEFAULT"
    error_message = "the audit key must be symmetric, rotated, single-region, 30-day recoverable, with the lockout safety check on"
  }

  assert {
    condition     = aws_kms_alias.audit.name == "alias/signalnest-staging/audit" && aws_kms_alias.audit.target_key_id == aws_kms_key.audit.key_id
    error_message = "the audit key alias must be alias/<name_prefix>/audit and target the audit key"
  }
}

# --- D4: application bucket (modules/storage), encrypted with the SUPPLIED secrets CMK --
run "app_bucket_uses_the_supplied_secrets_cmk_with_the_bucket_key_on" {
  command = plan

  module {
    source = "./modules/storage"
  }

  variables {
    bucket_name = "signalnest-staging-app-testfixture"
    kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
  }

  assert {
    condition = [
      for r in aws_s3_bucket_server_side_encryption_configuration.this.rule : [
        r.apply_server_side_encryption_by_default[0].sse_algorithm,
        r.apply_server_side_encryption_by_default[0].kms_master_key_id,
        tostring(r.bucket_key_enabled),
      ]
    ] == [["aws:kms", "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000", "true"]]
    error_message = "the application bucket must be SSE-KMS with exactly the supplied secrets CMK and the S3 Bucket Key ON (the task-role grant's encryption-context condition depends on it)"
  }
}

run "app_bucket_refuses_an_alias_instead_of_a_key_arn" {
  command = plan

  module {
    source = "./modules/storage"
  }

  variables {
    bucket_name = "signalnest-staging-app-testfixture"
    kms_key_arn = "arn:aws:kms:us-east-1:111122223333:alias/signalnest-staging/secrets"
  }

  expect_failures = [var.kms_key_arn]
}
