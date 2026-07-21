# Phase 4B-C infrastructure roadmap (INFRA-1 → INFRA-8)

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

## INFRA-3 — Infrastructure-as-code (plan-only, no apply)

- **Objective:** author IaC defining SIGNALNEST_STAGING without applying it.
- **Consider:** AWS provider/version pinning; VPC and networking; ECS cluster and task
  definitions; ALB; RDS PostgreSQL; ElastiCache; S3; ECR; Secrets/KMS references (names
  only); CloudWatch logs and alarms; **AWS Budget definitions (50/75/90/100%)**; resource
  tagging (§A of the contract); SIGNALNEST_STAGING definitions; **plan-only validation
  before any apply**.
- **Dependencies:** INFRA-1, INFRA-2.
- **Expected repository areas:** a new IaC directory (e.g., `infra/aws/`), `docs/`.
- **Expected external resources:** none created (plan-only).
- **Tests/validation:** IaC static validation / `plan`; no `apply`.
- **Security review points:** least-privilege roles; private DB/Redis; no public exposure;
  no secret values committed.
- **Cost implications:** the IaC encodes the sub-$200 design; a dated estimate accompanies
  it.
- **Required human authorization:** review + approve the IaC; **apply requires fresh, later
  authorization (INFRA-8).**
- **Rollback approach:** revert the IaC PR (nothing applied).
- **Exact stop boundary:** **No `apply` without fresh human authorization.**

## INFRA-4 — Protected build and deployment workflow (no production deploy)

- **Objective:** define the protected CI/CD path for staging.
- **Consider:** GitHub **OIDC** federation (no long-lived AWS keys); immutable image build;
  image-digest recording; **exact Git SHA** stamping; a **staging GitHub environment** with
  **human approval**; the one-shot migration job; ECS deployment; post-deploy health
  verification; rollback to a prior digest.
- **Dependencies:** INFRA-2, INFRA-3.
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
- **Exact stop boundary:** authoring only; execution needs INFRA-8 authorization.

## INFRA-5 — Secrets, networking, data protection, and recovery

- **Objective:** define secret lifecycle, network isolation, and data-protection procedures.
- **Consider:** secret creation/rotation procedures; TLS (ALB/CloudFront + ACM); network
  isolation (private subnets, security groups, no public DB/Redis); database initialization;
  backups; restore procedure; egress restrictions (NAT + VPC endpoints); security review.
- **Dependencies:** INFRA-3.
- **Expected repository areas:** `docs/`, IaC references.
- **Expected external resources:** none created in this tranche.
- **Tests/validation:** procedure review; restore-runbook completeness.
- **Security review points:** no secret reuse from production; least-privilege egress.
- **Cost implications:** documented; within ceiling.
- **Required human authorization:** review + approve.
- **Rollback approach:** revert the PR.
- **Exact stop boundary:** procedures/definitions only; no secret is created here.

## INFRA-6 — Observability, audit, evidence, and incident readiness (must complete before the canary)

- **Objective:** ensure the canary is fully observable before any override exists.
- **Consider:** logs; metrics; alarms; error monitoring; **audit views**; **gate-decision
  observability** (`opportunity_feedback_gate_decided` / `_failed`); override audit events
  (`workspace_capability_override.created|updated|rejected|cleared`); **secure evidence
  destination**; incident contacts; **independent observer access**; **clear-path evidence**
  (proof the DELETE clear plane works before enabling).
- **Dependencies:** INFRA-3, INFRA-5.
- **Expected repository areas:** `docs/operations/`, IaC (dashboards/alarms).
- **Expected external resources:** none created here (defined for INFRA-8 apply).
- **Tests/validation:** dashboard/alarm definitions reviewed; evidence template
  (`docs/verification/4b-b-feedback-canary.md`) confirmed sufficient.
- **Security review points:** redaction of tenant ids/secrets in logs and evidence.
- **Cost implications:** log/metric retention sized within budget.
- **Required human authorization:** review + approve.
- **Rollback approach:** revert the PR.
- **Exact stop boundary:** **This must complete before the canary.** No override created.

## INFRA-7 — Internal tenant and access readiness

- **Objective:** verify supported provisioning of internal tenants and roles.
- **Consider:** supported organization/workspace provisioning surfaces
  (`POST /auth/register`, `POST /organizations/{organization_id}/workspaces`, RBAC role
  assignment); internal test identities; **operator role**; **observer role**; **three
  independent sessions**; **no SQL provisioning**; **no customer data or integrations**; any
  required application-gap implementation in a **separate PR**.
- **Dependencies:** INFRA-4, INFRA-6.
- **Expected repository areas:** `docs/`; app code only if a provisioning gap is found
  (separate reviewed PR).
- **Expected external resources:** none created by this planning tranche.
- **Tests/validation:** provisioning-surface verification against the running app (only in
  INFRA-8, under authorization); planning/gap analysis here.
- **Security review points:** server-side tenant derivation; no client-invented workspace
  header; session independence.
- **Cost implications:** none.
- **Required human authorization:** review + approve.
- **Rollback approach:** revert the PR.
- **Exact stop boundary:** no tenant/user/session created outside an authorized run.

## INFRA-8 — Authorized staging deployment and Phase 4B-C.0 rerun

- **Objective:** with fresh authorization, provision and deploy SIGNALNEST_STAGING and rerun
  the environment-setup gate — **without** activating the canary.
- **Consider:** **fresh cost estimate** (mandatory pre-provisioning recalculation vs the
  $200 ceiling); exact resource-mutation plan; **human authorization**; infrastructure
  **apply**; **exact SHA `3aadb8a` deployment**; read-only readiness verification; internal
  tenant preparation (INFRA-7 surfaces); **global flags remaining `False`**; **no override
  creation**; rerun of the environment-setup gate (Phase 4B-C.0); separate later Phase 4B-C
  activation authorization.
- **Dependencies:** INFRA-1 through INFRA-7 all approved.
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
  authorized decision — not part of INFRA-8.**

---

## Sequencing summary

```
INFRA-1 (docs) ─▶ INFRA-2 (container/runtime) ─▶ INFRA-3 (IaC plan-only)
   ─▶ INFRA-4 (protected deploy workflow) ─▶ INFRA-5 (secrets/net/data)
   ─▶ INFRA-6 (observability/audit — before canary) ─▶ INFRA-7 (tenants/access)
   ─▶ INFRA-8 (authorized apply + deploy 3aadb8a + 4B-C.0 rerun; flags stay False)
   ─▶ [SEPARATE] Phase 4B-C activation: single-workspace enable override (explicit auth)
```

Each arrow is a separate review + authorization gate. Nothing downstream is authorized by
approving an upstream tranche.
