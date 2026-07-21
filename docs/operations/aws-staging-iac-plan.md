# AWS staging Infrastructure-as-Code plan (INFRA-4, plan-only)

## 1. Status and authority

- **Status:** Planning — **documentation-design only**. This document authors the
  implementation-ready Infrastructure-as-Code (IaC) design for the internal, non-customer
  **SIGNALNEST_STAGING** (dark canary) environment. It contains **no IaC source code**, runs
  **no** `init`/`validate`/`plan`/`apply`, performs **no** AWS authentication, and creates
  **no** cloud resource.
- **Authority chain (upstream, already merged):**
  - Decision record: [`docs/architecture/adr-0001-aws-ecs-fargate-staging.md`](../architecture/adr-0001-aws-ecs-fargate-staging.md)
    — AWS · ECS on Fargate · us-east-1 · $200/month hard ceiling.
  - Authoritative runtime/security/cost contract:
    [`docs/operations/aws-staging-runtime-contract.md`](./aws-staging-runtime-contract.md).
  - Deployment-SHA provenance and the fail-closed **G4** preflight:
    [`docs/operations/deployment-sha-wiring-plan.md`](./deployment-sha-wiring-plan.md).
  - Secret inventory, dispositions, ECS injection contract, and the fail-closed **G5**:
    [`docs/operations/aws-staging-secret-inventory.md`](./aws-staging-secret-inventory.md).
  - Roadmap: [`docs/phase-4b-c-infra-plan.md`](../phase-4b-c-infra-plan.md) — **INFRA-4**.
- **This tranche authorizes nothing downstream.** Merging it does **not** authorize provider
  selection to become binding, IaC source authoring, `plan`, `apply`, provisioning,
  deployment, secret operations, or Phase 4B-C activation. Every subsequent step is a
  separate, explicitly authorized gate (INFRA-5 … INFRA-9, then activation).
- **Golden rule (inherited):** infrastructure setup and canary activation are **never**
  combined. All three global flags (`opportunity_feedback_enabled`, `scout_scheduling_enabled`,
  `connector_rss_enabled`) remain Boolean `False`. No capability override is created by this or
  any INFRA tranche.

## 2. Goals

This plan exists so that the later, separately authorized IaC implementation (INFRA-4
implementation and INFRA-9 apply) can be written and reviewed against a fixed, agreed design.
Its goals are to:

1. Translate the runtime contract's topology, network, IAM, secret, observability, and cost
   requirements into a concrete, reviewable IaC **module and resource map** — without writing
   the modules.
2. Fix the IaC **project organization**, **remote state**, and **locking** model so the first
   implementation PR has no unreviewed structural decisions left.
3. Encode the **immutable, digest-pinned artifact** and **exact-SHA** wiring
   (`3aadb8a1da0f26ffd183a4b05161747038d5957c` for the first deploy) into the task-definition
   design, consistent with G4.
4. Bind the **secret-injection** design to the reviewed 87-field inventory **by reference and
   by name only** — never by value — consistent with G5.
5. Define the **least-privilege IAM** surface, the **private** data tier, and the **budget /
   cost guardrails** (50/75/90/100%) so the sub-$200 ceiling is encoded structurally.
6. Provide a **validation matrix**, **risk register**, **rollback/recovery** design, and an
   explicit **human decision register** and **future-gate** list so no downstream reviewer has
   to reconstruct intent.

## 3. Non-goals

Explicitly **out of scope** for this document (each is a later, separately authorized step):

- Writing IaC source of any kind (Terraform/OpenTofu HCL, CDK, CloudFormation, Pulumi, etc.).
- Running `init`, `validate`, `plan`, `apply`, `destroy`, or any provider command.
- Selecting or configuring a remote-state backend as a live resource, or creating a state
  bucket / lock table.
- Any AWS authentication, credential creation, OIDC trust configuration, or API call.
- Creating, reading, rotating, or referencing any **real** secret value, AWS account id,
  organization id, tenant id, ARN, IP/CIDR block, hostname, or KMS key id.
- Building, tagging, pushing, or deploying any container image.
- Defining or modifying CI/CD workflows (that is INFRA-5), application code, tests, the
  Dockerfile, migrations, the API contract, or dependency manifests.
- Activating the canary, creating a capability override, or flipping any global flag.

## 4. Evidence and assumptions

**Evidence (from merged, authoritative repository documents — no new facts invented here):**

- Provider/region/compute and budget ceiling: ADR-0001 and the runtime contract (AWS · ECS on
  Fargate · us-east-1 · $200/month hard).
- Topology, network isolation, IAM roles, secret categories, cost model, and the
  staging-vs-production control matrix: runtime contract §§ A, C, D, E, F, M, N.
- Immutable digest-pinned artifacts and the first-deploy exact SHA
  `3aadb8a1da0f26ffd183a4b05161747038d5957c`: runtime contract §§ A/B and
  [`deployment.md`](./deployment.md).
- Deployment-SHA provenance chain (Git SHA → `build_revision` → OCI
  `org.opencontainers.image.revision` label → image digest → task-def revision) and the
  fail-closed **G4** preflight: `deployment-sha-wiring-plan.md`.
- The complete, reconciled **87-field** configuration inventory and reviewed dispositions
  (4 Secrets Manager, 2 IAM-role-derived, 2 absent-in-staging, 3 local-only, 76 non-secret),
  the ECS injection contract, and the fail-closed **G5**: `aws-staging-secret-inventory.md`.
- Runtime topology and container guarantees (single multi-stage `apps/api/Dockerfile`;
  non-root UID/GID 10001; read-only root filesystem with writable `/tmp`; API on port 8000
  with liveness `/health` and readiness `/readiness`; worker with no port; one-shot migration
  actor `python -m app.db.migrate`): `deployment.md` and the runtime contract.

**Assumptions (to be confirmed at implementation time, not decided here):**

- The AWS account, its id, the DNS zone, and the ACM certificate exist or will be provisioned
  under a later authorized tranche; none are referenced by literal value in this design.
- The IaC tool is **DECIDED: OpenTofu** (project-owner human decision — see §16). Terraform
  providers and modules may be reused for compatibility, but **OpenTofu is the authoritative
  project CLI and implementation target**. The specific OpenTofu version is **not** fixed here;
  the implementation tranche must select and pin a supported version from then-current official
  compatibility evidence.
- Real identifiers (account id, ARNs, hostnames, CIDRs, KMS key ids, secret ARNs) are supplied
  only at authorized implementation/apply time via variables/state, never committed.

## 5. Target topology (design view)

Mirrors runtime-contract §C. All compute is ECS on Fargate in a single VPC in **us-east-1**.

| Plane | Component | Runtime | Ingress | Notes |
| --- | --- | --- | --- | --- |
| Static web | React/Vite SPA | S3 origin + CloudFront | Public HTTPS (TLS-only) | No Dockerfile; only `VITE_API_BASE_URL` baked at build; receives **no** backend secret. |
| API | FastAPI (`uvicorn app.main:app`) | ECS/Fargate service, port 8000 | ALB (private targets), public via ALB HTTPS | Liveness `/health`; readiness `/readiness`; non-root; read-only root + writable `/tmp`. |
| Worker | Durable jobs (`python -m app.jobs.worker`) | ECS/Fargate service, **no port** | None | No inbound; drains on `SIGTERM` within the grace window. |
| Migration | One-shot (`python -m app.db.migrate`) | ECS/Fargate run-task, one-shot | None | Runs to success **before** API/worker roll; single actor, never a replica. |
| Data — SQL | RDS PostgreSQL + pgvector | RDS instance | Private subnets only | `db.t4g.micro`, single-AZ, 20 GB gp3 (staging sizing per §M). |
| Data — cache/queue | ElastiCache Redis | ElastiCache node | Private subnets only | `cache.t4g.micro`; cache + queue + notify channel. |
| Object storage | S3 bucket(s) | S3 | Private; app access via task role | Object storage for the API; presigned URLs; SSE. |
| Registry | ECR | ECR | Private | Immutable tags; digest-pinned pulls. |
| Secrets | Secrets Manager + KMS | — | Runtime injection only | Names/ARNs referenced only; values never committed (§11). |
| Telemetry | CloudWatch Logs + alarms; CloudTrail | — | — | JSON stdout/stderr collected; no OTLP in staging. |
| Edge/DNS | Route53 + ACM | — | — | Public TLS certificates for ALB and CloudFront. |
| Egress | NAT Gateway + VPC endpoints | — | — | Private subnets reach AWS services/pulls without public IPs. |

## 6. IaC project organization (proposed layout)

Proposed repository placement is a **new `infra/aws/` directory** (per the roadmap's expected
areas), created only under the later authorized implementation tranche. Proposed structure
(illustrative, not created here):

```
infra/aws/
  README.md                 # scope, safety rails, how to plan (never auto-apply)
  versions.*                # provider + tool version pinning (exact, no floating)
  backend.*                 # remote state + locking config (values via CI/OIDC, not committed)
  variables.*               # typed inputs; NO defaults for account id / ARNs / secrets
  main.*                    # composition root wiring the modules below
  outputs.*                 # non-sensitive outputs only (never secret values)
  modules/
    network/                # VPC, subnets, route tables, NAT, VPC endpoints, SGs
    edge/                   # Route53, ACM, CloudFront, S3 web origin
    alb/                    # ALB, listeners (HTTPS only), target groups
    ecs/                    # cluster, API service, worker service, migration run-task
    data_sql/               # RDS PostgreSQL (+ pgvector), subnet group, parameter group
    data_cache/             # ElastiCache Redis, subnet group
    storage/                # S3 app buckets, lifecycle, SSE, access policy
    registry/               # ECR repositories (immutable tags)
    iam/                    # least-privilege roles/policies (execution, task, migration, CI-OIDC)
    secrets/                # Secrets Manager + KMS references (names only)
    observability/          # CloudWatch log groups, alarms, CloudTrail
    cost/                   # AWS Budget (50/75/90/100%) + notifications
```

**Rules for the implementation tranche:** one module per plane; every module least-privilege
and independently reviewable; no module hard-codes an account id, ARN, hostname, CIDR, or
secret; outputs never expose secret material.

## 7. Remote state and locking (design)

- **State is remote and encrypted.** Design targets an **S3 state bucket with
  SSE-KMS**, versioning enabled, public access blocked, and a **DynamoDB lock table** (or the
  tool-native equivalent) for concurrency locking. Exact bucket/table names, the KMS key, and
  the region binding are supplied at implementation time via variables/backend config — **never
  committed**.
- **State contains sensitive material** (e.g., resolved resource attributes); therefore state
  access is least-privilege, encrypted at rest and in transit, and restricted to the CI-OIDC
  deployment role. State is never printed to logs or attached as PR evidence.
- **Bootstrap ordering:** the state backend itself is provisioned once under a later authorized
  step before any environment module is applied; this document only fixes the design, not the
  bootstrap execution.

## 8. Network and ingress (design)

Mirrors runtime-contract §D:

- **One VPC**, us-east-1, with **public** subnets (ALB, NAT, CloudFront origin path) and
  **private** subnets (ECS tasks, RDS, ElastiCache). No task, database, or cache node receives
  a public IP.
- **Public ingress is TLS-only:** ALB HTTPS listener and CloudFront HTTPS, backed by ACM
  certificates via Route53-validated DNS. HTTP is redirected to HTTPS or refused.
- **Least-privilege security groups:** ALB → API on 8000 only; API → RDS 5432 and Redis 6379
  only; worker → RDS/Redis only; no lateral or inbound path beyond the minimum. No `0.0.0.0/0`
  ingress except the public ALB/CloudFront HTTPS entry points.
- **Egress:** private subnets reach AWS service APIs and ECR pulls via a **NAT Gateway** plus
  **VPC endpoints** (e.g., ECR, S3, Secrets Manager, CloudWatch Logs) to reduce NAT data cost
  and keep traffic on the AWS network.
- **No CIDR block, subnet id, or hostname is committed**; addressing is variable-driven.

## 9. Compute and task-definition design

Per runtime-contract §§C/N and `deployment.md`:

- **ECS on Fargate**, single cluster. One **API service** (desired count 1), one **worker
  service** (desired count 1), and a **migration run-task** invoked one-shot before rolls.
- **Task definitions** encode: non-root user (UID/GID 10001), **read-only root filesystem**
  with a writable `/tmp` mount, exec-form commands so the app is PID 1 and receives `SIGTERM`
  directly, and a termination grace period **≥ `WORKER_SHUTDOWN_GRACE_SECONDS`** for the
  worker.
- **Health wiring:** API container health check / ALB target-group health check against
  liveness `/health`; the orchestrator readiness signal wired to `/readiness` (active backend
  verification), never to `/health`. The worker exposes no port and no HTTP health check.
- **Sizing (staging, §M):** API and worker each **0.25 vCPU / 0.5 GB**, single task each. The
  migration task is short-lived and sized to complete the Alembic upgrade.
- **Rollout model (deployment.md):** publish images → run the single migration actor to success
  → roll API and worker. Replicas never migrate; a replica against an un-advanced schema fails
  fast (`pending`).

## 10. Immutable artifact and SHA wiring

Per runtime-contract §§A/B and `deployment-sha-wiring-plan.md` (G4):

- **Digest-pinned, immutable images.** Task definitions reference images **by digest**
  (`...@sha256:...`), never by mutable tag. ECR repositories use immutable tags to prevent
  overwrite.
- **Exact-SHA first deploy.** The first staging deployment must build and deploy source SHA
  **`3aadb8a1da0f26ffd183a4b05161747038d5957c`**. The Git SHA flows: build → `build_revision`
  (full 40-char lowercase hex) → OCI `org.opencontainers.image.revision` label → image digest →
  task-definition revision.
- **One digest, three actors.** API, worker, and migration tasks pin the **same** image digest
  for a given release, so provenance is identical across the plane.
- **G4 is fail-closed** and remains **unimplemented** here; this design only records where the
  SHA/digest bindings live in the task definitions so the later wiring and preflight can enforce
  them.

## 11. Configuration and secret wiring (by reference only)

Bound to `aws-staging-secret-inventory.md` (the authoritative **87-field** contract). **No
value appears in this document or in any IaC source.** Dispositions drive the design:

- **Secrets Manager (4 fields — `secret_key`, `database_url`, `redis_url`, `llm_api_key`):**
  injected **only** through the ECS task-definition `secrets` block (`valueFrom` a Secrets
  Manager ARN). Never plaintext task environment, never a Docker build arg, image label, or
  mutable tag. Referenced by **name/ARN variable** only.
- **IAM-role-derived (2 fields — `s3_access_key_id`, `s3_secret_access_key`):** **unset**;
  object-storage access is via the ECS task-role credential chain, not literals.
- **Absent-in-staging (2 fields — `otlp_endpoint`, `s3_endpoint_url`):** left unset (staging
  uses CloudWatch, native S3 endpoint).
- **Local-only (3 fields — `local_storage_dir`, `llm_mock_seed`, `llm_allow_dev_fallback`):**
  unset or safe non-mock default; the mock provider and dev fallback are forbidden in staging.
- **Non-secret (76 fields):** provided as **plaintext task environment** in the task
  definition (e.g., `ENVIRONMENT=staging`, `APP_MODE=full`, pool/timeout tuning, feature flags
  **all `false`**). Field names equal the UPPERCASED model field name (no env prefix).
- **Validator coupling (documented, no code change):** `is_production_like` requires a strong
  `secret_key`, a non-mock `llm_provider` + `llm_api_key`, and `s3_bucket` when
  `storage_backend=s3`. The **migration** task constructs `Settings` at startup and therefore
  must receive these secret references even though it does not functionally use them — the task
  definition design accounts for this over-provisioning.
- **Browser boundary:** the SPA build receives **only** `VITE_API_BASE_URL` and **no** backend
  secret. KMS encrypts Secrets Manager material; keys are referenced by name/alias only.
- **G5 alignment:** the design's injection map is exactly what a future fail-closed **G5** would
  verify (one reviewed disposition per field, `repr=False`/secret-like fields inventoried,
  actor-minimum secret subsets, no forbidden injection path). G5 remains **unimplemented** here.

## 12. IAM and least privilege (design)

Mirrors runtime-contract §E. Named roles only; **no ARNs, account ids, or policies-as-code**
are committed here:

- **Deployment / CI-OIDC role:** assumed by GitHub Actions via OIDC (no long-lived keys),
  scoped to staging deploy actions and remote-state access. OIDC trust configuration is
  **INFRA-5**, not this tranche.
- **ECS task execution role:** pulls images from ECR, reads the specific Secrets Manager
  references, writes CloudWatch Logs — nothing more.
- **API task role:** least-privilege access to the required S3 bucket(s) and any AWS service
  the API legitimately needs; no admin, no wildcard resources.
- **Worker task role:** minimum for job execution (S3/Redis/DB paths); no inbound anything.
- **Migration task role:** minimum for the schema upgrade plus the secret references forced by
  the validator coupling (§11).
- **Operator / observer / break-glass roles:** as defined in §E, least-privilege and audited;
  break-glass is exceptional and logged via CloudTrail.
- Every policy is resource-scoped and reviewed independently at implementation time.

## 13. Database and migration safety (design)

- **Private RDS PostgreSQL + pgvector**, single-AZ (staging), `db.t4g.micro`, 20 GB gp3,
  automated backups enabled, not publicly accessible, encrypted at rest (KMS) and in transit.
- **Single migration actor.** The one-shot migration run-task executes
  `python -m app.db.migrate` to success **before** API/worker roll; replicas never migrate.
- **Additive-first schema policy (deployment.md).** During the coexistence window, old replicas
  run `ahead` (startup-safe) against the newer additive schema; new replicas run `compatible`.
  A replica against an un-advanced schema reports `pending` and fails fast.
- **Alembic single head `98289430a3ec`** is the current baseline; the migration task advances
  the schema, never a replica. Rollback favors additive-first forward-compat; an explicit
  `downgrade` is a deliberate single-actor action with an explicit target revision.

## 14. Observability (design)

Mirrors runtime-contract §§ contract and `observability.md` intent:

- **Logs:** app writes structured JSON to stdout/stderr; the ECS runtime ships to **CloudWatch
  Logs** (one log group per service). No log files, no OTLP in staging (`otlp_endpoint` absent).
- **Metrics/alarms:** CloudWatch metrics with alarms for service health, error rate, DB/Redis
  saturation, and the **cost budget** thresholds (§15).
- **Audit:** CloudTrail records control-plane and IAM/secret access events.
- **Secret hygiene:** secret values are **never** logged, printed, or attached as evidence, per
  the G5 contract. Readiness/liveness probes surface health without exposing secrets.

## 15. Cost and environment controls (design)

Mirrors runtime-contract §M and the roadmap's budget requirement:

- **Hard ceiling $200/month.** Design estimate (from §M): ~$95 baseline / ~$119 typical /
  ~$165 upper, with primary drivers NAT (~$33), ALB (~$18), RDS (~$16), Fargate (~$18),
  ElastiCache (~$12). A **fresh dated estimate is mandatory before any authorized apply**
  (INFRA-9), not produced by this plan.
- **AWS Budgets** encoded in the `cost/` module at **50/75/90/100%** thresholds with
  notifications, so overspend alerts exist structurally before any spend begins.
- **Staging sizing** is single-task/single-AZ per §N; **security controls remain identical to
  production** (private data tier, TLS-only ingress, least-privilege IAM, encrypted state and
  secrets). Cost reductions never weaken a security control — if a projected estimate exceeds
  $200 without weakening a control, the rule is **STOP and reauthorize**.
- **Tagging (§A):** every resource carries the standard tag set — `Project`, `Environment`,
  `Alias`, `Owner`, `CostCenter`, `Phase=4B-C`, `DataClass`, `ManagedBy=iac` — for cost
  attribution and governance.

## 16. IaC tool selection — DECIDED: OpenTofu

**Decision:** **OpenTofu is DECIDED** as SignalNest's authoritative infrastructure-as-code tool,
made by the **project owner** as the required INFRA-4 human decision. OpenTofu is the
authoritative project CLI and implementation target for the future placeholder-only INFRA-4
skeleton and all subsequent authorized AWS infrastructure tranches. **Terraform** providers and
modules may be reused for compatibility, but Terraform and OpenTofu are **not** interchangeable
project authorities — **OpenTofu is the sole authoritative CLI**. AWS remains the planned cloud
provider (ADR-0001). This decision **resolves** the sole remaining tool-selection item and is
mirrored in the human decision register (§21).

The OpenTofu **version** is intentionally **not fixed here**: the implementation tranche MUST
select and pin a supported OpenTofu version (and its provider versions) from then-current
official compatibility evidence, with explicit, reproducible dependency locks.

**Scope of this decision (what it does NOT authorize):** no IaC skeleton implementation, no
OpenTofu install, no `init`, no `validate` requiring downloaded providers, no `plan`, no
`apply`, no `import`/`refresh`/`destroy`, no remote-state creation, no AWS authentication, no
provisioning, no deployment, no feature activation, and no INFRA-5 work. INFRA-4 remains
plan-only and unimplemented after this decision.

**Candidates evaluated (history — retained for the record, superseded by the decision above):**

| Candidate | Pros | Cons |
| --- | --- | --- |
| **OpenTofu (SELECTED)** | Terraform-compatible providers/modules; open-source governance; exact version pinning; human-readable `plan`; encrypted remote state + locking; GitHub-OIDC compatible | Younger ecosystem/tooling maturity |
| Terraform | Mature AWS provider, wide review familiarity, `plan` diffing | License considerations; state management overhead |
| AWS CDK | Native AWS, typed, higher-level constructs | Synthesized CloudFormation opacity; drift semantics |
| CloudFormation | First-party, no extra state backend | Verbose; weaker cross-account/module ergonomics |

The decision procedure that produced this choice required a tool that (a) supports exact version
pinning, (b) provides a reviewable, human-readable `plan`/diff **before** apply, (c) supports
encrypted remote state + locking, and (d) integrates with GitHub OIDC (INFRA-5); **OpenTofu
satisfies all four.**

## 17. Implementation sequence (later, separately authorized)

This is the order the **later** IaC implementation should follow; **none of it is authorized by
merging this plan**:

1. Pin exact OpenTofu and provider versions (tool already DECIDED as OpenTofu, §16).
2. Bootstrap the encrypted remote-state backend + lock table (one-time, authorized).
3. Author `network/` → `edge/` → `alb/` → `iam/` → `secrets/` (names only) → `data_sql/` →
   `data_cache/` → `storage/` → `registry/` → `ecs/` → `observability/` → `cost/` modules, each
   as an independently reviewed PR where practical.
4. Wire digest-pinned images and the exact first-deploy SHA into the task definitions (§10),
   aligned with G4.
5. Wire the secret-injection map (§11) aligned with the inventory and G5.
6. Run `validate`/`plan` (never `apply`) in CI as read-only design verification.
7. Defer `apply`, provisioning, and deployment to **INFRA-9** under fresh authorization.

## 18. Validation matrix

| Concern | Design validation (later, plan-only) | Enforced by |
| --- | --- | --- |
| Syntax/type correctness | tool `validate` | INFRA-4 implementation |
| Change preview | tool `plan` (no `apply`) | INFRA-4 implementation |
| Version pinning | exact provider/tool versions, no floating | review + CI |
| No public data tier | RDS/ElastiCache in private subnets, no public IP | plan review |
| TLS-only ingress | ALB/CloudFront HTTPS + ACM only | plan review |
| Least-privilege IAM | resource-scoped policies, no wildcards | plan review |
| Digest-pinned artifacts | image references by `@sha256:` digest | plan review + G4 (later) |
| Exact first-deploy SHA | `3aadb8a…` wired to task-def revision | plan review + G4 (later) |
| Secret injection contract | Secrets Manager `valueFrom` only; 87-field map | plan review + G5 (later) |
| No committed secrets/ids | no value/ARN/account/CIDR literals in source | CI secret scan + review |
| Budget guardrails | Budgets at 50/75/90/100% present | plan review |
| Standard tags present | §A tag set on every resource | plan review |
| Flags remain False | task env sets all three flags `false` | plan review + repo check |

**This document itself** is validated by the INFRA-4 doc gate: Markdown/link checks, exactly two
files changed, all three flags `False`, Alembic head `98289430a3ec`, API contract unchanged, no
app/test/workflow/Dockerfile/migration/dependency change, and no committed secret/account/tenant
identifier.

## 19. Risk register

| # | Risk | Likelihood | Impact | Mitigation |
| --- | --- | --- | --- | --- |
| R1 | Secret value or real identifier committed to IaC source | Low | High | Names/ARNs via variables only; CI secret scan; review; this plan forbids literals. |
| R2 | Mutable image tag used instead of digest | Low | High | Immutable ECR tags; task defs pin `@sha256:`; G4 preflight (later). |
| R3 | Public exposure of RDS/Redis/tasks | Low | High | Private subnets only; SGs least-privilege; no public IP; plan review. |
| R4 | Over-broad IAM policy | Medium | High | Resource-scoped policies; per-role review; no wildcards. |
| R5 | Cost overrun past $200 | Medium | Medium | Budgets 50/75/90/100%; fresh estimate before apply; STOP-and-reauthorize rule. |
| R6 | State backend exposes sensitive attributes | Low | High | Encrypted SSE-KMS state, locking, OIDC-scoped access, never logged. |
| R7 | Accidental `apply` from a plan-only tranche | Low | High | No IaC source in this tranche; apply gated to INFRA-9 fresh auth. |
| R8 | Migration run by a replica / schema race | Low | High | Single migration actor; additive-first; `pending` fail-fast. |
| R9 | Tool selection churn late in implementation | Medium | Low | Decision procedure §16 fixed now; recorded before module authoring. |
| R10 | Drift between design and inventory (87 fields) | Low | Medium | Secret map bound by reference to the inventory; G5 verifies (later). |

## 20. Rollback and recovery (design)

- **This plan:** revert the docs PR — nothing is provisioned, so rollback is a no-op on
  infrastructure.
- **Later IaC PRs (pre-apply):** revert the IaC PR; nothing applied means nothing to tear down.
- **Post-apply (INFRA-9 and beyond, not now):** immutable-artifact redeploy to a prior digest
  for application rollback; `downgrade` (single actor, explicit target) only for a specific
  migration reversal; **IaC destroy** for full teardown under authorization. RDS automated
  backups and a taken restore checkpoint support data recovery. The override clear plane is
  confirmed ready (no override exists).

## 21. Human decision register

Decisions **already made** (upstream, binding):

- Provider/region/compute: **AWS · ECS on Fargate · us-east-1** (ADR-0001).
- Budget: **$200/month hard ceiling** (ADR-0001 / runtime contract §M).
- Static web via **S3 + CloudFront** (runtime contract; SPA takes only `VITE_API_BASE_URL`).
- **Single-AZ / single-task** staging sizing with **production-identical security controls**
  (runtime contract §N).
- First-deploy **exact SHA `3aadb8a1da0f26ffd183a4b05161747038d5957c`** and **digest-pinned**
  artifacts (runtime contract §§A/B).
- **IaC tool: OpenTofu** (§16) — project-owner human decision; authoritative CLI and
  implementation target; Terraform providers/modules reusable for compatibility only.

Decisions **still required** (recorded here, **not decided in this tranche**):

- **OpenTofu and provider versions** (§16) — must be selected and pinned at implementation time
  from then-current official compatibility evidence (the *tool* is decided; the *version* is not).
- Remote-state bucket/lock names, KMS key, and bootstrap timing (§7) — implementation-time.
- Real account id, DNS zone, ACM certificate, CIDR plan, secret ARNs — supplied only at
  authorized implementation/apply time, never committed.
- Fresh dated cost estimate before any apply (§15) — mandatory at INFRA-9.

## 22. Exact future gates

Nothing below is authorized by merging this plan. Each is a separate review + authorization:

- **INFRA-4 implementation:** author the `infra/aws/` IaC source per this design; run
  `validate`/`plan` only; **no `apply`**.
- **INFRA-5:** protected build/deploy workflow with GitHub **OIDC** and a human-approval
  staging environment (no production deploy).
- **INFRA-6:** implement the secret/network/data operational side of the INFRA-3 contract.
- **INFRA-7:** observability readiness before any canary.
- **INFRA-8:** internal tenants and access.
- **INFRA-9:** with **fresh** authorization — cost recalculation, `apply`, provision, deploy the
  exact SHA, verify readiness — **while all global flags remain `False`** and **no override is
  created**.
- **[SEPARATE, LATER] Phase 4B-C activation:** the single-workspace enable override — an
  explicit, standalone authorization, never part of any INFRA tranche.

**Exact stop boundary for INFRA-4 (this tranche): documentation design only. No IaC source, no
`init`/`validate`/`plan`/`apply`, no AWS authentication, no provisioning, no deployment, no
secret operation, no override, and no flag change. Apply requires fresh human authorization at
INFRA-9.**
