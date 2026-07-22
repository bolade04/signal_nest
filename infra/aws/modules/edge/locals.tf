# locals.tf — derived, non-sensitive edge identifiers
#
# Only a deterministic CloudFront origin id is derived here. No domain, ARN,
# hosted-zone id, account id, or secret is embedded.

locals {
  # Stable logical id for the single S3 SPA origin / default cache behavior.
  origin_id = "${var.name_prefix}-spa-origin"
}
