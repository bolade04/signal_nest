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
`vpc_id`, `public_subnet_ids`, `alb_security_group_id`, `certificate_arn`,
`api_target_port`, `health_check_path`, `name_prefix`, `tags`.

## 6. Planned non-sensitive outputs (names only)
`alb_arn`, `alb_dns_name`, `https_listener_arn`, `api_target_group_arn`.

## 7. Security boundaries
HTTPS-only (ACM); ALB → API on port 8000 only. Target-group health check uses
liveness `/health`; readiness `/readiness` is the orchestrator signal, never the
ALB health check. No ARN or certificate id committed.

## 8. Staging-only assumptions
Single ALB, single API target group, desired count 1.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
