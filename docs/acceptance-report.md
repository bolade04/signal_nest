# Phase 1–2 Acceptance Report

Scope: foundation (Phase 1) + scouting → explainable opportunities (Phase 2).
Phases 3–5 are intentionally out of scope; see [`phase-3-plan.md`](phase-3-plan.md).

## Verification summary

| Check | Command | Result |
| --- | --- | --- |
| Backend tests | `npm run test:api` | **38 passed** |
| Frontend tests | `npm test` | **16 passed** |
| Type check (web) | `npm run type-check` | clean |
| Lint (web) | `npm run lint` (`--max-warnings 0`) | clean |
| Production build | `npm run build` | succeeds (chunk-size warning only) |
| Schema/type sync | `npm run gen:types` | regenerates cleanly |

## Requirement checklist

### Phase 1 — foundation
- [x] Dual-mode infra behind adapters (SQLite/in-process/in-memory/local-fs/mock by
      default; Postgres+pgvector/Redis/S3/real-LLM under `APP_MODE=full`).
- [x] Auth (email/password + JWT), RBAC roles, per-domain policy layers.
- [x] Server-side tenancy: every query scoped by org/workspace/location; client tenant
      IDs never trusted. **Proven by integration tests.**
- [x] Domain models + Alembic migrations for the Phase 1 tables.
- [x] Geography engine (haversine radius 1–200 mi, coverage, geo-relevance). **Unit
      tested.**
- [x] REST endpoints (56 operations / 41 paths) with OpenAPI documentation.
- [x] Audit logging on sensitive actions.
- [x] Frontend app shell (workspace/location/campaign switchers, breadcrumbs, search,
      theme, responsive), local auth screens, protected routes.
- [x] Onboarding wizard covering every presence path (website / social / both / GBP /
      marketplace / offline / brand-new), with autosave + resume.
- [x] Campaign Context Center (products, audiences, competitors, brand voice, offers,
      claims, source/channel preferences, campaigns) with brand-wide + override model.
- [x] Multi-location manager + Scout Reach radius UI.
- [x] Typed API client generated from OpenAPI; TanStack Query hooks; loading/empty/
      error states.

### Phase 2 — scouting → explainable opportunities
- [x] Scout requests: create / configure / pause / resume / run / review, isolated per
      workspace+brand+location+market+campaign.
- [x] Fixture-based connectors clearly labeled "simulated".
- [x] Canonical signal model, normalization, dedupe/cluster, classification.
- [x] Noise gate ("collect broadly, notify selectively"). **Unit tested.**
- [x] Relevance engine with the **<40 ⇒ never recommend action** hard rule. **Unit
      tested.**
- [x] Geo-relevance resolution with evidence + confidence + `inside_scout_area`.
- [x] Validation → classification bands. **Unit tested.**
- [x] Opportunity + confidence scoring (weighted, explainable breakdowns). **Unit
      tested.**
- [x] Decision engine (Act now / Act soon / Monitor / Archive / Stay silent / Block).
      **Unit tested.**
- [x] Explanation engine separating **Observed evidence / AI inference / Recommended
      action**, with claim-safety guard.
- [x] Opportunity Feed (scores, confidence, risk, filters, sorting, search, strict
      per-location separation). **Isolation proven by tests on 4 cities.**
- [x] Opportunity Detail (evidence vs inference, traceable source URLs, geo evidence,
      claim warnings, known-limitation + simulated disclosures, status controls).
- [x] Multi-business, multi-location fixtures (Dallas / London / Lagos / Nairobi).

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

## Known limitations

- **Simulated data.** All signal sources are fixture-based and clearly labeled
  "Simulated"; no live connectors (Reddit, reviews, Trends, Meta Ad Library, TikTok)
  are wired yet — they exist as adapter placeholders.
- **LLM is mock by default.** Classification/explanation use a deterministic offline
  mock; OpenAI/Anthropic adapters exist behind env but are not exercised in the demo.
- **Integration tests read seeded state.** `test_api_isolation.py` runs against the
  shared seeded SQLite DB and skips if demo data is absent; it does not create/tear
  down an isolated database per test.
- **Auth is the local provider.** Email/password + JWT only; no SSO/OAuth, refresh
  rotation, or rate-limit backend (a placeholder middleware is present).
- **Web bundle is a single chunk** (>500 kB) — no route-level code splitting yet.
- **Pydantic v2 deprecation warnings** remain for class-based `Config` in some schemas
  (non-blocking).
- **Phase 3+ features are stubs**, routing to a clearly-labeled "coming in Phase 3".
