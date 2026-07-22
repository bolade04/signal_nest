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

# --- Network module outputs (INFRA-4 network tranche) ---
# Non-sensitive identifiers only. No account id, ARN, NAT public IP, or endpoint.

output "vpc_id" {
  description = "ID of the staging VPC (from the network module)."
  value       = module.network.vpc_id
}

output "public_subnet_ids" {
  description = "Public subnet IDs, ordered by sorted AZ name (from the network module)."
  value       = module.network.public_subnet_ids
}

output "private_subnet_ids" {
  description = "Private subnet IDs, ordered by sorted AZ name (from the network module)."
  value       = module.network.private_subnet_ids
}

# --- Edge module outputs (INFRA-4 edge tranche; web/SPA only) ---
# Non-sensitive identifiers only. The consumed certificate ARN and hosted-zone id
# are inputs and are NOT re-exported.

output "spa_bucket_id" {
  description = "Name/id of the private SPA origin bucket (from the edge module)."
  value       = module.edge.spa_bucket_id
}

output "cloudfront_distribution_id" {
  description = "ID of the SPA CloudFront distribution (from the edge module)."
  value       = module.edge.cloudfront_distribution_id
}

output "cloudfront_domain_name" {
  description = "CloudFront-assigned domain name of the SPA distribution (from the edge module)."
  value       = module.edge.cloudfront_domain_name
}

output "web_url" {
  description = "Public HTTPS URL of the SPA derived from the supplied web FQDN (from the edge module)."
  value       = module.edge.web_url
}

# --- ALB module outputs (INFRA-4 alb tranche) ---
# Non-sensitive identifiers/ARNs only. The consumed certificate ARN is an input and
# is NOT re-exported. `ecs` will consume alb_security_group_id + api_target_group_arn;
# a later authorized DNS pass consumes alb_dns_name + alb_canonical_hosted_zone_id.

output "alb_arn" {
  description = "ARN of the Application Load Balancer (from the alb module)."
  value       = module.alb.alb_arn
}

output "alb_dns_name" {
  description = "Public DNS name of the ALB (from the alb module; alias target for the future API Route 53 record)."
  value       = module.alb.alb_dns_name
}

output "alb_canonical_hosted_zone_id" {
  description = "Canonical hosted-zone id of the ALB (from the alb module; used by the future API alias record)."
  value       = module.alb.alb_canonical_hosted_zone_id
}

output "https_listener_arn" {
  description = "ARN of the ALB HTTPS:443 listener (from the alb module)."
  value       = module.alb.https_listener_arn
}

output "api_target_group_arn" {
  description = "ARN of the API target group (from the alb module; consumed by the future ecs module)."
  value       = module.alb.api_target_group_arn
}

output "alb_security_group_id" {
  description = "ID of the ALB-owned security group (from the alb module; consumed by the future ecs module, which owns both ALB<->API cross-SG rules)."
  value       = module.alb.alb_security_group_id
}
