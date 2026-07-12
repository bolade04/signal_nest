# Testing

Two independent suites, each runnable on its own.

| Suite | Command | Count | Stack |
| --- | --- | --- | --- |
| Frontend | `npm test` | 16 | Vitest + React Testing Library + MSW v2 |
| Backend | `npm run test:api` | 38 | pytest |

## Backend (`apps/api`)

Pure-engine unit tests need no FastAPI or database and run in milliseconds.

- **`test_scoring_engines.py`**
  - Relevance: strong match clears the action floor; off-topic falls below it;
    exclusion terms hard-kill relevance.
  - Opportunity/confidence: weighted totals stay within 0–100; strong signals score
    high, weak ones low; classification and confidence bands; confidence rewards
    evidence quantity, diversity, and score consistency.
  - Noise gate: spam and bot authors are hard noise; a quality signal clears the
    full-analysis threshold; short content is penalized for low context.
  - Validation: cross-source agreement dominates; total caps at 100.
- **`test_decision_and_geo.py`**
  - Decision engine: blocked risk blocks; noise stays silent; out-of-area monitors;
    **relevance < 40 never recommends action**; unclear audience fit monitors;
    high-priority + high confidence acts now; validated acts soon.
  - Geography: haversine sanity (Dallas→London ≈ 4.9k mi); radius inside/outside;
    unevaluable radius returns `None`; explicit exclusions win; online-global covers
    everything; geo-relevance resolves the strongest-evidence market with confidence.
  - Claim safety: health claims flagged high; financial claims blocked; explicit
    blocked-claim list; strict industries escalate medium→high; clean text is low
    risk; competitor-weakness translation stays safe.
- **`test_api_isolation.py`** (integration — drives the real FastAPI app via
  `TestClient` against the seeded SQLite DB)
  - Unauthenticated requests are rejected (401/403).
  - A valid demo login yields a usable bearer token.
  - **Per-location isolation:** querying opportunities by `location_id` only ever
    returns that location's rows, and no opportunity id appears under two locations.
  - The unfiltered feed is a superset of every location's filtered feed.

  These tests require a migrated + seeded database (`npm run demo:setup`). If the demo
  data is absent, the module skips itself rather than failing spuriously.

## Frontend (`apps/web`)

Component/integration tests render the real `App` with a small in-memory MSW backend
(`src/test/handlers.ts`) modeling one org → one workspace → one brand → four
independent cities.

- **`auth.test.tsx`** — sign-in flow and protected-route redirects.
- **`onboarding.test.tsx`** — the wizard offers every presence path, gates continue
  on a business name, and autosaves the draft to `localStorage`.
- **`campaign-context.test.tsx`** — context tabs, empty state, add-product dialog.
- **`scout-requests.test.tsx`** — lists all requests with status, runs a scout to
  completion, and opens a detail page with its configuration and simulated-source
  disclosure.
- **`opportunities.test.tsx`** — renders scored cards with human-readable score labels
  and simulated badges; **proves strict per-location isolation** (Dallas shows only
  Dallas; London only London); filters by classification.
- **`opportunity-detail.test.tsx`** — separates observed evidence from AI inference,
  exposes a traceable source link, surfaces known-limitation/simulated disclosures,
  and lets the user change status.

## Also verified

- `npm run type-check` — strict `tsc -b --noEmit`, clean.
- `npm run lint` — ESLint with `--max-warnings 0`, clean.
- `npm run build` — production web build succeeds (single large-chunk warning only).
