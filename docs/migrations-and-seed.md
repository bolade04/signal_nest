# Database Migrations & Demo Seed

The FastAPI backend (`apps/api`) uses **Alembic** as the single authoritative schema
path in every mode (SQLite local + Postgres/pgvector full). The app no longer calls
`create_all()` — startup fails fast with an instruction if the database has not been
migrated. All commands run through the api virtualenv (`apps/api/.venv`) and error
clearly if it is missing (run `npm run bootstrap` first).

## Prerequisites

```bash
npm run bootstrap        # creates apps/api/.venv and installs Python deps
```

## Root commands

| Command | What it does |
| --- | --- |
| `npm run migrate` | `alembic upgrade head` — apply all pending migrations |
| `npm run migrate:down` | `alembic downgrade -1` — roll back the most recent migration |
| `npm run migrate:status` | show the current revision + full history |
| `npm run migrate:create -- --message "add x"` | autogenerate a new revision from model changes |
| `npm run seed` | seed idempotent demo data (safe to re-run; no duplicates) |
| `npm run seed:reset` | wipe all data and reseed from scratch (local dev only) |
| `npm run demo:setup` | one-shot: `migrate` then `seed` → app ready to run |

Start the API afterwards with `npm run dev:api`.

## Creating a migration

1. Change or add SQLAlchemy models under `apps/api/app/**/models.py`. Every model must
   be imported by `app/db/models.py` so `Base.metadata` is complete for autogenerate.
2. Generate the revision:
   ```bash
   npm run migrate:create -- --message "describe the change"
   ```
   `alembic/env.py` injects the database URL from `Settings` and enables
   `compare_type`, `compare_server_default`, and `render_as_batch` (required so SQLite
   can emulate `ALTER TABLE`). A post-write hook runs `ruff --fix` on the new file.

## Reviewing a migration

- Open the generated file in `apps/api/alembic/versions/` and read both `upgrade()` and
  `downgrade()`. Confirm constraints, indexes, foreign keys, tenant-scoping columns
  (`organization_id` / `workspace_id` / `brand_id` / `location_id` / `campaign_id`),
  timestamps, JSON columns, and unique constraints match intent.
- Verify there is no unintended drift:
  ```bash
  cd apps/api && .venv/bin/python -m alembic check   # "No new upgrade operations detected."
  ```
- Enum values are stored as plain strings (`String` columns), so enum changes are data
  concerns, not schema migrations.

## Applying / rolling back

```bash
npm run migrate            # upgrade to head
npm run migrate:down       # roll back one revision
npm run migrate:status     # confirm current revision
```

Round-trip test used to validate every revision (clean create, full down, re-up):

```bash
cd apps/api
rm -f signalnest.db
.venv/bin/python -m alembic upgrade head     # empty DB -> full schema (27 tables)
.venv/bin/python -m alembic downgrade base   # -> only alembic_version remains
.venv/bin/python -m alembic upgrade head     # -> 27 tables again
```

## Demo seed

`app/db/seed.py` (`python -m app.db.seed`) is **idempotent** and produces
**deterministic content**:

- Root entities (org, user, workspace, brand, locations, campaign context) use stable
  IDs derived from a fixed UUID namespace, so re-seeding produces identical primary keys.
- Pipeline-generated rows (signals, opportunities) use the base model's `uuid4`
  surrogate keys, so their primary keys differ between reseeds — but their observable
  content (titles, resolved markets, scores, classifications, per-market counts) is
  identical across `--reset` runs.
- Without `--reset`, an already-seeded database is left untouched (no duplicates).
- With `--reset`, every table is cleared and rebuilt.
- All generated signals and opportunities are flagged `is_simulated=True`.

**Scenario:** one specialty-coffee brand ("Brew & Bean") operating **four fully
independent locations** — Dallas TX, London UK, Lagos NG, Nairobi KE. Each has its own
geo-coverage rule, local competitors, audience, and a per-location promotion. The
pipeline runs once per location; because fixture signals are market-tagged and each scout
request is scoped to one market, **results never leak across locations** (Dallas
opportunities are built only from Dallas signals, etc.).

Demo login: `demo@signalnest.dev` / `demo1234`.

The seed exercises the full pipeline: relevant signals, off-topic low-relevance signals,
spam (noise-filtered), near-duplicate re-posts (deduped), low-confidence geo evidence,
and a comparative/superlative claim that trips the claim-safety guard (high-risk
opportunity with claims warnings).
