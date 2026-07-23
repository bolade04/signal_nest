# main.tf — staging ECS/Fargate compute plane (INFRA-4 ecs module)
#
# Owns the compute plane locked by aws-staging-iac-plan.md §26.2/§26.3/§26.9/§26.10:
# the ECS cluster, THREE deterministic CloudWatch log groups, THREE per-workload task
# security groups plus EVERY task-side cross-SG rule (both directions), THREE task
# definitions (API / worker / one-shot migration), and TWO long-running services
# (API, worker). The migration workload is a task definition ONLY — never a service;
# running it is a later, separately authorized one-shot `run-task` (INFRA-5/INFRA-9).
#
# BOUNDARIES (consumed, never created here): ALB + ALB SG + target group (`alb`),
# database resources + rule-free RDS SG (`data_sql`), cache resources + rule-free
# Redis SG (`data_cache`), the four IAM role ARNs (`iam`), the four secret
# containers (`secrets`), the two ECR repositories (`registry`), VPC/subnets
# (`network`). No storage input exists (§26.12 has no `storage -> ecs` edge;
# `S3_BUCKET` arrives via the ordinary environment maps at composition time).
# `observability` later CONSUMES this module's log-group/service outputs and owns
# alarms; this module creates no alarm, dashboard, or topic (§26.9).
#
# SECURITY MODEL (§26.2-§26.4): three task SGs — NOT one shared SG — with standalone
# provider-6.55 rule resources only (never inline blocks). SG-referenced traffic:
# ALB↔API TCP 8000 (both rules owned here); PostgreSQL TCP 5432 from api/worker/
# migration; Redis TCP 6379 from api/worker ONLY — the migration task is pinned to
# non-Redis backends (executable basis: apps/api/app/core/config.py:306-312) and
# receives NO Redis rule and NO REDIS_URL secret. Public-HTTPS egress is the §26.4
# NAT baseline: TCP 443 IPv4 per task SG (ECR pull, Secrets Manager injection,
# CloudWatch Logs delivery, S3, approved LLM providers) — no all-protocol egress,
# no IPv6, no CIDR-based INGRESS anywhere, no AmazonProvidedDNS rule.
#
# IMAGES (§26.5): exactly two immutable digest-pinned images. API task = api image
# digest; worker task = worker image digest; migration task = the SAME worker image
# digest with the locked command override `python -m app.db.migrate upgrade`. No
# third image, no mutable tag (input validation rejects anything but sha256:<hex64>).
#
# RUNTIME (§26.10): Fargate, LINUX/X86_64 (matches the CI linux/amd64 build),
# platform version 1.4.0, private subnets, assign_public_ip=false, 256 CPU/512 MiB
# baseline, desired count 1+1, min-healthy 100%/max 200%, deployment circuit breaker
# with rollback, API health-check grace 60s, ECS Exec disabled, autoscaling
# deferred. Containers run read-only-root + writable /tmp (task-scoped volume —
# Fargate supports no tmpfs) as non-root 10001:10001 with exec-form commands;
# `awslogs-create-group` is omitted so the OpenTofu-created groups must pre-exist
# (§26.9) and the execution role needs no CreateLogGroup permission.
#
# No `provider "aws"` block; `versions.tf` declares only the provider SOURCE per the
# sibling-module convention. Tagging is the root provider's `default_tags`; this
# module adds only per-resource `Name` tags. The awslogs region comes from a data
# source at plan time (iam-module precedent) — no region literal is committed.

data "aws_region" "current" {}

locals {
  cluster_name = coalesce(var.cluster_name, "${var.name_prefix}-cluster")
  workloads    = toset(["api", "worker", "migration"])

  log_group_names = { for w in local.workloads : w => "/ecs/${var.name_prefix}-${w}" }

  # Locked per-workload secret subsets (§26.7): api/worker = all four; migration =
  # three (REDIS_URL excluded — granting it would be a G5 ACTOR_SUBSET_EXCEEDED
  # over-provision). Sorted for deterministic task-definition JSON.
  workload_secret_keys = {
    api       = ["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY"]
    worker    = ["SECRET_KEY", "DATABASE_URL", "REDIS_URL", "LLM_API_KEY"]
    migration = ["SECRET_KEY", "DATABASE_URL", "LLM_API_KEY"]
  }
  workload_secrets = {
    for w, ks in local.workload_secret_keys : w => [
      for k in sort(ks) : { name = k, valueFrom = var.secret_arns[k] }
    ]
  }

  workload_environment = {
    api       = [for k in sort(keys(var.api_environment)) : { name = k, value = var.api_environment[k] }]
    worker    = [for k in sort(keys(var.worker_environment)) : { name = k, value = var.worker_environment[k] }]
    migration = [for k in sort(keys(var.migration_environment)) : { name = k, value = var.migration_environment[k] }]
  }

  # Two immutable image references (§26.5); migration reuses the worker reference.
  api_image    = "${var.repository_urls["api"]}@${var.api_image_digest}"
  worker_image = "${var.repository_urls["worker"]}@${var.worker_image_digest}"
  workload_image = {
    api       = local.api_image
    worker    = local.worker_image
    migration = local.worker_image
  }

  # Shared container skeleton (§26.10): non-root 10001, read-only root filesystem,
  # writable /tmp via the task-scoped volume, prefix-scoped awslogs delivery.
  container_common = {
    for w in local.workloads : w => {
      name                   = w
      image                  = local.workload_image[w]
      essential              = true
      user                   = "10001:10001"
      readonlyRootFilesystem = true
      mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
      environment            = local.workload_environment[w]
      secrets                = local.workload_secrets[w]
      logConfiguration = {
        logDriver = "awslogs"
        options = {
          "awslogs-group"         = local.log_group_names[w]
          "awslogs-region"        = data.aws_region.current.region
          "awslogs-stream-prefix" = w
        }
      }
    }
  }
}

# --- ECS cluster -------------------------------------------------------------------
resource "aws_ecs_cluster" "this" {
  name = local.cluster_name

  tags = {
    Name = local.cluster_name
  }
}

# --- Three deterministic ecs-owned CloudWatch log groups (§26.9) -------------------
# observability CONSUMES these outputs for metric filters/alarms and never creates
# them. Default CloudWatch encryption at rest; a customer-managed KMS design remains
# separately authorized.
resource "aws_cloudwatch_log_group" "workload" {
  for_each = local.workloads

  name              = local.log_group_names[each.key]
  retention_in_days = var.log_retention_days

  tags = {
    Name = local.log_group_names[each.key]
  }
}

# --- Three per-workload task security groups (§26.2) — created with ZERO inline rules
resource "aws_security_group" "task" {
  for_each = local.workloads

  name        = "${var.name_prefix}-${each.key}-task-sg"
  description = "${each.key} task SG for ${var.name_prefix}; every rule is a standalone resource owned by the ecs module (§26.2)."
  vpc_id      = var.vpc_id

  tags = {
    Name = "${var.name_prefix}-${each.key}-task-sg"
  }
}

# --- ALB↔API TCP 8000 (§26.3/§26.13) — both rules owned here -----------------------
resource "aws_vpc_security_group_egress_rule" "alb_to_api" {
  security_group_id            = var.alb_security_group_id
  description                  = "ALB egress to the API task SG only, TCP 8000 (owned by ecs; §26.3)."
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = aws_security_group.task["api"].id

  tags = {
    Name = "${var.name_prefix}-alb-to-api-8000"
  }
}

resource "aws_vpc_security_group_ingress_rule" "api_from_alb" {
  security_group_id            = aws_security_group.task["api"].id
  description                  = "API task ingress from the ALB SG only, TCP 8000 (§26.3). No public 8000."
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
  referenced_security_group_id = var.alb_security_group_id

  tags = {
    Name = "${var.name_prefix}-api-from-alb-8000"
  }
}

# --- PostgreSQL TCP 5432: api, worker, AND migration (§26.3) -----------------------
resource "aws_vpc_security_group_egress_rule" "task_to_postgres" {
  for_each = local.workloads

  security_group_id            = aws_security_group.task[each.key].id
  description                  = "${each.key} task egress to the PostgreSQL SG only, TCP 5432 (§26.3)."
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = var.rds_security_group_id

  tags = {
    Name = "${var.name_prefix}-${each.key}-to-postgres-5432"
  }
}

resource "aws_vpc_security_group_ingress_rule" "postgres_from_task" {
  for_each = local.workloads

  security_group_id            = var.rds_security_group_id
  description                  = "PostgreSQL ingress from the ${each.key} task SG only, TCP 5432 (three separate standalone rules; owned by ecs, destination SG owned by data_sql; §26.3)."
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
  referenced_security_group_id = aws_security_group.task[each.key].id

  tags = {
    Name = "${var.name_prefix}-postgres-from-${each.key}-5432"
  }
}

# --- Redis TCP 6379: api and worker ONLY — migration is Redis-excluded (§26.3) -----
resource "aws_vpc_security_group_egress_rule" "task_to_redis" {
  for_each = toset(["api", "worker"])

  security_group_id            = aws_security_group.task[each.key].id
  description                  = "${each.key} task egress to the Redis SG only, TCP 6379 (§26.3; migration has no Redis rule)."
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = var.redis_security_group_id

  tags = {
    Name = "${var.name_prefix}-${each.key}-to-redis-6379"
  }
}

resource "aws_vpc_security_group_ingress_rule" "redis_from_task" {
  for_each = toset(["api", "worker"])

  security_group_id            = var.redis_security_group_id
  description                  = "Redis ingress from the ${each.key} task SG only, TCP 6379 (two separate standalone rules; owned by ecs, destination SG owned by data_cache; §26.3)."
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
  referenced_security_group_id = aws_security_group.task[each.key].id

  tags = {
    Name = "${var.name_prefix}-redis-from-${each.key}-6379"
  }
}

# --- §26.4 NAT baseline: TCP 443 IPv4 egress per task SG ---------------------------
# Fargate platform operations (ECR pull, Secrets Manager injection, awslogs
# delivery) traverse the task ENI/NAT path, so ALL THREE workloads need this. It is
# CIDR-based EGRESS on one port only — never all-protocol, never IPv6, and no
# CIDR-based ingress exists anywhere in this module. VPC endpoints / egress
# filtering remain separately authorized improvements.
resource "aws_vpc_security_group_egress_rule" "task_https" {
  for_each = local.workloads

  security_group_id = aws_security_group.task[each.key].id
  description       = "${each.key} task HTTPS egress via NAT (TCP 443 IPv4 only; §26.4 staging baseline — ECR, Secrets Manager, CloudWatch Logs, S3, approved LLM providers)."
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
  cidr_ipv4         = "0.0.0.0/0"

  tags = {
    Name = "${var.name_prefix}-${each.key}-https-egress"
  }
}

# --- API task definition -----------------------------------------------------------
resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name_prefix}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.api_task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    merge(local.container_common["api"], {
      portMappings = [{ containerPort = 8000, protocol = "tcp" }]
      stopTimeout  = var.api_stop_timeout_seconds
    })
  ])

  tags = {
    Name = "${var.name_prefix}-api"
  }
}

# --- Worker task definition --------------------------------------------------------
resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name_prefix}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.worker_task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    merge(local.container_common["worker"], {
      stopTimeout = var.worker_stop_timeout_seconds
    })
  ])

  tags = {
    Name = "${var.name_prefix}-worker"
  }

  lifecycle {
    precondition {
      condition     = var.worker_stop_timeout_seconds >= var.worker_shutdown_grace_seconds
      error_message = "worker_stop_timeout_seconds must be >= worker_shutdown_grace_seconds so the worker can drain in-flight jobs before SIGKILL (§26.10)."
    }
  }
}

# --- Migration task definition (one-shot; NEVER a service) -------------------------
# Reuses the worker image digest with the locked command override (§26.5). No
# stopTimeout override (the ECS default applies to the short-lived one-shot task);
# no Redis secret, rule, or configuration. Executing it is a later, separately
# authorized run-task (INFRA-5/INFRA-9) — nothing here schedules or runs it.
resource "aws_ecs_task_definition" "migration" {
  family                   = "${var.name_prefix}-migration"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = var.execution_role_arn
  task_role_arn            = var.migration_task_role_arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([
    merge(local.container_common["migration"], {
      command = ["python", "-m", "app.db.migrate", "upgrade"]
    })
  ])

  tags = {
    Name = "${var.name_prefix}-migration"
  }
}

# --- API service (behind the alb-owned target group) -------------------------------
resource "aws_ecs_service" "api" {
  name                               = "${var.name_prefix}-api"
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = aws_ecs_task_definition.api.arn
  desired_count                      = var.api_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  health_check_grace_period_seconds  = 60
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.task["api"].id]
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = var.api_target_group_arn
    container_name   = "api"
    container_port   = 8000
  }

  tags = {
    Name = "${var.name_prefix}-api"
  }
}

# --- Worker service (no port, no load balancer) ------------------------------------
resource "aws_ecs_service" "worker" {
  name                               = "${var.name_prefix}-worker"
  cluster                            = aws_ecs_cluster.this.id
  task_definition                    = aws_ecs_task_definition.worker.arn
  desired_count                      = var.worker_desired_count
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  deployment_minimum_healthy_percent = 100
  deployment_maximum_percent         = 200
  enable_execute_command             = false

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  network_configuration {
    subnets          = var.private_subnet_ids
    security_groups  = [aws_security_group.task["worker"].id]
    assign_public_ip = false
  }

  tags = {
    Name = "${var.name_prefix}-worker"
  }
}
