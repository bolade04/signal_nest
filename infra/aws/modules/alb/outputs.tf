# outputs.tf — non-sensitive ALB outputs for downstream modules (ecs) and operators
#
# Only non-sensitive identifiers/ARNs are exported. The consumed certificate ARN and
# hosted-zone id are inputs and are NOT re-exported. No account id or secret is
# exposed. `ecs` consumes `alb_security_group_id` and `api_target_group_arn`;
# `alb_dns_name` and `alb_canonical_hosted_zone_id` are consumed INSIDE this module
# by the API alias record (P6-INF-3) and re-exported for operator verification.

# Windowed outputs (docs/operations/staging-window.md): each resolves to null while
# the window is closed (`one()` over an empty count), so consumers can test for it.
output "alb_arn" {
  description = "ARN of the Application Load Balancer."
  value       = one(aws_lb.this[*].arn)
}

output "alb_dns_name" {
  description = "Public DNS name of the ALB (alias target for the future API Route 53 record)."
  value       = one(aws_lb.this[*].dns_name)
}

output "alb_canonical_hosted_zone_id" {
  description = "Canonical hosted-zone id of the ALB (used by the future API alias record; never a hard-coded global id)."
  value       = one(aws_lb.this[*].zone_id)
}

output "https_listener_arn" {
  description = "ARN of the HTTPS:443 listener."
  value       = one(aws_lb_listener.https[*].arn)
}

output "api_target_group_arn" {
  description = "ARN of the API target group (consumed by the ecs module for service target registration)."
  value       = one(aws_lb_target_group.api[*].arn)
}

output "alb_security_group_id" {
  description = "ID of the ALB-owned security group (consumed by the ecs module, which owns both ALB<->API cross-SG rules)."
  value       = aws_security_group.alb.id
}

output "api_alias_record_name" {
  description = "Name of the API Route 53 alias record (api_fqdn) while the window is open; null while closed (the record does not exist then)."
  value       = one(aws_route53_record.api[*].name)
}
