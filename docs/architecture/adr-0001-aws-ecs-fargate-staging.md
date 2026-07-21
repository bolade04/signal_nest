# ADR-0001 — AWS ECS/Fargate for the SignalNest internal staging (canary) runtime

- **Status:** Accepted for planning and later separately authorized implementation.
  This ADR authorizes **no** provisioning and **no** spending.
- **Decision date:** 2026-07-21
- **Phase:** 4B-C.INFRA-1 (documentation only)
- **Supersedes / relates to:** `docs/phase-4b-plan.md`, `docs/phase-4b-b-plan.md`,
  `docs/operations/deployment.md`, `docs/operations/aws-staging-runtime-contract.md`
  (authoritative runtime contract), `docs/phase-4b-c-infra-plan.md` (roadmap).

> This ADR records an architecture selection only. It does not create an AWS account,
> organization, resource, role, secret, or budget; it does not authenticate to AWS; it
> does not deploy SignalNest; and it does not authorize any charge. All AWS provisioning
> and deployment are deferred to later, separately authorized INFRA tranches.

---

## 1. Context

Phase 4B activates the `opportunity_feedback` capability in a **single internal canary
workspace** via the governed override control plane, keeping the global flag `False`
(`docs/phase-4b-plan.md`, `docs/phase-4b-b-plan.md`). The read-only INFRA-0 discovery
established that **no reachable, authorized, production-like runtime exists**:
`infra/docker-compose.yml` is a local developer convenience with throwaway passwords, and
there is no deploy pipeline (`docs/operations/deployment.md`). The Phase 4B-C canary
therefore requires a real, secure, non-customer staging environment before any override
can be created.

This ADR converts the human hosting decisions into an authoritative selection.

## 2. Locked human decisions

| # | Decision | Value |
| - | -------- | ----- |
| 1 | Primary cloud provider | **AWS** |
| 2 | Primary container compute | **Amazon ECS on AWS Fargate** |
| 3 | Staging region | **us-east-1** |
| 4 | Logical staging environment alias | **SIGNALNEST_STAGING** |
| 5 | Hard monthly staging budget ceiling | **USD $200/month (hard guardrail, not a target)** |
| 6 | Budget interpretation | Target lower cost where security and required canary behavior stay intact; never weaken required security to fit budget; separate predictable AWS infra charges from variable third-party AI/LLM/API usage; if the minimum safe design cannot fit under $200, report explicitly. |
| 7 | Production interpretation | SIGNALNEST_STAGING is production-**like** but **not** customer-production. Single-AZ / reduced-replica staging is acceptable **when documented and safe for internal testing**; a single-AZ staging environment must never be described as fully highly-available production. |

## 3. Current application-topology evidence

Derived from repository evidence (not assumption):

- **Web** — React SPA (`docs/architecture.md`; `apps/web` has **no Dockerfile**; built by
  Vite to `apps/web/dist`; reads `VITE_API_BASE_URL` at build — `apps/web/src/api/config.ts`).
  It contains no business, scoring, authorization, or tenant-isolation logic.
- **API** — FastAPI modular monolith, `uvicorn app.main:app`, PID 1, port 8000
  (`apps/api/Dockerfile` target `api`; liveness `GET /health`; readiness
  `GET /readiness` + operator `GET /internal/system/readiness`; readiness actively probes
  backends — `apps/api/app/system/probes.py`).
- **Worker** — `python -m app.jobs.worker`, no port (`Dockerfile` target `worker`),
  SIGTERM drain lifecycle.
- **Migration actor** — one-shot `python -m app.db.migrate`; replicas never migrate
  (`docs/operations/deployment.md`, `docs/operations/migrations.md`).
- **PostgreSQL + pgvector** — required in production-shaped mode (`app/core/config.py`
  rejects SQLite / brute-force vector when `environment=production`).
- **Redis** — cache + durable-job availability pub/sub (`signalnest:jobs:available`);
  `environment=production` rejects in-process/in-memory backends.
- **S3-compatible object storage** — required (`storage_backend=s3`, `s3_bucket`).
- **LLM provider** — staging/production forbid `mock` (`config.py`); a real
  openai/anthropic key is required for startup validation even though the feedback gate
  itself makes no model call.
- **Governed capability control plane** — operator API
  `/internal/system/capabilities/{registry,effective,overrides}` with `PUT`/`DELETE`
  override planes (`apps/api/app/system/internal_capabilities_routes.py`); audit actions
  `workspace_capability_override.{created,updated,rejected,cleared}`
  (`app/capabilities/service.py`); gate events `opportunity_feedback_gate_decided` /
  `_failed` (`app/feedback/routes.py`).
- **No** SMTP/email sender, **no** WebSockets, **no** scheduler process active
  (`scout_scheduling_enabled=False`), **no** live RSS (`connector_rss_enabled=False`).

## 4. Alternatives considered

### 4.1 GCP managed containers (Cloud Run + Cloud Run Jobs)
Excellent contract fit (native one-shot jobs for the migration actor, IAM-based storage
credentials, readiness/liveness split). **Rejected** only because the human decision
locks AWS as the primary provider; retained as the closest cross-cloud equivalent should a
future migration trigger fire (§10).

### 4.2 Lower-complexity managed application platform (Render / Fly.io / Railway)
Fastest path to an isolated non-customer canary with platform-managed Postgres/Redis and a
pre-deploy migration hook. **Rejected as the primary** because it offers coarser
network-isolation, secret-management, IAM, and audit controls than AWS-native services, and
a lower enterprise ceiling for later customer-production phases. It remains a valid
break-glass fallback if AWS onboarding stalls, but is **not** selected.

### 4.3 Self-managed Kubernetes / Nomad on EC2
Technically capable but **premature**: it requires building and operating cluster
lifecycle, upgrades, networking, and security hardening for a single low-traffic internal
canary. `docs/operations/deployment.md` explicitly places orchestrator manifests in
"future / out of scope." **Rejected** for this phase; revisit only when customer-production
scale and multi-team operational maturity justify the burden (§10).

### 4.4 Local `infra/docker-compose.yml` as the canary runtime
**Rejected outright.** It uses throwaway passwords, no orchestration, no managed
secrets/backups, no isolated network, and no deploy/rollback path. It is a developer
convenience only (`infra/docker-compose.yml` header; `docs/operations/deployment.md`). It
cannot verify tenant isolation, audit durability, observability, or rollback under
production-like conditions and must never stand in for SIGNALNEST_STAGING.

## 5. Decision

Select **Amazon ECS on AWS Fargate in us-east-1** as the primary hosting architecture for
**SIGNALNEST_STAGING**, the internal, non-customer, production-like staging (canary)
environment, subject to the **$200/month hard budget ceiling**.

Fargate is the **primary compute** for the containerized services (API, worker, one-shot
migration task). Where repository evidence supports a more suitable AWS-native service for a
specific workload, that service is used and the evidence is documented:

- **Web SPA → Amazon S3 + CloudFront** (static hosting). Evidence: `apps/web` is a Vite
  SPA with **no Dockerfile** and no server-side rendering; it only needs `VITE_API_BASE_URL`
  fixed at build time. A container is unnecessary and more expensive; static hosting
  preserves all required behavior and TLS.
- **Object storage → Amazon S3** (native; `storage_backend=s3`).
- **PostgreSQL+pgvector → Amazon RDS for PostgreSQL** (managed; pgvector extension).
- **Redis → Amazon ElastiCache (Redis/Valkey)** (managed) if the staging profile enables a
  Redis-backed cache/queue-coordination path; otherwise documented as deferrable.
- **Container images → Amazon ECR**; **secrets → AWS Secrets Manager + KMS**;
  **logs/metrics/alarms → Amazon CloudWatch**; **control-plane audit → AWS CloudTrail**.

The authoritative logical topology, security controls, and cost contract are defined in
`docs/operations/aws-staging-runtime-contract.md`. The staged implementation sequence is in
`docs/phase-4b-c-infra-plan.md`.

## 6. Why AWS ECS/Fargate

- **Contract fit.** The one-shot migration actor maps cleanly to an ECS run-task; the
  readiness-vs-liveness split (`/readiness` vs `/health`) maps to ALB/target-group health
  checks; per-service horizontal scale-by-replica matches the "one uvicorn worker per
  container" model (`apps/api/Dockerfile`).
- **Serverless containers.** Fargate removes EC2/node lifecycle management — the right
  operational weight for a single low-traffic internal canary.
- **Native security primitives.** IAM task roles (no static AWS keys), Secrets Manager +
  KMS, private subnets/security groups, VPC isolation for RDS/ElastiCache, and CloudTrail
  audit — satisfying the isolation, secret-handling, and audit requirements without custom
  tooling.
- **Enterprise ceiling.** The same primitives extend to customer-production HA later
  (multi-AZ, autoscaling, WAF) without re-platforming.

## 7. Consequences

**Benefits:** production-like verification of tenant isolation, auth, audit, observability,
migrations, rollback, and the Phase 4B-C feedback canary; least-privilege IAM; managed
backups; a clean path to customer-production.

**Tradeoffs:** AWS-specific IAM/networking/service surface introduces provider-specific
operational knowledge; Fargate per-task pricing and the ALB/NAT fixed costs are the main
budget drivers (see the cost contract, §M of the runtime contract).

**Vendor-lock-in considerations:** the application core stays cloud-neutral behind
`APP_MODE` adapters (`docs/architecture.md`) — Postgres, Redis, S3-compatible storage, and
OCI images are portable. Lock-in is concentrated in IaC, IAM, and CloudWatch/CloudTrail
wiring, not in application code; §4.1 (GCP) documents the nearest exit.

**Security implications:** no public database/Redis exposure; secrets only in Secrets
Manager (never in images or the repo); short-lived federated GitHub→AWS auth (OIDC) for
deployment; separated deployment/runtime/operator/observer roles. These controls are
**mandatory** and must not be reduced for cost.

**Operational implications:** deployment, observability, backups, and rollback are built
across INFRA-2…INFRA-8 (`docs/phase-4b-c-infra-plan.md`), each separately authorized.

**Cost implications:** the complete minimum-safe design is expected to sit **below the
$200/month ceiling with contingency** on current dated planning estimates
(`aws-staging-runtime-contract.md` §M). A mandatory pre-provisioning cost recalculation
gate precedes any spend; if the minimum safe design is ever projected above $200 without
weakening a required control, provisioning stops and re-authorization is required.

**Staging-vs-production implications:** SIGNALNEST_STAGING may use single-AZ / reduced
replicas for internal testing (documented in the runtime contract §N); customer-production
must later add HA, redundancy, scaling, recovery objectives, and customer-data controls.

## 8. Why the local docker-compose environment is not a canary runtime

Restated for emphasis (§4.4): throwaway credentials, no managed secrets/backups/isolation,
no deploy/rollback path, and an explicit "not a production manifest" declaration make it
unusable for verifying isolation, audit, observability, or rollback. It remains valid only
for local development.

## 9. What this ADR does NOT authorize

- No AWS authentication, account, organization, resource, IAM role/policy, secret, KMS key,
  or AWS Budget.
- No deployment of any SignalNest image (including exact SHA `3aadb8a`).
- No IaC (Terraform/CloudFormation/CDK/Pulumi) and no GitHub environment/secret/workflow.
- No capability override, no feedback submission, no global-flag change. The three flags
  (`opportunity_feedback_enabled`, `scout_scheduling_enabled`, `connector_rss_enabled`)
  remain `False`; opportunity feedback stays dark and fail-closed.

## 10. Conditions that would justify a future architectural migration

- A hard requirement to standardize on a different cloud (e.g., organization-wide GCP
  mandate) → revisit §4.1.
- Multi-team, multi-service scale and operational maturity that make a managed orchestrator
  cost-effective → revisit §4.3.
- Fargate/ALB/NAT fixed costs materially exceeding the value delivered at sustained scale →
  re-evaluate compute shape (e.g., ECS-on-EC2) under a fresh ADR.

Any such migration requires a new ADR; this record is not amended silently.
