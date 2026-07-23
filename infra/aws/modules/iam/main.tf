# main.tf — staging IAM identity plane (INFRA-4 iam module)
#
# Owns the FOUR ECS-consumed IAM roles locked by aws-staging-iac-plan.md §26.8:
# one shared ECS task EXECUTION role plus three distinct APPLICATION task roles
# (API, worker, migration). `ecs` later consumes all four ARNs. No other role is
# created here: the CI-OIDC deployment role (trust via GitHub OIDC) is INFRA-5,
# and the operator/observer/break-glass human roles remain a later, separately
# designed tranche (their trust boundaries are not yet decided).
#
# CYCLE BREAK (§26.8): the execution-role CloudWatch Logs policy is scoped to the
# DETERMINISTIC name prefix /ecs/<name_prefix>-* built from `name_prefix` — this
# module never consumes an ECS or observability log-group output, so
# `iam -> ecs -> observability` stays one-way and acyclic. The account/region/
# partition segments of that ARN come from data sources resolved at plan time,
# never from committed literals; offline `tofu validate` does not contact AWS
# (data sources are not read during validate).
#
# LEAST PRIVILEGE (§26.8, runtime-contract §E):
# - Execution role: ECR image retrieval (scoped to the two application
#   repositories), CloudWatch Logs stream creation + delivery (prefix-scoped; NO
#   logs:CreateLogGroup — `ecs` owns the log groups and awslogs-create-group is
#   disabled), secretsmanager:GetSecretValue on exactly the four referenced
#   container ARNs, and kms:Decrypt on exactly the secrets CMK via Secrets
#   Manager. The single documented `Resource: "*"` exception is
#   ecr:GetAuthorizationToken, which AWS supports only at "*".
# - API/worker task roles: only the S3 calls the application code actually makes
#   (apps/api/app/infra/storage.py: put_object/get_object/head_object/
#   delete_object/head_bucket/presigned get_object), scoped to the single
#   application bucket. No RDS/Redis IAM permission (socket access is a
#   network/credential matter), no ECR/Logs-driver/secret-injection permission.
# - Migration task role: EMPTY (no attached policy) — migration code calls no
#   AWS API; DB access is network+credential, and its secrets are injected by
#   the execution role.
# Application containers never receive execution-role credentials.
#
# No `provider "aws"` block is declared; `versions.tf` declares only the provider
# SOURCE per the sibling-module convention. Tagging is the root provider's
# `default_tags`; this module adds only the conventional per-resource `Name` tag.
# No account id, secret value, credential, or real ARN is committed.

data "aws_partition" "current" {}
data "aws_region" "current" {}
data "aws_caller_identity" "current" {}

locals {
  # Deterministic log-group ARN prefix (§26.8): /ecs/<name_prefix>-* in the
  # deploying account/region. Matches the three ecs-owned groups
  # (/ecs/<name_prefix>-{api,worker,migration}) without consuming their outputs.
  log_group_arn_prefix = "arn:${data.aws_partition.current.partition}:logs:${data.aws_region.current.region}:${data.aws_caller_identity.current.account_id}:log-group:/ecs/${var.name_prefix}-*"

  # Trust policy shared by all four roles: only ECS tasks in THIS account may
  # assume them (aws:SourceAccount guards against the confused-deputy problem).
  ecs_tasks_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "EcsTasksAssume"
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })

  # The two application task roles that hold the S3 workload policy. The
  # migration role is deliberately absent (empty role, no policy).
  s3_workload_roles = {
    api    = aws_iam_role.api_task.id
    worker = aws_iam_role.worker_task.id
  }
}

# --- Shared ECS task execution role ----------------------------------------------
resource "aws_iam_role" "execution" {
  name               = "${var.name_prefix}-ecs-execution"
  description        = "Shared ECS task execution role for ${var.name_prefix}: ECR pull, prefix-scoped log delivery, referenced-secret retrieval. Application containers never receive these credentials."
  assume_role_policy = local.ecs_tasks_trust

  tags = {
    Name = "${var.name_prefix}-ecs-execution"
  }
}

resource "aws_iam_role_policy" "execution" {
  name = "${var.name_prefix}-ecs-execution"
  role = aws_iam_role.execution.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # AWS supports ecr:GetAuthorizationToken only at Resource "*" — the
        # smallest documented exception (§26.8), not a blanket wildcard.
        Sid      = "EcrAuthTokenOnly"
        Effect   = "Allow"
        Action   = ["ecr:GetAuthorizationToken"]
        Resource = "*"
      },
      {
        Sid    = "EcrPullApplicationImages"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ]
        Resource = sort(values(var.repository_arns))
      },
      {
        # Stream creation + event delivery into the ecs-owned deterministic
        # groups only. NO logs:CreateLogGroup: groups must pre-exist (§26.9).
        Sid    = "LogsDeliverToEcsPrefix"
        Effect = "Allow"
        Action = [
          "logs:CreateLogStream",
          "logs:PutLogEvents",
        ]
        Resource = [
          local.log_group_arn_prefix,
          "${local.log_group_arn_prefix}:*",
        ]
      },
      {
        Sid      = "SecretsReadReferencedContainers"
        Effect   = "Allow"
        Action   = ["secretsmanager:GetSecretValue"]
        Resource = sort(values(var.secret_arns))
      },
      {
        # Decrypt only the secrets CMK, and only when Secrets Manager in this
        # region is doing the decrypting on the role's behalf.
        Sid      = "KmsDecryptSecretsCmkViaSecretsManager"
        Effect   = "Allow"
        Action   = ["kms:Decrypt"]
        Resource = var.kms_key_arn
        Condition = {
          StringEquals = {
            "kms:ViaService" = "secretsmanager.${data.aws_region.current.region}.amazonaws.com"
          }
        }
      },
    ]
  })
}

# --- API task role ----------------------------------------------------------------
resource "aws_iam_role" "api_task" {
  name               = "${var.name_prefix}-api-task"
  description        = "API application task role for ${var.name_prefix}: application-bucket S3 access only. No secret, ECR, logs-driver, RDS, or Redis IAM permission."
  assume_role_policy = local.ecs_tasks_trust

  tags = {
    Name = "${var.name_prefix}-api-task"
  }
}

# --- Worker task role -------------------------------------------------------------
resource "aws_iam_role" "worker_task" {
  name               = "${var.name_prefix}-worker-task"
  description        = "Worker application task role for ${var.name_prefix}: application-bucket S3 access only. No secret, ECR, logs-driver, RDS, or Redis IAM permission."
  assume_role_policy = local.ecs_tasks_trust

  tags = {
    Name = "${var.name_prefix}-worker-task"
  }
}

# --- Migration task role (deliberately empty) -------------------------------------
# Migration code calls no AWS API (§26.8): database access is network+credential
# (execution-role secret injection), so this role exists only because ECS requires
# a task role per definition and the migration workload must not share the
# API/worker S3 grant. No aws_iam_role_policy is attached.
resource "aws_iam_role" "migration_task" {
  name               = "${var.name_prefix}-migration-task"
  description        = "Migration one-shot task role for ${var.name_prefix}: intentionally empty (no attached policy) — migration code calls no AWS API."
  assume_role_policy = local.ecs_tasks_trust

  tags = {
    Name = "${var.name_prefix}-migration-task"
  }
}

# --- Application-bucket S3 policy (API + worker only) ------------------------------
# Actions mirror the executable client exactly (apps/api/app/infra/storage.py):
# head_bucket -> s3:ListBucket on the bucket; put_object/get_object/head_object/
# delete_object and presigned get_object -> object-level Get/Put/Delete on
# bucket/*. No s3:* wildcard, no cross-bucket access, no ACL/policy mutation.
resource "aws_iam_role_policy" "app_s3" {
  for_each = local.s3_workload_roles

  name = "${var.name_prefix}-${each.key}-app-s3"
  role = each.value

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "AppBucketProbe"
        Effect   = "Allow"
        Action   = ["s3:ListBucket"]
        Resource = var.bucket_arn
      },
      {
        Sid    = "AppBucketObjects"
        Effect = "Allow"
        Action = [
          "s3:GetObject",
          "s3:PutObject",
          "s3:DeleteObject",
        ]
        Resource = "${var.bucket_arn}/*"
      },
    ]
  })
}
