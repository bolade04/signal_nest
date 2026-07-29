# signalnest-revision-reader

A single-purpose program that reads the live Alembic revision from
`alembic_version` and prints it. One line, one SQL statement, nothing else.

**Status: authored, not provisioned.** No reader image has been published, no
reader task has ever run, and `enable_revision_reader` defaults to `false`.
Publishing, enabling and invoking are three separate later authorizations.

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
port pin above makes it far less consequential (a shadowed DSN can now only address port
5432 behind the RDS security group), but not irrelevant. It is written up in full, with
its compensating controls and evidence classification, in
`infra/aws/modules/revision_reader/README.md` §9. That one attacks the *verification*
rather than the container, so read it before treating a reader result as authoritative.

## Behaviour

Reads `DATABASE_URL` from the environment and treats it as the one hostile input,
because `containerOverrides.environment` is caller-settable at RunTask time. Before any
socket is opened it requires all of:

- a `postgresql`/`postgres` scheme, with SQLAlchemy's driver suffix stripped —
  `bootstrap_app_role.py` writes `postgresql+psycopg://…`, which libpq does not accept, so
  without this the reader could not address the live database at all;
- a host;
- **port 5432, or unspecified** (libpq's default). This is a security control, not tidiness.
  The task security group must permit egress to `0.0.0.0/0` on 443 for ECR, Secrets Manager
  and CloudWatch Logs, and PostgreSQL speaks on any port — so without the port pin a
  redirected DSN naming `attacker:443` would be reachable and could feed the reader a
  chosen revision with exit 0. Outbound 5432 reaches only the RDS security group, so the
  pin and the security group together close that path; neither does alone;
- `sslmode` present exactly once with an exact value of `require`, `verify-ca` or
  `verify-full`. Parsed, not substring-matched: `sslmode=requireXXX` contains
  `sslmode=require` and guarantees nothing;
- **no other query parameter, and no fragment.** A libpq URI honours connection
  *keywords* in its query string, so a port check that inspects only the positional slot
  is not enough — `?host=evil&port=443` would satisfy it while libpq connected elsewhere.
  `service=` and `passfile=` can pull a whole connection definition in from a file and
  `options=` can push server settings, so the query string is an allowlist of exactly
  `{sslmode}`. libpq's keyword/value form (`host=… port=…`, which carries no scheme) is
  refused too;
- **no `%` in the host.** `urlsplit` does not percent-decode the host but libpq decodes
  URI components, so `evil.invalid%2Cdb.invalid` reads here as one comma-free hostname and
  `db.invalid%3A443` as having no port — while libpq, decoding first, could see a
  multi-host list and a redirected port. Which side of the split it decodes on is not
  establishable offline, so the question is refused: a real RDS endpoint is plain ASCII and
  never needs an escape. Credentials are unaffected — they live in the authority but not in
  the host, so the secret's `quote(safe="")` encoding still passes;
- **no `,` in the authority.** libpq accepts multi-host URIs and tries each host in turn,
  but `urlsplit` knows nothing about that syntax: it reports the whole comma-joined string
  as the hostname and parses a port from after the *last* colon. So `evil,db:5432` reads
  as port 5432, and the portless `evil,db` reads as unspecified — both satisfying the port
  pin while libpq would try the attacker's host first. The port pin alone never closed
  this; rejecting `,` is what makes the single-destination guarantee total;
- **exactly one literal `@`.** Two parsers decide where userinfo ends by locating an `@`,
  and if this program and libpq ever picked a different one they would disagree about the
  host — the DSN would be validated against one destination and connected to another.
  Refusing the ambiguity removes the question rather than answering it, and cannot reject
  a legitimate DSN: `compose_database_url` percent-encodes both credentials with
  `quote(safe="")`, so a real `@` in a password arrives as `%40`.

Every rejection returns the same code and token, so a probing caller learns nothing about
which constraint they tripped.

It also scrubs every `PG*` variable before connecting — libpq reads `PGSSLMODE`,
`PGOPTIONS`, `PGHOST` and friends, and those are settable through the same override
channel; every connection parameter is passed explicitly instead.

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
| 51 | DSN refused (absent, bad scheme, no host, port != 5432, or TLS not required) |
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

`tests/test_reader.py` covers the program (argv rejection before any connect,
DSN admission — including the redirect DSNs themselves, each asserted refused with
**zero** connection attempts — read-only enforcement, the `PG*` scrub, exactly one SQL
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

The order below matters: the publisher role does not exist until the module is applied, and
the task definition does not exist until a digest is pinned. Each step's workflow fails
closed with a named variable if you arrive early.

1. **Apply the module** — set `enable_revision_reader = true` in the git-ignored
   `*.tfvars` (leave `revision_reader_image_digest` null for now), plan, review, apply.
   This creates the ECR repository, log group, security group and the three roles. No
   task definition yet, so nothing is invocable. Independent of `deploy_workload`.
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
4. **Pin the digest** — put the digest from the run's manifest artifact into
   `revision_reader_image_digest`, plan, review, apply. This registers the task
   definition, which is what gives the runner role a `RunTask` grant at all.
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
