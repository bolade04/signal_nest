# Architecture

SignalNest is an AI marketing-intelligence platform whose core product is an
**intelligence pipeline**, not a copywriting tool. This document describes the
Phase 1–2 system.

## Topology

- **`apps/api` — FastAPI modular monolith (authoritative).** Owns the database,
  migrations, domain logic, the pure scoring/geo/claims engines, REST endpoints,
  authentication, RBAC, tenancy enforcement, and the in-process job runner.
- **`apps/web` — React SPA.** A presentation layer that calls the API through a typed
  client generated from the OpenAPI schema. It contains **no** business rules,
  scoring, authorization, or tenant-isolation logic.
- **`packages/shared`** — generated TS API types and shared enums/constants.

The frontend and backend agree on exactly one contract: `apps/api/openapi.json`,
regenerated via `npm run gen:types`.

## Backend module layout

Each domain module uses layered files (`models.py`, `schemas.py`, `repository.py`,
`service.py`, `policies.py`, `routes.py`):

`organizations, workspaces, brands, business_profiles, locations, geography,
campaign_context, claims, scouting_requests, signals, clustering, scoring,
opportunities, auth, audit, llm, jobs`.

Framework-free engines live under `scoring/`, `geography/`, and `claims/` as pure
services that are unit-tested without FastAPI or a database.

### The intelligence pipeline (Phase 2)

The pipeline chain (pure engines, orchestrated by the scout handler):

```
run_scout_request → ingest_source_data → normalize_signal → classify_signal
  → dedupe/cluster → noise_filter → score_relevance → score_geo_relevance
  → validate → score_opportunity → generate_explanation
```

Since Phase 3A.3 this pipeline runs as a **durable background job** (`app/jobs/`),
not synchronously inside the run request. The scout run endpoint atomically flips the
request to `queued` and enqueues a `scout_request.execute` job; a separate worker
(`python -m app.jobs.worker`, default local backend) claims it with a SQLite-safe atomic
compare-and-set, holds a lease with heartbeats, and drives it to a terminal state with
bounded retries, dead-lettering, cooperative cancellation and expired-lease recovery.
Delivery is **at-least-once with idempotency controls**, so handlers are safe to re-run.
See `docs/phase-3a-durable-jobs.md`.

Key engines and their rules:

- **Relevance** (`scoring/relevance.py`) — blends keyword/pain-point/audience/
  competitor overlap. Hard rule: **relevance < 40 ⇒ never recommend action.**
- **Noise gate** (`scoring/noise.py`) — "collect broadly, notify selectively";
  spam/bot/engagement-bait/unsafe content is hard noise; full analysis begins at ≥ 50.
- **Validation** (`scoring/validation.py`) — cross-source agreement, volume,
  engagement, ads, news, trends, buying intent.
- **Opportunity + confidence scoring** (`scoring/opportunity.py`) — weighted 0–100
  scores with explainable per-factor breakdowns, then banded into classifications
  (Noise → Discussion-only → Weak → Early → Validated → High-priority) and confidence
  levels (Low/Med/High).
- **Decision engine** (`scoring/decision.py`) — Act now / Act soon / Monitor /
  Archive / Stay silent / Block. Core rule: if it cannot explain *why the user should
  care*, it does not alert.
- **Geography engine** (`geography/engine.py`) — haversine radius matching (1–200 mi),
  coverage evaluation, and geo-relevance resolution from weighted evidence with a
  confidence score.
- **Claim safety** (`claims/engine.py`) — flags risky/blocked claims and translates
  competitor complaints into *safe* positioning (never an unsupported superiority
  claim or an attack).

## Dual-mode infrastructure

Every external dependency sits behind an adapter interface selected by `APP_MODE`:

| Concern | Local (default) | Full (`APP_MODE=full`) |
| --- | --- | --- |
| Database | SQLite | Postgres + pgvector |
| Queue | in-process | Redis |
| Durable job queue | SQLite-backed store + worker | (Redis/Celery/etc. — Phase 3B) |
| Cache | in-memory | Redis |
| Vector search | numpy brute-force | pgvector |
| Storage | local filesystem | S3 |
| LLM | deterministic mock | OpenAI / Anthropic |

Startup config validation fails fast in full/prod if a real provider or real DB is
not configured; the system never silently falls back between mock and real providers.

## Tenancy & security

- Every query is scoped server-side by `organization_id` / `workspace_id` (and
  `location_id` / `campaign_id` where applicable) in the repository layer.
  Client-supplied tenant IDs are never trusted.
- RBAC roles: Owner, Admin, Marketer, Reviewer, Viewer, Compliance Reviewer, enforced
  by per-domain policy layers.
- Scout requests are isolated by workspace + brand + location + market + campaign, so
  results from one city never influence another unless explicitly combined.

## Frontend structure (`apps/web/src`)

- `api/` — typed fetch client (correlation IDs, normalized errors, retry only on safe
  reads), query-key factory embedding `workspace_id`/`location` for cache isolation.
- `workspace/` — `WorkspaceContext` (active org/workspace/brand/location).
- `auth/` — session + protected routes.
- `pages/` — Overview, Onboarding, CampaignContext, Locations, ScoutRequests,
  ScoutRequestDetail, Opportunities, OpportunityDetail, Settings.
- `components/` — Radix-based shadcn-style UI primitives and layout.
