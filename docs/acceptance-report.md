# Phase 1–2 Acceptance Report

Scope: foundation (Phase 1) + scouting → explainable opportunities (Phase 2), plus the
security-remediation, CI-reliability, GitHub-Actions, and frontend-lint-toolchain work
that followed. Phase 3+ is intentionally out of scope; see
[`phase-3-plan.md`](phase-3-plan.md).

## Executive acceptance status

- **Phase 1–2 is complete and accepted.** The production-style vertical slice is
  implemented end to end (foundation → scouting → explainable, scored opportunities).
- **`main` is green.** The latest CI run for the accepted commit passes all four
  required jobs.
- **Security advisories are remediated.** `npm audit` reports zero vulnerabilities and
  there are zero open Dependabot security alerts.
- **CI quality checks now propagate real failures.** The pipefail masking bug is fixed
  and guarded by a regression test.
- **Frontend lint tooling is migrated and stable** (ESLint flat config on ESLint 10).
- **Phase 3 has not started.** All Phase 3 surface remains stubbed/planned only.

## Final repository baseline

| Item | Value |
| --- | --- |
| Repository | `bolade04/signal_nest` |
| Default branch | `main` |
| Current accepted & maintained `main` SHA | `b5965d354a0c2335c2ac9cf283fd28b56d8d612d` |
| Original Phase 1–2 implementation squash commit | `8dca455e9592fdec959e57e6d9f741007f421f5f` |
| Working tree at acceptance | clean (no tracked changes; `git status --short` empty) |
| Safety branch | `backup/signalnest-phase-1-2-pre-history-stitch` |

The safety branch is retained **intentionally** as a pre-history-stitch snapshot. It is
**not** the active development branch and should not be built on; `main` is authoritative.

## Delivered architecture

The implemented stack, as present in the repository:

- **Frontend:** React 18 + TypeScript + Vite + React Router + TanStack Query v5 +
  React Hook Form/Zod + Tailwind + Radix.
- **Backend:** Python 3.12 + FastAPI modular monolith — DB, migrations, domain logic,
  pure scoring/geo/claims engines, REST, auth, in-process jobs.
- **Monorepo:** npm workspaces (`apps/web`, `apps/api`, `packages/*`).
- **Default (zero-dependency) local mode:** SQLite, in-process queue, in-memory cache,
  numpy brute-force vector fallback, local-file storage, mock-first LLM.
- **Production adapters (implemented behind `APP_MODE=full`, not necessarily deployed):**
  PostgreSQL, pgvector, Redis, S3-compatible object storage, real LLM providers.
- **Migrations & contracts:** Alembic migrations; generated `apps/api/openapi.json` and
  a TypeScript client generated from the OpenAPI schema.

> The production adapters exist and are wired behind env selection. This report does
> **not** claim any production service is deployed.

## Completed functional vertical slice

Legend: **[T] Implemented & tested** · **[A] Adapter-ready, not deployed** ·
**[P] Planned for Phase 3**.

### Phase 1 — foundation
- **[T]** Organization / workspace / brand / location model with server-side tenancy:
  every query scoped by org/workspace/location; client-supplied tenant IDs never
  trusted (proven by integration tests).
- **[T]** Multi-location support (Dallas TX, London UK, Lagos NG, Nairobi KE demo
  markets) with strict per-location data isolation.
- **[T]** Demo authentication flow (email/password + JWT), RBAC roles, per-domain policy
  layers.
- **[T]** Domain models + Alembic migrations for the Phase 1 tables.
- **[T]** Geography engine (haversine radius 1–200 mi, coverage, geo-relevance) — unit
  tested.
- **[T]** REST API — **56 operations across 41 paths** — with OpenAPI documentation.
- **[T]** Audit logging on sensitive actions.
- **[T]** Frontend app shell (workspace/location/campaign switchers, breadcrumbs, search,
  theme, responsive), local auth screens, protected routes.
- **[T]** Onboarding wizard covering every presence path (website / social / both / GBP /
  marketplace / offline / brand-new), with autosave + resume.
- **[T]** Campaign Context Center (products, audiences, competitors, brand voice, offers,
  claims, source/channel preferences, campaigns) with brand-wide + override model.
- **[T]** Location & coverage configuration (multi-location manager + Scout Reach radius UI).
- **[T]** Typed API client generated from OpenAPI; TanStack Query hooks; loading/empty/
  error states.

### Phase 2 — scouting → explainable opportunities
- **[T]** Scout request workflow: create / configure / pause / resume / run / review,
  isolated per workspace+brand+location+market+campaign.
- **[A]** Fixture-based connectors clearly labeled "Simulated"; live connectors
  (Reddit, reviews, Trends, Meta Ad Library, TikTok, RSS/news) exist as adapter
  placeholders only.
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
  per-location separation) — isolation proven by tests on all four cities.
- **[T]** Opportunity Detail (evidence vs inference, traceable source URLs, geo evidence,
  claim warnings, simulated/known-limitation disclosures, status controls).
- **[T]** HTTP smoke flow proving four-market isolation over the real API.
- **[A]** LLM is mock-first by default; OpenAI/Anthropic adapters behind env, not
  exercised in the demo.
- **[P]** Creative generation, approvals, analytics, live integrations, billing — stubs
  routing to "coming in Phase 3".

## Security and dependency status

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

## Current GitHub Actions versions

| Action | Version on `main` |
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
  annotations remain.

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
- Frontend test count is now **18**.
- **No duplicate ESLint major and no invalid peers** remain.

## Final quality evidence

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
| Four-market isolation | smoke + RTL | pass |
| Latest `main` CI | four jobs | all passing (no annotations) |

Latest verified CI run for the current accepted commit
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

A temporary review-rule relaxation was used for owner-authored PRs and **restored
immediately, with no administrator bypass**. The authoritative final state is fully
restored protection as tabulated above.

## Known accepted limitations

- Large frontend bundle/chunk warning remains (single chunk >500 kB; no route-level
  code splitting) — non-blocking.
- Backend Pydantic v2 class-based `Config` deprecation warnings remain — non-blocking.
- Production infrastructure adapters (PostgreSQL/pgvector/Redis/S3) are implemented but
  not necessarily deployed.
- Real external AI/provider integrations may still use mock-first behavior.
- Live external data connectors are fixture-based ("Simulated") placeholders.
- Auth is the local email/password + JWT provider only (no SSO/OAuth, refresh rotation,
  or rate-limit backend).
- Phase 3 features are not yet implemented.
- TypeScript 7 upgrade (Dependabot PR #6) is intentionally deferred.

### Completed maintenance
- `actions/setup-python` was upgraded to **v6** (PR #19), and the prior Node-runtime
  deprecation annotation is **no longer present** — this is no longer an active
  limitation.

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
