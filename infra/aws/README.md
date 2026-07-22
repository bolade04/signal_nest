# SIGNALNEST_STAGING — OpenTofu IaC skeleton (INFRA-4, repository-only)

## 1. Purpose and scope

This directory is the **repository-only, placeholder-safe** Infrastructure-as-Code
skeleton for the internal, non-customer **SIGNALNEST_STAGING** (dark canary)
environment. It is **staging-only**. It contains **no** executable resource
bodies, was **not** produced by any tool run, and provisions **nothing**.

Authoritative design: [`docs/operations/aws-staging-iac-plan.md`](../../docs/operations/aws-staging-iac-plan.md)
(the plan-only INFRA-4 design), under
[`docs/phase-4b-c-infra-plan.md`](../../docs/phase-4b-c-infra-plan.md) (roadmap),
[`docs/architecture/adr-0001-aws-ecs-fargate-staging.md`](../../docs/architecture/adr-0001-aws-ecs-fargate-staging.md)
(decision record) and
[`docs/operations/aws-staging-runtime-contract.md`](../../docs/operations/aws-staging-runtime-contract.md)
(runtime/security/cost contract).

## 2. Authoritative IaC tool

**OpenTofu** is SignalNest's authoritative IaC CLI and implementation target
(INFRA-4 project-owner decision). Terraform providers/modules may be reused for
compatibility, but Terraform and OpenTofu are **not** interchangeable project
authorities — OpenTofu is the sole authoritative CLI.

## 3. Layout

Flat, single-root, staging-only. There are **no** `environments/`, `envs/`,
`staging/`, or `production/` directories and **no** second root module. Production
infrastructure is out of scope for INFRA-4.

```
infra/aws/
  README.md                 # this file
  versions.tf               # OpenTofu + AWS provider compatibility constraints
  providers.tf              # default AWS provider (region + default tags)
  backend.tf                # empty S3 backend declaration (no values)
  variables.tf              # typed root inputs (no secrets/ids)
  locals.tf                 # name prefix + the authoritative eight-tag set
  main.tf                   # composition-root placeholder (no module/resource/data)
  outputs.tf                # non-sensitive metadata outputs
  terraform.tfvars.example  # synthetic example inputs
  modules/                  # 12 documentation-only stubs (README.md each)
```

## 4. Accepted compatibility constraints

Bounded ranges only — **not** exact pins. Exact selection + `.terraform.lock.hcl`
generation are deferred (§10, §12).

- OpenTofu: `>= 1.12.3, < 1.13.0`
- AWS provider source: `hashicorp/aws`
- AWS provider: `>= 6.55.0, < 6.56.0`

## 5. Module inventory (exactly 12)

The twelve reusable modules are fixed by the authoritative design
(`aws-staging-iac-plan.md` §6). In this tranche each is a **documentation-only
stub** (`modules/<name>/README.md`) with no HCL.

## 6. Planned module responsibilities

| Module | Planned responsibility (design §6) |
| --- | --- |
| `network` | VPC, public/private subnets, route tables, NAT Gateway, VPC endpoints, security groups |
| `edge` | Route 53, ACM certificates, CloudFront, S3 web (SPA) origin |
| `alb` | Application Load Balancer, HTTPS-only listeners, target groups |
| `ecs` | ECS cluster, API service, worker service, one-shot migration run-task |
| `data_sql` | RDS PostgreSQL + pgvector, subnet group, parameter group |
| `data_cache` | ElastiCache Redis, subnet group |
| `storage` | S3 application buckets, lifecycle, SSE, access policy |
| `registry` | ECR repositories (immutable tags) |
| `iam` | Least-privilege roles/policies (execution, task, migration, CI-OIDC) |
| `secrets` | Secrets Manager + KMS references (names only; no values) |
| `observability` | CloudWatch log groups, alarms, CloudTrail |
| `cost` | AWS Budgets (50/75/90/100%) + notifications |

## 7. Root-file responsibilities

- `versions.tf` — OpenTofu + AWS provider compatibility ranges (no lock).
- `providers.tf` — default AWS provider; region via `var.aws_region`; default
  tags via `local.common_tags`. No credentials/profile/role/account/alias.
- `backend.tf` — empty S3 backend declaration; **no** values.
- `variables.tf` — typed inputs; no secret/account/ARN/CIDR variables.
- `locals.tf` — deterministic name prefix + the eight-tag set (§A).
- `main.tf` — composition-root placeholder; no `module`/`resource`/`data`.
- `outputs.tf` — non-sensitive metadata echoes only.
- `terraform.tfvars.example` — synthetic example inputs; never real values.

## 8. Remote-state design (not initialized)

The design targets an **S3** state backend with **SSE-KMS** encryption,
versioning, blocked public access, and **DynamoDB** state locking
(`aws-staging-iac-plan.md` §7). In this tranche:

- **No backend identifiers** (bucket, key, region, KMS id, DynamoDB table, role
  ARN, workspace prefix) are committed — `backend.tf` is intentionally empty.
- **No state bootstrap** is performed. No state bucket or lock table exists.
- The backend has **not** been initialized (`tofu init` was not run).

## 9. Security rules

Committed IaC in this repository must contain **no**: AWS credentials, real
account ids, real ARNs, secret values, committed state, committed plans, provider
cache, or real `.tfvars`. Real identifiers and secret references are supplied only
at a later authorized implementation/apply time via variables/state — never
committed.

## 10. Current validation status

**Static review only.** This tranche performed **no** `tofu fmt`, **no**
`tofu init`, **no** `tofu validate`, **no** provider download, **no** dependency
lock, and **no** AWS contact. HCL is hand-formatted in conventional two-space
style; no formatter was executed.

## 11. Dark-state rules

Infrastructure existence must **never** activate product behavior. All three
global feature flags remain Boolean `false`
(`opportunity_feedback_enabled`, `scout_scheduling_enabled`,
`connector_rss_enabled`). No capability override is created by this or any INFRA
tranche.

## 12. Next tranches (each separately authorized)

1. **Tool-assisted validation & lock:** install/run OpenTofu, `tofu fmt`,
   `tofu init -backend=false`, `tofu validate`, and generate
   `.terraform.lock.hcl` (with cross-platform provider checksums).
2. **Module-body implementation:** author the real HCL resource bodies for the
   twelve modules (later INFRA-4 tranche).
3. **INFRA-5:** protected build/deploy workflow with GitHub **OIDC** and a
   human-approval staging environment (no production deploy).
4. **Remote-state bootstrap + INFRA-9:** authenticated `plan`, state bootstrap,
   provisioning, and deployment of the exact first-deploy SHA — under fresh
   authorization, with all global flags remaining `false`.

## 13. Never auto-apply

OpenTofu is **never** auto-applied. Any future `apply` is gated behind a
protected, human-approved deployment path (INFRA-5 approval gate; INFRA-9 fresh
authorization). Infrastructure setup and canary activation are never combined.

## 14. Documentation-only future commands

Any OpenTofu/AWS command shown in this directory's documentation is **illustrative
documentation only** and was **not executed** during this tranche. No tool was
installed, initialized, or run.
