# Module: `ecs` (documentation-only stub)

## 1. Purpose
ECS on Fargate compute plane: the API service, the worker service, and the
one-shot migration run-task.

## 2. Planned AWS scope
ECS cluster; API service (desired count 1, port 8000); worker service (desired
count 1, no port); migration run-task (`python -m app.db.migrate`, one-shot);
task definitions with non-root UID/GID 10001, read-only root filesystem + writable
`/tmp`, exec-form commands, worker grace period ≥ `WORKER_SHUTDOWN_GRACE_SECONDS`.

## 3. Out of scope
ALB (`alb`), image registry (`registry`), IAM roles (`iam`), secret material
(`secrets`), data stores (`data_sql`/`data_cache`), buckets (`storage`).

## 4. Planned upstream dependencies
`network`, `alb`, `iam`, `secrets`, `data_sql`, `data_cache`, `storage`,
`registry`.

## 5. Planned inputs (names only, no values)
`cluster_name`, `private_subnet_ids`, `app_security_group_id`, `image_digest`,
`execution_role_arn`, `task_role_arn`, `secret_arns`, `plaintext_env`,
`target_group_arn`, `name_prefix`, `tags`.

## 6. Planned non-sensitive outputs (names only)
`cluster_id`, `api_service_name`, `worker_service_name`, `migration_task_family`.

## 7. Security boundaries
Tasks run in private subnets, no public IP. Secrets injected only via the
task-definition `secrets` block (`valueFrom` a Secrets Manager ARN) — never
plaintext env, build arg, or image label. Images are digest-pinned (`@sha256:`);
the first deploy pins exact SHA `3aadb8a1da0f26ffd183a4b05161747038d5957c`
(G4, later). All three feature flags remain `false` in task env.

## 8. Staging-only assumptions
Single API + single worker task, each 0.25 vCPU / 0.5 GB; short-lived migration
task. Replicas never migrate.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche.
