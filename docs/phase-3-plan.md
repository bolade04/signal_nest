# Phase 3+ Implementation Plan (out of scope for this pass)

Phases 1–2 delivered the foundation and the scouting → explainable-opportunity
pipeline. This document sketches the remaining product surface so the stubs shipped in
this pass have a clear destination. Nothing here is implemented yet; Phase 3 buttons
in the UI render as clearly-labeled "coming in Phase 3" routes.

## Phase 3 — Creative generation & claims-safe authoring

- **Creative generation** from an opportunity: ad copy, angles, and variants driven by
  the LLM abstraction, seeded from the opportunity's observed evidence + audience/
  product fit. Deterministic mock first; OpenAI/Anthropic behind env.
- **Claims-safe authoring loop.** Full authoring safety (beyond the Phase 2 reasoning
  guard): every generated claim checked against the Claims Library + industry rules,
  with blocking, rewrite suggestions, and a Compliance Reviewer approval gate.
- **Approval workflow.** Draft → review → approve/reject with roles (Marketer author,
  Reviewer/Compliance Reviewer approver), audit trail, and versioned revisions.
- **Asset library.** Uploads + analysis (brand-safety, on-brand checks), reusable in
  generated creative.
- **Offer authoring → ad use.** Turn Offer Calendar entries into campaign-ready offers.

## Phase 4 — Publishing, growth & analytics

- **Publishing/scheduling** to channels; calendar and queue.
- **Website growth authoring** (landing/section copy) from opportunities.
- **Analytics/ROI dashboard** — performance, attribution, spend vs. outcome.
- **Learning loop** — feed outcomes back into scoring/relevance priors.

## Phase 5 — Live integrations & billing

- **Live connectors** implementing the existing adapter interface: Reddit, reviews,
  Google Trends, Meta Ad Library, TikTok Creative Center, RSS/news, competitor/website
  scans (replacing the Phase 2 fixtures).
- **Billing & plans** — metering, quotas, subscription management.
- **SSO/OAuth** auth providers alongside the local email/password provider.

## Cross-cutting hardening (any phase)

- Route-level code splitting to break up the web bundle.
- Isolated per-test databases for API integration tests.
- Migrate remaining Pydantic v2 class-based `Config` to `ConfigDict`.
- Real rate-limiting backend behind the existing placeholder middleware.
- LLM contract tests across mock/openai/anthropic proving identical normalized shapes.
