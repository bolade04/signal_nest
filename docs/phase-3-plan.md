# Phase 3 Implementation Plan

**Status: planned, not started.** Phase 1–2 is complete and accepted (see
[`acceptance-report.md`](acceptance-report.md)). This plan starts from that accepted
baseline and does **not** repeat Phase 1–2 work.

## Ground rules

- Phase 3 is **planned but not started**. No Phase 3 code exists yet; UI entry points for
  Phase 3 render as clearly-labeled "coming in Phase 3" stubs.
- **`main` must remain stable and releasable throughout Phase 3.**
- New work ships in **small vertical slices behind protected PRs** (the `main protection`
  ruleset, ID `18820692`, stays active).
- **Every slice must preserve strict market/request isolation** (Dallas / London / Lagos /
  Nairobi and any future market never blend unless the user explicitly opts in).

## Accepted baseline (entry state)

| Item | Value |
| --- | --- |
| Accepted `main` SHA | `73c21aca819f680aa986160dba3da4f32f8981a8` |
| Phase 1–2 squash commit | `8dca455e9592fdec959e57e6d9f741007f421f5f` |
| Security alerts / `npm audit` | 0 / 0 |
| Required checks | Frontend quality · Backend quality · Migrations and API contract · Integration smoke |
| Frontend toolchain | ESLint 10.7.0 flat config · typescript-eslint 8.63.0 · TS 5.9.3 |

---

## Phase 3 goal

Deliver **production-grade AI scouting and actionable marketing intelligence**: real
data-source connectors, a continuous scouting pipeline, a production scoring framework,
evidence-backed opportunity reports, a recommendation-to-creative workflow, and the
production infrastructure/tenancy/compliance foundations to run it safely — all while
preserving the Phase 1–2 isolation and explainability guarantees.

## Phase 3 workstreams

### A. Production data-source connectors

Plan connectors for approved and lawful sources: website crawling; RSS/news feeds;
search-derived sources; Reddit/forum sources where APIs and policies permit; YouTube
transcripts/metadata; connected customer websites; Google Search Console; Google
Analytics; Google Business Profile; social platform APIs where authorized; premium
social-listening providers where native APIs are insufficient.

Every connector requires: legal/policy review · rate limiting · credential isolation ·
source attribution · retry and backoff · failure classification · data-retention rules ·
jurisdiction filters · per-location isolation · mock/sandbox support.

> This plan does **not** claim unrestricted scraping of all social platforms. Each
> connector is gated on terms-of-service and legal feasibility.

### B. Continuous scouting pipeline

Scheduled and event-triggered scout runs → ingestion → normalization → deduplication →
language detection → location/jurisdiction inference → entity extraction → topic
clustering → trend detection → competitor-gap detection → pain-point scoring →
opportunity ranking → evidence packaging → human review.

Require idempotency, retries, audit trails, and job observability throughout.

### C. SignalNest scoring and "secret sauce"

A production scoring framework that may combine: relevance to the customer's
products/services · pain intensity · recency · engagement velocity · geographic fit ·
commercial intent · competitive whitespace · brand safety · source credibility ·
confidence · saturation · actionability.

Keep scoring components **configurable and independently testable**. Do not expose
confidential weighting choices unnecessarily in customer-facing documentation.

### D. Evidence-backed opportunity reports

The next version of opportunity results should include: what was found · why it matters ·
source links and attribution · geography · target audience · confidence · urgency ·
competitor context · recommended positioning · suggested channels · recommended
content/ad concepts · risks and brand-safety notes.

**Every generated recommendation remains traceable to source evidence.**

### E. Multi-location and multi-request isolation (non-negotiable)

- Each scout request is independently scoped.
- Each location has independent inputs, sources, jurisdiction, coverage radius,
  competitors, products/services, recommendations, generated assets, and analytics.
- Results must **not** blend across Dallas, London, Lagos, Nairobi, or any future market
  unless the user explicitly chooses a comparison or shared strategy.
- Shared campaigns must be an **explicit user action**.
- Localized campaigns remain independently generated and editable.
- **Automated isolation tests are required for every new Phase 3 data model and worker.**

### F. Geographic and jurisdiction controls

Address-based coverage · radius selection · city/state/country targeting · service-area
polygons · inclusion/exclusion zones · language filters · jurisdiction-aware source
selection · explicit "unknown location" handling · confidence scoring for inferred
locations · **no reliance on IP alone** as authoritative location evidence. Include
customer-visible explanations of how location inference was determined.

### G. Recommendation-to-creative workflow

1. Scout completes.
2. User reviews findings.
3. User selects an opportunity.
4. User optionally uploads product images, brand assets, videos, promotions, logos,
   disclaimers.
5. SignalNest generates copy, image concepts, flyers, video scripts, short-form concepts,
   campaign variants.
6. User reviews and edits.
7. User optionally exports, schedules, or connects an ad platform.

**Human approval is required before publishing or spending money.**

### H. Website-only business support

Separate and combined modes: website scouting only · social scouting only · website +
social scouting. Website-focused recommendations should include SEO opportunities,
technical SEO, content gaps, conversion optimization, local search, Google Business
Profile, Search Console insights, Analytics insights, landing-page recommendations,
competitor website analysis, structured-data recommendations, and website-derived
campaign ideas.

### I. Provider and LLM architecture

A production provider abstraction for LLM generation, embeddings, reranking, moderation,
image generation, and video-generation providers where practical.

Require: provider fallback · cost tracking · tenant quotas · prompt/version registry ·
structured outputs · evaluation datasets · hallucination checks · evidence grounding ·
PII minimization · provider-specific feature flags · mock mode for tests.

### J. Production infrastructure

Migrate from local fallbacks to production services: PostgreSQL · pgvector (or selected
vector service) · Redis · durable job queue · S3-compatible object storage · worker
deployment · scheduler · secrets management · backups · observability · error tracking ·
metrics · distributed tracing · health checks · runbooks. **Retain local-development
adapters** for fast onboarding.

### K. Authentication, tenancy, and billing foundations

Production authentication · organization membership · roles and permissions ·
multi-location account management · tenant quotas · usage metering · plan entitlements ·
audit logs · billing-ready events · referral-credit ledger design (if still in scope) ·
clear separation between business credits and withdrawable referral cash.

> Payout logic must **not** be a mere balance field: require a proper ledger and
> anti-abuse controls.

### L. Safety, compliance, and platform policy

Source terms-of-service compliance · consent for connected accounts · OAuth token
handling · data minimization · retention/deletion controls · copyright-aware content
handling · attribution · brand safety · moderation · regulated-industry safeguards ·
political-ad restrictions · financial/health claims review · user approval before
publication · ad-platform policy checks.

### M. Evaluation and quality system

Golden datasets · relevance evaluation · location-accuracy evaluation · source-grounding
evaluation · duplicate-opportunity detection · recommendation-usefulness ratings ·
human-review feedback · A/B testing · model/provider comparisons · cost/latency
evaluation · regression thresholds.

### N. Analytics and history

Customer-visible history for scout runs · opportunities · recommendations · generated
campaigns · asset versions · approval state · export/publish history · performance data ·
website/SEO trends · social and advertising analytics · per-location comparison ·
**explicit** cross-location comparison mode.

---

## Phase 3 sequencing

Small vertical slices, each landing green on `main`. Adjust only when repository
evidence supports a better dependency order.

### Phase 3A — Production foundation
Environment configuration · PostgreSQL/Redis/storage activation · durable queue · worker
and scheduler · authentication and tenancy hardening · observability · provider
abstraction · feature flags · data-retention foundations.

### Phase 3B — First real scouting connector
Choose **one** legally accessible, high-value connector. Deliver source connection,
ingestion, normalization, evidence storage, opportunity creation, location isolation, UI
display, and full tests. **Do not begin with every social network simultaneously.**

### Phase 3C — Scoring and evidence
Opportunity scoring · source credibility · geographic fit · commercial intent ·
confidence · evidence-backed explanation · human feedback loop.

### Phase 3D — Website intelligence
Website crawl · SEO analysis · content-gap analysis · conversion recommendations ·
website-only and combined modes.

### Phase 3E — Creative generation workflow
Finding selection · asset upload · promotion inputs · copy generation · creative briefs ·
image-generation integration · human review and version history.

### Phase 3F — Connectors
Search Console · Analytics · Business Profile · authorized social connectors ·
ad-platform export or draft creation.

### Phase 3G — Billing, quotas, and operational readiness
Usage metering · entitlements · multi-location pricing foundation · cost controls · admin
operations · security review · performance and load testing · beta readiness.

---

## Phase 3 entry criteria

Before Phase 3 implementation begins, require:

- [ ] Updated docs merged.
- [ ] `main` green.
- [ ] Zero open security alerts.
- [ ] `npm audit` clean.
- [ ] CI pipefail regression green.
- [ ] No schema or contract drift.
- [ ] Product owner approves the first Phase 3 vertical slice.
- [ ] Connector policy and legal feasibility confirmed.
- [ ] Acceptance criteria written.
- [ ] Data-isolation tests defined.
- [ ] Rollback plan defined.
- [ ] Cost limits defined.

## Per-PR quality gates (every Phase 3 PR)

- [ ] Real failing exit-code propagation (pipefail-safe).
- [ ] Lint with zero warnings.
- [ ] Type-check.
- [ ] Frontend tests.
- [ ] Backend tests.
- [ ] Ruff.
- [ ] Migration check.
- [ ] Contract generation and drift check.
- [ ] Integration smoke.
- [ ] Location/request isolation tests.
- [ ] Security review for credentials and data flows.
- [ ] Provider mock tests.
- [ ] Idempotency tests for workers.
- [ ] Observability validation.
- [ ] Documentation update.
- [ ] Rollback notes.

---

## Immediate maintenance queue (before Phase 3)

Classification: **[R] Required before Phase 3** · **[E] Recommended early in Phase 3** ·
**[N] Non-blocking technical debt**.

1. **[E]** Review replacement PR #17 (PostCSS `8.5.17 → 8.5.18`, npm-minor-patch group).
2. **[E]** Decide whether to close or defer TypeScript 7 PR #6 (currently deferred).
3. **[N]** Review and reduce the frontend large-chunk warning where practical
   (route-level code splitting).
4. **[E]** Migrate Pydantic class-based `Config` to `ConfigDict`.
5. **[R]** Confirm production environment-variable and secret-management documentation.
6. **[R]** Confirm local setup documentation matches the current toolchain.
7. **[N]** Verify no stale Dependabot branches or obsolete documentation remain.

> Security/tooling health should be addressed **before** major Phase 3 feature work
> unless there is a documented reason to defer.

### Completed maintenance
- **`actions/setup-python` upgraded v5 → v6** (PR #19, merged; squash commit
  `b5965d354a0c2335c2ac9cf283fd28b56d8d612d`). The Node-runtime deprecation annotation
  is no longer emitted; Python 3.12, pip caching, and `contents: read` permissions are
  unchanged.
