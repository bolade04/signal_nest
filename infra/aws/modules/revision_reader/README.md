# Module: `revision_reader`

## 1. Purpose
Owns the **dedicated live database revision-reader** capability: a private ECR
repository for a minimal reader image, a dedicated CloudWatch log group, an
egress-only security group, three purpose-built IAM roles, and a task definition
that runs the reader with no overrideable surface. This module defines resource
bodies but **applies nothing** — `enabled` defaults to `false`, no reader image
has been published, and no reader task has ever run.

The reader verifies that the live schema is at this repository's Alembic code
head, which is a precondition of the workload apply.

## 2. Owned resources (implemented here)
- `aws_ecr_repository` — private, **immutable** tags, scan-on-push, AES-256.
- `aws_ecr_lifecycle_policy` — expire untagged reader images beyond the newest 10.
- `aws_cloudwatch_log_group` — `/ecs/<prefix>-revision-reader`.
- `aws_security_group` + two `aws_vpc_security_group_egress_rule` — TCP 5432 to
  the RDS security group, TCP 443 for pull/secrets/logs. **No ingress rule
  exists**: nothing ever connects *to* the reader. No Redis egress.
- `aws_iam_role` × 3 + `aws_iam_role_policy` × 3 — execution, publisher, runner.
- `aws_ecs_task_definition` × 1 — created only when an image digest is pinned.

There is **no service**, no autoscaling, no alarm, no secret, no KMS key, no
task role, and no scheduled invocation. Nothing here starts a task.

## 3. Why this is not part of `registry`, `iam` or `ecs`
1. **Circularity.** The reader must be able to run *before* the workload plan it
   gates. It is gated by its own `enabled` flag, never by `deploy_workload`, and
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
3. **Lifecycle.** Everything here is destroyable as a unit without touching
   `registry`, `iam` or `ecs` state.

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

## 5. Inputs
| Name | Default | Notes |
| ---- | ------- | ----- |
| `enabled` | `false` | Independent of `deploy_workload` by design. |
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
`task_definition_arn`, `task_definition_family`, `container_name`, `enabled`.
No secret ARN, DSN or credential is re-exported.

The resource-derived outputs are `null` when the module is disabled. Three are **not**:
`task_definition_family`, `container_name` and `enabled` are computed from inputs and
always return a value — `enabled` returning `false` is the point of it. Saying "all
outputs are null when disabled" would have been an overclaim, and a consumer that
branched on `== null` to detect a disabled module would branch wrongly.

`task_definition_arn` **includes the revision suffix** and the invocation
workflow must pass that exact value: the runner's `RunTask` grant is scoped to
that revision, so a family-only reference is denied rather than silently
running something else.

Five outputs are consumed by the two workflows via protected-environment variables:
`log_group_name`, `security_group_id`, `publisher_role_arn`, `runner_role_arn` and
`task_definition_arn`, plus `container_name`, which the run workflow defaults rather than
requires. The other five — `repository_url`, `repository_arn`, `execution_role_arn`,
`task_definition_family` and `enabled` — are **diagnostic and consumed by nothing today**,
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
hardening. The security lane raised it as blocking and was right to.

**The chain, as it stood.** `ContainerOverride.environment` is caller-settable and IAM has
no condition key for it. The reader validated only that a DSN was present and contained an
`sslmode` substring — no host or port constraint. This module's own
`aws_vpc_security_group_egress_rule.reader_https` permits egress to `0.0.0.0/0` on **443**,
which is unavoidable without VPC interface endpoints (ECR, Secrets Manager and CloudWatch
Logs have no managed prefix list). PostgreSQL's wire protocol does not care which port it
runs on. So a DSN naming `attacker-host:443` was reachable, and anything answering a
minimal handshake plus one query could dictate the result.

**How it is closed.** The reader now pins the DSN port to 5432 (or unspecified, which
libpq resolves to 5432). Outbound 5432 is permitted **only** to the RDS security group, so
a port-constrained DSN can reach nothing but the intended database. The two halves compose:
neither the port pin nor the security group closes the path alone.

That control deliberately lives **in the image**, which is digest-pinned and has no
override channel, precisely because the environment does. It is enforced before any socket
is opened, and `tests/test_reader.py` pins it with the attack DSNs themselves
(`evil.invalid:443`, `:80`, `:6379`), each asserted to be refused with **zero** connection
attempts. Both in-image bands replay it against the built artefact.

Pinning the port is not sufficient on its own, and assuming it was would have left the
hole open. A libpq URI honours connection **keywords in its query string**, so
`?host=evil&port=443` passes a positional port check while libpq connects elsewhere; and
libpq accepts multi-host URIs, which it tries in order. The DSN check therefore allowlists
the query string to exactly `{sslmode}` — which also excludes `service=`/`passfile=`
(a whole connection definition from a file) and `options=` (server settings, including
clearing the read-only default) — rejects a fragment, and rejects any DSN whose port slot
does not parse cleanly. Each of those forms is a named test case.

Multi-host URIs needed their own rule rather than falling out of the port check, and the
distinction is worth recording because it was the fourth hole found in this one function.
libpq tries each host of a multi-host URI in turn; `urlsplit` does not model that syntax at
all. It returns the whole comma-joined string as the hostname and takes everything after
the **first** colon as the port. `evil,db:5432` therefore reads as a clean port 5432 and
the portless `evil,db` reads as unspecified — both satisfying the port pin while libpq
would try the attacker's host first. Only the two-colon shape was caught, and only because
its port string `443,db:5432` fails `int()` — an accident rather than a guard. The authority now
rejects `,` outright, which is what makes the single-destination guarantee total.

It also requires exactly one literal `@` in the authority. Two parsers locate the end of
userinfo by finding an `@`, and a disagreement about *which* one would mean the DSN was
validated against one host and connected to another. That ambiguity is refused rather than
adjudicated — it cannot occur in a legitimate DSN, because `compose_database_url`
percent-encodes both credentials, so a real `@` arrives as `%40`. Removing the question is
worth more than winning the argument about whose parsing convention prevails.

The same pass replaced the `sslmode` substring check with a parsed exact-value check —
`sslmode=requireXXX` contains `sslmode=require` and guaranteed nothing — and made the
reader normalise the `postgresql+psycopg://` scheme that `bootstrap_app_role.py` actually
writes, which libpq does not accept. Without that second fix the reader could not have
connected to the live database at all.

**What remains unresolved, honestly.** Whether ECS lets a `containerOverrides.environment`
entry *shadow* a `secrets`-sourced variable of the same name is **unverified** and cannot
be established offline. `ContainerOverride` has no `secrets` member, so the binding itself
cannot be re-pointed (AWS containers-roadmap issue 1269 requests exactly that capability,
which is evidence it does not exist) — classification: **structurally derived**, not
directly observed. The port pin makes the answer far less important, since a shadowed
DATABASE_URL can now only address port 5432 behind the RDS security group, but it does not
make it irrelevant: a caller able to reach the real database could still substitute
credentials for it. Resolve it before treating a reader result as authoritative under a
threat model that includes a compromised runner role.

Ranked compensating controls, unchanged:
1. `ecs:RunTask` on this revision is held by **one** identity, assumable only by a job
   declaring the `staging-reader-run` environment. No human permission set holds it.
2. `.github/workflows/reader-run.yml` sends no `--overrides` at all and is reviewed
   through the same path as this module.
3. That workflow asserts positively on the **whole** `overrides` object in the RunTask
   response, so an `environment` override is detected rather than merely disallowed.

Control 3 is detection by the same principal that made the call. It is meaningful only
because control 1 makes that principal the sole caller; do not present it as a boundary
against a caller who already holds RunTask.

## 10. Fail-closed posture
- `enabled = false` (the default) creates nothing at all.
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
**Nothing is provisioned.** Enabling this module, publishing a reader image, and
invoking the reader are three separate later authorizations.
