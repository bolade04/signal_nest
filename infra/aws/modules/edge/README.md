# Module: `edge` (documentation-only stub)

## 1. Purpose
Public edge for SIGNALNEST_STAGING: DNS, TLS certificates, CDN, and the static
SPA origin.

## 2. Planned AWS scope
Route 53 records, ACM certificates (DNS-validated), CloudFront distribution, S3
web (SPA) origin bucket + origin access control.

## 3. Out of scope
ALB and target groups (`alb`), API/worker compute (`ecs`), any backend secret
(the SPA receives only `VITE_API_BASE_URL`).

## 4. Planned upstream dependencies
`network` (for origin/edge wiring where applicable).

## 5. Planned inputs (names only, no values)
`domain_name`, `hosted_zone_id`, `acm_certificate_arn` (or managed here),
`spa_bucket_name`, `name_prefix`, `tags`.

## 6. Planned non-sensitive outputs (names only)
`cloudfront_distribution_id`, `spa_bucket_id`, `certificate_arn` (reference),
`web_url`.

## 7. Security boundaries
TLS-only public ingress (HTTPS); HTTP redirected or refused. The SPA build
receives no backend secret. No domain, hosted-zone id, or certificate ARN is
committed; all are variable-driven at apply time.

## 8. Staging-only assumptions
Logical staging hostnames only; certificates issued/validated via Route 53.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
