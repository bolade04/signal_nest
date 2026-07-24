# Module: `alb`

## 1. Purpose
Internet-facing Application Load Balancer fronting the private API service with
HTTPS-only ingress. This module defines resource bodies but **applies nothing** —
no infrastructure exists in AWS.

## 2. Owned resources (implemented here)
- `aws_security_group` — the ALB security group (public HTTPS 443 ingress only)
- `aws_vpc_security_group_ingress_rule` — TCP 443 from `0.0.0.0/0` (IPv4 only)
- `aws_lb` — internet-facing IPv4 application load balancer in the public subnets
- `aws_lb_target_group` — IP-target-type API target group (HTTP/1.1, port 8000,
  health check against liveness `/health`, matcher `200`)
- `aws_lb_listener` — HTTPS:443 listener (TLS terminates here) forwarding to the
  target group
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
certificate/DNS creation (`edge`; the ALB cert is consumed by value), VPC/subnets
(`network`), WAF and the API Route 53 alias (deferred, §24.7).

## 4. Upstream dependencies
`network` (`vpc_id`, `public_subnet_ids`). The regional ACM certificate ARN is
consumed by value from a required root variable (`api_certificate_arn`), not from
another module. No hard dependency on `edge`.

## 5. Inputs (names only, no values)
`vpc_id`, `public_subnet_ids`, `api_certificate_arn`, `api_target_port`,
`health_check_path`, `name_prefix`. This module **creates and owns** the ALB security
group, so it takes **no** security-group id input; it also takes **no** `tags` input — the
authoritative common tag set is applied by the root provider's `default_tags`.
`api_target_port` (8000) and `health_check_path` (`/health`) default to the locked
staging values.

## 6. Non-sensitive outputs (names only)
`alb_arn`, `alb_dns_name`, `alb_canonical_hosted_zone_id`, `https_listener_arn`,
`api_target_group_arn`, `alb_security_group_id`.

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
bucket (the §24.7 pre-live logging gate is resolved in configuration; nothing is
provisioned). WAF and the API Route 53 alias remain deferred.

## 9. Status
Resource bodies authored and validated offline only (`tofu fmt`, `tofu init
-backend=false`, `tofu validate`). The log-delivery bucket and both logging
blocks are configuration only. **No `tofu plan`/`apply`, no AWS API call, no
state, no certificate/DNS/WAF resource, no ECS target registration, and no
provisioning have occurred. Nothing exists in AWS.**

## 10. Owning tranche
INFRA-4 alb resource-definition tranche; access/connection logging added by the
INFRA-4 pre-live tranche. Live remote-state bootstrap and any `apply` remain
later, separately authorized (`apply` is INFRA-9).
