# Phase 4B-C infrastructure roadmap (INFRA-1 → INFRA-9)

- **Status:** Planning. Each tranche is separately reviewed and separately authorized.
  Merging any tranche authorizes **no** provisioning, deployment, or Phase 4B-C activation
  beyond that tranche's explicit scope.
- **Decision record:** `docs/architecture/adr-0001-aws-ecs-fargate-staging.md`
- **Runtime contract:** `docs/operations/aws-staging-runtime-contract.md`
- **Baseline SHA:** `3aadb8a1da0f26ffd183a4b05161747038d5957c`
- **Provider / region / compute:** AWS · us-east-1 · ECS on Fargate
- **Budget ceiling:** USD $200/month (hard)

> **Golden rule:** infrastructure setup and canary activation are **never** merged into one
> tranche. Global flags stay `False` until an explicit, separate Phase 4B-C activation
> authorization. No capability override is created by any INFRA tranche.

---

## INFRA-1 — Decision and runtime contract (THIS tranche)

- **Objective:** record the AWS ECS/Fargate decision, the authoritative SIGNALNEST_STAGING
  runtime/security/cost contract, and this roadmap — **documentation only**.
- **Dependencies:** INFRA-0 discovery; locked human decisions (AWS, us-east-1,
  SIGNALNEST_STAGING, $200/month).
- **Expected repository areas:** `docs/architecture/`, `docs/operations/`, `docs/`.
- **Expected external resources:** none.
- **Tests/validation:** Markdown-only diff checks; config flags still `False`; Alembic head
  `98289430a3ec`; no contract/app/test/workflow change.
- **Security review points:** confirm no secret/account-id/tenant-id leaks; no invented env
  vars; safety boundaries preserved.
- **Cost implications:** none (no provisioning).
- **Required human authorization:** review + approve this draft PR.
- **Rollback approach:** revert the docs PR.
- **Exact stop boundary:** STOP after the documentation-only draft PR is open and verified.
  **No provisioning, no AWS auth, no deployment, no override.**

## INFRA-2 — Container and runtime hardening (no deployment)

- **Planning artifact:** [deployment-sha-wiring-plan.md](operations/deployment-sha-wiring-plan.md)
  defines the deployment-SHA provenance design (build → image metadata → ECS task definition →
  API/worker/migration runtime → readiness/preflight evidence) and the fail-closed **G4**
  preflight gate. That document is **documentation-only**; wiring implementation, container,
  workflow, and IaC changes remain separate later tranches.
- **Objective:** ready the images/runtime contract for staging without deploying.
- **Consider:** production container review; non-root execution (already UID/GID 10001);
  image minimization; health/readiness endpoints (`/health`, `/readiness`); graceful
  shutdown; **deployment-SHA injection and reporting** (wire the running Git SHA into
  `build_revision`/`application_version`); runtime configuration validation
  (`ENVIRONMENT=staging`, `APP_MODE=full`); worker health behavior; migration-task behavior;
  the static-web-vs-web-container decision (evidence supports **static S3+CloudFront**);
  tests and security scans.
- **Dependencies:** INFRA-1.
- **Expected repository areas:** `apps/api/` (SHA-reporting wiring only, in its own PR),
  `apps/web/` build config, `docs/`. Any app-code change is a separate reviewed PR — **not**
  bundled with IaC.
- **Expected external resources:** none.
- **Tests/validation:** backend suite, ruff, contract check, container security check
  (`scripts/docker-security-check.sh`).
- **Security review points:** no secrets in images; SHA reporting exposes no sensitive data.
- **Cost implications:** none.
- **Required human authorization:** review + approve.
- **Rollback approach:** revert the PR.
- **Exact stop boundary:** **No deployment during INFRA-2.**

## INFRA-3 — Secrets-management scaffolding and G5 planning (no secret operations)

- **Objective:** produce the repository-side secrets contract for SIGNALNEST_STAGING — a
  complete inventory of every sensitive setting, its exact environment-variable name, and its
  reviewed disposition (AWS Secrets Manager / non-secret configuration / IAM-derived / absent
  in staging / local-development-only) — plus the fail-closed **G5** secret-readiness gate
  definition. **Documentation and placeholder planning only; no secret is created, read,
  rotated, or stored, and no AWS API is called.**
- **Consider:** enumerate every `repr=False` field and every `SecretStr`/secret-like setting
  in `apps/api/app/core/config.py`; map each to its exact environment variable; classify each
  as secret / non-secret / IAM-derived / absent-in-staging / local-only with exactly one
  reviewed disposition; the API, worker, and migration-actor secret boundaries (each actor
  receives only its minimum necessary subset); the browser/SPA exclusion (no backend secret
  ever reaches the `apps/web` build output); the ECS injection contract (Secrets Manager →
  task-definition `secrets`, never plaintext task environment, never a Docker build argument,
  image label, or mutable tag); AWS task-role access where supported; rotation, failure,
  incident, evidence, and rollback documentation rules; and the fail-closed **G5** gate design.
- **Future planned deliverables** (named here, **not created in this amendment**):
  - `docs/operations/aws-staging-secret-inventory.md` — the full field inventory and reviewed
    dispositions.
  - `.env.canary.example` — a root-level, values-free placeholder template (variable names
    only; never a real secret value).
  - minimal roadmap progress/link updates under `docs/`.
- **Delivered (INFRA-3 scaffolding tranche):** the full field inventory, actor-boundary
  matrix, ECS injection contract, operational rules, and detailed fail-closed **G5**
  contract now live in
  [`docs/operations/aws-staging-secret-inventory.md`](operations/aws-staging-secret-inventory.md);
  the values-free placeholder template is the root
  [`.env.canary.example`](../.env.canary.example). Documentation and placeholder only — no
  secret operation, AWS call, IaC, deployment, or activation. Implementing the inventory
  checks and executing G5 remains the later, separately authorized INFRA-3 implementation.
- **G5 — Secret readiness.** A future gate that MUST be **fail-closed**, **read-only**,
  **repeatable**, **non-mutating**, **required before any runtime-canary authorization**,
  **not bypassable by any capability override**, and **incapable of authorizing feature
  activation**. The future G5 MUST verify that: every required secret-bearing setting has
  exactly one reviewed disposition; every `repr=False` field is inventoried; `SecretStr` and
  other secret-like settings are inventoried; ECS secret references match the reviewed
  inventory; the required secret objects, keys, and enabled versions exist; the correct
  execution role can access only the required references; the API, worker, and migration
  actors each receive only their minimum necessary secret subset; browser and SPA artifacts
  receive no backend secret; no required secret is supplied through committed values, Docker
  build arguments, image labels, plaintext task environment, mutable tags, development
  fallbacks, tenant-controlled values, or mock providers; AWS service access uses task roles
  where supported; PostgreSQL, Redis, object-storage, and non-mock LLM requirements fail
  closed; and any missing, empty, malformed, inaccessible, excessive, mismatched, or
  misclassified secret configuration blocks the canary. Secret values are **never** printed,
  logged, diffed, or retained as evidence. **This amendment only adds G5 to the roadmap; the
  future INFRA-3 will document the detailed inventory and the full G5 contract; a separately
  authorized later implementation may implement the gate; no AWS-backed or live G5 check runs
  during this amendment; and all global feature flags remain Boolean `False`.**
- **Dependencies:** INFRA-1, INFRA-2.
- **Expected repository areas:** `docs/operations/`, `docs/`, and a root-level
  `.env.canary.example` placeholder template (all as later deliverables — none created now).
- **Expected external resources:** none created.
- **Tests/validation:** Markdown/link checks; config-flag check (all three flags `False`);
  Alembic head `98289430a3ec`; contract generation clean; inventory-completeness review.
- **Security review points:** no secret value, AWS account id, or tenant id committed; the web
  build is excluded from every secret path; IAM references named only, least-privilege.
- **Cost implications:** none (no provisioning).
- **Required human authorization:** review + approve.
- **Rollback approach:** revert the docs PR.
- **Exact stop boundary:** **planning and scaffolding only.** No secret, no
  `.env.canary.example`, no `docs/operations/aws-staging-secret-inventory.md`, no IaC, no AWS
  authentication, and no G5 execution. Implementing the inventory and gate is the later,
  separately authorized INFRA-3 implementation.

## INFRA-4 — Infrastructure-as-code (plan-only, no apply)

- **Delivered (INFRA-4 plan-only tranche):** the implementation-ready AWS staging IaC
  **design** — target topology, project/module organization, remote state and locking,
  network/ingress, compute and task definitions, immutable digest + exact-SHA wiring, the
  secret-injection map bound by reference to the 87-field inventory, least-privilege IAM,
  database/migration safety, observability, cost/budget guardrails (50/75/90/100%), a
  validation matrix, risk register, rollback/recovery, a human decision register, and the exact
  future gates — now lives in
  [`docs/operations/aws-staging-iac-plan.md`](operations/aws-staging-iac-plan.md).
  **Documentation design only** — no IaC source, no `init`/`validate`/`plan`/`apply`, no AWS
  authentication, no provisioning, no secret operation, and no activation. (Upstream INFRA-3
  secrets scaffolding merged via PR #93.) The IaC tool was **not** selected in that plan-only
  tranche (candidates + a decision procedure were recorded); authoring the `infra/aws/` source is
  the later, separately authorized INFRA-4 implementation, and `apply` remains INFRA-9.
- **IaC tool decision — DECIDED: OpenTofu.** The project owner selected **OpenTofu** as
  SignalNest's authoritative IaC tool (the required INFRA-4 human decision), resolving the sole
  open tool-selection item from the readiness audit. OpenTofu is the authoritative project CLI
  and implementation target for the future placeholder-only INFRA-4 skeleton and subsequent
  authorized AWS tranches; Terraform providers/modules may be reused for compatibility, but
  Terraform and OpenTofu are **not** interchangeable project authorities. The OpenTofu and
  provider **versions** remain to be pinned at implementation time from then-current official
  compatibility evidence. This decision is **documentation-only** and authorizes **no** IaC
  implementation, OpenTofu install, `init`/`validate`/`plan`/`apply`/`import`/`refresh`/`destroy`,
  remote-state creation, AWS authentication, provisioning, deployment, feature-flag change, or
  INFRA-5 work; INFRA-4 remains plan-only and unimplemented, and a separate implementation
  instruction + review are required before any IaC skeleton is created. See
  [`docs/operations/aws-staging-iac-plan.md`](operations/aws-staging-iac-plan.md) §16/§21.
- **Delivered (INFRA-4 repository-only OpenTofu skeleton tranche):** the first IaC
  implementation step — a **repository-only, placeholder-safe** OpenTofu skeleton under
  [`infra/aws/`](../infra/aws/). It establishes: a flat, **staging-only** root (no
  `environments/`/`production/`); bounded compatibility **constraints** (OpenTofu
  `>= 1.12.3, < 1.13.0`; `hashicorp/aws` `>= 6.55.0, < 6.56.0`); root placeholders
  (`versions.tf`, `providers.tf`, empty `backend.tf`, `variables.tf`, `locals.tf` with the
  authoritative eight-tag set, composition-root `main.tf`, `outputs.tf`,
  `terraform.tfvars.example`); **exactly 12 documentation-only module stubs**
  (`network`, `edge`, `alb`, `ecs`, `data_sql`, `data_cache`, `storage`, `registry`, `iam`,
  `secrets`, `observability`, `cost`) — each a `README.md` with no HCL; and IaC-safety
  `.gitignore` rules. **No `.terraform.lock.hcl`, no OpenTofu execution
  (`fmt`/`init`/`validate`/`plan`/`apply`), no provider download, no AWS authentication, no
  state, no module resource bodies, no provisioning or deployment, and no INFRA-5 work.**
  Module resource bodies, tool-assisted validation + the dependency lock, remote-state
  bootstrap, and `apply` remain later, separately authorized tranches; INFRA-4 module
  implementation is **not** yet complete.
- **Objective:** author IaC defining SIGNALNEST_STAGING without applying it.
- **Consider:** AWS provider/version pinning; VPC and networking; ECS cluster and task
  definitions; ALB; RDS PostgreSQL; ElastiCache; S3; ECR; Secrets/KMS references (names
  only); CloudWatch logs and alarms; **AWS Budget definitions (50/75/90/100%)**; resource
  tagging (§A of the contract); SIGNALNEST_STAGING definitions; **plan-only validation
  before any apply**.
- **Dependencies:** INFRA-1, INFRA-2, INFRA-3.
- **Expected repository areas:** a new IaC directory (e.g., `infra/aws/`), `docs/`.
- **Expected external resources:** none created (plan-only).
- **Tests/validation:** IaC static validation / `plan`; no `apply`.
- **Security review points:** least-privilege roles; private DB/Redis; no public exposure;
  no secret values committed.
- **Cost implications:** the IaC encodes the sub-$200 design; a dated estimate accompanies
  it.
- **Required human authorization:** review + approve the IaC; **apply requires fresh, later
  authorization (INFRA-9).**
- **Rollback approach:** revert the IaC PR (nothing applied).
- **Exact stop boundary:** **No `apply` without fresh human authorization.**

## INFRA-5 — Protected build and deployment workflow (no production deploy)

- **Objective:** define the protected CI/CD path for staging.
- **Consider:** GitHub **OIDC** federation (no long-lived AWS keys); immutable image build;
  image-digest recording; **exact Git SHA** stamping; a **staging GitHub environment** with
  **human approval**; the one-shot migration job; ECS deployment; post-deploy health
  verification; rollback to a prior digest.
- **Dependencies:** INFRA-3, INFRA-4.
- **Expected repository areas:** `.github/workflows/` (new staging workflow, separate PR),
  `docs/`.
- **Expected external resources:** none created by authoring the workflow (it runs only
  under later authorization).
- **Tests/validation:** workflow lint; dry-run where possible.
- **Security review points:** OIDC trust scoped to staging; no secrets in logs; approval
  gate enforced.
- **Cost implications:** none until executed.
- **Required human authorization:** review + approve; **no production deployment**.
- **Rollback approach:** revert the workflow PR.
- **Exact stop boundary:** authoring only; execution needs INFRA-9 authorization.

## INFRA-6 — Secrets, networking, data protection, and recovery

- **Objective:** operationalize the secret contract established in INFRA-3 and define secret
  lifecycle, network isolation, and data-protection procedures. This tranche **implements the
  operational side** (secrets-management infrastructure and lifecycle, rotation implementation,
  IAM enforcement, networking and isolation, TLS, data protection, backup and recovery) of the
  repository-side inventory, dispositions, ECS injection contract, and G5 design authored in
  INFRA-3; it does not delete or weaken any requirement inherited from that contract.
- **Consider:** secret creation/rotation procedures; TLS (ALB/CloudFront + ACM); network
  isolation (private subnets, security groups, no public DB/Redis); database initialization;
  backups; restore procedure; egress restrictions (NAT + VPC endpoints); security review.
- **Dependencies:** INFRA-4 (operationalizes the secret contract established in INFRA-3).
- **Expected repository areas:** `docs/`, IaC references.
- **Expected external resources:** none created in this tranche.
- **Tests/validation:** procedure review; restore-runbook completeness.
- **Security review points:** no secret reuse from production; least-privilege egress.
- **Cost implications:** documented; within ceiling.
- **Required human authorization:** review + approve.
- **Rollback approach:** revert the PR.
- **Exact stop boundary:** procedures/definitions only; no secret is created here.

## INFRA-7 — Observability, audit, evidence, and incident readiness (must complete before the canary)

- **Objective:** ensure the canary is fully observable before any override exists.
- **Consider:** logs; metrics; alarms; error monitoring; **audit views**; **gate-decision
  observability** (`opportunity_feedback_gate_decided` / `_failed`); override audit events
  (`workspace_capability_override.created|updated|rejected|cleared`); **secure evidence
  destination**; incident contacts; **independent observer access**; **clear-path evidence**
  (proof the DELETE clear plane works before enabling).
- **Dependencies:** INFRA-4, INFRA-6.
- **Expected repository areas:** `docs/operations/`, IaC (dashboards/alarms).
- **Expected external resources:** none created here (defined for INFRA-9 apply).
- **Tests/validation:** dashboard/alarm definitions reviewed; evidence template
  (`docs/verification/4b-b-feedback-canary.md`) confirmed sufficient.
- **Security review points:** redaction of tenant ids/secrets in logs and evidence.
- **Cost implications:** log/metric retention sized within budget.
- **Required human authorization:** review + approve.
- **Rollback approach:** revert the PR.
- **Exact stop boundary:** **This must complete before the canary.** No override created.

## INFRA-8 — Internal tenant and access readiness

- **Objective:** verify supported provisioning of internal tenants and roles.
- **Consider:** supported organization/workspace provisioning surfaces
  (`POST /auth/register`, `POST /organizations/{organization_id}/workspaces`, RBAC role
  assignment); internal test identities; **operator role**; **observer role**; **three
  independent sessions**; **no SQL provisioning**; **no customer data or integrations**; any
  required application-gap implementation in a **separate PR**.
- **Dependencies:** INFRA-5, INFRA-7.
- **Expected repository areas:** `docs/`; app code only if a provisioning gap is found
  (separate reviewed PR).
- **Expected external resources:** none created by this planning tranche.
- **Tests/validation:** provisioning-surface verification against the running app (only in
  INFRA-9, under authorization); planning/gap analysis here.
- **Security review points:** server-side tenant derivation; no client-invented workspace
  header; session independence.
- **Cost implications:** none.
- **Required human authorization:** review + approve.
- **Rollback approach:** revert the PR.
- **Exact stop boundary:** no tenant/user/session created outside an authorized run.

## INFRA-9 — Authorized staging deployment and Phase 4B-C.0 rerun

- **Objective:** with fresh authorization, provision and deploy SIGNALNEST_STAGING and rerun
  the environment-setup gate — **without** activating the canary.
- **Consider:** **fresh cost estimate** (mandatory pre-provisioning recalculation vs the
  $200 ceiling); exact resource-mutation plan; **human authorization**; infrastructure
  **apply**; **exact SHA `3aadb8a` deployment**; read-only readiness verification; internal
  tenant preparation (INFRA-8 surfaces); **global flags remaining `False`**; **no override
  creation**; rerun of the environment-setup gate (Phase 4B-C.0); separate later Phase 4B-C
  activation authorization.
- **Dependencies:** INFRA-1 through INFRA-8 all approved.
- **Expected repository areas:** `docs/verification/` (evidence), `docs/`.
- **Expected external resources:** the actual AWS staging resources (created **only** here,
  under fresh authorization).
- **Tests/validation:** readiness probes; deployed-SHA verification; isolation preflight;
  budget alerts active; restore checkpoint taken.
- **Security review points:** all §N "identical-to-production" controls present; no customer
  data; no override.
- **Cost implications:** live infra charges begin; must stay under $200 with active budget
  alerts; **STOP-and-reauthorize if projected above $200 without weakening a control**.
- **Required human authorization:** explicit, fresh authorization to spend and deploy.
- **Rollback approach:** immutable-artifact redeploy; IaC destroy for full teardown;
  override clear plane readiness confirmed (no override yet exists).
- **Exact stop boundary:** STOP after environment is verified ready with all flags `False`.
  **Phase 4B-C activation (creating the enable override) is a separate, later, explicitly
  authorized decision — not part of INFRA-9.**

---

## Sequencing summary

```
INFRA-1 (docs) ─▶ INFRA-2 (container/runtime) ─▶ INFRA-3 (secrets scaffolding and G5 planning)
   ─▶ INFRA-4 (IaC plan-only) ─▶ INFRA-5 (protected deployment workflow)
   ─▶ INFRA-6 (secrets, networking, data protection, and recovery)
   ─▶ INFRA-7 (observability before canary) ─▶ INFRA-8 (internal tenants and access)
   ─▶ INFRA-9 (separately authorized apply, deployment, and Phase 4B-C.0 rerun while flags remain False)
   ─▶ [SEPARATE AUTHORIZATION] Phase 4B-C activation: single-workspace enable override (explicit auth)
```

Each arrow is a separate review + authorization gate. Nothing downstream is authorized by
approving an upstream tranche.
