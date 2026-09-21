# Database Migrations (Phase 3A.4b Batch 4)

SignalNest uses **Alembic** as the single, authoritative schema path in every
mode. Application code never calls `create_all()` at startup, and **no API or
worker replica migrates the database**. Schema changes are applied by exactly one
actor; every replica only *verifies* compatibility when it starts.

## Single-actor migration model

In a multi-replica deployment, letting each process run `alembic upgrade` would
race N writers against one schema. Instead:

* **One migration actor** runs migrations as a discrete step, before (or as part
  of) a rollout — a one-shot job, not a long-lived service.
* **Replicas verify, never mutate.** Each API/worker process runs a
  schema-compatibility check at startup (`app/db/schema.py`) and refuses to start
  if the schema is behind the code. It never applies DDL.

The migration actor is the same code and image as the API/worker; it simply runs
a different command.

### The migration command

```bash
python -m app.db.migrate            # upgrade to head (default)
python -m app.db.migrate upgrade    # explicit upgrade to head
python -m app.db.migrate check      # verify compatibility, mutate nothing
python -m app.db.migrate downgrade-confirmation <target>   # print the confirmation
python -m app.db.migrate downgrade <target> --confirm <TOKEN>   # guarded downgrade
```

* The **bare invocation** (no subcommand) is the fail-closed staging path and
  the exact shape the ECS one-shot migration task runs: upgrade to the single
  code head, read the database revision back, and exit `0` only on an exact
  match (exit codes `3`–`7` classify every failure band).
* `upgrade` applies every pending migration up to the current head. This is the
  **only** write path and must be run by a single actor. A driver/Alembic
  failure exits `4` with a fixed classification — never a re-raised traceback.
* `check` reports the schema state and exits `0` when the schema is startup-safe,
  `1` otherwise (including when the schema state cannot be read at all — a
  connection failure is contained to a fixed `migrate.check.unverifiable`
  event). It performs no DDL and is safe to run anywhere.
* `downgrade` requires an explicit target revision — a bare `head` is rejected —
  so a downgrade is always a deliberate, named step. Failures exit `4` with a
  fixed classification.
* `downgrade` additionally requires a **confirmation bound to this exact
  database and transition**. There is no `--force`, no `--yes` and no
  environment variable: a generic flag confirms intent to downgrade *something*
  and says nothing about *where*, which is the failure this control exists to
  prevent. Obtain the confirmation with `downgrade-confirmation`, which reads
  the live revision and writes nothing:

  ```bash
  TOKEN="$(python -m app.db.migrate downgrade-confirmation -1)"
  python -m app.db.migrate downgrade -1 --confirm "$TOKEN"
  ```

  `downgrade-confirmation` prints the bare token on **stdout** (so `$(...)` is
  safe) and a `connected database :` line naming the server that actually
  answered on **stderr**. Read that line **before** you run the downgrade —
  minting is the only moment at which it can stop you. A successful downgrade
  never prints it again; a refusal reprints the same line.

  If the mint fails it exits `4` and prints nothing on stdout, so
  `TOKEN="$(...)"` silently yields an empty string and the downgrade then looks
  like a confirmation mismatch rather than a failed mint. Check the exit status,
  or test that the token is 16 hex characters, before running the downgrade.

  The same confirmation works through the direct CLI
  (`alembic -x confirm=<TOKEN> downgrade <target>`, where `-x` is a global
  option and must precede the subcommand), through
  `npm run migrate:down -- <target> <TOKEN>`, and through
  `command.downgrade(cfg, target)` with `cfg.attributes["confirm"]`. All of
  these enforce the same value; none of them has its own semantics.

  A downgrade target must be `-1` (or `-01`), `base`, or an explicit revision
  id (a unique prefix of a revision id also works, and the literal text you
  typed is what gets bound — so mint and run must use the same spelling).
  Other relative forms (`-2`, `-0`, `-001`, `<rev>-1`, `head-1`) are refused.
  To go back more than one revision, either repeat `-1` and mint a fresh
  confirmation for each step, or name the ancestor revision explicitly and
  confirm the whole chain at once — run the downgrade unconfirmed once to read
  the `would revert : N migration(s)` count, then mint.

  > **The CI one-liner is not an operator procedure.**
  > `.github/workflows/ci.yml` mints and consumes the token in a single
  > `$(...)`. That is correct there — the database was created moments earlier
  > in the same job and is thrown away after it. Copying that idiom to a real
  > database removes the human from the loop entirely: nobody ever reads the
  > `connected database :` line, which is the only check that can catch a
  > tunnel or a stale `.env`. Mint, read, then run. Two steps, deliberately.

  The confirmation stops matching when the target database, the server that
  actually answered when it was minted, the revision it is on, the requested
  target, the resolved destination or the migrations that would run change. The
  server term means a failover or a restart onto a different host invalidates a
  token with no configuration change at all. It is **not** a single-use nonce:
  deliberately returning a
  database to the same revision and requesting the same transition re-authorises
  the transition you already reviewed. Running the downgrade unconfirmed prints
  the required command, including the connected database, and changes nothing.
* `stamp` is guarded the same way, in **either** direction. Moving
  `alembic_version` without running migrations rewrites the fact every
  confirmation is bound to, and an unguarded forward stamp could re-arm a spent
  confirmation and leave `check` reporting `compatible` on a schema missing its
  objects.

  Guarding makes that confirmable, not impossible. The refusal hands you a
  working command, and for a `stamp` that command rewrites the version table
  without migrating: a **confirmed** forward stamp still re-arms the spent
  confirmation and still leaves `check` reporting `compatible` on a schema
  missing its objects. Use it only when you already know the schema matches the
  revision you are stamping — never to "fix" a stuck state.

  There is no `stamp-confirmation` command. `downgrade-confirmation` mints
  downgrade confirmations only, and its token will never satisfy a stamp — the
  stamp is bound in a separate `stamp:` namespace. To stamp, run it unconfirmed
  (which changes nothing) and copy the full command, token included, out of the
  refusal:

  ```bash
  alembic stamp <revision>                      # refused; prints the command to re-run
  alembic -x confirm=<TOKEN> stamp <revision>
  ```

  Two things about that refusal. It is raised as an exception through the
  `alembic` CLI, so it appears after the last stack frame, on the
  `DowngradeTargetError:` line and the indented block that follows it — that is
  the refusal, not a crash. The command to re-run is the indented line under
  "then re-run with the confirmation…", partway down that block, with one
  closing paragraph after it. And its headline reads `Destructive migration
  downgrade refused.` even for a stamp; the `requested target : stamp:<rev>`
  line and the re-run command are what identify the operation. The refusal also
  prints `would revert : 0 migration(s)`. That is accurate — a stamp runs no
  migrations — but the stamp still rewrites `alembic_version`, which is the
  whole reason it is gated.

  A few failures surface as raw Alembic errors rather than as a guard refusal —
  a current database revision that is absent from this migration history, or a
  migration file that cannot be read. An unknown **requested target** is not one
  of these — it is a guard refusal. The effect is
  the same (nothing is applied), but the output is a traceback rather than the
  catalogue above.
* Offline (`--sql`) downgrades and stamps are **refused outright**: the script
  would be applied later to a database this process cannot identify.

#### When the guard refuses

The downgrade and stamp guards refuse any target they cannot pin to one specific
database. There is no `--force`, no `--yes` and no environment variable — by
design. `downgrade-confirmation` runs the same target, revision-state and
live-identity checks (and rejects a bare `head` itself), and creates no schema
and no rows, so **run it first for a downgrade**: it surfaces almost every refusal
below before you run anything destructive. (On SQLite, connecting to a path that
does not yet exist creates an empty file — a 0-byte database, never schema or
data. This cannot happen on PostgreSQL.) There is no equivalent dry run for a
stamp; see the stamp note above.

**Transport and DSN shape**

* **Unix-domain sockets** (`postgresql:///db`, `?host=/var/run/postgresql`,
  `?host=@…`, and percent-encoded spellings of those). PostgreSQL reports no
  server address or port over a socket, so the connection cannot be attributed
  to a particular server. **Connect over TCP — including when you are logged in
  on the database host**, which is the likeliest place to be during an incident.
  A hostless `postgresql:///db` is refused slightly earlier, as "missing a host
  or database name".
* **Several candidate hosts** (`host=a,b`, or a comma in the authority),
  `service=`, or `target_session_attrs=` — each resolves the target outside the
  URL or at connect time, so none can be pinned. A comma in a *host* is caught
  by the host-shape check first, so it reports "not a hostname or IP address";
  the several-candidates wording appears for `port` and `dbname`.
* **Any connection parameter not known to leave the target unchanged.** TLS
  settings, timeouts and labels are allowed (`sslmode`, `sslcert`, `sslkey`,
  `sslcrl`, `sslrootcert`, `sslcompression`, `connect_timeout`,
  `application_name`, `fallback_application_name`, `client_encoding`); anything
  else is refused rather than assumed benign, so a future libpq routing keyword
  cannot slip through. **`sslmode=require` — the production minimum — is
  explicitly supported.**
* **A repeated parameter** (`?sslmode=require&sslmode=disable`), on either
  PostgreSQL or SQLite.
* **A host that is not a hostname or an IP literal**, including all-numeric or
  octal/integer loopback aliases (`0177.0.0.1`, `2130706433`). Single-label
  hosts (`postgres`, `localhost`), underscores and IPv6 literals are accepted.
* **`postgres://`** is not a synonym for `postgresql://` — SQLAlchemy reports
  its backend as `postgres` and will not build an engine for it, so it is
  refused rather than silently folded in. Any other backend is refused outright.
* **In-memory SQLite** (`:memory:`, `sqlite://`, `sqlite:///`, `file::memory:`,
  `?mode=memory`) — such a database cannot be re-identified on a later
  connection, so it can never match a confirmation.

**What the server reports**

* The connected database name must equal the one the URL names (following any
  `?dbname=` override). A mismatch means the connection did not land on the
  configured target — the stale-`.env` case — and is refused with both names in
  the message.
* The server must report a non-empty address and a non-zero port. This is
  checked on the server's answer, not on the DSN, so it can also fire on a
  well-formed TCP connection through a proxy that suppresses
  `inet_server_addr()`.
* A pooler that presents a different database name than the backend is refused
  by design; adopting one is a reviewed change, not a bug.
* If the server's identity cannot be read at all (driver error, permission
  failure), the operation is refused rather than assumed safe.

**Target expression**

* Relative forms other than `-1`/`-01` and the `<rev>-1` style are refused with
  "the requested downgrade target is not a revision in this migration history".
* The `<source>:<target>` range form never reaches this guard: Alembic rejects
  it first with `Range revision not allowed` (online), and with `--sql` it is
  caught by the offline refusal instead.

**Revision state**

* `alembic_version` must hold **exactly one** revision. Zero (fresh or
  truncated) and several (unmerged branches) are both ambiguous and both refuse.
* The requested target must be **the current revision or an ancestor of it**
  (naming the current revision is accepted and reverts nothing) and must
  exist in this migration history, and every migration in the path must be
  readable on disk.

**Operation and mode**

* An Alembic operation this guard cannot identify is refused — a rename in a
  future Alembic release fails closed on the upgrade path rather than silently
  disarming the guard.
* Offline (`--sql`) downgrades and stamps are refused outright.

`upgrade`, `check` and `current` are unaffected by every item above: the guard
returns on the safe path before any of these checks run. A refusal here never
indicates a problem with your deployment's upgrade path.

**If the guard refuses a downgrade you are certain of, there is no bypass.**
Re-read the `connected database :` line, confirm the DSN and the revision state,
and re-mint. If a re-mint reports a *different* server address for the same
endpoint, that is the control working — a load balancer, a multi-node pooler or
round-robin DNS is answering, and you must pin the connection to one backend
before downgrading. If it still refuses, the guard is reporting that the target
is not what you believe it is — escalate rather than applying the DDL by hand.

The `python -m app.db.migrate` upgrade, downgrade and check paths emit
structured, secret-free logs (the database URL, driver messages
and tracebacks are never logged; failures carry only the exception *class*
name) and increment the bounded `migration_runs_total` metric
(`operation`, `outcome`). The bare invocation records
`operation=upgrade_verify`, not `upgrade`. `downgrade-confirmation` and
refusals raised through the direct `alembic` CLI emit neither a structured log
nor a metric.

The container images expose the same commands; run the migration actor as a
one-shot container/job that shares the API's configuration:

```bash
# one-shot migration actor (shares the API image + env)
docker run --rm --env-file <prod.env> signalnest-api python -m app.db.migrate
```

## Startup schema-compatibility check (verify, never mutate)

`app/db/schema.py` compares the database's current Alembic revision against the
head revision the running code expects and classifies the relationship:

| State | Meaning | Startup-safe? |
| --- | --- | --- |
| `compatible` | database is exactly at the code's head | yes |
| `ahead` | database carries a newer revision the code does not know | yes (additive-first) |
| `pending` | database is at an ancestor of the code head — migrations not applied | **no** |
| `uninitialized` | no `alembic_version` row (fresh database) | **no** |

On `pending`/`uninitialized` the process fails fast with an actionable message
telling the operator to run `python -m app.db.migrate`. It never migrates on your
behalf.

The `ahead` state is what makes a **rolling deploy** safe: during a rollout the
migration actor advances the schema first, so for a short window old replicas run
against a newer schema. Because migrations are **additive-first** (new columns are
nullable/defaulted, nothing an old replica reads is dropped or renamed in the same
release), an old replica remains compatible and reports `ahead` rather than
failing.

## Additive-first policy (this phase)

To keep rolling deploys safe, a single release must not both add and remove usage
of a column. The safe sequence for a breaking change spans **two** releases:

1. **Release N** — add the new column (nullable/defaulted); code writes both old
   and new, reads old.
2. **Migrate + deploy** — run the migration actor, then roll replicas so all code
   writes/reads the new column.
3. **Release N+1** — stop using the old column; a later migration drops it once no
   running code references it.

This is why the current migration head is reached purely by additive migrations
(`df66ff0426d2`, `e7c2a9b4f1d3`, `a1b2c3d4e5f6` are all nullable additive columns).

## Recommended rollout order

1. Build/publish the image at the new revision.
2. Run the **migration actor** (`python -m app.db.migrate`) to advance the schema
   to head. Wait for it to succeed.
3. Roll the API and worker replicas. Each verifies `compatible` (or `ahead` for a
   brief window) at startup; a replica that somehow starts against an un-migrated
   database fails fast instead of corrupting data.
4. If a rollback is required, redeploy the previous image. Because the schema is
   additive-first, the previous code runs against the newer schema (`ahead`); only
   run `downgrade` if a specific migration must be reversed, and only via the
   single actor with an explicit target revision and its confirmation.

## Never do this

* Do **not** run migrations from every replica (no auto-migrate on startup).
* Do **not** edit an already-applied migration in place — add a new revision.
* Do **not** downgrade with a bare `head`; always name the target revision.
  (`python -m app.db.migrate downgrade` rejects `head` outright, before any
  database access, with an argparse usage error and exit `2`. `npm run
  migrate:down` and the `alembic` CLI pass it through to the guard, which gates
  it like any other target: unconfirmed it produces the ordinary refusal, and a
  confirmation for it authorises an operation that reverts nothing. That no-op
  holds only while the database is at head; from any earlier revision, `head`
  is a descendant and is refused as "not an ancestor of the database's current
  revision".)
* Do **not** mint a confirmation from one shell and run the downgrade from
  another directory or against another `DATABASE_URL`. The confirmation is bound
  to the database that answered when it was minted; a relative SQLite path
  resolves against the working directory, so minting and running must happen
  from the same place.
* Do **not** treat a matching confirmation as proof you are on the right
  server. Two PostgreSQL instances that report the same address, port and
  database name — an `ssh -L` tunnel or port-forward to production while a local
  database is also named `signalnest` — produce the same confirmation. Read the
  `connected database :` line that `downgrade-confirmation` prints on stderr
  before you run the downgrade. A successful downgrade prints nothing, so that
  line and any refusal are the only places it ever appears.
* Do **not** make a column non-nullable and start reading it in the same release
  that adds it — that breaks the rolling-deploy `ahead` guarantee.

## Caller-controlled Alembic logging (Gate 4F)

The application-controlled migration paths (everything under
`python -m app.db.migrate`) build their Alembic `Config` through the shared
helper `app.db.schema.alembic_config()`, which:

1. constructs the `Config` from the real `alembic.ini`,
2. forces the **lazy** ini parse and verifies an ini-derived sentinel
   (`prepend_sys_path = .`) is present, and
3. only then clears `config_file_name`, so `alembic/env.py`'s existing
   conditional skips `logging.config.fileConfig()`.

The ordering is load-bearing: Alembic's `Config.file_config` is memoized off
the filename at first access, so clearing the filename *before* the parse
silently loses every ini-derived option while migrations still appear to work.
Tests pin both halves (`config_file_name is None` **and** the sentinel
surviving), and the staging-publish in-image gate re-asserts them.

Behavior delta (measured, not assumed):

* **Before:** `fileConfig()` replaced the root handler with the ini's
  plain-format stderr handler at `WARNING` and disabled the application's
  migrate logger — Alembic INFO went to stderr in plain text, and **every
  application event after the first `command.upgrade()` (including all
  verification failure classifications and the success event) was silently
  dropped** while exit codes still worked.
* **After:** the application's structured stdout handler, level, and redacting
  formatter survive the whole run. Application lifecycle and verification
  events emit as before-and-after-upgrade structured JSON; Alembic's own INFO
  records flow through the same structured handler onto stdout; stderr is
  empty. SQLAlchemy engine/SQL logging remains suppressed (SQLAlchemy pins its
  own logger to `WARN` when unset; a regression test asserts no
  `sqlalchemy.engine` records even at root `DEBUG`). No bound SQL parameters
  or connection details become newly visible.

The direct `alembic` CLI (`python -m alembic ...`) builds its own config, so
this logging behaviour does not apply to it. The downgrade and stamp guards do
— they live in `alembic/env.py`, which every entry point loads.

The bare-command success event `migrate.upgrade_verify.done` carries two
**independently sourced** provenance fields: `code_head` (repository script
directory) and `db_revision` (raw `alembic_version` read-back). An exact match
is required for exit `0` — which also means that *on the success path the two
values are necessarily equal*, so independent revision evidence comes from the
failure classifications (where the values genuinely differ and both are
emitted) and from the reader → comparator pipeline; tests pin the
`db_revision` field's read-back provenance mechanically rather than by value.

Note a deliberate format coupling: the reader and comparator validate
revisions as **12 lowercase hex characters** (this repository's Alembic
default). A future revision created with an explicit non-conforming
`--rev-id` would fail closed (reader exit `35`, comparator exit `41`) rather
than pass — keep revision ids on the default format.

## Raw live-revision reader

```bash
python -m app.db.revision_status     # prints exactly one line: the DB revision
```

A one-shot diagnostic that reads the live database's Alembic revision through
the application configuration path (`DATABASE_URL`; the staging one-shot task
also sets `SN_MIGRATION_MODE=1`) and prints **exactly one raw revision line**
to stdout on success. It makes no policy decision, mutates nothing, uses a
short-lived `NullPool` engine (disposed on exit), applies a bounded
`connect_timeout` for PostgreSQL only (never passed to SQLite), and emits no
logging, no tracebacks, and no exception text — failures are fixed stderr
tokens with dedicated exit codes:

| Exit | Classification |
| --- | --- |
| `0` | success — exactly one well-formed revision printed |
| `30` | configuration failure (arguments, settings, engine construction) |
| `31` | connection failure |
| `32` | `alembic_version` table missing |
| `33` | zero revision rows |
| `34` | multiple revision rows |
| `35` | malformed revision value |
| `36` | unexpected safe failure |

## Offline strict revision comparator

```bash
python -m app.db.revision_status | python -m app.db.revision_compare
```

Reads the live revision (exactly one line) from stdin, resolves the single
repository code head, and exits `0` only on **exact equality**. It is strictly
offline (never imports engine construction, never connects), reads the
complete input (extra or truncated lines reject), and is deliberately stricter
than the replica startup gate, which admits `ahead` for rolling deploys. Exit
band: `40` bad arguments, `41` invalid input, `42` code head unresolved
(zero/multiple heads), `43` mismatch, `44` unexpected safe failure.

## Verification boundaries (Part A / Part B)

**Part A — repository and in-image (delivered, PR-gated):** unit and
integration tests run the real `command.upgrade()` through the real `env.py`
and the production formatter; CI's container job and the staging-publish gate
execute the **bare** actor command (the exact ECS `command` shape,
`["python","-m","app.db.migrate"]`) inside the built worker image against
controlled SQLite with no network, asserting positive verification events,
fail-closed failure bands, reader/comparator behavior, and no secret-bearing
output.

**Part B — live execution (NOT yet performed or authorized):** none of the
above proves behavior against a live/staging PostgreSQL database, an actual
ECS task execution, or CloudWatch delivery of the events. SQLite differs from
PostgreSQL (e.g. non-transactional DDL), and the ECS log wiring is exercised
only at a real deployment. **No live verification has happened.** Live
verification, image republication, and a fresh planning cycle each require
separate authorization.

## Dedicated revision-reader ECS task (authored, NOT provisioned)

Nothing in this section is deployed or permitted today. The reader exists as
code, IaC and reviewed workflows. It has a **two-stage lifecycle**:
`enable_revision_reader_publication_bootstrap` (Stage A — the reader ECR
repository + publisher role, so the image can be published) and
`enable_revision_reader_runtime` (Stage B — the log group, security group, the
reader→RDS ingress rule, execution/runner roles, and the task definition).
Runtime requires publication bootstrap **and** a pinned image digest. Both
default to `false`, no reader image has been published, and no reader task has
ever run. Publishing (Stage A), pinning-plus-runtime (Stage B) and invoking are
separate later authorizations; **disable runtime before bootstrap** on teardown.

**It is a dedicated artefact, not the worker image.** The design this section
originally sketched — reuse the worker image with a task-definition `command` —
was superseded, because that image has no `ENTRYPOINT`, contains a shell, and
contains `app.db.migrate` (upgrade *and* downgrade). See
`apps/revision-reader/README.md` for the whole program and its honest limits.

**Destination authenticity (Gate 4J.1).** The reader does not trust the DSN's host or the
network path to decide which database it reads — both are caller-supplied and
un-constrainable by IAM. The expected host, database and role are **baked into the image**
(from `EXPECTED_DB_HOST`/`_NAME`/`_USER` build args supplied by the protected
`staging-reader-publish` environment), and the reader connects with `sslmode=verify-full`
against a committed, checksum-pinned AWS RDS CA bundle, taking only the password from the
injected secret. The publish workflow therefore requires those three build-arg variables in
addition to the role/account variables. See `infra/aws/modules/revision_reader/README.md` §9.

**Identity and permission delta** — three purpose-built identities, none of
them an existing role (`infra/aws/modules/revision_reader/iam.tf`):

* **Execution role** — pulls the reader repository *only*, injects exactly the
  `DATABASE_URL` secret, decrypts via `kms:ViaService` scoped to Secrets
  Manager, and writes only the dedicated reader log group. No
  `logs:CreateLogGroup`. Also an explicit **`Deny` on `s3:GetObject`** (Gate
  4J.1), which unconditionally closes the caller-supplied `environmentFiles`
  channel that would otherwise fetch env from S3 using this role.
* **Runner role** — `ecs:RunTask` on the **exact task-definition revision ARN**,
  not `ArnLike` on the family: a family-scoped grant widens silently the moment
  anyone registers revision N+1. Plus `ecs:DescribeTasks` (cluster-conditioned,
  since task ARNs are generated per run and cannot be pinned) and **`logs:GetLogEvents`
  alone** on the reader's own group — `logs:DescribeLogStreams` is deliberately **not**
  granted, because the workflow derives the stream name from the task ARN rather than
  enumerating. It is explicitly denied `ecs:StopTask`, ECS Exec at launch and connect,
  `ecs:TagResource`, and `cloudtrail:LookupEvents`.
* **Publisher role** — a separate identity with a different OIDC subject claim,
  scoped to pushing the reader repository. Publish and invoke are not
  interchangeable at the trust boundary.
* `iam:PassRole` names **one exact ARN**, the reader execution role. There is
  **no reader task role at all** — not an empty one — which is what keeps that
  list to a single entry.

**No temporary human policy is involved.** The reader is invoked by a reviewed
workflow under GitHub OIDC (`.github/workflows/reader-run.yml`), not by an
operator holding a time-boxed `RunTask` grant. That workflow takes **zero
inputs**: an operator-supplied task definition, cluster or command would be an
override channel, which is the property the reader exists to remove.

**Tags: omit them.** Invoke the reader with neither `--tags` nor
`--propagate-tags`. When an ECS tag-on-create API (`RunTask`,
`RegisterTaskDefinition`, `CreateService`) receives tags, ECS performs an
additional authorization check on `ecs:TagResource`; omitting tags removes
that dependency and an entire failure mode. If tags are ever wanted, grant
`ecs:TagResource` explicitly and narrowly in the reader's own temporary policy
(with the `ecs:CreateAction` condition key) and record it here — never rely on
a tagging grant inherited from a broader permission set, and never leave a
required permission undocumented. A tagging denial is fail-closed *before*
execution (the API returns `AccessDeniedException`; no task, no image pull, no
secret injection, no DB connection) — distinct from an execution-role failure,
which occurs after the task record exists (`STOPPED` with
`ResourceInitializationError`), still before application code runs.

**What does not contain a `RunTask` holder** — never treat these as security
boundaries. This analysis is what forced the dedicated image; each bullet notes
what changes for the reader specifically, and what does not change at all for
the migration task.

* **The task definition's `command`.** `RunTask` accepts
  `overrides.containerOverrides[].command`, so a `RunTask` holder replaces the
  command without needing `ecs:RegisterTaskDefinition`; a
  `RegisterTaskDefinition` holder can additionally register any image. The
  migration task definition's `command` is therefore **documentation, not a
  control**, and remains so.
  *For the reader only:* `ContainerOverride` exposes `{name, command,
  environment, environmentFiles, cpu, memory, memoryReservation,
  resourceRequirements}` — it has **no `entryPoint` member**. The reader image
  pins a fixed exec-form `ENTRYPOINT` and its task definition sets neither
  `entryPoint` nor `command`, so an override `command` arrives as argv to a
  program that rejects all argv (exit 50). That is prevention, not detection —
  and it is a property of *that image*, not of ECS.
* **The empty migration task role.** `RunTask` accepts `overrides.taskRoleArn`
  *and* `overrides.executionRoleArn`; the override names a *different* role,
  so the emptiness of the definition's role is irrelevant. No ECS condition
  key constrains the overridden role or override contents — the only boundary
  is the `iam:PassRole` resource list. The reader's answer is to make that list
  exactly one ARN and to omit the task role entirely.
* **Environment variables.** `containerOverrides[].environment` is settable at
  run time. Secret bindings cannot be re-pointed (container overrides have no
  `secrets` field) and values cannot be read back through the ECS API, but the
  injected `DATABASE_URL` is present in a container whose command the caller
  controls, and the task security groups permit TCP 443 egress — the
  credential can be exfiltrated. Redirecting the *connection* is harder (the
  only non-443 egress is TCP 5432 scoped to the RDS security group), but
  exfiltration needs no redirection.

The operative control is **who holds `ecs:RunTask`**, and nothing else.

**CloudTrail is corroborating recorded evidence, not a control.** The staging
trail records management events only (single-region, no data events, no
CloudWatch Logs delivery): it is detective, never preventive; delivery is
delayed (typically within ~15 minutes); no alarm fires on trail events today;
and `RunTask`/`RegisterTaskDefinition` are recorded but the container's own
behavior (connections, reads) is not. Never cite CloudTrail as a reason a
permission grant is safe.

## Deployment prerequisites after this remediation

* All previously generated workload plan artifacts remain **retired and
  ineligible**; no repository content references any saved plan.
* Both the **API and worker images must be republished** from a merge that
  contains this remediation before any deployment planning resumes: the
  previously published digests do not contain these fixes and are ineligible
  for the workload stage.
* The secured (git-ignored) tfvars must receive the **new** digests, the
  operator-held source SHA must move to the merge commit, and a **fresh
  planning cycle** is mandatory — all under separate authorization. Phase 4 is
  **not** apply-ready on the strength of this PR.

## Remaining architectural residuals (known, not addressed here)

* **Two PostgreSQL servers reporting the same address, port and database name
  share one confirmation** — the downgrade guard's only fail-open path; see
  "the confirmation is not proof of server" above.
* **A confirmation is not a single-use nonce, and a confirmed stamp re-arms a
  spent one** — accepted by design; see the `stamp` notes above.
* **Worker startup gate is a table-existence probe, not a revision gate**: the
  worker's `validate()` checks that the jobs/registry tables exist; unlike the
  API it does not compare Alembic revisions, so a worker can start against a
  schema that is behind the code as long as its tables exist. Adopting the
  revision gate for the worker is a separate decision.
* **Catch-all correlation**: FastAPI's generic `Exception` handler runs in
  Starlette's outermost error middleware — above the correlation middleware,
  whose context resets during unwind — so the 500 envelope/event on that path
  has no request id (pre-existing; pinned by test). Starlette also re-raises
  the exception to the server after the handler responds; Uvicorn's own logger
  (outside the application logging stack) may render that traceback.
* **`sanitize_exception().error_message` is not safe for driver errors**: its
  redaction only strips URL-shaped credentials; non-URL driver prose
  (host/IP/port/user, SQL, parameters) passes through. Only `error_class` may
  be emitted for database exceptions; no caller emits the message field today.
* **Exit code `3` is overloaded across actors**: Uvicorn exits `3` on startup
  failure (API container) and the migration actor exits `3` on ambiguous code
  heads — different task definitions, same number; keep dashboards per-actor.
