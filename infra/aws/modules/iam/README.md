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
References resource ARNs from `secrets`, `storage`, `registry`, `data_sql`,
`data_cache` at wiring time.

## 5. Planned inputs (names only, no values)
`secret_arns`, `bucket_arns`, `repository_arns`, `log_group_arns`,
`name_prefix`, `tags`.

## 6. Planned non-sensitive outputs (names only)
`execution_role_arn`, `api_task_role_arn`, `worker_task_role_arn`,
`migration_task_role_arn` (references).

## 7. Security boundaries
No long-lived keys; deployment via GitHub OIDC (trust in INFRA-5). Every policy
resource-scoped, no wildcard resources, no admin. Execution role reads only the
specific Secrets Manager references. No ARN or account id committed.

## 8. Staging-only assumptions
Roles scoped to staging resources only; break-glass exceptional and CloudTrail-
audited.

## 9. Status
No executable HCL yet. No resources created. Implementation and validation are
deferred.

## 10. Owning tranche
Real resource bodies belong to the later, separately authorized INFRA-4 module
implementation tranche. GitHub OIDC trust configuration is INFRA-5.
