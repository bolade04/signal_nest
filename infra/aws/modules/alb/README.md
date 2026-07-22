# Module: `alb` (documentation-only stub)

## 1. Purpose
Application Load Balancer fronting the private API service with HTTPS-only
ingress.

## 2. Planned AWS scope
Application Load Balancer, HTTPS listener(s), target group(s), listener rules,
health-check configuration against liveness `/health`.

## 3. Out of scope
ECS services/task definitions (`ecs`), certificates/DNS (`edge`), VPC/subnets/SGs
(`network`).

## 4. Planned upstream dependencies
`network` (subnets, security groups), `edge` (ACM certificate reference).

## 5. Planned inputs (names only, no values)
`vpc_id`, `public_subnet_ids`, `api_certificate_arn`, `api_target_port`,
`health_check_path`, `name_prefix`. This module **creates and owns** the ALB security
group, so it takes **no** security-group id input; it also takes **no** `tags` input — the
authoritative common tag set is applied by the root provider's `default_tags`.

## 6. Planned non-sensitive outputs (names only)
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
Single ALB, single API target group, desired count 1.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
