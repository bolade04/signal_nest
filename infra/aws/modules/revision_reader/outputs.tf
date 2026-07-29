# Outputs are the exact values the reader publication and invocation workflows need.
#
# The RESOURCE-DERIVED outputs are null when the module is disabled — `one()` rather than
# `[0]`, so a disabled module still evaluates instead of erroring on an empty list. Three
# are NOT: `task_definition_family`, `container_name` and `enabled` are computed from
# inputs and always return a value (`enabled` returning false is the whole point of it).
# Do not restate this as "every output is null when disabled": a consumer branching on
# `== null` to detect a disabled module would branch wrongly on those three.
#
# NOTHING SENSITIVE IS EXPORTED. No secret ARN is re-exported (the caller already holds
# it), no DSN, no account-derived value that is not already public in the plan.

output "repository_url" {
  description = "ECR repository URL for the reader image. Publication target."
  value       = one(aws_ecr_repository.reader[*].repository_url)
}

output "repository_arn" {
  description = "ECR repository ARN for the reader image."
  value       = one(aws_ecr_repository.reader[*].arn)
}

output "log_group_name" {
  description = "Dedicated reader log group. The invocation workflow reads the run's stream from here."
  value       = one(aws_cloudwatch_log_group.reader[*].name)
}

output "security_group_id" {
  description = "Reader task security group (egress only: 5432 to RDS, 443 for pull/secrets/logs)."
  value       = one(aws_security_group.reader[*].id)
}

output "execution_role_arn" {
  description = "Reader task execution role. The ONLY role the runner may pass."
  value       = one(aws_iam_role.reader_execution[*].arn)
}

output "publisher_role_arn" {
  description = "CI role that may push the reader image. Null unless a GitHub OIDC provider ARN was supplied."
  value       = one(aws_iam_role.reader_publisher[*].arn)
}

output "runner_role_arn" {
  description = "CI role that may invoke the reader task. Null unless a GitHub OIDC provider ARN was supplied."
  value       = one(aws_iam_role.reader_runner[*].arn)
}

output "task_definition_arn" {
  description = <<-EOT
    Reader task definition ARN, INCLUDING the revision suffix. The invocation workflow must
    pass this exact value: the runner role's ecs:RunTask grant is scoped to this revision,
    so a family-only reference would be denied rather than silently running something else.
    Null until an image digest is pinned.
  EOT
  value       = one(aws_ecs_task_definition.reader[*].arn)
}

output "task_definition_family" {
  description = "Reader task definition family. Provided for CloudTrail assertions, which record the SHORT form (family:revision), not the ARN."
  value       = local.family
}

output "container_name" {
  description = "Reader container name. Needed to locate the run's log stream (<prefix>/<container>/<task-id>)."
  value       = local.container
}

output "enabled" {
  description = "Whether this module materialized anything. Independent of deploy_workload by design — the reader must be able to run BEFORE the workload plan it gates."
  value       = var.enabled
}
