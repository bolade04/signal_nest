# SIGNALNEST_STAGING — OpenTofu IaC (INFRA-4, fully composed; ~~repository-only, not provisioned~~ *(struck 2026-10-09: accurate when written on 2026-07-24)* — the staging foundation was applied from 2026-07-27, see the live-state notice)

> **Live-state notice (2026-10-03, `P6-GOV-1` — PARTIAL reconciliation; corrected 2026-10-09).** This README was first written before anything was applied, and several of its statements were carried forward unchanged after the staging foundation was applied (resources were created from 2026-07-27, per a read-only observation of 2026-09-27). `operator-closure-contract.json` in this directory records the complete full-graph refresh of 2026-07-28T22:01:44Z–22:01:48Z (267 CloudTrail events) and an audit trail delivering since 2026-07-27. Three summary passages below (§1, §8 bootstrap, §10) were reconciled on 2026-10-03; on 2026-10-09 every statement of that kind identified by the GOV-1 assessment — that a resource does not exist in AWS, or that a live step never ran — was struck or corrected in place with a dated note. *(The 2026-10-03 wording cited "§9 bootstrap" for the §8 passage and called those statements historical text superseded by the contract; two of them were already inaccurate when last edited, and the reader-role statement is contradicted by a dated read-only observation, not by the contract.)* This file does **not** record current AWS state: each corrected live fact below carries the date and scope of the observation it rests on; the contract records the 2026-07-28 reference refresh and the operator permission closures, not a resource inventory; what exists now is established only by dated, operator-held sealed evidence. Later repository changes — including PRs #188, #194, #195, #197 and #198 — were repository-only as last recorded on 2026-10-03 (`docs/project-phase-6-plan.md` §4.6a); no later observation is recorded here.

## 1. Purpose and scope

This directory is the Infrastructure-as-Code (the repository source; see the live-state notice for what has been applied) for the internal,
non-customer **SIGNALNEST_STAGING** (dark canary) environment. It is **staging-only**.
**All twelve** modules — **`network`**, **`edge`**, **`alb`**, **`secrets`**, **`registry`**, **`storage`**, **`data_sql`**, **`data_cache`**, **`iam`**, **`ecs`**, **`observability`**, and **`cost`** —
contain executable, **offline-validated** HCL resource bodies, and **all twelve
are now wired into the root composition** (`main.tf`) per the locked acyclic
§26.12 graph. A **thirteenth** module, **`revision_reader`**, was added by Gate 4J.
It is deliberately **outside** the §6 twelve and outside the §26.12 graph: it is a
verification *instrument* for the schema state the workload apply depends on, gated by
its own **two** lifecycle flags — `enable_revision_reader_publication_bootstrap` (Stage A:
the reader ECR repository, its lifecycle policy, and the publisher OIDC role) and
`enable_revision_reader_runtime` (Stage B: the log group, security group, reader→RDS
ingress, execution/runner roles, and the task definition) — rather than by `deploy_workload`,
because gating the check on the flag it exists to gate would be circular. The split lets the
reader image be published (Stage A) **before** any runtime resource — in particular before
the execution role that holds the `DATABASE_URL` secret grant — exists; `runtime` requires
`publication_bootstrap = true` **and** a pinned image digest. Both flags default to creating
nothing. Composition is **configuration only**: "root-composed" is still
distinct from "provisioned"/"deployed". **Live state (reconciled 2026-10-03, `P6-GOV-1`):**
the staging foundation **has** been applied — `operator-closure-contract.json` in this
directory records the complete full-graph refresh of 2026-07-28T22:01:44Z–22:01:48Z
(267 CloudTrail events) and a trail delivering since 2026-07-27; the later windowed-staging,
API-alias and budget-bound changes (PRs #195, #197, #198) were repository-only — not applied — as last recorded on 2026-10-03 (`docs/project-phase-6-plan.md` §4.6a, which also lists #188 and #194); no later observation is recorded here. "Implemented" (HCL exists and offline-validates),
"root-composed" (referenced by `main.tf`), "provisioned", and "deployed" are distinct states
and are not equivalent. **The committed HCL describes intended resources; what exists in AWS at a given time is established only
by dated, operator-held sealed evidence records** (the closure contract records the 2026-07-28 reference refresh and the permission closures, not an inventory), never by the HCL
itself. Offline validation (§10) contacts no AWS API.

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
`staging/`, or `production/` directories and **no** second root module **for the
SIGNALNEST_STAGING environment composition**. Production infrastructure is out of
scope for INFRA-4. The `bootstrap/` directory is the explicit carve-out from that
rule: it is the one-time **state-backend** bootstrap root fixed by the plan's §7
(a root cannot remote-back itself into a bucket that does not exist yet), composes
**zero** environment resources, and is not an environment split — see
`bootstrap/README.md`.

```
infra/aws/
  README.md                 # this file
  versions.tf               # OpenTofu + AWS provider compatibility constraints
  providers.tf              # default AWS provider (region + default tags)
  backend.tf                # empty S3 backend declaration (no values)
  backend.hcl.example       # synthetic partial-backend-config template (real backend.hcl git-ignored)
  variables.tf              # typed root inputs (no secrets/ids)
  locals.tf                 # name prefix + the authoritative eight-tag set
  main.tf                   # composition root: composes the twelve §26.12 modules + revision_reader (no root resource/data)
  outputs.tf                # metadata + composed-module reference outputs (`rds_db_address` is marked sensitive)
  terraform.tfvars.example  # synthetic example inputs
  bootstrap/                # one-time remote-state backend root (§7; executed 2026-07-27 — see §8; never auto-applied)
  modules/                  # 13 modules: the twelve §26.12 modules + revision_reader (Gate 4J); all implemented (offline-validated); no doc-only stub remains
```

## 4. Compatibility constraints and dependency lock

The `versions.tf` constraints are bounded ranges; the committed
`.terraform.lock.hcl` records the exact selected provider version and checksums
(generated in the earlier tool-assisted tranche — no longer deferred).

- OpenTofu: `>= 1.12.3, < 1.13.0` (validated with 1.12.5)
- AWS provider source: `hashicorp/aws`
- AWS provider constraint: `>= 6.55.0, < 6.56.0`; **locked to `6.55.0`** in
  `.terraform.lock.hcl`.

## 5. Module inventory (the twelve §26.12 modules, plus `revision_reader`)

The twelve reusable modules are fixed by the authoritative design
(`aws-staging-iac-plan.md` §6). **All twelve** are implemented (HCL authored and
offline-validated in the repository; ~~**not** provisioned or deployed~~ *(struck 2026-10-09: accurate when written on 2026-07-24; stale since the foundation apply from 2026-07-27 — see the live-state notice; no active ECS service was observed on 2026-10-02 (read-only))*); **no**
documentation-only stub remains, and **all twelve are root-composed** (wired in
`main.tf` per the locked §26.12 graph; composition is configuration only).

`revision_reader` is listed last and is **not** one of the §6 twelve. It was added by
Gate 4J as a verification instrument, sits outside the §26.12 graph, and is gated by its
own two flags (both default `false`) rather than by `deploy_workload` — see its own README for why
that independence is load-bearing.

| Module | Status |
| --- | --- |
| `network` | **Implemented** (offline-validated) — VPC, subnets, route tables, single NAT (in the current configuration, the NAT gateway, its EIP and the private default route exist only while `enable_nat_gateway` and `staging_window_active` are both true) |
| `edge` | **Implemented** (offline-validated) — CloudFront + private S3 SPA origin + web DNS aliases |
| `alb` | **Implemented** (offline-validated, **root-composed**) — ALB SG + public HTTPS 443 ingress, internet-facing IPv4 ALB, API IP target group, HTTPS listener (in the current configuration, the load balancer, target group, listener and API alias exist only while `staging_window_active = true`; the security group and log bucket persist); **access + connection logging enabled** into a dedicated private module-owned log bucket (SSE-S3, versioned, public-access-blocked, TLS-only; delivery via the plan-time-resolved regional ELB service account — the §24.7 pre-live logging gate is resolved in configuration) |
| `ecs` | **Implemented** (offline-validated, **root-composed**) — ECS/Fargate compute plane per §26.2–§26.15: cluster, the three deterministic `/ecs/<name_prefix>-*` log groups, three per-workload task SGs + every task-side cross-SG rule (ALB↔API 8000, PostgreSQL 5432 incl. migration, Redis 6379 api/worker-only, TCP 443 NAT egress; no CIDR ingress), three digest-pinned LINUX/X86_64 task definitions (migration reuses the worker image with the bare `python -m app.db.migrate` upgrade-and-verify command; execution-role-only secret injection, migration injects `DATABASE_URL` only), two services (circuit breaker + rollback, ECS Exec disabled); migration is never a service; ~~nothing exists in AWS~~ *(struck 2026-10-09: already inaccurate when this line was last edited on 2026-07-28 — the phrase, first written 2026-07-24, was carried unchanged after one-shot tasks had run on the cluster that day (CloudTrail, read 2026-09-27); the cluster is recorded as tagged in `operator-closure-contract.json` (2026-08-10 text) and was observed ACTIVE with tags and an active-services count of 0 on 2026-10-02 (read-only observation); current state is not recorded here)* |
| `data_sql` | **Implemented** (offline-validated, **root-composed**) — one private RDS PostgreSQL instance + DB subnet group + rule-free RDS security group + TLS-enforcing parameter group (`rds.force_ssl=1`); `manage_master_user_password=true` (no password in HCL/state); private, encrypted (gp3); ~~no DB provisioned, no pgvector activated~~ *(struck 2026-10-09: "no DB provisioned" was accurate when written on 2026-07-24 and is stale since the instance was created on 2026-07-27 — observed available on 2026-09-27, 2026-09-30 and 2026-10-02, read-only observations; no committed migration activates pgvector, and its live state is not recorded; current state is not recorded here)* |
| `data_cache` | **Implemented** (offline-validated, **root-composed**) — one private ElastiCache for Redis replication group (encrypted at rest, TLS-required in transit, **no `auth_token`** — no Redis credential in HCL/state) + cache subnet group + rule-free Redis security group + empty custom parameter group; in the current configuration the replication group exists only while `staging_window_active = true`; ~~no cache provisioned~~ *(struck 2026-10-09: accurate when written on 2026-07-24; stale since the replication group was created on 2026-07-27 — the group was observed available on 2026-09-27 and 2026-10-02 and its member node on 2026-09-30, read-only observations; current state is not recorded here)* |
| `storage` | **Implemented** (offline-validated, **root-composed**) — one private S3 application bucket (SSE-S3/AES256, versioning, all four public-access-block controls, bucket-owner-enforced ownership, TLS-only deny policy); no `bucket_key_enabled`, no KMS; ~~no object stored~~ *(struck 2026-10-09: unverified — no recorded observation lists this bucket's objects; object state is UNKNOWN)* |
| `registry` | **Implemented** (offline-validated, **root-composed**) — two private ECR repositories (`api`, `worker`) + two lifecycle-policy instances; ~~no image built/pushed~~ *(struck 2026-10-09: accurate when written on 2026-07-24; stale since 2026-07-27 — CloudTrail, read on 2026-09-27, shows eight image pushes to the two repositories by the CI image-publisher role on 2026-07-27, 2026-07-28 and 2026-07-29, in sessions named like the publish workflow's; current state is not recorded here)* |
| `iam` | **Implemented** (offline-validated, **root-composed**) — the four §26.8 ECS-consumed roles: one shared task execution role (ECR pull scoped to the two repositories, deterministic `/ecs/<name_prefix>-*` log delivery with no `CreateLogGroup`, `GetSecretValue` on the four container ARNs, `kms:Decrypt` on the secrets CMK via Secrets Manager; sole `Resource:"*"` = `ecr:GetAuthorizationToken`) + API/worker task roles (application-bucket S3; the API role also holds the 6B-4C `ses:SendEmail` grant on one sending identity — repository configuration, live status in `docs/project-phase-6-plan.md` §4.6a) + an intentionally **empty** migration task role; **plus (INFRA-9) the CI image-publisher role** (GitHub OIDC → ECR push, created only when `github_oidc_provider_arn` is supplied — the account-wide OIDC provider is consumed, never created); ~~no role exists in AWS~~ *(struck 2026-10-09: accurate when written on 2026-07-24; stale since the roles were created on 2026-07-27 — read as already existing by the 2026-07-28 reference refresh (`operator-closure-contract.json` (2026-08-10 text)) and listed by a read-only observation on 2026-09-27; current state is not recorded here)* |
| `secrets` | **Implemented** (offline-validated, **root-composed**) — four empty Secrets Manager containers + one customer-managed KMS key/alias; ~~no secret value populated~~ *(struck 2026-10-09: accurate when written on 2026-07-24; stale — secret metadata read on 2026-09-27 showed one current version for each of the four application secrets, last changed 2026-07-28; values were not read; values are populated out of band (see the `secrets` paragraph below); current state is not recorded here)* |
| `observability` | **Implemented** (offline-validated, **root-composed**) — metric filters + alarms + audit trail per §6/§14/§26.9: 3 error metric filters over the ecs-owned log groups (evidence-backed `{ $.severity = "ERROR" }` pattern) **plus 3 INFRA-7 capability filters** (gate-failed, override-driven enable, override set/clear — API log group, no tenant identifier in any pattern), **15 alarms** (12 caller-thresholded ECS/log/RDS/Redis + 3 INFRA-7 fixed-threshold(1) capability signals), **one INFRA-7 canary observability dashboard** (a disclosed supersede of the module's earlier no-dashboard exclusion), and a single-region management-events CloudTrail with log-file validation delivered to a dedicated private TLS-only audit bucket; creates NO log group (ecs owns them) and no SNS/budget resource; ~~nothing exists in AWS~~ *(struck 2026-10-09: accurate when written on 2026-07-24; stale since 2026-07-27 — the audit trail has logged since 2026-07-27 per `operator-closure-contract.json` (2026-08-10 text) and was observed logging in a read-only observation on 2026-10-03; that the metric filters and alarms existed at the 2026-07-28 reference refresh is an inference from the refresh's observed describe actions; current state is not recorded here)* |
| `cost` | **Implemented** (offline-validated, **root-composed**) — one monthly AWS Budget (`COST`/USD) with ACTUAL-spend notifications at the fixed 50/75/90/100% thresholds to a caller-supplied email (§15); `monthly_budget_limit` statically validated ≤ the $200 ADR-§M hard ceiling; **observational only** — no budget action, remediation, SNS topic, or IAM resource; independent (no sibling dependency); ~~no budget exists in AWS~~ *(struck 2026-10-09: CloudTrail event history read on 2026-09-30, which reaches back 90 days (to about 2026-07-02), records no budget creation from then until after this line was written on 2026-07-24; stale since 2026-07-25, when a separate console-created monthly budget with a 20.00 USD limit was created, and since 2026-07-27 for this module's budget (`<name_prefix>-monthly`, monthly, limit 150 USD), created by an apply under the Terraform provider user agent; no update or delete of either through 2026-09-30; an operator billing-console attestation on 2026-10-01 (not an API read) recorded it at 150, `docs/project-phase-6-plan.md` §4.6a; current state is not recorded here)* |
| `revision_reader` | **Implemented** (offline-validated, **root-composed**, **creates nothing by default**) — Gate 4J/4M dedicated live-revision reader, outside the §6 twelve and the §26.12 graph, on a **two-stage lifecycle**. **Stage A — publication bootstrap** (`enable_revision_reader_publication_bootstrap`): its own private ECR repository (immutable tags, `force_delete = false` so a pinned image cannot be destroyed out from under a task definition), that repository's lifecycle policy, and the GitHub-OIDC **publisher** role. **Stage B — runtime** (`enable_revision_reader_runtime`): its own `/ecs/<name_prefix>-revision-reader` log group, an **egress-only** task SG (5432 to the RDS SG, 443 for pull/secrets/logs; **no ingress on the reader**, no Redis), the **reader→RDS `5432` ingress rule on the RDS SG** (owned here, runtime-gated — the reader is not in the `ecs` module's workload `for_each`, and without this rule the stateful default-deny RDS SG would refuse the connection), the **execution** role (which alone holds the `DATABASE_URL` secret grant), the GitHub-OIDC **runner** role, and one task definition that exists **only** once an image digest is pinned. Stage B requires Stage A **and** a pinned digest (cross-variable `validation`), so the image is published before the secret-bearing execution role exists. **No task role at all**, `DATABASE_URL` as the only injected secret, and neither `entryPoint` nor `command` set — the image's fixed exec-form ENTRYPOINT is the control, and `ContainerOverride` has no `entryPoint` member to shadow it. **Teardown order:** disable `runtime` before `publication_bootstrap` (the task definition and execution role reference the bootstrap ECR repository). The publisher/runner roles require a GitHub OIDC provider ARN; **no GitHub environment is created automatically** — the protected `staging-reader-publish`/`staging-reader-run` environments are provisioned out of band under separate authorization. Both flags **never** gated by `deploy_workload`. Both default `false`; ~~nothing has been applied and nothing exists in AWS today~~ *(struck 2026-10-09: accurate when written on 2026-07-29; stale since 2026-08-15, when the role-bootstrap executor created the three reader IAM roles out of band for adoption by this module (`trust_binding.tftest.hcl`; a read-only observation on 2026-09-27 listed them with that creation date); the Stage-A adoption into OpenTofu was refused that day and no reader repository was observed on 2026-09-27, so no reader resource is recorded as applied by OpenTofu; current state is not recorded here)* |

The root composition (`main.tf`) wires **all twelve** §26.12 modules, plus
`revision_reader`. The `alb` module owns
the ALB security group and the public HTTPS ingress and exposes seven outputs
(`alb_arn`, `alb_dns_name`, `alb_canonical_hosted_zone_id`, `https_listener_arn`,
`api_target_group_arn`, `alb_security_group_id`, `api_alias_record_name`); it also owns the dedicated private
ALB log-delivery bucket, with **access and connection logging enabled** into it
(distinct `alb-access`/`alb-connection` prefixes — the §24.7 pre-live logging gate,
resolved in configuration). The `ecs` module owns the API task security group and
**both** ALB↔API TCP 8000 cross-security-group rules and consumes the ALB outputs;
the ALB never consumes an ECS/API security-group id, so the dependency is one-way and
acyclic: `ecs -> alb`. The regional ACM certificate is supplied through the required
`api_certificate_arn` input (no real ARN is committed); tags are applied by the
provider `default_tags` (no module-level `tags` input). The ALB plane still has
**no** HTTP/port-80 listener, no public port 8000, no IPv6 ingress, no unrestricted
ALB egress, no certificate creation, and no WAF (deferred by locked decision). Since
2026-10-01 the `alb` module also owns the **windowed API Route 53 alias record**
(P6-INF-3; §24.7 deferral lifted for a windowed record only): one `A` alias
`api_fqdn` → the ALB inside the consumed hosted zone, sharing the ALB's window gate so
it exists only while `staging_window_active = true` and re-points to each window's
re-created ALB automatically; `api_fqdn` is a required root input rejected when it
equals `web_fqdn`. Code and mocked tests only — ~~no record exists in AWS~~ *(dated 2026-10-09: no record existed as last recorded on 2026-10-03, `docs/project-phase-6-plan.md` §4.6a; current state is not recorded here)*.

The `secrets`, `registry`, `storage`, `data_sql`, `data_cache`, `iam`, `ecs`, `observability`, and `cost` modules are **now wired
into `main.tf`** by the root-composition tranche, exactly along the locked §26.12
edges (no interface was changed to compose them). Required caller-supplied
values (bucket name, DB/Redis engine versions, DB name/master username, image
digests, LLM provider, alarm thresholds, budget limit/email) are root variables
supplied via a git-ignored `*.tfvars` — none is committed, and the image digests
come only from the INFRA-5-authored publish workflow *(corrected 2026-10-09: the earlier text said they
"cannot exist until" it is executed under INFRA-9 — images were pushed from 2026-07-27, see §12 item 1)*.
The composed root carries a **`deploy_workload`** switch (INFRA-9 execution-path
tranche; default `false` = foundation stage). In the foundation stage the root
creates everything **except** the three ECS task definitions (API, worker, migration) and the two services (window-, flag- and OIDC-gated resources follow their own inputs) —
including the two ECR repositories — so it can plan/apply **without** any image
digest existing, resolving the digest/ECR bootstrap circularity with **no
targeted apply and no manual ECR creation**. The image-digest inputs are
therefore nullable (still fail-closed: a non-null value must be an immutable
`sha256:<64 hex>` digest, and `deploy_workload = true` requires both digests
non-null). The workload stage (`deploy_workload = true` with real digests
supplied after an INFRA-5 publish run) creates the task definitions and services.
The `secrets` module creates only the declarative secret *containers* — four empty AWS
Secrets Manager secrets and one customer-managed KMS key/alias — the module itself populates **no** secret
value (values are populated out-of-band under a separately authorized
operational step — *(corrected 2026-10-09: the earlier text said no secret value "has been populated"; secret metadata read on 2026-09-27 showed one current version per application secret, last changed 2026-07-28; values were not read)*); it produces outputs for the future `iam` and `ecs` modules
(`secrets -> iam`, `secrets -> ecs`). The `registry` module declares **two** private ECR
repositories, `api` and `worker`, and **two** lifecycle-policy instances (immutable tags,
scan-on-push, AES-256, force-delete disabled, untagged-only expiry preserving tagged
images). API and worker are separate future image artifacts with distinct immutable
digests; the migration task **reuses the worker image** with a command override. The module builds **no** ECR
image *(corrected 2026-10-09: the earlier text said no image "has been built, tagged, scanned remotely, pushed, published, or digest-resolved"; images were pushed from 2026-07-27 — see §12 item 1)*,
and creating the repositories does not make ECS deployable; it produces repository
references for the future `iam` and `ecs` modules (`registry -> iam`, `registry -> ecs`).
The `storage` module declares exactly **one** private S3 application bucket with
**SSE-S3/AES256** encryption (no `bucket_key_enabled`, no KMS), versioning enabled, all
**four** public-access-block controls enabled, bucket-owner-enforced ownership (ACLs
disabled), and a **deny-only** bucket policy rejecting any request where
`aws:SecureTransport` is false. ~~**No object has been stored**, no bucket exists in AWS,~~ *(struck 2026-10-09: accurate when written on 2026-07-23; the bucket clause is stale since the bucket was created on 2026-07-27 — read-only observation of 2026-09-27; the 2026-07-28 reference refresh also read the workload buckets, `operator-closure-contract.json` (2026-08-10 text); the object clause is unverified — no recorded observation lists this bucket's objects)*
and the module does not change the application default (`storage_backend` stays `local`; the composed staging workload sets `STORAGE_BACKEND` to `s3` in `main.tf`);
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
credential, **not** the finished API/worker credential. ~~**No database exists in AWS**~~ *(struck 2026-10-09: accurate when written on 2026-07-23; stale since the instance was created on 2026-07-27 — observed available on 2026-09-27, 2026-09-30 and 2026-10-02, read-only observations; current state is not recorded here)*; the
~~`pgvector` extension is **not** activated~~ *(struck 2026-10-09: unverified — its live state is not recorded)* (activation is a deferred database-bootstrap step; no committed
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
authorized step). ~~**No cache exists in AWS**~~ *(struck 2026-10-09: accurate when written on 2026-07-23; stale since the replication group was created on 2026-07-27 — the group was observed available on 2026-09-27 and 2026-10-02 and its member node on 2026-09-30, read-only observations; in the current configuration it exists only while `staging_window_active = true`; current state is not recorded here)*, and the module grants no ECS/IAM access.
The future `ecs` module owns the standalone TCP 6379 ingress rules (from the API and
worker task security groups only; the migration task receives **no** Redis access) and
consumes `redis_security_group_id` (one-way `data_cache -> ecs`, acyclic).
The `iam` module declares the **four ECS-consumed roles** locked by
`aws-staging-iac-plan.md` §26.8 — one shared ECS task **execution role** plus three
distinct application task roles (**API**, **worker**, and an intentionally **empty
migration** role; migration code calls no AWS API). All four trust only
`ecs-tasks.amazonaws.com` with an `aws:SourceAccount` condition. Secret injection is
**execution-role-only** (`GetSecretValue` scoped to exactly the four container ARNs,
`kms:Decrypt` scoped to the secrets CMK via Secrets Manager); the application task
roles hold **no** Secrets Manager, KMS, ECR, logs-driver, RDS, or Redis permission —
only the application-bucket S3 actions the code actually makes, plus (API role only, 6B-4C) `ses:SendEmail` on one sending identity conditioned on `ses:FromAddress` and `ses:ApiVersion`. The CloudWatch Logs
policy is scoped to the deterministic `/ecs/<name_prefix>-*` prefix (never consuming
ECS/observability outputs — the §26.8 cycle break), and the sole `Resource:"*"` is
`ecr:GetAuthorizationToken`. ~~**No role exists in AWS.**~~ *(struck 2026-10-09: accurate when written on 2026-07-23; stale since the roles were created on 2026-07-27 — read as already existing by the 2026-07-28 reference refresh, `operator-closure-contract.json` (2026-08-10 text), and listed by a read-only observation on 2026-09-27; current state is not recorded here.)* It consumes `secret_arns` +
`kms_key_arn` (`secrets -> iam`), `bucket_arn` (`storage -> iam`), and
`repository_arns` (`registry -> iam`), and outputs exactly `execution_role_arn`,
`api_task_role_arn`, `worker_task_role_arn`, and `migration_task_role_arn` for the
`ecs` module (one-way `iam -> ecs`). The CI image-publisher role is implemented (above); a CI-OIDC deployment role and
the operator/observer/break-glass roles remain deferred.
The `ecs` module declares the compute plane locked by §26.2–§26.15: the ECS
cluster, the **three deterministic ecs-owned log groups** (`observability`
consumes their outputs and owns alarms, §26.9), **three per-workload task
security groups plus every task-side cross-SG rule** (ALB↔API TCP 8000 both
directions; PostgreSQL TCP 5432 from api/worker/**migration**; Redis TCP 6379
from api/worker **only** — migration is Redis-excluded; TCP 443 IPv4 NAT-baseline
egress; **no CIDR-based ingress**), **three Fargate task definitions**
(LINUX/X86_64, platform 1.4.0, immutable `sha256:`-digest-pinned images only —
the migration task reuses the worker image with the bare
`python -m app.db.migrate` upgrade-and-verify entrypoint; execution-role-only secret
injection with the locked per-workload subsets, migration injecting
`DATABASE_URL` only), and **two services** (private subnets, no public IP, circuit
breaker with rollback, ECS Exec disabled; **the migration workload is a task
definition only — never a service**, and nothing here runs it). *(Corrected 2026-10-09: the earlier statement that no cluster,
task, service, rule, or log group exists in AWS and that nothing here was planned, applied, provisioned or deployed — accurate when written on 2026-07-24 — is stale: one-shot tasks ran on the cluster on 2026-07-28 (CloudTrail, read 2026-09-27; `main.tf`, Batch 4F), the cluster is recorded as tagged in `operator-closure-contract.json` (2026-08-10 text) and was observed ACTIVE on 2026-09-27, 2026-09-30 and 2026-10-02, and three `/ecs/` log groups were observed on 2026-09-30;
no active service was observed (active-services count 0 on each of those three dates, read-only observations); current state is not recorded here.)* It consumes the implemented
outputs of `network`, `alb`, `registry`, `secrets`, `data_sql`, `data_cache`,
and `iam` (one-way edges into `ecs`; `ecs -> observability` remains the only
outbound edge, acyclic).
The `observability` module consumes the four ecs outputs
(`log_group_names`/`log_group_arns`, `api_service_name`/`worker_service_name`)
and creates **no log group** (ecs owns the three groups, §26.9): 3 error metric
filters (pattern `{ $.severity = "ERROR" }`, matching the application's JSON
`severity` field) plus **3 INFRA-7 capability filters** over the API log group
(gate-failed, override-driven enable, override set/clear — patterns match only
`event`/`outcome`/`decided_by`; no tenant identifier), **15 alarms** — 12 with
**caller-supplied** thresholds via the `alarm_thresholds` input (the plan
documents categories, not values) and 3 INFRA-7 capability alarms at an
**intrinsic fixed threshold of 1** (any occurrence of a discrete audit event is
the signal) — with explicit per-alarm missing-data behavior (fail-closed
`breaching` for service-health and DB/Redis saturation; `notBreaching` for
match-only counts), **one INFRA-7 canary observability dashboard**
(`aws_cloudwatch_dashboard.canary`, a disclosed supersede of the module's
earlier no-dashboard exclusion), and a single-region management-events **CloudTrail** trail with
log-file validation delivered to a dedicated private, versioned, SSE-S3,
TLS-only **audit** bucket (service-principal writes scoped by `aws:SourceArn`;
`storage` still owns all application buckets). DB/Redis/cluster alarm
dimensions reuse the deterministic `<name_prefix>-postgres`/`-redis-001`/
`-cluster` names (§26.8 pattern — no new graph edge; observability remains a
sink). ALB-dimension alarms are deferred until `alb` exposes `arn_suffix`
outputs (separately authorized). Optional `sns_topic_arn` is consumed, never
created. ~~**No filter, alarm, trail, or bucket exists in AWS.**~~ *(struck 2026-10-09: accurate when written on 2026-07-24; stale since 2026-07-27 — the trail has logged since 2026-07-27 per `operator-closure-contract.json` (2026-08-10 text) and was observed logging in a read-only observation on 2026-10-03; the audit bucket was created on 2026-07-27 (read-only observation of 2026-09-27); that the metric filters and alarms existed at the 2026-07-28 reference refresh is an inference from its observed describe actions; current state is not recorded here.)*
The `cost` module declares exactly **one** resource — a monthly AWS Budget
(`COST` type, USD) with an **ACTUAL-spend** notification at each fixed
threshold (50/75/90/100%, §15) delivered to a caller-supplied email address.
It is **independent** (§26.12 — no sibling input, no data source, no
consumer), **observational only** (a budget alert never stops or remediates
spending; no budget action, SNS topic, or IAM resource is created), and the
`monthly_budget_limit` input is statically validated by the module at or below the
historical **$200/month ADR-§M architecture ceiling**, and — since 2026-10-01 — by the
**staging root at or below the operator's current limit of USD 20 TOTAL per month**
(recorded 2026-09-30; the stricter of the two governs). Neither bound enforces
spending: validation constrains the declared limit and the budget's notifications
only e-mail. The last attested live budget is 150 (operator billing-console attestation of 2026-10-01; see the `cost` row in §5); this repository change alters no
budget in AWS until an authorized apply.

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
- `providers.tf` — default AWS provider (region via `var.aws_region`; default
  tags via `local.common_tags`) plus the aliased `aws.revision_reader` provider
  (same region, NO default_tags) consumed only by `module.revision_reader` so its
  executor-created IAM roles adopt without tag drift. No credentials/profile/
  role/account values in either block.
- `backend.tf` — empty S3 backend declaration; **no** values.
- `variables.tf` — typed inputs; no committed secret, account, ARN or CIDR value (ARN- and CIDR-typed inputs such as `vpc_cidr`, `acm_certificate_arn` and `role_permissions_boundary_arn` are supplied via git-ignored tfvars).
- `locals.tf` — deterministic name prefix + the eight-tag set (§A).
- `main.tf` — composition root; composes **all twelve** §26.12 modules per the
  locked graph, plus `revision_reader` (Gate 4J, outside that graph by design); no
  root-level `resource`/`data` block.
- `outputs.tf` — metadata echoes and composed-module reference outputs; `rds_db_address` is marked sensitive.
- `terraform.tfvars.example` — synthetic example inputs; never real values.
- `backend.hcl.example` — synthetic partial-backend-config template; the real
  `backend.hcl` is git-ignored and filled from the bootstrap outputs only at
  the live bootstrap *(corrected 2026-10-09: "later-authorized" was accurate when written on 2026-07-24; the bootstrap ran on 2026-07-27 — see §8)*.
- `bootstrap/` — the one-time state-backend root (§3 carve-out; own README,
  own committed lockfile, local one-time state; ~~configuration only~~ *(struck 2026-10-09: accurate when written on 2026-07-24)* executed on 2026-07-27 — see §8).

### INFRA-9 B-3 — root-wiring regression protection (ENFORCED in CI)

Deleting `module.revision_reader`'s `providers = { aws = aws.revision_reader }`
map, or adding `default_tags`/`assume_role`/`ignore_tags` to the aliased provider
block, silently reintroduces the TagRole drift (or a credential/tag reroute) that
the B-2 Stage-A barrier refused. `tofu validate` does **not** catch this (proven
2026-08-15: it reports `Success!` for the correctly-wired form, the
providers-map-deleted form, AND an aliased block carrying a literal `default_tags`).
A prior hand-rolled HCL-scanner guard was removed after six review rounds found
successive fail-open evasions, so the enforced guard parses **no HCL**.

**Enforcement**: the graded CI step `root_wiring` (job *Revision reader*) runs
`scripts/root_wiring_check.py --mode full` on every PR. OpenTofu itself is the
configuration oracle: the checker copies the git-tracked `infra/aws` tree (excluding
`bootstrap/`) into a disposable digest-bound work directory, overrides the backend to
`local` **inside the copy only**, installs the pinned provider from a
filesystem mirror verified against the committed `.terraform.lock.hcl` h1 hashes and
`provider-binary-pin.json` (no registry download, no network fallback), runs
`tofu fmt -check` (before the override exists) and `tofu validate`, completes an
offline `tofu plan -refresh=false` against a localhost-only synthetic STS stub that
answers exactly one `GetCallerIdentity` shape, and then asserts BOTH halves over
OpenTofu-generated artifacts only:

1. **Providers-map wiring + roster** — `tofu show -json` `configuration`: the exact
   15-resource `aws_*` reader roster carries `provider_config_key == "aws.revision_reader"`
   (the module's two `terraform_data` guards are outside the aws provider surface),
   the alias leaks nowhere else, every non-reader `aws_*` resource stays on the
   default provider, exactly one module call sources `./modules/revision_reader`,
   and the aws provider-config universe is closed. `tofu graph` is the secondary
   independent witness (reader nodes reference exactly the alias node; the alias
   node references exactly `var.aws_region`).
2. **Aliased-block argument set** — the alias's expression-key surface must equal
   `{region}` exactly and the default provider must keep `{default_tags, region}`,
   via the same plan JSON (the graph is blind to literal provider arguments —
   proven byte-identical DOT — which is why the plan-JSON control is primary).

The step is graded through the *Gate 4N guard results* aggregation and runs a
**positive control plus a complete in-run negative mutation battery** (map removed /
redirected, rogue `default_tags`/`ignore_tags`/`assume_role`/`profile`/credentials/
account/endpoint routing, duplicate-alias shadow provider, an in-module provider
block, a `configuration_aliases` passthrough escaping one reader resource to the
tagged default provider, shadow module, roster grow/shrink, post-bind fixture
substitution, unexpected stub action): every doctored
copy must fail at its designated stage with its expected signature, so JSON/DOT
format drift or a fail-open regression in the checker itself fails the job. The
expected values live in the reviewed contract
`tests/fixtures/root-wiring-contract.json`; the probe inputs in
`tests/fixtures/root-wiring-synthetic.tfvars.example` are synthetic and never applied. This
same step gives root `main.tf`/`providers.tf` their `tofu fmt`/`tofu validate` CI
gate (previously module-only). The committed S3 `backend.tf` is never initialized by
any of this. Configuration wiring only: it does not verify live-state adoption and
does not replace the B-2/B-3 import/plan/apply gating.

## 8. Remote-state design (configuration authored, ~~not initialized~~ bootstrapped 2026-07-27 — corrected 2026-10-09)

The design targets an **S3** state backend with **SSE-KMS** encryption,
versioning, blocked public access, and **DynamoDB** state locking
(`aws-staging-iac-plan.md` §7). Status:

- The bootstrap **configuration** is authored at `bootstrap/` (SSE-KMS state
  bucket + dedicated CMK/alias + `LockID` DynamoDB lock table) and
  offline-validated when authored (no CI step covers `bootstrap/`), then executed on 2026-07-27, with DynamoDB-only locking recorded in `operator-closure-contract.json` (2026-08-17 text). §7 also permits the tool-native S3 `use_lockfile`
  equivalent; DynamoDB is authored as the plan's named mechanism and the
  live-bootstrap authorizer may revisit that choice.
- **No backend identifiers** (bucket, key, region, KMS id, DynamoDB table, role
  ARN, workspace prefix) are committed — `backend.tf` is intentionally empty,
  the bucket/table names are required git-ignored `*.tfvars` inputs, and only
  `*.example` templates are tracked (`backend.hcl.example` carries placeholders plus a literal state key and region).
- **Live state bootstrap (reconciled 2026-10-03, `P6-GOV-1`):** the earlier statement that no
  state bucket, lock table or key existed described the pre-INFRA-9 state and is superseded
  by the live execution recorded in `operator-closure-contract.json`. Bucket, table and key
  names remain git-ignored `*.tfvars` inputs and are not tracked.
- ~~The S3 backend has **not** been initialized.~~ *(struck 2026-10-09: accurate when written on 2026-07-22; stale by inference from `operator-closure-contract.json` (2026-08-10 and 2026-08-17 text) — its state-backend closure is "required by tofu init/plan/apply against the S3 backend", the state bucket has a bucket key and the lock table is encrypted with the same CMK with CloudTrail showing both encryption contexts, and locking is DynamoDB-only; a read-only observation on 2026-10-02 found the lock table ACTIVE and the state bucket KMS-encrypted; no state file was read.)* Offline validation uses
  `tofu init -backend=false`, which deliberately skips backend initialization; a
  backend-configured `tofu init` ~~has not been run and no state exists~~ *(struck 2026-10-09: contradicted by inference from the contract, as above; state contents are not recorded here)*.

## 9. Security rules

Committed IaC in this repository must contain **no**: AWS credentials, real
account ids, real ARNs, secret values, committed state, committed plans, provider
cache, or real `.tfvars`. Real identifiers and secret references are supplied only
at a later authorized implementation/apply time via variables/state — never
committed.

## 10. Current validation status

**Offline validation only.** All twelve §26.12 modules (each in its own tranche)
and `revision_reader` (which additionally carries an offline `tofu test` contract suite
run in CI against a fully mocked provider), the root composition, and (when authored; no CI step covers it) the `bootstrap/` root have been checked
with `tofu fmt`, `tofu init -backend=false`
(using a disposable, repository-external data directory and the locked provider), and
`tofu validate` — all offline, with the S3 backend disabled and AWS credentials
suppressed. The committed `.terraform.lock.hcl` pins `hashicorp/aws 6.55.0`. This offline
validation runs **no** live `tofu plan`, `apply`, `destroy`, `import`, `state`, or `refresh` (CI's root-wiring check plans offline with `-refresh=false` against a 127.0.0.1-only synthetic STS stub) and
contacts **no** AWS API — the staging foundation's 2026-07-28 reference refresh is recorded in
`operator-closure-contract.json` (`P6-GOV-1` reconciliation, 2026-10-03) and its apply and publish history in dated, operator-held evidence, not here; **no** repository-local `.terraform` directory or state is
committed. Offline validation confirms configuration validity — it does **not** mean
any AWS resource exists.

## 11. Dark-state rules

Infrastructure existence must **never** activate product behavior. All three
global feature flags remain Boolean `false`
(`opportunity_feedback_enabled`, `scout_scheduling_enabled`,
`connector_rss_enabled`). No capability override is created by this or any INFRA
tranche.

## 12. Status and next tranches (each separately authorized)

**INFRA-4 repository-only scope is complete**; ~~**INFRA-5 is unstarted.**~~ *(struck 2026-10-09: accurate when written at 15:49Z on 2026-07-24; stale since INFRA-5 workflow authoring completed at 17:06Z the same day)* **INFRA-5 (workflow authoring) is complete** — item 1 below. Done:
tool-assisted validation + `.terraform.lock.hcl` (cross-platform provider
checksums), **all twelve module bodies**, the **full root composition** (all
twelve modules wired in `main.tf` per the locked §26.12 graph, plus the Gate 4J
`revision_reader` module wired alongside it), the resolved
pre-live-apply gates (ALB **access + connection logging**; the §25
rate-limiting-behind-proxy fix — uvicorn trusted-proxy resolution, VPC-CIDR-only
trust, pinned by `apps/api/app/tests/test_rate_limit.py`; API graceful shutdown
via the earlier ECS `stopTimeout` work), and the **remote-state bootstrap
configuration** (`bootstrap/` + `backend.hcl.example`). Everything is
offline-validated in the repository; ~~**nothing provisioned or deployed**~~ *(struck 2026-10-09: already inaccurate when this line was last edited on 2026-10-02 — the phrase, first written 2026-07-24, was carried unchanged after the foundation apply from 2026-07-27; no active ECS service was observed on 2026-10-02 (read-only))*. WAF, ACM creation,
and the interactive-docs path restriction remain **deferred by locked decision**
(§23/§24.7/§25; runtime contract §N); the API Route 53 alias is now authored as a
windowed `alb`-owned record (§24.7, 2026-10-01 — configuration and tests, not
applied as last recorded on 2026-10-03, `docs/project-phase-6-plan.md` §4.6a); the separate, expiring **window-transition principal** is emitted by
`scripts/gen_operator_policies.py` in three documents (inline + two customer
managed policies, 2026-10-02 — generated and tested, NOT provisioned as last recorded on 2026-10-03; W0 itself was
restored to its reviewed document on 2026-10-03 under a separate live
authorization *(corrected 2026-10-09: the 2026-10-02 text, accurate when written, said W0 "is unchanged and is to be restored"; restoration recorded in `docs/project-phase-6-plan.md` §4.6a)*; `docs/operations/staging-window.md` §8/§12). Remaining
(each separately authorized):

1. **INFRA-5 (workflow authoring): COMPLETE.** The protected staging publish
   workflow (`.github/workflows/staging-publish.yml`) is authored with GitHub
   **OIDC** (no long-lived keys), the protected human-approval `staging`
   GitHub environment, fail-closed prerequisite guards, and the two-image
   §26.5 build/scan/push/digest-read-back path — ~~**never executed**~~ *(struck 2026-10-09: accurate when written on 2026-07-24; stale since 2026-07-27 — CloudTrail, read on 2026-09-27, shows image pushes by the CI image-publisher role in sessions named by this workflow's `role-session-name` (`signalnest-staging-publish-${{ github.run_id }}`) on 2026-07-27, 2026-07-28 and 2026-07-29, so image digests were produced; that these sessions were runs of this workflow is an inference from that naming and the publisher role's trust in the repository's `staging` environment; run history is not recorded in this repository)*: authoring followed the
   phase plan's stop boundary ("authoring only; execution needs INFRA-9
   authorization"); ~~no run has occurred and no digest exists~~ *(struck 2026-10-09, as above)*. Operator design:
   `docs/operations/staging-publish-workflow.md`.
2. **Live remote-state bootstrap + INFRA-9** *(corrected 2026-10-09: largely done — the bootstrap and foundation provisioning, including the two ECR repositories and the CI-OIDC publisher role, from 2026-07-27; backend initialization, inferred from `operator-closure-contract.json`; image pushes from 2026-07-27 to 2026-07-29, inferred to be publish-workflow runs; no active ECS service was observed on 2026-10-02 (read-only))*: executing the authored
   `bootstrap/` root, backend-configured `tofu init`, authenticated `plan`,
   provisioning (including the two ECR repositories and the specified CI-OIDC
   publisher role), executing the publish workflow to produce the image
   digests, and deployment of the exact first-deploy SHA — under fresh
   authorization, with all global flags remaining `false`.

## 13. Never auto-apply

OpenTofu is **never** auto-applied. Any future `apply` is gated behind a
protected, human-approved deployment path (INFRA-5 approval gate; INFRA-9 fresh
authorization). Infrastructure setup and canary activation are never combined.

## 14. Command execution scope

*(Corrected 2026-10-09: the earlier text said only offline commands "have been run" and that live commands shown here "have not been executed"; live OpenTofu operations ran during INFRA-9 — e.g. the 2026-07-28 reference refresh recorded in `operator-closure-contract.json`. This section now describes the repository's own validation and illustrations.)* Repository validation runs only **offline** OpenTofu commands (`fmt`, `init -backend=false`, `validate`)
against the implemented modules, in a repository-external data directory with the
backend disabled and AWS credentials suppressed. Any **live** OpenTofu/AWS command
(`plan`, `apply`, `destroy`, `import`, `state`, `refresh`, or any AWS CLI/SDK call)
shown in this directory's documentation is **illustrative only** — showing it authorizes nothing;
live operations remain gated behind INFRA-5 approval and INFRA-9 fresh
authorization (§13).
