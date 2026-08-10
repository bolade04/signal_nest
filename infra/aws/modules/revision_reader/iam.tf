
# THE derivation. Every role in this module reads local.effective_permissions_boundary, so a
# role cannot bypass the mode by consuming the ARN directly — and the boundary cannot vanish
# because an ARN happened to be null: in "required" mode a null ARN fails the precondition
# below instead of silently producing an unbounded role.
locals {
  boundary_enforced = var.role_boundary_mode == "required"

  effective_permissions_boundary = (
    local.boundary_enforced ? var.role_permissions_boundary_arn : null
  )

  # GATE 4N-I16 PHASE B. ONE authoritative state model. Every boundary guard in this module
  # classifies through this local, so no guard can key off a signal the resources do not read.
  #
  #   UNBOUNDED_DARK_STATE       mode "disabled" + ARN null. Legitimate ONLY while nothing
  #                              creates or updates a protected role.
  #   BOUNDARY_ENFORCED          mode "required" + non-null ARN. The only state in which a
  #                              protected-role bootstrap may run.
  #   INVALID_PARTIAL_BOOTSTRAP  every remaining combination. Named, not tolerated: an ARN
  #                              present under "disabled" mode is the exact shape that made
  #                              the Gate 4N-I15 guard report safety it was not measuring.
  boundary_state = (
    var.role_boundary_mode == "required" && var.role_permissions_boundary_arn != null
    ? "BOUNDARY_ENFORCED"
    : (var.role_boundary_mode == "disabled" && var.role_permissions_boundary_arn == null
      ? "UNBOUNDED_DARK_STATE"
    : "INVALID_PARTIAL_BOOTSTRAP")
  )

  # Stage A creates the publisher role and updates protected role state.
  protected_role_bootstrap = var.publication_bootstrap_enabled
}

# Fail at PLAN time, before any resource is touched. Gate 4N-I7 showed that a null ARN
# against bounded deployed roles plans REMOVAL with no error at all.
# GATE 4N-I16. THE THREE AXES ARE SEPARATE RESOURCES, not three preconditions on one.
#
# WHY. `expect_failures` names a RESOURCE, not a precondition. With all three axes on one
# resource, a test asserting "this configuration is rejected" passes when ANY axis fires —
# so a corrupted state classifier stayed green because the ceiling-identity axis happened to
# fail on the same input for an unrelated reason. The Phase E mutation harness caught exactly
# that. Separate resources make each failure attributable, which is what lets a mutation of
# one axis be distinguished from a mutation of another.

# AXIS 1 — the state itself must be coherent. This is the check Gate 4N-I15 lacked: it
# rejects mode "disabled" WITH a non-null ARN, which previously passed every guard.
resource "terraform_data" "boundary_state_coherence" {
  lifecycle {
    precondition {
      condition     = local.boundary_state != "INVALID_PARTIAL_BOOTSTRAP"
      error_message = "Incoherent boundary state. role_boundary_mode = \"required\" requires a non-null role_permissions_boundary_arn — a null ARN in required mode plans REMOVAL of the boundary from every deployed role. role_boundary_mode = \"disabled\" requires role_permissions_boundary_arn = null — a non-null ARN under disabled mode is NOT a boundary, because the roles consume the mode-derived value and would be created UNBOUNDED while the configuration reads as protected."
    }
  }
}

# AXIS 2 — identity of the ceiling.
#
# NOTE ON WHAT IS *NOT* HERE. An earlier draft of this gate added a third resource axis
# asserting "bootstrap requires BOUNDARY_ENFORCED". It was removed: the
# publication_bootstrap_enabled variable validation already encodes exactly that rule, and
# the Phase E harness proved the consequence — with the rule encoded twice, deleting either
# copy left the suite green, so neither copy could be shown to be load-bearing. A guard that
# cannot be falsified is not a second line of defence, it is unmeasured surface. The rule is
# now stated once, in the variable validation, where the mutation harness can reach it. A syntactically valid ARN naming some OTHER policy would
# attach the wrong ceiling, which is not distinguishable from the right one by shape.
resource "terraform_data" "boundary_mode_precondition" {
  lifecycle {
    # GATE 4N-I16: this axis owns the IDENTITY of the ceiling, not its PRESENCE. The
    # null case is deliberately excused here because it belongs to the coherence axis —
    # without the excusal, regex() on a null returns false and this axis fires for a
    # required+null configuration, making the two axes indistinguishable to expect_failures
    # and letting a corrupted state classifier hide behind this one.
    precondition {
      condition     = !local.boundary_enforced || var.role_permissions_boundary_arn == null || can(regex("policy/signalnest-staging-role-boundary$", var.role_permissions_boundary_arn))
      error_message = "role_permissions_boundary_arn must name the reviewed boundary policy signalnest-staging-role-boundary."
    }
  }
}

# =====================================================================================
# Gate 4J — reader IAM. Three purpose-built identities, none of them an existing role.
#
#   execution role  — what ECS uses to START the task (image pull, secret injection, logs)
#   publisher role  — what CI uses to PUSH the reader image (reader repository ONLY)
#   runner role     — what CI uses to RUN the reader (exact task-definition revision ONLY)
#
# Publisher and runner are DELIBERATELY separate identities pinned to DIFFERENT trust
# subjects. Conflating them would mean any job declaring the shared environment could
# assume either one: publish jobs could invoke tasks, and invocation jobs could push
# images — which would defeat the point of pinning the image by digest.
#
# WHAT IAM CAN AND CANNOT DO HERE (re-derived, not inherited). For an ecs:RunTask holder
# IAM constrains exactly three things: WHICH task-definition revision runs, role
# substitution (exact-ARN iam:PassRole), and ECS Exec at launch
# (ecs:enable-execute-command). It constrains NOTHING about the override payload,
# environment, subnets, securityGroups or assignPublicIp — no condition keys exist for
# them. That is precisely why the entryPoint control lives in the IMAGE, not here.
# =====================================================================================

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id

  ecs_tasks_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "ecs-tasks.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = data.aws_caller_identity.current.account_id }
      }
    }]
  })
}

# --- ECS task execution role ---------------------------------------------------------
resource "aws_iam_role" "reader_execution" {
  count = local.create_runtime

  permissions_boundary = local.effective_permissions_boundary
  name                 = "${var.name_prefix}-revision-reader-execution"
  description          = "Starts the revision-reader task: pull the reader image, inject DATABASE_URL, write reader logs. Nothing else."
  assume_role_policy   = local.ecs_tasks_trust
  tags                 = merge(var.tags, { Name = "${var.name_prefix}-revision-reader-execution" })
}

resource "aws_iam_role_policy" "reader_execution" {
  count = local.create_runtime

  name = "${var.name_prefix}-revision-reader-execution"
  role = aws_iam_role.reader_execution[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # AWS supports no resource scoping for this action. Documented exception — the
        # same one the pre-existing shared execution and publisher roles take.
        Sid      = "EcrAuthToken"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        # THE READER REPOSITORY ONLY — not the two-repository `repository_arns` set the
        # shared execution role holds. This role structurally cannot pull the api or
        # worker image, so it cannot start a container that contains Alembic.
        Sid    = "PullReaderImageOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:GetDownloadUrlForLayer",
          "ecr:BatchGetImage",
        ]
        Resource = [aws_ecr_repository.reader[0].arn]
      },
      {
        # EXACTLY the DATABASE_URL secret ARN. Not a wildcard, not the four-secret set.
        # HONEST LIMIT: this credential is a database OWNER (bootstrap_app_role.py issues
        # ALTER DATABASE ... OWNER TO), so read-only-ness is a property of the READER CODE
        # and the session settings it sets, never of the credential.
        Sid      = "InjectDatabaseUrlSecretOnly"
        Effect   = "Allow"
        Action   = "secretsmanager:GetSecretValue"
        Resource = [var.database_url_secret_arn]
      },
      {
        # Decrypt confined to decryption performed BY Secrets Manager on this role's
        # behalf. Without kms:ViaService this would be a general grant on the key.
        Sid      = "KmsDecryptViaSecretsManagerOnly"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = [var.secrets_kms_key_arn]
        Condition = {
          StringEquals = { "kms:ViaService" = "secretsmanager.${var.aws_region}.amazonaws.com" }
        }
      },
      {
        # The reader's OWN group only. NO logs:CreateLogGroup: this module creates the
        # group, so the group always pre-exists and granting creation would be unused
        # privilege that also lets a misconfigured task write outside the audited group.
        Sid      = "WriteReaderLogsOnly"
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = ["${aws_cloudwatch_log_group.reader[0].arn}:*"]
      },
      {
        # DEFENCE IN DEPTH (Gate 4J.1). `environmentFiles` is a caller-supplied
        # ContainerOverride channel that fetches an env file from S3 USING THIS EXECUTION
        # ROLE. The role holds no s3 grant, so it is already closed by ABSENCE — but a
        # future same-account bucket resource-policy could grant this role access without
        # any identity-policy allow. An explicit Deny makes the closure unconditional and
        # survives future widening: the reader mounts nothing from S3, ever.
        Sid      = "DenyS3EnvironmentFileFetch"
        Effect   = "Deny"
        Action   = "s3:GetObject"
        Resource = "*"
      },
    ]
  })
}

# THERE IS NO TASK ROLE. The reader makes no AWS API call of any kind. Omitting the role
# entirely (rather than attaching an empty one) is what lets the runner's PassRole grant
# be a single exact ARN — an empty task role would still have to be passable.

# --- GitHub OIDC trust ---------------------------------------------------------------
# StringEquals on BOTH aud and sub, never StringLike: a `sub` wildcard would let any
# branch or any environment in the repository assume these roles.
locals {
  publisher_sub = "repo:${var.github_repository}:environment:staging-reader-publish"
  runner_sub    = "repo:${var.github_repository}:environment:staging-reader-run"

  oidc_trust = {
    for k, sub in { publisher = local.publisher_sub, runner = local.runner_sub } :
    k => jsonencode({
      Version = "2012-10-17"
      Statement = [{
        Effect    = "Allow"
        Principal = { Federated = var.github_oidc_provider_arn }
        Action    = "sts:AssumeRoleWithWebIdentity"
        Condition = {
          StringEquals = {
            "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
            "token.actions.githubusercontent.com:sub" = sub
          }
        }
      }]
    })
  }
}

# --- reader publisher role: pushes the reader repository and nothing else ------------
resource "aws_iam_role" "reader_publisher" {
  count = local.create_oidc_publisher

  permissions_boundary = local.effective_permissions_boundary
  name                 = "${var.name_prefix}-revision-reader-publisher"
  description          = "CI identity that publishes the reader image. Scoped to the reader ECR repository; cannot reach the api or worker repositories."
  assume_role_policy   = local.oidc_trust["publisher"]
  tags                 = merge(var.tags, { Name = "${var.name_prefix}-revision-reader-publisher" })
}

resource "aws_iam_role_policy" "reader_publisher" {
  count = local.create_oidc_publisher

  name = "${var.name_prefix}-revision-reader-publisher"
  role = aws_iam_role.reader_publisher[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Sid = "EcrAuthToken", Effect = "Allow", Action = "ecr:GetAuthorizationToken", Resource = "*" },
      {
        Sid    = "PushReaderRepositoryOnly"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:InitiateLayerUpload",
          "ecr:UploadLayerPart",
          "ecr:CompleteLayerUpload",
          "ecr:PutImage",
          "ecr:BatchGetImage",
          "ecr:DescribeImages",
        ]
        Resource = [aws_ecr_repository.reader[0].arn]
      },
      {
        # A publisher that could RunTask, PassRole or read secrets would make the
        # publish/invoke split cosmetic.
        Sid    = "DenyInvocationSecretsAndIdentityWrites"
        Effect = "Deny"
        Action = [
          "ecs:RunTask", "ecs:StartTask", "ecs:RegisterTaskDefinition", "ecs:UpdateService",
          "ecs:CreateService", "ecs:ExecuteCommand",
          "iam:PassRole", "iam:CreateRole", "iam:PutRolePolicy", "iam:AttachRolePolicy",
          "iam:UpdateAssumeRolePolicy",
          "secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue",
          "kms:CreateGrant", "kms:PutKeyPolicy", "kms:ScheduleKeyDeletion",
          "ecr:DeleteRepository", "ecr:BatchDeleteImage", "ecr:PutImageTagMutability",
          "cloudtrail:StopLogging", "cloudtrail:DeleteTrail",
        ]
        Resource = "*"
      },
    ]
  })
}

# --- reader runner role: runs the exact revision and reads its log stream -------------
resource "aws_iam_role" "reader_runner" {
  count = local.create_oidc_runner

  permissions_boundary = local.effective_permissions_boundary
  name                 = "${var.name_prefix}-revision-reader-runner"
  description          = "CI identity that invokes the reader task and reads its log stream. Cannot register task definitions, create services, push images, or read secrets."
  assume_role_policy   = local.oidc_trust["runner"]
  tags                 = merge(var.tags, { Name = "${var.name_prefix}-revision-reader-runner" })
}

resource "aws_iam_role_policy" "reader_runner" {
  count = local.create_oidc_runner

  name = "${var.name_prefix}-revision-reader-runner"
  role = aws_iam_role.reader_runner[0].id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat(
      # Until an image digest is pinned there is no task definition, and therefore no
      # RunTask grant at all: the role exists but can invoke nothing. Written as a
      # comprehension over the resource rather than `count == 0 ? [] : [...[0]]` so there
      # is no index expression that could be evaluated against an empty list.
      [for td in aws_ecs_task_definition.reader : {
        # EXACT REVISION, never the family alias: family-only scoping widens silently the
        # moment anyone registers revision N+1, which is exactly how a hardened invocation
        # path degrades into an arbitrary one.
        Sid      = "RunExactReaderRevisionOnly"
        Effect   = "Allow"
        Action   = "ecs:RunTask"
        Resource = td.arn
        Condition = {
          ArnEquals    = { "ecs:cluster" = var.ecs_cluster_arn }
          StringEquals = { "aws:RequestedRegion" = var.aws_region }
        }
      }],
      [
        {
          # Task ARNs are generated per run and cannot be pinned in advance; the cluster
          # condition is the correct compensator for the required Resource "*".
          Sid       = "DescribeTasksInStagingClusterOnly"
          Effect    = "Allow"
          Action    = "ecs:DescribeTasks"
          Resource  = "*"
          Condition = { ArnEquals = { "ecs:cluster" = var.ecs_cluster_arn } }
        },
        {
          # The reader's OWN group only — NOT an /ecs/<prefix>-* prefix, which would hand
          # the runner the api and worker application logs.
          #
          # GetLogEvents ALONE. `logs:DescribeLogStreams` was granted here and never used:
          # the invocation workflow derives the stream name from the task ARN that RunTask
          # returned (`reader/<container>/<task-id>`) rather than listing streams, which is
          # deliberate — a search could return a different or older run's output, and the
          # whole point is that the answer belongs to one specific execution. A grant whose
          # only purpose would be to enable the enumeration we refuse to do is exactly the
          # unused privilege this module rejects elsewhere (see the absent CreateLogGroup).
          Sid      = "ReadReaderLogStreamOnly"
          Effect   = "Allow"
          Action   = "logs:GetLogEvents"
          Resource = ["${aws_cloudwatch_log_group.reader[0].arn}:*"]
        },
        {
          # Exact-ARN PassRole is the ONLY genuine prevention in the whole override
          # family: TaskOverride carries both taskRoleArn and executionRoleArn, and this
          # is what stops either being substituted for a more privileged role.
          Sid       = "PassOnlyReaderExecutionRole"
          Effect    = "Allow"
          Action    = "iam:PassRole"
          Resource  = [aws_iam_role.reader_execution[0].arn]
          Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
        },
        {
          # Belt-and-braces: NotResource catches any future Allow that widens the grant
          # above, since an explicit Deny always wins.
          Sid         = "DenyPassRoleExceptReaderExecutionRole"
          Effect      = "Deny"
          Action      = "iam:PassRole"
          NotResource = [aws_iam_role.reader_execution[0].arn]
        },
        {
          Sid    = "DenyAllEcsWritesAndExec"
          Effect = "Deny"
          Action = [
            "ecs:RegisterTaskDefinition", "ecs:DeregisterTaskDefinition",
            "ecs:CreateService", "ecs:UpdateService", "ecs:DeleteService",
            "ecs:StartTask", "ecs:StopTask", "ecs:ExecuteCommand",
            "ecs:CreateCluster", "ecs:DeleteCluster", "ecs:UpdateCluster",
            "ecs:TagResource", "ecs:UntagResource",
          ]
          Resource = "*"
        },
        {
          # ecs:enable-execute-command IS a real request condition key (unlike overrides
          # and networkConfiguration, which have none). This closes the launch-time flag;
          # the ecs:ExecuteCommand deny above closes the later session connect.
          Sid       = "DenyRunTaskWithExecuteCommandEnabled"
          Effect    = "Deny"
          Action    = "ecs:RunTask"
          Resource  = "*"
          Condition = { Bool = { "ecs:enable-execute-command" = "true" } }
        },
        {
          # cloudtrail:LookupEvents is DENIED on purpose. Override validation reads the
          # CloudTrail record of THIS principal's own RunTask call; a principal that can
          # read its own audit trail is not audited by it. That validation belongs to a
          # separate identity under a separate authorization.
          Sid    = "DenySecretsIdentityWritesBulkLogsAndSelfAudit"
          Effect = "Deny"
          Action = [
            "secretsmanager:GetSecretValue", "secretsmanager:BatchGetSecretValue",
            "iam:CreateRole", "iam:DeleteRole", "iam:AttachRolePolicy", "iam:DetachRolePolicy",
            "iam:PutRolePolicy", "iam:DeleteRolePolicy", "iam:UpdateAssumeRolePolicy",
            "kms:CreateGrant", "kms:PutKeyPolicy", "kms:ScheduleKeyDeletion",
            "logs:StartQuery", "logs:CreateExportTask", "logs:PutLogEvents",
            "logs:CreateLogStream", "logs:DeleteLogGroup", "logs:DeleteLogStream",
            "logs:PutRetentionPolicy",
            "ecr:PutImage", "ecr:InitiateLayerUpload", "ecr:BatchDeleteImage",
            "cloudtrail:LookupEvents",
            "ec2:AuthorizeSecurityGroupIngress", "ec2:AuthorizeSecurityGroupEgress",
            "ec2:RevokeSecurityGroupEgress", "ec2:ModifyNetworkInterfaceAttribute",
            "rds:ModifyDBInstance", "rds:DeleteDBInstance",
          ]
          Resource = "*"
        },
      ]
    )
  })
}
