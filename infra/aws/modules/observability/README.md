# Module: `observability` (documentation-only stub)

## 1. Purpose
Staging observability: log collection, metrics/alarms, and control-plane audit.

## 2. Planned AWS scope
CloudWatch log groups (one per service), CloudWatch metric alarms (service
health, error rate, DB/Redis saturation, budget thresholds), CloudTrail trail.

## 3. Out of scope
Cost budgets themselves (`cost`), application-level tracing (no OTLP in staging),
secret material (`secrets`).

## 4. Planned upstream dependencies
`ecs` (service/log-group naming), `cost` (budget alarm coordination).

## 5. Planned inputs (names only, no values)
`log_group_names`, `retention_days`, `alarm_thresholds`, `sns_topic_arn`,
`name_prefix`, `tags`.

## 6. Planned non-sensitive outputs (names only)
`log_group_arns`, `alarm_arns`, `trail_arn` (references).

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
