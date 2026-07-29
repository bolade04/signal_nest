# =====================================================================================
# Gate 4J — the reader task definition.
#
# WHAT IS DELIBERATELY ABSENT HERE, and why each absence is the control:
#
#   entryPoint  — ABSENT. Setting it would SHADOW the image's fixed ENTRYPOINT, and since
#                 a task-definition entryPoint is just another registered value, the
#                 prevention would collapse back into Gate 4I's detection-only posture.
#                 apps/revision-reader/tests/test_dockerfile.py asserts this mechanically
#                 against every .tf file in this module.
#
#   command     — ABSENT. In the migration task definition the `command` is DOCUMENTATION,
#                 NOT A CONTROL: RunTask overrides it freely. Here the image needs none,
#                 and leaving it empty means an override command lands as argv against a
#                 program that rejects all argv.
#
#   taskRoleArn — ABSENT ENTIRELY, not an empty role. The reader makes no AWS API call.
#                 Omitting it is what allows the runner's iam:PassRole grant to name a
#                 single ARN; an empty task role would still have to be passable, which is
#                 one more ARN in the escalation surface for zero benefit.
#
#   a writable volume — ABSENT. readonlyRootFilesystem with PYTHONDONTWRITEBYTECODE means
#                 the process needs no writable path at all. The api/worker tasks mount a
#                 /tmp volume because Fargate supports no tmpfs and their frameworks write;
#                 this program does not.
#
# MAINTENANCE INVARIANT (Gate 4J.1): do NOT add a volume with `configuredAtLaunch = true`.
# RunTask's `volumeConfigurations` can only configure a volume the task definition already
# DECLARES; with none declared, that caller-supplied channel is inert. A configuredAtLaunch
# volume would open it.
#
# DESTINATION AUTHENTICITY (Gate 4J.1): the host, database and role are baked into the image
# (revision_reader/_pinned, generated from build args), and the reader connects with
# sslmode=verify-full against a committed CA bundle. The `environment` block below stays
# empty precisely BECAUSE it is caller-replaceable — nothing that decides which server is
# read may live in an overridable channel. The reader takes only the password from the
# injected DATABASE_URL secret and connects to the baked host regardless of the DSN.
# =====================================================================================

locals {
  # A task definition cannot exist without a real image, and the runner's RunTask grant is
  # derived from this resource's ARN — so with no digest pinned the runner can invoke
  # nothing at all. Fail-closed by construction rather than by a runtime check.
  create_task = var.enabled && var.revision_reader_image_digest != null ? 1 : 0
}

resource "aws_ecs_task_definition" "reader" {
  count = local.create_task

  family                   = local.family
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = aws_iam_role.reader_execution[0].arn

  # No task_role_arn. See the header.

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name                   = local.container
    image                  = local.image
    essential              = true
    user                   = "10001:10001"
    readonlyRootFilesystem = true

    # Empty, and NOT a control: ContainerOverride carries `environment`, so anything set
    # here is caller-replaceable. Configuration that must hold is baked into the IMAGE (the
    # expected host/db/role and the verify-full CA), never here — see the header.
    environment = []

    # DATABASE_URL ONLY. The reader needs no SECRET_KEY, REDIS_URL or LLM_API_KEY, and the
    # execution role is scoped to exactly this ARN so a fourth secret could not be injected
    # even if a future edit listed one.
    secrets = [{ name = "DATABASE_URL", valueFrom = var.database_url_secret_arn }]

    logConfiguration = {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = local.log_group
        "awslogs-region"        = var.aws_region
        "awslogs-stream-prefix" = "reader"
        # `awslogs-create-group` is deliberately omitted so the OpenTofu-created group must
        # pre-exist and the execution role needs no logs:CreateLogGroup.
      }
    }
  }])

  tags = merge(var.tags, { Name = local.family })

  lifecycle {
    precondition {
      condition     = var.revision_reader_image_digest != null
      error_message = "The revision-reader task definition requires a real immutable sha256 image digest published by the reader publication workflow."
    }
  }
}
