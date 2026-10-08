# Phase 1–2 Acceptance Report

> **Status banner (2026-10-03, `P6-GOV-5`):** this document is a historical record of the Phase 1–2 acceptance (its Phase 3 statements describe that era) and is not updated for later phases. Current repository and readiness status lives in [`docs/project-phase-6-plan.md`](project-phase-6-plan.md) (§4 residual-work matrix and §4.6a live-state reconciliation; §1.2 is the dated 2026-09-19 baseline snapshot).

Scope: foundation (Phase 1) + scouting → explainable opportunities (Phase 2), plus the
security-remediation, CI-reliability, GitHub-Actions, and frontend-lint-toolchain work
that followed. Phase 3+ is intentionally out of scope; see
[`phase-3-plan.md`](phase-3-plan.md).

## Executive acceptance status

- **Phase 1–2 is complete and accepted.** The production-style vertical slice is
  implemented end to end (foundation → scouting → explainable, scored opportunities).
- **`main` is green** *(at acceptance)*. The latest CI run for the accepted commit passes all four
  required jobs *(the ruleset has required six contexts since Phase 6 — see "Governance and protection" below)*.
- **Security advisories are remediated** *(at acceptance)*. `npm audit` reports zero vulnerabilities and
  there are zero open Dependabot security alerts *(the 2026-10-05 measurement — 3 open MEDIUM alerts, none HIGH/CRITICAL on a runtime dependency, and 3 `npm audit` advisories — is recorded in [`docs/project-phase-6-plan.md`](project-phase-6-plan.md) §19 item 12 and ~~rows `P6-CI-6`/`P6-CI-7`~~ row `P6-CI-7`)* *(2026-10-07 correction — inaccurate when written: row `P6-CI-6` carries none of these figures; §19 item 12 carries all three and row `P6-CI-7` the alert figures)*.
- **CI quality checks now propagate real failures.** The pipefail masking bug is fixed
  and guarded by a regression test.
- **Frontend lint tooling is migrated and stable** (ESLint flat config on ESLint 10).
- ~~**Phase 3 has not started.** All Phase 3 surface remains stubbed/planned only.~~ *(True at acceptance. Phase 3 was then implemented and closed ~~with its capabilities shipped dark~~ — [`docs/verification/phase-3-closeout.md`](verification/phase-3-closeout.md); Phase 4 tranches are recorded under [`docs/verification/`](verification/); the current track is Project Phase 6 — [`docs/project-phase-6-plan.md`](project-phase-6-plan.md). Nothing from Phase 6 is deployed.)* *(2026-10-07 correction — inaccurate when written: on `c4315d8d`, the commit this 2026-10-05 note was written against, the RSS connector, scouting-schedule changes and the 3C feedback loop were off by default through flags, but signal intelligence and its opportunity scoring, the SB-A run-history endpoint and the 3A durable job runtime had no flag; measured detail: the 2026-10-07 corrections in [`docs/phase-3-plan.md`](phase-3-plan.md) and in the closeout record. Repository behaviour only, not deployment)*

## Final repository baseline

| Item | Value |
| --- | --- |
| Repository | `bolade04/signal_nest` |
| Default branch | `main` |
| ~~Current accepted & maintained~~ Accepted `main` SHA *(at this report; `main` has since advanced)* | `b5965d354a0c2335c2ac9cf283fd28b56d8d612d` |
| Original Phase 1–2 implementation squash commit | `8dca455e9592fdec959e57e6d9f741007f421f5f` |
| Working tree at acceptance | clean (no tracked changes; `git status --short` empty) |
| Safety branch | `backup/signalnest-phase-1-2-pre-history-stitch` *(2026-10-05: exists only as a local branch on the operator machine, not on `origin`; retained pending `P6-GOV-7`)* |

The safety branch is retained **intentionally** as a pre-history-stitch snapshot. It is
**not** the active development branch and should not be built on; `main` is authoritative.

## Delivered architecture

The implemented stack, as present in the repository:

- **Frontend:** React 18 + TypeScript + Vite + React Router + TanStack Query v5 +
  React Hook Form/Zod + Tailwind + Radix.
- **Backend:** Python 3.12 + FastAPI modular monolith — DB, migrations, domain logic,
  pure scoring/geo/claims engines, REST, auth, in-process jobs *(true at acceptance: `InProcessQueue` ran each job synchronously in the API process; since `67ed438` (2026-07-13) scout runs use the durable job store and a separate worker — 2026-10-06)*.
- **Monorepo:** npm workspaces (`apps/web`, ~~`apps/api`,~~ `packages/*`) *(root `package.json` `workspaces` on `main` `c4315d8d`: `apps/web` and the `packages/*` glob only; no `packages/` directory has ever been tracked, and `apps/api` is a Python project driven by npm scripts, not a workspace — 2026-10-05)* *(2026-10-08 correction — inaccurate when written: at the accepted commit `b5965d35` and on `main` `1a24c4fb` the root `package.json` declares two workspace patterns, `apps/web` and the glob `packages/*`. The only workspace package is `apps/web` (`@signalnest/web`); no `packages/` path exists at either commit or in any commit reachable from `main` `1a24c4fb`, so the glob matches nothing. `apps/api` is a Python project (`apps/api/pyproject.toml`, no `package.json`) that root npm scripts drive through `scripts/` helpers (for example `scripts/run-api.sh` and `scripts/run-tests-api.sh`); it is not a workspace at either commit)*.
- **Default (zero-dependency) local mode:** SQLite, in-process queue, in-memory cache,
  numpy brute-force vector fallback, local-file storage, mock-first LLM.
- **Production adapters (implemented ~~behind `APP_MODE=full`~~, not necessarily deployed):** *(2026-10-06 correction — inaccurate when written: `APP_MODE` selects no adapter; each concern has its own setting — `database_url`, `queue_backend`, `cache_backend`, `vector_backend`, `storage_backend`, `llm_provider` — and `APP_MODE=full` only adds validation; see [`architecture.md`](architecture.md) "Dual-mode infrastructure".)*
  PostgreSQL, ~~pgvector~~, Redis, S3-compatible object storage, real LLM providers. *(2026-10-06 correction — inaccurate when written: pgvector was never an implemented adapter. `vector_backend=pgvector` is accepted by `apps/api/app/core/config.py`, but `build_index()` in `apps/api/app/infra/vector.py` returns the brute-force index unconditionally and has no callers, nothing imports the `pgvector` package and the `embedding` column is JSON — at `b5965d35` and on `main` `c5b48ed7`; see [`architecture.md`](architecture.md) and `P6-PLAT-1`. The other adapters are measured in the next note.)* *(2026-10-06, measured: at `8dca455e`/`b5965d35` the Redis queue, Redis cache and S3 storage adapters existed as classes inside their builder functions, each selected by its own setting; which application code calls them is recorded under "Dual-mode infrastructure" in [`architecture.md`](architecture.md). None of these adapters is verified against a live service here. This makes no ruling on production readiness.)*
- **Migrations & contracts:** Alembic migrations; generated `apps/api/openapi.json` and
  a TypeScript client ~~generated from the OpenAPI schema~~ *(2026-10-07 correction — inaccurate when written: the client in `apps/web/src/api/client.ts` and `endpoints.ts` is hand-written; what is generated from `openapi.json` is its types, `schema.d.ts` — see [`architecture.md`](architecture.md) "Topology")*.

> The production adapters exist ~~and are wired~~ behind env selection *(2026-10-06: pgvector excepted — it has no adapter, see the correction above)* *(2026-10-06 correction — "wired" was inaccurate when written for the cache and storage adapters: no module imported `app.infra.cache` or `app.infra.storage` at `b5965d35`. The scout run did enqueue through the queue adapter, but its Redis branch wrote to a stream that no process read, because no worker existed; the database engine and the LLM service were on the request and pipeline paths. Details: [`architecture.md`](architecture.md) "Dual-mode infrastructure".)*. This report does
> **not** claim any production service is deployed.

## Completed functional vertical slice

Legend: **[T] Implemented & tested** · **[A] Adapter-ready, not deployed** ·
**[P] Planned for Phase 3** *(at writing; see the dated note on the [P] item below)*.

### Phase 1 — foundation
- **[T]** Organization / workspace / brand / location model with server-side tenancy:
  ~~every query scoped by org/workspace/location;~~ ~~client-supplied tenant IDs never
  trusted~~ ~~(proven by integration tests)~~ *(2026-10-07 correction — inaccurate when written: no acceptance-time test or smoke step sent another tenant's identifiers; the integration tests prove per-location separation inside one workspace — see the correction after the Phase 1 items)* *(2026-10-06: the tests prove the cases they cover; the universal "every query" is not re-verified here — see [`architecture.md`](architecture.md) "Tenancy & security")*. *(2026-10-08 correction — inaccurate when written: the struck universal did not hold at `b5965d35` and does not on `main` `1a24c4fb`. The organization and workspace context is derived on the server and a scout request's `location_id` is checked against its workspace, but a scout request's `campaign_id` and `product_profile_id` and the `location_ids` / `eligible_location_ids` lists of campaigns and offers are stored as sent, with no workspace or membership check. The location-list gap is the open launch-blocker row `P6-PLAT-8` in [`docs/project-phase-6-plan.md`](project-phase-6-plan.md); this note changes no code. Measured detail: [`architecture.md`](architecture.md) "Tenancy & security", 2026-10-08 correction)* *(2026-10-08: the "every query" universal, struck above, was not established either — see the 2026-10-08 correction on it in [`architecture.md`](architecture.md) "Tenancy & security" for the measured scope and its limits)*
- **[T]** Multi-location support (Dallas TX, London UK, Lagos NG, Nairobi KE demo
  markets) ~~with strict per-location data isolation~~. *(2026-10-08 correction — inaccurate when written: a run's relevance-scoring context is not per-location — every scout run is classified against all of the workspace's audience labels and its relevance scored against all of the workspace's product, audience and competitor rows and all markets served, which the demo seed writes for all four cities — and what the tests show is that the opportunity feed's location filter returns only rows stored for that location, for the seeded locations; see [`architecture.md`](architecture.md) "Tenancy & security")*
- **[T]** Demo authentication flow (email/password + JWT), RBAC roles, ~~per-domain policy layers~~.
  *(2026-10-06 correction — inaccurate when written: there was no per-domain policy layer at `8dca455e`/`b5965d35`; roles were enforced by the `require_role` dependency and an inline membership check — see [`architecture.md`](architecture.md) "Tenancy & security".)*
- **[T]** Domain models + Alembic migrations for the Phase 1 tables.
- **[T]** Geography engine (haversine radius 1–200 mi, coverage, geo-relevance) — unit
  tested.
- **[T]** REST API — **56 operations across 41 paths** *(figure at acceptance; 85 paths / 106 operations at `2aa683d0`, 2026-10-03)* — with OpenAPI documentation.
- **[T]** Audit logging on sensitive actions.
- **[T]** Frontend app shell (workspace/location/~~campaign~~ switchers *(2026-10-07 correction — inaccurate when written: the shell at `b5965d35` had organization, workspace and location switchers and no campaign switcher)*, breadcrumbs, search,
  theme, responsive), local auth screens, protected routes.
- **[T]** Onboarding wizard covering every presence path (website / social / both / GBP /
  marketplace / offline / brand-new), with autosave + resume.
- **[T]** Campaign Context Center (products, audiences, competitors, brand voice, offers,
  claims, source/channel preferences, campaigns) with brand-wide + override model. *(2026-10-08: no scout run reads the per-location parts of this model — a campaign's `location_ids`, an offer's `eligible_location_ids` or a location's `local_competitors`; see [`architecture.md`](architecture.md) "Tenancy & security")*
- **[T]** Location & coverage configuration (multi-location manager + Scout Reach radius UI).
- **[T]** Typed API client ~~generated from OpenAPI~~ *(2026-10-07 correction — inaccurate when written: hand-written, with types generated from OpenAPI — see the correction under "Delivered architecture")*; TanStack Query hooks; loading/empty/
  error states.

*(2026-10-07 correction, measured at the accepted commit `b5965d35`, whose CI ran these automated checks: lint, type-check, 18 frontend tests and a production build; the CI failure-propagation regression test; ruff and 38 backend tests — engine unit tests plus four API tests against the seeded demo database (unauthenticated rejection, login, and two on per-location feed filtering); a migration round-trip with a schema-drift check and seed idempotency; the OpenAPI regeneration contract-drift check; and the 13-check HTTP smoke flow. Seeding runs the scout pipeline. None of these asserted the following parts of the [T] items above, so "tested" was inaccurate when written for them: the brand entity's API and scoping; scoping across organizations or workspaces and the refusal of client-supplied tenant IDs (no test or smoke step sent another tenant's identifiers; what the integration tests prove is per-location separation inside one workspace); role enforcement (only the rejection of unauthenticated requests is tested); audit logging; the geography engine's 1–200 mi radius range (enforced by the location schema; the tests use 25 mi); the organization/workspace switcher, breadcrumbs, header search, theme toggle and mobile layout; the Register screen; the onboarding presence paths other than "brand new", and resume (autosave is tested); the Campaign Context sections other than products and claims, and its brand-wide/per-location model; the Scout Reach radius control (the geography engine behind it is unit-tested); and the loading and error states (an empty state is tested).)* *(2026-10-08: the refusal of client-supplied tenant IDs named above also did not hold for every identifier — see the 2026-10-08 correction on the tenancy item above.)*

### Phase 2 — scouting → explainable opportunities
- **[T]** Scout request workflow: create / configure / pause / resume / run / review,
  ~~isolated per workspace+brand+location+market+campaign~~. *(2026-10-08 correction — inaccurate when written: each request records its workspace, brand, location, market and campaign, but every run's classification and relevance scoring use the workspace's product, audience and competitor rows and its business profile's markets served rather than location-specific context, the campaign is stored without a workspace check and the market can be typed — see [`architecture.md`](architecture.md) "Tenancy & security")*
- **[A]** Fixture-based connectors clearly labeled "Simulated"; live connectors
  (Reddit, reviews, Trends, Meta Ad Library, TikTok, RSS/news) exist as ~~adapter~~
  placeholders only *(2026-10-07 correction — inaccurate when written: no per-source adapter existed at `b5965d35`; `apps/api/app/scouting_requests/connectors.py` defined one generic `Connector` protocol, the `FixtureConnector` and `get_connector()`, and the listed sources existed only as `SourceType` values)* *(2026-10-05: `apps/api/app/connectors/` now holds the connector framework
  and an RSS parser with a sandbox provider; live egress is not wired on `main` — draft PR #34;
  plan gate E5 is not met)*.
- **[T]** Canonical signal model, normalization, dedupe/cluster, classification.
- **[T]** Noise gate ("collect broadly, notify selectively") — unit tested.
- **[T]** Relevance engine with the **<40 ⇒ never recommend action** hard rule — unit tested.
- **[T]** Geo-relevance resolution with evidence + confidence + `inside_scout_area`.
- **[T]** Validation → classification bands — unit tested.
- **[T]** Opportunity + confidence scoring (weighted, explainable breakdowns) — unit tested.
- **[T]** Decision engine (Act now / Act soon / Monitor / Archive / Stay silent / Block)
  — unit tested.
- **[T]** Explanation engine separating **Observed evidence / AI inference / Recommended
  action**, with claim-safety guard.
- **[T]** Opportunity Feed (scores, confidence, risk, filters, sorting, search, strict
  per-location separation) — isolation proven by tests on all four cities. *(2026-10-08 clarification: the tests assert that the feed's location filter returns only opportunities stored for that location — the API test over the seeded locations, the smoke flow over the four cities and the frontend test against a mocked backend; they do not test how a run selects signals for a market)*
- **[T]** Opportunity Detail (evidence vs inference, traceable source URLs, geo evidence,
  claim warnings, simulated/known-limitation disclosures, status controls).
- **[T]** HTTP smoke flow proving four-market isolation over the real API. *(2026-10-08 clarification: its isolation step checks that each city's location-filtered feed is non-empty, returns only rows of that location, and that no row appears under two locations; its "all in-market" message does not inspect markets)*
- **[A]** LLM is mock-first by default; OpenAI/Anthropic adapters behind env, not
  exercised in the demo.
- **[P]** Creative generation, approvals, analytics, live integrations, billing — ~~stubs routing to "coming in Phase 3"~~ *(2026-10-07 correction — inaccurate when written: at `b5965d35` no route or stub existed for any of these; creative generation appeared only as notices that it "arrives in Phase 3", in the sidebar and on the opportunity detail page, and two Campaign Context descriptions mention Phase 3)*
  *(2026-10-05: ~~this scope became the Phase 5A–5E guided-action
  work — [`docs/project-phase-5-plan.md`](project-phase-5-plan.md) — excluded from Phase 6 by
  resolved decision `P6-D01`;~~ Phase 3 as executed delivered, ~~dark,~~ the 3B scouting work — connector
  foundation with an RSS sandbox, signal intelligence and opportunity scoring, scouting schedules —
  and the 3C feedback loop on top of the 3A runtime foundation; the closeout record lists the
  scheduling PRs #48–#51 and the 3C PRs, the connector and intelligence work is in git history —
  fe78b39, #35)*. *(2026-10-07 correction — inaccurate when written: this 2026-10-05 annotation, not the original acceptance text, called the work "dark", meaning feature-flagged off by default. On `c4315d8d`, which it was written against, and on `main` `61f49eb8`, the RSS connector, scouting schedules and the 3C feedback loop were off by default through actual flags (`connector_rss_enabled`, `scout_scheduling_enabled`, `opportunity_feedback_enabled`, each `False` in `apps/api/app/core/config.py`; since Phase 4B-A, #88 of 2026-07-21, a per-workspace override can enable feedback), but signal intelligence and its opportunity scoring had no flag: for each signal its connector returns, the scout pipeline analyses and scores it and stores the result, fail-open — a fault is logged and leaves no intelligence record (`apps/api/app/jobs/pipeline.py:229`, `:268-269`) — and the intelligence API route and the opportunity detail panel have no flag check. Of the 3A runtime foundation, the durable job store and worker have no flag, and span emission is off by default (see the phase-3-plan correction). Measured evidence and enforcement points: the 2026-10-07 correction in [`docs/phase-3-plan.md`](phase-3-plan.md). Repository behaviour only, not deployment or live exposure)* *(2026-10-08 correction — inaccurate when written: the struck part of this 2026-10-05 annotation sends all five areas to the Phase 5A–5E work that `P6-D01` excludes. On `c4315d8d`, which it was written against, and on `main` `1a24c4fb`: approvals, text creative generation and derived status analytics are Phase 5B, 5C and 5E scope, which `P6-D01` excludes from Phase 6; the Phase 5 plan excludes billing, live ad-platform integration, new external connectors and image, video and audio generation from all of Phase 5 (its §2); billing is out of Phase 6 under `P6-D02`, the unpaid-pilot decision (row `P6-COM-1`); and live data enablement stays in Phase 6 — its exit item E5 requires a real connector to return real signals for at least one market that the operator does not control and that is not a fixture market, and E11 requires the reworked Integration smoke gate (`P6-CI-11`) to assert against real data, while connector breadth beyond that first source (`P6-DATA-5`) is deferred to Phase 7. Section references: the 2026-10-08 correction in [`docs/phase-3-plan.md`](phase-3-plan.md). Scope routing only)*

*(2026-10-07 correction, measured at `b5965d35` against the automated checks listed after the Phase 1 items: "tested" was inaccurate when written for these parts of the [T] items above, which none of them asserted: scout creation (the create dialog, which is also where a scout is configured), pause and resume, and the isolation of scout requests by workspace, brand, market and campaign — listing, running and the detail view are tested, and opportunity results are tested for separation by location; the opportunity and confidence scores' per-factor breakdowns (their totals and bands are tested); the decision engine's Archive outcome (the other five are tested); the feed's confidence and risk display, sorting, risk filter and search filtering (the score labels, the search box and its reset on a location switch are tested); and the detail page's Recommended action section, geo evidence and claim warnings. Signal normalization, dedupe/clustering, classification and the explanation step have no unit tests: they run inside the scout pipeline during seeding, the API tests then read its results, and the smoke flow requires at least one in-market opportunity per city; the frontend test of the evidence/inference split runs against fixture data, and the claim-safety engine is unit-tested. Nothing notified at `b5965d35`: "notify selectively" is the noise gate's motto, and the notifications menu was a placeholder.)*

## Security and dependency status

*All figures in this section are the acceptance-time measurements (2026-07-12). For the
2026-10-05 state see [`docs/project-phase-6-plan.md`](project-phase-6-plan.md) §19 item 12 and
rows `P6-CI-6`, `P6-CI-7`, `P6-PLAT-4`.*

| Control / package | State |
| --- | --- |
| GitHub secret scanning | enabled |
| Push protection | enabled |
| Dependabot security updates | enabled |
| Open Dependabot security alerts | **0** at acceptance |
| `npm audit` | **0 vulnerabilities** |
| Vite | `7.3.6` |
| esbuild | `0.28.1` |
| `@vitejs/plugin-react` | `5.2.0` |
| TypeScript | `5.9.3` (unchanged; TS 7 deferred) |

Completed remediation sequence (summary):

- Critical Vitest and Vite advisories addressed.
- Nested vulnerable Vite/esbuild versions removed from the dependency tree.
- **No `npm` overrides** were used.
- **No unsupported peer forcing** (`--legacy-peer-deps` / `--force`) was used.

## CI reliability correction

Earlier `command | tee log` pipelines could **hide failures** because the shell did not
enable `pipefail` — the pipeline reported `tee`'s (successful) exit status, masking a
failing quality command as green.

Fix:

- The workflow now runs every step under a strict shell:

  ```
  shell: bash --noprofile --norc -euo pipefail {0}
  ```

- A maintained regression test verifies failure propagation:

  ```bash
  npm run test:ci-pipefail
  ```

- Required checks are now trustworthy.
- Diagnostic log uploads remain **conditional on failure** only.

> CI runs that predate this fix must not be treated as reliable acceptance evidence.
> Acceptance evidence below is from the accepted commit, after the fix.

## ~~Current~~ GitHub Actions versions at acceptance

| Action | Version on `main` at acceptance |
| --- | --- |
| `actions/checkout` | `v7` |
| `actions/setup-node` | `v6` |
| `actions/upload-artifact` | `v7` |
| `actions/setup-python` | `v6` |

`actions/setup-python` was upgraded from v5 to **v6** (PR #19, squash commit
`b5965d354a0c2335c2ac9cf283fd28b56d8d612d`). v6:

- runs internally on **Node 24** (its own action runtime; this is unrelated to
  SignalNest's application/frontend runtime, which remains **Node 20**),
- continues to install the configured **Python 3.12** runtime (CI installs CPython
  3.12.13),
- preserves pip caching and `cache-dependency-path` behavior,
- introduced **no workflow-permission change** (jobs remain `contents: read`), and
- **removed the previous Node-20 action-runtime deprecation annotation** — no CI
  annotations remain *(true at acceptance: run 29215104167 on `b5965d35` carried none; stale since —
  see the 2026-10-06 note under "Completed maintenance")*.

## Frontend lint-toolchain migration

Accepted toolchain:

| Package | Version |
| --- | --- |
| ESLint | `10.7.0` (native flat config) |
| typescript-eslint | `8.63.0` |
| eslint-plugin-react-hooks | `7.1.1` |
| eslint-plugin-react-refresh | `0.5.3` |
| TypeScript | `5.9.3` |

- `.eslintrc.cjs` **removed**; `eslint.config.js` (flat config) **added**.
- React Hooks findings (React Compiler rule suite, incl. `set-state-in-effect` /
  `set-state-in-render`) were **remediated in source** — effect-driven state updates were
  converted to guarded render-phase updates, not silenced.
- **No broad rule disabling** was introduced.
- The React Refresh exception is **narrowly limited to UI primitive modules**
  (`src/components/ui/**`).
- Frontend test count is now **18** *(at acceptance; 763 in the Frontend quality job of CI run
  37228710926 on `main` `c4315d8d`, 2026-10-04)*.
- **No duplicate ESLint major and no invalid peers** remain.

## Final quality evidence *(at the accepted commit)*

| Check | Command | Result |
| --- | --- | --- |
| Deterministic install | `npm ci` | pass |
| Security audit | `npm audit` | **0 vulnerabilities** |
| CI pipefail regression | `npm run test:ci-pipefail` | pass |
| Lint (web) | `npm run lint` (`--max-warnings 0`) | **0 errors, 0 warnings** |
| Type check (web) | `npm run type-check` | pass |
| Frontend tests | `npm test` | **18/18** |
| Backend tests | `npm run test:api` | **38/38** |
| Ruff (api) | `ruff check` | pass |
| Production build | `npm run build` | pass (chunk-size warning only) |
| OpenAPI/type generation | `npm run gen:types` | no drift |
| Alembic | migration check | no schema drift |
| HTTP smoke | `npm run smoke` | **13/13** |
| Four-market isolation *(2026-10-08: the location-filter checks described under the HTTP smoke item above)* | smoke + RTL | pass |
| Latest `main` CI | four jobs | all passing (no annotations) |

Latest verified CI run for the ~~current~~ accepted commit *(at this report)*
(`b5965d354a0c2335c2ac9cf283fd28b56d8d612d`):

- <https://github.com/bolade04/signal_nest/actions/runs/29215104167> — workflow **CI**,
  jobs **Frontend quality**, **Backend quality**, **Migrations and API contract**,
  **Integration smoke** all `success`, with **no remaining CI annotations**.

## Governance and protection

Final ruleset state (restored and active at acceptance):

| Property | Value |
| --- | --- |
| Ruleset ID | `18820692` |
| Name | `main protection` |
| Enforcement | active |
| Required approvals | 1 |
| Dismiss stale approvals on push | yes |
| Last-push approval required | yes |
| Review-thread resolution required | yes |
| Required status checks (strict) | Frontend quality · Backend quality · Migrations and API contract · Integration smoke |
| Bypass actors | none |
| Force pushes | blocked |
| Branch deletion | blocked |

*2026-10-05: ruleset 18820692 is still active and now requires **six** contexts — Frontend
quality · Backend quality · Migrations and API contract · Integration smoke · Container build and
security · Revision reader (unit, IaC contract, in-image) — with 1 approval, last-push approval
and review-thread resolution (`P6-CI-1` closed; plan §4). The legacy branch-protection endpoint
still returns 404 (`P6-GOV-6`).*

A temporary review-rule relaxation was used for owner-authored PRs and **restored
immediately, with no administrator bypass**. The authoritative final state is fully
restored protection as tabulated above.

## Known accepted limitations

- Large frontend bundle/chunk warning remains (single chunk >500 kB; no route-level
  code splitting) — non-blocking.
- Backend Pydantic v2 class-based `Config` deprecation warnings remain — non-blocking.
- Production infrastructure adapters (PostgreSQL/~~pgvector~~/Redis/S3) are implemented but
  not necessarily deployed. *(2026-10-06 correction — inaccurate when written for pgvector, which
  has no adapter; see the dated correction under "Delivered architecture". The other adapters'
  implementation, selection, call paths and tests are measured in [`architecture.md`](architecture.md)
  "Dual-mode infrastructure"; none is verified against a live service here.)*
- Real external AI/provider integrations may still use mock-first behavior.
- Live external data connectors are fixture-based ("Simulated") placeholders *(2026-10-05: the
  connector framework and an RSS sandbox exist; live egress is not wired — see the [A] note above)*.
- Auth is the local email/password + JWT provider only (no SSO/OAuth, refresh rotation,
  or rate-limit backend).
- ~~Phase 3 features are not yet implemented.~~ *(Implemented and closed ~~dark~~ after acceptance —
  see the dated note under "Executive acceptance status".)* *(2026-10-07 correction — inaccurate when written: not every Phase 3 feature was off by default; see the 2026-10-07 correction to that note)*
- TypeScript 7 upgrade (Dependabot PR #6) is intentionally deferred.

### Completed maintenance
- `actions/setup-python` was upgraded to **v6** (PR #19), and the prior Node-runtime
  deprecation annotation is **no longer present**
  ~~— this is no longer an active limitation~~ *(true at acceptance; stale since)*.
  *(2026-10-06: a Node-runtime deprecation warning is active again, from other actions. CI run
  [37445036772](https://github.com/bolade04/signal_nest/actions/runs/37445036772) — push to `main`
  `c5b48ed7`, 2026-10-06, every job concluded success — carries the warning annotation "Node.js 20 is
  deprecated. The following actions target Node.js 20 but are being forced to run on Node.js 24" on
  two jobs: Container build and security (`docker/build-push-action@v6`,
  `docker/setup-buildx-action@v3`) and Revision reader (`docker/build-push-action@v6`,
  `opentofu/setup-opentofu@v1`); run 37228710926 on `c4315d8d` (2026-10-04) carries the same two
  warnings. Both jobs were added after acceptance, and no annotation on those runs names
  `actions/setup-python`. These are warnings about the runtime GitHub uses for those third-party
  actions — not CI failures — and they are separate from the application's own Node toolchain
  (`package.json` `engines` `node >=20 <21`; `ci.yml` `NODE_VERSION: '20'`), which this note does
  not assess. No runtime or workflow upgrade is made here.)*
  *(2026-10-07 addition: the runs named above also carry a notice-level annotation, "The ubuntu-latest label will migrate to Ubuntu 26 beginning October 19, 2026", on every job of run 37228710926 and on four of the six jobs of run 37445036772; it concerns the runner image, not an action's Node runtime, and nothing here assesses it.)*

## Review checklist (for a human reviewer)

1. `npm run bootstrap && npm run demo:setup && npm run dev`.
2. Sign in as `demo@signalnest.dev` / `demo1234`.
3. Walk onboarding via a no-website path; confirm autosave/resume.
4. Open Campaign Context; add a product; confirm brand-wide wording.
5. Open Scout Requests; run a scout; confirm completion + simulated-source disclosure.
6. Open Opportunities; switch the active location across all four cities; confirm each
   shows only its own market. Filter by classification.
7. Open an opportunity; confirm Observed evidence vs AI inference separation, a
   traceable source link, score breakdowns, and simulated/known-limitation labels;
   change its status.
8. Run `npm test` and `npm run test:api`; confirm both green.
