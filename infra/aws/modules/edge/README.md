# Module: `edge`

## 1. Purpose
Public **web/SPA** edge for SIGNALNEST_STAGING: the compiled React/Vite SPA is
served from a **private** S3 origin through a CloudFront distribution over HTTPS,
with web DNS aliases pointing at CloudFront. This module defines resource bodies
but **applies nothing** — no infrastructure exists in AWS.

Certificate and hosted-zone **ownership** are architecturally resolved as
**consume, not create** (`aws-staging-iac-plan.md` §23, resolved 2026-07-22).

## 2. Owned resources (implemented here)
- `aws_s3_bucket` — private SPA origin bucket
- `aws_s3_bucket_public_access_block` — blocks all public access
- `aws_s3_bucket_server_side_encryption_configuration` — SSE (SSE-S3/AES256)
- `aws_s3_bucket_versioning` — versioning enabled
- `aws_s3_bucket_lifecycle_configuration` — expire noncurrent versions
- `aws_s3_bucket_ownership_controls` — bucket-owner-enforced (ACLs disabled)
- `aws_cloudfront_origin_access_control` — OAC (SigV4, always-sign)
- `aws_cloudfront_distribution` — the SPA distribution (see §6)
- `aws_s3_bucket_policy` — grants read to **only** this CloudFront distribution
- `aws_route53_record` web **`A`** alias → CloudFront
- `aws_route53_record` web **`AAAA`** alias → CloudFront

## 3. Consumed (NOT created) — via typed inputs
- **CloudFront ACM certificate** — supplied by ARN (`acm_certificate_arn`), must
  be in `us-east-1`. The module never creates, requests, validates, renews, or
  imports a certificate; there is **no** `aws_acm_certificate`,
  `aws_acm_certificate_validation`, DNS-validation record, ACM data source, or
  certificate provider alias.
- **Route 53 hosted zone** — supplied by id (`hosted_zone_id`). The module never
  creates or imports a zone; there is **no** `aws_route53_zone`, delegation, or
  nameserver record.

## 4. Deferred / explicit non-goals (NOT in this module)
- **ALB** and its listeners, security groups, and **certificate attachment** — the
  future ALB module consumes a regional ACM certificate ARN for its HTTPS listener.
- **API hostname and its Route 53 record / alias** — added only in a later,
  separately authorized cross-module integration pass, after the ALB exposes its
  DNS name and canonical hosted-zone id. No temporary ALB target or placeholder is
  invented here.
- ECS origins, an API CloudFront behavior, WAF, Lambda@Edge / CloudFront Functions,
  access-log delivery, observability integration, SPA object upload/deployment, and
  CloudFront invalidation.
- Application/runtime object storage — owned by the still-stub `storage` module.

## 5. Inputs
| Name | Type | Default | Notes |
| --- | --- | --- | --- |
| `name_prefix` | string | — (required) | Deterministic name prefix from the root. |
| `web_fqdn` | string | — (required) | One complete web FQDN (e.g. `app.staging.example.com`). Rejects scheme, path, query, fragment, empty labels, and malformed FQDNs. No real domain committed. |
| `hosted_zone_id` | string | — (required) | Existing Route 53 hosted-zone id (consumed). Statically validated as a plausible `Z…` id; **not** queried against AWS. No real id committed. |
| `acm_certificate_arn` | string | — (required) | Existing CloudFront ACM certificate ARN (consumed). Statically validated as an ACM cert ARN in `us-east-1`; **not** queried against AWS. No real ARN committed. |
| `price_class` | string | `PriceClass_100` | CloudFront price class; one of `PriceClass_100`, `PriceClass_200`, `PriceClass_All`. |

The module takes **no** `tags` input: the authoritative eight-tag common set is
applied to every taggable resource automatically by the root provider's
`default_tags` (`providers.tf`). This module adds only the conventional per-resource
`Name` tag. It declares **no** `provider "aws"` block or alias; the root AWS provider
and its committed version lock are inherited.

## 6. CloudFront SPA behavior (fixed policy, §23 decision 4)
- Private S3 **REST** origin via OAC / **SigV4** — no S3 website endpoint, no
  public-read ACL or bucket policy.
- Default root object `index.html`; viewer protocol `redirect-to-https`.
- Viewer certificate = the consumed ARN, `sni-only`, minimum `TLSv1.2_2021`.
- Compression enabled; allowed methods `GET`/`HEAD`/`OPTIONS`, cached `GET`/`HEAD`.
- No cookie, query-string, or arbitrary-header forwarding.
- TTLs: min `0`, default `3600`, max `86400` seconds.
- SPA fallback: origin **403 → `/index.html` (200)** and **404 → `/index.html`
  (200)**, error-caching TTL `0`.
- IPv6 enabled; no geographic restriction.

## 7. Non-sensitive outputs
`spa_bucket_id`, `spa_bucket_arn`, `cloudfront_distribution_id`,
`cloudfront_distribution_arn`, `cloudfront_domain_name`,
`cloudfront_hosted_zone_id`, `web_url`. No account id, secret, or certificate
material is exposed (the consumed certificate ARN is an input, not re-exported).

## 8. Security boundaries
TLS-only public ingress (HTTPS; HTTP redirected). The SPA origin bucket is private
with all public access blocked and ACLs disabled; only the specific CloudFront
distribution (matched by its ARN in the bucket policy `AWS:SourceArn` condition)
may read objects. The SPA build receives no backend secret. No domain, hosted-zone
id, or certificate ARN is committed; all are variable-driven at apply time.

## 9. Staging-only assumptions
Single distribution, `PriceClass_100` by default. Logical staging hostnames only;
the certificate and hosted zone are provisioned/owned outside this module and
referenced by value at apply time.

## 10. Status
Resource bodies authored and validated offline only. **No `tofu plan`/`apply`, no
AWS API call, no state, no asset upload, no CloudFront invalidation, and no
provisioning have occurred. Nothing exists in AWS.**

## 11. Owning tranche
INFRA-4 edge (web/SPA) resource-definition tranche. Remote-state bootstrap, ALB/API
DNS integration, and any `apply` remain later, separately authorized tranches
(`apply` is INFRA-9).
