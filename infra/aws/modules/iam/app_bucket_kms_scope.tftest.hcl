# P6-CI-6 (IaC finding AWS-0132 on the application bucket, operator decision 2026-10-18) —
# the api/worker task roles' KMS grant for the SSE-KMS application bucket must be EXACTLY
# scoped.
#
# WHAT THIS PROVES, offline, with a fully mocked provider (no backend, no state, no AWS
# call): each rendered app_s3 policy is exactly the reviewed three statements — the two S3
# statements unchanged, plus ONE KMS statement granting kms:GenerateDataKey and kms:Decrypt
# on exactly the secrets CMK, conditioned on BOTH kms:ViaService = S3 in this region AND
# the S3 encryption context equal to the application bucket ARN (the context S3 uses when
# the bucket's S3 Bucket Key is on). Any widening — another action, another key, a dropped
# condition, a wildcard — changes the rendering and fails the equality. A second run proves
# no other policy in this module gained a KMS data-key grant.
#
# Why the secrets CMK: the live role permissions boundary (scripts/gen_boundary_policy.py,
# DenyKmsUseOutsideSecretsCmk) lets these bounded roles use no other key.

mock_provider "aws" {}

override_data {
  target = data.aws_caller_identity.current
  values = {
    account_id = "111122223333"
  }
}

override_data {
  target = data.aws_partition.current
  values = {
    partition = "aws"
  }
}

override_data {
  target = data.aws_region.current
  values = {
    region = "us-east-1"
    name   = "us-east-1"
  }
}

variables {
  name_prefix = "signalnest-staging"
  secret_arns = {
    DATABASE_URL = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/DATABASE_URL-AAAAAA"
    REDIS_URL    = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/REDIS_URL-BBBBBB"
    SECRET_KEY   = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/SECRET_KEY-CCCCCC"
    LLM_API_KEY  = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/LLM_API_KEY-DDDDDD"
  }
  kms_key_arn = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
  bucket_arn  = "arn:aws:s3:::signalnest-staging-app-testfixture"
  repository_arns = {
    api    = "arn:aws:ecr:us-east-1:111122223333:repository/signalnest-staging/api"
    worker = "arn:aws:ecr:us-east-1:111122223333:repository/signalnest-staging/worker"
  }
  role_boundary_mode            = "required"
  role_permissions_boundary_arn = "arn:aws:iam::111122223333:policy/signalnest-staging-role-boundary"
  github_oidc_provider_arn      = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"

  mail_sending_identity_domain = "mail.staging.example.com"
  mail_from_address            = "no-reply@mail.staging.example.com"
}

# --- Positive: both task-role policies render exactly the reviewed statements -------------
run "app_s3_policies_are_exactly_the_two_s3_statements_and_one_s3_scoped_kms_statement" {
  command = apply

  assert {
    condition = alltrue([
      for k in ["api", "worker"] :
      jsonencode(jsondecode(aws_iam_role_policy.app_s3[k].policy)) == jsonencode({
        Version = "2012-10-17"
        Statement = [
          {
            Sid      = "AppBucketProbe"
            Effect   = "Allow"
            Action   = ["s3:ListBucket"]
            Resource = "arn:aws:s3:::signalnest-staging-app-testfixture"
          },
          {
            Sid      = "AppBucketObjects"
            Effect   = "Allow"
            Action   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
            Resource = "arn:aws:s3:::signalnest-staging-app-testfixture/*"
          },
          {
            Sid      = "AppBucketKmsViaS3Only"
            Effect   = "Allow"
            Action   = ["kms:GenerateDataKey", "kms:Decrypt"]
            Resource = "arn:aws:kms:us-east-1:111122223333:key/00000000-0000-0000-0000-000000000000"
            Condition = {
              StringEquals = {
                "kms:ViaService"                   = "s3.us-east-1.amazonaws.com"
                "kms:EncryptionContext:aws:s3:arn" = "arn:aws:s3:::signalnest-staging-app-testfixture"
              }
            }
          },
        ]
      })
    ])
    error_message = "an app_s3 policy is not exactly the reviewed S3 statements plus the one S3-scoped KMS statement (GenerateDataKey + Decrypt on the secrets CMK, kms:ViaService = s3 AND the bucket encryption context)"
  }

  assert {
    condition     = toset(keys(aws_iam_role_policy.app_s3)) == toset(["api", "worker"])
    error_message = "the S3 + KMS workload grant must exist for exactly the api and worker task roles (never migration)"
  }
}

# --- Negative: no other policy in this module gained a KMS data-key grant ------------------
run "no_other_module_policy_carries_a_kms_data_key_grant" {
  command = apply

  assert {
    condition     = !strcontains(aws_iam_role_policy.execution.policy, "kms:GenerateDataKey") && !strcontains(aws_iam_role_policy.api_ses_send.policy, "kms:") && !strcontains(aws_iam_role_policy.ci_publisher[0].policy, "kms:")
    error_message = "only the api/worker app_s3 policies may grant kms:GenerateDataKey; the execution role keeps its Secrets-Manager-only kms:Decrypt, and the SES and publisher policies carry no KMS action"
  }
}
