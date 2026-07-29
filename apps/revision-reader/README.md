# signalnest-revision-reader

A single-purpose program that reads the live Alembic revision from
`alembic_version` and prints it. One line, one SQL statement, nothing else.

**Status: authored, not provisioned.** No reader image has been published, no
reader task has ever run, and both lifecycle flags
(`enable_revision_reader_publication_bootstrap`, `enable_revision_reader_runtime`)
default to `false`. Applying publication bootstrap, publishing the image, applying
runtime (which requires a pinned digest), and invoking are separate later
authorizations.

## Why this is a separate artefact

The workload apply depends on the live schema being at this repository's code
head. Verifying that requires *reading* the database — but the obvious way to
do it (run the worker image with a task-definition `command` that reads the
revision) is not safe:

- the worker image has **no `ENTRYPOINT`**, so the command is fully replaceable;
- it contains a **shell**;
- it contains **`app.db.migrate`**, which can upgrade *and* downgrade;
- it receives the application **`DATABASE_URL`**.

An ECS `RunTask` holder can supply `overrides.containerOverrides[].command`.
IAM has **no condition key** for override contents, environment, subnets,
security groups or `assignPublicIp` — it constrains exactly three things: which
task-definition *revision* runs, role substitution (exact-ARN `iam:PassRole`),
and ECS Exec at launch (`ecs:enable-execute-command`). So with that image the
task definition's `command` is documentation, and override defence is
*detection after the fact*.

This image changes that. `ContainerOverride` exposes `{name, command,
environment, environmentFiles, cpu, memory, memoryReservation,
resourceRequirements}` — **it has no `entryPoint` member**. The image pins a
fixed exec-form `ENTRYPOINT` and the task definition sets neither `entryPoint`
nor `command`, so an override `command` arrives as **argv** to a program whose
first action is to reject all argv. Prevention, not detection.

The entrypoint is `["/usr/bin/python3.11", "-m", "revision_reader.reader"]`.
The `-m` form is deliberate: CPython stops parsing interpreter options at
`-m <module>`, so an override of `["-c", "..."]` arrives as argv to be rejected
rather than as a flag that would execute arbitrary code.

Shell absence is **defence in depth, not the control**. With the entrypoint
pinned the shell is already unreachable; describing the image as safe *because*
it is shell-free would break silently if the base image were ever swapped.

## What it deliberately does not contain

`alembic`, `sqlalchemy`, `pydantic`, `boto3`, and the `app` package itself. The
absence of Alembic *is* the control — and note that `alembic` is a **base**
dependency of `apps/api` (not an extra), so any install of that package would
restore migration capability. That is why this is a separate distribution and
why the image never installs the application package.

It also shares no code with `apps/api`. An instrument that shares code with the
thing it measures is not an independent check: a defect in the shared module
would corrupt the measurement and the measured object identically.

## Honest limit — read the whole of this section

These controls bound what **code** exists and what can **run**. They do not
bound the **credential**.

The injected `DATABASE_URL` is the application role, and
`bootstrap_app_role.py` issues `ALTER DATABASE … OWNER TO` — so that role owns
the database and can write and perform DDL. "Read-only" describes this
program's *behaviour* (it opens a read-only transaction, sets
`default_transaction_read_only=on` server-side, and issues exactly one
`SELECT`), never the identity it connects as.

A dedicated PostgreSQL role with `SELECT` on `alembic_version` and nothing
else, delivered as its own secret, is the only unconditional control. It
requires separate authorization and does not exist yet. Until it does, do not
describe the reader as "read-only" without that qualification.

A related question — whether a `containerOverrides` `environment` entry can *shadow* the
`secrets`-injected `DATABASE_URL` — is **unverified** and cannot be settled offline. The
Gate 4J.1 design makes the answer **not matter**: the destination is baked into the image,
so even a fully attacker-controlled `DATABASE_URL` cannot change which server/database/role
is read. It is written up in full, with its compensating controls and evidence
classification, in `infra/aws/modules/revision_reader/README.md` §9. That attack targets the
*verification* rather than the container, so read it before treating a result as authoritative.

## Behaviour

The destination is **baked into the image**, not taken from the DSN. Host, database name and
role are generated into `revision_reader/_pinned.py` at build time from the
`EXPECTED_DB_HOST`/`_NAME`/`_USER` build args (supplied from the protected
`staging-reader-publish` environment), and the AWS RDS CA bundle is committed and baked at
`/etc/ssl/rds/rds-global-bundle.pem`. A baked source constant is the only anchor no RunTask
parameter can reach — an image `ENV` value would be overridable via
`containerOverrides.environment`, a source constant is not.

As its **first** action the reader scrubs every `PG*` variable **and `HOME`** (libpq reads
`PGSSLMODE`, `PGSSLROOTCERT`, `PGHOST`, `PGSERVICE`, `PGSERVICEFILE`, `PGPASSFILE`, … and its
default `sslrootcert`/`passfile` live under `~`), all settable through the override channel.

It then connects with **discrete** `psycopg` keyword arguments — never a DSN string — to:

- `host` = the **baked** expected host (the DSN's host is never used to connect);
- `port` = 5432, `dbname`/`user` = the baked values;
- `sslmode="verify-full"` with `sslrootcert` = the baked CA path. The AWS RDS CA signs every
  customer's instance, so verify-full alone proves only "some RDS server" — the baked host is
  what says "ours", and the two together authenticate the intended server;
- `password` = **the only value taken from the DSN**. It is percent-**decoded** and then
  gated to printable ASCII (`[\x21-\x7e]`, ≤256): a percent-encoded NUL is invisible to a
  raw-string check but truncates libpq's conninfo at the C boundary, silently dropping later
  parameters including `sslmode`/`sslrootcert`. Gating the decoded value closes that.

Because the authority is never parsed for a destination nor handed to libpq, the entire class
of `urlsplit`-vs-libpq parser-divergence bugs (bracketed authority, multi-host, `%`-encoded
delimiters, `@` ambiguity, keyword smuggling) is structurally eliminated. The reader still
requires a `postgresql`/`postgres` scheme, exactly one `@`, and rejects bracketed authorities
before extracting the password. The query is schema-qualified to `public.alembic_version` so a
shadowed role's `search_path` cannot select a different schema. A tamper detector additionally
fails closed (exact ASCII) if the DSN names a host other than the baked one — evidence quality,
not the control. Every rejection returns the same code and token, so a probing caller learns
nothing about which constraint they tripped.

Prints exactly one line — the revision — to stdout on success. On any failure
it prints one fixed classification token to stderr: never a driver message,
never a DSN, never a traceback.

The expected head **never enters the container**. Comparison happens outside,
in `.github/workflows/reader-run.yml`, via `python -m app.db.revision_compare`
on the runner. A reader that knew the expected answer could be argued into
agreeing with itself.

### Exit codes

A disjoint band, so a reader failure can never be confused with a migration
tool's (`migrate` 0–7, `bootstrap` 10–20, `revision_status` 30–36,
`revision_compare` 40–44).

| Code | Meaning |
| ---- | ------- |
| 0  | Success; one revision printed to stdout |
| 50 | **argv rejected** — an override `command` was supplied |
| 51 | Config refused (DSN absent/bad scheme/bad password/host-tamper, or unbaked/missing pins/CA) |
| 52 | Connection failed |
| 53 | `alembic_version` table missing |
| 54 | No revision rows |
| 55 | Multiple revision rows |
| 56 | Malformed revision (not 12 lowercase hex) |
| 57 | Unexpected failure |

Exit 50 has its own code rather than being folded into 51 precisely so that an
attempted override is a distinct, greppable fingerprint.

## Tests

```bash
python -m pytest apps/revision-reader/tests -q
```

`tests/test_reader.py` covers the program (argv rejection before any connect, the
discrete-parameter contract — the raw DSN is never forwarded and host/db/role are the baked
constants — the decoded-password gate, the confirmed redirect/bracket exploits each refused
with **zero** connection attempts, and two independent (security-lane and adversarial-lane)
DSN attack corpora asserted disjoint — read-only enforcement, the `PG*`+`HOME` scrub, one SQL
statement, the frozen token set, the disjoint exit band, and an AST scan proving no
forbidden import).
`tests/test_dockerfile.py` covers the build contract — entrypoint form, empty
`CMD`, non-root uid, and the interpreter-minor triple (builder, entrypoint,
`PYTHONPATH`), which exists because a `python:3.12-slim` builder against a
Python-3.11 distroless base produced an image that built cleanly and could not
import `psycopg`.

`infra/aws/modules/revision_reader/reader_contract.tftest.hcl` covers the IaC
half offline with a fully mocked provider, and the `revision-reader` CI job
replays the whole in-image band against a real build on every PR.

## Operator runbook

### Preconditions (not performed by this runbook, and not implied by its ordering)

Both must already hold before step 6. Neither is created by this module, neither is
checked by the IaC, and the six-step sequence below cannot imply them because the operator
does not perform them here:

- **The `DATABASE_URL` secret is populated** — the module creates no secret value; the
  container is created by `secrets` and populated by INFRA-6 / `bootstrap_app_role`.
- **The RDS instance exists, is bootstrapped, and is reachable** from the reader's subnets.

Absent either, the task fails **before application code runs** — `ResourceInitializationError`,
`STOPPED` with no container exit code — and `reader-run.yml` classifies that BLOCKED, never
PASS. That is the same distinction exit 51-vs-52 draws inside the reader: it tells you the
failure is a missing prerequisite, not a reader defect. Check both before concluding
anything from a BLOCKED run.

### Sequence

The order below matters: the publisher role does not exist until **Stage A** is applied, and
the task definition (and the execution/runner roles, log group, security group and reader→RDS
ingress) does not exist until **Stage B** is applied with a pinned digest. Each step's
workflow fails closed with a named variable if you arrive early.

1. **Apply Stage A (publication bootstrap)** — set
   `enable_revision_reader_publication_bootstrap = true` in the git-ignored `*.tfvars`
   (leave `enable_revision_reader_runtime = false` and `revision_reader_image_digest` null
   for now), plan, review, apply. This creates **only** the ECR repository, its lifecycle
   policy and the publisher role — deliberately no execution role and therefore no
   `DATABASE_URL` secret grant yet, so the image is published against a stage that cannot
   reach the database. Independent of `deploy_workload`.
2. **Set the publish environment's variables** on `staging-reader-publish`:

   | Variable | Source |
   | --- | --- |
   | `AWS_STAGING_READER_PUBLISH_ROLE_ARN` | output `revision_reader_publisher_role_arn` |
   | `AWS_STAGING_ACCOUNT_ID` | operator-held; shared with the existing `staging` environment |
   | `AWS_STAGING_NAME_PREFIX` | optional; defaults to `signalnest-staging` |

3. **Publish** — run `reader-publish.yml` (manual dispatch, `main` only, protected
   environment). It verifies the built image *before* any credential exists in the job,
   then pushes and reads the registry digest back. Publishing is inert: the image does
   nothing until its digest is pinned and a run is invoked.
4. **Apply Stage B (runtime) with the pinned digest** — put the digest from the run's
   manifest artifact into `revision_reader_image_digest` **and** set
   `enable_revision_reader_runtime = true` (keeping `..._publication_bootstrap = true`),
   plan, review, apply. Runtime with no bootstrap or no digest is rejected at plan time by a
   cross-variable `validation`. This creates the log group, security group, the reader→RDS
   `5432` ingress rule, the execution and runner roles, and registers the task definition —
   which is what gives the runner role a `RunTask` grant at all. On teardown, set
   `..._runtime = false` (removing these) **before** `..._publication_bootstrap = false`.
5. **Set the run environment's variables** on `staging-reader-run`:

   | Variable | Source |
   | --- | --- |
   | `AWS_STAGING_READER_RUN_ROLE_ARN` | output `revision_reader_runner_role_arn` |
   | `AWS_STAGING_READER_TASK_DEFINITION_ARN` | output `revision_reader_task_definition_arn` — the **full ARN including the revision suffix**; a family-only value is denied by IAM |
   | `AWS_STAGING_READER_LOG_GROUP` | output `revision_reader_log_group_name` |
   | `AWS_STAGING_READER_SECURITY_GROUP_ID` | output `revision_reader_security_group_id` |
   | `AWS_STAGING_CLUSTER_ARN` | output `ecs_cluster_id` |
   | `AWS_STAGING_PRIVATE_SUBNET_IDS` | output `private_subnet_ids`, as **bare comma-separated ids** (`subnet-aaa,subnet-bbb`) — the workflow interpolates it into `subnets=[…]` CLI shorthand, so pasting the rendered HCL list breaks the call |
   | `AWS_STAGING_ACCOUNT_ID` | operator-held |
   | `AWS_STAGING_READER_CONTAINER_NAME` | optional; defaults to `revision-reader`, matching output `revision_reader_container_name` |

   Re-pinning a new digest registers a **new revision**, so step 5's task-definition ARN
   must be updated with it — otherwise the next run is denied rather than silently
   running the old image.
6. **Invoke** — run `reader-run.yml`. It takes no inputs.

If the task hangs, the job fails on the wait and the task is left to its own
watchdog: the runner role deliberately holds no `ecs:StopTask`, because that
permission could only be cluster-scoped and would also permit stopping the API
and worker service tasks.

A `PENDING` result is never a `PASS`. An absent container exit code, an empty
log stream, or output that is not a well-formed revision all fail the job as
**BLOCKED**; there is no path from "could not establish" to "verified".
