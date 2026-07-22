# Module: `iam` (documentation-only stub)

## 1. Purpose
Least-privilege IAM roles and policies for the staging plane.

## 2. Planned AWS scope
ECS task execution role, API task role, worker task role, migration task role,
CI-OIDC deployment role (trust configured in INFRA-5), operator/observer/
break-glass roles; resource-scoped policies.

## 3. Out of scope
GitHub OIDC trust provisioning (INFRA-5), secret material (`secrets`), any
resource the roles grant access to (owned by their modules).

## 4. Planned upstream dependencies
Producer → `iam`: `secrets` (`secret_arns`), `storage` (`bucket_arn`), `registry`
(`repository_arns`). `data_sql`/`data_cache` only **if** executable code and the approved
runtime require data-resource IAM permissions — RDS/Redis socket access is a network/credential
matter, not an IAM permission by default, so stale data ARN inputs without a demonstrated
consumer are dropped. `iam` consumes **no** ECS output (one-way `iam -> ecs`). See
`docs/operations/aws-staging-iac-plan.md` §26.8.

## 5. Planned inputs (names only, no values)
`secret_arns` (from `secrets`), `bucket_arns` (from `storage`), `repository_arns` (from
`registry`), `name_prefix`. The `log_group_arns` input is **removed**: the execution-role
CloudWatch Logs policy is scoped to a deterministic name prefix
(`/ecs/<name_prefix>-*`) built from `name_prefix`, never by consuming ECS/observability
outputs — this breaks the `iam -> observability -> ecs -> iam` cycle. No `tags` input
(provider `default_tags`).

## 6. Planned non-sensitive outputs (names only)
`execution_role_arn`, `api_task_role_arn`, `worker_task_role_arn`,
`migration_task_role_arn` (references). One shared execution role + three distinct
application task roles; `ecs` consumes all four ARNs.

## 7. Security boundaries
No long-lived keys; deployment via GitHub OIDC (trust in INFRA-5). **Execution role:** ECR
retrieval, CloudWatch Logs stream creation + delivery (prefix-scoped), retrieval of only the
secret ARNs referenced in task definitions, `kms:Decrypt` only if a separately approved CMK
requires it; application containers never receive execution-role credentials. **Application
task roles:** only the AWS API calls the code actually makes — S3 only for workloads proven to
use it; no RDS/Redis IAM permission (unless IAM DB auth is separately implemented); no
ECR/Logs-driver/secret-injection permissions; migration task role empty/minimal unless
migration code calls an AWS API. Policies are resource-scoped by name prefix wherever AWS
supports it; the only documented `Resource: "*"` exception is ECR authorization
(`ecr:GetAuthorizationToken`), which AWS supports only at `*`. No ARN or account id committed.

## 8. Staging-only assumptions
Roles scoped to staging resources only; break-glass exceptional and CloudTrail-
audited.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche. GitHub OIDC trust configuration is INFRA-5.
