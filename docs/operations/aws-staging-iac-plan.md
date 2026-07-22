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
| Edge/DNS | Route53 + ACM | — | — | Public TLS certificates for ALB and CloudFront are **consumed** by ARN, not created by IaC (§23). The `edge` module owns only web/SPA CloudFront + S3 origin + web DNS aliases; the ALB cert/record is deferred to ALB work. |
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
- **Public ingress is TLS-only:** ALB HTTPS listener (port 443 only) and CloudFront HTTPS,
  backed by ACM certificates. The **ALB opens no port 80, defines no HTTP listener, and
  performs no HTTP-to-HTTPS redirect** — plain-HTTP connections are refused. CloudFront
  performs the viewer `redirect-to-https` on the web/SPA path. §24 locks the full ALB contract.
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
- **Two images, three actors (corrected — see §26.5).** The Dockerfile and CI build **two**
  distinct images (`api` and `worker` targets); the API task pins the **API** image by digest,
  and the worker task **and** the one-shot migration task both pin the **worker** image by digest
  (migration overrides the command to `python -m app.db.migrate upgrade`). Both images derive from
  one shared `runtime` base so provenance is aligned, but they are two separate digest-pinned
  artifacts — not one image. The ECS task-definition interface therefore consumes an API image
  reference and a worker image reference, both immutable digests, never a `latest` tag.
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
- **Non-secret fields:** dispositioned as plaintext task environment **only where a value must
  differ from a safe application default** (corrected — the earlier "inject all 76 fields"
  framing is withdrawn; see §26.11). The minimum explicit set is `ENVIRONMENT=staging`,
  `APP_MODE=full`, `LLM_PROVIDER`, `STORAGE_BACKEND=s3`, `S3_BUCKET`, `S3_REGION`, the per-workload
  `QUEUE_BACKEND`/`CACHE_BACKEND`/`VECTOR_BACKEND`, and the three feature flags **all `false`**;
  safe tuning defaults are not duplicated. Field names equal the UPPERCASED model field name.
- **Validator coupling (documented, no code change):** `is_production_like` requires a strong
  `secret_key`, a non-mock `llm_provider` + `llm_api_key`, and `s3_bucket` when
  `storage_backend=s3`. The **migration** task constructs `Settings` at startup and therefore
  receives `SECRET_KEY`, `DATABASE_URL`, and `LLM_API_KEY` — **not** `REDIS_URL` (corrected to
  three, not four; migration is pinned with non-Redis backends so `Settings()` does not require
  `redis_url` — see §26.7). API and worker receive all four.
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
3. Author the modules in **producer-before-consumer** order (corrected in §26.8; `iam` moves
   **after** the resource modules whose ARNs it consumes): `network/` → `edge/` → `alb/` →
   `secrets/` (empty containers, names only) → `registry/` → `storage/` → `data_sql/` →
   `data_cache/` → `iam/` (consumes the secret/repository/bucket/data ARNs) → `ecs/` →
   `observability/` (consumes ECS log-group outputs) → `cost/`, each as an independently reviewed
   PR where practical. Secret **values** are populated out-of-band between the data modules and
   ECS service start (§26.6), never by OpenTofu.
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
- **Edge certificate & hosted-zone ownership: CONSUME, not create** (resolved 2026-07-22, §23) —
  the `edge` module **consumes** an existing CloudFront ACM certificate ARN (in `us-east-1`) and
  an existing Route 53 hosted-zone id via typed inputs; it never creates, requests, validates,
  renews, or imports an ACM certificate or a hosted zone. The **values** (real ARN / zone id /
  domain) are still supplied only at authorized apply time and are never committed — but the
  *architectural ownership* is no longer open.

Decisions **still required** (recorded here, **not decided in this tranche**):

- **OpenTofu and provider versions** (§16) — must be selected and pinned at implementation time
  from then-current official compatibility evidence (the *tool* is decided; the *version* is not).
- Remote-state bucket/lock names, KMS key, and bootstrap timing (§7) — implementation-time.
- Real account id, DNS zone **id**, ACM certificate **ARN**, CIDR plan, secret ARNs — the concrete
  **values** are supplied only at authorized implementation/apply time, never committed. (Edge
  *ownership* of the certificate and hosted zone is architecturally resolved as **consume**, §23;
  only the values remain apply-time.)
- Fresh dated cost estimate before any apply (§15) — mandatory at INFRA-9.

## 23. Edge module architecture decisions (resolved 2026-07-22, human-approved)

These four decisions resolve the previously ambiguous / contradictory `edge` language (the
"create versus consume" tension between the module README and §21) **for static authoring
only**. Nothing here authorizes any AWS action; no resource exists or is deployed. Real domain,
hosted-zone id, and certificate ARN remain future authorized apply-time configuration values and
are never committed.

1. **ACM certificates are consumed, not created.** The `edge` module accepts an existing
   CloudFront ACM certificate **ARN** through a typed input and validates only (statically, no
   AWS call) that the ARN denotes an ACM certificate in **`us-east-1`**. It must **not** create,
   request, validate, renew, or import a certificate, and must **not** declare
   `aws_acm_certificate`, `aws_acm_certificate_validation`, DNS-validation records, ACM data
   sources, or certificate-creation provider aliases. The future **ALB** certificate is outside
   this tranche and is consumed/attached during later ALB work — no ALB certificate input is
   added to `edge` now.
2. **Route 53 hosted zone is consumed, not created.** The module accepts an existing hosted-zone
   **id** through a typed input; it must not create or import a zone. It may create only the
   web-facing `A` and `AAAA` alias records targeting the CloudFront distribution (IPv6 is
   enabled), using CloudFront's exported hosted-zone attribute for the alias target (never a
   hard-coded global id). No `aws_route53_zone`, delegation/NS records, validation records, or
   API/ALB alias records.
3. **Current edge tranche is web/SPA only.** Owned here: a **private** S3 SPA-origin bucket
   (public-access blocked, SSE, lifecycle controls), CloudFront **Origin Access Control**, a
   bucket policy granting access only to the CloudFront distribution, the CloudFront
   distribution, and the web `A`/`AAAA` aliases. The private SPA-origin bucket belongs to `edge`;
   the `storage` module stays reserved for application/runtime storage and remains stub-only.
   **Deferred:** all ALB resources/listeners/SGs/certificate attachment, the API hostname and its
   Route 53 record, ALB-to-DNS integration, ECS origins, WAF, asset upload/deployment, CloudFront
   invalidation, access-log delivery, and observability integration. The API Route 53 alias is
   added only in a later, separately authorized cross-module pass after the ALB exposes its DNS
   name and canonical hosted-zone id. After this tranche `network` and `edge` are the only
   implemented resource modules; the root has exactly the `network` and `edge` child-module
   blocks; the other ten modules remain documentation-only stubs.
4. **Hostnames and CloudFront SPA behavior.** The module accepts one complete, caller-supplied
   web **FQDN** (no apex-domain derivation, no committed real domain; docs use reserved examples
   such as `app.staging.example.com` / `api.staging.example.com`, and the API hostname is
   explanatory/deferred only — not a current input or resource). CloudFront SPA policy: private
   S3 **REST** origin via OAC/**SigV4** (no S3 website endpoint, no public-read ACL/policy);
   default root object `index.html`; viewer protocol `redirect-to-https`; custom-domain TLS via
   the consumed certificate ARN with `sni-only` and minimum `TLSv1.2_2021`; compression enabled;
   allowed methods `GET`/`HEAD`/`OPTIONS`, cached `GET`/`HEAD`; no cookie, query-string, or
   arbitrary-header forwarding; min/default/max TTL `0`/`3600`/`86400`; SPA fallback mapping
   CloudFront origin **403 → `/index.html` (200)** and **404 → `/index.html` (200)** with
   error-caching TTL `0`; IPv6 enabled; no geographic restriction; a typed `price_class`
   defaulting to `PriceClass_100` (allowed `PriceClass_100`/`PriceClass_200`/`PriceClass_All`).
   No Lambda@Edge, CloudFront Functions, WAF, multiple origins, custom cache/response-header/
   origin-request policies, signed URLs/cookies, or API behavior; no object upload or
   invalidation.

## 24. ALB module architecture decisions (resolved 2026-07-22, human-approved)

These decisions lock the `alb` module architecture contract and resolve the previously stale /
omitted ALB language (the `/readiness`-as-probe statement in the runtime contract §D, the
`alb_security_group_id`-as-input and stale `tags` inputs in the `alb`/`ecs` README stubs, and the
absence of a recorded SG-ownership / health-check / TLS / header / logging / lifecycle register).
**Nothing here authorizes any AWS action or any HCL.** The `alb` module remains a
documentation-only stub; no ALB resource is authored, planned, provisioned, or deployed. Real
certificate ARN, account id, and DNS values remain future authorized apply-time configuration and
are never committed. The decisions are verified against committed application behavior (§25).

1. **Health-check semantics.** The ALB target-group health check probes the shallow,
   dependency-free liveness endpoint **`/health`** (root route, `apps/api/app/main.py`), which
   returns a constant `{"status":"ok","mode":...}` with no database/cache/network access. The
   dependency-aware **`/readiness`** endpoint (`/api/v1/system/readiness`, which returns `503`
   when a required backend is down) is **never** the ALB probe: because ECS replaces tasks that
   fail their load-balancer health check, probing `/readiness` would turn a shared database
   outage into a task-replacement loop. `/readiness` remains the operational readiness signal
   only. This supersedes the earlier runtime-contract §D wording.

2. **Security-group ownership (cycle-free).** The `alb` module **creates and owns the ALB
   security group** and exposes it as output `alb_security_group_id`. The `ecs` module **creates
   and owns the API task security group** and **owns both cross-SG rule resources**: ALB SG
   egress → API task SG on **TCP 8000 only**, and API task SG ingress ← ALB SG on **TCP 8000
   only**. `ecs` **consumes** `alb_security_group_id` and `api_target_group_arn`. The `alb`
   module **never** consumes an ECS/API security-group id. This yields a one-way module
   dependency **`ecs -> alb`** (acyclic). `network` owns neither service security group. No
   CIDR-based ALB-to-task substitute and no unrestricted ALB security-group egress are
   authorized.

3. **Exposure and addressing.** Internet-facing ALB (`internal = false`) placed **only in the
   `network` public subnets**; targets remain in the private application subnets. **IPv4 only**
   (`ip_address_type = "ipv4"`) — no dual-stack / IPv6 ingress for this staging tranche. Public
   ingress is **TCP 443 from `0.0.0.0/0`**. **No port 80, no HTTP listener, no HTTP-to-HTTPS
   redirect**; HTTPS is mandatory.

4. **TLS.** An existing regional ACM certificate is **consumed, never created or queried**,
   through the root/module variable **`api_certificate_arn`**; it must be valid for the configured
   `us-east-1` provider. TLS policy **`ELBSecurityPolicy-TLS13-1-2-2021-06`**. TLS terminates at
   the ALB; ALB-to-target traffic is plain **HTTP** within the restricted VPC security-group path.

5. **ALB and request-handling attributes.** `internal = false`; `ip_address_type = "ipv4"`;
   HTTP/2 enabled; idle timeout **60 seconds**; invalid header fields dropped; desync mitigation
   **`defensive`**; host-header preservation **disabled**; X-Forwarded-For handling **`append`**;
   X-Forwarded-For client-port preservation **disabled**; deletion protection **disabled**
   (staging only); cross-zone behavior uses the ALB's enabled/default configuration **without a
   target-group override**.

6. **Target group.** Target type **`ip`**; protocol **HTTP**; protocol version **HTTP/1.1**;
   port **8000**; health-check **enabled**, protocol **HTTP**, port **`traffic-port`**, path
   **`/health`**, matcher **`200`**, interval **30s**, timeout **5s**, healthy threshold **2**,
   unhealthy threshold **3**, deregistration delay **60s**, stickiness **disabled**, slow start
   **disabled / zero**, load-balancing algorithm **round robin**.

7. **Logging and later integrations (deferred).** ALB **access logging and connection logging
   are deferred** to the storage/logging integration tranche; the `storage` module will own the
   S3 bucket and delivery policy, and any later `aws_lb.access_logs.bucket` value must consume an
   S3 **bucket name, not an ARN**. No conditional or placeholder log-bucket input is added to the
   initial ALB module. Logging must be resolved **before any live staging plan/apply
   authorization**. **WAF** remains deferred. **API Route 53 alias** creation remains deferred
   (added only after the ALB exposes its DNS name and canonical hosted-zone id). The future ALB
   module must eventually expose outputs: `alb_arn`, `alb_dns_name`,
   `alb_canonical_hosted_zone_id`, `https_listener_arn`, `api_target_group_arn`, and
   `alb_security_group_id`.

8. **Naming and tagging.** Use the committed `signalnest-staging` naming convention. Tags are
   supplied through the root provider `default_tags`; the stale module-level `tags` inputs are
   removed from the `alb` and `ecs` README contracts and **no module-level `tags` input is
   added** (mirrors `network`/`edge`).

## 25. ALB application-contract verification and pre-live-apply follow-ups

The decisions in §24 were verified read-only against committed application behavior at
`main` HEAD `5104de8`. Compatibility summary (all `verified compatible` unless noted):

- API binds `0.0.0.0:8000`, container `EXPOSE 8000` (`apps/api/Dockerfile`) — matches target-group
  port 8000, target type `ip`, HTTP/1.1.
- `/health` is dependency-free and anonymous (`apps/api/app/main.py`); `/readiness` is
  dependency-aware and returns `503` on a failed required probe (`apps/api/app/system/routes.py`,
  `apps/api/app/system/probes.py`) — confirms the §24.1 split.
- No WebSocket, SSE, streaming, or long-polling exists; heavy/LLM work is off-request via the
  durable job queue and worker process — compatible with the 60s idle timeout and HTTP/1.1.
- Auth is stateless bearer JWT with no server-side session (`apps/api/app/auth/dependencies.py`)
  and the app is stateless across replicas — compatible with round robin and stickiness disabled.
- No `TrustedHostMiddleware`, host-based routing, or Host-dependent redirect/cookie logic exists —
  host-header preservation disabled is safe. CORS origins are an explicit config list, not derived
  from the Host header.
- The app imposes no IPv6 requirement (IPv6 is a CloudFront/web-path concern only) — IPv4-only ALB
  is compatible.

**Pre-live-apply follow-ups (owned outside the ALB module; do not change any §24 attribute):**

- **Rate limiting behind the proxy.** `RateLimitMiddleware` keys on `request.client.host` and does
  not read `X-Forwarded-For` (`apps/api/app/core/middleware.py`); the code documents it as a
  placeholder ("production uses Redis adapter"). Once the ALB fronts the API, `request.client.host`
  becomes the ALB ENI IP, so the naive fixed-window limiter collapses to a single shared bucket
  across all clients. Resolution is application/ECS-layer (enable uvicorn `--proxy-headers` /
  `--forwarded-allow-ips` and trust the client-most `X-Forwarded-For`, or land the Redis adapter),
  or explicitly accept a global staging-only limit until then. No ALB attribute changes.
- **API graceful shutdown.** The API uvicorn command sets no explicit graceful-shutdown window
  (`apps/api/Dockerfile`); the ECS tranche must align ECS `stopTimeout` with the 60s
  deregistration delay so in-flight requests drain cleanly.
- **Interactive docs exposure.** `/api/v1/docs` and `/api/v1/openapi.json` are mounted
  unconditionally (`apps/api/app/main.py`); with a single all-paths target group and WAF deferred,
  they become internet-reachable — to be addressed with the deferred WAF / path-restriction work.

## 26. ECS dependency and ownership decisions (resolved 2026-07-22, human-approved)

Locks the future `iam`/`secrets`/`registry`/`storage`/`data_sql`/`data_cache`/`ecs`/`observability`
module contract so it is **acyclic and implementation-ready**. **Documentation only — no HCL is
authorized.** All modules named here remain documentation-only stubs; nothing is provisioned. This
section supersedes contradictory earlier wording (the §10 "one digest", the §17 ordering, and the
data/log-group/`tags` language in the affected module READMEs). Notation throughout is
**`producer -> consumer`** (the arrow points to the module that consumes the named output).

### 26.1 Dependency notation and the network/edge relationship
Every edge is stated as `producer -> consumer : <exact output consumed>`. The root wiring
(`infra/aws/main.tf:37-74`) shows `edge` consumes **only root variables** (`web_fqdn`,
`hosted_zone_id`, `acm_certificate_arn`, `price_class`, `name_prefix`) and references **no**
`module.network` output — so there is **no `network -> edge` edge**; `network` and `edge` are
independent foundational modules. `alb` consumes `network` (`vpc_id`, `public_subnet_ids`) and a
root `api_certificate_arn`. The prior audit's `iam -> storage` phrasing was ambiguous and is
corrected to **`storage -> iam`**: `storage` creates and outputs `bucket_arn`; `iam` consumes it to
build identity policies. `storage` never consumes an IAM-role output.

### 26.2 Per-workload security-group ownership (three task SGs — NOT one shared)
`ecs` creates and owns **three** task security groups — **API**, **worker**, and **migration** —
not one shared application SG. Rationale: only the API receives ALB traffic; the three workloads
have different egress needs; SGs have no recurring cost; separate SGs avoid granting every workload
the union of permissions; IAM cannot scope PostgreSQL/Redis/public-HTTPS reachability. Ownership:
- `alb` owns the ALB SG, public IPv4 TCP 443 ingress to the ALB, and consumes **no** ECS SG id.
- `ecs` owns the API/worker/migration task SGs **and every standalone cross-SG rule that involves a
  task SG** (both directions), authored with the AWS provider 6.55.0 families
  `aws_vpc_security_group_ingress_rule` / `aws_vpc_security_group_egress_rule` — never inline blocks
  mixed with standalone rules on the same SG.
- `data_sql` owns the PostgreSQL/RDS SG and **outputs** its id; `data_cache` owns the Redis SG and
  **outputs** its id. Neither data module consumes an ECS output. `ecs` **consumes** both data SG
  ids to author the task↔data rules → one-way `data_sql -> ecs`, `data_cache -> ecs` (acyclic).
- No central security-group module is introduced. No VPC-CIDR-based PostgreSQL/Redis ingress. No
  public port 8000. Tasks run in `network` private subnets with `assign_public_ip` disabled. ALB
  target type remains `ip`.

### 26.3 Exact private cross-SG traffic matrix (all task-side rules owned by `ecs`)
- **ALB↔API:** ALB SG egress → API task SG **TCP 8000**; API task SG ingress ← ALB SG **TCP 8000**.
  No ALB rule targets the worker or migration SG. No unrestricted ALB SG egress.
- **PostgreSQL (5432):** egress **TCP 5432** to the PostgreSQL SG from the API, worker, **and**
  migration task SGs; PostgreSQL SG ingress **TCP 5432** as **three separate** standalone rules
  (from API, from worker, from migration). Owned by `ecs`; destination SG created by `data_sql`.
- **Redis (6379):** egress **TCP 6379** to the Redis SG from the API and worker task SGs; Redis SG
  ingress **TCP 6379** as **two separate** rules (from API, from worker). Owned by `ecs`;
  destination SG created by `data_cache`. **Migration is explicitly prohibited from Redis access** —
  executable Settings validation (`apps/api/app/core/config.py:306-312`) requires `redis_url` only
  when `app_mode=full` and `queue_backend=redis`/`cache_backend=redis`; the migration task is pinned
  with `queue_backend=inprocess` / `cache_backend=memory` (permitted in staging, which is
  `is_production_like` but not `is_production`, so the production-only backend forbiddance at
  `config.py:319-346` does not apply), so migration constructs `Settings()` without Redis. No
  migration→Redis rule is created.

### 26.4 Outbound HTTPS and DNS baseline (NAT, not SG-referenced; no DNS SG rule)
SG-**referenced** destinations apply only to private ENIs (ALB, PostgreSQL, Redis). Public services
reached over the existing single-NAT path have **no repository-owned destination SG**, so, until a
separately authorized VPC-endpoint / egress-proxy / AWS Network Firewall layer exists, the staging
baseline for each task SG that needs it is **TCP 443 IPv4 egress via NAT** for: ECR image
retrieval, Secrets Manager injection, CloudWatch Logs delivery, application S3 access, approved LLM
providers, and any other enabled external-HTTPS integration. **No** all-protocol `0.0.0.0/0` egress,
no unrestricted port range, no IPv6 egress (unless IPv6 is separately designed). Fargate platform
operations (ECR pull, ECS secret injection, `awslogs` delivery) traverse the **task ENI/NAT** path
and therefore require working private-subnet + NAT connectivity. Security groups **cannot** filter
by DNS name and **cannot** block the Route 53 Resolver, so **no AmazonProvidedDNS (UDP/TCP 53) SG
rule is added**; domain-aware DNS filtering (Route 53 Resolver DNS Firewall) and VPC endpoints
remain separately authorized improvements. Dark connectors remain gated by Boolean feature flags and
application authorization, not by SG destination filtering.

### 26.5 Two-image artifact contract
Two immutable, digest-pinned images (`api`, `worker`; `apps/api/Dockerfile:94-118`,
`.github/workflows/ci.yml:240-260`). API task = API image digest; worker task = worker image
digest; **migration task = worker image digest** with command override `python -m app.db.migrate
upgrade` (`.github/workflows/ci.yml:276-279`). No `latest` accepted by the ECS interface. Registry
documentation must not claim one image serves all three actors. First-deploy SHA
`3aadb8a1da0f26ffd183a4b05161747038d5957c` (G4). No image is built or pushed in this tranche;
build/push is INFRA-5.

### 26.6 Secret container vs. value lifecycle
- `secrets` creates the four secret **containers** (Secrets Manager + KMS) and outputs `secret_arns`
  only; it does **not** create `aws_secretsmanager_secret_version` resources holding application
  values. Secret **values** never enter Git, `.tfvars`, HCL, OpenTofu variables/locals/outputs/plan/
  state.
- `DATABASE_URL` and `REDIS_URL` embed endpoints produced by `data_sql`/`data_cache`; their values
  can be composed only **after** those endpoints exist and are populated **out-of-band** by a
  separately authorized operational procedure (INFRA-6). ECS consumes secret **ARNs** only.
- Future live rollout is staged and **not authorized now**: (1) prerequisite infrastructure →
  (2) secret-value population → (3) fail-closed G5 secret-readiness check → (4) ECS service
  creation/start → (5) separately authorized migration execution. A live apply must not assume an
  empty container is sufficient for service start. The permanent secret-operator principal remains
  an **undecided live-operation gate** (INFRA-6) and does not block offline HCL.

### 26.7 Per-workload secret injection (smallest sufficient subset, from executable validation)
Derived from `apps/api/app/core/config.py` `_validate_runtime` (`:302-389`). All are ECS `secrets`
`valueFrom` ARN references, never plaintext.
- **API:** `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `LLM_API_KEY`.
- **Worker:** `SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `LLM_API_KEY`.
- **Migration:** `SECRET_KEY`, `DATABASE_URL`, `LLM_API_KEY` — **REDIS_URL excluded** (§26.3
  rationale). Granting migration `REDIS_URL` would be a G5 `ACTOR_SUBSET_EXCEEDED` over-provision.
  This resolves the prior "three vs four migration secrets" contradiction in favor of **three**, by
  executable behavior. `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY` remain **unset** for all workloads
  (task-role credential chain). No secret is granted merely because another workload has it.

### 26.8 IAM roles and cycle breaks
One shared **execution role** + three distinct **application task roles** (API/worker/migration);
`ecs` consumes all four ARNs (`iam` outputs `execution_role_arn`, `api_task_role_arn`,
`worker_task_role_arn`, `migration_task_role_arn`). IAM roles are created outside ECS.
- **Execution role:** ECR retrieval, CloudWatch Logs stream creation + delivery, retrieval of only
  the secret ARNs referenced in task definitions, and `kms:Decrypt` only if a separately approved
  customer-managed KMS key requires it. Application containers do not receive execution-role creds.
- **Application task roles:** only the AWS API calls the code actually makes. S3 access only for
  workloads proven to use S3. **No** RDS/Redis IAM permission (socket access is a network/credential
  matter unless IAM DB auth is separately implemented). **No** ECR / Logs-driver / secret-injection
  permissions on application roles. Migration task role is empty/minimal unless migration code calls
  an AWS API.
- **Dependency direction:** `secrets -> iam`, `registry -> iam`, `storage -> iam`; `iam -> ecs`.
  `data_sql`/`data_cache` -> `iam` **only if** executable code and the approved runtime require
  data-resource IAM permissions; stale ARN inputs without a demonstrated consumer are dropped.
- **CloudWatch scoping breaks `iam -> ecs -> iam`:** IAM scopes the execution-role Logs policy to a
  **deterministic name prefix** (`arn:aws:logs:us-east-1:<account>:log-group:/ecs/${name_prefix}-*:*`)
  built from `name_prefix`, **never** by consuming ECS/observability `log_group_arns`. The
  `log_group_arns` input is removed from the `iam` contract. Resource wildcards are prefix-scoped
  wherever AWS supports resource-level scoping; the narrow AWS-required exception is ECR
  authorization (`ecr:GetAuthorizationToken`) which AWS only supports at `Resource: "*"` — documented
  as the smallest required exception, not a blanket wildcard.

### 26.9 Log-group ownership (ECS owns; observability consumes)
`ecs` creates and owns **three** deterministic CloudWatch Logs groups (`/ecs/<name_prefix>-api`,
`/ecs/<name_prefix>-worker`, `/ecs/<name_prefix>-migration`) — one per workload — and outputs their
names/ARNs. Staging retention **30 days**; default CloudWatch encryption at rest is acceptable for
this tranche (a customer-managed KMS design remains separately authorized). `awslogs-create-group`
is disabled/omitted; OpenTofu-created groups must exist before task start. Each task definition uses
its group, region, and stream prefix; the execution role holds the delivery permissions.
`observability` **consumes** the ECS log-group outputs for metric filters/alarms and does **not**
create the ECS workload log groups. This preserves `iam -> ecs -> observability` and forbids
`iam -> observability -> ecs -> iam` and `ecs -> observability -> ecs`. Logging remains required
before any live staging plan/apply.

### 26.10 Runtime and deployment baseline
ECS/Fargate, Linux, **X86_64** (matches the current CI `linux/amd64` build — `ci.yml` sets no
`platforms:`), Fargate platform version **1.4.0**, private subnets, public IP disabled. Three task
definitions (API/worker/migration); two long-running services (API, worker); migration is a
**one-shot** task, never a service. API container port **8000/TCP**, health path **`/health`**
(bare, `apps/api/app/main.py:103-105`), ALB target type **`ip`**. API desired count **1**, worker
desired count **1** (staging). Baseline task size **256 CPU / 512 MiB** per workload. API and worker
deployment **minimum-healthy 100% / maximum 200%**, deployment **circuit breaker enabled with
rollback**. API **health-check grace period 60s**. **ECS Exec disabled** by default. Autoscaling
**deferred**. Immutable digest-pinned images required. Read-only root filesystem + writable `/tmp`
preserved from the container contract (`apps/api/Dockerfile`). Graceful shutdown reflects the actual
application behavior: exec-form PID 1 receives SIGTERM directly; the worker drains within
`worker_shutdown_grace_seconds` (default 10s, `config.py:175`); ECS `stopTimeout` must be ≥ that
worker grace, and the API service `stopTimeout` should accommodate the ALB 60s deregistration delay.

### 26.11 Ordinary environment configuration
ECS injects **only** the values that must differ from safe application defaults, avoid forbidden
dev/mock/local behavior, set explicit staging feature flags, select workload backends, or supply
resource identifiers. The prior "inject all 76 non-secret fields" statement is withdrawn — safe
tuning defaults are not duplicated. The minimum explicit set (executable names): `ENVIRONMENT=staging`,
`APP_MODE=full`, `LLM_PROVIDER` (openai/anthropic — mock forbidden), `STORAGE_BACKEND=s3`,
`S3_BUCKET`, `S3_REGION`, `QUEUE_BACKEND`/`CACHE_BACKEND`/`VECTOR_BACKEND` (per workload; migration
uses non-Redis backends per §26.3), and the three global capability flags explicitly **`false`**
(`OPPORTUNITY_FEEDBACK_ENABLED`, `SCOUT_SCHEDULING_ENABLED`, `CONNECTOR_RSS_ENABLED`). AWS access-key
env vars remain unset (task-role credential chain). If the ECS interface uses per-workload
`map(string)` env inputs, it must enforce a **denylist precondition** rejecting at least:
`SECRET_KEY`, `DATABASE_URL`, `REDIS_URL`, `LLM_API_KEY`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`,
`AWS_ACCESS_KEY_ID`, `AWS_SECRET_ACCESS_KEY`, `AWS_SESSION_TOKEN`.

### 26.12 Complete acyclic module graph
`producer -> consumer : output`. Independent roots have no in-graph producer.
- `network -> alb : vpc_id, public_subnet_ids`
- `network -> data_sql : private_subnet_ids` ; `network -> data_cache : private_subnet_ids`
- `network -> ecs : vpc_id, private_subnet_ids`
- `edge` : independent (root vars only)
- `secrets -> iam : secret_arns` ; `secrets -> ecs : secret_arns`
- `registry -> iam : repository_arns` ; `registry -> ecs : repository_urls (2 image refs)`
- `storage -> iam : bucket_arn`
- `data_sql -> ecs : rds_security_group_id (+ db_endpoint via out-of-band secret value, not HCL)`
- `data_cache -> ecs : redis_security_group_id (+ redis_endpoint via out-of-band secret value)`
- `iam -> ecs : execution_role_arn, api/worker/migration_task_role_arn`
- `alb -> ecs : alb_security_group_id, api_target_group_arn`
- `ecs -> observability : log_group_names/arns, service names`
- `cost` : independent (budgets)

No module consumes its own downstream output; no `storage↔iam`, `iam↔logging↔ecs`, `data↔ecs`,
`secrets↔data↔secrets`, or `observability↔ecs` cycle exists. Distinct phases: (1) implementation/
authoring order (§26.8 / §17); (2) offline root-composition readiness (each module offline-validates
in isolation with placeholder vars today; root composition of `ecs` becomes possible only after its
eight upstreams are authored); (3) future live prerequisite deployment; (4) out-of-band secret-value
population; (5) future ECS deployment; (6) separately authorized migration execution. **These are not
one live apply.**

### 26.13 Preserved ALB decisions (unchanged)
§24 stands: `alb` owns its SG and public IPv4 TCP 443 ingress, no public port 8000, target type
`ip`, consumes no ECS output, no unrestricted egress; `ecs` owns the API task SG and both ALB↔API
TCP 8000 cross-SG rules; one-way `alb -> ecs`.

### 26.14 Explicitly deferred / separately authorized
All prerequisite-module HCL, ECS HCL, root-composition changes, the `terraform.tfvars.example`
`api_certificate_arn` placeholder (a pre-live, non-blocking follow-up — not changed here), VPC
endpoints, domain-aware egress filtering, customer-managed KMS keys (unless already mandatory), live
AWS plan/apply, secret-value population, container build/push, ECS migration execution, observability
implementation, autoscaling, ECS Exec, production deployment, and feature activation all remain
separately authorized. **INFRA-4 remains incomplete; INFRA-5 remains unstarted.**

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
