# Module: `observability` (documentation-only stub)

## 1. Purpose
Staging observability: log collection, metrics/alarms, and control-plane audit.

## 2. Planned AWS scope
CloudWatch **metric filters and alarms** (service health, error rate, DB/Redis
saturation, budget thresholds) over the ECS-owned log groups, and a CloudTrail trail.
**The three ECS workload log groups are created and owned by `ecs`** (`/ecs/<name_prefix>-
{api,worker,migration}`, retention 30 days — see `docs/operations/aws-staging-iac-plan.md`
§26.9), **not** here; this module consumes their names/ARNs.

## 3. Out of scope
The ECS workload log groups themselves (owned by `ecs`), cost budgets themselves
(`cost`), application-level tracing (no OTLP in staging), secret material (`secrets`).

## 4. Planned upstream dependencies
Producer → `observability`: `ecs` (log-group names/ARNs + service names), `cost`
(budget-alarm coordination). This module consumes ECS log-group outputs and never creates
the ECS workload log groups, preserving `ecs -> observability` (no
`observability -> ecs -> observability` cycle).

## 5. Planned inputs (names only, no values)
`log_group_names`/`log_group_arns` (from `ecs`), `api_service_name`/`worker_service_name`
(from `ecs` — the exact planned `ecs` output names; there is no generic `service_names`
output), `alarm_thresholds`, `sns_topic_arn`, `name_prefix`. No `tags` input (provider `default_tags`); log-group
retention/encryption are owned by `ecs`.

## 6. Planned non-sensitive outputs (names only)
`alarm_arns`, `trail_arn` (references). Log-group ARNs are outputs of `ecs`, not of this
module.

## 7. Security boundaries
Secret values are never logged, printed, or attached as evidence (G5). Structured
JSON stdout/stderr shipped to CloudWatch; no log files. CloudTrail records
IAM/secret access events. No ARN or account id committed.

## 8. Staging-only assumptions
CloudWatch-based (no OTLP; `otlp_endpoint` absent in staging).

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche; observability readiness gating is INFRA-7.
