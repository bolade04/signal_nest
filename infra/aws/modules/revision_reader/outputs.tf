# Outputs are the exact values the reader publication and invocation workflows need.
#
# The RESOURCE-DERIVED outputs are null when their stage is disabled — `one()` rather than
# `[0]`, so a disabled stage still evaluates instead of erroring on an empty list
# (bootstrap-stage: repository_url/repository_arn/publisher_role_arn; runtime-stage:
# log_group_name/security_group_id/execution_role_arn/runner_role_arn/task_definition_arn).
# FOUR are NOT null-gated: `task_definition_family`, `container_name`,
# `publication_bootstrap_enabled` and `runtime_enabled` are computed from inputs and always
# return a value (the two enablement flags returning false is the whole point of them).
# Do not restate this as "every output is null when disabled": a consumer branching on
# `== null` to detect a disabled stage would branch wrongly on those four.
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

output "publication_bootstrap_enabled" {
  description = "Whether the publication-bootstrap stage (ECR repository + publisher role) is materialized. Independent of deploy_workload by design."
  value       = var.publication_bootstrap_enabled
}

output "runtime_enabled" {
  description = "Whether the reader runtime stage (log group, SG, ingress, execution/runner roles, task definition) is materialized. Independent of deploy_workload by design."
  value       = var.runtime_enabled
}

# NOTE on lifecycle-gated outputs: the resource-derived outputs above (repository_url,
# repository_arn -> bootstrap stage; log_group_name, security_group_id, execution_role_arn,
# runner_role_arn, task_definition_arn -> runtime stage) each use one(<resource>[*]) and so
# return null exactly when their own resource's count is 0 — the output gating follows the
# resource lifecycle automatically, with no unsafe [0] index. publisher_role_arn is bootstrap;
# runner_role_arn is runtime; they are no longer coupled.
