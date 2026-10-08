# Architecture

> **Status banner (2026-10-03, `P6-GOV-5`):** this document is a historical record of the Phase 1–2 system as built and is not updated for later phases. Current repository and readiness status lives in [`docs/project-phase-6-plan.md`](project-phase-6-plan.md) (§4 residual-work matrix and §4.6a live-state reconciliation; §1.2 is the dated 2026-09-19 baseline snapshot).

SignalNest is an AI marketing-intelligence platform whose core product is an
**intelligence pipeline**, not a copywriting tool. This document describes the
Phase 1–2 system; the dated section at the end records where `main` has moved since.

## Topology

- **`apps/api` — FastAPI modular monolith (authoritative).** Owns the database,
  migrations, domain logic, the pure scoring/geo/claims engines, REST endpoints,
  authentication, RBAC, tenancy enforcement, and the in-process job runner *(accurate for Phase 1–2:
  `InProcessQueue` ran each job synchronously in the API process at `8dca455e`; stale since `67ed438`
  (2026-07-13), when scout runs moved to the durable job store and a separate worker — see "The
  intelligence pipeline" below — 2026-10-06)*.
- **`apps/web` — React SPA.** A presentation layer that calls the API through a typed
  client ~~generated from the OpenAPI schema~~ *(2026-10-07 correction — inaccurate when written: the client functions in `apps/web/src/api/client.ts` and `endpoints.ts` are hand-written; what `npm run gen:types` generates from `apps/api/openapi.json` is their types — `schema.d.ts`, by openapi-typescript, aliased in `types.ts` — at `8dca455e` and on `main`)*. It contains **no** business rules,
  scoring, authorization, or tenant-isolation logic. *(Phase 1–2. On `main` `c5b48ed7` the SPA also
  decides which controls to show by role — `apps/web/src/lib/roles.ts` — and guards operator routes —
  `apps/web/src/auth/RequireOperator.tsx`; both gate what is displayed, and the API enforces
  authorization — 2026-10-06.)*
- ~~**`packages/shared`** — generated TS API types and shared enums/constants.~~ *(No
  `packages/` directory has ever been tracked in this repository, including at the Phase 1–2
  ~~acceptance~~ squash commit `8dca455e` *(and the accepted commit `b5965d35` — 2026-10-06 clarification)*; the generated types live in `apps/web/src/api/types.ts` and
  `apps/web/src/api/schema.d.ts` — measured on `main` `c4315d8d`, 2026-10-05.)* *(2026-10-07 clarification: `schema.d.ts` is the generated file; `types.ts` holds hand-written aliases of its types and a few hand-written types of its own.)*

The frontend and backend agree on exactly one contract: `apps/api/openapi.json`,
regenerated via `npm run gen:types`.

## Backend module layout

~~Each domain module uses layered files (`models.py`, `schemas.py`, `repository.py`, `service.py`, `policies.py`, `routes.py`):~~
*(2026-10-06 correction — inaccurate when written:
no `repository.py` or `policies.py` file exists under `apps/api/app/` at the Phase 1–2 squash
`8dca455e`, at the accepted commit `b5965d35` or on `main` `c5b48ed7`. Modules hold differing
subsets of `models.py`, `schemas.py` and `routes.py`; of the module names listed below, only `audit`,
`auth`, `brands`, `jobs` and `llm` have a `service.py` on `main`; `geography` and `claims` each hold an
`engine.py`, and `scoring` holds the scoring-engine modules (`relevance.py`, `noise.py`, `validation.py`,
`opportunity.py`, `decision.py`, `types.py`);
`business_profiles` and `clustering` contain only `__init__.py`; there is no `workspaces`
directory — the Workspace model is in `organizations/models.py`.)* Domain module names as
written:

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

*(2026-10-07 correction — inaccurate when written: the chain above gives stage labels in an order the code does not follow. Of its names only `run_scout_request` (the job entrypoint in `apps/api/app/jobs/pipeline.py`), `score_relevance` and `score_opportunity` are functions, and `classify_signal` is the name of an LLM task; the scoring, geography and claim engines are pure, but classification and the explanation are calls to the LLM service. At `8dca455e` the job fetches fixture signals from the connector; for each signal it stores the raw signal, evaluates noise (`evaluate_noise`), embeds it and marks it a duplicate if it is close to a signal already kept in the run, classifies only signals that are neither noise nor duplicates, and stores the normalized signal with its geo evidence; it then clusters the kept signals by pain-point DNA (falling back to the signal type, then to "general") and, for each cluster, scores relevance, validation, opportunity and confidence, bands the classification and confidence level, resolves the cluster's geography, checks claim safety, decides the action (`decide`) and builds the explanation — the LLM task `explain_opportunity` when relevance is at or above the action floor, fixed text otherwise. `main` keeps this order and adds, for each signal, an intelligence read (`_analyze_candidate`), persisted as an intelligence record, that the code describes as advisory metadata that does not alter these outputs.)*

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
  spam/bot/engagement-bait/unsafe content is hard noise; ~~full analysis begins at ≥ 50~~ *(2026-10-07 correction — inaccurate when written: `scoring/noise.py` defines a 50 threshold (`PRE_ANALYSIS_FULL_THRESHOLD`) that only a unit test reads; the pipeline treats a signal as noise when a hard-noise rule fires or its pre-analysis score is below 25, and attempts classification of every other signal that is not a duplicate, then scores the clusters those signals form — at `8dca455e` and on `main`)*.
- **Validation** (`scoring/validation.py`) — cross-source agreement, volume,
  engagement, ads, news, trends, buying intent.
- **Opportunity + confidence scoring** (`scoring/opportunity.py`) — weighted 0–100
  scores with explainable per-factor breakdowns, then banded into classifications
  (Noise → Discussion-only → Weak → Early → Validated → High-priority) and confidence
  levels (Low/Med/High).
- **Decision engine** (`scoring/decision.py`) — Act now / Act soon / Monitor /
  Archive / Stay silent / Block. Core rule: if it cannot explain *why the user should
  care*, it does not alert. *(2026-10-07 clarification: `decide()` does not test for an explanation — after returning Block for blocked claim risk, Stay silent for noise and Monitor outside the scout area, it returns Stay silent below the relevance floor and then Monitor when audience fit is unclear, and the explanation is built after the decision; nothing alerts at `8dca455e` or on `main`, where the notifications menu is a placeholder.)*
- **Geography engine** (`geography/engine.py`) — haversine radius matching (1–200 mi),
  coverage evaluation, and geo-relevance resolution from weighted evidence with a
  confidence score.
- **Claim safety** (`claims/engine.py`) — flags risky/blocked claims and translates
  competitor complaints into *safe* positioning (never an unsupported superiority
  claim or an attack).

## Dual-mode infrastructure

Every external dependency sits behind an adapter interface ~~selected by `APP_MODE`~~ *(2026-10-06
correction — inaccurate when written: `APP_MODE` selects no adapter at `8dca455e` or on `main`; each
row has its own setting, named in the measured note below)*:

| Concern | Local (default) | Full ~~(`APP_MODE=full`)~~ |
| --- | --- | --- |
| Database | SQLite | Postgres ~~+ pgvector~~ |
| Queue | in-process | Redis |
| Durable job queue | SQLite-backed store + worker | ~~(Redis/Celery/etc. — Phase 3B)~~ the same store on PostgreSQL |
| Cache | in-memory | Redis |
| Vector search | numpy brute-force | pgvector |
| Storage | local filesystem | S3 |
| LLM | deterministic mock | OpenAI / Anthropic |

Startup config validation fails fast in full/prod if a real provider or real DB is
not configured; the system never silently falls back between mock and real providers. *(2026-10-06,
precisely: full mode rejects SQLite but does not itself require a real LLM provider — staging and
production do (see "Selection" below). Staging and production did not themselves require PostgreSQL at
`8dca455e` (only `APP_MODE=full` did); on `main`, production rejects SQLite and staging does not. The LLM service falls back to the mock only under the dev-only
flag, and logs it; separately, the pipeline's explanation step returns fixed fallback text, without a
log line, when the provider raises `LLMError` (`apps/api/app/jobs/pipeline.py`, at `8dca455e` and on
`main`).)*

*(2026-10-05: two cells of this table describe adapters that are not operative on `main`
`c4315d8d` — `build_index()` in `apps/api/app/infra/vector.py` returns `BruteForceIndex()`
unconditionally and has no callers, so the `pgvector` cell is not operative: `vector_backend=pgvector`
is accepted (and required in production), but nothing on the search path reads it — the pipeline's
dedupe step (`apps/api/app/jobs/pipeline.py`) embeds each signal with `embed_text()` and compares
pairwise `cosine()` in process, no index object is constructed, the `embedding` column is JSON, and
no module imports the installed `pgvector` package or issues a vector SQL operator (`P6-PLAT-1`); and the
`Redis` queue adapter `xadd`s to a stream that no consumer reads (`P6-PLAT-2`). Both rows are open in the
Phase 6 plan.)*

*(2026-10-06 — measured facts for every row, at the Phase 1–2 squash `8dca455e` (`apps/api` is
byte-identical at the accepted commit `b5965d35`) and on `main` `c5b48ed7`. The list records what exists,
what selects it, what calls it, what tests it and what is live-verified; it makes no ruling on whether any
adapter is production-ready and does not revise the 2026-10-05 note above, which names two rows.)*

- **Selection.** `APP_MODE` selects nothing. Each row has its own setting: `database_url`, `queue_backend`,
  `cache_backend`, `vector_backend`, `storage_backend`, `llm_provider` (and, on `main`, `job_queue_backend`,
  which accepts only `local`). `APP_MODE=full` only adds validation (the mode is also reported by `/health`): it rejects SQLite, and a Redis backend
  without `redis_url`. Staging and production also reject the mock LLM provider and the dev-only LLM
  fallback flag (on `main`, outside the one-shot migration mode); the fallback is opt-in and logged. On
  `main`, production additionally requires `APP_MODE=full` and rejects each local backend.
- **Database (Postgres).** Implementation: the SQLAlchemy engine built from `database_url`
  (`apps/api/app/db/session.py`), which the application's database sessions use. Tests: none against Postgres at
  acceptance (CI ran SQLite only). On `main` the Backend quality job points the `TEST_POSTGRES_URL`-gated
  tests at a `postgres:16` service; run 37445036772 reports 2,558 passed and none skipped. Live: not
  verified here.
- **Queue (Redis).** Implementation: `RedisQueue`, defined inside `build_queue()` (`apps/api/app/infra/queue.py`),
  appends jobs to the Redis stream `signalnest:jobs`. Call path: the module-level `queue` is built at import.
  At acceptance the scout run endpoint enqueued through it — synchronously in-process by default; with
  `queue_backend=redis` the job was written to the stream, and no worker existed to read it. Since `67ed438`
  (2026-07-13) scout runs use the durable job store; on `main` no non-test code enqueues on this adapter,
  and only the readiness probe references the `queue` object (`jobs/pipeline.py` imports the module for its
  job registry). The same `queue_backend=redis` setting also selects the durable job store's Redis
  wake-up notifier — see the next bullet. Tests: none exercise the Redis queue branch (marked `pragma: no
  cover`); configuration tests only select it. Live: not verified.
- **Durable job queue.** Implementation: one backend, `local` — the SQLAlchemy-backed store
  (`apps/api/app/jobs/store.py`) and the worker (`python -m app.jobs.worker`), both since `67ed438`
  (2026-07-13). The store is SQLAlchemy-based — its `67ed438` docstring already names SQLite and PostgreSQL,
  and at `67ed438` every dialect claims with the same atomic compare-and-set; since `3fefb36` (2026-07-14)
  PostgreSQL claims with `SELECT … FOR UPDATE SKIP LOCKED` instead. Selection:
  `job_queue_backend` accepts only `local`; the dialect follows `database_url`; with `queue_backend=redis`,
  each immediately-due durable enqueue also publishes a Redis wake-up — scheduled, future-dated jobs are
  not signalled (`apps/api/app/jobs/coordination.py`, called from
  `apps/api/app/jobs/service.py`, since `3fefb36`), and no non-test code subscribes to it — the worker
  polls. Call path: scout runs enqueue through `jobs/service.py`; the worker claims. Tests: on `main` two
  `TEST_POSTGRES_URL`-gated store tests in `test_production_adapters.py` run on the CI `postgres:16`
  service, and the wake-up notifier is tested against `fakeredis`. Not present at acceptance. Live: not
  verified.
- **Cache (Redis).** Implementation: `RedisCache` (inside `build_cache()` at acceptance; a module class on
  `main`). Call path: the module-level `cache` is built when `app.infra.cache` is imported. No non-test module
  imported it at acceptance. On `main` it is imported to close the cache at shutdown
  (`apps/api/app/core/lifecycle.py`) and, for its Redis client factory, by `apps/api/app/jobs/coordination.py`
  when `queue_backend=redis` (for the wake-up notifier above); no non-test code reads or writes the cache. Tests: none at acceptance; on
  `main`, `apps/api/app/tests/test_production_adapters.py` exercises `RedisCache` against `fakeredis`. Live:
  not verified.
- **Vector search (pgvector).** No implementation: see the 2026-10-05 note above (`P6-PLAT-1`). Live: not
  applicable.
- **Storage (S3).** Implementation: `S3Storage` (inside `build_storage()` at acceptance; a module class on
  `main`). Call path: the module-level `storage` is built when `app.infra.storage` is imported; no non-test
  module imports it, at acceptance or on `main`. Tests: none at acceptance; on `main`,
  `test_production_adapters.py` exercises `S3Storage` with an injected fake client (no boto3 call, no
  bucket). Live: not verified.
- **LLM (OpenAI / Anthropic).** Implementation: `OpenAIProvider` and `AnthropicProvider`
  (`apps/api/app/llm/providers_real.py`), selected by `llm_provider` (default `mock`). Call path: the
  module-level `llm_service` builds the selected provider at import, and the scout pipeline calls it
  (`apps/api/app/jobs/pipeline.py`, two call sites) at both commits. Tests: no test names the real provider
  classes; CI runs with `LLM_PROVIDER=mock`. Live: not verified.
- **Live verification, all rows:** none is recorded for this correction. It would need a recorded check
  against deployed services, and no Phase 6 commit is deployed.

## Tenancy & security

- ~~Every query is scoped server-side by `organization_id` / `workspace_id` (and
  `location_id` / `campaign_id` where applicable)~~ ~~in the repository layer~~ *(2026-10-06
  correction — inaccurate when written: there is no repository layer. The tenant predicates are
  written directly in the modules that run the queries — route handlers, `service.py` modules and
  other module helpers. At `8dca455e` they appear in the route handlers, `brands/service.py`,
  `opportunities/context.py`, `jobs/pipeline.py` and `auth/dependencies.py`; on `main` `c5b48ed7`
  also in, for example, `jobs/store.py`, `scouting_requests/schedules.py`,
  `intelligence/persistence.py` and `organizations/members.py`. This note does not re-verify that
  every query is scoped)*. *(2026-10-08 correction — the struck universal was not established when written and is not established now; no counterexample was found. Measured at the accepted commit `b5965d35`, from each route function's own text: of the 24 route functions whose path names a workspace, 23 declare `get_tenant_context` or a role dependency built on it and the other, `GET /workspaces/{workspace_id}`, checks organization membership in its body; 20 of them filter their reads by that workspace (the audit-log route also returns the organization's rows that have no workspace) or load a row by id and reject it unless its `workspace_id` matches (`_get_scoped` in `opportunities/routes.py` and `scouting_requests/routes.py`), and the 3 create handlers write their rows under it (onboarding through its service helper); the three campaign-context route functions serve nine collections; the organization routes filter by, or check, the caller's membership in their bodies; the scout pipeline reads the request's own rows by id and its workspace's business context. Evidence limits: this reads route text and, apart from that one helper, does not trace helper functions or every query; `main` `1a24c4fb` has 82 route functions — 81 on routers and the root `/health` — including organization-scoped and operator routes, not re-traced here; at `b5965d35` no test or smoke step reads across organizations or workspaces; and some client-supplied relationship identifiers are stored without a workspace check (see the 2026-10-08 correction below))*
  ~~Client-supplied tenant IDs are never trusted.~~ *(2026-10-08 correction — inaccurate when written, and still inaccurate on `main` `1a24c4fb`. Measured at the accepted commit `b5965d354a0c2335c2ac9cf283fd28b56d8d612d` and on `main`: the organization and workspace context is derived on the server — `get_tenant_context` loads the workspace named by the request's `workspace_id` (a path parameter on both create handlers) and admits the caller only as a member of that workspace's organization, and the scout-request and campaign-context create handlers take `organization_id` from that context — and scout-request creation rejects a `location_id` from another workspace. The following client-supplied relationship identifiers are stored as sent, with no workspace check: a scout request's `campaign_id`, whose only constraint is a foreign key to `campaigns.id` that does not tie the campaign to the workspace, and which the opportunities created by its runs copy; a scout request's `product_profile_id`, a plain column with no foreign key; and the `location_ids` of campaigns and `eligible_location_ids` of offers, JSON lists with no foreign key and no membership check (`apps/api/app/scouting_requests/routes.py:85` and `apps/api/app/campaign_context/routes.py:97` at the accepted commit; `:137` and `:97` on `main`). No API path writes a client-supplied value to any of them after creation — the scout-request update schema has none of these fields and the campaign-context collections have no update route — although deleting a campaign sets `campaign_id` to null on the scout requests and opportunities that reference it (`ON DELETE SET NULL`); no API test names any of them at either commit. The location-list gap is the open launch-blocker row `P6-PLAT-8` in [`project-phase-6-plan.md`](project-phase-6-plan.md); no plan row names the two scout-request identifiers. This note changes no code and does not remediate either gap. Other client-supplied identifiers were not examined, and nothing here describes exposure or deployed behaviour)*
- RBAC roles: Owner, Admin, Marketer, Reviewer, Viewer, Compliance Reviewer, enforced
  ~~by per-domain policy layers~~ *(2026-10-06 correction — inaccurate when written: there are no
  per-domain policy layers. The roles are defined in `apps/api/app/core/enums.py`. Authorization is
  done by FastAPI dependencies from `apps/api/app/auth/dependencies.py` that routes declare, together
  with checks inside some modules. At `8dca455e` and `b5965d35`, `get_tenant_context` resolves the
  caller's organization-membership role for the requested workspace and `require_role(...)` admits
  every role ranked at or above the lowest-ranked role it names; `organizations/routes.py` also
  checks membership inline (`_assert_member`). On `main` `c5b48ed7` those two dependencies remain,
  `require_exact_roles(...)` and `require_exact_organization_roles(...)` (added after acceptance)
  admit only the named roles, `require_operator` gates operator-only routes, and
  `organizations/members.py` and `organizations/invitations.py` re-check the actor's role
  themselves. This note does not enumerate every check)*.
- ~~Scout requests are isolated by workspace + brand + location + market + campaign, so
  results from one city never influence another unless explicitly combined.~~ *(2026-10-08 correction — inaccurate when written, and still inaccurate on `main` `1a24c4fb`, for what a run is scored against: each run classifies its signals against the workspace's audience labels and scores their relevance against workspace-wide context — the workspace's product, audience and competitor rows and every `markets_served` entry of its business profile (`build_business_context` in `apps/api/app/opportunities/context.py`, called once per run from `apps/api/app/jobs/pipeline.py`); products, audiences and competitors have no location column, no run reads the other Campaign Context collections, and a location's own `local_competitors` field is not read by any run. The Campaign Context page calls its context brand-wide ("applies to every location"), but the Phase 1–2 demo seed writes each city's audience and competitors as "per-location" rows of the workspace and lists all four markets in `markets_served`, so in the accepted demo each city's scout run is classified against all four cities' audience labels and its relevance is scored against all four cities' audiences, competitors and markets. Measured at `b5965d35` otherwise: a scout request records its workspace, that workspace's primary brand, an optional location (checked against the workspace), a market — typed by the user or derived from the location — and an optional campaign (stored without a workspace check; see the 2026-10-08 correction above). A run reads signals through its market string: the fixture connector returns the first demo market whose name contains the string or is contained in it, nothing for any other string, and all four markets' fixtures when there is no market — "Workspace-wide" with no market typed, the location the dialog opens with whenever the header shows all locations (`apps/api/app/scouting_requests/fixtures.py:156-166`); dedupe and clustering run over the signals the request fetched — with no market, over all four markets' signals together; its opportunities are stored with the request's workspace and location, and the opportunity feed filters by workspace and, when a location is given, by location. The acceptance-time API test and smoke flow check that the seeded locations' location-filtered feeds return only rows stored for that location and no row under two locations; the frontend test checks, against a mocked backend that applies the location filter itself, that the Dallas and London views show no other city's opportunity titles. Not established beyond this: market matching here and in the geography engine is substring-based and permissive for radius rules without a market list (`P6-DATA-6`, open), the market string can be typed, and the campaign link is unchecked)*

*(2026-10-06: the statements in this section that every query is scoped, that client-supplied tenant IDs
are never trusted and that results never cross cities are universal properties. The evidence on record for
them is the acceptance-time isolation testing — integration tests and the four-market HTTP smoke flow in
[`acceptance-report.md`](acceptance-report.md); this correction did not re-verify them exhaustively.)* *(2026-10-07 correction — inaccurate when written: that testing covers only the separation of opportunity results by location inside one workspace. No acceptance-time test or smoke step read across organizations or workspaces or sent another tenant's identifiers, so the first two properties had no test evidence beyond that at acceptance; this note does not assess later tests.)* *(2026-10-08: the second of these properties was also inaccurate when written — client-supplied relationship identifiers are not all checked; see the 2026-10-08 correction where it is struck above.)* *(2026-10-08: the first is now struck as not established and the third as inaccurate when written for a run's scoring context — see the 2026-10-08 corrections on them above.)*

## Frontend structure (`apps/web/src`)

- `api/` — typed fetch client (correlation IDs, normalized errors, ~~retry only on safe reads~~), query-key factory embedding
  `workspace_id`/`location` for cache isolation. *(2026-10-07 correction — inaccurate when written: nothing in `api/` retries. `apiRequest` in `apps/web/src/api/client.ts` sends one `fetch` per call and turns a network failure or an error response into an `ApiError` — status 0 when the server could not be reached; an aborted request is re-thrown unchanged. Retrying is the TanStack Query default set in `apps/web/src/app/providers.tsx`, unchanged from `8dca455e` to `main`: a query is retried after an `ApiError` with status 0 or 500 and above, at most twice (three attempts in all), and never after any other status or any other error; mutations are never retried; and a query can set its own `retry` — two queries did at `8dca455e`, six do on `main`. The policy keys on query versus mutation and on the error, not on the HTTP method; that every query currently reads with `GET` — the 17 query calls at `8dca455e` and the 33 on `main` (32 `useQuery` calls and one `fetchQuery`, which also takes the default policy) — is a property of the call sites, not something the policy enforces.)*
- `workspace/` — `WorkspaceContext` (active org/workspace/brand/location).
- `auth/` — session + protected routes.
- `pages/` — Overview, Onboarding, CampaignContext, Locations, ScoutRequests,
  ScoutRequestDetail, Opportunities, OpportunityDetail, Settings.
- `components/` — Radix-based shadcn-style UI primitives and layout.

## Where `main` has moved since Phase 1–2 (measured 2026-10-05 on `c4315d8d`)

This section exists so that the body above does not mislead by omission (`P6-GOV-5`). It
lists what is *present* in the tree, not what is complete, enabled or deployed; status lives
in [`docs/project-phase-6-plan.md`](project-phase-6-plan.md). None of the Phase 6 repository
changes is deployed (plan §4.6a).

- **Backend module directories** under `apps/api/app/` (26, excluding `__pycache__`): `api`,
  `audit`, `auth`, `brands`, `business_profiles`, `campaign_context`, `capabilities`, `claims`,
  `clustering`, `connectors`, `core`, `db`, `feedback`, `geography`, `infra`, `intelligence`,
  `jobs`, `llm`, `locations`, `opportunities`, `organizations`, `scoring`, `scouting_requests`,
  `signals`, `system`, `tests`. Present at the ~~acceptance~~ Phase 1–2 squash commit `8dca455e` *(and the accepted commit `b5965d35`;
  `apps/api` is identical — 2026-10-06 clarification)* but not in the list above: `api` (router
  aggregation), `core`, `db`, `infra` (cache, ~~mail,~~ queue, storage and vector adapters *(2026-10-06
  correction — inaccurate when written: `infra/mail.py` was first added on 2026-09-26, in #184)*), `tests` (the API's pytest suite) *(2026-10-07
  correction — inaccurate when written: `tests` was present at `8dca455e` and the 2026-10-05 list put it in neither group)*. Added
  since: `capabilities` (Phase 4A capability registry, resolver and operator overrides —
  `docs/verification/4a-c-*.md`), `connectors` (Phase 3B connector base, policy, rate limiting,
  retry, registry and an RSS parser whose live egress is not wired on `main` — draft PR #34),
  `feedback` (Phase 3C human feedback loop — `docs/verification/phase-3-closeout.md`),
  `intelligence` (Phase 3B signal intelligence and opportunity scoring —
  `docs/phase-3b/signal-intelligence-design.md`), `system` (health, readiness and operator-only
  internal routes). `workspaces` is not a module directory; the Workspace
  model lives in `organizations/models.py`. Modules exposing a `routes.py`: `audit`, `auth`,
  `brands`, `campaign_context`, `feedback`, `jobs`, `locations`, `opportunities`,
  `organizations`, `scouting_requests`, `system`.
- **Durable jobs:** `JOB_QUEUE_BACKEND` implements only `local` (~~SQLite-backed~~ store + worker *(2026-10-06
  correction — inaccurate when written: the `local` store is SQLAlchemy-based and its `67ed438` docstring
  already names SQLite and PostgreSQL; since `3fefb36` (2026-07-14) it claims with `FOR UPDATE SKIP LOCKED`
  on PostgreSQL, tested in CI on `postgres:16`; see "Durable job queue" in the measured note
  under "Dual-mode infrastructure")*;
  `docs/phase-3a-durable-jobs.md`). The "(Redis/Celery/etc. — Phase 3B)" cell in the table
  above was a plan; Phase 3B did not deliver it.
- **API surface:** 85 paths / 106 operations in `apps/api/openapi.json`.
- **Frontend pages** under `apps/web/src/pages/` beyond the list above: `auth/` (SignIn,
  Register, ForgotPassword, ResetPassword, VerifyEmail, AuthLayout, DemoSignInShortcut),
  `invite/InvitePage`, `operations/Operations`, `settings/` (OrganizationMembers, Invitations,
  ChangePasswordDialog), `scouts/` (JobsPanel, SchedulePanel, ScoutRequestDialog),
  `opportunities/` (FeedbackPanel, IntelligencePanel, OpportunityCardView),
  `locations/LocationDialog`, `NotFound`. `src/auth/` also holds `RequireOperator.tsx` and
  `sign-out.ts`. *(2026-10-07 clarification: of these, `auth/` SignIn, Register and AuthLayout, `locations/LocationDialog`, `opportunities/OpportunityCardView`, `scouts/ScoutRequestDialog` and `NotFound` already existed at `8dca455e` but were not in the list above; the others were added since.)*
- **Top-level layout** *(2026-10-07, measured on `main` `c0989789`; not in the 2026-10-05 list)*: `apps/` holds `api`, `web` and, since #140 (`0ccd39d`, 2026-07-29), `revision-reader` — a separate Python program with its own `pyproject.toml`, `Dockerfile` and tests that reads the live Alembic revision from the database and prints it; its README records it as authored, not provisioned, and CI tests it in the required "Revision reader (unit, IaC contract, in-image)" job. Also added at the top level since `8dca455e`: `infra/` (the AWS staging IaC and a local full-mode Docker Compose stack), `tests/` (repository-level tests) and `.env.canary.example`. This document describes none of these beyond this entry.
- **Not described in this document:** the Phase 3–4 verification records
  (`docs/verification/`), the AWS staging IaC and runbooks (`infra/aws/`, `docs/operations/`),
  and the Phase 5A–5E guided-action scope (`docs/project-phase-5-plan.md`, excluded from
  Phase 6 by `P6-D01`).
