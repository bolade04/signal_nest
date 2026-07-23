# SIGNALNEST_STAGING — OpenTofu IaC (INFRA-4, repository-only, partially implemented)

## 1. Purpose and scope

This directory is the **repository-only** Infrastructure-as-Code for the internal,
non-customer **SIGNALNEST_STAGING** (dark canary) environment. It is **staging-only**.
**Eight** modules — **`network`**, **`edge`**, **`alb`**, **`secrets`**, **`registry`**, **`storage`**, **`data_sql`**, and **`data_cache`** —
now contain executable, **offline-validated** HCL resource bodies; the remaining four
modules are documentation-only stubs. Of the eight, only **three** (`network`, `edge`, `alb`)
are currently wired into the **root composition** (`main.tf`); `secrets`, `registry`, `storage`, `data_sql`, and `data_cache` are
**implemented but not yet root-composed**. "Implemented" (HCL exists and offline-validates),
"root-composed" (referenced by `main.tf`), "provisioned", and "deployed" are distinct states
and are not equivalent. **No live `tofu plan`/`apply` has run, no AWS API has been contacted,
and nothing has been provisioned or deployed** — the committed HCL describes intended
resources, it does not mean any resource exists in AWS.

Authoritative design: [`docs/operations/aws-staging-iac-plan.md`](../../docs/operations/aws-staging-iac-plan.md)
(the INFRA-4 design), under
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
  main.tf                   # composition root: composes network, edge, alb (no root resource/data)
  outputs.tf                # non-sensitive metadata + network/edge/alb module outputs
  terraform.tfvars.example  # synthetic example inputs
  modules/                  # 12 modules: network, edge, alb, secrets, registry, storage, data_sql, data_cache implemented; 4 doc-only stubs
```

## 4. Compatibility constraints and dependency lock

The `versions.tf` constraints are bounded ranges; the committed
`.terraform.lock.hcl` records the exact selected provider version and checksums
(generated in the earlier tool-assisted tranche — no longer deferred).

- OpenTofu: `>= 1.12.3, < 1.13.0` (validated with 1.12.5)
- AWS provider source: `hashicorp/aws`
- AWS provider constraint: `>= 6.55.0, < 6.56.0`; **locked to `6.55.0`** in
  `.terraform.lock.hcl`.

## 5. Module inventory (exactly 12)

The twelve reusable modules are fixed by the authoritative design
(`aws-staging-iac-plan.md` §6). **Eight** are implemented (HCL authored and
offline-validated only — **not** provisioned or deployed); the other **four** remain
documentation-only stubs (`modules/<name>/README.md`, no HCL). Of the eight implemented
modules, only `network`, `edge`, and `alb` are currently **root-composed**; `secrets`,
`registry`, `storage`, `data_sql`, and `data_cache` are implemented but **not yet root-composed**.

| Module | Status |
| --- | --- |
| `network` | **Implemented** (offline-validated) — VPC, subnets, route tables, single NAT |
| `edge` | **Implemented** (offline-validated) — CloudFront + private S3 SPA origin + web DNS aliases |
| `alb` | **Implemented** (offline-validated) — ALB SG + public HTTPS 443 ingress, internet-facing IPv4 ALB, API IP target group, HTTPS listener |
| `ecs` | Documentation-only stub |
| `data_sql` | **Implemented** (offline-validated, **not root-composed**) — one private RDS PostgreSQL instance + DB subnet group + rule-free RDS security group + TLS-enforcing parameter group (`rds.force_ssl=1`); `manage_master_user_password=true` (no password in HCL/state); private, encrypted (gp3); no DB provisioned, no pgvector activated |
| `data_cache` | **Implemented** (offline-validated, **not root-composed**) — one private ElastiCache for Redis replication group (encrypted at rest, TLS-required in transit, **no `auth_token`** — no Redis credential in HCL/state) + cache subnet group + rule-free Redis security group + empty custom parameter group; no cache provisioned |
| `storage` | **Implemented** (offline-validated, **not root-composed**) — one private S3 application bucket (SSE-S3/AES256, versioning, all four public-access-block controls, bucket-owner-enforced ownership, TLS-only deny policy); no `bucket_key_enabled`, no KMS, no object stored |
| `registry` | **Implemented** (offline-validated, **not root-composed**) — two private ECR repositories (`api`, `worker`) + two lifecycle-policy instances; no image built/pushed |
| `iam` | Documentation-only stub |
| `secrets` | **Implemented** (offline-validated, **not root-composed**) — four empty Secrets Manager containers + one customer-managed KMS key/alias; no secret value populated |
| `observability` | Documentation-only stub |
| `cost` | Documentation-only stub |

The root composition (`main.tf`) currently wires exactly `network`, `edge`, and
`alb`; no later module is composed. The `alb` module owns the ALB security group and
the public HTTPS ingress and exposes six outputs (`alb_arn`, `alb_dns_name`,
`alb_canonical_hosted_zone_id`, `https_listener_arn`, `api_target_group_arn`,
`alb_security_group_id`). The **future** `ecs` module will own the API task security
group and **both** ALB↔API TCP 8000 cross-security-group rules and will consume the
ALB outputs; the ALB never consumes an ECS/API security-group id, so the dependency is
one-way and acyclic: `ecs -> alb`. The regional ACM certificate is supplied through the
required `api_certificate_arn` input (no real ARN is committed); tags are applied by the
provider `default_tags` (no module-level `tags` input). The committed ALB tranche adds
**no** HTTP/port-80 listener, no public port 8000, no IPv6 ingress, no unrestricted ALB
egress, no certificate creation, no Route 53 record, no WAF, no access/connection
logging or log bucket, no ECS resource, and no target registration/attachment.

The `secrets`, `registry`, `storage`, `data_sql`, and `data_cache` modules are **implemented and
offline-validated but not wired into `main.tf`** — the root composition was **not** changed
when they were added.
The `secrets` module creates only the declarative secret *containers* — four empty AWS
Secrets Manager secrets and one customer-managed KMS key/alias — **no secret value has
been populated** (values are populated out-of-band under a later, separately authorized
operational step); it produces outputs for the future `iam` and `ecs` modules
(`secrets -> iam`, `secrets -> ecs`). The `registry` module declares **two** private ECR
repositories, `api` and `worker`, and **two** lifecycle-policy instances (immutable tags,
scan-on-push, AES-256, force-delete disabled, untagged-only expiry preserving tagged
images). API and worker are separate future image artifacts with distinct immutable
digests; the migration task **reuses the worker image** with a command override. **No ECR
image has been built, tagged, scanned remotely, pushed, published, or digest-resolved**,
and creating the repositories does not make ECS deployable; it produces repository
references for the future `iam` and `ecs` modules (`registry -> iam`, `registry -> ecs`).
The `storage` module declares exactly **one** private S3 application bucket with
**SSE-S3/AES256** encryption (no `bucket_key_enabled`, no KMS), versioning enabled, all
**four** public-access-block controls enabled, bucket-owner-enforced ownership (ACLs
disabled), and a **deny-only** bucket policy rejecting any request where
`aws:SecureTransport` is false. **No object has been stored**, no bucket exists in AWS,
and its presence does not make the application S3-backed (`storage_backend` is unchanged);
it exposes `bucket_name`/`bucket_arn` for later wiring — the future `iam` module consumes
`bucket_arn` to scope the task-role S3 policy (`storage -> iam`) and the future
ECS/root-composition tranche passes `bucket_name` to the application.
The `data_sql` module declares exactly **four** resources — one private **RDS
PostgreSQL** instance, its DB subnet group, an RDS security group **created with zero
rules**, and a DB parameter group that sets only `rds.force_ssl = "1"` (TLS in transit).
The instance is not publicly accessible, is encrypted at rest (gp3), and uses
**`manage_master_user_password = true`** so RDS generates and holds the master password
in an RDS-managed Secrets Manager secret — **no password value or complete `DATABASE_URL`
enters the HCL or OpenTofu state**, and that master credential is an administrative/bootstrap
credential, **not** the finished API/worker credential. **No database exists in AWS**, the
`pgvector` extension is **not** activated (a deferred database-bootstrap step; no committed
migration creates it), and the module grants no ECS/IAM access. The future `ecs` module
owns the TCP 5432 ingress rules and consumes `rds_security_group_id` (one-way
`data_sql -> ecs`); the endpoint is composed into `DATABASE_URL` out-of-band.
The `data_cache` module declares exactly **four** resources — one private **ElastiCache
for Redis** replication group, its cache subnet group, a Redis security group **created
with zero rules**, and an empty custom Redis parameter group pinned to the engine family.
The replication group is private (private subnets only, not publicly accessible),
encrypted at rest, and requires TLS in transit (`transit_encryption_mode = "required"`);
there is deliberately **no `auth_token`** — no Redis password or complete `REDIS_URL`
enters the HCL or OpenTofu state (`REDIS_URL` is composed out-of-band with the
`rediss://` scheme and injected through Secrets Manager under a later, separately
authorized step). **No cache exists in AWS**, and the module grants no ECS/IAM access.
The future `ecs` module owns the standalone TCP 6379 ingress rules (from the API and
worker task security groups only; the migration task receives **no** Redis access) and
consumes `redis_security_group_id` (one-way `data_cache -> ecs`, acyclic).

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
- `main.tf` — composition root; composes the `network`, `edge`, and `alb` modules;
  no root-level `resource`/`data` block.
- `outputs.tf` — non-sensitive metadata echoes only.
- `terraform.tfvars.example` — synthetic example inputs; never real values.

## 8. Remote-state design (not initialized)

The design targets an **S3** state backend with **SSE-KMS** encryption,
versioning, blocked public access, and **DynamoDB** state locking
(`aws-staging-iac-plan.md` §7). In this tranche:

- **No backend identifiers** (bucket, key, region, KMS id, DynamoDB table, role
  ARN, workspace prefix) are committed — `backend.tf` is intentionally empty.
- **No state bootstrap** is performed. No state bucket or lock table exists.
- The S3 backend has **not** been initialized. Offline validation used
  `tofu init -backend=false`, which deliberately skips backend initialization; a
  backend-configured `tofu init` has not been run and no state exists.

## 9. Security rules

Committed IaC in this repository must contain **no**: AWS credentials, real
account ids, real ARNs, secret values, committed state, committed plans, provider
cache, or real `.tfvars`. Real identifiers and secret references are supplied only
at a later authorized implementation/apply time via variables/state — never
committed.

## 10. Current validation status

**Offline validation only.** The implemented `network`, `edge`, `alb`, `secrets`,
`registry`, `storage`, `data_sql`, and `data_cache` modules (each in its own tranche) and the root composition have been checked
with `tofu fmt`, `tofu init -backend=false`
(using a disposable, repository-external data directory and the locked provider), and
`tofu validate` — all offline, with the S3 backend disabled and AWS credentials
suppressed. The committed `.terraform.lock.hcl` pins `hashicorp/aws 6.55.0`. **No**
`tofu plan`, `apply`, `destroy`, `import`, `state`, or `refresh` has run; **no** AWS
API has been contacted; **no** repository-local `.terraform` directory or state is
committed. Offline validation confirms configuration validity — it does **not** mean
any AWS resource exists.

## 11. Dark-state rules

Infrastructure existence must **never** activate product behavior. All three
global feature flags remain Boolean `false`
(`opportunity_feedback_enabled`, `scout_scheduling_enabled`,
`connector_rss_enabled`). No capability override is created by this or any INFRA
tranche.

## 12. Status and next tranches (each separately authorized)

**INFRA-4 is not complete** and **INFRA-5 is unstarted.** Done so far: tool-assisted
validation + `.terraform.lock.hcl` (with cross-platform provider checksums), and the
`network`, `edge`, `alb`, `secrets`, `registry`, `storage`, `data_sql`, and `data_cache` module bodies (offline-validated only;
`secrets`, `registry`, `storage`, `data_sql`, and `data_cache` are implemented but **not yet root-composed**). Remaining:

1. **Remaining module bodies + root composition:** author HCL for the **four**
   documentation-only stubs (`ecs`, `iam`,
   `observability`, `cost`) — later INFRA-4 tranches — and **root-compose** the already
   implemented `secrets`, `registry`, `storage`, `data_sql`, and `data_cache` modules. ECS will own the API task SG and both
   ALB↔API cross-SG rules and consume the ALB outputs.
2. **Pre-live requirements:** access/connection **logging must be resolved before any
   live staging plan/apply**; DNS, WAF, ACM certificate creation, ECS, and target
   registration remain deferred; the applicable pre-live follow-ups recorded in the
   phase plan (`docs/phase-4b-c-infra-plan.md`) remain unresolved.
3. **INFRA-5:** protected build/deploy workflow with GitHub **OIDC** and a
   human-approval staging environment (no production deploy).
4. **Remote-state bootstrap + INFRA-9:** authenticated `plan`, state bootstrap,
   provisioning, and deployment of the exact first-deploy SHA — under fresh
   authorization, with all global flags remaining `false`.

## 13. Never auto-apply

OpenTofu is **never** auto-applied. Any future `apply` is gated behind a
protected, human-approved deployment path (INFRA-5 approval gate; INFRA-9 fresh
authorization). Infrastructure setup and canary activation are never combined.

## 14. Command execution scope

Only **offline** OpenTofu commands (`fmt`, `init -backend=false`, `validate`) have been
run against the implemented modules, in a repository-external data directory with the
backend disabled and AWS credentials suppressed. Any **live** OpenTofu/AWS command
(`plan`, `apply`, `destroy`, `import`, `state`, `refresh`, or any AWS CLI/SDK call)
shown in this directory's documentation is **illustrative only** and has **not** been
executed; live operations remain gated behind INFRA-5 approval and INFRA-9 fresh
authorization (§13).
