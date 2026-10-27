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
would be a separate application change.) `SECRET_KEY` is also the HMAC key of
the opaque per-workspace tenant reference sent to the LLM provider
(`app.llm.metering.tenant_reference`), so rotating it **changes every tenant
reference**: the provider sees each workspace under a new identifier from the
first call a new task makes. Usage rows (`llm_usage_events`) carry workspace
ids, not the reference, so metering and budgets are unaffected.

### 5.2 `DATABASE_URL` — application-role credential rotation

Two rotation paths exist. The zero-handoff noninteractive path (section 6.1)
is the default and preferred mechanism whenever the rotation can be driven by
the one-shot bootstrap task; the interactive human path below is retained for
any operator-performed rotation that is not run through that task.

**Preferred (zero-handoff, noninteractive):** re-run the bootstrap module in
its separately-authorized recovery mode (section 6.1). The task generates a
fresh password internally, rotates the role's credential via a driver-bound
parameter, writes the recomposed `DATABASE_URL` directly to its container, and
verifies with a fresh TLS round trip - no operator ever sees or handles the
value, and no password-bearing SQL is ever rendered. Then restart api/worker
(and any future migration run picks up the new value automatically).

**Interactive (human-performed rotation):** rotate the **application role's**
password (section 6.2): as the administrative identity, in a psql session
started with history disabled
(`PSQL_HISTORY=/dev/null psql "sslmode=require" ...`), use the interactive
**`\password <APP_ROLE>`** meta-command - it prompts without echo and never
places the literal in psql's history. A literal
`ALTER ROLE ... PASSWORD '<value>'` statement is **prohibited**: psql's
default readline history (`~/.psql_history`) would persist the plaintext SQL
to disk. Then recompose the URL (section 2 format), populate per section 3,
restart api/worker.

In both paths the RDS-managed **master** secret rotates independently under
RDS's own managed rotation - it is administrative, is never embedded in
`DATABASE_URL`, and its rotation never requires an application change.

### 5.3 `REDIS_URL` — no rotatable credential
Option A carries **no auth token**; access control is network-only (SG +
private subnets). There is nothing to rotate. The container's value changes
only if the **endpoint** changes (e.g., replication-group replacement) —
recompose and repopulate then. Do not invent an auth-token rotation path; one
does not exist under the current locked design.

### 5.4 `LLM_API_KEY` — provider rotation (restart-based)
The credential contract is **one platform-owned provider key per environment**
(`<name_prefix>/LLM_API_KEY`), shared by every tenant; there are no per-tenant
keys. Rotation is **restart-based**: no running process ever re-reads the key.

**Which processes hold the key, and when they read it.**
- **API tasks** (`uvicorn app.main:app`) and **worker tasks**
  (`python -m app.jobs.worker`) both receive `LLM_API_KEY` (the `api` and
  `worker` secret subsets in `infra/aws/modules/ecs`). ECS resolves the secret
  once, at **task start**, into the container environment.
- In both processes, importing the application imports
  `app.llm.service`, whose module-level `llm_service` builds the provider at
  **import time** from `get_settings()`. `get_settings()` is cached for the
  life of the process (`@lru_cache`), and the provider keeps the key it was
  built with. The API process holds the key this way even though it does not
  call the provider itself (runs execute on the worker).
- The one-shot **migration** task does not receive `LLM_API_KEY` and does not
  import `app.llm.service`. The E6 live probe (`python -m app.llm.live_probe`,
  a separately authorized one-off task) reads the key when it starts.
- Consequence: a new key takes effect **only in tasks started after the secret
  changes**. A task that is already running keeps sending the old key until it
  stops; there is no reload signal and no in-process refresh.

**Procedure.** Issue a new key at the provider (dedicated staging key, low
spend cap) → §3 population → **replace every API and every worker task**
(all services, all tasks, including any scaled-out or one-off task started
before the change) **while the old key is still valid** → confirm no task
started before the secret change is still running → only then revoke the old
key at the provider. Revoking first, or leaving any old task running, makes
that task's provider calls fail (permanent provider errors) until it is
replaced. Provider revocation remains the authoritative kill switch; in an
exposure incident (§5.5) revoke immediately and accept those failures.

**In-flight jobs during the task replacement.** A worker task being replaced
gets `SIGTERM`: it stops claiming jobs and waits up to
`worker_shutdown_grace_seconds` (one shared deadline for all slots,
`JobRunner.join`) for in-flight attempts to finish. An attempt that finishes
inside that grace completes normally on the **old** key; that is why the old
key must stay valid until the old tasks are gone. An attempt **still running
when the grace ends** (the slot threads are daemon threads, so the process
exits under them), when a second signal shortens the grace to
`worker_force_shutdown_grace_seconds`, or when the task is killed (`SIGKILL`
at the task's `stopTimeout`) is abandoned mid-attempt. On PostgreSQL every
model call of that attempt was reserved against the workspace's month before
it was made and settled right after it (the budget ledger below), so the
abandoned attempt leaves at most one call reserved and unsettled; the worker's
recovery sweep later charges that call its worst case. Its lease expires and a
worker on a new task re-runs the job with the new key, starting from the
ledger's recorded amount. Keeping `worker_shutdown_grace_seconds` above the
longest expected scout run (and the worker `stopTimeout` at or above it, as
`infra/aws/modules/ecs` already requires) and rotating at a low-traffic time
still reduce the number of attempts abandoned this way.

**Per-workspace LLM budget ledger (D-LLM-1 option B, `app.llm.budget`, `app.llm.bound`).** The
guarantee the code gives, verbatim from `app.llm.metering`: *On PostgreSQL, the sum of the worst-case cost bounds of all provider calls
started by a workspace in a UTC calendar month never exceeds the workspace
budget as configured at reservation time, across any number of processes. A
call's bound is `worst_case_cost` of the exact request bytes under the
configured price schedule (UTF-8 bytes bound the tokens, plus stated overhead
constants, plus the request's output limit, in exact decimal arithmetic
rounded up at 1e-6), and a call whose cost cannot be bounded is not made. This
is the application's defined budget guarantee; it is not a statement about the
provider's invoice. Not covered: provider-side billing differences from the
price schedule, including the tokenizer premise and overhead constants the
bound rests on (stated in `app.llm.bound`); calls made outside the metered
service (E6 probe); budget changes mid-month lowering below already-reserved
amounts; SQLite, where the guarantee degrades to per-session (documented,
tests pin the degraded behaviour).*

**The cost basis (`app.llm.bound.worst_case_cost`).** A call's worst case is a
pure function of the request body exactly as the transport sends it (the
adapters serialise once and send those bytes; a spy on the `httpx` request
proves the bound sees the same bytes), the model and the price schedule
(`llm_prices_usd_per_mtok`): input tokens are bounded by the UTF-8 byte count
of the whole body (a byte-level BPE tokenizer emits at most one token per byte)
plus two **stated conservative constants** (128 tokens per request, 16 per
message, `system` counted as a message), output tokens by the request's own
`max_tokens` / `max_completion_tokens`, and the cost is computed in exact
decimal arithmetic and **rounded up** at the ledger's 1e-6 (never below 1e-6).
Components covered: **input tokens and output tokens** at the schedule's
`[input, output]` USD per million tokens. Cache writes and reads, tool and
server-tool charges and thinking tokens are excluded **because the request
cannot carry them**: the function accepts only the keys and text-only message
content the adapters send, and any other key, content block or `cache_control`
makes the request *unbounded*. The bytes-per-token premise is a documented fact
for the public OpenAI tokenizers and a **coordinator engineering premise (lane brief 2026-10-27), not a provider document and not an operator ruling; open for the operator to accept or to replace with a provider-sourced figure** for Claude models, whose tokenizer is not public; the two overhead
constants are not provider figures. **Fail closed:** a model missing from the
price schedule, a model family whose premise is not accepted, a body that is not
one JSON object, an unknown parameter or content block, a missing output limit,
or a provider that cannot show its request bytes is refused before any call with
the distinct reason `cost_unbounded` (never a zero reservation); the pipeline
marks the output `budget_limited` as for any refused attempt. With the shipped
defaults (`claude-sonnet-4-6`, the classify prompt, `max_tokens` 1 024) one call
reserves about USD 0.02.

**Application guarantee versus the provider's invoice.** The ledger is a
ceiling on the *reserved bounds* the application computes from its own price
schedule. It is not the invoice: the provider bills by its own tokenizer, price
list and tiers. Expect the ledger to read at or above the invoice for metered
calls; reconcile `llm_budget_periods` and `llm_usage_events` against the provider
console monthly. If a provider ever reports usage above a call's bound (the
premise failed), the call is still charged **its bound** — `reserved_usd` never
exceeds the budget, an after-the-fact refusal cannot repair a budget already
exceeded, so the ledger must not be the thing that exceeds it — and the event is
logged at ERROR (`llm.cost_bound_violated`), counted
(`llm_cost_bound_violations_total`) and recorded on the period
(`bound_violations`), with the provider-reported usage kept in the usage row.
Any non-zero `bound_violations` means the premise or the price schedule must be
re-examined before the ledger is trusted for that model; the invoice may then
exceed the ledger by the excess.

**Uncertain outcomes are charged the worst case.** A timeout, a connection
failure *after the request was sent* (read, write or protocol error: the
provider may have processed it; only connect-phase errors, before any byte can
have left, are known-free), an undecodable reply and a reply with no usage block
settle at the call's bound (the undecodable-reply charge is a conservative basis
pending D-LLM-3). A call whose process died between reservation and settlement
keeps its worst case as the month's spend ("an unknown outcome is charged its
worst case"); the reservation row is its record. A rejected, rate-limited or
unavailable (4xx, 429, 5xx) attempt is charged 0 at settlement but has consumed
its bound at reservation, and the service's retries multiply that
((`llm_max_retries` + 1) × bound per logical call with no spend).

**Settlement after recovery.** If recovery releases a reservation before its own
process settles it (the expiry sweep, which never touches a reservation whose job
still holds a live lease, or a retried attempt's rebind), the worst case stands
as the charge and the late settlement still writes the call's usage row —
**exactly once per attempt**, however many processes or repeats settle late (a
conditional claim, proven on PostgreSQL with concurrent and repeated settlements
after `released` and after `expired`) — logged at WARNING
(`llm.settlement_after_release`) and counted
(`llm_settlements_after_release_total`). A settlement or recovery that finds its
workspace or organization deleted writes nothing: it logs and counts
(`llm_settlements_orphaned_total`) and never recreates a period, reservation or
usage row. The ledger therefore bounds what the product *starts*; the provider's
invoice remains the only statement of what was *billed*. The E6 probe and any call
made outside `app.llm.service` are not in the ledger.

Residual limitations of the ledger — the complete list (each is also stated
where it lives in the code, `app.llm.metering`, `app.llm.bound` and
`app.llm.budget`):
1. provider-side billing may differ from the price schedule; the bound's
   tokenizer premise (a coordinator engineering premise for Claude, not an operator ruling) and overhead constants are
   not provider figures; the ledger is a ceiling on reserved bounds, not an
   invoice (reconcile monthly);
2. calls outside the metered service (the E6 probe, anything not through
   `app.llm.service`) are not in the ledger;
3. a budget lowered mid-month below the amount already reserved is not
   retroactive: nothing is un-reserved, further reservations are refused;
4. SQLite keeps the per-session behaviour (two runs bound before either
   commits can together exceed the budget); the hard ceiling is PostgreSQL-only;
5. a reply whose usage exceeds its bound (premise failure) is charged the bound,
   not the usage: the ledger stays within the budget and `bound_violations`
   says the invoice may exceed it;
6. a transport failure *before* the request was sent (connect phase) is charged
   0; every failure after dispatch is charged the worst case;
7. if the reservation cannot be written (database unavailable) the attempt
   raises before any call: the job fails as transient and the worker retries
   it; the run's outputs are not marked `budget_limited`;
8. the recovery sweep runs on every worker poll (two indexed queries per
   poll, `app.jobs.worker.JobRunner.poll_once`);
9. an undecodable reply and a reply with no usage block are charged the worst
   case as a conservative basis pending D-LLM-3, not an accepted rule;
10. a reservation that recovery released before its own process settled it is
    charged its worst case; the late settlement still records the call's actual
    usage (one usage row, WARNING, metric) but the budget keeps the worst case;
11. the bound is conservative by design: its input part is about three times
    the earlier characters-per-token estimate (for the shipped defaults, whose
    output limit dominates, a call reserves about 25% more: USD 0.0213 against
    0.0171), so a budget admits fewer calls than the provider would bill for;
    tightening it needs a documented tokenizer.

Nothing here provisions, populates or rotates a key, and nothing here is an
approved quota or spending authority: the configured LLM budget and call-cap
values are engineering defaults (D-LLM-5 is undecided); the ledger makes the
configured budget a ceiling on reserved worst-case cost on PostgreSQL, not an
approval of any value.

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

There are two authored forms of this procedure. The **zero-handoff
noninteractive bootstrap** (section 6.1) is the **default and mandatory**
mechanism for the first creation of `<APP_ROLE>`: it is executed by a one-shot
ECS task with no human in the credential path. The **interactive
human-performed procedure** (section 6.2) is retained only as a fallback for
operator-performed work outside that task, and its literal-SQL prohibition is
unchanged.

### 6.1 Zero-handoff noninteractive bootstrap (default; module-driven)

**Rationale (why the interactive password-handoff design is prohibited as the
default):** any procedure in which an operator generates, reads, types,
copies, pastes, or otherwise transports the application password - even
transiently, even via `\password` - places the plaintext credential in a
human's hands and therefore inside some terminal, clipboard, prompt buffer, or
transcript. That violates the no-transcript / no-terminal secret rules
(section 10 leak-vector rules): the value must never exist in an
operator-visible surface at any moment. A credential no human ever sees cannot
be leaked by a human. The bootstrap task therefore generates and consumes the
password entirely inside its own process memory and hands the finished
`DATABASE_URL` straight to Secrets Manager.

**Executor:** the one-shot ECS task running module
`apps/api/app/db/bootstrap_app_role.py` (invoked as
`python -m app.db.bootstrap_app_role`), under a temporary task role and a
temporary task definition provisioned for this gate. **No ECS Exec** is used
or enabled at any point; there is no interactive shell into the task. The
temporary task role and task definition are **retired in a later gate** once
the bootstrap has succeeded - they are not standing infrastructure.

**What the task does (no operator sees or handles any credential):**

1. Reads only **non-secret** configuration from its environment (master and
   target secret identifiers by reference, DB host/port/name, expected account
   id and region, and an optional mode selector). No secret value and no ARN
   is hard-coded; nothing secret is printed.
2. Verifies identity (`sts:GetCallerIdentity`) against the expected account and
   region, and confirms the target `DATABASE_URL` container has **no**
   `AWSCURRENT` version (metadata-only `DescribeSecret`; default mode aborts if
   it is already populated).
3. Reads the RDS-managed **master** credential
   (`GetSecretValue` on the master secret only) into task memory at runtime,
   never to disk, argv, env, or logs.
4. Connects to the database as master over **TLS** (`sslmode=require`) and, as
   a session guard **before any credential statement**, disables statement
   logging for the session so no SQL text is ever written to the database log.
5. Generates the application password **inside the task** with
   `secrets.token_urlsafe(48)` - a fresh, high-entropy, URL-safe value that
   exists only in process memory.
6. Creates the role with a **restricted** attribute set:
   `LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS`, using
   a driver-bound password parameter (see the literal-SQL exception below).
   Transfers ownership of the application database to `<APP_ROLE>` and
   re-checks the metadata (all attribute flags false except `LOGIN`; database
   owner is `<APP_ROLE>`) **before** writing any secret.
7. Composes `DATABASE_URL` in memory in the section 2 format (username and
   password percent-encoded) and writes it with a single `PutSecretValue`
   **directly to the existing empty `DATABASE_URL` container**. The container
   is never created, deleted, or replicated here; the composed value never
   leaves the task except as that one write.
8. Performs a **mandatory fresh TLS authentication round trip**: opens a new,
   independent connection **as `<APP_ROLE>` using the newly written
   credential** (`sslmode=require`) and runs a trivial `SELECT 1`. Success is
   not declared until this round trip passes. It then closes all connections,
   drops its in-memory references to the password and URL, and discards its
   AWS clients.

**Modes.** The task defaults to **create-only**: it refuses to run if the role
already exists or the target container is already populated. A **recovery
mode** exists (it rotates an existing role's password via a driver-bound
`ALTER ROLE` parameter and rewrites the container) but is **never
auto-invoked**; selecting it requires **separate operational authorization**
and is the noninteractive path referenced by section 5.2.

**Fixed sanitized exit codes.** The task communicates outcome only through a
stable, unique, documented exit-code map (authored in the module docstring of
`apps/api/app/db/bootstrap_app_role.py`); it emits only fixed constant strings,
never a value, never an exception object, never a response body. The map:

| Code | Meaning |
| --- | --- |
| 0  | Success (role created/rotated, secret written, round trip verified) |
| 10 | Configuration invalid (missing/malformed environment; field name only) |
| 11 | Identity mismatch (account or region does not match expected) |
| 12 | Target not empty (`DATABASE_URL` container already has a version; default mode) |
| 13 | Role exists (`<APP_ROLE>` already present; default mode; report-only) |
| 14 | Master secret failure (access denied / not found / wrong JSON shape) |
| 15 | DB connect failure (master TLS connect or session-setup guard failed) |
| 16 | Role or ownership failure (create/alter, ownership transfer, or metadata check failed **before** any secret write) |
| 17 | Secret write failure (role committed but `PutSecretValue` failed/invalid) - **recovery required** |
| 18 | Verify failure (secret written but new-credential round trip failed) - **recovery required** |
| 19 | Cleanup failure (operation succeeded but a resource close failed) |
| 20 | Recovery precondition (recovery mode selected but role absent) |

Codes 17 and 18 mean the role change committed but the container or its
verification did not settle cleanly; both require the separately-authorized
recovery mode, not an ad-hoc manual fix.

**Approved literal-SQL exception (precisely scoped).** The general prohibition
on password-bearing SQL (sections 5.2 and 6.2) remains fully in force. The
single approved exception is a **driver-bound parameter**: inside this
controlled noninteractive process, the password is passed to the role
statement as a **client-side bound parameter** (psycopg `ClientCursor` `%s`
placeholder), which the driver escapes and quotes. This is permitted because
the value is **never rendered** into shell history, psql history,
operator-visible SQL, logs, argv, environment variables, task definitions, or
transcripts, and no human is present. **String interpolation of the password
into SQL (f-string, `format`, or concatenation) remains prohibited
everywhere**, including inside this task. Interactive **`\password`** remains
the rule for any human-performed operation (section 6.2).

### 6.2 Interactive human-performed procedure (fallback)

Retained for any operator-performed role work that is **not** run through the
bootstrap task of section 6.1. Executed once, at live-sequence step 2-3, under
that authorization:

1. Retrieve the master credential transiently from the RDS-managed secret
   (administrative access; interactive; never written to disk or history).
2. Connect with `PSQL_HISTORY=/dev/null psql "sslmode=require" ...` to the
   instance endpoint (psql history disabled for the whole session - psql's
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
   vector;` (requires master privileges). This remains **deferred** - the
   composed root deliberately omits `VECTOR_BACKEND` (application default
   `bruteforce`); run this step only when the pgvector bootstrap is explicitly
   authorized. Nothing in this tranche changes that posture.
5. Compose `DATABASE_URL` per section 2 and populate per section 3. Discard all
   transient credential material; the master credential remains only in its
   RDS-managed secret.
6. End state: master = administrative/incident-only; `<APP_ROLE>` = the sole
   application credential, rotatable per section 5.2.

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
