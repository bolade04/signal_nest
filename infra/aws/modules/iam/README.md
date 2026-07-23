# Module: `iam` (implemented — offline-validated only, NOT root-composed)

## 1. Purpose
Least-privilege IAM identity plane for the staging compute plane: the **four
ECS-consumed roles** locked by `docs/operations/aws-staging-iac-plan.md` §26.8 —
one shared **ECS task execution role** plus three distinct **application task
roles** (API, worker, migration). Implemented and offline-validated only: **no
role exists in AWS**, nothing is provisioned, and the module is **not**
root-composed.

## 2. Implemented AWS scope (exactly seven resources)
- `aws_iam_role.execution` — `<name_prefix>-ecs-execution`, trust
  `ecs-tasks.amazonaws.com` (all four roles add an `aws:SourceAccount`
  condition against the deploying account — confused-deputy guard).
- `aws_iam_role_policy.execution` — inline least-privilege policy:
  1. `ecr:GetAuthorizationToken` at `Resource: "*"` — the **single documented
     wildcard exception** (AWS supports this action only at `*`);
  2. `ecr:BatchCheckLayerAvailability`/`GetDownloadUrlForLayer`/`BatchGetImage`
     scoped to the two application repository ARNs (`registry -> iam`);
  3. `logs:CreateLogStream`/`logs:PutLogEvents` scoped to the **deterministic**
     prefix `/ecs/<name_prefix>-*` (account/region/partition resolved by data
     source at plan time; **no `logs:CreateLogGroup`** — `ecs` owns the three
     log groups and `awslogs-create-group` is disabled, §26.9);
  4. `secretsmanager:GetSecretValue` scoped to exactly the four container ARNs
     (`secrets -> iam`);
  5. `kms:Decrypt` scoped to exactly the secrets CMK, conditioned on
     `kms:ViaService = secretsmanager.<region>.amazonaws.com`.
- `aws_iam_role.api_task` + `aws_iam_role_policy.app_s3["api"]` — API task role
  with the application-bucket S3 policy only.
- `aws_iam_role.worker_task` + `aws_iam_role_policy.app_s3["worker"]` — worker
  task role with the same application-bucket S3 policy.
- `aws_iam_role.migration_task` — **intentionally empty** (no attached policy):
  migration code calls no AWS API; its DB access is network + execution-role
  secret injection (§26.8).

The S3 policy mirrors the executable client exactly
(`apps/api/app/infra/storage.py`: `put_object`/`get_object`/`head_object`/
`delete_object`/`head_bucket`/presigned `get_object`): `s3:ListBucket` on the
bucket ARN plus `s3:GetObject`/`s3:PutObject`/`s3:DeleteObject` on
`<bucket_arn>/*`. No `s3:*`, no ACL/policy mutation, no cross-bucket access.

## 3. Out of scope
GitHub **CI-OIDC deployment role** and its trust provisioning (**INFRA-5**);
**operator/observer/break-glass** human roles (trust boundaries not yet
designed — a later, separately authorized tranche); secret material
(`secrets`); any resource the roles grant access to (owned by their modules);
IAM database authentication (not implemented — no RDS/Redis IAM permission
exists, §26.8).

## 4. Upstream dependencies (all producer outputs EXIST; §26.15)
Producer → `iam`: `secrets` (`secret_arns`, `kms_key_arn`), `storage`
(`bucket_arn` — singular), `registry` (`repository_arns`).
`data_sql`/`data_cache` produce **no** `iam` input — RDS/Redis socket access is
a network/credential matter, not an IAM permission by default. `iam` consumes
**no** ECS or observability output: the execution-role Logs policy is scoped to
the deterministic `/ecs/<name_prefix>-*` prefix built from `name_prefix`,
breaking the `iam -> observability -> ecs -> iam` cycle. One-way `iam -> ecs`.
See `docs/operations/aws-staging-iac-plan.md` §26.8/§26.15.

## 5. Inputs (implemented)
`name_prefix` (≤48 chars so every derived role name fits IAM's 64-char limit),
`secret_arns` (map, from `secrets`), `kms_key_arn` (from `secrets`),
`bucket_arn` (from `storage` — singular; the `storage` module outputs exactly
one `bucket_arn`), `repository_arns` (map, from `registry`). ARN inputs are
statically validated for the expected service prefix. No `log_group_arns` input
(removed by §26.8). No `tags` input (root provider `default_tags`; this module
adds only per-resource `Name` tags).

## 6. Outputs (implemented — exactly four)
`execution_role_arn`, `api_task_role_arn`, `worker_task_role_arn`,
`migration_task_role_arn`. One shared execution role + three distinct
application task roles; the future `ecs` module consumes all four ARNs.

## 7. Security boundaries
No long-lived keys; deployment via GitHub OIDC (trust in INFRA-5). Application
containers never receive execution-role credentials. **Execution role:** ECR
retrieval, prefix-scoped log delivery, retrieval of only the referenced secret
ARNs, `kms:Decrypt` only on the secrets CMK via Secrets Manager. **Application
task roles:** only the AWS API calls the code actually makes — S3 for API and
worker (proven use), **no** RDS/Redis IAM permission, **no**
ECR/Logs-driver/secret-injection permission; migration task role empty. All
policies are resource-scoped by ARN or deterministic name prefix; the only
`Resource: "*"` is `ecr:GetAuthorizationToken`. Trust policies are limited to
`ecs-tasks.amazonaws.com` with an `aws:SourceAccount` condition. No ARN,
account id, or credential is committed — account/region/partition enter only
through data sources at plan time.

## 8. Staging-only assumptions
Roles scoped to staging resources only; single account; break-glass and human
roles deferred (§3). Data sources (`aws_partition`/`aws_region`/
`aws_caller_identity`) are read at plan/apply time only — offline `tofu
validate` does not contact AWS.

## 9. Scope boundaries (this tranche)
Implemented, but **uncomposed** (not in root `main.tf`), **unprovisioned**, and
**inactive**. No AWS access, no live role, no root-composition change, no
policy attachment to any existing principal, no ECS integration. Offline
validation only (`tofu fmt` / `init -backend=false` / `validate` with the
committed root lockfile, backend disabled, AWS credentials suppressed). GitHub
CI does not independently validate HCL (its five jobs are
application/integration checks); HCL correctness rests on the offline harness,
static validations, and independent review. INFRA-4 remains incomplete;
INFRA-5 remains unstarted.

## 10. Owning tranche
Implemented by the INFRA-4 `iam` module resource-definition tranche.
Root-composition, ECS consumption, any live `plan`/`apply` (INFRA-9), the
CI-OIDC role (INFRA-5), and the operator/observer/break-glass roles are later,
separately authorized tranches.
