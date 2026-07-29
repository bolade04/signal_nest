# =====================================================================================
# Gate 4J — dedicated live database revision-reader prerequisite stack.
#
# WHY THIS MODULE EXISTS SEPARATELY FROM `registry`, `iam` AND `ecs`:
#
#   1. CIRCULARITY. The reader verifies the live schema revision BEFORE the workload plan
#      is generated. If it lived in the workload plan it gates, it could never run first.
#      It is gated by its own two lifecycle flags (publication_bootstrap_enabled and
#      runtime_enabled), never by `deploy_workload`, and it consumes NOTHING that
#      `deploy_workload` gates — no task definition, no service, neither workload image
#      digest. It does consume `module.ecs.cluster_id`, and that is fine: `aws_ecs_cluster`
#      is ungated and exists in the foundation stage today, so every prerequisite of this
#      module can be satisfied while the workload stays dark.
#
#   2. BLAST RADIUS. The reader gets its OWN ECR repository and its OWN publisher role
#      here, rather than a third entry in registry's fixed two-repository map. That map
#      feeds `repository_arns`, which feeds BOTH the shared execution role's pull grant
#      (iam/main.tf) and the ci_publisher push grant — so a third entry there would have
#      silently widened two hardened identities with no diff in iam/main.tf at all.
#      Keeping the reader self-contained means a defect in the NEW, untested reader
#      publication path can never reach the api/worker repositories that run the
#      production path. registry/main.tf and iam/main.tf are untouched by this gate.
#
#   3. LIFECYCLE. Repository, log group, security group, roles and task definition live
#      together and are destroyable as a unit without touching registry/iam/ecs state.
#
# THE CONTROL THIS WHOLE STACK RESTS ON: ECS `ContainerOverride` exposes
# {name, command, environment, environmentFiles, cpu, memory, memoryReservation,
# resourceRequirements} — it has NO `entryPoint` member. The image pins a fixed exec-form
# ENTRYPOINT, and this task definition deliberately sets NEITHER `entryPoint` NOR
# `command`, so nothing shadows it. An override `command` therefore becomes argv to a
# program that rejects all argv (exit 50). That makes override prevention real here,
# where Gate 4I could only detect it after the fact.
# =====================================================================================

locals {
  family    = "${var.name_prefix}-revision-reader"
  repo_name = "${var.name_prefix}/revision-reader"
  log_group = "/ecs/${var.name_prefix}-revision-reader"
  container = "revision-reader"

  # STAGE A — publication bootstrap. ECR repository + lifecycle, and the publisher OIDC role.
  create_bootstrap      = var.publication_bootstrap_enabled ? 1 : 0
  create_oidc_publisher = var.publication_bootstrap_enabled && var.github_oidc_provider_arn != null ? 1 : 0

  # STAGE B — reader runtime. Log group, SG, egress, reader->RDS ingress, execution role,
  # runner role, task definition. `create_task` (task.tf) additionally requires bootstrap (so
  # the ECR repository the image is derived from exists) and a digest; the runtime_enabled
  # variable validations already enforce runtime => bootstrap and runtime => digest, so those
  # conjunctions are belt-and-braces, not the primary control.
  create_runtime     = var.runtime_enabled ? 1 : 0
  create_oidc_runner = var.runtime_enabled && var.github_oidc_provider_arn != null ? 1 : 0

  # Comprehension over the repository rather than `[0]`, so that supplying a digest while
  # the module is DISABLED yields null instead of erroring on an empty tuple. A plan that
  # crashes on an inert flag combination is a configuration landmine, not a safety control.
  image = one([
    for r in aws_ecr_repository.reader :
    "${r.repository_url}@${var.revision_reader_image_digest}"
    if var.revision_reader_image_digest != null
  ])
}

# --- dedicated ECR repository (STAGE A: publication bootstrap) -----------------------
resource "aws_ecr_repository" "reader" {
  count = local.create_bootstrap

  name                 = local.repo_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "AES256"
  }

  tags = merge(var.tags, { Name = local.repo_name })
}

resource "aws_ecr_lifecycle_policy" "reader" {
  count = local.create_bootstrap

  repository = aws_ecr_repository.reader[0].name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Expire untagged reader images beyond the 10 most recent."
      # COUNT-based, where `registry`'s policy is DAYS-based, and the divergence is
      # deliberate: reader images are published rarely and a digest may be pinned in tfvars
      # for months, so an age rule could expire the very image the task definition
      # references. A count rule cannot, because a pinned image is tagged.
      selection = { tagStatus = "untagged", countType = "imageCountMoreThan", countNumber = 10 }
      action    = { type = "expire" }
    }]
  })
}

# --- dedicated log group (STAGE B: reader runtime) -----------------------------------
# Its OWN group, not the shared migration group: the reader's stdout IS the verification
# evidence, so it must not interleave with another workload's diagnostics, and the runner
# role is then scoped to exactly this group rather than an /ecs/<prefix>-* prefix.
resource "aws_cloudwatch_log_group" "reader" {
  count = local.create_runtime

  name              = local.log_group
  retention_in_days = var.log_retention_days

  tags = merge(var.tags, { Name = local.log_group })
}

# --- dedicated security group: egress only, no ingress at all (STAGE B) --------------
resource "aws_security_group" "reader" {
  count = local.create_runtime

  name        = "${var.name_prefix}-revision-reader"
  description = "Revision reader task. Egress only: PostgreSQL to RDS, HTTPS for pull/secrets/logs."
  vpc_id      = var.vpc_id

  tags = merge(var.tags, { Name = "${var.name_prefix}-revision-reader" })
}

# No ingress rule exists, deliberately: nothing ever connects TO the reader.

resource "aws_vpc_security_group_egress_rule" "reader_to_postgres" {
  count = local.create_runtime

  security_group_id            = aws_security_group.reader[0].id
  referenced_security_group_id = var.rds_security_group_id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "Reader -> RDS PostgreSQL. The only data-plane egress."
}

resource "aws_vpc_security_group_egress_rule" "reader_https" {
  count = local.create_runtime

  security_group_id = aws_security_group.reader[0].id
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 443
  to_port           = 443
  ip_protocol       = "tcp"
  description       = "HTTPS for ECR pull, Secrets Manager injection and CloudWatch Logs (NAT baseline). VPC endpoints remain a separately authorized improvement."
}

# NOTE: no Redis egress. The reader has no cache dependency and must not acquire one.

# --- RDS ingress FROM the reader SG (Gate 4M defect fix, STAGE B) ---------------------
# The reader SG has egress to RDS:5432 (above), but security groups are STATEFUL and
# default-deny ingress, so the reader could not actually connect without a matching INGRESS
# rule on the RDS security group. As merged in Gate 4J that rule did not exist anywhere: the
# api/worker/migration ingress rules on the RDS SG are owned by the `ecs` module (for_each
# over its workload set) and the reader is not in that set, so a reader task would have failed
# at connect. This rule is owned HERE, not added to the ecs module's for_each, for two reasons:
# it is RUNTIME-gated (it must appear and disappear with the reader runtime lifecycle, which
# the ecs module does not track), and the reader module already receives `var.rds_security_group_id`,
# so no new cross-module edge is introduced. Exactly one rule, targeting exactly the RDS SG,
# sourced from exactly the reader SG, TCP 5432 only — never a CIDR.
resource "aws_vpc_security_group_ingress_rule" "rds_from_reader" {
  count = local.create_runtime

  security_group_id            = var.rds_security_group_id
  referenced_security_group_id = aws_security_group.reader[0].id
  from_port                    = 5432
  to_port                      = 5432
  ip_protocol                  = "tcp"
  description                  = "RDS PostgreSQL ingress from the revision-reader task SG only, TCP 5432. Owned by revision_reader (runtime-gated); api/worker/migration ingress is owned by ecs."
}
