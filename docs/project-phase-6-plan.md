# Project Phase 6 — Production Readiness, Live Integration & Launch Completion

**Status:** `PROPOSED / DOCS-ONLY — AUTHORIZES NO CODE, NO MIGRATION, NO INFRASTRUCTURE APPLY, NO FLAG CHANGE, NO ACTIVATION, NO DEPLOYMENT, NO SPEND.`

> This document is a planning record. It authorizes nothing. Every workstream below
> requires its own separate branch, pull request, independent review, protected merge,
> and — where it touches AWS, secrets, live providers or customer data — its own
> explicit operator authorization. Merging this plan does not authorize Phase 6A.

**Authored:** 2026-09-19, against baseline `main` HEAD
`5d978d8d59be425444ba1196217be62857705090`, tree
`68b4e6a6c6dec11b0fe5e53246f2325d0d96d812`, `origin/main` identical (0 ahead / 0 behind).

**Derived from:** a four-lane read-only completion audit of the repository conducted
2026-09-19 (product/roadmap, backend/data/AI, infrastructure/security/operations, and
an adversarial launch review). Every claim in §4 carries a `file:line` or live-API
citation. No claim is inherited from a historical document without revalidation.

---

## 0. Recorded operator decisions (binding)

Two scope-setting decisions were **RESOLVED by the operator on 2026-09-19**, after this
plan's first draft. They are recorded here, at the top, because they govern every scope
statement below and because an earlier draft carried them as open and blocking.

### `P6-D01` — RESOLVED: `PHASE_6_DOES_NOT_SUBSUME_PHASE_5A_THROUGH_5E`

> **Phase 6 excludes the unimplemented Project Phase 5A–5E guided-action product work.**

**Consequence.** Phase 6 is a **production-readiness and pilot-readiness phase for the
product foundation that currently exists** — the Phase 1–4 scouting → scored, explainable
opportunities loop. Phase 5A–5E (recommendation briefs, approvals, text creative
generation, output-side content review, the normalized jurisdiction axis, notifications,
server-side analytics, audited export) remain **separately scoped future product work**
under their own plan set and their own future authorization. Phase 6 must not implement
them, must not partially implement them, and must not absorb their scope by increment.
Their unbuilt status is **not** a Phase-6 obligation.

This does not change Phase 6's role as their *precondition*: the Phase-5 plan's own §10
gates 5A/5C/5E activation on the retention policy, provider selection and infrastructure
completion that Phase 6 delivers. Precondition ≠ inclusion.

### `P6-D02` — RESOLVED: `FIRST_EXTERNAL_LAUNCH = UNPAID_PILOT`

> **Initial external release target: unpaid design-partner pilot.**

**Consequences.**

- Billing is **not** a Phase-6 pilot launch gate.
- Payment-provider integration is **not** required for the first pilot, unless some
  separate existing repository requirement independently mandates it. None was found in
  the audit: every billing reference in the repository defers it (§4.11).
- Ad spend and customer charging are **not authorized**.
- Pilot readiness must **still** satisfy, in full: security, tenant and workspace
  isolation, **real data**, reliability, authentication and account lifecycle, privacy
  and retention, monitoring that reaches a human, and a production environment that
  exists. Unpaid narrows the commercial surface, **not** the safety surface.

### What these decisions do NOT relax

`P6-D02` removes exactly one thing from the launch path: monetization. Every P0 item in
§4 remains P0. In particular `P6-PRIV-1` (retention/deletion) stays a launch blocker —
pilot users are still data subjects — and `P6-DATA-1`/`P6-DATA-2` stay launch blockers,
because a pilot that shows a design partner fictional coffee-shop data is not a pilot.

---

## 1. Predecessor state

### 1.1 What the operator has declared closed

Project Phase 5's **governance round** is declared COMPLETE and frozen by the operator:
`ORD_017 = CLOSED`, `C1_TO_C10_ALL_PROVEN = true`,
`POST_CLOSEOUT_STATE_STABLE__NO_FURTHER_ACTION_REQUIRED`. That closure chain lives
outside this repository, under a separate evidence tree. **This plan does not reopen it,
does not reinterpret it, and mutates nothing in it.**

### 1.2 What the repository independently shows

The following are measured facts about *this repository* at the baseline SHA. They are
recorded because Phase 6 scope must rest on repository truth, not on inference from a
governance closure that concerns a different plane.

| Fact | Evidence |
|---|---|
| The Phase 5 **product** milestones 5A–5E have **no implementation** in the repository | 69 paths / 89 operations in `apps/api/openapi.json`, none of them recommendation, approval, creative-generation, notification, analytics, export or jurisdiction; 33 `__tablename__` declarations, none of `recommendation_briefs`, `creative_drafts`, `approvals`, `jurisdictions`, `campaign_locations`, `notifications`; 3 product feature flags, none of `recommendation_briefs_enabled`, `creative_generation_enabled`, `content_export_enabled` |
| The Phase 5 plan set says so itself | `docs/project-phase-5-plan.md:3-9` — "**Status:** PLANNED / DOCS-ONLY… **Project Phase 5 implementation is authorized by nothing in this repository**"; §15 repeats the non-authorization; all five sub-plans carry the same header |
| The Phase 5 plan set is **untracked in git** | `git status` — six `docs/project-phase-5*.md` files present on disk, never committed. A fresh clone contains no Phase-5 documentation at all |
| Last product-code commit | `a02d091` (Phase 4A-D/4A-E, PR #147). All 19 commits in 2026-08 are INFRA-9 governance. Zero commits in 2026-09 |
| Newest migration | `20260720_1259-98289430a3ec` — 2026-07-20, two months before the Phase-5 plans were authored |

**Reconciliation (binding for this plan).** "Project Phase 5 complete" is true of the
**governance/evidence plane** and is accepted as frozen. It is **not** a statement that
Phase 5A–5E product functionality was built. Phase 6 scoping therefore treats the
guided-action product loop as unbuilt work, and **§7 places it out of Phase-6 scope** so
that Phase 6 does not become an unbounded container.

### 1.3 Phase 4 remainder (carried, not reopened)

- **Phase 4B-A** — merged (`4d253f7`, PR #88). Fail-closed opportunity-feedback gate wired.
- **Phase 4B-B** — **never executed.** `docs/verification/4b-b-feedback-canary.md:2` is a
  placeholder-only template: "NO LIVE CANARY EXECUTED — GLOBAL FLAG FALSE — NO OVERRIDE
  CREATED". No capability has ever been activated in any environment.
- **INFRA-9** — **partially executed, and the tracked documentation understates it.** See
  `P6-GOV-1` in §4.

---

## 2. Definition of complete

Two distinct targets. Phase 6 delivers **Target B for the product that exists**; it does
not deliver Target A.

### TARGET A — PRODUCT BUILD COMPLETE

All committed product functionality implemented and tested, per the roadmap's own scope:
the Phase 1–4 foundation (delivered) **plus** the Phase 5A–5E guided-action loop
(specified, unbuilt) **plus** the `docs/phase-3-plan.md` workstreams that remain open
(§D source breadth, §H website intelligence, §K/§3G billing and quotas).

**Current assessment: MIDWAY.** Denominator: the project's own committed and planned
scope documents. Rationale in §3.

### TARGET B — PRODUCTION LAUNCH READY

Target A for the shipped surface, plus: a production environment that exists, deployment
with rollback, security controls, tenant isolation proven by test, observability that
reaches a human, external integrations that return real data, retention/deletion that can
service a legal request, and launch controls — such that **one real external customer** can sign up,
onboard their team, configure their market, receive real opportunities about their own
business, recover from their own mistakes, and be operated safely by someone on call.

**Scope note (binding, settled by `P6-D02`).** Target B is **design-partner /
unpaid-pilot ready**. It excludes billing, subscriptions and metering (§4.11). A **paid**
launch is Target B **plus** §4.11 and is explicitly **out of Phase 6** under the resolved
decision — it is a later phase, not a variant of this one. Unpaid narrows the commercial
surface only; every security, isolation, real-data, reliability, privacy and
observability requirement in §19 applies unchanged.

**Current assessment: EARLY.** Denominator: the one-external-customer threshold above.

---

## 3. Completion assessment

Quantitative percentages are **not** defensible here and are deliberately not given: the
repository measures no code coverage (`--cov` appears in no config), and the remaining
denominator includes an entire unbuilt product phase whose own plan estimates a 26
working-day ceiling. A percentage would be false precision.

| Axis | Assessment | Evidence basis |
|---|---|---|
| **Product build** | `MIDWAY` | Real system: 69 API paths, 33 tables, 12 migrations with full downgrade coverage, a durable job runner with leases/heartbeats/dead-letter that is genuinely failure-tested, a deny-biased capability resolver, config validation that fails closed on every local-only backend in production, 13 OpenTofu modules. Against that: Phase 5A–5E is 0% built; the only data source is 9 fixture signals about a fictional coffee chain in 4 hard-coded cities; "semantic" dedupe is a SHA-1 token hash; pgvector is a comment |
| **Production launch readiness** | `EARLY` | **No production environment exists in any form** — `infra/aws/variables.tf:19-28` hard-rejects any `environment` value but `"staging"`. A production launch requires authoring a new environment, not flipping a flag. Additionally: no SPA publish path, no API DNS record, no alarm destination, no metrics exporter, no deployment workflow, no rollback, no E2E test, single-seat accounts, no password reset |

---

## 4. Residual-work matrix

Every unresolved item appears exactly once. `LB?` = launch blocker for Target B.
Effort: S ≤ 1 day, M ≤ 1 week, L > 1 week, XL > 2 weeks.

### 4.1 Governance & documentation integrity

| ID | Item | Current state | Evidence | LB? | Dep | Effort | Risk |
|---|---|---|---|---|---|---|---|
| `P6-GOV-1` | Tracked infra docs assert the opposite of tracked evidence | `infra/aws/README.md:22-27` — "no live `tofu plan`/`apply` has run, no AWS API has been contacted, and **nothing exists in AWS**… nothing has been provisioned or deployed"; `:367-369` — "No state bucket, lock table, or key exists in AWS"; `:390-392` — "**No** `tofu plan`, `apply`, `destroy`, `import`, `state`, or `refresh` has run". Against this, tracked `infra/aws/operator-closure-contract.json` records "the complete successful full-graph refresh of 2026-07-28T22:01:44Z-22:01:48Z: 267 CloudTrail events under the Terraform provider user agent" and "the **trail has been logging since 2026-07-27 and is delivering**" | Both files tracked at HEAD, in the same directory; cannot both be true | **Yes** | none | S | An incident responder, new engineer or auditor reading the README — the obvious entry point — concludes no AWS resources exist, affecting teardown, cost review and blast-radius assessment |
| `P6-GOV-2` | Phase-5 plan set is untracked | 6 files, 2,979 lines, holding the frozen contracts and SHA-256-pinned authorization digests, exist only in one working tree | `git status` | No | Operator authorization | S | **Irreversible loss** on `git clean -fd`, fresh clone, or disk failure |
| `P6-GOV-3` | Committed `README.md` is materially false | Says "Phases 3–5 … are out of scope" and "56 operations across 41 paths"; measured 69 paths / 89 operations. Correction exists **only** in the uncommitted working tree | `git show HEAD:README.md` vs `apps/api/openapi.json` | No | `P6-GOV-2` | S | A fresh clone misrepresents the product |
| `P6-GOV-4` | B-4 / B-5 / B-6 are undefined anywhere in the repository | The only reference is untracked `docs/project-phase-5-plan.md:1516` — "without located closure records" | `grep` over `docs/`, `infra/`, `git log --all --grep` → no definition | **Yes** | Operator | S to locate | **Unknown scope cannot be assessed** |
| `P6-GOV-6` | Legacy branch-protection API reports `main` unprotected | `gh api .../branches/main/protection` → `404 "Branch not protected"`, while the **ruleset** API shows active protection with 4 required contexts. Any tool or reviewer using the legacy endpoint concludes `main` is unprotected | live API, both endpoints | No | none | S | Same misleading-evidence class as `P6-GOV-1` |
| `P6-GOV-7` | Unmerged local work with no remote | 13 local branches; **8 have no upstream**. `fix/b2b-reader-adoption-reconciliation` is **+9 commits ahead of `main`** with no PR and no remote | `git rev-list --count main..<branch>`; `git for-each-ref` | No | none | S | Silent work loss on disk failure |
| `P6-GOV-5` | Stale status docs | `docs/architecture.md:5` "describes the Phase 1-2 system", omits 5 shipped backend modules; `docs/acceptance-report.md` claims stub screens that do not exist; `docs/phase-3-plan.md:3` "planned, not started" | Per-file citations in §4 of the audit | No | none | M | New-engineer onboarding error |

### 4.2 Data acquisition — the product's core value

| ID | Item | Current state | Evidence | LB? | Dep | Effort | Risk |
|---|---|---|---|---|---|---|---|
| `P6-DATA-1` | **The product has no real data source** | `get_connector()` returns `FixtureConnector()` whenever no live connector resolves. The only live connector is RSS, which is `False` by default. The RSS connector **is committed at HEAD** (8 files, 676 lines) but performs no egress; the live-egress work sits in unmerged CONFLICTING draft PR #34 (13 files, +1,901/−0) | `apps/api/app/scouting_requests/connectors.py:55-72`; `apps/api/app/core/config.py:235` | **Yes** | Legal/ToS sign-off; egress allow-list; SSRF guard | L | A pilot design partner's dashboard fills with invented posts about a fictional coffee chain |
| `P6-DATA-2` | Any market outside 4 hard-coded cities silently returns nothing | `fixtures_for_market()` substring-matches `{Dallas TX, London UK, Lagos NG, Nairobi KE}` and `return []` otherwise — no error, no explanation | `apps/api/app/scouting_requests/fixtures.py:156-167` | **Yes** | `P6-DATA-1` | L | A Berlin customer's scout completes *successfully* with zero signals; indistinguishable from "broken" |
| `P6-DATA-3` | Geocoder is 7 hard-coded cities; 404 otherwise | Fixture geocoder | `apps/api/app/geography/geocoder.py:10-19,33`; `apps/api/app/locations/routes.py:33` | **Yes** | Provider selection + egress | M | Multi-location is unusable outside 7 cities |
| `P6-DATA-4` | RSS connector performs no HTTP egress even when enabled | Complete parse/normalize/attribute path; feed bytes come from `sample_feeds`; `is_simulated = True` | `apps/api/app/connectors/rss.py:5-15,62,70` | **Yes** | `P6-DATA-1` | M | The flag cannot produce live data |
| `P6-DATA-5` | 9 declared source types, 2 connectors exist | `SourceType` declares manual, website_scan, competitor_scan, rss_news, reddit, reviews, google_trends, meta_ad_library, tiktok_creative_center; `connectors/` holds RSS + fixture | `apps/api/app/core/enums.py`; `apps/api/app/connectors/` | No (Target A) | `P6-DATA-1` | XL | Each carries its own ToS exposure |
| `P6-DATA-7` | Website intelligence (crawl / SEO / content gap) | NOT_STARTED. A `summarize_website` prompt is registered with **no consumer** | `apps/api/app/llm/prompts.py:52`; scope at `docs/phase-3-plan.md` §H | No — deferred to Phase 7 | `P6-DATA-5` | L | The §H roadmap promise is unmet |
| `P6-DATA-6` | Market matching is bidirectional substring and fails open | `if t and (t in m or m in t)`; radius rules with no market list are permissive; connector policy skips the market check when `market is None` | `apps/api/app/geography/engine.py:83,86-87`; `apps/api/app/connectors/policy.py:33-36` | **Yes** before any live connector | none | M | A London, UK signal satisfies a "London, Ontario" rule — the exact cross-location blending the product promises never to make |

### 4.3 Account lifecycle & authorization

| ID | Item | Current state | Evidence | LB? | Dep | Effort | Risk |
|---|---|---|---|---|---|---|---|
| `P6-AUTH-1` | **A customer cannot add a second user** | `register()` is the only non-seed `OrganizationMember(` write. No invite, member-add, or role-assignment endpoint exists. 6 roles defined; only `OWNER` is ever assigned, so 5 are unreachable | `apps/api/app/auth/service.py:41`; no such path among the 69 | **Yes** | none | M | SignalNest is single-seat; no company can use it |
| `P6-AUTH-2` | **No password reset, no email verification** | Zero hits for `reset_password` / `forgot_password` / `verify_email` in `apps/api/app` and `apps/web/src`. No mail transport anywhere | `apps/api/app/auth/routes.py` exposes 3 operations | **Yes** | Email transport decision | M | A customer who forgets their password on day 3 is permanently locked out; only recourse is an operator DB write |
| `P6-AUTH-3` | Audit-log route over-permits by rank-floor widening | `require_role(OWNER, ADMIN, COMPLIANCE_REVIEWER)` computes `min_rank = min(4,3,1) = 1`, so **MARKETER (2) and REVIEWER (1) both pass**. Only VIEWER is denied | `apps/api/app/auth/dependencies.py:102-113`, `:23-30`; `apps/api/app/audit/routes.py:20` | **Yes** | none | S | Live defect in `main`; masked today only because `P6-AUTH-1` means nobody but OWNER exists. Becomes exploitable the moment role assignment ships |
| `P6-AUTH-4` | No token refresh or revocation; 12-hour token | Stateless JWT, no `jti`, no denylist, no session store | `apps/api/app/core/config.py:104` | **Yes** for public exposure | none | M | A stolen token is valid for 12 hours and cannot be invalidated |
| `P6-AUTH-5` | JWT in `localStorage` with no CSP and no security headers | `localStorage.setItem(TOKEN_KEY, …)`; zero hits for CSP / HSTS / X-Frame-Options / referrer-policy across `apps/`, `infra/`, `.github/`; no CloudFront response-headers policy | `apps/web/src/auth/AuthContext.tsx:34,42` | **Yes** | none | M (headers) / L (cookie) | Any XSS exfiltrates a 12-hour unrevocable bearer token with no `connect-src` to block it |
| `P6-AUTH-7` | `POST /organizations/{id}/workspaces` has no role gate | Membership check only — any member of any rank, including `VIEWER`, can create a workspace | `apps/api/app/organizations/routes.py` | No | `P6-AUTH-1` | S | Sets the precedent that makes a future tenant-scoping bypass likely; currently latent because only OWNER exists |
| `P6-AUTH-6` | No exact-set authorization primitive | `require_role` is rank-floor only; `require_exact_roles` does not exist | `apps/api/app/auth/dependencies.py:102-113` | Yes, for approvals | `P6-AUTH-3` | S | Blocks any separation-of-duties claim |

### 4.4 Platform correctness — configurations that cannot work

| ID | Item | Current state | Evidence | LB? | Dep | Effort | Risk |
|---|---|---|---|---|---|---|---|
| `P6-PLAT-1` | **No valid production vector configuration exists** | Production **rejects** `vector_backend=bruteforce`; `build_index()` returns `BruteForceIndex()` unconditionally; zero `CREATE EXTENSION` across all 12 migrations; IaC does not preload pgvector; `build_index()` has zero callers | `apps/api/app/core/config.py:357-361`; `apps/api/app/infra/vector.py:60-62`; `infra/aws/modules/data_sql/main.tf:22-23` | **Yes** | DB bootstrap authority | M | `bruteforce` → containers crash-loop on startup. `pgvector` → startup succeeds, readiness reports `DEGRADED` (non-blocking) on config alone, and dedupe then **silently runs in-process brute force forever** — no SQL anywhere references a vector type, so there is no error to detect. The silent path is the more dangerous one |
| `P6-PLAT-2` | Production forces a queue backend that is dead code | Production forbids `queue_backend=inprocess` → selects `RedisQueue`, which `xadd`s to a stream **no consumer reads**; readiness reports it `HEALTHY` because the object is non-`None`; built at import time | `apps/api/app/core/config.py:344-348`; `apps/api/app/infra/queue.py:58-70`; `apps/api/app/system/probes.py:166-181` | **Yes** | none | S | A mandatory production config value selects dead code and readiness green-lights it |
| `P6-PLAT-3` | Rate limiter is a documented placeholder: in-memory, per-process, unbounded | "Naive fixed-window limiter. Placeholder; production uses Redis adapter" — no Redis adapter exists. `self._hits` prunes timestamp lists but **never evicts keys** | `apps/api/app/core/middleware.py:100-123`; applied globally at `main.py:90` | **Yes** | Live Redis | M | (a) Effective limit is N×240/min across replicas → credential stuffing with no WAF, no lockout, no detection. (b) An IP-rotating client grows the dict until the task is OOM-killed |
| `P6-PLAT-4` | No Python dependency lockfile; 16 unbounded `>=` constraints | No `requirements.txt` / `poetry.lock` / `uv.lock` / `Pipfile.lock` for the API. `Dockerfile:57` claims "locked dependencies"; there is no lock | `apps/api/pyproject.toml:6-20` | **Yes** | none | M | Two builds of the same SHA produce different dependency sets, defeating the digest-pinning discipline used everywhere else. `python-jose>=3.3` gates the entire auth path and its resolved version is unknowable offline |
| `P6-PLAT-5` | OpenAPI declares zero `securitySchemes` | 0 security schemes, 0 per-operation `security`, despite bearer auth on nearly every operation | `apps/api/openapi.json` `components.securitySchemes` → `[]` | No | none | S | Generated client types describe no auth; docs UI has no Authorize control |
| `P6-PLAT-6` | Dead-lettered tick permanently ends a recurring schedule | `DEAD_LETTERED` is terminal with an empty outgoing-transition set; the schedule chain is self-propagating with no external cron; no requeue endpoint, no alert | `apps/api/app/jobs/status.py:44,118`; `apps/api/app/scouting_requests/schedules.py:315,392-393` | **Yes** for scheduled scouting (latent — currently dark) | none | M | A recurring schedule silently stops forever |
| `P6-PLAT-7` | Two empty packages shadow real modules | `app/business_profiles/` and `app/clustering/` contain only 0-byte `__init__.py`; real code is in `brands/` and `intelligence/clustering.py` | `find` | No | none | S | Navigation confusion |
| `P6-PLAT-8` | Campaign↔location binding is an unvalidated JSON array | `location_ids` and `eligible_location_ids` are `JSON` columns with **no FK and no membership validation** — a campaign can reference a location in another workspace | `apps/api/app/campaign_context/models.py:84,125`; schemas `:55,87` | **Yes** | none | M | **A live integrity defect in shipped Phase 1–2 code**, on the same cross-location isolation invariant as `P6-DATA-6`. Phase 5D would *harden* it; it is broken in `main` today |
| `P6-PLAT-9` | Campaign-context rows cannot be edited | All **9** context collections expose `GET`+`POST` on the collection and `DELETE /{item_id}` — and **no `PUT`/`PATCH` on any of them** (6 PUT operations exist elsewhere in the API, so this is specific to these collections) | `apps/api/openapi.json`; `apps/api/app/campaign_context/routes.py` | **Yes** | none | M | A customer who typos a competitor must delete and recreate, losing the row id and any referential history |
| `P6-PLAT-12` | No cursor pagination on customer-facing lists | Every list is `limit`-bounded, but only internal/operator lists and feedback accept `offset`. Customer lists (opportunities, audit-logs, campaign context, scout-requests) cannot page past their cap | `apps/api/app/opportunities/routes.py:59`; `apps/api/app/audit/routes.py:18` | No (bounded, so not a stability risk) — but it is the backend half of `P6-UI-022` | none | M | A workspace with >100 opportunities silently loses rows in the UI |
| `P6-PLAT-11` | Weak-secret detection is literal-match only | Config rejects only the literal default and empty string | `docs/security/phase-3a-4b-security-review.md:53-60` (F-4) | No | none | S | A trivially weak but non-default secret passes |
| `P6-PLAT-10` | Three empty workspace packages | `packages/{config,shared,ui}` contain **0 files**, while `README.md` and `docs/architecture.md` describe them as holding shared types | `find packages -type f` → 0 | No | none | S | Decide: populate or delete |

### 4.5 AI / LLM production readiness

| ID | Item | Current state | Evidence | LB? | Dep | Effort | Risk |
|---|---|---|---|---|---|---|---|
| `P6-LLM-1` | **No real-provider code path has ever executed under test** | Both adapters are hand-rolled `httpx`; every error branch is `# pragma: no cover - network`. Production **forbids** `llm_provider=mock`, so the first real execution lands in a production-like environment | `apps/api/app/llm/providers_real.py:69,79,124,134`; `apps/api/app/core/config.py:385-389` | **Yes** | `P6-LLM-6` for a live sandbox run; **none** if satisfied by recorded-fixture replay of timeout/429/5xx/non-JSON | M | The first real scout run in production is the first execution of that code, ever |
| `P6-LLM-2` | Raw third-party text reaches the prompt unsanitized | `sanitize_text` exists but its only non-test caller is excerpt building. The pipeline `.format()`s untrusted scraped content straight into the template | `apps/api/app/jobs/pipeline.py:211-213`; `apps/api/app/intelligence/extraction.py:48,140` | **Yes** before any real provider | none | M | Prompt injection via scraped content |
| `P6-LLM-3` | Model output is persisted verbatim and never re-screened | Validated output assigned directly and returned into the persisted opportunity; the registered `check_claim_safety` LLM task is **never invoked** — only the deterministic regex engine runs | `apps/api/app/jobs/pipeline.py:216-219,596`; `apps/api/app/llm/schemas.py:46` | **Yes** | `P6-LLM-2` | M | Unscreened model text reaches the customer |
| `P6-LLM-4` | Zero cost accounting, zero quotas, zero per-tenant isolation | `estimated_cost_usd` is hard-coded `0.0` everywhere; no price table; no per-workspace cap; `llm_service` is a module-level singleton holding one process-wide key; **no tenant identifier ever reaches the provider**. One model call per non-noise signal with no batch cap | `apps/api/app/llm/base.py:50`; `apps/api/app/llm/service.py:125`; `apps/api/app/jobs/pipeline.py:208-211` | **Yes** | none | M | Unbounded, unattributable paid spend from a single large scout run |
| `P6-LLM-5` | Silent degradation persists canned copy unmarked | On provider failure, `explain_opportunity` returns generic strings ("Signal is relevant to your market.") persisted into the opportunity **with no marker** distinguishing them from real model output | `apps/api/app/jobs/pipeline.py:597-604` | **Yes** | none | S | Data-integrity failure for a product whose value proposition is evidence-backed reasoning |
| `P6-LLM-6` | Real provider selection + credentials undecided | Operator decision O-7 `UNDECIDED_RESERVED_TO_OPERATOR` | `docs/project-phase-5-plan.md` §12 | **Yes** | Operator | S once decided | Blocks all generation |
| `P6-LLM-7` | Embeddings are a SHA-1 token hash, not a semantic model | 256-dim hashing embedder; dedupe at 0.92 is therefore lexical | `apps/api/app/infra/vector.py:22-33` | No | `P6-DATA-1` | M | Degrades the core value proposition; visible once real data lands |

### 4.6 Infrastructure & deployment

| ID | Item | Current state | Evidence | LB? | Dep | Effort | Risk |
|---|---|---|---|---|---|---|---|
| `P6-INF-1` | **No production environment exists in any form** | Both IaC roots hard-reject any `environment` value but `"staging"`. No production workflow, no production GitHub environment | `infra/aws/variables.tf:19-28` | **Yes — the hardest blocker** | most of §4.6 | XL | A production launch requires authoring a new environment, not flipping a flag |
| `P6-INF-2` | No SPA deployment path | Private SPA origin bucket exists and is readable only by CloudFront; no `s3 sync` / `cp` / `create-invalidation` anywhere in `.github/workflows/` or `scripts/` | `infra/aws/modules/edge/main.tf:27,177-199` | **Yes** | none | M | Every request hits the 403→`/index.html` rewrite against an empty bucket |
| `P6-INF-3` | No API DNS record | "Deferred (§24.7): WAF and the API Route 53 alias." The edge module creates records only for `web_fqdn` | `infra/aws/modules/alb/main.tf:25`; `modules/edge/main.tf:204-226` | **Yes** | none | S | API unreachable at its certificate's name; the raw ALB name produces a TLS name mismatch |
| `P6-INF-4` | **11 alarms and a dashboard notify nobody** | `alarm_actions = var.sns_topic_arn == null ? [] : [...]`, default `null`; **zero `aws_sns` resources exist anywhere** (verified) | `infra/aws/modules/observability/main.tf:54`; `infra/aws/variables.tf:364-374` | **Yes** | none | S | An RDS storage alarm fires at 02:00 into `[]`; the volume fills, PostgreSQL goes read-only, detection is by customer report |
| `P6-INF-5` | Application metrics are discarded | `_backend: MetricsBackend = NoOpMetrics()`; "a real exporter is deferred"; no `/metrics` route. Against this, `docs/operations/alerts.md` specifies 5xx-ratio and p95-latency alerts — neither metric reaches any sink | `apps/api/app/core/metrics.py:18,246` | **Yes** | `P6-INF-4` | L | 30% 5xx with `WARNING`-level logs fires no alarm; the dashboard shows healthy CPU throughout |
| `P6-INF-6` | No ALB 5xx / unhealthy-host alarm | Explicitly deferred because the alb module exposes no `arn_suffix` | `infra/aws/modules/observability/main.tf:18-19` | **Yes** | `P6-INF-4` | S | No signal on the public entry point |
| `P6-INF-7` | No deployment workflow, no rollback, no canary, no post-deploy smoke | Apply is manual and off-CI. ECS circuit-breaker rollback covers only a failed rollout; no workflow redeploys a prior digest | no deploy job in `.github/workflows/` | **Yes** | `P6-INF-2` | L | No deployment record, no deployed-SHA attestation, no audit trail |
| `P6-INF-8` | Reader workflows declare GitHub environments that do not exist | `staging-reader-publish` and `staging-reader-run` are declared; live API shows `total_count: 1` (`staging` only) | `.github/workflows/reader-run.yml:51`, `reader-publish.yml:49`; `gh api .../environments` | **Yes** | none | S | On first dispatch GitHub **auto-creates them with no protection rules** — the human-approval gate the workflow calls its primary control silently evaporates while assuming an AWS role against the staging database |
| `P6-INF-9` | Live DB schema revision is unknown | `revision_reader` exists solely to answer this; **0 runs**; both lifecycle flags default `false`; both reader environments absent | `gh api` run counts | **Yes** | `P6-INF-8` | M | Gates any migration |
| `P6-INF-10` | B-3 `PassRole` dispute is an unresolved self-declared mandatory gate | Contract: whether `RegisterTaskDefinition` performs a PassRole check is `DISPUTED`; "MANDATORY PART-B PRE-FLIGHT GATE: resolve the dispute BEFORE any apply that reaches a task definition"; `iam:SimulatePrincipalPolicy` "EXPLICITLY INSUFFICIENT" | `infra/aws/operator-closure-contract.json` | **Yes** | Fresh AWS authorization | M | Apply fails `AccessDenied` **mid-graph** after surrounding resources exist — a partial apply |
| `P6-INF-11` | No WAF | `grep wafv2` → 0 files; deferred by locked decision in three module headers | `infra/aws/README.md:418` | **Yes** for production | `P6-INF-3`, `P6-PLAT-3` | M | With the placeholder rate limiter unfixed, zero volumetric defence |
| `P6-INF-12` | No tested restore; no RTO/RPO; Multi-AZ off | Procedure documented, never executed; `multi_az = false`; single region; no cross-region snapshot copy | `docs/operations/aws-staging-operational-procedures.md:439`; `modules/data_sql/variables.tf:136-139` | **Yes** for production | Live AWS | M | Backups that have never been proven restorable |
| `P6-INF-13` | No lifecycle rules on the audit and ALB-log buckets | The SPA bucket has one, so the pattern exists and was simply not applied | `modules/observability/main.tf:257-264`; `modules/alb/main.tf:119-126` | No | none | S | Unbounded growth against a $200/month hard ceiling; audit trail deletable by any principal with bucket write |
| `P6-INF-15` | Stage-A permissions-boundary prerequisite is an unnamed pre-apply gate | `stage_a_create_closure._boundary_dependency`: Stage A **requires** `role_boundary_mode = "required"` and the boundary policy to already exist, or `CreateRole` never matches and the apply fails **after** the ECR resources already exist (`_ordering_hazard`) | `infra/aws/operator-closure-contract.json` | **Yes** | none | S | Same partial-apply hazard class as `P6-INF-10`; must be asserted **before** apply, not discovered during it |
| `P6-INF-16` | CloudTrail has no CloudWatch Logs delivery and no data events | Single-region, management-events-only, `is_multi_region_trail = false` | `infra/aws/modules/observability/main.tf:366-378` | No | `P6-INF-4` | M | **No alarm can ever fire on an IAM change or a `GetSecretValue`** — archive without detection |
| `P6-INF-17` | No reviewed path to grant `is_operator` outside dev/test | Settable only by the dev/test seed | `apps/api/app/db/seed.py:212` | No | none | S | The entire operator plane requires a manual DB write in staging/production |
| `P6-INF-18` | `/docs` and `/openapi.json` served unconditionally; Redis has no auth token; ALB deletion protection off | Three independent hardening gaps | `apps/api/app/main.py:85-86`; `infra/aws/modules/data_cache/main.tf`; `modules/alb/main.tf:85` | No | none | S | Information disclosure; unauthenticated cache access inside the VPC; accidental ALB deletion |
| `P6-INF-14` | Secrets Manager containers are empty; no rotation | 1 CMK (rotation on) + 4 empty containers; zero `aws_secretsmanager_secret_version`; no `aws_secretsmanager_secret_rotation` | `infra/aws/modules/secrets/main.tf` | Yes for production | Operator | M | Out-of-band population only |

### 4.7 CI, test and release gates

| ID | Item | Current state | Evidence | LB? | Dep | Effort | Risk |
|---|---|---|---|---|---|---|---|
| `P6-CI-1` | **The two security-bearing CI jobs are not required contexts** | Live ruleset requires exactly `Frontend quality`, `Backend quality`, `Migrations and API contract`, `Integration smoke`. `Container build and security` and `Revision reader` run on every PR and are advisory. Recorded as operator decision **O-9, resolved 2026-09-01**; the GitHub-side promotion was never performed | `gh api repos/bolade04/signal_nest/rulesets/18820692` | **Yes** | Operator GitHub action | S | A PR adding `USER root` to the Dockerfile, or tripping the live-identifier leak scan, merges green on one approval |
| `P6-CI-2` | Zero browser/E2E coverage of a 16k-LOC SPA | No Playwright / Cypress / Puppeteer / Selenium harness, config, or CI job. The `dist/` bundle is built in CI and never executed | tracked-file grep | **Yes** | none | M | Frontend correctness rests entirely on jsdom tests against MSW mocks |
| `P6-CI-3` | No cross-organization isolation test on any customer-facing route | Isolation tests cover per-**location** separation inside one workspace; cross-org HTTP tests exist only on 4 operator capability routes. The seed creates one org, one user | `apps/api/app/tests/test_api_isolation.py`; `apps/api/app/db/seed.py:203-231` | **Yes** | none | S | The core security invariant has no route-level regression guard |
| `P6-CI-4` | Migration round-trip runs on SQLite only | CI upgrade→downgrade→upgrade is SQLite; production is PostgreSQL-only. Partial/failed-migration-midway is untested | `.github/workflows/ci.yml:189` | **Yes** | none | S | Postgres service already exists in `backend-quality` |
| `P6-CI-5` | The PostgreSQL job-store concurrency path is never exercised | `SKIP LOCKED` claim and `FOR UPDATE SKIP LOCKED` lease recovery are PostgreSQL-only; CI runs SQLite | `apps/api/app/jobs/store.py:234,721` | **Yes** | `P6-CI-4` | M | The production concurrency-control algorithm has never run under test |
| `P6-CI-6` | No CVE scan, SAST, dependency scan, IaC scan or SBOM on the merge path | Trivy runs only in manually-dispatched publish workflows. `.github/` contains only `workflows/` — no Dependabot config file | `.github/workflows/ci.yml` | **Yes** | `P6-CI-1` | M | A PR introducing a CRITICAL CVE merges clean |
| `P6-CI-7` | 12 open Dependabot alerts, 3 HIGH; 15 open PRs | Three `react-router` advisories hit a **runtime production dependency** of the shipped SPA, including an open-redirect→XSS. PR #163 carries the fix and is `MERGEABLE` | `gh api .../dependabot/alerts`; `gh pr list` | **Yes** | none | S (patches) / M (majors #6, #27) | Combines with `P6-AUTH-4`/`P6-AUTH-5`: XSS → token theft → 12-hour unrevocable session |
| `P6-CI-11` | The required **Integration smoke** gate certifies fixtures, not the product | It asserts the four demo cities (Dallas/London/Lagos/Nairobi) each return ≥1 opportunity and that no opportunity crosses locations | `scripts/smoke_http.py:11-26` | **Yes** (coupled) | `P6-DATA-1` | S | Real data will either break this required gate or it will keep certifying simulated output. It must be reworked **in the same tranche** as `P6-DATA-1/2`, not after |
| `P6-CI-8` | Code coverage is not measured | No `--cov`, no coverage config anywhere | repo grep | No | none | S | Every completeness claim is unfalsifiable |
| `P6-CI-9` | Actions are tag-pinned, not SHA-pinned; Docker base image is tag-pinned | 10 actions, zero hex pins; `python:3.12-slim` | workflow grep; `apps/api/Dockerfile:26` | No | `P6-PLAT-4` | S | Supply-chain posture inconsistent with the digest-pinning used elsewhere |
| `P6-CI-10` | No SPA error boundary | Zero `ErrorBoundary` / `componentDidCatch` in `apps/web/src` | grep | No (near-blocker) | `P6-CI-2` | S | A single render throw yields a white screen with no recovery and no report |

### 4.8 Data privacy, retention & compliance

| ID | Item | Current state | Evidence | LB? | Dep | Effort | Risk |
|---|---|---|---|---|---|---|---|
| `P6-PRIV-1` | **No retention/deletion policy for any data class; no mechanism to enforce one** | Operator decision O-5 `UNDECIDED_RESERVED_TO_OPERATOR_LEGAL`. **No soft-delete column exists anywhere**; no purge worker; no account-deletion endpoint; no DSAR/export path | `docs/project-phase-5-plan.md` §12; `apps/api/app/scouting_requests/schedules.py:529` — "no soft-delete column exists" | **Yes** for any GDPR/CCPA-exposed launch | Operator + legal | L | Neither erasure nor portability can be serviced |
| `P6-PRIV-2` | No policy on prompts sent to third-party LLM providers | No DPA record, no retention position | O-7 unresolved | **Yes** | `P6-LLM-6` | M | Tenant data may be retained by a third party under unknown terms |
| `P6-PRIV-3` | Audit immutability is app-layer only | Single `record_audit` seam, but `action` is an open `str` with no enum; no DB-level immutability; the app role owns the database | `apps/api/app/audit/service.py:10-23` | **No for the pilot** (`P6-D02`); **P1 — before GA** | DB role authority | M | Append-only is bypassable by anything holding `DATABASE_URL` |
| `P6-PRIV-5` | Audit **coverage** is thin (distinct from immutability) | Exactly **19** `record_audit` call sites; **zero** on the 27 campaign-context operations, on workspace creation, or on onboarding. There is also no frontend consumer — `apps/web/src/api/queryKeys.ts:69` defines an `auditLogs` key with no endpoint function and no caller | `grep 'record_audit('` → 19 | **No for the pilot** (`P6-D02`); **P1 — before GA** | none | M | An audit-spine claim fails on **coverage** before it ever reaches immutability |
| `P6-PRIV-4` | No scraped-source provenance/ToS record or takedown path | Dark today | `apps/api/app/connectors/` | **Yes** with `P6-DATA-1` | Legal | M | Per-source ToS exposure |

### 4.9 Capability governance

| ID | Item | Current state | Evidence | LB? | Dep | Effort | Risk |
|---|---|---|---|---|---|---|---|
| `P6-CAP-1` | **2 of 3 capabilities bypass the resolver** | `resolve_capability` has exactly one **enforcement** consumer — feedback (its other non-test caller, `system/internal_capabilities_routes.py:329`, is an operator *read*). Scheduling and RSS read the raw global flag, so **neither honours a workspace override nor the operator safety ceiling** — though the registry advertises both, and the operator console renders those controls from the registry's booleans | `apps/api/app/feedback/routes.py:85` (sole consumer); `apps/api/app/scouting_requests/routes.py:75`; `apps/api/app/connectors/registry.py:26` | **Yes** | none | M | A documented safety control that silently does not apply |
| `P6-CAP-2` | `FeatureFlagsOut` reflects 1 of 3 flags — **IN SCOPE for 6U-1, not deferred** | A client cannot learn scheduling or RSS is dark without probing and receiving a 503. **This is the backend half of `P6-UI-002`** (UI-P0): `SchedulePanel` cannot gate itself because `scout_scheduling_enabled` is not reflected. At minimum the scheduling flag must be added | `apps/api/app/system/routes.py:40-52` | **Yes — via `P6-UI-002`** | none | S | Without it the most prominent unbuilt feature stays clickable and always fails |
| `P6-CAP-3` | `future_activation_phase="4B"` on all three, consumed by nothing | Factually wrong for RSS (blocked on legal, not Phase 4B) | `apps/api/app/capabilities/registry.py:70-72,95,104,116` | No | none | S | No test can catch the drift |

### 4.10 Product surface (Phase-5A–5E scope — **excluded by resolved `P6-D01`**, recorded for completeness only)

> **`OUTSIDE_PHASE_6__PHASE_5A_5E_FUTURE_PRODUCT_WORK`.** Nothing in this subsection is a
> Phase-6 obligation, a Phase-6 exit condition, or an input to the Phase-6 critical path.
> It is listed so that the absence of these surfaces is a known, recorded fact rather
> than a discovery during a founder walkthrough.

> Two items that an earlier draft filed here — the unvalidated campaign `location_ids`
> and the create+delete-only campaign-context collections — are **live defects in shipped
> Phase 1–2 code**, not Phase-5 scope. They have been moved to §4.4 as `P6-PLAT-8` and
> `P6-PLAT-9` and carry launch-blocker judgements there.

| ID | Item | State | Evidence |
|---|---|---|---|
| `P6-P5-1` | Recommendation briefs | NOT_STARTED | No table, model, route, or flag |
| `P6-P5-2` | Approval workflow + approver gate | NOT_STARTED | No `require_exact_roles`, no approvals table |
| `P6-P5-3` | Text creative generation | NOT_STARTED | No `creative_draft` identifier; no generation prompt registered |
| `P6-P5-4` | Output-side content review | NOT_STARTED | `claims/engine.py` is input-side only |
| `P6-P5-5` | Jurisdiction axis (normalized, fail-closed) | NOT_STARTED | No jurisdiction entity, column or resolver anywhere |
| `P6-P5-6` | In-app notifications | STUB_ONLY | `apps/web/src/components/layout/notifications.tsx:10,23` — "Live alerts arrive in a later phase" |
| `P6-P5-7` | Status analytics (server-side, derived) | PARTIAL | `Overview.tsx` computes 4 cards + 1 chart client-side from the opportunity list |
| `P6-P5-8` | Audited manual export | NOT_STARTED | No route, table, or flag |

### 4.11 Commercialization (**out of Phase-6 scope**, recorded for completeness)

| ID | Item | State | Evidence |
|---|---|---|---|
| `P6-COM-1` | Billing, subscriptions, metering, entitlements, invoicing | NOT_STARTED | Zero implementation; the only `subscription` hits in `apps/` are coffee-shop demo fixtures. Explicitly excluded by Phase-5 §2; deferred to `docs/phase-3-plan.md` §K / Phase 3G. **Out of Phase 6 under resolved `P6-D02`** — required before any paid launch, not before the unpaid pilot |

---

## 5. Critical path — `SIGNALNEST_CRITICAL_PATH`

```
GATE 0 — TRUTH (no dependencies; unblocks review of everything else)
  P6-GOV-1 reconcile infra docs   P6-GOV-2 commit Phase-5 plans
  P6-GOV-4 locate/dispose B-4/5/6  P6-CI-1 promote 2 required contexts
        │
        ├──────── LANE A (pure repo work — fully parallel, no AWS) ────────┐
        │  P6-AUTH-3 rank-floor fix ─► P6-AUTH-6 exact-role primitive      │
        │  P6-AUTH-1 members/roles ─┬─► (unmasks P6-AUTH-3)                │
        │  P6-AUTH-2 reset/verify ──┘                                       │
        │  P6-PLAT-1 pgvector  P6-PLAT-2 queue  P6-PLAT-4 lockfile          │
        │  P6-PLAT-3 rate limiter (needs Redis at runtime only)             │
        │  P6-LLM-2/3/4/5 seam hardening   P6-CAP-1 resolver coverage       │
        │  P6-CI-2/3/4/5/6/7 test + gate closure                            │
        └───────────────────────────────────────────────────────────────────┘
        │
        ├──────── LANE B (AWS-authorized; strictly sequential) ─────────────┐
        │  P6-INF-15 assert boundary prerequisite (before ANY apply)        │
        │  P6-INF-8 create reader envs WITH protection  ∥  P6-INF-10 canary │
        │     P6-INF-10 = resolve PassRole dispute by an authorized single  │
        │       canary task-definition registration WITH TAGS (also settles │
        │       ecs:TagResource). Simulator explicitly insufficient.        │
        │           └─► APPLY-1 reader Stage-A publication bootstrap        │
        │                 └─► APPLY-2 reader-publish run (image)            │
        │                       └─► APPLY-3 Stage-B runtime enablement      │
        │                             └─► P6-INF-9 reader-run → live DB rev │
        │                                   └─► workload apply permitted    │
        │  P6-INF-4 SNS topic ─► P6-INF-5 metrics exporter ─► P6-INF-6 alarms│
        │  P6-INF-3 API DNS ─► P6-INF-2 SPA publish ─► P6-INF-7 deploy+rollback│
        └───────────────────────────────────────────────────────────────────┘
        │
        ├──────── LANE C (external/legal — longest lead time, start day 1) ─┐
        │  P6-LLM-6 provider + credentials (O-7)                            │
        │  P6-PRIV-1 retention policy (O-5) ─► purge/soft-delete/DSAR       │
        │  P6-DATA-1 connector ToS/legal sign-off ─► live egress            │
        └───────────────────────────────────────────────────────────────────┘
        │
        ▼
  CONVERGENCE — staging proven end-to-end with real data and real alerting
        │
        ▼
  P6-INF-1 author the production environment  ─► P6-INF-11 WAF
        └─► P6-INF-12 tested restore + RTO/RPO ─► LAUNCH GATE
```

**Longest chain (blocks production):** `P6-INF-15 → P6-INF-10 → APPLY-1 → APPLY-2 →
APPLY-3 → P6-INF-9 → workload apply → P6-INF-4/5/6 → P6-INF-3/2/7 → P6-INF-1 →
P6-INF-11 → P6-INF-12`. **APPLY-1 and APPLY-3 are distinct AWS applies and
APPLY-2 is a GitHub Actions image publish; all three require their own authorization** — they are named here because an earlier draft
collapsed them into a single arrow. `P6-INF-8` (GitHub-side) and `P6-INF-10` (AWS-side)
are independent and may run in parallel, but **both** must complete before APPLY-1.

**Longest lead time (start immediately, finishes elsewhere):** Lane C. Legal/ToS sign-off
and a retention policy are exogenous; nothing in Lanes A or B shortens them, and
`P6-DATA-1`, `P6-LLM-6` and `P6-PRIV-1` each block a launch outright.

**Cheapest risk reduction (all `S`, no dependencies):** `P6-CI-1`, `P6-INF-4`,
`P6-INF-8`, `P6-GOV-1`, `P6-AUTH-3`, `P6-CI-7`(PR #163), `P6-PLAT-2`, and — added by
`P6-UI-0` — `P6-UI-001`, `P6-UI-002`, `P6-UI-008`, `P6-UI-010`.

### 5.1 Four parallel lanes (refined after `P6-UI-0`)

```
Lane U — FOUNDER-VISIBLE PRODUCT / PILOT UX        (repo-only, no deps, start now)
   6U-1  P6-UI-018 start the worker ─┐
         P6-UI-001 error envelope ───┤ seven items, all S, all independent,
         P6-UI-019 signals key ──────┤ all repo-only. Together they remove
         P6-UI-020 active-count tile ┤ every "looks broken" artefact from a
         P6-UI-002 + P6-CAP-2 gate ──┤ founder demo.
         P6-UI-008 demo creds ───────┤
         P6-UI-010 stale phase copy ─┘
   6U-2  P6-UI-004 · 009 · 011 · 012 · 013 · 015 · 021 · 022 · 023 · 024
         (004 and 022 need their backend halves from Lane P/S first)

Lane P — PLATFORM CORRECTNESS / AUTH / DATA        (repo-only, no deps, start now)
   6B    P6-AUTH-3 ─► P6-AUTH-6 ;  P6-AUTH-1 ─► unmasks P6-AUTH-3
         P6-AUTH-2 (needs P6-D11 email transport)
   6C    P6-PLAT-1/2/4 · P6-LLM-2/3/4/5 · P6-CAP-1
         └─ P6-CAP-1 is the backend half of P6-UI-006 only
   6D    P6-CI-2/3/4/5/6/7 · P6-CI-11

Lane I — INFRASTRUCTURE / PRODUCTION               (AWS-authorized, serialized)
   6E    P6-INF-15 ─► P6-INF-10(canary, with tags) ─► APPLY-1 ─► APPLY-2
           ─► APPLY-3 ─► P6-INF-9 ─► workload apply
         P6-INF-4 ─► P6-INF-5 ─► P6-INF-6
         P6-INF-3 ─► P6-INF-2 ─► P6-INF-7
   6G    P6-INF-1 ─► P6-INF-11 ─► P6-INF-12 ─► LAUNCH GATE

Lane S — SECURITY / OPERATIONS                     (spans P and I)
   6A    P6-GOV-1/4 · P6-CI-1 · P6-CI-7
   6F    P6-PRIV-1 (needs P6-D05) · P6-PRIV-2 (needs P6-D04)
         P6-DATA-1 (needs P6-D03 legal) ─► P6-DATA-2/4 ─► P6-CI-11 rework
```

**Lane dependencies that matter.**
- Lane U is **fully independent of Lanes I and S** and needs no operator input. It is the
  only lane that can start, finish and be seen by the founder immediately.
- `P6-UI-006` is a UI *symptom* of `P6-CAP-1` (Lane P, 6C). Do not patch it in Lane U —
  fixing the resolver fixes it. **`P6-UI-005` is different**: feedback already consumes
  the resolver, so its defect is reflection-only (`P6-CAP-2`) and it ships in 6U-1
  alongside `P6-UI-002`, which shares that root cause.
- `P6-UI-004` cannot be fully closed until `P6-DATA-2` lands, because the honest empty
  state depends on the backend distinguishing "no signals found" from "market not
  covered". Lane U can ship the copy change; Lane S makes it true.
- `P6-CI-11` must be reworked **in the same tranche as** `P6-DATA-1/2`, or real data will
  turn a required merge context red (risk `RK-15`).

**Sequencing rule (binding).** Lane U must not displace Lane I or Lane S work: a
good-looking UI is not production readiness. Equally, Lane I must not bury Lane U: four
`S`-sized UI defects currently make a working product look broken, and leaving them
behind months of infrastructure work is the wrong trade. **They run in parallel.**

---

## 5A. `P6-UI-0` — Current product UI reality check (executed 2026-09-19)

**Status: COMPLETE.** This was executed as the first Phase-6 product activity, ahead of
extended backend and infrastructure work, so that what the founder can actually use today
is established fact rather than inference. It audits the UI that already exists. **It is
not Phase 5A–5E implementation and changed no product code.**

### 5A.1 How it was run

The existing local development configuration, unmodified: `npm run bootstrap` →
`npm run demo:setup` → API on `127.0.0.1:8000`, Vite on `localhost:5173`. SQLite,
in-process queue, in-memory cache, fixture connectors, **mock LLM**. No AWS, no paid
provider, no external egress, no secrets, no flag changes. Verified at runtime:
`GET /health` → `{"status":"ok","mode":"local"}`; local Alembic at head `98289430a3ec`.

`CURRENT_UI_LAUNCH_RESULT = PASS` — meaning **the application started and served both
tiers**. It is a startup result, not a launch-readiness judgement; the readiness verdict
is §5A.8.

**Method limitation, disclosed:** the browser automation extension was not connected, so
no visual screenshot pass was possible. Findings below come from full source reading of
every route plus **live API verification against the running server**. Layout assessments
are therefore derived from the responsive class structure, not from rendered pixels, and
are marked as such. A visual pass remains worth doing.

### 5A.2 Headline result

The application runs, and the core loop — sign in → workspace → locations → scout
requests → scored opportunities → evidence — is **genuinely usable end to end on seeded
demo data, provided all three processes are running** (API, web **and worker** — see
§5A.7; `npm run dev` starts only the first two). It is a real product, not a shell.

Two things qualify that, and neither is cosmetic:

1. **Everything the founder will see is fixture data** — and, to the product's credit, it
   says so (§5A.4).
2. **A cluster of small, cheap UI defects makes shipped-and-working features look broken**
   — chiefly `P6-UI-001`, which discards every backend error message in the product.

### 5A.3 Route inventory

12 declared routes plus a catch-all (`apps/web/src/App.tsx:21-41`); 8 sidebar entries, one
operator-only (`apps/web/src/components/layout/nav.ts`).

| Route | Page | Auth | Role | In nav | Backend dependency | Flag | State |
|---|---|---|---|---|---|---|---|
| `/sign-in` | `pages/auth/SignIn.tsx` | no | — | — | `POST /auth/login` | — | **WORKING** |
| `/register` | `pages/auth/Register.tsx` | no | — | — | `POST /auth/register` | — | **WORKING** |
| `/` | `pages/Overview.tsx` | yes | any | ✅ | opportunities + scout-requests | — | **WORKING_WITH_FIXTURE_DATA** |
| `/onboarding` | `pages/Onboarding.tsx` | yes | any | ✅ | `POST /workspaces/{id}/onboarding` | — | **WORKING** |
| `/context` | `pages/CampaignContext.tsx` | yes | any | ✅ | 9 context collections (27 ops) | — | **PARTIAL** — create + delete only; no edit (`P6-PLAT-9`) |
| `/locations` | `pages/Locations.tsx` | yes | any | ✅ | locations + geo-coverage | — | **WORKING** |
| `/scout-requests` | `pages/ScoutRequests.tsx` | yes | any | ✅ | scout-requests | — | **WORKING_WITH_FIXTURE_DATA** |
| `/scout-requests/:id` | `pages/ScoutRequestDetail.tsx` | yes | any | — | scout-request, runs, schedule | `scout_scheduling_enabled` | **PARTIAL** — run history empty; schedule panel clickable and always fails (`P6-UI-002`) |
| `/opportunities` | `pages/Opportunities.tsx` | yes | any | ✅ | opportunities | — | **WORKING_WITH_FIXTURE_DATA** |
| `/opportunities/:id` | `pages/OpportunityDetail.tsx` | yes | any | — | opportunity, intelligence, feedback | `opportunity_feedback_enabled` | **WORKING_WITH_FIXTURE_DATA**; feedback correctly invisible |
| `/settings` | `pages/Settings.tsx` | yes | any | ✅ | `/auth/me`, workspaces, runtime | — | **PARTIAL** — roles card inert (`P6-UI-007`) |
| `/operations` | `pages/operations/Operations.tsx` | yes | **operator** | ✅ (operator) | 11 `/internal/system/*` | — | **WORKING**, but misreports 2 of 3 capabilities (`P6-UI-005/006`) |
| `*` | `pages/NotFound.tsx` | yes | any | — | — | — | **WORKING** |

**Verified live** against the seeded workspace: 4 locations (Dallas, London, Lagos,
Nairobi), 4 scout requests all `completed`, **12 opportunities — every one
`is_simulated: true`**, business-profile 200, audit-logs `[]`, jobs `total: 0`,
runs `total: 0`, schedule `404 "This request has no schedule."`, feedback
`503 capability_unavailable`.

**`MISSING_EXPECTED_ROUTE`** — routes a pilot user would reasonably expect and that do not
exist: forgot-password · email verification · change password · team/member management ·
notifications centre · analytics · exports · account security · billing. Team management
*appears* to exist as a Settings card and is inert.

### 5A.4 Fixture / simulation boundary — the product is honest

This was the single most important thing to check, and the answer is good. The UI labels
simulated data **prominently and in six places**:

- `components/common/badges.tsx:50-54` — a `Simulated` warning badge, tooltip "Generated
  from fixture data, not a live source".
- Rendered on `Overview.tsx:241`, `opportunities/OpportunityCardView.tsx:44`,
  `OpportunityDetail.tsx:133`, `opportunities/IntelligencePanel.tsx:286`.
- `OpportunityDetail.tsx:167-174` — a full warning callout: **"Simulated opportunity.
  Generated from fixture connectors for demonstration — not sourced from a live feed."**
- `ScoutRequestDetail.tsx:92-99` — **"Simulated sources.** This build uses fixture
  connectors. Generated signals are clearly labeled and not from live feeds."
- Stated *in advance* at `Onboarding.tsx:342` and `scouts/ScoutRequestDialog.tsx:208`.

**Consequence for the plan.** `P6-DATA-1` remains a launch blocker — the product still has
no real data source — but the "customer is misled into thinking fixtures are real
intelligence" risk is **materially lower than the completion audit implied**. The audit's
wording that simulated rows are "marked internally but presented as opportunities" is
corrected here: the marking is surfaced, prominently, on every view that shows them.

The genuine misleading case is narrower and is `P6-UI-004`: a user outside the four
fixture markets whose scout completes successfully with zero signals.

### 5A.5 `FOUNDER_INTERACTIVE_UI_NOW`

**Fully interactive** — sign in · register · onboarding wizard · business profile ·
locations (create, edit, geo-coverage) · campaign context (create/delete across 9
collections) · scout request create/list/detail · opportunity feed with filters, search
and sort · opportunity detail with evidence and intelligence panel · opportunity status
change · workspace create/switch · theme · operator console (queue, workers, schedules,
telemetry, capability registry and overrides).

**Partially interactive** — scout scheduling (panel renders, buttons enabled, every
mutation 503s) · campaign context (no edit path) · operator capability overrides (writes
succeed but 2 of 3 capabilities ignore them).

**View-only** — Organizations & roles · runtime status · notifications bell (permanent
"You're all caught up").

**Not currently demonstrable** — recommendations · approvals · creative generation ·
notifications with content · server-side analytics · exports · jurisdiction controls ·
team invites · password reset · billing. All are either Phase 5A–5E (§4.10, excluded by
`P6-D01`) or unbuilt account-lifecycle work (`P6-AUTH-1`, `P6-AUTH-2`).

### 5A.6 UI gap register

| UI ID | Screen / flow | Current state | User impact | Pilot blocker? | Backend dep | Tranche |
|---|---|---|---|---|---|---|
| `P6-UI-001` | **Every error toast, app-wide** | `api/client.ts:120-123` builds messages from `humanizeValidation(payload)`, which only reads a top-level `detail` key (`:41-62`). The **sole envelope for application errors** is `{"error":{"code","message","request_id"}}` (`app/core/errors.py:212`), emitted by all three registered handlers (`:220,224,229,244`) **including** `RequestValidationError`. No application error carries `detail`, so the fallback `Request failed (${status})` always fires. **Two things the fix must handle:** field-level detail lives at `error.details` (**plural**, `:214`) — read `message` **and** `details`, or every 422 collapses to "Request validation failed"; and `StarletteHTTPException` is **not** registered, so an unmatched route or 405 still returns Starlette's `{"detail":…}` and must keep working | The server's secret-free, well-written messages — "Opportunity feedback is not available yet.", "This request has no schedule." — **never reach a human**. Every failure in the product is a bare HTTP code | **UI-P0 — YES** | none | **6U-1** |
| `P6-UI-002` | Scout request detail → schedule | `SchedulePanel` mounts unconditionally (`ScoutRequestDetail.tsx:160`) and renders enabled "Schedule daily"/"Schedule weekly" (`SchedulePanel.tsx:123-131`); backend 503s the POST (`scouting_requests/routes.py:75,389`). Root cause: `scout_scheduling_enabled` is absent from `FeatureFlagsOut`, so the client cannot know | The most prominent unbuilt feature is presented as working. Click → opaque failure (compounded by `P6-UI-001`) → user concludes the product is broken | **UI-P0 — YES** | `P6-CAP-2` | **6U-1** |
| `P6-UI-003` | Sign-in | No "Forgot password?" link exists anywhere (`SignIn.tsx:105-128`); no reset route server-side | A forgotten password means permanent loss of the org, its workspaces and all data. Only recourse is registering a new account, which creates a new org | **UI-P0 — YES** | `P6-AUTH-2` | **6B** |
| `P6-UI-004` | Opportunities empty state | `Opportunities.tsx:293-297` shows "No opportunities yet — Run a scout request to generate…" — identical whether the user has never run a scout or ran one that returned zero signals (the guaranteed outcome outside the 4 fixture markets, `fixtures.py:156-167`) | The app advises the user to do the thing they just did. This is the one place fixtures genuinely mislead | **UI-P1 — YES** | `P6-DATA-2` | **6U-2** |
| `P6-UI-005` | Operations → capability overrides (feedback) | Feedback's **enforcement is already correct** — `feedback/routes.py:85-91` decides via `resolve_capability`. The defect is purely the **reflection**: `system/routes.py:94-96` populates `FeatureFlagsOut` from the raw global flag | An operator enabling feedback for one workspace sees "Enabled · Workspace override" on `/operations` and **still sees no feedback UI**, because the client gate reads the global reflection | **UI-P1** | **`P6-CAP-2`** (not `P6-CAP-1` — an earlier draft mis-attributed this; feedback already consumes the resolver) | **6U-1**, with `P6-CAP-2` |
| `P6-UI-006` | Operations → capability overrides (scheduling) | `scouting_requests/routes.py:75` reads the raw global flag and never calls `resolve_capability` | Operator sees "Enabled · Workspace override"; every schedule mutation still 503s. **Misreports** rather than under-reports | **UI-P1** | `P6-CAP-1` | **6C** |
| `P6-UI-007` | Settings → Organizations & roles | Static row + read-only `Badge` (`Settings.tsx:129-144`); no invite, role change or remove, and no explanatory empty state — unlike the Workspaces card directly beneath it, which does have `+ New` | Looks like team management, does nothing. A pilot user hunts for the invite button and finds none | **UI-P1** | `P6-AUTH-1` | **6B** |
| `P6-UI-008` | Sign-in | Live demo credentials printed in plain text (`SignIn.tsx:126-128`) plus a one-click "Use demo account" button (`:110-118`). **`SignIn.tsx` carries no environment guard on any of this**, so the constants compile into the production bundle — confirmed, the built `apps/web/dist/assets/index-*.js` contains `demo1234` | Anyone reaching an externally reachable deployment where the demo org was seeded gets a working **OWNER account inside a real tenant**. Blast radius is the demo org — tenant isolation still protects a pilot's own org. **Correction to an earlier draft:** this row previously claimed the account also grants the operator console. That holds only locally — `apps/api/app/db/seed.py:212` sets `demo_is_operator = environment in ("development","test")`, so `is_operator` is **False** in staging/production. The live `is_operator: true` observation came from a dev server. The finding stands on the unguarded credentials alone | **UI-P0 — YES for any external exposure** | none | **6U-1** |
| `P6-UI-009` | Scout request detail → run history | 4 requests show `completed`, but `/jobs` returns `total: 0` and `/runs` returns `total: 0` — the seed creates completed requests without durable job records | The founder clicks into a completed scout and sees an empty run history. Looks like data loss | **UI-P1** | seed only | **6U-2** |
| `P6-UI-010` | Sidebar + auth layout | Hard-coded customer-facing copy: "**Phase 1 & 2** — Scouting to explainable opportunities. **Creative generation arrives in Phase 3.**" (`sidebar.tsx:68-72`) and "Phase 1 & 2" (`auth/AuthLayout.tsx:54-56`) | Internal roadmap vocabulary on a customer surface, and stale — "Phase 3" shipped; creative generation is now Phase 5C and unbuilt | **UI-P1** | none | **6U-1** |
| `P6-UI-011` | Header, 768–1023px band | Hamburger + two fixed-width switchers (150px + 180px, `context-switchers.tsx:28,41`) + breadcrumbs with **no `min-w-0`/`truncate`** (`breadcrumbs.tsx:32-33`) + theme + bell + full user name, in a `h-14 px-4` row. The `overflow-x-auto` compact strip is `md:hidden` (`app-shell.tsx:83`) — disabled exactly in this band | Cramped or clipped header on portrait tablet. **Derived from class structure, not a rendered check** | **UI-P2** | none | **6U-2** |
| `P6-UI-012` | Dialogs on mobile | `dialog.tsx:31` is `w-full` with no `mx-*`; close button at `right-4 top-4` (`:39`) | At 390px the dialog is edge-to-edge with the close button against the screen edge. *(Height is fine — `:32` does carry `max-h-[90vh] overflow-y-auto`; an earlier draft of this audit claimed otherwise and was wrong.)* | **UI-P2** | none | **6U-2** |
| `P6-UI-013` | Session expiry | 401 clears the token (`AuthContext.tsx:67-70`) and redirects (`ProtectedRoute.tsx:24-26`); no "session expired" notice | After 12h the user is bounced to a blank sign-in mid-task. Softened by `intendedPath` preservation | **UI-P2** | `P6-AUTH-4` | **6U-2** |
| `P6-UI-018` | **Scout Requests → "Run now"** | `POST .../run` flips the request to `queued` and enqueues a durable job. **The worker is a separate process** (`apps/api/app/jobs/worker.py:756`) and `app/main.py`'s `lifespan` never starts one — `scripts/dev.sh` has **zero** references to it. Verified live: `GET /internal/system/workers` → `{"active_count":0,"workers":[]}`. The toast meanwhile promises "A background job is processing this scout" (`useScoutActions.ts:43`). **There is no in-process fallback on this route** — the run path is durable-only (`scouting_requests/routes.py:284` → `enqueue_scout_request` → `job_store.enqueue`). Note one decoy: the legacy `@register_job("run_scout_request")` on the synchronous `InProcessQueue` (`jobs/pipeline.py:97`, `infra/queue.py:41-46`) is **never called by the run route** and is not a fallback | **The primary action of the product's primary screen is a dead end under `npm run dev`.** The card flips to Queued and stays there forever; retrying is rejected with "Request is already queued or running." The founder will conclude the product is broken | **UI-P0 — YES for the walkthrough** | none — `npm run worker` is a third process (§5A.7) | **6U-1** |
| `P6-UI-019` | Overview / Scout list / Scout detail — "N signals" | Frontend reads `stats.signals_processed` (`Overview.tsx:99`, `ScoutRequests.tsx:120`, `ScoutRequestDetail.tsx:68`); the API returns `scanned` / `noise_filtered` / `signals_analyzed` / `opportunities`. Verified live: `{"scanned":9,"noise_filtered":1,"signals_analyzed":7,"opportunities":3}`. **The correct key is `signals_analyzed`** = `len(normalized)` (`jobs/pipeline.py:301`) — what survived noise **and** dedupe. It must **not** be derived client-side as `scanned − noise_filtered`: 9−1=8 ≠ 7, because one signal was a duplicate | **Every "signals" figure in the product renders 0** despite real values existing. Overview shows "4 noise filtered · 0 signals" | **UI-P1 — YES** | none | **6U-1** |
| `P6-UI-020` | Overview — "Active scout requests" | Counts only `running\|queued\|paused` (`Overview.tsx:94`); all 4 seeded scouts are `completed` | Renders **0** directly beside the hint "4 total". Reads as broken | **UI-P1** | none | **6U-1** |
| `P6-UI-021` | Header global search → Opportunities | `GlobalSearch` navigates to `/opportunities?search=…` (`global-search.tsx:17`), but `Opportunities.tsx:89-92` reads `searchParams` only in a `useState` **initializer**. Navigating within the same route does not remount, so the term is ignored — and the effect at `:114-117` then **rewrites the URL back**, erasing what the user typed | Global search silently fails whenever the user is already on Opportunities | **UI-P1** | none | **6U-2** |
| `P6-UI-022` | Opportunities pagination | `limit: 100` hard-coded, no pagination control and no truncation warning; the aria-live count reports the returned length | Beyond 100 opportunities the user silently loses rows while the UI confidently reports "100 opportunities" | **UI-P1** | `P6-PLAT-12` (cursor pagination) | **6U-2** |
| `P6-UI-023` | Locations | No delete or deactivate control — and no backend route to call | A mistyped location is permanent | **UI-P2** | backend route needed | **6U-2** |
| `P6-UI-024` | 404 handling | The catch-all sits **inside** `ProtectedRoute` (`App.tsx:24,38`) | An unauthenticated user hitting a bad URL is bounced to `/sign-in` and never sees a 404 | **UI-P2** | none | **6U-2** |
| `P6-UI-014` | Notifications bell | Permanent "You're all caught up. Live alerts arrive in a later phase." (`notifications.tsx:10-27`) | A control that can never do anything. Honest, but dead chrome | **UI-P2** | Phase 5E | **defer** |
| `P6-UI-015` | Search | `hidden lg:block` (`global-search.tsx:13`) with no tablet/mobile substitute | Search unavailable below 1024px | **UI-P2** | none | **6U-2** |
| `P6-UI-016` | Roles vocabulary | Six roles mapped in `lib/labels.ts:162-169`; only `owner` is ever assigned, so role-gate `false` branches (`SchedulePanel.tsx:132-136`) are unreachable | Dead presentation code; the UI will never show a non-owner role | **UI-P2** | `P6-AUTH-1` | **6B** |
| `P6-UI-017` | Settings → Account | Two read-only rows; no password change. User menu offers two entries that both go to `/settings` (`user-menu.tsx:61-66`) | No self-serve credential management | **UI-P2** | `P6-AUTH-2` | **6B** |

**What is genuinely right and should not be disturbed:** `opportunity_feedback_enabled` is
the reference dark-capability implementation — a pre-request client gate
(`useFeedback.ts:27-37`, `FeedbackPanel.tsx:80,102`) means **no request is ever issued
while dark**, and nothing renders. The operator surface is correctly non-enumerable
(`sidebar.tsx:34-36` filters; `RequireOperator.tsx:32-34` renders the 404 page in place
rather than redirecting, so existence is not leaked). Empty, error and loading states
exist as shared primitives (`components/common/states.tsx`). There is **no `<table>` and
no fixed page width** anywhere, so the responsive bones are sound.

### 5A.7 Founder walkthrough

Local only. **Three processes are required, not two** — this is the single most important
practical finding of `P6-UI-0`:

```bash
npm run bootstrap     # once
npm run demo:setup    # once — migrate + seed
npm run dev           # terminal 1 — API (127.0.0.1:8000) + web (localhost:5173)
npm run worker        # terminal 2 — REQUIRED, or "Run now" does nothing (P6-UI-018)
```

Then `http://localhost:5173`, sign in `demo@signalnest.dev` / `demo1234`.

`scripts/dev.sh` contains **zero** references to the worker, and the README quickstart does
not mention `npm run worker`. Without it the product's primary action silently dead-ends.

| # | Step | What works | What is simulated | Intentionally unavailable |
|---|---|---|---|---|
| 1 | Sign in | Full | — | No "forgot password" (`P6-UI-003`) |
| 2 | Overview | Stat cards, classification chart, by-market chart, recent opportunities, recent activity | All 12 opportunities, each badged **Simulated** | Two stat values render wrong — "0 signals" (`P6-UI-019`) and "Active 0 / 4 total" (`P6-UI-020`) |
| 3 | Locations | View 4 cities, create, edit, set geo-coverage radius | The 4 seeded cities | Jurisdiction controls (Phase 5D) |
| 4 | Campaign Context | Create/delete across 9 collections | — | **Editing** — delete and recreate only (`P6-PLAT-9`) |
| 5 | Scout Requests | List 4 completed requests, create a new one. **"Run now" works only if `npm run worker` is running** (`P6-UI-018`) | Fixture connectors, flagged in the create dialog | Live sources; edit; delete/archive |
| 6 | Scout detail | Request config, "Simulated sources" banner | Sources | **Run history is empty** (`P6-UI-009`); schedule buttons render and always fail (`P6-UI-002`) |
| 7 | Opportunities | Filter (verified live: `classification=early`→8, `min_score=61`→4, `search=Dallas`→3), sort, change status | All rows badged **Simulated** | Header global search fails when already on this page (`P6-UI-021`); no pagination past 100 (`P6-UI-022`) |
| 8 | Opportunity detail | Scores, why-it-matters, evidence/inference split, intelligence panel, explicit "Simulated opportunity" callout | The whole record | Feedback (correctly invisible); recommendations, approvals, creative (Phase 5) |
| 9 | Settings | Account read-only, theme, workspace create/switch, runtime status | — | Roles card inert (`P6-UI-007`); no password change |
| 10 | Operations *(operator)* | Capability registry, queue health, worker fleet, schedules, telemetry, override read/write | — | Overrides don't bind for 2 of 3 capabilities (`P6-UI-005/006`) |

### 5A.8 Verdict

`UI-READY-2` — `CURRENT_SIGNALNEST_UI_RUNS_BUT_REQUIRES_SMALL_UI_REMEDIATION_BEFORE_FOUNDER_WALKTHROUGH`

The founder can walk the product today and it will demo well **provided `npm run worker`
is running**. Fix `P6-UI-018`, `P6-UI-001`, `P6-UI-002`, `P6-UI-019` and `P6-UI-020`
first: together they are the difference between "a real product with a dark feature" and
"a product that appears broken on its primary screen". All five are `S`, repo-only, need
no operator input and no AWS, and none of them touches Phase 5A–5E scope.

---

## 5B. First Phase-6 implementation tranche after PR #167 merges

`FIRST_PHASE6_IMPLEMENTATION_TRANCHE = 6U-1 — Founder-visible UI truthfulness`
**run in parallel with** `6A — Baseline truth & gate closure`.

**Rationale.** The original recommendation was 6A alone (governance truth + CI gate
closure). `P6-UI-0` changed that, for a reason that is not cosmetic: **six independent
defects currently make a working product look broken**, all of them repo-only, `S`-sized,
requiring no operator decision, no AWS, no credentials and no Phase-5 scope:

| Item | One-line fix |
|---|---|
| `P6-UI-018` | Start the worker in `scripts/dev.sh` (or detect zero workers and say so) |
| `P6-UI-001` | Read `payload.error.message` in `api/client.ts:120-123` |
| `P6-UI-019` | Read `signals_analyzed`, not `signals_processed`, in 3 files |
| `P6-UI-020` | Count `completed` in the Overview "active" tile, or relabel it |
| `P6-UI-002` | Add `scout_scheduling_enabled` to `FeatureFlagsOut`; gate `SchedulePanel` like `FeedbackPanel` |
| `P6-UI-010` | Replace the stale "Creative generation arrives in Phase 3" chrome |

6A stays first-equal because `P6-CI-1` (promoting the two security contexts) and
`P6-GOV-1` (the tracked-doc contradiction) are prerequisites for trusting **any**
subsequent merge, and both are operator-side or `S`-sized.

**Why not lead with infrastructure.** Lane I cannot start until `P6-D03`/`P6-D04`/`P6-D05`
are answered and a fresh AWS authorization exists. Lane U and Lane P need none of that.
Starting them immediately keeps founder-visible progress moving while the external
prerequisites are obtained — which is exactly the parallelism §5.1 is built for.

**Explicitly not in the first tranche:** anything in §4.10 (`P6-D01`), anything in §4.11
(`P6-D02`), any AWS action, any flag activation, and **`P6-UI-006`**, whose real fix is
`P6-CAP-1` in Lane P (6C) rather than a UI patch.

**`P6-UI-005` IS in the first tranche.** An earlier draft excluded it alongside
`P6-UI-006` on the assumption that both were `P6-CAP-1`. They are not: feedback already
enforces correctly through `resolve_capability` (`apps/api/app/feedback/routes.py:85`), so
`P6-UI-005` is a **reflection-only** defect — i.e. `P6-CAP-2`, which 6U-1 already carries
for `P6-UI-002`. One change to `FeatureFlagsOut` closes `P6-UI-002` and `P6-UI-005`
together. `P6-UI-006` is genuinely different: `scouting_requests/routes.py:75` reads the
raw global flag and never consults the resolver.

---

## 6. Phase 6 objective

> Take SignalNest from a Phase 1–4 product foundation that runs only on simulated data in
> a dark staging environment, to a **single-tenant-proven, observable, deployable,
> legally operable system running on real data in a real production environment**, such
> that onboarding the first real customer is a business decision rather than an
> engineering risk.

Phase 6 is a **readiness and correctness** phase, not a feature phase. Where it adds code,
that code closes a gap between what the system **claims** and what it **does**.

**One acknowledged exception.** `P6-UI-023` (no way to delete or deactivate a location)
requires a genuinely new backend route, because none exists. It is the only item in Phase
6 that adds a product capability rather than closing a claim/behaviour gap, it is rated
UI-P2, and it is recorded here rather than hidden so the "no new capability" framing stays
honest.

---

## 7. Scope

### 7.1 IN SCOPE

- **6A** — Governance & baseline truth (§4.1) and the cheap CI gate closures.
- **6B** — Account lifecycle and authorization correctness (§4.3).
- **6C** — Platform correctness: configurations that cannot currently work (§4.4), LLM
  seam hardening (§4.5), capability-resolver coverage (§4.9).
- **6D** — Test and release gates (§4.7).
- **6E** — Infrastructure, observability and deployment (§4.6).
- **6F** — Live data enablement and privacy/retention (§4.2, §4.8).
- **6G** — Production environment authoring, WAF, DR and launch closeout (`P6-INF-1`,
  `P6-INF-11`, `P6-INF-12`).

### 7.2 OUT OF SCOPE

- **The Phase 5A–5E guided-action product loop** (§4.10) — **excluded by resolved
  operator decision `P6-D01` (§0)**, not merely by this plan's recommendation. It is
  specified, frozen, and unbuilt. It has its own plan set, its own resolved operator decisions O-1…O-11, its own
  open O-12, and its own 26-working-day ceiling. Folding it into Phase 6 would make Phase
  6 an unbounded container and would obscure the readiness work. **Phase 5 implementation
  remains a separate, separately-authorized track.** Phase 6 is its precondition: the
  Phase-5 plan's own §10 gates 5A/5C/5E activation on the retention policy (`P6-PRIV-1`),
  provider selection (`P6-LLM-6`) and infrastructure completion (§4.6) that Phase 6
  delivers. **Phase 6 does not deliver the normalized fail-closed jurisdiction axis** —
  it delivers only the fail-open market-matching fix (`P6-DATA-6`). The jurisdiction axis
  (`P6-P5-5`) remains a Phase-5 deliverable and remains an activation gate for 5C and 5D.
- **Billing, subscriptions, metering, entitlements** (§4.11) — **excluded by resolved
  operator decision `P6-D02` (§0)**, and independently deferred by the roadmap to Phase
  3G/§K. The audit found no repository requirement that independently mandates billing
  for a pilot. Not revisited inside Phase 6.
- **Connector breadth beyond the first live source** (`P6-DATA-5`) — each additional
  source carries its own ToS review; Phase 6 proves the *pattern* with one.
- **Website intelligence** (`docs/phase-3-plan.md` §H / Phase 3D).
- **Any reopening, reinterpretation or mutation of the ORD-017 closure chain.**

### 7.3 DEFERRED TO PHASE 7 / POST-LAUNCH

`P6-DATA-5` connector breadth · `P6-DATA-7` website intelligence · `P6-LLM-7` real
embedding provider · `P6-CI-9` SHA-pinning · `P6-CI-10` SPA error boundary ·
`P6-PLAT-5` OpenAPI security schemes · `P6-PLAT-7` empty packages · `P6-PLAT-10` empty
workspaces · `P6-PLAT-11` weak-secret detection · `P6-CAP-3` stale registry field · `P6-INF-16` CloudTrail Logs delivery · `P6-INF-17`
operator provisioning · `P6-INF-18` docs/Redis/ALB hardening · VPC flow logs and
endpoints · ECS autoscaling · SSO/OAuth · i18n · accessibility automation ·
load/performance baseline · independent pentest (recommended immediately post-launch) ·
route-splitting the SPA bundle.

**`P6-INF-13` (audit and ALB-log bucket lifecycle) is IN scope for 6E**, not deferred —
it is `S` effort and bears on both audit tamper-resistance and the hard cost ceiling. An
earlier draft listed it in both places; 6E governs.

---

## 8. Workstreams

| WS | Title | Items | Needs AWS? | Needs operator/legal? | Parallel with |
|---|---|---|---|---|---|
| **6A** | Baseline truth & gate closure | `P6-GOV-1..7`, `P6-CI-1`, `P6-CI-7` | No | GitHub ruleset action | everything |
| **6B** | Account lifecycle & authorization | `P6-AUTH-1..7`; UI surfaces `P6-UI-003`, `P6-UI-007`, `P6-UI-016`, `P6-UI-017` | No | Email transport choice | 6C, 6D, 6E |
| **6C** | Platform & AI correctness | `P6-PLAT-1..4`, `P6-PLAT-6`, `P6-PLAT-8`, `P6-PLAT-9`, `P6-PLAT-12`, `P6-LLM-1..5`, `P6-CAP-1`; UI surface `P6-UI-006` | No | `P6-LLM-6` for 6F only | 6B, 6D, 6E |
| **6D** | Test & release gates | `P6-CI-2..6`, `P6-CI-8`, `P6-CI-11` | No | No | 6B, 6C, 6E |
| **6E** | Infrastructure, observability & deployment | `P6-INF-2..10`, `P6-INF-13`, `P6-INF-14`, `P6-INF-15` | **Yes** | Apply + spend authorization | 6B, 6C, 6D |
| **6F** | Live data & privacy | `P6-DATA-1..4`, `P6-DATA-6`, `P6-PRIV-1..5`, `P6-LLM-6` | Partly | **Yes — legal/ToS + retention policy** | after 6C |
| **6G** | Production environment & launch closeout | `P6-INF-1`, `P6-INF-11`, `P6-INF-12` | **Yes** | Spend + launch authorization | after 6E, 6F |
| **6U-1** | **Founder-visible UI truthfulness** (added by `P6-UI-0`) | `P6-UI-001`, `P6-UI-002` and `P6-UI-005` (+ their shared backend half `P6-CAP-2`), `P6-UI-008`, `P6-UI-010`, `P6-UI-018`, `P6-UI-019`, `P6-UI-020` | No | No | everything — it is repo-only and tiny |
| **6U-2** | Founder-visible UI polish | `P6-UI-004`, `P6-UI-009`, `P6-UI-011`, `P6-UI-012`, `P6-UI-013`, `P6-UI-015`, `P6-UI-021`, `P6-UI-022`, `P6-UI-023`, `P6-UI-024` | No | No | 6B, 6C, 6D, 6E |

**Rationale for this shape rather than the suggested 6A–6E labels.** The audit produced
three natural constraint classes — repo-only work (unblocked, parallel), AWS-authorized
work (serialized behind human approval windows), and exogenous legal/operator work
(longest lead). The workstreams follow those constraints so that lanes genuinely run in
parallel. A "security" workstream was deliberately **not** created: security findings
here are not separable from the components that carry them, and isolating them would
produce a lane that cannot merge independently.

---

## 9. Entry criteria

Phase 6 may begin when all of the following are objectively true:

1. Phase 5 governance terminal state verified and left untouched — ✅ at authoring.
2. `main` baseline known and stable: HEAD `5d978d8…`, `origin/main` identical — ✅.
3. Repository consistent: single Alembic head `98289430a3ec`, 12 migrations, no multiple
   heads, all 12 carrying real downgrades — ✅ (computed statically).
4. Last CI on HEAD green: run `32147377858`, workflow **CI**, `completed/success` — ✅.
5. This plan reviewed by four independent lanes and merged through the protected workflow.
6. Operator decisions `P6-D01` and `P6-D02` answered — ✅ **RESOLVED 2026-09-19** (§0).
7. Infrastructure mutation boundaries explicit (§10.2).
8. Feature-activation boundaries explicit (§10.3).

## 10. Boundaries

### 10.1 This planning tranche
Docs only. No code, config, workflow, migration, contract, IaC or flag file may change.

### 10.2 Infrastructure
No `tofu apply`, `import`, `state` or `destroy`, and no AWS mutation, in **any** Phase-6
tranche without that tranche's own explicit operator authorization naming the resources.
`P6-INF-10` (the `PassRole` dispute) is a **mandatory pre-flight gate before any apply
that reaches a task definition** — self-declared by `infra/aws/operator-closure-contract.json`,
and the IAM policy simulator is explicitly insufficient to satisfy it.

### 10.3 Feature activation
All three capability flags stay `False` globally. No workspace override is created in any
Phase-6 tranche. Activation — including Phase 4B-B — is a separate authorization.

### 10.4 Live integration
No live provider credential, no live LLM call, no external egress, and no connector
enablement without a tranche authorization that names the provider and the legal sign-off.

### 10.5 Billing & spend
No billing code, no payment integration, no customer charge. AWS spend remains under the
existing hard ceiling; any change to it is an operator act.

---

## 11. Operator decision register

| ID | Question | Options | Technical consequence | Product consequence | Recommendation | Blocking? |
|---|---|---|---|---|---|---|
| `P6-D01` | Does Phase 6 subsume the unbuilt Phase 5A–5E product loop? | — | — | — | **✅ RESOLVED 2026-09-19 — (a) NO.** `PHASE_6_DOES_NOT_SUBSUME_PHASE_5A_THROUGH_5E`. Binding text in §0 | **CLOSED** |
| `P6-D02` | Is the first launch paid? | — | — | — | **✅ RESOLVED 2026-09-19 — (a) UNPAID PILOT.** `FIRST_EXTERNAL_LAUNCH = UNPAID_PILOT`. Binding text in §0 | **CLOSED** |
| `P6-D03` | Which live data source goes first, and is its ToS cleared? | RSS (PR #34, already built) vs. another | RSS is the only one with an implementation | Determines what a customer actually sees | **RSS**, conditional on legal sign-off — PR #34 is 13 files and +1,901/−0 of already-written work (the committed connector module itself is 676 lines and already at HEAD) | **Yes — `P6-DATA-1`** |
| `P6-D04` | LLM provider and credentials (O-7, still `UNDECIDED_RESERVED_TO_OPERATOR`) | OpenAI · Anthropic · both behind the seam | Both adapters exist and are untested | Blocks all generation and all real classification | Choose one for launch; test it properly under `P6-LLM-1` | **Yes** |
| `P6-D05` | Retention/deletion durations per data class (O-5, `UNDECIDED_RESERVED_TO_OPERATOR_LEGAL`) | Fixed tiers · delete-on-request · jurisdiction-dependent | No soft-delete column exists anywhere; the mechanism must be built either way | GDPR/CCPA erasure and portability are unserviceable until decided | Decide durations early — the mechanism is `L` effort and on the critical path | **Yes** |
| `P6-D06` | Promote `Container build and security` and `Revision reader` to required contexts? | Yes · No | Currently the whole container/IaC security apparatus is advisory | — | **Yes** — already decided as O-9 on 2026-09-01; only the GitHub action is outstanding | Non-blocking but `S` and high-value |
| `P6-D07` | Commit the six untracked Phase-5 plan files? | Yes · No · Redact first | They hold frozen contracts and authorization digests and exist in exactly one working tree | Loss would destroy the Phase-5 specification | **Yes** — verify no secret/account identifier first | Non-blocking, **urgent** |
| `P6-D08` | JWT storage: `localStorage` (today) or `HttpOnly` cookie? | Keep + add CSP/HSTS headers · Migrate to cookie + CSRF | Cookie migration is `L` and touches auth, CORS and CSRF | — | **Add headers now** (`M`), schedule the cookie migration for Phase 7 | Non-blocking |
| `P6-D09` | Multi-AZ RDS and Redis failover in production? | On · Off | Cost vs. availability, against a hard monthly ceiling | Determines the achievable RTO | Decide with `P6-INF-12`; a stated RTO/RPO is a launch requirement either way | Blocks `P6-INF-1` sizing |
| `P6-D10` | What is B-4 / B-5 / B-6? | Locate · Formally dispose | Undefined anywhere in the repository; the only reference is an untracked line | Unknown scope cannot be assessed or estimated | **Locate or formally dispose before 6E begins** | **Yes** |
| `P6-D11` | Email transport for password reset and verification | SES · third-party · none | `P6-AUTH-2` cannot ship without one; no mail transport exists anywhere | A locked-out customer is unrecoverable | SES — the account and region already exist | Blocks `P6-AUTH-2` |
| `P6-D12` | O-12 (Phase-5 register): should 5E notifications/analytics carry a flag? | Yes · No | Phase-5 scope only | — | Defer with the rest of Phase 5 | Non-blocking; **recorded so it is not lost with the untracked files** |

---

## 12. External prerequisites

Only items the audit proved are actually missing or undecided.

| # | Prerequisite | Why | Blocks |
|---|---|---|---|
| X1 | Legal/ToS sign-off for the first live connector | `connectors/rss.py:10-14` gates live egress on product-owner approval plus legal sign-off | `P6-DATA-1`, `P6-DATA-4` |
| X2 | LLM provider account + API key | O-7 unresolved; production forbids the mock provider | `P6-LLM-1`, `P6-LLM-6` |
| X3 | Retention/deletion policy from operator/legal | O-5 unresolved | `P6-PRIV-1` |
| X4 | Fresh AWS apply authorization (workload stage) | Foundation is applied; workload is not | all of 6E, 6G |
| X5 | GitHub ruleset change (2 contexts) + creation of `staging-reader-publish` / `staging-reader-run` **with protection rules** | Both are operator-side GitHub actions; neither environment exists today | `P6-CI-1`, `P6-INF-8` |
| X6 | SNS alarm destination (email/pager) | No SNS topic exists; 11 alarms fire into `[]` | `P6-INF-4` |
| X7 | Production domain, DNS zone and ACM certificate decisions | Zone and certs are consumed by id/ARN, never created | `P6-INF-1`, `P6-INF-3` |
| X8 | Email sending domain + verification | For `P6-AUTH-2` | `P6-AUTH-2` |
| X9 | Production spend ceiling decision | The existing hard ceiling is a staging figure | `P6-INF-1` |

**Already present and verified — do not re-request:** AWS account and Identity Center
tenancy; GitHub OIDC provider; the `staging` GitHub environment with required reviewers;
remote-state backend; ECR repositories with immutable tags and scan-on-push; the locked
OpenTofu provider; a working secret-hygiene control (verified: no real account id, ARN or
bucket name appears in any tracked file).

---

## 13. Risk register

| ID | Risk | Sev | Prob | Component | Mitigation | Detection | Rollback | Owner |
|---|---|---|---|---|---|---|---|---|
| RK-1 | Workload apply fails mid-graph on `PassRole`, leaving a partial apply | High | Med | ECS / IAM | Resolve `P6-INF-10` by authorized canary registration **before** any apply reaching a task definition | Apply-time `AccessDenied` | Documented teardown of partially created resources | Infra |
| RK-2 | Production outage undetected | **Critical** | **High** | Observability | `P6-INF-4` + `P6-INF-5` + `P6-INF-6` before any customer traffic | Currently **none** — that is the risk | n/a | Infra |
| RK-3 | Credential stuffing / OOM via the placeholder rate limiter | High | Med | API edge | `P6-PLAT-3` + `P6-INF-11` | No signal today (metrics are no-op) | ECS task replacement (attack resumes) | Backend |
| RK-4 | XSS → 12-hour unrevocable token theft | High | Med | SPA auth | `P6-CI-7` (PR #163) + `P6-AUTH-5` headers + `P6-AUTH-4` revocation | None today | Rotate the signing secret (logs everyone out) | Frontend + Backend |
| RK-5 | Non-reproducible Python builds admit an unreviewed dependency | High | Med | Build | `P6-PLAT-4` lockfile or `--require-hashes` | Trivy at publish only, against an unrecorded dependency set | Redeploy a prior digest | Backend |
| RK-6 | First real LLM call in production hits an untested error branch | High | **High** | LLM | `P6-LLM-1` — exercise both adapters against a sandbox before launch | Customer-visible failure | Fall back to the mock (forbidden in prod — so: none) | Backend |
| RK-7 | Unbounded LLM spend from one large scout run | High | Med | LLM | `P6-LLM-4` ceilings + quotas | Provider bill | Kill switch (needs `P6-CAP-1`) | Backend |
| RK-8 | Reader workflow auto-creates an unprotected environment and runs against the staging DB with no reviewer | High | **High** on first dispatch | CI/CD | `P6-INF-8` — create both environments with protection **before** first dispatch | None — the gate silently would not exist | Delete the environment; audit the run | Infra |
| RK-9 | Customer data cannot be erased on request | High | High | Privacy | `P6-PRIV-1` + `P6-D05` | Legal request arrives | None | Operator/legal |
| RK-10 | Cross-tenant leak ships undetected | **Critical** | Low | API | `P6-CI-3` cross-org route tests | No regression guard today | Revoke + notify | Backend |
| RK-11 | Phase-5 specification lost (untracked files, one machine) | Med | Med | Governance | `P6-GOV-2` | Irreversible when it happens | None | Operator |
| RK-12 | A reviewer acts on the false "nothing is provisioned" README | Med | **High** | Governance | `P6-GOV-1` | Already occurred within this audit | n/a | Infra |
| RK-13 | Security-red PR merges green | High | Med | CI | `P6-CI-1` | None today | Revert | Operator |
| RK-14 | W0 permission set loses its last standing assignment | High | Low | IAM | `ic_provisioning_closure._permanent_assignment_invariant` — the reserved role is **deleted** when the last assignment is dropped; re-assignment mints a **new suffix**, so the exact-ARN B-3 grant goes dead. Treat assignment changes as "a reviewed governance event, never routine hygiene" | Apply-time `AccessDenied` on a previously working grant | Re-grant against the new ARN | Operator |
| RK-15 | Real data breaks the required Integration smoke gate | Med | **High** | CI | `P6-CI-11` — rework the gate in the same tranche as `P6-DATA-1/2` | Required context turns red on the data-enablement PR | Revert the data tranche | Backend |

---

## 14. Testing requirements

Each Phase-6 tranche must land with tests. Minimum bar for the phase:

- **`P6-CI-3`** cross-organization HTTP tests on every customer-facing route family.
- **`P6-CI-4`/`P6-CI-5`** the migration round-trip and the job-store claim/lease path
  exercised on **PostgreSQL**, not only SQLite.
- **`P6-CI-2`** a browser/E2E harness covering at minimum: sign-up → onboarding → create
  location → create scout → run → see opportunity → sign out.
- **`P6-LLM-1`** both real adapters exercised against a sandbox or recorded fixtures,
  including timeout, 429, 5xx and non-JSON responses.
- **`P6-CI-8`** coverage measured and reported, so completeness claims become falsifiable.
- Failure-mode tests for `P6-PLAT-6` (dead-letter ends a schedule) and `P6-LLM-5`
  (degradation marking).

## 15. Review requirements

Every Phase-6 PR: one independent approval, all required contexts green, and — after
`P6-CI-1` — that includes `Container build and security` and `Revision reader`. Tranches
touching IaC, IAM, secrets or auth additionally require an adversarial security review
pass recorded in the PR.

## 16. Rollout, canary and rollback

Rollback is currently **absent** (`P6-INF-7`). Phase 6 must deliver, before any customer
traffic: a deployment workflow that records the deployed SHA and digest; a post-deploy
smoke test against the live environment; a one-command redeploy of the prior digest; and
a documented, **exercised** restore (`P6-INF-12`). The existing ECS circuit-breaker
rollback covers only a failed rollout, not a bad-but-healthy release.

## 17. Observability exit bar

No Phase-6 closeout while alarms notify `[]`. Required: an SNS topic with a real
subscription; a metrics exporter so `docs/operations/alerts.md` becomes implementable;
ALB 5xx and unhealthy-host alarms; and one proven end-to-end alert delivery test.

## 18. Deferred-work register

Everything in §7.3, plus §4.10 (Phase 5A–5E) and §4.11 (commercialization), carried
forward explicitly. Nothing in this plan cancels them.

## 19. Completion evidence requirements

Phase 6 closes only when **both** of the following hold. Item **E0 governs**: an earlier
draft listed only the demonstrations below, which could all be satisfied while roughly a
dozen of this plan's own launch blockers remained open.

### E0 — The binding condition

> **(i) Every item in §4 marked `LB? = Yes` and assigned to a Phase-6 workstream in §8,
> and (ii) every row in §5A.6 marked `UI-P0`, is closed — each with a cited artifact
> (merge SHA, test name, or recorded execution).**

Limb (ii) exists because the UI gap register lives in §5A.6 and uses a different column
(`Pilot blocker?`) with different values (`UI-P0`/`UI-P1`). Without it, a closeout could
satisfy every §4 blocker and all thirteen demonstrations while 6U-1 was never executed —
shipping a pilot whose sign-in page prints working credentials into a real tenant.

That set is, explicitly: `P6-GOV-1`, `P6-GOV-4`, `P6-DATA-1`, `P6-DATA-2`, `P6-DATA-3`,
`P6-DATA-4`, `P6-DATA-6`, `P6-AUTH-1`, `P6-AUTH-2`, `P6-AUTH-3`, `P6-AUTH-4`,
`P6-AUTH-5`, `P6-AUTH-6`, `P6-PLAT-1`, `P6-PLAT-2`, `P6-PLAT-3`, `P6-PLAT-4`,
`P6-PLAT-6`, `P6-PLAT-8`, `P6-PLAT-9`, `P6-LLM-1`, `P6-LLM-2`, `P6-LLM-3`, `P6-LLM-4`,
`P6-LLM-5`, `P6-LLM-6`, `P6-CAP-1`, `P6-INF-1`, `P6-INF-2`, `P6-INF-3`, `P6-INF-4`,
`P6-INF-5`, `P6-INF-6`, `P6-INF-7`, `P6-INF-8`, `P6-INF-9`, `P6-INF-10`, `P6-INF-11`,
`P6-INF-12`, `P6-INF-14`, `P6-INF-15`, `P6-CI-1`, `P6-CI-2`, `P6-CI-3`, `P6-CI-4`,
`P6-CI-5`, `P6-CI-6`, `P6-CI-7`, `P6-CI-11`, `P6-PRIV-1`, `P6-PRIV-2`, `P6-PRIV-4`,
`P6-CAP-2`.

**Limb (ii) — the `UI-P0` set, enumerated:** `P6-UI-001`, `P6-UI-002`, `P6-UI-003`,
`P6-UI-008`, `P6-UI-018`.

No ellipsis or range appears in that enumeration deliberately: it is binding text, and a
range is where an omission hides.

**Conditional rows — now determinate under `P6-D02`.** `P6-PRIV-3` (DB-level audit
immutability) and `P6-PRIV-5` (audit coverage) were marked "Yes **if** compliance review
is a launch requirement". `P6-D02` resolved to an **unpaid design-partner pilot**, and no
repository authority imposes a compliance review on that cohort. Both therefore sit
**outside E0 for the pilot** and move to **P1 — required before general availability**.

This is a deliberate, recorded narrowing, not an omission. It does **not** touch
`P6-PRIV-1` (retention/deletion) or `P6-PRIV-2` (LLM prompt data policy), which remain
**inside E0**: pilot users are data subjects, and an unpaid pilot does not suspend
GDPR/CCPA erasure or a third-party data-processing position.

**Two couplings recorded rather than left implicit.** (a) `P6-PRIV-5` has a limb that is
**not** about compliance: zero `record_audit` coverage on workspace creation and on the 27
campaign-context operations means no application-level incident forensics for pilot users.
CloudTrail and structured logs cover part of it, which is why deferring is still
defensible — but the deferral rests on that partial coverage, not on compliance alone. It
also compounds `P6-AUTH-7` (no role gate on workspace creation) into "any member creates
workspaces, unlogged" in a pilot that E0 requires to be multi-user (E7). (b) Because
`P6-PRIV-5` is P1, **E10's deletion demonstration must be independently verifiable** —
it cannot rely on an audit record that Phase 6 is not required to produce.

A closeout that demonstrates E1–E13 while any of the above is open is **not** a Phase-6
closeout. No demonstration substitutes for an open blocker.

### E1–E13 — The demonstrations

Each must be evidenced by a recorded execution, not by the existence of code:

1. A production environment exists, is applied, and serves the SPA and API at their
   intended names over valid TLS.
2. A deployment records its SHA and digest; a rollback to the prior digest has been
   **executed** and recorded.
3. A restore from backup has been **executed** and recorded, with stated RTO/RPO.
4. An alert has been **delivered** end-to-end to a human destination, from a real alarm
   transition — not a console test.
5. **A real connector returns real signals for at least one market that the operator does
   not control and that is not a fixture market.** A graceful empty state does **not**
   satisfy this item — `P6-DATA-2` is closed under E0 separately, and an explanation for
   an empty result is not a substitute for the product returning data.
6. A real LLM provider has served the pipeline under test, including its timeout, 429,
   5xx and non-JSON failure branches.
7. A second user has been invited to an organization, holds a **non-OWNER** role, and the
   full authorization matrix — including the `P6-AUTH-3` audit-route case — is proven by
   test.
8. A password reset has been completed end to end by a user who did not know their
   password.
9. Cross-organization isolation is proven by route-level tests on **every**
   customer-facing route family.
10. A data-deletion request has been serviced end to end by an implemented mechanism.
11. All six CI contexts are **required** and green, and the reworked Integration smoke
    gate (`P6-CI-11`) asserts against real data rather than fixtures.
12. Zero open HIGH/CRITICAL dependency alerts on runtime dependencies.
13. No tracked document contradicts tracked evidence, and the legacy branch-protection
    endpoint no longer misreports `main` (`P6-GOV-6`).

### Explicitly NOT a Phase-6 exit condition

Phase 5A–5E functionality; billing; connector breadth beyond the first live source;
anything in §7.3. Phase 6 closing does **not** mean SignalNest is feature-complete — it
means the product that exists is safe to put in front of an external customer.

## 20. Non-authorization statement

This plan authorizes no implementation, no migration, no dependency change, no test
execution, no flag change, no deployment, no AWS access, no provider call, no spend, and
no change to PR #34 or to the ORD-017 closure chain. Each workstream requires a fresh
authorization naming this plan and that workstream.
