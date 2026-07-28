# Focused contract tests for the one-shot migration task definition and the
# API/worker service parity guarantees (Batch 5 tofu migration-contract hardening).
#
# Offline only: the aws provider is fully mocked, so `tofu test` renders the
# module in-memory with NO backend, NO AWS calls, NO plan against real state.
# Each run asserts on the rendered aws_ecs_task_definition JSON.

mock_provider "aws" {}

variables {
  name_prefix             = "signalnest-staging"
  vpc_id                  = "vpc-test"
  private_subnet_ids      = ["subnet-a", "subnet-b"]
  alb_security_group_id   = "sg-alb"
  api_target_group_arn    = "arn:aws:elasticloadbalancing:us-east-1:111122223333:targetgroup/tg/abc"
  rds_security_group_id   = "sg-rds"
  redis_security_group_id = "sg-redis"
  repository_urls = {
    api    = "111122223333.dkr.ecr.us-east-1.amazonaws.com/signalnest-staging/api"
    worker = "111122223333.dkr.ecr.us-east-1.amazonaws.com/signalnest-staging/worker"
  }
  deploy_workload         = true
  api_image_digest        = "sha256:96fa64dde9d70000000000000000000000000000000000000000000000000000"
  worker_image_digest     = "sha256:2b4b063bca250000000000000000000000000000000000000000000000000000"
  execution_role_arn      = "arn:aws:iam::111122223333:role/signalnest-staging-ecs-execution"
  api_task_role_arn       = "arn:aws:iam::111122223333:role/signalnest-staging-api-task"
  worker_task_role_arn    = "arn:aws:iam::111122223333:role/signalnest-staging-worker-task"
  migration_task_role_arn = "arn:aws:iam::111122223333:role/signalnest-staging-migration-task"
  secret_arns = {
    SECRET_KEY   = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/SECRET_KEY-aa"
    DATABASE_URL = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/DATABASE_URL-bb"
    REDIS_URL    = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/REDIS_URL-cc"
    LLM_API_KEY  = "arn:aws:secretsmanager:us-east-1:111122223333:secret:signalnest-staging/LLM_API_KEY-dd"
  }
  api_environment = {
    ENVIRONMENT = "staging"
    APP_MODE    = "full"
  }
  worker_environment = {
    ENVIRONMENT = "staging"
    APP_MODE    = "full"
  }
  migration_environment = {
    ENVIRONMENT       = "staging"
    SN_MIGRATION_MODE = "1"
  }
}

run "migration_contract" {
  command = plan

  # The rendered migration container definition (single container).
  variables {}

  assert {
    condition     = length(jsondecode(aws_ecs_task_definition.migration[0].container_definitions)) == 1
    error_message = "migration task must define exactly one container"
  }

  # (1) bare command; (2) no `upgrade` argument; (17) no shell wrapper.
  assert {
    condition = jsondecode(aws_ecs_task_definition.migration[0].container_definitions)[0].command == [
      "python", "-m", "app.db.migrate"
    ]
    error_message = "migration command must be the bare hardened entrypoint (no 'upgrade' arg, no shell)"
  }

  # (3)(4) exactly ENVIRONMENT=staging + SN_MIGRATION_MODE=1, nothing else.
  assert {
    condition = { for e in jsondecode(aws_ecs_task_definition.migration[0].container_definitions)[0].environment :
    e.name => e.value } == { ENVIRONMENT = "staging", SN_MIGRATION_MODE = "1" }
    error_message = "migration environment must be exactly ENVIRONMENT=staging + SN_MIGRATION_MODE=1"
  }

  # (5)(6)(7)(8) exactly one secret, DATABASE_URL, and no other/master secret.
  assert {
    condition = [for s in jsondecode(aws_ecs_task_definition.migration[0].container_definitions)[0].secrets :
    s.name] == ["DATABASE_URL"]
    error_message = "migration must inject exactly one secret: DATABASE_URL (no SECRET_KEY/REDIS_URL/LLM_API_KEY/master)"
  }

  # (9) read-only root filesystem.
  assert {
    condition     = jsondecode(aws_ecs_task_definition.migration[0].container_definitions)[0].readonlyRootFilesystem == true
    error_message = "migration container must have a read-only root filesystem"
  }

  # (10) writable ephemeral /tmp mount + task-scoped volume.
  assert {
    condition = anytrue([for m in jsondecode(aws_ecs_task_definition.migration[0].container_definitions)[0].mountPoints :
      m.containerPath == "/tmp" && m.readOnly == false && m.sourceVolume == "tmp"
    ])
    error_message = "migration must mount a writable ephemeral /tmp"
  }

  # (13) no privileged mode (absent or false).
  assert {
    condition     = try(jsondecode(aws_ecs_task_definition.migration[0].container_definitions)[0].privileged, false) == false
    error_message = "migration container must not be privileged"
  }

  # (11) no port mappings.
  assert {
    condition     = length(try(jsondecode(aws_ecs_task_definition.migration[0].container_definitions)[0].portMappings, [])) == 0
    error_message = "migration container must expose no ports"
  }

  # (14) digest-shaped worker image (immutable @sha256:).
  assert {
    condition     = can(regex("@sha256:[0-9a-f]{64}$", jsondecode(aws_ecs_task_definition.migration[0].container_definitions)[0].image))
    error_message = "migration image must be pinned by immutable sha256 digest"
  }

  # FARGATE / awsvpc / Linux / X86_64.
  assert {
    condition     = aws_ecs_task_definition.migration[0].requires_compatibilities == toset(["FARGATE"]) && aws_ecs_task_definition.migration[0].network_mode == "awsvpc"
    error_message = "migration must be FARGATE + awsvpc"
  }

  # (15) API service contract unchanged: all four application secrets injected.
  assert {
    condition = toset([for s in jsondecode(aws_ecs_task_definition.api[0].container_definitions)[0].secrets : s.name]) == toset([
      "SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY"
    ])
    error_message = "API service must still receive all four application secrets"
  }

  # (15) worker service contract unchanged: all four application secrets injected.
  assert {
    condition = toset([for s in jsondecode(aws_ecs_task_definition.worker[0].container_definitions)[0].secrets : s.name]) == toset([
      "SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY"
    ])
    error_message = "worker service must still receive all four application secrets"
  }

  # Migration-mode variables must not leak into API/worker environments.
  assert {
    condition     = !contains([for e in jsondecode(aws_ecs_task_definition.api[0].container_definitions)[0].environment : e.name], "SN_MIGRATION_MODE")
    error_message = "SN_MIGRATION_MODE must not appear in the API environment"
  }
  assert {
    condition     = !contains([for e in jsondecode(aws_ecs_task_definition.worker[0].container_definitions)[0].environment : e.name], "SN_MIGRATION_MODE")
    error_message = "SN_MIGRATION_MODE must not appear in the worker environment"
  }

  # (11) migration uses the intentionally-empty migration task role (zero AWS
  # permissions) via the caller-provided ARN -- not the execution role, not a
  # broad role.
  assert {
    condition     = aws_ecs_task_definition.migration[0].task_role_arn == var.migration_task_role_arn
    error_message = "migration task role must be the caller-provided (intentionally empty) migration task role"
  }

  # (13) ECS Exec disabled on both long-running services (the migration task has
  # no service, so no exec surface exists for it).
  assert {
    condition     = aws_ecs_service.api[0].enable_execute_command == false && aws_ecs_service.worker[0].enable_execute_command == false
    error_message = "ECS Exec must be disabled on the api and worker services"
  }

  # (12) no public IP: both services attach ENIs with assign_public_ip disabled
  # (the migration task carries no run-time network config in its definition).
  assert {
    condition     = aws_ecs_service.api[0].network_configuration[0].assign_public_ip == false && aws_ecs_service.worker[0].network_configuration[0].assign_public_ip == false
    error_message = "api and worker services must not assign a public IP"
  }
}
