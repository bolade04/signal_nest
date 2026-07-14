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
python -m app.db.migrate downgrade <revision>   # explicit, targeted downgrade
```

* `upgrade` applies every pending migration up to the current head. This is the
  **only** write path and must be run by a single actor.
* `check` reports the schema state and exits `0` when the schema is startup-safe,
  `1` otherwise. It performs no DDL and is safe to run anywhere.
* `downgrade` requires an explicit target revision — a bare `head` is rejected —
  so a downgrade is always a deliberate, named step.

All three emit structured, secret-free logs (the database URL is never logged)
and increment the bounded `migration_runs_total` metric (`operation`, `outcome`).

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
   single actor with an explicit target revision.

## Never do this

* Do **not** run migrations from every replica (no auto-migrate on startup).
* Do **not** edit an already-applied migration in place — add a new revision.
* Do **not** downgrade with a bare `head`; always name the target revision.
* Do **not** make a column non-nullable and start reading it in the same release
  that adds it — that breaks the rolling-deploy `ahead` guarantee.
