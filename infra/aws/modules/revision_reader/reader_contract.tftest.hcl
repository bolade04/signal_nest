# Gate 4J — contract tests for the dedicated revision-reader stack.
#
# Offline only: the aws provider is fully mocked, so `tofu test` renders the module
# in-memory with NO backend, NO AWS calls, and NO plan against real state.
#
# WHAT THESE TESTS ARE FOR. The reader's safety rests on things that are ABSENT —
# no entryPoint, no command, no task role, no second secret, no CreateLogGroup, no
# RunTask for the publisher. Absences do not fail loudly when someone adds them back;
# they simply stop being true. Every assertion below pins one absence or one exact
# scope, so restoring the unsafe form breaks a test rather than shipping quietly.
#
# All identifiers are synthetic. No real account id, digest prefix, or ARN appears here.

mock_provider "aws" {}

# DISTINCT synthetic values per resource, not one shared mock default.
#
# This matters for correctness of the tests themselves: the provider's generated mock
# values are random strings (which the task definition rejects as a malformed role ARN),
# and a single shared default would make every "scoped to exactly X" assertion pass
# trivially — a PassRole grant pointing at the PUBLISHER role would still compare equal
# to the execution role. Distinct values are what give those assertions teeth.
override_resource {
  target = aws_iam_role.reader_execution
  values = { arn = "arn:aws:iam::111122223333:role/signalnest-staging-revision-reader-execution" }
}

override_resource {
  target = aws_iam_role.reader_publisher
  values = { arn = "arn:aws:iam::111122223333:role/signalnest-staging-revision-reader-publisher" }
}

override_resource {
  target = aws_iam_role.reader_runner
  values = { arn = "arn:aws:iam::111122223333:role/signalnest-staging-revision-reader-runner" }
}

override_resource {
  target = aws_ecr_repository.reader
  values = {
    arn            = "arn:aws:ecr:us-east-1:111122223333:repository/signalnest-staging/revision-reader"
    repository_url = "111122223333.dkr.ecr.us-east-1.amazonaws.com/signalnest-staging/revision-reader"
  }
}

override_resource {
  target = aws_cloudwatch_log_group.reader
  values = { arn = "arn:aws:logs:us-east-1:111122223333:log-group:/ecs/signalnest-staging-revision-reader" }
}

override_resource {
  target = aws_ecs_task_definition.reader
  values = { arn = "arn:aws:ecs:us-east-1:111122223333:task-definition/signalnest-staging-revision-reader:1" }
}

variables {
  enabled                      = true
  name_prefix                  = "signalnest-staging"
  aws_region                   = "us-east-1"
  vpc_id                       = "vpc-test"
  rds_security_group_id        = "sg-rds"
  database_url_secret_arn      = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/DATABASE_URL-bb"
  secrets_kms_key_arn          = "arn:aws:kms:us-east-1:111122223333:key/11111111-2222-3333-4444-555555555555"
  ecs_cluster_arn              = "arn:aws:ecs:us-east-1:111122223333:cluster/signalnest-staging-cluster"
  github_oidc_provider_arn     = "arn:aws:iam::111122223333:oidc-provider/token.actions.githubusercontent.com"
  github_repository            = "example-owner/example-repo"
  revision_reader_image_digest = "sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
}

# =====================================================================================
# The task definition: the IaC half of the override-prevention control.
# =====================================================================================
run "task_definition_contract" {
  command = plan

  assert {
    condition     = length(jsondecode(aws_ecs_task_definition.reader[0].container_definitions)) == 1
    error_message = "reader task must define exactly one container"
  }

  # THE CONTROL. A task-definition entryPoint SHADOWS the image's fixed ENTRYPOINT; the
  # moment one exists, override prevention degrades back to Gate 4I's detection-only
  # posture, silently and with no other symptom.
  assert {
    condition     = !contains(keys(jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0]), "entryPoint")
    error_message = "task definition sets entryPoint, shadowing the image's fixed ENTRYPOINT"
  }

  # A `command` here would be DOCUMENTATION, NOT A CONTROL (RunTask overrides it freely),
  # and its presence would invite the reader to be described as safe because of it.
  assert {
    condition     = !contains(keys(jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0]), "command")
    error_message = "task definition sets command; the image entrypoint is the control, not this"
  }

  # No task role AT ALL — not an empty one. This is what lets the runner's PassRole grant
  # name exactly one ARN.
  assert {
    condition     = aws_ecs_task_definition.reader[0].task_role_arn == null
    error_message = "reader task definition must have no task role whatsoever"
  }

  assert {
    condition     = aws_ecs_task_definition.reader[0].execution_role_arn == aws_iam_role.reader_execution[0].arn
    error_message = "reader task must use the dedicated reader execution role"
  }

  # DATABASE_URL and nothing else: no SECRET_KEY, REDIS_URL or LLM_API_KEY.
  assert {
    condition     = [for s in jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0].secrets : s.name] == ["DATABASE_URL"]
    error_message = "reader must receive exactly one secret, DATABASE_URL"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0].secrets[0].valueFrom == var.database_url_secret_arn
    error_message = "reader DATABASE_URL must come from the supplied secret ARN"
  }

  # Empty and deliberately so: ContainerOverride carries `environment`, so nothing set
  # here is a control. Configuration that must hold lives inside the program.
  assert {
    condition     = length(jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0].environment) == 0
    error_message = "reader task must declare no environment; env is caller-overridable and therefore not a control"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0].readonlyRootFilesystem == true
    error_message = "reader container must run with a read-only root filesystem"
  }

  assert {
    condition     = jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0].user == "10001:10001"
    error_message = "reader container must run as the non-root fleet uid"
  }

  # Digest-pinned, never a tag: a mutable tag would make the runner's exact-revision
  # RunTask scoping meaningless, since the revision could point at new content.
  assert {
    condition     = strcontains(jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0].image, "@${var.revision_reader_image_digest}")
    error_message = "reader image must be pinned by immutable digest"
  }

  # Its OWN log group. Sharing the migration group would interleave the verification
  # evidence with another workload's diagnostics and force wider runner log scoping.
  assert {
    condition     = jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0].logConfiguration.options["awslogs-group"] == "/ecs/signalnest-staging-revision-reader"
    error_message = "reader must log to its own dedicated group"
  }

  # Omitted so the OpenTofu-created group must pre-exist and the execution role needs no
  # logs:CreateLogGroup.
  assert {
    condition     = !contains(keys(jsondecode(aws_ecs_task_definition.reader[0].container_definitions)[0].logConfiguration.options), "awslogs-create-group")
    error_message = "awslogs-create-group must stay omitted"
  }
}

# =====================================================================================
# Execution role: what ECS may do to START the task.
# =====================================================================================
run "execution_role_scoping" {
  command = plan

  # The reader repository ONLY. If this ever widened to the api/worker repositories, the
  # execution role could start a container that contains Alembic.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.reader_execution[0].policy).Statement :
      s.Resource == [aws_ecr_repository.reader[0].arn] if s.Sid == "PullReaderImageOnly"
    ])
    error_message = "execution role may pull only the dedicated reader repository"
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.reader_execution[0].policy).Statement :
      s.Resource == [var.database_url_secret_arn] if s.Sid == "InjectDatabaseUrlSecretOnly"
    ])
    error_message = "execution role may read exactly the DATABASE_URL secret"
  }

  # Without kms:ViaService this is a general Decrypt grant on the CMK rather than
  # decryption performed by Secrets Manager on this role's behalf.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.reader_execution[0].policy).Statement :
      try(s.Condition.StringEquals["kms:ViaService"], "") == "secretsmanager.us-east-1.amazonaws.com"
      if contains(flatten([s.Action]), "kms:Decrypt")
    ])
    error_message = "kms:Decrypt must be confined by kms:ViaService to Secrets Manager"
  }

  # Unused privilege that would also let a misconfigured task write outside the audited
  # group. The module creates the group, so creation is never needed.
  assert {
    condition     = !contains(flatten([for s in jsondecode(aws_iam_role_policy.reader_execution[0].policy).Statement : flatten([s.Action])]), "logs:CreateLogGroup")
    error_message = "execution role must not hold logs:CreateLogGroup"
  }

  # Every escalation-shaped action must be absent from an execution role.
  assert {
    condition = length(setintersection(
      toset(flatten([for s in jsondecode(aws_iam_role_policy.reader_execution[0].policy).Statement : flatten([s.Action]) if s.Effect == "Allow"])),
      toset(["ecs:RunTask", "ecs:ExecuteCommand", "iam:PassRole", "ecr:PutImage", "logs:GetLogEvents"])
    )) == 0
    error_message = "execution role holds an action outside starting the task"
  }

  # Gate 4J.1: environmentFiles is a caller-supplied override that fetches from S3 using this
  # role; an explicit Deny on s3:GetObject makes its closure unconditional rather than
  # relying on the absence of an allow.
  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_role_policy.reader_execution[0].policy).Statement :
      s.Effect == "Deny" && contains(flatten([s.Action]), "s3:GetObject")
    ])
    error_message = "execution role must explicitly Deny s3:GetObject (environmentFiles closure)"
  }
}

# =====================================================================================
# Runner role: what CI may do to INVOKE the reader.
# =====================================================================================
run "runner_role_scoping" {
  command = plan

  # EXACT REVISION, not the family. A family-scoped grant widens silently the moment
  # anyone registers revision N+1.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement :
      s.Resource == aws_ecs_task_definition.reader[0].arn if s.Sid == "RunExactReaderRevisionOnly"
    ])
    error_message = "RunTask must be scoped to the exact task-definition revision ARN"
  }

  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement :
      s if s.Sid == "RunExactReaderRevisionOnly"
    ]) == 1
    error_message = "there must be exactly one RunTask grant"
  }

  # Exact-ARN PassRole is the ONLY genuine prevention in the override family: TaskOverride
  # carries both taskRoleArn and executionRoleArn.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement :
      s.Resource == [aws_iam_role.reader_execution[0].arn] if s.Sid == "PassOnlyReaderExecutionRole"
    ])
    error_message = "runner may pass only the reader execution role"
  }

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement :
      s.NotResource == [aws_iam_role.reader_execution[0].arn] if s.Sid == "DenyPassRoleExceptReaderExecutionRole"
    ])
    error_message = "the PassRole Deny must exempt only the reader execution role"
  }

  # ecs:enable-execute-command is one of the three request parameters IAM can actually
  # condition on. Losing this condition re-opens an interactive shell into the task.
  assert {
    condition = anytrue([
      for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement :
      s.Effect == "Deny" && try(s.Condition.Bool["ecs:enable-execute-command"], "") == "true"
    ])
    error_message = "runner must deny RunTask with ECS Exec enabled"
  }

  # Self-audit is not a control: the principal whose RunTask call is being validated must
  # not be the principal that reads the CloudTrail record of it.
  assert {
    condition = contains(flatten([
      for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement :
      flatten([s.Action]) if s.Effect == "Deny"
    ]), "cloudtrail:LookupEvents")
    error_message = "runner must be denied cloudtrail:LookupEvents (self-audit is not a control)"
  }

  # The runner must never gain the ability to register a new (and therefore differently
  # scoped) revision, nor to read the api/worker secrets.
  assert {
    condition = length(setintersection(
      toset(flatten([for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement : flatten([s.Action]) if s.Effect == "Allow"])),
      toset(["ecs:RegisterTaskDefinition", "ecs:ExecuteCommand", "ecs:StopTask", "ecs:UpdateService", "secretsmanager:GetSecretValue", "ecr:PutImage"])
    )) == 0
    error_message = "runner holds an action beyond invoking the reader and reading its log stream"
  }

  # Log reads pinned to the reader's own group — never an /ecs/<prefix>-* prefix, which
  # would hand the runner the api and worker application logs.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement :
      s.Resource == ["${aws_cloudwatch_log_group.reader[0].arn}:*"] if s.Sid == "ReadReaderLogStreamOnly"
    ])
    error_message = "runner log reads must be pinned to the dedicated reader group"
  }

  # EXACTLY GetLogEvents, pinned as a whole. The resource assertion above passed unchanged
  # when logs:DescribeLogStreams was removed, which means it never constrained the action
  # set — so re-adding an unused enumeration grant would have been invisible. The workflow
  # derives the stream name from the task ARN and never lists, deliberately: a search could
  # return a different run's output.
  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement :
      flatten([s.Action]) == ["logs:GetLogEvents"] if s.Sid == "ReadReaderLogStreamOnly"
    ])
    error_message = "runner must hold logs:GetLogEvents alone — no stream enumeration"
  }
}

# =====================================================================================
# Publisher role and the publish/invoke split.
# =====================================================================================
run "publisher_role_scoping" {
  command = plan

  assert {
    condition = alltrue([
      for s in jsondecode(aws_iam_role_policy.reader_publisher[0].policy).Statement :
      s.Resource == [aws_ecr_repository.reader[0].arn] if s.Sid == "PushReaderRepositoryOnly"
    ])
    error_message = "publisher may push only the dedicated reader repository"
  }

  assert {
    condition = length(setintersection(
      toset(flatten([for s in jsondecode(aws_iam_role_policy.reader_publisher[0].policy).Statement : flatten([s.Action]) if s.Effect == "Allow"])),
      toset(["ecs:RunTask", "iam:PassRole", "secretsmanager:GetSecretValue"])
    )) == 0
    error_message = "publisher must not be able to invoke tasks, pass roles, or read secrets"
  }

  # The split is only real if the two roles are not interchangeable at the TRUST boundary:
  # identical subject claims would let either job assume either role.
  assert {
    condition     = aws_iam_role.reader_publisher[0].assume_role_policy != aws_iam_role.reader_runner[0].assume_role_policy
    error_message = "publisher and runner must pin different OIDC subject claims"
  }

  # StringLike on `sub` would admit any branch or environment in the repository.
  assert {
    condition = alltrue([
      for r in [aws_iam_role.reader_publisher[0], aws_iam_role.reader_runner[0]] :
      alltrue([
        for s in jsondecode(r.assume_role_policy).Statement :
        contains(keys(s.Condition), "StringEquals") && !contains(keys(s.Condition), "StringLike")
      ])
    ])
    error_message = "OIDC trust must use StringEquals on both aud and sub, never StringLike"
  }
}

# =====================================================================================
# Networking and the digest fail-closed path.
# =====================================================================================
run "network_and_repository_contract" {
  command = plan

  assert {
    condition     = aws_vpc_security_group_egress_rule.reader_to_postgres[0].referenced_security_group_id == var.rds_security_group_id
    error_message = "the reader's data-plane egress must target the RDS security group"
  }

  assert {
    condition     = aws_vpc_security_group_egress_rule.reader_to_postgres[0].from_port == 5432 && aws_vpc_security_group_egress_rule.reader_to_postgres[0].to_port == 5432
    error_message = "reader database egress must be exactly 5432"
  }

  # Immutable tags are what make a published digest a stable identity.
  assert {
    condition     = aws_ecr_repository.reader[0].image_tag_mutability == "IMMUTABLE"
    error_message = "reader repository tags must be immutable"
  }
}

# Fail-closed: with no digest there is no task definition, so the runner's RunTask grant
# does not exist and the role can invoke nothing at all.
run "no_digest_means_nothing_is_invocable" {
  command = plan

  variables {
    revision_reader_image_digest = null
  }

  assert {
    condition     = length(aws_ecs_task_definition.reader) == 0
    error_message = "no task definition may exist without a pinned image digest"
  }

  assert {
    condition = length([
      for s in jsondecode(aws_iam_role_policy.reader_runner[0].policy).Statement :
      s if try(s.Action, "") == "ecs:RunTask" && s.Effect == "Allow"
    ]) == 0
    error_message = "with no digest the runner must hold no RunTask grant at all"
  }
}

# Disabled is genuinely inert: the default posture creates nothing.
run "disabled_creates_nothing" {
  command = plan

  variables {
    enabled = false
  }

  assert {
    condition     = length(aws_ecr_repository.reader) == 0 && length(aws_cloudwatch_log_group.reader) == 0 && length(aws_security_group.reader) == 0 && length(aws_iam_role.reader_execution) == 0 && length(aws_iam_role.reader_publisher) == 0 && length(aws_iam_role.reader_runner) == 0 && length(aws_ecs_task_definition.reader) == 0
    error_message = "enabled = false must create nothing"
  }
}
