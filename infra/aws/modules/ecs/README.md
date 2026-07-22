# Module: `ecs` (documentation-only stub)

## 1. Purpose
ECS on Fargate compute plane: the API service, the worker service, and the
one-shot migration run-task.

## 2. Planned AWS scope
ECS cluster; API service (desired count 1, port 8000); worker service (desired
count 1, no port); migration run-task (`python -m app.db.migrate upgrade`, one-shot,
never a service); **three** task security groups (API, worker, migration) and every
task-side cross-SG rule; **three** deterministic CloudWatch log groups
(`/ecs/<name_prefix>-{api,worker,migration}`); task definitions with non-root UID/GID
10001, read-only root filesystem + writable `/tmp`, exec-form commands, X86_64/Linux,
Fargate platform version 1.4.0, `assign_public_ip` disabled, worker grace period
≥ `WORKER_SHUTDOWN_GRACE_SECONDS`. See `docs/operations/aws-staging-iac-plan.md` §26
for the full locked contract.

## 3. Out of scope
ALB (`alb`), image registry (`registry`), IAM roles (`iam`), secret material
(`secrets`), data stores (`data_sql`/`data_cache`), buckets (`storage`).

## 4. Planned upstream dependencies
`network`, `alb`, `iam`, `secrets`, `data_sql`, `data_cache`, `storage`,
`registry`.

## 5. Planned inputs (names only, no values)
`cluster_name`, `vpc_id`, `private_subnet_ids`, `alb_security_group_id`,
`api_target_group_arn`, `rds_security_group_id` (from `data_sql`),
`redis_security_group_id` (from `data_cache`), `api_image_digest`,
`worker_image_digest` (migration reuses the worker image), `execution_role_arn`,
`api_task_role_arn`, `worker_task_role_arn`, `migration_task_role_arn`,
`secret_arns` (per-workload subsets, ARNs only), per-workload ordinary environment,
`name_prefix`. This module **creates and owns** the API/worker/migration task security
groups and every task-side cross-SG rule, so it takes **no** task security-group id
input; it takes **no** `tags` input (provider `default_tags`). Per-workload `map(string)`
env inputs enforce a denylist precondition rejecting `SECRET_KEY`, `DATABASE_URL`,
`REDIS_URL`, `LLM_API_KEY`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`.

## 6. Planned non-sensitive outputs (names only)
`cluster_id`, `api_service_name`, `worker_service_name`, `migration_task_family`,
`api_task_security_group_id`, `log_group_names`/`log_group_arns` (three groups,
consumed by `observability`).

## 7. Security boundaries
Tasks run in `network` private subnets, `assign_public_ip` disabled, no public port
8000. This module creates and owns **three** task security groups (API, worker,
migration) and **every task-side standalone cross-SG rule** (AWS provider 6.55.0
`aws_vpc_security_group_ingress_rule`/`aws_vpc_security_group_egress_rule`):
- **ALB↔API:** ALB SG egress → API task SG **TCP 8000**; API task SG ingress ← ALB SG
  **TCP 8000**. No ALB rule targets the worker or migration SG.
- **PostgreSQL 5432:** API, worker, and migration task SGs egress → RDS SG; RDS SG
  ingress ← each of the three SGs (destination SG owned by `data_sql`, consumed here).
- **Redis 6379:** API and worker task SGs only; Redis SG ingress ← API and worker SGs
  (destination SG owned by `data_cache`). **Migration has no Redis access** — it is
  pinned with non-Redis backends so `Settings()` does not require `REDIS_URL`.

It **consumes** `alb_security_group_id`, `api_target_group_arn`, `rds_security_group_id`,
and `redis_security_group_id`, and never exposes a task SG id back to `alb`/`data_sql`/
`data_cache`, giving one-way, cycle-free `alb -> ecs`, `data_sql -> ecs`, `data_cache -> ecs`.
Public-HTTPS egress (ECR, Secrets Manager, CloudWatch Logs, S3, LLM providers) is **TCP
443 IPv4 via NAT** — not SG-referenced (no repository-owned destination SG); no
all-protocol/`0.0.0.0/0` egress, no IPv6, and no AmazonProvidedDNS SG rule (SGs cannot
filter the Route 53 Resolver). Secrets injected only via the task-definition `secrets`
block (`valueFrom` a Secrets Manager ARN, per-workload subset) — never plaintext env,
build arg, or image label; API/worker carry `SECRET_KEY`/`DATABASE_URL`/`REDIS_URL`/
`LLM_API_KEY`, migration carries `SECRET_KEY`/`DATABASE_URL`/`LLM_API_KEY` only. `S3`
access-key vars stay unset (task-role chain). This module owns three deterministic
CloudWatch log groups (retention 30 days; `awslogs-create-group` disabled); `observability`
consumes their outputs. Two digest-pinned images (`@sha256:`): API task = API image;
worker task and migration task = worker image; first deploy pins exact SHA
`3aadb8a1da0f26ffd183a4b05161747038d5957c` (G4, later). All three feature flags remain
`false` in task env.

## 8. Staging-only assumptions
Single API + single worker task, each 0.25 vCPU / 0.5 GB; short-lived migration
task. Replicas never migrate.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
