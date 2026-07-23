# outputs.tf — the four ECS-consumed role ARNs (aws-staging-iac-plan.md §26.8)
#
# Exactly the four role ARNs the future `ecs` module consumes (`iam -> ecs`,
# one-way). ARNs are configuration REFERENCES, not secret values, so none is
# marked sensitive. No policy JSON, account id, credential, or session material
# is exposed.

output "execution_role_arn" {
  description = "ARN of the shared ECS task execution role (ECR pull, prefix-scoped log delivery, referenced-secret retrieval). Consumed by every future ECS task definition's execution_role_arn."
  value       = aws_iam_role.execution.arn
}

output "api_task_role_arn" {
  description = "ARN of the API application task role (application-bucket S3 only). Consumed by the future ECS API task definition's task_role_arn."
  value       = aws_iam_role.api_task.arn
}

output "worker_task_role_arn" {
  description = "ARN of the worker application task role (application-bucket S3 only). Consumed by the future ECS worker task definition's task_role_arn."
  value       = aws_iam_role.worker_task.arn
}

output "migration_task_role_arn" {
  description = "ARN of the intentionally empty migration task role (no attached policy). Consumed by the future ECS migration one-shot task definition's task_role_arn."
  value       = aws_iam_role.migration_task.arn
}
