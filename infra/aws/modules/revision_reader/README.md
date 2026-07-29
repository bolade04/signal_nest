# Module: `revision_reader`

## 1. Purpose
Owns the **dedicated live database revision-reader** capability: a private ECR
repository for a minimal reader image, a dedicated CloudWatch log group, an
egress-only security group, three purpose-built IAM roles, and a task definition
that runs the reader with no overrideable surface. This module defines resource
bodies but **applies nothing** — both lifecycle flags
(`publication_bootstrap_enabled`, `runtime_enabled`) default to `false`, no reader
image has been published, and no reader task has ever run.

The module has a **two-stage lifecycle** (Gate 4M). **Stage A — publication
bootstrap** (`publication_bootstrap_enabled`) creates only the ECR repository, its
lifecycle policy and the publisher OIDC role, so the image can be published. **Stage
B — runtime** (`runtime_enabled`) creates the log group, security group, the
reader→RDS ingress rule, the execution and runner roles, and the task definition.
Runtime **requires** publication bootstrap and a pinned image digest (enforced by
cross-variable `validation`), so the image is published before the execution role
that holds the `DATABASE_URL` secret grant exists. On teardown, disable `runtime`
before `publication_bootstrap`.

The reader verifies that the live schema is at this repository's Alembic code
head, which is a precondition of the workload apply.

## 2. Owned resources (implemented here)
**Stage A — publication bootstrap** (`publication_bootstrap_enabled`):
- `aws_ecr_repository` — private, **immutable** tags, scan-on-push, AES-256,
  `force_delete = false`.
- `aws_ecr_lifecycle_policy` — expire untagged reader images beyond the newest 10.
- publisher `aws_iam_role` + `aws_iam_role_policy` (only with an OIDC provider ARN).

**Stage B — runtime** (`runtime_enabled`):
- `aws_cloudwatch_log_group` — `/ecs/<prefix>-revision-reader`.
- `aws_security_group` + two `aws_vpc_security_group_egress_rule` — TCP 5432 to
  the RDS security group, TCP 443 for pull/secrets/logs. **No ingress rule on the
  reader SG**: nothing ever connects *to* the reader. No Redis egress.
- `aws_vpc_security_group_ingress_rule` — the reader→RDS `5432` ingress on the **RDS**
  SG, sourced from the reader SG (never a CIDR). Owned here rather than in `ecs`
  because it is runtime-gated; without it the stateful default-deny RDS SG would refuse
  the reader's connection.
- execution and runner `aws_iam_role` + `aws_iam_role_policy` (runner only with an
  OIDC provider ARN).
- `aws_ecs_task_definition` × 1 — created only when an image digest is pinned.

There is **no service**, no autoscaling, no alarm, no secret, no KMS key, no
task role, and no scheduled invocation. Nothing here starts a task.

## 3. Why this is not part of `registry`, `iam` or `ecs`
1. **Circularity.** The reader must be able to run *before* the workload plan it
   gates. It is gated by its own two lifecycle flags, never by `deploy_workload`, and
   consumes nothing that `deploy_workload` gates. It does consume
   `module.ecs.cluster_id`, which is fine: `aws_ecs_cluster` is ungated and
   exists in the foundation stage today.
2. **Blast radius.** A third entry in `registry`'s fixed two-repository map would
   feed `repository_arns`, which feeds **both** the shared execution role's pull
   grant and the `ci_publisher` push grant — silently widening two hardened
   identities with no diff in `iam/main.tf` at all. Keeping the reader
   self-contained means a defect in this new, less-exercised path cannot reach
   the api/worker repositories backing the currently-pinned staging digests.
   `registry/main.tf` and `iam/main.tf` are untouched.
3. **Lifecycle.** The module is coordinated for teardown as a unit and touches no
   `registry`, `iam` or `ecs` state — but it is **not self-destructing under the CI
   identities**. `aws_ecr_repository.reader` sets `force_delete = false` to protect
   published images, and both CI roles are explicitly denied `ecr:BatchDeleteImage`, so
   `tofu destroy` fails while any reader image remains in the repository. Destruction
   therefore requires a separate **administrative image-retirement step** (empty the
   repository) before OpenTofu can remove it.

## 4. The control this module rests on
ECS `ContainerOverride` exposes `{name, command, environment, environmentFiles,
cpu, memory, memoryReservation, resourceRequirements}` — it has **no
`entryPoint` member**. The image pins a fixed exec-form `ENTRYPOINT`; this task
definition sets **neither `entryPoint` nor `command`**, so nothing shadows it.
An override `command` therefore becomes argv to a program that rejects all argv.

What IAM can and cannot do here, re-derived rather than assumed: for an
`ecs:RunTask` holder, IAM constrains exactly three things — which
task-definition **revision** runs, role substitution (exact-ARN
`iam:PassRole`), and ECS Exec at launch (`ecs:enable-execute-command`). It
constrains **nothing** about override payload, environment, subnets,
`securityGroups` or `assignPublicIp`: no condition keys exist. That is precisely
why the primary control lives in the image and not in this module.

**Destination authenticity (Gate 4J.1) lives in the image for the same reason.** Since
network placement and `environment` are caller-supplied and un-constrainable by IAM,
*which* database the reader reads is decided by values BAKED into the digest-pinned
image — the expected host, database and role (from build args) plus `sslmode=verify-full`
against a committed CA bundle. The reader connects to the baked host and takes only the
password from the injected DSN. See §9.

## 5. Inputs
Note: the destination pins (`EXPECTED_DB_HOST`/`_NAME`/`_USER`) are **image build args**, not
module variables — they are supplied at publication from the protected
`staging-reader-publish` environment and baked into the digest-pinned image, so no RunTask
parameter can reach them. They deliberately do not appear in the table below.


| Name | Default | Notes |
| ---- | ------- | ----- |
| `publication_bootstrap_enabled` | `false` | Stage A: ECR repo + lifecycle + publisher role. Independent of `deploy_workload`. |
| `runtime_enabled` | `false` | Stage B: log group, SG, reader→RDS ingress, execution/runner roles, task def. Requires bootstrap + a pinned digest (cross-variable `validation`). |
| `name_prefix` | — | Deterministic staging prefix. |
| `aws_region` | — | Log configuration and `kms:ViaService`. |
| `vpc_id` | — | For the reader security group. |
| `rds_security_group_id` | — | The only data-plane egress target. |
| `database_url_secret_arn` | — | The only injected secret. See §8. |
| `secrets_kms_key_arn` | — | For `ViaService`-scoped `kms:Decrypt`. |
| `revision_reader_image_digest` | `null` | Validated `sha256:<64 hex>`. |
| `github_oidc_provider_arn` | `null` | Consumed, never created. Null leaves both CI roles uncreated. |
| `github_repository` | `bolade04/signal_nest` | Pinned in the trust subjects. |
| `ecs_cluster_arn` | — | IAM condition value only. |
| `log_retention_days`, `task_cpu`, `task_memory`, `tags` | 30 / 256 / 512 / `{}` | |

This module deliberately takes **no subnet input**. Network placement is supplied per call
in `RunTask`'s `networkConfiguration`, is not IAM-constrainable (no condition key exists
for subnets, security groups or `assignPublicIp`), and is therefore a property of the
invocation workflow rather than of this module. Accepting a subnet list here would have
read as ownership of something the module cannot enforce. The invocation workflow reads
the subnets from the root `private_subnet_ids` output via a protected-environment
variable.

## 6. Outputs
`repository_url`, `repository_arn`, `log_group_name`, `security_group_id`,
`execution_role_arn`, `publisher_role_arn`, `runner_role_arn`,
`task_definition_arn`, `task_definition_family`, `container_name`,
`publication_bootstrap_enabled`, `runtime_enabled`. No secret ARN, DSN or
credential is re-exported.

The resource-derived outputs follow the two-stage lifecycle: `repository_url`/
`repository_arn` are `null` unless Stage A is enabled; `log_group_name`/
`security_group_id`/`execution_role_arn`/`task_definition_arn` are `null` unless Stage B
is enabled. The two OIDC-role outputs carry an **additional** condition: `publisher_role_arn`
is `null` unless Stage A **and** a `github_oidc_provider_arn` are both present, and
`runner_role_arn` likewise requires Stage B **and** the provider ARN. Each is a `one()` over
its own count-gated resource, so no unsafe `[0]` index is ever taken. Four are **not**
null-gated: `task_definition_family`, `container_name`, `publication_bootstrap_enabled`
and `runtime_enabled` are computed from inputs and always return a value — the two
enablement flags returning `false` is the point of them. Saying "all outputs are null
when disabled" would have been an overclaim, and a consumer that branched on `== null`
to detect a disabled stage would branch wrongly.

`task_definition_arn` **includes the revision suffix** and the invocation
workflow must pass that exact value: the runner's `RunTask` grant is scoped to
that revision, so a family-only reference is denied rather than silently
running something else.

Five outputs are consumed by the two workflows via protected-environment variables:
`log_group_name`, `security_group_id`, `publisher_role_arn`, `runner_role_arn` and
`task_definition_arn`, plus `container_name`, which the run workflow defaults rather than
requires. The others — `repository_url`, `repository_arn`, `execution_role_arn`,
`task_definition_family`, `publication_bootstrap_enabled` and `runtime_enabled` — are
**diagnostic and consumed by nothing today**,
listed as such so nobody assumes they are wired. `repository_url` in particular is not
read by the publish workflow, which composes the repository path from the name prefix,
matching the convention `staging-publish.yml` already uses;
`task_definition_family` exists because CloudTrail records the short form
(`family:revision`) rather than the ARN, and will be needed by the separately authorized
override-audit gate.

## 7. Three identities, three jobs
- **Execution role** — `ecr:GetAuthorizationToken` (no resource scoping exists
  for it), pull **the reader repository only**, `GetSecretValue` on **exactly**
  the `DATABASE_URL` ARN, `kms:Decrypt` conditioned on `kms:ViaService`, and
  `CreateLogStream`/`PutLogEvents` on **exactly** the reader group. No
  `logs:CreateLogGroup`: this module creates the group, so granting creation
  would be unused privilege that also permits writing outside the audited group.
  Also an explicit **`Deny` on `s3:GetObject`** (Gate 4J.1): `environmentFiles` is a
  caller-supplied override that fetches from S3 using this role, so the Deny makes that
  channel's closure unconditional rather than relying on the absence of an allow.
- **Publisher role** — pushes the reader repository; explicitly denied ECS,
  `iam:PassRole`, secrets, KMS key administration and repository deletion.
- **Runner role** — `RunTask` on the **exact revision**, `DescribeTasks`
  (cluster-conditioned, because task ARNs are generated per run and cannot be
  pinned), `GetLogEvents` on **exactly** the reader group, and `PassRole` to
  exactly the execution role with a `NotResource` Deny behind it. Note
  `logs:DescribeLogStreams` is **not** granted: the workflow derives the stream
  name from the task ARN rather than listing, so enumeration is never needed and
  the grant would be unused privilege. Denied: all ECS
  writes, `StopTask`, ECS Exec at launch and connect, `TagResource`, secrets,
  identity writes, bulk log export — and `cloudtrail:LookupEvents`, because a
  principal that reads the audit record of its own calls is not audited by it.
  `ecs:ListTasks` is **omitted**: the workflow addresses the task by the ARN
  `RunTask` returned, so enumeration is never needed.

**There is no task role.** Not an empty one — none. The reader makes no AWS API
call, and omitting the role entirely is what keeps the runner's `PassRole` list
to a single ARN.

The publisher and runner pin **different** OIDC subject claims
(`environment:staging-reader-publish` / `environment:staging-reader-run`) with
`StringEquals` on both `aud` and `sub`. Identical subjects would make the
publish/invoke split cosmetic.

## 8. Honest limit on "read-only"
The injected `DATABASE_URL` is the application role, and `bootstrap_app_role.py`
issues `ALTER DATABASE … OWNER TO` — so that role **owns the database** and can
write and perform DDL. Everything in this module and image bounds what *code*
exists and what can *run*; none of it bounds the *credential*. "Read-only" is a
property of the reader's behaviour, not of the identity it connects as. A
dedicated PostgreSQL role with `SELECT` on `alembic_version` and nothing else,
delivered as its own secret, is the only unconditional control and requires
separate authorization.

## 9. Can a RunTask holder re-point the reader at another database?

This is the attack that matters most, because it defeats the *verification* rather than
the container: a reader pointed at a database the caller controls would report whatever
revision that database contains, with exit 0, without ever engaging the entrypoint
hardening.

**Why the network cannot be the anchor.** `ContainerOverride.environment` is caller-settable
and IAM has no condition key for it, so `DATABASE_URL` must be treated as hostile. The
network placement is caller-supplied too: subnets, `securityGroups` and `assignPublicIp`
are members of the RunTask `networkConfiguration`, and **IAM has no condition key for any of
them**. So the reader's own security group — outbound 5432 only to the RDS SG — is a
property of `reader-run.yml`, **not** of authorization: a RunTask holder can attach a
different security group (including the unmanaged VPC default SG, which this module does not
manage and which permits broad egress). An earlier design tried to close the redirect with a
DSN port pin composed with that security group; that composition is **false against the very
actor it names**, because the actor supplies the security group in the same call. Do not
reintroduce "the port pin composes with the SG to close the path" language — it was wrong.

**How it is actually closed (Gate 4J.1): baked destination + verify-full.** The host,
database name and role are **baked into the image** at build time (`revision_reader/_pinned`,
generated from the `EXPECTED_DB_HOST`/`_NAME`/`_USER` build args the publish workflow
supplies from the protected `staging-reader-publish` environment). A baked source constant is
the only trust anchor no RunTask parameter can reach: `ContainerOverride` has no member that
rewrites image contents, `environment` overrides touch only `os.environ` (which the reader
does **not** read these from), and an image `ENV` value *would* be overridable — a source
constant is not. The reader connects with discrete `psycopg` keyword arguments to the **baked
host**, and takes exactly one value from the DSN: the password. The authority is never parsed
for a destination nor handed to libpq, which structurally eliminates the whole class of
`urlsplit`-vs-libpq parser-divergence bugs (including the bracketed-authority bypass).

Because the AWS RDS CA signs **every** customer's instance, TLS alone would only prove "some
RDS server". So the reader also sets `sslmode=verify-full` with `sslrootcert` pointing at a
committed, checksum-pinned AWS RDS CA bundle baked at `/etc/ssl/rds/rds-global-bundle.pem`
(carried forward from the builder after a build-time `sha256sum -c`, so the image bytes equal
the reviewed bytes). The baked host says *which* server; verify-full proves the server owns
that name. The decoded password is gated to printable ASCII (`[\x21-\x7e]`), because a
percent-encoded NUL is invisible to a raw-string check but truncates libpq's conninfo at the
C boundary — silently dropping `sslmode`/`sslrootcert`. The query is schema-qualified to
`public.alembic_version` so a shadowed role's `search_path` cannot select a different schema.
A tamper detector additionally fails closed (exact-ASCII) if the DSN names a host other than
the baked one — evidence quality, not the control.

`tests/test_reader.py` pins all of this against the built artefact's own interpreter in the
CI in-image band, and `apps/revision-reader` carries two independent DSN attack corpora
(security-lane and adversarial-lane, asserted disjoint).

**What remains unresolved / residual, honestly.**
- **Credential scope (unchanged).** The injected identity still OWNS the database. "Read-only"
  is a property of the reader's behaviour, not the credential. A dedicated `SELECT`-only
  PostgreSQL role delivered as its own secret is the only unconditional fix and requires
  separate authorization; it is **not** provisioned here.
- **Environment-shadows-secrets (moot by design).** Whether `containerOverrides.environment`
  can shadow a `secrets`-sourced variable is still offline-unverifiable — but the baked-host
  design **does not depend on the answer**: even a fully attacker-controlled `DATABASE_URL`
  cannot change host/db/role, so the worst outcome is a failed authentication, never a false
  head at exit 0.
- **CA authenticity vs byte-stability.** The pinned SHA-256 proves the bundle has not changed
  since it was committed, not that it is genuinely AWS's — that provenance is a one-time
  manual review of the committed asset against AWS's published truststore.
- **Environment coupling.** Baking host/db/role makes the reader image environment-specific:
  one digest per endpoint, and an RDS rename/restore/blue-green cutover requires republishing
  the reader before it can verify anything.

Ranked compensating controls (defence in depth, not the primary boundary):
1. `ecs:RunTask` on this revision is held by **one** identity, assumable only by a job
   declaring the `staging-reader-run` environment. No human permission set holds it.
2. `.github/workflows/reader-run.yml` sends no `--overrides` at all and is reviewed
   through the same path as this module; it asserts positively on the whole `overrides`
   object in the RunTask response. This is detection by the calling principal — meaningful
   only because control 1 makes it the sole caller; it is **not** a boundary against a caller
   who already holds RunTask. The baked-destination control above is what holds against that
   caller.

## 10. Fail-closed posture
- Both flags `false` (the default) creates nothing at all, including no reader→RDS
  ingress rule.
- `runtime_enabled = true` is **rejected at plan time** unless
  `publication_bootstrap_enabled = true` and a non-null image digest are both present
  (cross-variable `validation`), so runtime can never reference the bootstrap-owned ECR
  repository that was never created.
- With no image digest there is **no task definition**, therefore no `RunTask`
  grant on the runner role, therefore nothing invocable. The role can exist and
  do nothing.
- Supplying a digest while disabled yields `null`, not a plan error.

## 11. Tests
`reader_contract.tftest.hcl` — offline, fully mocked provider, run by the
`revision-reader` CI job. It pins every absence (no `entryPoint`, no `command`,
no task role, one secret, no `CreateLogGroup`, no publisher `RunTask`) and every
exact scope, because absences do not fail loudly when they stop holding.

## 12. Status
Authored and offline-validated. `tofu fmt`, `tofu validate` and `tofu test` pass.
**Nothing is provisioned.** Applying Stage A (publication bootstrap), publishing a
reader image, applying Stage B (runtime, which needs the pinned digest), and invoking
the reader are separate later authorizations.
