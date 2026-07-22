# outputs.tf — non-sensitive registry references for downstream modules
#
# Plural, keyed by the two fixed logical repositories `api` and `worker`, so
# downstream `iam` and `ecs` can address each repository independently. ARNs, names,
# and URLs are configuration REFERENCES, not secret values, so they are not marked
# sensitive. No credential, authorization token, image tag, digest, manifest, scan
# finding, policy JSON, or full resource object is exposed.

output "repository_arns" {
  description = "Map of logical repository key (api|worker) -> ECR repository ARN. Consumed by iam to scope least-privilege pull/publish permissions per repository."
  value       = { for k, r in aws_ecr_repository.app : k => r.arn }
}

output "repository_names" {
  description = "Map of logical repository key (api|worker) -> ECR repository name (<name_prefix>/<key>)."
  value       = { for k, r in aws_ecr_repository.app : k => r.name }
}

output "repository_urls" {
  description = "Map of logical repository key (api|worker) -> ECR repository URL. Consumed by ecs, combined with a separately verified immutable digest, to pin task-definition images."
  value       = { for k, r in aws_ecr_repository.app : k => r.repository_url }
}
