# SIGNALNEST_STAGING — AWS runtime contract (authoritative, planning only)

- **Status:** Accepted for planning; **no AWS resource exists**. This document defines the
  intended logical topology and controls. It authorizes no provisioning, no authentication,
  no deployment, and no spending.
- **Decision record:** `docs/architecture/adr-0001-aws-ecs-fargate-staging.md`
- **Implementation roadmap:** `docs/phase-4b-c-infra-plan.md`
- **Baseline SHA:** `3aadb8a1da0f26ffd183a4b05161747038d5957c`
- **Region:** us-east-1 · **Compute:** Amazon ECS on AWS Fargate · **Budget ceiling:**
  USD $200/month (hard)

> All identifiers below are **logical names only**. This document contains no AWS account
> id, VPC/subnet/security-group id, ARN, domain, tenant id, credential, or secret value.

---

## A. Environment identity

- **Logical alias:** `SIGNALNEST_STAGING`.
- **Region:** us-east-1.
- **Ownership:** SignalNest-controlled, **internal-only**. No customer tenant, no customer
  data, no real advertising/publishing/messaging/billing/customer integration.
- **Environment label:** the application runs with `ENVIRONMENT=staging`, `APP_MODE=full`
  — the production-shaped validation path (`app/core/config.py`). Staging must **not** be
  downgraded to a non-production-like label merely to ease startup.
- **Required resource-tagging categories** (logical): `Project=SignalNest`,
  `Environment=staging`, `Alias=SIGNALNEST_STAGING`, `Owner=<internal-team-logical>`,
  `CostCenter=<logical>`, `Phase=4B-C`, `DataClass=internal-no-customer`,
  `ManagedBy=iac`. (Applied by INFRA-3 IaC; none created now.)
- **Intended owner:** the internal platform/operations owner named at INFRA-3/INFRA-8
  authorization time (not committed here).
- **Retention & cleanup:** SIGNALNEST_STAGING is a temporary internal environment; a
  teardown/cleanup decision is required at INFRA-8 and after the Phase 4B-C canary window.
- **Separation from production:** a distinct account/OU boundary (or at minimum a distinct
  VPC, database, cache, buckets, secrets, and IAM roles) from any future customer-production
  environment. No secret, bucket, database, or role is shared with production.

## B. Source and artifact integrity

- The **initial** staging deployment must build and deploy **exact source SHA
  `3aadb8a1da0f26ffd183a4b05161747038d5957c`**.
- An image that merely *contains* an earlier commit (e.g., `4d253f7`, the 4B-A merge) is
  **not** acceptable as the initial staging artifact; the deployed tree must be `3aadb8a`.
- Container images are **immutable** (content-addressed by digest; never overwrite a tag).
- Every deployment records the **Git SHA** and the **image digest**.
- The runtime must **verifiably report the deployed Git SHA** (via the application's
  `build_revision` / `application_version` configuration surface — `app/core/config.py`;
  wiring is INFRA-2, not done here).
- Build provenance is preserved (build metadata retained with the image/digest).
- Deployment must **never** originate from an uncommitted worktree.
- **Protected deployment approval** is required (GitHub environment protection + human
  approval — INFRA-4).
- **Rollback** uses a previously approved **immutable** artifact (a prior digest), never a
  mutable rebuild.

## C. Service topology

| Component | AWS target | Classification |
| --------- | ---------- | -------------- |
| Web SPA | **S3 + CloudFront** (static; `apps/web/dist`, `VITE_API_BASE_URL` at build) | **Mandatory before canary** |
| API service | **ECS/Fargate** task (`uvicorn app.main:app`, port 8000) behind an ALB | **Mandatory before canary** |
| Background worker | **ECS/Fargate** service (`python -m app.jobs.worker`, no port) | **Mandatory before canary** |
| Migration task | **ECS run-task** (one-shot `python -m app.db.migrate`) | **Mandatory before canary** |
| PostgreSQL + pgvector | **RDS for PostgreSQL** (managed) | **Mandatory before canary** |
| Redis / Valkey | **ElastiCache** (managed) | **Mandatory before canary** if the staging profile selects a Redis-backed cache/queue coordination path (see §H) |
| Object storage | **S3** bucket(s) | **Mandatory before canary** |
| Container registry | **ECR** | **Mandatory before canary** |
| Secret manager | **Secrets Manager + KMS** | **Mandatory before canary** |
| Logging | **CloudWatch Logs** | **Mandatory before canary** |
| Metrics + alarms | **CloudWatch Metrics/Alarms** | **Mandatory before canary** |
| Control-plane audit | **CloudTrail** (management events) | **Mandatory before canary** |
| Application audit events | persisted by the app (audit tables) + surfaced to CloudWatch | **Mandatory before canary** |
| Secure evidence destination | restricted S3 (or equivalent), separate from app buckets | **Mandatory before canary** |
| Scheduler / periodic process | none active (`scout_scheduling_enabled=False`) | **Deployable but kept dark** |
| RSS / connector process | none active (`connector_rss_enabled=False`) | **Deployable but kept dark** |
| Error-monitoring (external APM) | CloudWatch first; external APM optional | **Deferred until customer production / future** |
| Multi-AZ HA, autoscaling, WAF/edge | — | **Deferred until customer production** (see §N) |

**Dark-but-deployed:** scheduler and RSS code ship in the same image but remain inert while
their global flags are `False`; they are not wired to any live trigger in staging.

## D. Network contract (logical only)

- **VPC:** one dedicated staging VPC.
- **Public ingress:** only the **ALB** (API) and **CloudFront** (SPA) are public, TLS-only.
- **Private subnets:** API, worker, migration task, RDS, and ElastiCache run in **private**
  subnets with **no public IP**.
- **Security groups:** least-privilege — ALB→API on 8000 only; API/worker→RDS on the DB
  port only; API/worker→ElastiCache on the cache port only; no lateral wildcards.
- **Database isolation:** RDS is **not publicly accessible**; reachable only from the API/
  worker/migration security groups.
- **Redis/Valkey isolation:** ElastiCache is **not publicly accessible**; same private-only
  rule.
- **TLS termination:** at the ALB (API) and CloudFront (SPA), using **ACM** certificates.
- **Domain & certificate:** logical staging hostnames via **Route 53** + ACM (no domain or
  certificate created now; logical names only).
- **Outbound egress:** required for the LLM provider (openai/anthropic) and image pulls.
  Provided via a **NAT Gateway** (or a documented lower-cost secure alternative — §M),
  plus **VPC endpoints** (S3 gateway endpoint; interface endpoints for ECR, Secrets Manager,
  CloudWatch Logs) to keep AWS-service traffic off the NAT path.
- **Access surfaces:** administrative and **operator** access to `/internal/system/*`
  reaches the ALB over TLS and is additionally restricted (IP allow-list / private access);
  **observer** access is read-only to logs/audit/effective-state views; **health-check**
  access is the ALB target-group probe against `/readiness` (liveness `/health`).
- No raw VPC/subnet/security-group/account ids or domains are recorded here.

## E. Identity and access (logical least-privilege roles; none created now)

| Logical role | Purpose | Key constraints |
| ------------ | ------- | --------------- |
| Deployment (CI) | build/push image, run migration task, update ECS service | **GitHub OIDC**, short-lived; no long-lived AWS keys; scoped to staging resources |
| ECS task execution | pull from ECR, read secrets, write logs | execution role, minimum permissions |
| API task | app runtime (DB, cache, S3, secrets read) | scoped task role; read-only on secrets it needs |
| Worker task | job processing (DB, cache, S3, secrets read) | scoped task role |
| Migration task | run one-shot migrations | DB access only; no service-update rights |
| Internal canary operator | operator control plane (`/internal/system/...`) | application operator role; **not** an AWS admin |
| Independent observer | read-only logs/audit/effective-state | no mutation rights, no override rights |
| Emergency override-clear operator | execute the DELETE clear plane | may equal the operator or be a distinct break-glass identity |

Requirements: short-lived/federated GitHub→AWS auth for deployment (OIDC, no long-lived
keys); no secrets baked into images; no credentials committed to the repo; minimum
necessary permissions; separation between deployment, runtime, operator, and observer
roles; MFA and existing identity protections; **no customer identity use**. **This tranche
creates no role or policy.**

## F. Secrets and runtime configuration (logical categories only)

- **Store:** AWS Secrets Manager, encrypted with the approved **KMS** key; **staging-only**
  secrets, never reused from production.
- **Injection:** injected at runtime into ECS tasks; **never** embedded in an image or the
  repository.
- **Rotation/revocation:** documented rotation and revocation procedures (INFRA-5).
- **Redaction:** secret values are redacted from logs and evidence (the app already redacts
  sensitive keys — `docs/operations/observability.md`).
- **Logical secret categories** (exact env names derived from `app/core/config.py`; not
  invented): application `SECRET_KEY`; PostgreSQL connection URL (`DATABASE_URL`); Redis URL
  (`REDIS_URL`); S3 access configuration (prefer IAM task role over static
  `S3_ACCESS_KEY_ID`/`S3_SECRET_ACCESS_KEY`); LLM provider key (`LLM_API_KEY`) with provider
  (`LLM_PROVIDER=openai|anthropic`).
- **Validation path preserved:** SIGNALNEST_STAGING uses the real production-like validation
  (`ENVIRONMENT=staging`, `APP_MODE=full`) which **rejects** SQLite, in-process queue,
  in-memory cache, local storage, a weak `SECRET_KEY`, and a `mock` LLM provider
  (`app/core/config.py`). Do **not** weaken validation to make staging start.
- **LLM credential note:** staging requires a **real** LLM key even though the feedback gate
  makes no model call. Use a **staging-specific** credential with the **smallest practical
  spending cap**, no customer data, and bounded usage; store it only in Secrets Manager;
  treat its variable charges separately from AWS infra cost (§M). **No credential is created
  or displayed in this tranche.**

## G. Database and migrations

- **Managed PostgreSQL** (RDS), **private access only**, **encryption at rest and in
  transit**, a **dedicated staging database**, **no customer data**.
- **pgvector** extension enabled (production-shaped mode requires a persistent vector
  backend — `config.py`).
- **Migrations** run as a **controlled one-time ECS task** using the **approved image**
  (`python -m app.db.migrate`); replicas never migrate (`docs/operations/deployment.md`,
  `docs/operations/migrations.md`).
- **Single Alembic head** verified before/after: `98289430a3ec`.
- A **migration backup / recovery checkpoint** (snapshot) is taken before applying
  migrations.
- **Forward and rollback** procedures follow the additive-first policy
  (`docs/operations/migrations.md`); a downgrade is a deliberate single-actor action.
- **No schema mutation via an application startup race** — startup runs a read-only
  schema-compatibility gate and fails fast if behind (`app/main.py` lifespan).
- **No direct SQL provisioning** of tenants, users, roles, or overrides (see §I, §J).

## H. Redis/Valkey and worker processing

- **Is Redis mandatory?** The durable job store is currently the local SQLite-backed store
  in code (`job_queue_backend=local`), but production-shaped validation rejects in-memory
  cache and in-process queue (`config.py`), so a staging profile that sets
  `CACHE_BACKEND=redis`/`QUEUE_BACKEND=redis` **requires** a reachable Redis/Valkey
  (ElastiCache). SIGNALNEST_STAGING therefore treats **ElastiCache as mandatory** for a
  faithful production-like profile. If a future documented profile runs without Redis, that
  must be justified against the validator, not by weakening it.
- **Private access only**; encryption/auth per the selected ElastiCache engine.
- **Worker** starts, registers in the worker registry, and heartbeats; readiness treats
  worker presence as informational unless `require_worker_fleet=true` (`probes.py`).
- **Queue isolation, retry, failure visibility:** durable jobs use at-least-once delivery
  with bounded retries, dead-lettering, and lease recovery (`docs/architecture.md`,
  `docs/phase-3a-durable-jobs.md`); failed work is visible via job metrics/events
  (`docs/operations/observability.md`).
- **Scaling assumptions:** single API replica and single worker replica are sufficient for
  one low-traffic canary workspace (documented staging reduction — §N).
- **Scheduling and RSS stay dark** while their global flags remain `False`.

## I. Authentication and tenant isolation

- **Auth provider:** the application's existing session auth — `POST /auth/register`,
  `POST /auth/login` return a bearer `access_token` (`app/auth/routes.py`,
  `app/auth/schemas.py`). Callback/origin/CORS configuration (`CORS_ORIGINS`) must match the
  staging SPA origin.
- **Internal tenants (SignalNest-controlled, no customer data):**
  - **Two internal organizations.**
  - **Three internal workspaces:** `TARGET_CANARY`, `SAME_ORG_SIBLING`, `CROSS_ORG_CONTROL`.
  - **Three independent authenticated sessions** — no shared cookies or tokens; each session
    holds its own bearer token.
- **Tenant identity** is derived **server-side** from the authenticated context
  (`organization_id`/`workspace_id` scoping in the repository layer — `docs/architecture.md`);
  **no client-invented workspace header** is trusted.
- **Provisioning surfaces (supported application APIs only, never SQL):**
  `POST /auth/register` (user/org bootstrap), `POST /organizations/{organization_id}/workspaces`
  (workspace creation), and RBAC role assignment via supported admin surfaces. INFRA-7
  verifies these end-to-end.
- **No customer identity, no direct SQL provisioning.** If a required
  organization/workspace/role provisioning surface proves missing or insufficient, it is
  flagged as an **application gap** for a separately reviewed tranche — not worked around
  with SQL.

## J. Opportunity-feedback safety state

The contract **requires**, throughout INFRA implementation and environment setup:

- `opportunity_feedback_enabled=False`, `scout_scheduling_enabled=False`,
  `connector_rss_enabled=False` (all three global flags remain `False`).
- **No capability override** created or cleared during infrastructure implementation.
- Opportunity feedback remains **dark and fail-closed** (the gate opens only on an explicit
  `effective_enabled is True` — `app/feedback/routes.py`).
- Scheduling and RSS remain **outside** the opportunity-feedback override resolver (import
  boundaries enforced by tests).
- `/system/capabilities` behavior is **unchanged** unless a future separately reviewed
  tranche explicitly requires it (backend-first canary divergence acknowledged in
  `docs/phase-4b-b-plan.md`).
- **No Phase 4B-C activation** during INFRA-1 through environment setup.
- Override creation requires the later **exact human authorization gate** (the operator
  `PUT /internal/system/capabilities/overrides` set plane, executed only under Phase 4B-C
  authorization).
- **Clear/DELETE readiness must exist before override activation** (the
  `DELETE /internal/system/capabilities/overrides` clear plane is the primary rollback —
  `docs/phase-4b-b-plan.md` D3).

## K. Observability, audit, and evidence (mandatory before the canary)

Visibility required **before** Phase 4B-C (not merely before customer production):

- Deployment success/failure; **runtime deployment SHA**; API health; worker health;
  migration status.
- Authentication failures; authorization failures; tenant-resolution failures; application
  errors; queue/worker failures; database availability; Redis/Valkey availability;
  cost/usage.
- **Opportunity-feedback gate** decisions and failures:
  `opportunity_feedback_gate_decided`, `opportunity_feedback_gate_failed`
  (`app/feedback/routes.py`).
- **Override audit events:**
  `workspace_capability_override.created`, `workspace_capability_override.updated`,
  `workspace_capability_override.rejected`, `workspace_capability_override.cleared`
  (`app/capabilities/service.py`).

Definitions:
- **Central log destination:** CloudWatch Logs (structured JSON to stdout; the app writes no
  log files — `docs/operations/deployment.md`, `observability.md`).
- **Metric/alarm categories:** health, error rate, worker/job failures, dependency
  availability, cost.
- **Error-monitoring destination:** CloudWatch first; external APM deferred.
- **Audit retention:** a defined retention window for application audit events and CloudTrail
  management events (set at INFRA-6, sized within budget — §M).
- **Evidence redaction:** the app's existing secret redaction applies
  (`app/core/redaction.py`); evidence records use placeholders and correlation ids, never
  raw tenant ids, tokens, or secrets (`docs/verification/4b-b-feedback-canary.md`).
- **Secure external evidence destination:** a restricted store separate from app buckets;
  runtime identifiers, if captured, live only in the restricted executed copy — never in the
  repo.
- **Correlation-reference handling:** opaque request/job correlation ids only
  (`observability.md`).
- **Operator responsibilities:** execute governed override planes, read effective state,
  observe gate decisions/audit. **Observer responsibilities:** independent read-only
  confirmation of isolation and audit. **Incident escalation:** per §L.
- **No observability resource is created in this tranche.**

## L. Backups, restore, rollback, and incident response

- **Automated staging database backups** with a **defined retention** window.
- **Restore verification** performed before customer production.
- **Immutable deployment rollback** to a previously approved digest (§B).
- **Migration rollback / forward-recovery** decision per the additive-first policy
  (`docs/operations/migrations.md`).
- **Emergency capability-override clear** procedure = operator `DELETE` clear plane, with
  **independent observer confirmation** (`docs/phase-4b-b-plan.md` D3;
  `docs/operations/incident_response.md`).
- **Incident conditions** and **clear ownership** are defined in the roadmap/runbooks.
- **No database fallback** for clearing an override (never mutate the override row via SQL).
- **No global-flag change as a substitute** for a workspace-specific clear unless separately
  authorized as an explicit incident response (setting an already-`False` global flag to
  `False` is not a canary rollback — `docs/phase-4b-plan.md` D3).

## M. Cost contract

- **Hard ceiling:** USD **$200/month** — a guardrail, not a target.
- **Pricing basis:** dated **planning estimate**, us-east-1, **2026-07-21**, standard
  on-demand pricing (no promotional credits). Amounts are planning estimates pending the
  **mandatory pre-provisioning pricing recalculation gate** (INFRA-8) using current official
  AWS pricing at provisioning time.
- **Runtime-size assumptions:** API and worker each ~0.25 vCPU / 0.5 GB Fargate; single
  replica each; RDS `db.t4g.micro` single-AZ ~20 GB gp3; ElastiCache `cache.t4g.micro`
  single node; low traffic; ~14–30 day log retention; ~7-day DB backup retention.

| Line item | Low | Planning | Conservative | Notes |
| --------- | --- | -------- | ------------ | ----- |
| Fargate API + worker (2 small tasks) | $14 | $18 | $24 | scale-to-zero off-hours reduces low end; worker may use Fargate Spot |
| Application Load Balancer | $16 | $18 | $22 | fixed base + LCU; **major fixed driver** |
| RDS PostgreSQL (single-AZ, micro) | $12 | $16 | $22 | incl. ~20 GB gp3 storage |
| ElastiCache (single micro node) | $9 | $12 | $15 | mandatory for production-like profile (§H) |
| NAT Gateway + data processing | $32 | $33 | $40 | **largest fixed driver**; LLM egress requires internet |
| S3 + CloudFront (SPA + storage) | $2 | $4 | $8 | low traffic |
| ECR | $1 | $1 | $2 | image storage |
| Secrets Manager + KMS | $3 | $4 | $6 | ~6 secrets + 1–2 keys |
| CloudWatch logs/metrics/alarms | $3 | $6 | $12 | retention-driven |
| CloudTrail (management events) | $0 | $1 | $2 | first management-event copy free |
| Route 53 + ACM | $1 | $1 | $2 | ACM public certs free; hosted zone ~$0.50 |
| Backups + evidence storage | $1 | $2 | $4 | snapshot + restricted store |
| Data transfer | $1 | $3 | $6 | low volume |
| **AWS infra subtotal** | **~$95** | **~$119** | **~$165** | below $200 with contingency |

- **Largest cost drivers:** NAT Gateway (~$33), ALB (~$18), RDS (~$16), Fargate (~$18),
  ElastiCache (~$12).
- **Contingency margin:** the conservative estimate (~$165) leaves ~$35 headroom under the
  $200 ceiling; the planning estimate (~$119) targets a normal range well below the ceiling.
- **Variable third-party charges (separate from AWS infra):** LLM/API usage
  (openai/anthropic). The feedback canary makes no model call, so expected usage is near
  zero; enforce the smallest practical provider-side spending cap. Tracked and reported
  **separately**; not counted in the AWS infra subtotal.
- **Costs excluded** (variable/unknown): sustained high traffic, external APM, WAF, and any
  customer-production HA additions.
- **Safe scale-down levers** (without weakening security): schedule API/worker to zero
  desired-count off testing hours; short log retention; Fargate Spot for the worker; a
  documented single small NAT-instance alternative to the managed NAT Gateway; delete unused
  ECR images. **Resources that must stay up during the canary window:** RDS (state),
  ElastiCache (coordination), Secrets Manager, and the ALB while the API is under test.
- **Planned AWS Budget alert thresholds** (created later, INFRA-3/INFRA-8, **not now**):
  **50% / 75% / 90% / 100%** of the $200 ceiling, plus cost-anomaly monitoring.
- **Controls:** log-retention limits, image-retention (ECR lifecycle) limits,
  backup-retention limits, resource tagging (§A), and a documented staging shutdown/cleanup
  policy.
- **Mandatory pre-provisioning recalculation:** before any spend (INFRA-8), recompute with
  current official pricing. **If the minimum safe design is then projected above $200
  without weakening a required control, STOP and re-authorize** — do not remove TLS, auth,
  secret management, tenant isolation, audit, observability, backups, or rollback, and do not
  substitute docker-compose for staging.

## N. Staging-versus-production matrix

| Dimension | SIGNALNEST_STAGING (internal canary) | Future customer-production |
| --------- | ------------------------------------ | -------------------------- |
| Availability zones | Single-AZ acceptable (documented) | Multi-AZ required |
| Service replicas | 1 API / 1 worker | ≥2 each, load-balanced |
| Database availability | Single-AZ RDS | Multi-AZ RDS + read scaling as needed |
| Redis/Valkey availability | Single node | Replication/failover |
| Backups | Automated, short retention | Longer retention + tested restore + PITR |
| Scaling | Fixed small | Autoscaling |
| Traffic capacity | Low (internal testers) | Customer-scale |
| WAF / edge protection | Optional/minimal | Required |
| Monitoring | Mandatory (CloudWatch) | Mandatory + APM/alerting maturity |
| Incident coverage | Internal, business-hours | On-call/production SLAs |
| Data classification | Internal, **no customer data** | Customer data + compliance controls |
| Customer integrations | None | As productized |
| Recovery objectives | Best-effort, documented | Defined RTO/RPO |
| Deployment approvals | Protected + human approval | Protected + change management |
| Cost | ≤ $200/month hard ceiling | Sized to production |

**Security controls that must be IDENTICAL to production (never reduced for staging):**
TLS everywhere, authentication, secret management (Secrets Manager + KMS), tenant isolation,
no public DB/Redis exposure, least-privilege IAM, audit (CloudTrail + app audit events),
immutable artifacts, and protected deployment approval.

**Acceptable staging reductions (documented, internal-only):** single-AZ, single replicas,
single-node cache, shorter backup/log retention, minimal/absent WAF, and business-hours
incident coverage. A single-AZ staging environment must **never** be described as fully
highly-available production.
