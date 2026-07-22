# outputs.tf — non-sensitive repository metadata outputs (INFRA-4 skeleton)
#
# Only safe, non-sensitive configuration echoes are exposed. No account id, ARN,
# endpoint, bucket name, domain, database identifier, secret, or non-existent
# module output is referenced here.

output "project_name" {
  description = "Logical project name (echo of var.project_name)."
  value       = var.project_name
}

output "environment" {
  description = "Deployment environment (echo of var.environment; staging-only)."
  value       = var.environment
}

output "aws_region" {
  description = "Selected AWS region (echo of var.aws_region)."
  value       = var.aws_region
}

output "name_prefix" {
  description = "Deterministic resource name prefix derived from project + environment."
  value       = local.name_prefix
}
