# Module: `ecs` (implemented — offline-validated only, NOT root-composed)

## 1. Purpose
ECS on Fargate compute plane for SIGNALNEST_STAGING: the API service, the worker
service, and the **one-shot migration task definition** — implemented exactly per
the locked contract (`docs/operations/aws-staging-iac-plan.md` §26.2–§26.15).
Implemented and offline-validated only: **no cluster, task, service, rule, or log
group exists in AWS**; nothing is provisioned, planned live, deployed, or tested
live, and the module is **not** root-composed.

## 2. Implemented AWS scope
- `aws_ecs_cluster.this` — `<name_prefix>-cluster` (name overridable via the
  planned `cluster_name` input).
- **Three deterministic CloudWatch log groups** (`/ecs/<name_prefix>-{api,worker,
  migration}`, retention 30 days default) — **ecs owns these; `observability`
  consumes their outputs** and owns alarms (§26.9). `awslogs-create-group` is
  omitted: groups must pre-exist, so the execution role needs no
  `logs:CreateLogGroup`.
- **Three per-workload task security groups** (API, worker, migration — **not** one
  shared SG) created with zero inline rules, plus **every task-side cross-SG rule
  (both directions)** as standalone AWS provider 6.55 rule resources (§26.2):
  - **ALB↔API TCP 8000** — ALB SG egress → API task SG AND API task SG ingress ←
    ALB SG (both owned here; `alb` consumes no ECS output — §26.13).
  - **PostgreSQL TCP 5432** — egress from api/worker/**migration** task SGs and
    three separate ingress rules on the `data_sql`-owned RDS SG (§26.3).
  - **Redis TCP 6379** — egress from api/worker ONLY and two separate ingress
    rules on the `data_cache`-owned Redis SG; **the migration task is
    Redis-excluded** (executable basis: `apps/api/app/core/config.py:306-312` —
    migration is pinned to non-Redis backends, §26.3).
  - **TCP 443 IPv4 egress per task SG** — the §26.4 NAT staging baseline (ECR
    pull, Secrets Manager injection, CloudWatch Logs delivery, S3, approved LLM
    providers). No all-protocol egress, no IPv6, **no CIDR-based ingress
    anywhere**, no DNS SG rule; VPC endpoints remain separately authorized.
- **Three Fargate task definitions** (API / worker / migration): LINUX/**X86_64**
  (matches the CI `linux/amd64` build), `awsvpc`, 256 CPU / 512 MiB baseline,
  shared execution role + per-workload task role, non-root `10001:10001`,
  **read-only root filesystem + writable `/tmp`** (task-scoped volume — Fargate
  supports no tmpfs), per-workload `awslogs` config (region from a plan-time data
  source; no literal committed).
- **Two services** (API, worker): `FARGATE`, platform version **1.4.0**, private
  subnets, `assign_public_ip = false`, desired count 1 each, minimum-healthy
  100% / maximum 200%, **deployment circuit breaker with rollback**, **ECS Exec
  disabled**, autoscaling deferred. The API service attaches to the `alb`-owned
  target group (container `api`, port 8000, health path `/health` on the ALB
  side) with a **60s health-check grace period**.
- **Migration = task definition only, never a service** — no schedule, no
  run-task, no execution here; running it is a later, separately authorized
  one-shot step (INFRA-5/INFRA-9).

## 3. Two-image contract (§26.5)
Exactly **two** immutable, digest-pinned images: API task = the `api` repository
URL + `api_image_digest`; worker task = the `worker` repository URL +
`worker_image_digest`; **the migration task reuses the worker image digest** with
the locked command override `python -m app.db.migrate upgrade`. Input validation
rejects anything but `sha256:<64 hex>` — **no mutable tag and no `latest`** can
enter a task definition. No third image exists.

## 4. Upstream dependencies (all producer outputs EXIST; §26.15)
Producer → `ecs`: `network` (`vpc_id`, `private_subnet_ids`), `alb`
(`alb_security_group_id`, `api_target_group_arn`), `registry`
(`repository_urls`), `secrets` (`secret_arns`), `data_sql`
(`rds_security_group_id`), `data_cache` (`redis_security_group_id`), `iam`
(`execution_role_arn`, `api_task_role_arn`, `worker_task_role_arn`,
`migration_task_role_arn`). **No storage input** (§26.12 has no `storage -> ecs`
edge; `S3_BUCKET` arrives via the ordinary environment maps, §26.11). This module
consumes no `observability` output and none of its consumers feeds back into it —
`alb -> ecs`, `data_sql -> ecs`, `data_cache -> ecs`, `iam -> ecs`, and
`ecs -> observability` all stay one-way and acyclic.

## 5. Inputs (implemented)
`name_prefix`, `cluster_name` (optional override, deterministic default),
`vpc_id`, `private_subnet_ids`, `alb_security_group_id`, `api_target_group_arn`,
`rds_security_group_id`, `redis_security_group_id`, `repository_urls` (api|worker
map), `api_image_digest`/`worker_image_digest` (immutable `sha256:` digests —
statically validated), `execution_role_arn` + the three task-role ARNs,
`secret_arns` (the four container ARNs; per-workload subsets are applied
in-module), per-workload `api_environment`/`worker_environment`/
`migration_environment` maps (each enforcing the **§26.11 denylist** rejecting
`SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `LLM_API_KEY`, `S3_ACCESS_KEY_ID`,
`S3_SECRET_ACCESS_KEY`, `AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`,
`AWS_SESSION_TOKEN`), sizing/deployment baselines (`task_cpu` 256,
`task_memory` 512, desired counts 1/1, `log_retention_days` 30), and the
graceful-shutdown trio (below). This module takes **no** task-SG id input (it
creates the task SGs) and **no** `tags` input (root provider `default_tags`).

## 6. Outputs (implemented — exactly the planned interface)
`cluster_id`, `api_service_name`, `worker_service_name`, `migration_task_family`,
`api_task_security_group_id`, `log_group_names`, `log_group_arns` (maps keyed
api|worker|migration). `observability` consumes the log-group and service-name
outputs (§26.9/§26.15); `iam` never consumes the log-group ARNs (deterministic
prefix scoping, §26.8).

## 7. Secret injection (§26.6–§26.8)
Secrets enter tasks **only** through the task-definition `secrets` block
(`valueFrom` = Secrets Manager container ARN) resolved by the **shared execution
role** at task start — never plaintext env, build arg, image label, HCL value,
output, or state. Locked per-workload subsets applied in-module: **API/worker:**
`SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `LLM_API_KEY`; **migration:** three —
`REDIS_URL` excluded (granting it would be a G5 `ACTOR_SUBSET_EXCEEDED`
over-provision). Application task roles hold no Secrets Manager permission (owned
by `iam`). Secret **values** remain unpopulated and out-of-band (INFRA-6); this
module never reads, validates, or rotates one. AWS access-key env vars stay unset
(task-role credential chain).

## 8. Graceful shutdown (§26.10)
Exec-form PID 1 receives SIGTERM directly. The worker container `stopTimeout`
(default 30s) is precondition-enforced `>= worker_shutdown_grace_seconds`
(default 10s, mirroring `apps/api/app/core/config.py:175`) so in-flight jobs can
drain. The API container `stopTimeout` (default 70s, validated ≥ 60) accommodates
the ALB 60s deregistration delay. The one-shot migration container uses the ECS
default (30s). Fargate's 120s ceiling is validated on every input.

## 9. Scope boundaries (this tranche)
Implemented, but **uncomposed** (not in root `main.tf`), **unprovisioned**, and
**inactive**. No AWS access, no live `plan`/`apply`, no image built or pushed, no
secret value populated, no migration executed, no root-composition change, no
change to any producer module, and no `observability`/`cost` implementation (they
remain documentation-only stubs). Offline validation only: `tofu fmt`,
isolated-copy `tofu init -backend=false -lockfile=readonly` + `tofu validate`
with the committed root lockfile, AWS credentials suppressed, temporary artifacts
outside the repository. GitHub CI does not independently validate HCL (its five
jobs are application/integration checks); HCL correctness rests on the offline
harness, static validations, and review. **Known pre-live follow-ups remain
unresolved outside this tranche** (per the phase plan and §25/§26.14):
access/connection logging before any live plan/apply, rate-limiter
X-Forwarded-For handling, WAF/path restrictions for `/internal/system/*`,
storage access-log integration, DNS/ACM/target verification, VPC endpoints, and
autoscaling. INFRA-4 remains incomplete; INFRA-5 remains unstarted.

## 10. Owning tranche
Implemented by the INFRA-4 `ecs` module resource-definition tranche.
Root-composition (wiring the six-plus-one uncomposed modules), secret-value
population (INFRA-6), the build/deploy workflow (INFRA-5), any live
`plan`/`apply`/run-task (INFRA-9), and observability/cost implementation are
later, separately authorized tranches.
