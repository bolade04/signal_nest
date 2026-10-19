# Module: `edge`

## 1. Purpose
Public **web/SPA** edge for SIGNALNEST_STAGING: the compiled React/Vite SPA is
served from a **private** S3 origin through a CloudFront distribution over HTTPS,
with web DNS aliases pointing at CloudFront. This module defines resource bodies;
~~**applies nothing** — no infrastructure exists in AWS~~ *(struck 2026-10-13: accurate when written on 2026-07-22; stale since the foundation apply from 2026-07-27 — see the notice)*.

> **Live-state notice (2026-10-13 read-only observation; `P6-GOV-1`).** The private SPA origin bucket was observed (created 2026-07-27; versioning, SSE-S3, all four public-access-block controls and the noncurrent-version expiry rule exactly as §2 authors them; the 2026-07-28 reference refresh is recorded in `../../operator-closure-contract.json`). The CloudFront distribution and the ACM certificate were **not** readable — listing denied to the reading principal, as in every earlier dated read, so their existence by API has never been observed from this repository's evidence. The consumed hosted zone was read by id (5 record sets, unchanged since 2026-10-02) but its records could not be listed, so the web `A`/`AAAA` aliases are unverified; a public DNS probe of the zone apex returned only name-server records. Each fact here is a snapshot of 2026-10-13; current state is not recorded here. Root composition (`main.tf`) is configuration only and establishes neither provisioning nor deployment; the dated observations above are what establish existence, and nothing here establishes successful operation. *(Added 2026-10-18, P6-CI-6: §2 now authors SSE-KMS with a module-owned key; "as §2 authors them" above refers to the configuration authored when the observation was made (SSE-S3). The configuration change is not applied; nothing here establishes the live bucket's current encryption.)*

Certificate and hosted-zone **ownership** are architecturally resolved as
**consume, not create** (`aws-staging-iac-plan.md` §23, resolved 2026-07-22).

## 2. Owned resources (implemented here)
- `aws_s3_bucket` — private SPA origin bucket
- `aws_s3_bucket_public_access_block` — blocks all public access
- `aws_s3_bucket_server_side_encryption_configuration` — SSE ~~(SSE-S3/AES256)~~ *(2026-10-18, P6-CI-6: SSE-KMS with the module's SPA key below, S3 Bucket Key on; IaC finding AWS-0132)*
- `aws_kms_key` + `aws_kms_alias` (`alias/<name_prefix>/spa`) — *(added 2026-10-18, P6-CI-6)* the SPA origin's customer-managed key: symmetric, rotation on, single-region, 30-day deletion window, lockout safety check on. Its policy is the AWS default account-root statement (IAM delegation) plus `kms:Decrypt` for `cloudfront.amazonaws.com` bound by `AWS:SourceArn` to **this** distribution — the statement CloudFront OAC needs to read SSE-KMS objects, narrowed to decrypt because the distribution only serves `GET`/`HEAD`/`OPTIONS`.
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
- Application/runtime object storage — owned by the ~~still-stub~~ `storage` module *(implemented and root-composed since 2026-07-24 — `../storage/README.md`, `../../main.tf`)*.

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
may read objects. *(Added 2026-10-18, P6-CI-6:)* objects are encrypted at rest with the module's own customer-managed key, which only that distribution (by `AWS:SourceArn`) may use, and only to decrypt; a future SPA deployer (`P6-INF-2`) will need `kms:GenerateDataKey` on the key through its own IAM policy. The SPA build receives no backend secret. No domain, hosted-zone
id, or certificate ARN is committed; all are variable-driven at apply time.

## 9. Staging-only assumptions
Single distribution, `PriceClass_100` by default. Logical staging hostnames only;
the certificate and hosted zone are provisioned/owned outside this module and
referenced by value at apply time.

## 10. Status
Resource bodies authored and validated offline only. ~~**No `tofu plan`/`apply`, no
AWS API call, no state, no asset upload, no CloudFront invalidation, and no
provisioning have occurred. Nothing exists in AWS.**~~ *(struck 2026-10-13: accurate when written on 2026-07-22; the bucket clause is stale since 2026-07-27 (observed 2026-10-13); the distribution, certificate and alias records remain **unobserved** by API (reads denied) — not known absent, not known present; whether any SPA asset was uploaded is UNKNOWN (the SPA bucket's objects were not listed); current state is not recorded here)*

## 11. Owning tranche
INFRA-4 edge (web/SPA) resource-definition tranche. Remote-state bootstrap, ALB/API
DNS integration, and any `apply` remain later, separately authorized tranches
(`apply` is INFRA-9).
