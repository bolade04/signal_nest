# SignalNest

AI marketing-intelligence ("AI Scout") platform. SignalNest turns public market
signals into **explainable, scored opportunities** for a business — separating what
was *observed* from what the AI *inferred*, and never recommending action it cannot
justify.

This repository contains a complete, reviewable vertical slice covering **Phase 1
(foundation)** and **Phase 2 (scouting → explainable opportunities)**. Phases 3–5
(creative generation, approvals, analytics, live integrations, billing) are out of
scope and documented in [`docs/phase-3-plan.md`](docs/phase-3-plan.md).

## Architecture at a glance

```
apps/
  web/   React 18 + TS + Vite + React Router + TanStack Query + RHF/Zod + Tailwind + Radix
  api/   FastAPI modular monolith (Python 3.12) — DB, migrations, domain logic,
         pure scoring/geo/claims engines, REST, auth, in-process jobs
packages/  shared TS types (generated from OpenAPI), config
scripts/   dev bootstrap, migrate, seed, gen-types, run-api, run-tests-api
docs/      architecture, testing, acceptance report, phase-3 plan
infra/     docker-compose (postgres+pgvector, redis) for full mode
```

- **Backend is authoritative.** All business rules — scoring, tenant isolation,
  RBAC, geo-relevance — live in `apps/api`. The frontend never re-implements them; it
  calls the API through a typed client generated from the OpenAPI schema.
- **Dual-mode infra.** Default is zero-dependency: SQLite + in-process queue +
  in-memory cache + local-file storage + numpy vector search. Full mode (Postgres +
  pgvector + Redis + S3) is selected via `APP_MODE=full` behind adapter interfaces.
- **LLM is mock-first.** A deterministic offline mock provider is the default;
  OpenAI/Anthropic adapters sit behind env with identical response contracts.

The API exposes **56 operations across 41 paths**. See the live OpenAPI docs at
`/api/v1/docs` when the server is running, or the committed
[`apps/api/openapi.json`](apps/api/openapi.json).

## Prerequisites

- Node 20 / npm 10 (pinned in root `package.json` `engines`)
- Python 3.12

## Quickstart (zero-dependency local mode)

```bash
npm run bootstrap      # install JS deps + create apps/api/.venv + install the API
npm run demo:setup     # migrate the SQLite schema + seed demo data
npm run dev            # start FastAPI (http://127.0.0.1:8000) + web (http://localhost:3000)
```

Then open http://localhost:3000 and sign in with the seeded demo account:

- **Email:** `demo@signalnest.dev`
- **Password:** `demo1234`

The demo data models one organization → one workspace → one brand → **four
independent city locations** (Dallas TX, London UK, Lagos NG, Nairobi KE), each with
its own scout request and scored opportunities, demonstrating strict per-location
isolation and geo accuracy.

## Common commands

| Command | What it does |
| --- | --- |
| `npm run bootstrap` | Install JS deps + create/refresh the API virtualenv |
| `npm run dev` | Run API + web together (Ctrl-C stops both) |
| `npm run dev:web` / `npm run dev:api` | Run one side only |
| `npm run migrate` | Apply Alembic migrations (SQLite by default) |
| `npm run seed` | Seed idempotent demo data (`-- --reset` to rebuild) |
| `npm run demo:setup` | Migrate + seed in one step |
| `npm run gen:types` | Regenerate `openapi.json` + the typed web client |
| `npm test` | Frontend test suite (Vitest + Testing Library) |
| `npm run test:api` | Backend test suite (pytest) |
| `npm run lint` / `npm run type-check` | Web lint (0 warnings) / strict tsc |
| `npm run build` | Production web build |
| `npm run smoke` | Start the API, run the HTTP isolation smoke test, stop it |

## Testing

- **Frontend:** 16 tests (Vitest + React Testing Library + MSW) covering the app
  shell, auth, onboarding, campaign context, scout requests, and the opportunity
  feed/detail — including three tests that prove Dallas/London/Lagos/Nairobi never
  leak across locations.
- **Backend:** 38 tests (pytest) — pure-engine unit tests for relevance, opportunity
  and confidence scoring, the noise gate, validation, the decision engine, the
  geography engine, and claim safety; plus integration tests that drive the real
  FastAPI app to prove auth enforcement and per-location isolation over HTTP.

**Continuous integration.** [`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs
four gates — **Frontend quality**, **Backend quality**, **Migrations and API contract**,
and **Integration smoke** — on pull requests to `main`, pushes to `main` and `feature/**`,
and manual dispatch. CI runs entirely in zero-dependency local mode (SQLite, in-process
adapters, fixture connectors, mock LLM); **no Docker, PostgreSQL, Redis or paid API keys
are required.**

See [`docs/testing.md`](docs/testing.md) for the full strategy (including how to run each
CI gate locally) and [`docs/acceptance-report.md`](docs/acceptance-report.md) for the
Phase 1–2 acceptance checklist and known limitations.

## Full mode (optional)

```bash
docker compose -f infra/docker-compose.yml up -d   # postgres+pgvector, redis
APP_MODE=full npm run migrate && APP_MODE=full npm run dev
```
