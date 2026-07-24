# AWS staging operational procedures — secrets, data protection, and recovery (Phase 4B-C · INFRA-6)

- **Status:** PROCEDURES/DEFINITIONS ONLY. This document authors the operational
  side of the INFRA-3 secret contract and the data-protection/recovery
  procedures for SIGNALNEST_STAGING. Per the INFRA-6 stop boundary
  ([phase-4b-c-infra-plan.md](../phase-4b-c-infra-plan.md)): **"procedures/
  definitions only; no secret is created here"** and **"Expected external
  resources: none created in this tranche."** Authoring and merging this
  document creates no AWS resource, populates no secret, runs no command
  against any live system, and authorizes no live operation.
- **Authoritative parents:**
  [aws-staging-secret-inventory.md](./aws-staging-secret-inventory.md)
  (INFRA-3 inventory/disposition/G5 contract — this document does not weaken
  or restate it),
  [aws-staging-iac-plan.md](./aws-staging-iac-plan.md) (§26.6 container-vs-value
  lifecycle, §26.7 per-actor subsets, §26.8 identity plane),
  [aws-staging-runtime-contract.md](./aws-staging-runtime-contract.md),
  the merged module contracts (`infra/aws/modules/secrets`, `data_sql`,
  `data_cache`, `iam`, `ecs` — cited by reference, never re-implemented here).
- **Sanitization rule (public repository):** no AWS account id, real ARN,
  endpoint hostname, VPC/subnet/SG id, or secret value (including encodings,
  hashes, or fragments) may ever appear in this document, in evidence derived
  from it, or in any revision of it. Examples use `<PLACEHOLDER>` tokens only.

---

## 1. Scope and boundaries

**In scope (definitions/procedures):** secret population, rotation, and
incident-mechanics procedures for the four staging runtime secrets; the
population-operator least-privilege design; the database-initialization
procedure (application role + `DATABASE_URL`/`REDIS_URL` composition rules);
backup posture and the restore runbook; TLS/network-isolation/egress
verification checklists; security review.

**Out of scope (each separately authorized):** executing any procedure in this
document; creating any AWS resource, IAM identity, or secret version; the G5
gate *implementation* (INFRA-3 follow-up); observability/incident *decision*
runbooks (INFRA-7); image publication and deployment (INFRA-9); any `.tf`
change; any feature activation. The five capability flags remain `False`
throughout.

**Position in the locked live sequence** (aws-staging-iac-plan.md §26.6;
`secrets` module README §8 — stated identically in both):

1. Bootstrap remote state and apply the prerequisite infrastructure (INFRA-9,
   fresh authorization — creates the secret containers, CMK, data stores).
2. Obtain the database/cache endpoints via the approved path (root outputs).
3. **Populate the four secret values out-of-band** (the §3 procedure below —
   authored here, executed only under that later authorization).
4. Run the **fail-closed G5 secret-readiness check** (separately implemented).
5. Create/start the ECS services. An empty container is **not** sufficient for
   service start: ECS resolves `secrets.valueFrom` at task launch, so an
   unpopulated container fails the task — G5 exists to catch this before it
   happens live.
6. Execute the one-shot migration task under separate authorization.

Canary activation remains an entirely separate, later human authorization.

## 2. The four staging runtime secrets (inventory reference)

The inventory, dispositions, and per-actor subsets are owned by
`aws-staging-secret-inventory.md` (§3.1, §4) and §26.7; this section only
binds the procedures to them. Containers are the four **empty** Secrets
Manager containers created by the `secrets` module, deterministically named
`<name_prefix>/<LOGICAL_KEY>` and encrypted by the module's customer-managed
KMS key.

| Logical key | Consumers (§26.7) | Staging requirement | Format contract | Rotation class |
| --- | --- | --- | --- | --- |
| `SECRET_KEY` | api (functional), worker + migration (construction) | Required; startup fails closed on empty/dev default | Non-empty, strong, newly generated for staging; never the dev default | Hard cutover (§5.1) |
| `DATABASE_URL` | api, worker, migration (all three — one shared credential) | Required | `postgresql+psycopg://<APP_ROLE>:<ENCODED_PASSWORD>@<DB_ENDPOINT>:5432/<DB_NAME>?sslmode=require` (credentials percent-encoded; `sslmode=require` minimum) | Application-role rotation (§5.2) |
| `REDIS_URL` | api, worker only — **migration excluded** (§26.7) | Required — the composed root pins `QUEUE_BACKEND=redis` / `CACHE_BACKEND=redis` for api and worker (`infra/aws/main.tf` env maps), so the Redis path is selected in staging | `rediss://<REDIS_ENDPOINT>:6379/0` — TLS scheme mandatory (`transit_encryption_mode = "required"`); **no credential component exists** (Option A: no auth token) | No rotatable credential (§5.3) |
| `LLM_API_KEY` | api + worker (functional), migration (construction) | Required; staging forbids `llm_provider=mock` and dev fallback | Provider key for the configured `openai`/`anthropic` provider; dedicated staging key with a low provider-side spend cap (runtime contract) | Provider rotation (§5.4) |

Non-secret metadata only, always: procedures and evidence reference these by
logical key, container name pattern, and ARN-by-reference. Values are never
printed, logged, diffed, hashed-for-evidence, or retained. `S3_ACCESS_KEY_ID`
/ `S3_SECRET_ACCESS_KEY` remain **unset forever** in staging (task-role
credential chain; G5-18).

## 3. Secret-population procedure (authored now; execution separately authorized)

**Preconditions (all fail-closed; abort if any is unmet):**

1. The live sequence has reached step 3 of §1 (containers and endpoints exist
   via the INFRA-9 apply). Executing earlier is impossible (no containers) and
   prohibited.
2. The operator holds an explicit, current authorization for this execution.
3. Identity verification: `aws sts get-caller-identity` resolves to the
   intended SIGNALNEST_STAGING account (compare account id against the
   operator's out-of-band record — never paste it into a shared transcript)
   and region `us-east-1`. Any mismatch, or any indication of a production
   account, aborts.
4. Destination allowlist is **exactly** the four container names
   `<name_prefix>/SECRET_KEY`, `<name_prefix>/DATABASE_URL`,
   `<name_prefix>/REDIS_URL`, `<name_prefix>/LLM_API_KEY`. Any other
   identifier is out of allowlist — abort. No container is ever created,
   deleted, or replicated by this procedure (`create-secret`,
   `delete-secret`, `--force-overwrite-replica-secret`, and replica regions
   are all prohibited).
5. Source values arrive through the operator's secure channel (password
   manager / provider console). They must never enter a Claude/agent
   transcript, a Git-tracked file, CI, a shell argument, or shell history.

**Per-secret invocation pattern (the only approved shape):**

```bash
# Interactive operator shell. NO set -x, NO --debug/--trace, ever.
# Write the value to a memory-backed, owner-only temp file WITHOUT echoing it:
#   install -m 600 /dev/null "$F"      # F on a tmpfs path, e.g. /dev/shm/<random>
#   IFS= read -rs SECRET_VALUE && printf '%s' "$SECRET_VALUE" > "$F"; unset SECRET_VALUE
# (silent prompt; printf is a shell builtin, so the value never reaches the
# process list, shell history, or an editor. If an editor must be used instead,
# it MUST run with swap/backup/undo files disabled — e.g. `vim -n` — otherwise
# an auxiliary editor file can persist the plaintext on disk outside tmpfs.)
aws secretsmanager put-secret-value \
  --secret-id "<name_prefix>/<LOGICAL_KEY>" \
  --secret-string "file://$F" \
  --cli-binary-format raw-in-base64-out \
  --query 'ARN' --output text
# Deterministic cleanup IMMEDIATELY, on success AND failure:
shred -u "$F" 2>/dev/null || rm -P "$F"
```

Prohibited variants: `--secret-string '<literal>'` (shell history/process
list), `--secret-string "$VAR"` (tracing/history), any here-doc or inline
construction of `$F`'s contents (`cat <<EOF > "$F"` with the literal value is
equivalent to a literal argument for history/session-buffer purposes), any
wrapper that echoes the value, any CI/GitHub Actions execution (no scoped
population role exists; CI logs are a leak surface; the four values never
transit GitHub in any form).

**Verification (metadata-only, never `get-secret-value`):**

```bash
aws secretsmanager describe-secret --secret-id "<name_prefix>/<LOGICAL_KEY>" \
  --query '{Name:Name,Changed:LastChangedDate,Stages:VersionIdsToStages}' --output json
aws secretsmanager list-secret-version-ids --secret-id "<name_prefix>/<LOGICAL_KEY>" \
  --query 'Versions[].{Id:VersionId,Stages:VersionStages}' --output json
```

Pass criteria: exactly one `AWSCURRENT` version (plus at most one
`AWSPREVIOUS` after a repeat), `LastChangedDate` within the operation window.
Recorded evidence: logical key, version id, stage labels, timestamp —
**nothing else**.

**Idempotency and rollback:** `put-secret-value` creates a new `AWSCURRENT`
and automatically relabels the prior one `AWSPREVIOUS` — that is the built-in
rollback path. To roll back: either `put-secret-value` again with the known-
good value, or move `AWSCURRENT` back to the previous version id with
`aws secretsmanager update-secret-version-stage`. Previous versions are never
deleted by this procedure. Re-running the procedure is safe and repeatable.

**No downstream side effects:** population never restarts tasks, deploys,
migrates, or activates anything. Running services pick up a new value only on
their next task start (§5).

## 4. Population-operator principal (design decision; creation separately authorized)

The "permanent secret-operator principal" was left as an undecided
live-operation gate by §26.6. This document decides its **design**; its
creation (IaC + trust policy) remains a later-authorized change and is *not*
part of INFRA-6.

- **Interim model (first population):** a human operator using an existing
  administrative staging identity, interactively, under the §3 rules. No
  automation, no GitHub Actions path, no long-lived key minted for this.
- **Permanent least-privilege specification** (for the future IaC):
  - `secretsmanager:PutSecretValue`, `secretsmanager:DescribeSecret`,
    `secretsmanager:ListSecretVersionIds`, and (rollback only)
    `secretsmanager:UpdateSecretVersionStage` scoped to **exactly the four
    container ARNs** (the `secrets` module's `secret_arns` output). No
    `secretsmanager:*`, no `GetSecretValue` (read access, if ever needed for
    troubleshooting, is a separate explicitly-justified grant), no
    create/delete/policy actions.
  - `kms:GenerateDataKey` + `kms:Decrypt` scoped to the secrets CMK ARN,
    conditioned `kms:ViaService = secretsmanager.us-east-1.amazonaws.com` —
    mirroring the execution-role pattern in the merged `iam` module. No KMS
    key-management actions.
  - No ECS, EC2, RDS, ElastiCache, S3, or IAM permission of any kind.
- **Invariant:** the four values flow **operator → Secrets Manager** directly.
  They never exist as GitHub repository/environment secrets or variables, in
  any workflow file, artifact, or cache, at any time.

## 5. Rotation procedures (mechanics — INFRA-6) and the incident boundary

Rotation happens in Secrets Manager; task definitions reference ARNs, so a
rotation is picked up on the **next task start** with no image rebuild
(inventory §6). Rolling the ECS services after rotation is a deployment-plane
action executed under the then-current operational authorization — this
document defines when it is required, not permission to do it.

### 5.1 `SECRET_KEY` — hard cutover
The application holds a single signing key (no dual-key window), so rotation
**immediately invalidates all outstanding signed tokens/sessions** once tasks
restart. Procedure: generate a new strong value → §3 population → restart
API/worker tasks in one window → expect forced re-authentication. Schedule
deliberately; this is not a zero-downtime rotation. (Key-versioning support
would be a separate application change.)

### 5.2 `DATABASE_URL` — application-role credential rotation
Rotate the **application role's** password (§6): as the administrative
identity, in a psql session started with history disabled
(`PSQL_HISTORY=/dev/null psql "sslmode=require" ...`), use the interactive
**`\password <APP_ROLE>`** meta-command — it prompts without echo and never
places the literal in psql's history. A literal
`ALTER ROLE ... PASSWORD '<value>'` statement is **prohibited**: psql's
default readline history (`~/.psql_history`) would persist the plaintext SQL
to disk. Then recompose the URL (§2 format) → §3 population → restart
api/worker (and any future migration run picks up the new value
automatically). The RDS-managed
**master** secret rotates independently under RDS's own managed rotation —
it is administrative, is never embedded in `DATABASE_URL`, and its rotation
never requires an application change.

### 5.3 `REDIS_URL` — no rotatable credential
Option A carries **no auth token**; access control is network-only (SG +
private subnets). There is nothing to rotate. The container's value changes
only if the **endpoint** changes (e.g., replication-group replacement) —
recompose and repopulate then. Do not invent an auth-token rotation path; one
does not exist under the current locked design.

### 5.4 `LLM_API_KEY` — provider rotation
Issue a new key at the provider (dedicated staging key, low spend cap) → §3
population → restart api/worker → revoke the old key at the provider. Provider
revocation is the authoritative kill switch.

### 5.5 Incident boundary (INFRA-6 vs INFRA-7)
**INFRA-6 owns the mechanics** above: how to safely place a new value and roll
back. **INFRA-7 owns the decision and containment apparatus**: detection,
severity, communication, and the incident runbook that decides *when* to
rotate. Per the inventory §6: a suspected exposure triggers immediate rotation
of the affected entry and redeployment, and the exposed value is treated as
compromised — the mechanics for that response are §§5.1–5.4.

## 6. Database-initialization procedure (definition)

**Decision (resolves the deferred `data_sql` items):** staging uses **one
dedicated application database role** (working name `<APP_ROLE>`, e.g.
`signalnest_app`) — **not** the RDS master user — as the identity inside
`DATABASE_URL`.

- **Why one role:** §26.7 gives api, worker, **and** migration the *same*
  `DATABASE_URL` container, so exactly one application credential exists. The
  migration actor runs DDL through it; therefore the role holds DDL+DML
  **within the application database only** (`CREATEDB`/`CREATEROLE`/superuser
  attributes are withheld; it owns the application database's objects).
- **Why not the master user:** the RDS-managed master credential is
  administrative/bootstrap-only (`manage_master_user_password = true`; its
  secret has no graph edge and is not an ECS input). Embedding it in
  `DATABASE_URL` would put superuser-equivalent rights in every task and fuse
  application-credential rotation to master-credential rotation.

**Procedure (executed once, at live-sequence step 2–3, under that
authorization):**

1. Retrieve the master credential transiently from the RDS-managed secret
   (administrative access; interactive; never written to disk or history).
2. Connect with `PSQL_HISTORY=/dev/null psql "sslmode=require" ...` to the
   instance endpoint (psql history disabled for the whole session — psql's
   default `~/.psql_history` would otherwise persist any literal credential
   SQL to disk).
3. `CREATE ROLE <APP_ROLE> LOGIN;` then set its password with the interactive
   **`\password <APP_ROLE>`** meta-command (prompts without echo; never a
   literal `PASSWORD '<value>'` clause in SQL). The generated value must be
   strong and percent-encoding-safe (or encoded at composition). Grant the
   role ownership of the application database
   (`ALTER DATABASE <DB_NAME> OWNER TO <APP_ROLE>;`) so migrations can run
   DDL without superuser rights.
4. **Optional, separately authorized:** `CREATE EXTENSION IF NOT EXISTS
   vector;` (requires master privileges). This remains **deferred** — the
   composed root deliberately omits `VECTOR_BACKEND` (application default
   `bruteforce`); run this step only when the pgvector bootstrap is explicitly
   authorized. Nothing in this tranche changes that posture.
5. Compose `DATABASE_URL` per §2 and populate per §3. Discard all transient
   credential material; the master credential remains only in its RDS-managed
   secret.
6. End state: master = administrative/incident-only; `<APP_ROLE>` = the sole
   application credential, rotatable per §5.2.

## 7. Backup posture (existing substrate — referenced, not modified)

Already configured by merged IaC (cited; no HCL change in this tranche):

- **RDS PostgreSQL** (`data_sql` module): automated backups
  `backup_retention_period` default **7 days** (bounded 1–35; cannot be
  disabled), `copy_tags_to_snapshot`, `deletion_protection` input,
  `skip_final_snapshot = false` with the deterministic final-snapshot
  identifier supplied by the composed root, storage encrypted.
- **ElastiCache Redis** (`data_cache` module): `snapshot_retention_limit`
  (caller-configured) daily snapshots.
- **S3 application bucket** (`storage` module): versioning enabled — object
  recovery = restore a prior version. The alb-logs and audit buckets are
  telemetry (versioned; no restore obligation beyond retention).
- **Documented gap (not silently fixed):** `data_sql` sets **no explicit
  `backup_window`/`maintenance_window`** — both fall to AWS-assigned defaults.
  Before relying on a specific time, verify with
  `aws rds describe-db-instances` (read-only). Pinning explicit windows would
  be a small, separately-authorized `data_sql` change.

## 8. Restore runbook

**Staging recovery targets** (staging-grade, non-customer; production targets
are a later, separate decision): **RPO ≤ 24 hours** (guaranteed by daily
automated backups; RDS point-in-time recovery typically achieves ≈ 5 minutes)
and **RTO ≤ 8 working hours** from the decision to restore.

### 8.1 RDS PostgreSQL restore
1. Decide the restore point (PITR timestamp within the retention window, or a
   specific automated/final snapshot). Restoring is a **new-instance**
   operation: `aws rds restore-db-instance-to-point-in-time` (or
   `restore-db-instance-from-db-snapshot`) creates a NEW instance with a NEW
   endpoint — the original is never overwritten in place.
2. Restore into the same private subnet group and attach the existing
   rule-free RDS security group (ecs owns the ingress rules — no SG edits).
3. **KMS caveat (locked in the `data_sql` README):** the storage KMS key is
   ForceNew — a snapshot-restore is also the only path to *change* keys. A
   restore preserves the snapshot's encryption unless explicitly re-keyed at
   restore time; a "restore onto a new key" DR scenario must plan for full
   re-encryption via this path.
4. Verify on the new instance (read-only psql, `sslmode=require`): schema at
   the expected Alembic head, `<APP_ROLE>` present with expected grants, row
   spot-checks.
5. Repoint the application: recompose `DATABASE_URL` with the new endpoint
   (§2), populate per §3, run G5, restart tasks (deployment-plane action under
   the then-current authorization). If the app role or its password did not
   survive the restore point, re-run §6 first.
6. **Rollback of the restore:** the original instance (if intact) and the
   pre-restore `AWSPREVIOUS` secret version still exist — repoint back by
   restoring the previous `DATABASE_URL` version and restarting. Decommission
   whichever instance is abandoned only under explicit authorization
   (deletion protection and final-snapshot settings apply).

### 8.2 ElastiCache Redis restore
Redis holds **cache and queue-transport state only** (durable jobs live in
PostgreSQL — the queue is DB-backed with Redis as availability signaling), so
the default recovery is **accept cache loss**: create/replace the replication
group (optionally seeded `snapshot_name`), recompose `REDIS_URL` for the new
endpoint, populate per §3, restart api/worker. No credential exists to
restore.

### 8.3 S3 object restore
Versioned bucket: restore = copy the desired prior version over the current
one (or delete the delete-marker). No secret interaction.

### 8.4 Completeness checklist (the phase plan's acceptance test)
Restore point selection ✓ · new-endpoint consequence ✓ · KMS/ForceNew caveat
✓ · post-restore verification ✓ · secret recomposition/rotation path ✓ · G5
before service reattachment ✓ · rollback-of-restore ✓ · RTO/RPO stated ✓ ·
no step mutates anything until its own execution is authorized ✓.

## 9. TLS, network-isolation, and egress verification (checklist over merged IaC)

Procedures verify the already-merged posture — they change nothing:

- **TLS in transit:** ALB HTTPS-only listener, `ELBSecurityPolicy-TLS13-1-2-2021-06`,
  no port-80 listener (`alb` module); CloudFront + consumed ACM certificate
  (`edge`); `rds.force_ssl = 1` parameter (`data_sql`);
  `transit_encryption_mode = "required"` (`data_cache`); deny-non-TLS bucket
  policies (`storage`, alb-logs, audit). Verification: read-only inspection of
  listener/parameter/policy attributes; a `sslmode=disable` psql attempt MUST
  fail.
- **Isolation:** DB/Redis in private subnets, `publicly_accessible = false`,
  rule-free data-store SGs with every ingress rule ecs-owned per the §26.3
  matrix (ALB↔API 8000; 5432 api/worker/migration; 6379 api/worker only; no
  CIDR ingress). Verification: SG rule listing matches the matrix exactly; any
  extra rule is a finding.
- **Egress:** NAT-only TCP 443 baseline; VPC endpoints remain a separately
  authorized improvement. Verification: task SG egress rules list exactly the
  §26.4 set.

## 10. Security review

- **No production reuse:** no production environment exists; every staging
  value is **newly generated for staging** (SECRET_KEY generated fresh; DB
  credential created by §6; LLM key a dedicated staging key with a provider-
  side spend cap). Nothing is copied from any other environment, ever.
- **Least-privilege egress:** §9 baseline; the population/DB-init procedures
  require no egress change.
- **Leak-vector rules (binding on every execution of any procedure here):** no
  values in command-line args, shell history, tracing output, `--debug`, CI
  logs, artifacts, tofu plans/state (no `aws_secretsmanager_secret_version`
  resource may ever be added — values must never enter state), Git, or
  transcripts. Temp files are memory-backed, 0600, and deterministically
  destroyed. Evidence is sanitized metadata only. Base64/hash/partial forms of
  a value are still the value.
- **State safety invariant:** the HCL plane owns containers; the operational
  plane owns values. Preserving that split is a standing requirement, not a
  historical accident.

## 11. What merging this document does and does not do

Does: lock the operational procedures, the application-DB-role and
operator-principal designs, the rotation/incident mechanics split
(INFRA-6/INFRA-7), the restore runbook, and the verification checklists that
INFRA-7 and INFRA-9 build on.

Does not: create, read, populate, rotate, or delete any secret; touch AWS or
GitHub state; change any `.tf` file; implement G5; deploy, migrate, or
activate anything. All five capability flags remain `False`; Alembic remains
`98289430a3ec` (12 migrations); execution of every procedure above requires
its own later authorization within the §1 sequence.
