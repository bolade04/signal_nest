# Module: `iam` (implemented — offline-validated only, root-composed)

## 1. Purpose
Least-privilege IAM identity plane for the staging compute plane: the **four
ECS-consumed roles** locked by `docs/operations/aws-staging-iac-plan.md` §26.8 —
one shared **ECS task execution role** plus three distinct **application task
roles** (API, worker, migration). Implemented and offline-validated only: **no
role exists in AWS** and nothing is provisioned. The module **is root-composed**
(wired in `infra/aws/main.tf` by the root-composition tranche, PR #120;
composition is configuration only).

## 2. Implemented AWS scope (seven ECS-role resources + up to two publisher-role resources)
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
- `aws_iam_role.ci_publisher` + `aws_iam_role_policy.ci_publisher` — the
  **CI image-publisher role** (GitHub OIDC → ECR push, INFRA-9 execution-path
  tranche), created **only when `github_oidc_provider_arn` is supplied**
  (`count = provider_arn == null ? 0 : 1`). Trust: `sts:AssumeRoleWithWebIdentity`
  restricted to `aud = sts.amazonaws.com` **and**
  `sub = repo:bolade04/signal_nest:environment:staging` (repo + protected
  environment — blocks pull-request/fork execution). Policy: least privilege —
  `ecr:GetAuthorizationToken` at `Resource:"*"` (the same single documented
  exception) plus push/read-back actions
  (`BatchCheckLayerAvailability`/`InitiateLayerUpload`/`UploadLayerPart`/
  `CompleteLayerUpload`/`PutImage`/`BatchGetImage`/`DescribeImages`) scoped to
  exactly the two application repository ARNs. **No** ECS, IAM, Secrets Manager,
  RDS, or any other permission — this role can only push images. The account-wide
  OIDC **provider** is consumed, never created (external prerequisite).

The S3 policy mirrors the executable client exactly
(`apps/api/app/infra/storage.py`: `put_object`/`get_object`/`head_object`/
`delete_object`/`head_bucket`/presigned `get_object`): `s3:ListBucket` on the
bucket ARN plus `s3:GetObject`/`s3:PutObject`/`s3:DeleteObject` on
`<bucket_arn>/*`. No `s3:*`, no ACL/policy mutation, no cross-bucket access.

## 3. Out of scope
*[Updated 2026-07-24, INFRA-9 execution-path tranche: the CI image-publisher
role moved OUT of this list and INTO §2 — it is now authored here (see §2),
superseding the earlier "CI-OIDC deployment role ... INFRA-5" deferral (INFRA-5
authored only the workflow + prose spec).]* The account-wide GitHub Actions
**OIDC provider** resource itself (consumed via `github_oidc_provider_arn`,
never created here — an external prerequisite); **operator/observer/break-glass**
human roles (trust boundaries not yet designed — a later, separately authorized
tranche); secret material
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

## 6. Outputs (five)
`execution_role_arn`, `api_task_role_arn`, `worker_task_role_arn`,
`migration_task_role_arn` (the four ECS-consumed ARNs), plus
`ci_publisher_role_arn` (the CI image-publisher role ARN, or **null** when the
publisher role is uncreated). The `ecs` module consumes the four task-role
ARNs; the publisher ARN is consumed by no module — the operator sets it as the
`staging-publish.yml` workflow's `AWS_STAGING_PUBLISH_ROLE_ARN` environment
variable.

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
Implemented and **root-composed** (wired in root `main.tf` by the later
root-composition tranche), but **unprovisioned** and **inactive**. No AWS
access, no live role, no policy attachment to any existing principal. Offline
validation only (`tofu fmt` / `init -backend=false` / `validate` with the
committed root lockfile, backend disabled, AWS credentials suppressed). GitHub
CI does not independently validate HCL (its five jobs are
application/integration checks); HCL correctness rests on the offline harness,
static validations, and independent review.

## 10. Owning tranche
Implemented by the INFRA-4 `iam` module resource-definition tranche;
root-composed by the INFRA-4 root-composition tranche. The CI image-publisher
role HCL was added by the INFRA-9 execution-path tranche (specified by INFRA-5
in `docs/operations/staging-publish-workflow.md` §4; created only when
`github_oidc_provider_arn` is supplied). Any live `plan`/`apply` (INFRA-9), the
account-wide OIDC provider creation, and the operator/observer/break-glass roles are later,
separately authorized tranches.
