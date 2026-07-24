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

# CI image-publisher role ARN (GitHub OIDC → ECR push). null when the publisher
# role is uncreated (github_oidc_provider_arn not supplied). The operator sets
# this value as the `AWS_STAGING_PUBLISH_ROLE_ARN` GitHub environment variable
# for staging-publish.yml — not consumed by any module. Contains an account id,
# but role ARNs are configuration references (same disposition as the four
# above), so it is not marked sensitive.
output "ci_publisher_role_arn" {
  description = "ARN of the CI image-publisher role, or null when uncreated (github_oidc_provider_arn absent). Set by the operator as the staging-publish workflow's AWS_STAGING_PUBLISH_ROLE_ARN environment variable."
  value       = one(aws_iam_role.ci_publisher[*].arn)
}
