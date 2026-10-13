# Module: `alb`

## 1. Purpose
Internet-facing Application Load Balancer fronting the private API service with
HTTPS-only ingress. This module defines resource bodies; ~~**applies nothing** —
no infrastructure exists in AWS~~ *(struck 2026-10-13: accurate when written on 2026-07-22; stale since 2026-07-27 — see the notice)*.

> **Live-state notice (2026-10-13 read-only observation; `P6-GOV-1`).** The internet-facing application load balancer was `active` (created 2026-07-27T18:16Z) with one HTTPS:443 forward listener, the API target group (HTTP 8000, health check `/health`), the ALB security group and the private log bucket (versioning, SSE-S3, public-access block) — so the windowed resources of §9 were present and the closed-window state was not in effect; target health and load-balancer attributes were denied to the reading principal. CloudTrail shows the load-balancing service re-creating the ALB's network interface and service-managed address on 2026-10-05 and 2026-10-13 (service activity, not an operator change). API alias (§2): the consumed hosted zone held 5 record sets (unchanged since 2026-10-02), no record-set change event appeared from 2026-09-30T21:50Z to 2026-10-13, and the API name did not resolve publicly (NXDOMAIN) — consistent with no alias record, as recorded on 2026-10-03 in `docs/project-phase-6-plan.md` §4.6a; the records themselves could not be listed. Each fact here is a snapshot of 2026-10-13; current state is not recorded here. Root composition (`main.tf`) is configuration only and establishes neither provisioning nor deployment; the dated observations above are what establish existence, and nothing here establishes successful operation.

## 2. Owned resources (implemented here)
- `aws_security_group` — the ALB security group (public HTTPS 443 ingress only)
- `aws_vpc_security_group_ingress_rule` — TCP 443 from `0.0.0.0/0` (IPv4 only)
- `aws_lb` — internet-facing IPv4 application load balancer in the public subnets
- `aws_lb_target_group` — IP-target-type API target group (HTTP/1.1, port 8000,
  health check against liveness `/health`, matcher `200`)
- `aws_lb_listener` — HTTPS:443 listener (TLS terminates here) forwarding to the
  target group
- `aws_route53_record` — the **windowed API alias** (P6-INF-3): one `A` alias
  `api_fqdn` → this ALB inside the **consumed** hosted zone (`hosted_zone_id`, the
  same zone the `edge` module writes the web aliases into). `A` only because the ALB
  is IPv4-only; alias target = the planned `aws_lb` `dns_name`/`zone_id` (never a
  hard-coded ELB zone id), `evaluate_target_health = false`. Shares the ALB's
  `count`: created after the load balancer on open, destroyed before it on close,
  re-pointed automatically to each window's re-created ALB.
- `aws_s3_bucket` (+ ownership controls, public-access block, SSE-S3 encryption,
  versioning, bucket policy) — the dedicated private ALB log-delivery bucket;
  the ALB's **access and connection logging** both write into it (distinct
  `alb-access`/`alb-connection` prefixes). This is an infra-telemetry bucket
  owned here (mirroring the `observability` audit-bucket convention); `storage`
  still owns exactly one application bucket. Delivery is granted to the regional
  ELB service account resolved at plan time (`aws_elb_service_account` +
  `aws_caller_identity` data sources — no account id committed).

## 3. Out of scope
ECS services/task definitions and the API task SG + ALB↔API cross-SG rules (`ecs`),
certificate creation and the hosted ZONE (both consumed by value), the web DNS
aliases (`edge`), VPC/subnets (`network`), and WAF (deferred, §24.7). The API
alias RECORD is in scope since 2026-10-01 (§24.7 deferral lifted for a windowed
record); the root rejects `api_fqdn == web_fqdn` so the two modules never contend
for one record name.

## 4. Upstream dependencies
`network` (`vpc_id`, `public_subnet_ids`). The regional ACM certificate ARN is
consumed by value from a required root variable (`api_certificate_arn`), not from
another module. No hard dependency on `edge`.

## 5. Inputs (names only, no values)
`vpc_id`, `public_subnet_ids`, `api_certificate_arn`, `hosted_zone_id`, `api_fqdn`,
`api_target_port`, `health_check_path`, `name_prefix`, `enabled`. `hosted_zone_id`
and `api_fqdn` are consumed root values (never `edge` outputs — no `edge → alb`
edge exists); `api_fqdn` must be a bare multi-label hostname (statically validated)
that `api_certificate_arn` covers. This module **creates and owns** the ALB security
group, so it takes **no** security-group id input; it also takes **no** `tags` input — the
authoritative common tag set is applied by the root provider's `default_tags`.
`api_target_port` (8000) and `health_check_path` (`/health`) default to the locked
staging values.

## 6. Non-sensitive outputs (names only)
`alb_arn`, `alb_dns_name`, `alb_canonical_hosted_zone_id`, `https_listener_arn`,
`api_target_group_arn`, `alb_security_group_id`, `api_alias_record_name` (null
while the window is closed, like every other windowed output).

## 7. Security boundaries
Internet-facing, **HTTPS / 443 only** (consumed ACM cert `api_certificate_arn`, TLS policy
`ELBSecurityPolicy-TLS13-1-2-2021-06`); **no port 80, no HTTP listener, and no
HTTP-to-HTTPS redirect** — plain HTTP is refused. TLS terminates at the ALB; ALB → API is
plain HTTP on **port 8000 only**, inside the restricted VPC security-group path. This module
**creates and owns the ALB security group** and exposes it as output `alb_security_group_id`;
the two ALB↔API cross-SG rules (ALB SG egress → API SG :8000, API SG ingress ← ALB SG :8000)
are owned by the `ecs` module, which consumes `alb_security_group_id` and
`api_target_group_arn`. The ALB **never** consumes an ECS/API security-group id — the module
dependency is one-way `ecs -> alb` (cycle-free). Target-group health check uses the shallow
liveness `/health`; the dependency-aware `/readiness` is never the ALB health check. No ARN,
certificate id, or account id committed. See `docs/operations/aws-staging-iac-plan.md` §24 for
the full locked ALB contract.

## 8. Staging-only assumptions
Single ALB, single API target group. The ALB security group is created with **no
inline rules** so the provider removes AWS's implicit allow-all default egress; the
public 443 ingress is a standalone rule, and the ALB→API `:8000` egress rule is added
by the `ecs` module (no unrestricted ALB egress, no mixing of inline/standalone rules).
Access **and** connection logging are enabled into the module-owned private log
bucket (the §24.7 pre-live logging gate is resolved in configuration; ~~nothing is
provisioned~~ *(struck 2026-10-13: the log bucket and load balancer were observed on 2026-10-13 — see the §1 notice; whether access/connection logs are being delivered was not read)*). WAF remains deferred; the API Route 53 alias is authored as a windowed
record (~~nothing provisioned~~ *(2026-10-13: no record observed — §1 notice; record listing denied)*).

## 9. Status
Resource bodies authored and validated offline only (`tofu fmt`, `tofu init
-backend=false`, `tofu validate`). ~~The log-delivery bucket and both logging
blocks are configuration only. **No `tofu plan`/`apply`, no AWS API call, no
state, no certificate/WAF resource, no DNS record in AWS, no ECS target
registration, and no provisioning have occurred. Nothing exists in AWS.**~~ *(struck 2026-10-13: already inaccurate when this passage was last edited on 2026-10-01 — the load balancer, listener, target group, security group and log bucket had existed since 2026-07-27 (observed 2026-09-27 to 2026-10-02 and again on 2026-10-13); "no DNS record" held as recorded on 2026-10-03 and was consistent with the 2026-10-13 observation; target registration is UNKNOWN (target health denied); current state is not recorded here)*

## 10. Owning tranche
INFRA-4 alb resource-definition tranche; access/connection logging added by the
INFRA-4 pre-live tranche. Live remote-state bootstrap and any `apply` remain
later, separately authorized (`apply` is INFRA-9).

**Staging window (`enabled`, root `staging_window_active`):** the load balancer, HTTPS listener, API target group and API alias record exist only while `enabled = true`; the security group, its ingress rule and the private log bucket persist; windowed outputs are null while closed. See `docs/operations/staging-window.md` (§4 for the alias ordering and residual DNS caching).

**CI:** `window.tftest.hcl` (3 runs) and `api_alias.tftest.hcl` (6 runs: open/closed alias, A-only, binding to the planned ALB, malformed `api_fqdn`/`hosted_zone_id` rejected) run in the `revision-reader` job (`window_tests_alb`); this module therefore carries the root's byte-identical provider constraint in `versions.tf` and its cache is a classified member of `EXPECTED_CACHE_ROOTS`.
