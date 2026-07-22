# outputs.tf — non-sensitive ALB outputs for downstream modules (ecs, future DNS)
#
# Only non-sensitive identifiers/ARNs are exported. The consumed certificate ARN is
# an input and is NOT re-exported. No account id or secret is exposed. `ecs` consumes
# `alb_security_group_id` and `api_target_group_arn`; a later, separately authorized
# DNS pass consumes `alb_dns_name` and `alb_canonical_hosted_zone_id`.

output "alb_arn" {
  description = "ARN of the Application Load Balancer."
  value       = aws_lb.this.arn
}

output "alb_dns_name" {
  description = "Public DNS name of the ALB (alias target for the future API Route 53 record)."
  value       = aws_lb.this.dns_name
}

output "alb_canonical_hosted_zone_id" {
  description = "Canonical hosted-zone id of the ALB (used by the future API alias record; never a hard-coded global id)."
  value       = aws_lb.this.zone_id
}

output "https_listener_arn" {
  description = "ARN of the HTTPS:443 listener."
  value       = aws_lb_listener.https.arn
}

output "api_target_group_arn" {
  description = "ARN of the API target group (consumed by the ecs module for service target registration)."
  value       = aws_lb_target_group.api.arn
}

output "alb_security_group_id" {
  description = "ID of the ALB-owned security group (consumed by the ecs module, which owns both ALB<->API cross-SG rules)."
  value       = aws_security_group.alb.id
}
