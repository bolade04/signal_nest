# Module: `observability` (implemented — offline-validated only, root-composed)

## 1. Purpose
Staging observability per `docs/operations/aws-staging-iac-plan.md` §6/§14 and
§26.9: **metric filters and alarms** over the ecs-owned workload log groups and
AWS service metrics, plus the **CloudTrail audit trail**. Implemented and
offline-validated only: **no filter, alarm, dashboard, trail, or bucket exists
in AWS**; nothing is provisioned, deployed, or live. The module **is
root-composed** (wired in `infra/aws/main.tf` by the INFRA-4 root-composition
tranche; composition is configuration only). The INFRA-7 readiness tranche
added the capability gate/override filters, alarms, and the canary dashboard
(§3/§5).

## 2. Log-group ownership ruling (evidence-settled)
**`ecs` owns the three workload log groups; this module only consumes their
names.** Evidence: §26.9 ("ecs creates and owns three deterministic CloudWatch
Logs groups … `observability` consumes the ECS log-group outputs for metric
filters/alarms and does not create the ECS workload log groups") and the MERGED
`ecs` module, which implements `aws_cloudwatch_log_group.workload` for
api/worker/migration and outputs `log_group_names`/`log_group_arns`. This
module therefore creates **no log group**, sets no retention/encryption (both
ecs-owned), and attaches metric filters to the supplied names. The future
dependency graph is acyclic: `ecs -> observability` only, and observability is
a **sink** — no module consumes its outputs, so no
`observability -> ecs`/`iam` edge can form (`iam` scopes its Logs policy by
deterministic name prefix, never by these resources — §26.8).

## 3. Implemented AWS scope (29 resource instances)
- **3 error metric filters** (`aws_cloudwatch_log_metric_filter.errors`) — one
  per ecs-owned log group, pattern **`{ $.severity = "ERROR" }`**
  (evidence-backed: the application JSON formatter writes
  `"severity": record.levelname` — `apps/api/app/core/logging.py:65`), emitting
  `<workload>_error_count` into the deterministic custom namespace
  `<name_prefix>/logs` (value 1, default 0, unit Count).
- **15 alarms** (`aws_cloudwatch_metric_alarm.*`) — inventory in §5 (12
  caller-thresholded + 3 INFRA-7 fixed-threshold capability signals).
- **3 capability metric filters** (`aws_cloudwatch_log_metric_filter.capability`,
  INFRA-7) over the ecs-owned **API** log group: `opportunity_feedback_gate_failed`,
  override-driven ALLOWED gate decisions (`decided_by = "workspace_override"`),
  and `workspace_capability_override_set|_clear` mutations — evidence-backed
  against the application's structured `event` JSON key; no tenant identifier
  enters any pattern, metric, or dimension. The FULL override audit trail
  (actor/reason/state) lives in the application's Postgres `audit_logs` table
  behind the operator read plane; these filters give CloudWatch the paging
  signal.
- **1 canary observability dashboard** (`aws_cloudwatch_dashboard.canary`,
  INFRA-7) — four widgets over the deterministic metric names/dimensions above;
  a **disclosed supersede** of this module's earlier "no dashboard (none
  documented)" exclusion, authorized by INFRA-7's phase-plan expected
  repository area "IaC (dashboards/alarms)".
- **CloudTrail audit trail** (`aws_cloudtrail.audit`, `<name_prefix>-audit`):
  single-region, management events only (control-plane + IAM/secret access,
  §14), `include_global_service_events`, **log-file validation enabled**,
  default SSE (a customer-managed KMS design remains separately authorized),
  no data events, no organization trail, no CloudWatch Logs delivery.
- **Dedicated private audit bucket** (+ ownership controls, all-four
  public-access block, SSE-S3, versioning, policy): `bucket_prefix
  "<name_prefix>-audit-"` (edge-module precedent — no global name committed),
  `BucketOwnerEnforced`, TLS-only deny, and CloudTrail-service-only
  `GetBucketAcl`/`PutObject` statements scoped by `aws:SourceArn` to this exact
  trail (confused-deputy guard) and `bucket-owner-full-control`. This is an
  **audit** bucket owned by observability per §6 ("CloudTrail"); `storage` owns
  **application** buckets. Account/region/partition enter only through
  plan-time data sources — nothing is committed.

**Not created (with reasons):** no SNS topic/notification destination
(`sns_topic_arn` is an optional external input); no composite alarm, anomaly
detector, or metric math (none documented — the single INFRA-7 canary dashboard
above is now the one documented dashboard); no log group (ecs-owned);
no budget alarms (**`cost` module scope**, §15); **no ALB-dimension alarms** —
the `alb` module exposes no `arn_suffix` outputs and changing `alb` is outside
this tranche (a later, separately authorized `alb` output addition is required
first).

## 4. Inputs / outputs (exactly the planned pinned interface)
Inputs: `log_group_names` + `log_group_arns` (maps, from `ecs` — §26.15 pins),
`api_service_name` + `worker_service_name` (from `ecs`), `alarm_thresholds`
(typed object, **required — the plan documents alarm categories, not values, so
thresholds are deliberately caller-supplied and validated**, percent 1–100,
bytes/counts positive), `sns_topic_arn` (optional, nullable — null means no
alarm actions), `name_prefix`. No `tags` input (root `default_tags`).
Outputs: **`alarm_arns`** (map of the 15 alarm ARNs), **`trail_arn`**, and
**`canary_dashboard_name`** (INFRA-7) — nothing else.

**Deterministic dimensions (no new graph edges):** ClusterName
`<name_prefix>-cluster` (the ecs module's deterministic default),
DBInstanceIdentifier `<name_prefix>-postgres` (the data_sql local),
CacheClusterId `<name_prefix>-redis-001` (the data_cache replication group +
single-node member suffix). This mirrors the §26.8 deterministic-name pattern;
root composition must keep those deterministic names (and the data_cache
single-node default) or the dimensions will not match.

## 5. Alarm inventory (15 — 12 caller-supplied thresholds + 3 INFRA-7 fixed)
| Alarm | Namespace / metric | Stat | Period × eval | Operator | Dimensions | Missing data |
| --- | --- | --- | --- | --- | --- | --- |
| `api/worker/migration-log-errors` (×3) | `<prefix>/logs` / `<w>_error_count` | Sum | 300s × 1 | >= `log_error_count_per_period` | (filter metric) | `notBreaching` — the filter emits only on matches; no datapoint genuinely means no errors (health coverage comes from the breaching service alarms) |
| `api/worker-cpu-high` (×2) | AWS/ECS / CPUUtilization | Average | 300s × 3 | >= `ecs_cpu_high_percent` | ClusterName + ServiceName | `breaching` (fail-closed: a metric gap must not look healthy) |
| `api/worker-memory-high` (×2) | AWS/ECS / MemoryUtilization | Average | 300s × 3 | >= `ecs_memory_high_percent` | ClusterName + ServiceName | `breaching` |
| `rds-cpu-high` | AWS/RDS / CPUUtilization | Average | 300s × 3 | >= `rds_cpu_high_percent` | DBInstanceIdentifier | `breaching` |
| `rds-storage-low` | AWS/RDS / FreeStorageSpace | Average | 300s × 3 | <= `rds_free_storage_low_bytes` | DBInstanceIdentifier | `breaching` |
| `rds-memory-low` | AWS/RDS / FreeableMemory | Average | 300s × 3 | <= `rds_freeable_memory_low_bytes` | DBInstanceIdentifier | `breaching` |
| `redis-cpu-high` | AWS/ElastiCache / CPUUtilization | Average | 300s × 3 | >= `redis_cpu_high_percent` | CacheClusterId | `breaching` |
| `redis-memory-high` | AWS/ElastiCache / DatabaseMemoryUsagePercentage | Average | 300s × 3 | >= `redis_memory_high_percent` | CacheClusterId | `breaching` |
| `gate-failed` (INFRA-7) | `<prefix>/logs` / `api_gate_failed_count` | Sum | 300s × 1 | >= **1 (fixed)** | (filter metric) | `notBreaching` (match-only) |
| `override-enable` (INFRA-7) | `<prefix>/logs` / `api_override_enable_count` | Sum | 300s × 1 | >= **1 (fixed)** | (filter metric) | `notBreaching` (match-only) |
| `override-mutation` (INFRA-7) | `<prefix>/logs` / `api_override_mutation_count` | Sum | 300s × 1 | >= **1 (fixed)** | (filter metric) | `notBreaching` (match-only) |

The three INFRA-7 capability alarms use an **intrinsic fixed threshold of 1**:
they watch discrete audit events where any occurrence is the signal (the
capability plane is dark until the separately authorized canary), so they are
deliberately NOT part of the caller-supplied `alarm_thresholds` contract.

Alarm and OK actions are `[sns_topic_arn]` when supplied, otherwise empty; no
destination resource is created or assumed.

## 6. Security and cost considerations
Secret values are never logged, filtered on, or attached as evidence (G5); the
error filter matches only the `severity` field. The audit bucket is private,
TLS-only, service-principal-scoped, versioned, and SSE-S3-encrypted; CloudTrail
log-file validation provides tamper evidence. Costs are bounded: one
single-region management-events trail (first copy of management events is
free), 15 alarms, 6 filters, one dashboard (~$3/month), and minimal S3 audit
storage — no Container Insights, no data events. No IAM resource or policy is
created or modified here. The capability filter patterns match only
`event`/`outcome`/`decided_by` fields — never a tenant identifier or secret.

## 7. Staging-only assumptions
Single deployment per account/region for the deterministic dimension names;
CloudWatch-based (no OTLP; `otlp_endpoint` absent in staging); single-node
Redis (`-001` member) per the data_cache default.

## 8. Scope boundaries (this tranche)
Implemented and **root-composed** (wired in root `main.tf`), but
**unprovisioned** and **inactive** — nothing has been deployed or made live.
No AWS access and no live `tofu` operation have occurred. Offline validation only:
`tofu fmt`, external-harness `tofu init -backend=false -lockfile=readonly` +
`tofu validate` with the committed root lockfile (aws 6.55.0), AWS credentials
suppressed, artifacts outside the repository. GitHub CI does not independently
validate HCL. **Future root-composition requirements:** wire the four ecs
outputs into this module's inputs, supply `alarm_thresholds` (and optionally an
SNS topic ARN), and keep the deterministic cluster/DB/Redis names — all done by
the merged root composition. ALB-dimension alarms still require new `alb`
`arn_suffix` outputs (**separately authorized** — deliberately NOT exercised by
the INFRA-7 tranche, whose generic "dashboards/alarms" area does not name `alb`
changes).

## 9. Status
Executable HCL, offline-validated. No resource created in AWS; no live plan or
apply has ever run.

## 10. Owning tranche
Implemented by the INFRA-4 `observability` module resource-definition tranche;
root-composed by the INFRA-4 root-composition tranche; capability filters,
alarms, and the canary dashboard added by the INFRA-7 readiness tranche (see
`docs/operations/aws-staging-observability-incident-readiness.md`). Threshold
selection, SNS destination provisioning, and live `plan`/`apply` (INFRA-9)
remain later, separately authorized tranches.
