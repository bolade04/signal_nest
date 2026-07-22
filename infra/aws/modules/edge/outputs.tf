# outputs.tf — non-sensitive web/SPA edge outputs
#
# Only non-sensitive identifiers are exported. The consumed certificate ARN is an
# input and is NOT re-exported. No account id or secret is exposed.

output "spa_bucket_id" {
  description = "Name/id of the private SPA origin bucket."
  value       = aws_s3_bucket.spa.id
}

output "spa_bucket_arn" {
  description = "ARN of the private SPA origin bucket."
  value       = aws_s3_bucket.spa.arn
}

output "cloudfront_distribution_id" {
  description = "ID of the SPA CloudFront distribution."
  value       = aws_cloudfront_distribution.spa.id
}

output "cloudfront_distribution_arn" {
  description = "ARN of the SPA CloudFront distribution."
  value       = aws_cloudfront_distribution.spa.arn
}

output "cloudfront_domain_name" {
  description = "CloudFront-assigned domain name of the SPA distribution (alias target)."
  value       = aws_cloudfront_distribution.spa.domain_name
}

output "cloudfront_hosted_zone_id" {
  description = "CloudFront's canonical hosted-zone id used by the web alias records."
  value       = aws_cloudfront_distribution.spa.hosted_zone_id
}

output "web_url" {
  description = "Public HTTPS URL of the SPA (derived from the supplied web FQDN)."
  value       = "https://${var.web_fqdn}"
}
