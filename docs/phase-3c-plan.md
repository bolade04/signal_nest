# Phase 3C Implementation Plan — Human Feedback Loop (Scoring & Evidence)

**Batch:** Phase 3C, sub-batch 3C-A (planning).
**Nature:** documentation only — **no product code, no test code, no migration, no
OpenAPI/contract change, no feature flag, no dependency change, no CI change, no
runtime behavior change.**
**Branch:** `docs/phase-3c-plan` (from `main` at
`6db5620e19290b900367c5a66ed81b27730a22a4`).
**Alembic head:** single head `b2c3d4e5f6a7` (unchanged; no new migration).

_Independent-review status: NO INDEPENDENT THIRD-PARTY REVIEW COMPLETED._

---

## 1. Status and purpose

- **Phase 3B is complete** — scouting run history (SB-A), schedule persistence and
  durable tick execution (SB-B), schedule API and frontend controls (SB-C), and
  four-market hardening + closeout (SB-D) are all merged; scheduling ships dark
  (`scout_scheduling_enabled = False`).
- **Phase 3C is only partially defined.** The roadmap
  (`docs/phase-3-plan.md` §"Phase 3C — Scoring and evidence") lists opportunity
  scoring, source credibility, geographic fit, commercial intent, confidence,
  evidence-backed explanation, and a **human feedback loop**.
- **Most of that roadmap scope is already implemented on `main`** — it was
  delivered ahead of its phase label under Phase 3B Batch 3 (deterministic
  intelligence core), Batch 4A (persistence), and Batch 4B/4C (read API + UI).
  See the matrix in §4.
- **The one clearly-absent residual Phase 3C capability is the human feedback
  loop.** No feedback capture (model, endpoint, or UI) exists.
- **This plan defines scope only.** It authorizes no implementation, no schema
  change, no API, no UI, no feature-flag creation, and no production enablement.
  Implementation of the feedback loop (3C-B onward) is gated behind the owner
  decision gate in §19.

## 2. Source of authority

| Document | Authority | What it establishes |
| --- | --- | --- |
| `docs/phase-3-plan.md` §"Phase 3C — Scoring and evidence" | Roadmap / architecture guidance | Original Phase 3C intent: "Opportunity scoring · source credibility · geographic fit · commercial intent · confidence · evidence-backed explanation · human feedback loop." |
| `docs/phase-3b-next-work-decision-brief.md` §2 (C2), §4.2 | Binding blocking analysis | Phase 3C is deferred; **no** implementation-ready plan exists (`docs/phase-3c*` was absent); it "requires owner product decisions before it can even be scoped." |
| `docs/phase-3b-implementation-plan.md` §17.19, §462–465, §1770 | Implementation closeout | "Human feedback deferred to Phase 3C." Human feedback / thumbs / approval are explicitly out of scope for all Batch 4 work. |
| `docs/phase-3b/signal-intelligence-design.md` §7, §11, §14–19 | Implementation closeout / ADR | Delivers the deterministic scoring core (`SCORING_VERSION = "3b.1"`, eight factors) and the `signal_intelligence_records` persistence — most of the 3C scoring/evidence scope, ahead of the phase label. §11 defers API/frontend exposure and model-backed enrichment. |
| `docs/operations/signal-scoring-operations.md` | Operations runbook | Documents the shipped scoring/intelligence read path in production-operations terms. |
| `docs/verification/sb-d-schedule-closeout.md` | Implementation closeout | Confirms scheduling remains dark; establishes the "dark-first, closeout-gated" batch convention this plan follows. |

**Three distinct layers must not be conflated:**

- **Roadmap intent** — what `phase-3-plan.md` *assigned* to Phase 3C.
- **Implemented reality** — what already exists on `main` (§4/§5), regardless of
  the batch label under which it shipped.
- **Product decisions still required** — the choices (§6, §19) that must be made
  before the residual feedback-loop scope can move to code.

## 3. Original Phase 3C roadmap scope

Verbatim scope from `docs/phase-3-plan.md` §"Phase 3C — Scoring and evidence":

1. Opportunity scoring
2. Source credibility
3. Geographic fit
4. Commercial intent
5. Confidence
6. Evidence-backed explanation
7. Human feedback loop

## 4. Current implementation status

Each "implemented" claim below was verified against current-main source before
writing. **Implemented means the code path exists and is exercised by tests; it
does not assert perfect calibration, tuned weights, or production enablement.**

| Roadmap capability | Current state | Implementation evidence | Remaining work |
| --- | --- | --- | --- |
| Opportunity scoring | **Implemented** | `apps/api/app/intelligence/scoring.py` — `score_candidate()`, `SCORING_VERSION = "3b.1"`, eight weighted factors summing to 100 (asserted at import). | None for this plan. Weight tuning/calibration is not Phase 3C feedback-loop scope. |
| Source credibility | **Implemented** | `scoring.py` `source_quality` factor (weight 15) from `_SOURCE_CREDIBILITY` priors in `app/scoring/opportunity.py`. | None. |
| Geographic fit | **Implemented** | `scoring.py` `market_fit` factor (weight 10, `inside_scout_area`); `app/signals/models.py` `SignalLocationEvidence` (resolved market + confidence + evidence). | None. |
| Commercial intent | **Implemented** | `scoring.py` `commercial_usefulness` factor (weight 10) via `score_validation` + buying-intent bonus; `has_buying_intent` on `ExtractedIntelligence`. | None. |
| Confidence | **Implemented** | `scoring.py` `_confidence_factor()` (weight 5); `IntelligenceScore.version/total/classification/factors`. | None. |
| Evidence-backed explanation | **Implemented** | `app/intelligence/models.py` `EvidenceSpan`; `signal_intelligence_records` table (migration `0155a5c468e3`) persisting `facts`/`inference`/`relevance`/`score_components`/`provenance`; read API `GET …/opportunities/{opportunity_id}/intelligence` (`app/opportunities/routes.py` + `app/intelligence/read_service.py` + `OpportunityIntelligenceResponse`); frontend `apps/web/src/pages/opportunities/IntelligencePanel.tsx` + `useOpportunityIntelligence.ts`. | None. |
| **Human feedback loop** | **Absent** | No feedback model, table, endpoint, or UI. No `feedback`/`rating`/`approval` capture on opportunities or intelligence records (status mutation `PUT …/opportunities/{id}/status` is operator workflow, not user feedback). | **All of Phase 3C residual scope (§5, batches 3C-B → 3C-D).** |

**Conclusion:** the scoring/evidence half of Phase 3C is delivered; the residual
Phase 3C deliverable is the human feedback loop.

## 5. Residual Phase 3C scope

**Product capability.** A workspace user can provide **structured feedback on a
specific opportunity or its persisted intelligence result**, so SignalNest records
whether that result was **useful or not useful**, along with an **optional
structured reason**.

**The initial implementation is capture-only.** Explicitly, in the first release:

- Feedback **must not** alter score calculation (`score_candidate` /
  `SCORING_VERSION` stay untouched).
- Feedback **must not** trigger automatic rescoring of any signal or opportunity.
- Feedback **must not** train, fine-tune, or otherwise automatically adjust any
  model or weight.
- Feedback **must not** influence any other customer, workspace, location, or
  market.
- Feedback **must not** become an autonomous publishing, campaign, or ad action.

Feedback is a **recorded observation** for later human/offline analysis only.

## 6. Product-decision register

Every row is a product decision. **All eleven decisions below were APPROVED by the
product owner on 2026-07-18 — the recommended defaults were accepted in full** (see
the decision gate, §19, now fully checked). The "Recommended default" column is the
**approved policy** for the first version. This approval authorizes 3C-B planning to
proceed; it does **not** enable any feature (`opportunity_feedback_enabled` stays
`False`) or change any runtime behavior.

| Decision | Options | Recommended default (first version) | Owner decision required? | Implementation impact |
| --- | --- | --- | --- | --- |
| **Feedback shape** | binary useful/not; numeric rating; structured reasons; optional free text | Binary useful/not-useful **plus** optional structured reason; **no** unrestricted free text unless separately approved | Yes | Determines column type + validation surface in 3C-B/3C-C |
| **Feedback target** | opportunity; intelligence record/version; run result; recommendation | Versioned linkage to the intelligence record (`analysis_version` + `scoring_version` + `fingerprint`) **and** the parent opportunity, so feedback stays attributable when intelligence is re-scored to a new row | Yes | FK columns + attribution stability in 3C-B |
| **Mutability** | immutable event history; one mutable current rating per user; one mutable workspace rating | Immutable audit history; add a derived "current" projection **only if** the UI requires it | Yes | Table shape + uniqueness policy + 409 behavior |
| **Who may submit** | all workspace members; owner/admin/marketer; narrower editor role | Owner/admin/marketer (mirrors existing schedule-mutation editor gate) | Yes | Authorization guard in 3C-C |
| **Visibility** | submitter only; all workspace members; authorized editors only | Authorized editors (owner/admin/marketer) | Yes | Read authorization + response filtering in 3C-C |
| **Retention** | indefinite; fixed retention window; tied to workspace deletion lifecycle | Tied to workspace deletion lifecycle (cascade with workspace); no separate purge job initially | Yes | FK `ondelete` + retention docs |
| **Reason taxonomy** | fixed enum | **APPROVED enum** — positive: `useful_insight`, `strong_evidence`, `commercially_relevant`, `correct_market`; negative: `irrelevant`, `wrong_market`, `weak_evidence`, `duplicate`, `outdated`, `not_commercially_useful`, `other` | Yes | Enum/validation in 3C-B/3C-C |
| **Free-text notes** | included; deferred | **Deferred** (sensitive-data, prompt-injection, moderation, retention, export/deletion burden) | Yes | Avoids untrusted free text in first slice |
| **Scoring influence** | capture only; manual analyst review; offline aggregate analysis; real-time score change | **Capture only** | Yes | Keeps scoring deterministic; no worker in first slices |
| **Cross-market behavior** | isolated; aggregated | Strictly isolated — Dallas feedback never affects London/Lagos/Nairobi | Yes (confirm) | Isolation tests in 3C-B/3C-D |
| **User identity / attribution** | reveal submitter to all; reveal to editors only; hide | Reveal submitter to authorized editors only | Yes | Response schema in 3C-C |
| **Feature flag & rollout** | — | Dedicated dark flag, recommended name `opportunity_feedback_enabled = False` | Yes | **Planning recommendation only — not added now** |

## 7. Data model proposal (conceptual, non-binding)

For evaluation in **3C-B**. No migration SQL is written here; field names are
illustrative and subject to the §19 decision gate.

Proposed table (working name `opportunity_feedback`), fields to evaluate:

- `id`
- `organization_id` (FK, `CASCADE`)
- `workspace_id` (FK, `CASCADE`)
- `opportunity_id` (FK, `CASCADE`)
- `intelligence_record_id` (FK to `signal_intelligence_records`) **and/or** the
  `analysis_version` + `scoring_version` + `fingerprint` reference so feedback
  stays attributable across immutable re-scored rows
- `location_id` / market reference where applicable (nullable, `SET NULL`)
- `submitted_by_user_id` (FK)
- `is_useful` (or `sentiment`)
- `reason_code` (nullable enum)
- optional sanitized `note` **only if approved** (deferred per §6)
- `created_at` (immutable)
- optional supersession/reference fields **only if** edits are supported

**Invariants (binding on 3C-B regardless of final field choices):**

- All foreign keys are tenant-scoped; no cross-workspace or cross-organization
  reference is representable.
- Authorization is workspace-scoped (§8).
- `created_at` is immutable.
- Feedback must reference an **existing** opportunity / intelligence result within
  the same workspace.
- Version attribution must remain stable when intelligence is re-scored (new
  intelligence rows are immutable; feedback keeps pointing at the row/version it
  was given for).
- Deletion/retention behavior is explicit (§6 retention decision).
- The immutable-history vs. current-projection choice (§6 mutability) is settled
  before the migration is written.

## 8. Authorization model

- **Read.** Recommended: authorized editors (owner/admin/marketer) may view
  feedback; final visibility per the §6 decision. Reads are workspace-scoped.
- **Write.** Recommended: owner/admin/marketer only, mirroring the existing
  schedule-mutation editor gate.
- **Isolation — every request must validate, in order:** organization →
  workspace membership → opportunity ownership within that workspace →
  intelligence-record ownership within that workspace → market/location
  consistency. Out-of-scope resources return a **hidden 404** (not 403) where
  consistent with existing repository policy, so cross-tenant existence is not
  leaked.

## 9. Security and privacy requirements

Customer feedback is **untrusted input** and must be treated as such.

- IDOR protection on `opportunity_id` / `intelligence_record_id` (scoped lookups
  only; hidden 404 for out-of-scope IDs).
- Strict tenant isolation and cross-market isolation (Dallas/London/Lagos/Nairobi
  never blend).
- Input allowlists (reason code ∈ approved enum) and value validation
  (`is_useful` boolean/enum only).
- Length limits on any accepted string.
- Control-character stripping; reuse the existing `sanitize_text()` pattern from
  `app/intelligence/extraction.py` for any accepted text.
- HTML/script rejection or safe encoding for any accepted text.
- Prompt-injection containment: any stored text is defanged and is **never**
  placed into a downstream model prompt without separate review (free text is
  deferred in the first slice precisely to avoid this).
- Audit logging of every write and authorization rejection (§14).
- No raw feedback text in unsafe logs.
- No raw database errors or exception/stack detail in API responses.
- Rate limiting / abuse protection on the write endpoint.
- Explicit retention and deletion behavior (§6); privacy/export considerations
  documented before any free-text is ever enabled.
- **No automatic model training** and **no unreviewed downstream prompt
  inclusion** of feedback content.

## 10. Proposed API shape (conceptual — for 3C-C)

Not implemented here; paths marked conceptual require confirmation against
existing routing conventions during 3C-C.

```
POST /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/feedback
GET  /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/feedback
```

If version attribution (§6 feedback-target decision) requires it, an
intelligence-record-scoped variant may be preferred instead. **Mark as conceptual
until confirmed.**

Expected responses:

| Status | Meaning |
| --- | --- |
| `201` | Feedback created |
| `200` | Feedback read / list |
| `403` | Role denied (submitter/reader not an authorized editor) |
| `404` | Scoped target missing or hidden (out-of-workspace opportunity/record) |
| `409` | Conflict — only if a single-current-feedback policy is chosen (§6 mutability) |
| `422` | Invalid reason code or value |
| `503` | Feature disabled (flag off) — consistent with existing feature-gated mutations |

## 11. Frontend behavior proposal (for 3C-D)

- **Control location.** Feedback control co-located with the existing
  `IntelligencePanel` / opportunity detail surface.
- **Actions.** Useful / not-useful; optional structured-reason dialog.
- **States.** Submitted state; update/undo policy per the §6 mutability decision;
  read-only presentation for non-editor roles; loading; empty (no feedback yet);
  API error; **feature-disabled** (flag off → control hidden or disabled, no error
  noise).
- **Accessibility.** Keyboard-operable controls, labelled buttons, dialog focus
  management.
- **Cache isolation.** Query keys scoped by workspace **and** opportunity **and**
  intelligence version; no stale feedback flash when switching markets; a mutation
  on one opportunity must not invalidate another request's cache.
- **Isolation testing required** across Dallas/London/Lagos/Nairobi (mirrors the
  SB-D `schedule-panel.isolation.test.tsx` pattern).

## 12. Proposed implementation batches

### 3C-A — Planning (this document)

- **Scope:** `docs/phase-3c-plan.md` only.
- **Entry:** Phase 3B merged; Phase 3C discovery complete.
- **Exit:** product decisions recorded (register + gate); plan reviewed and merged.
- **Non-goals:** any runtime behavior, schema, API, or UI.
- **Status:** **COMPLETE.**

### 3C-B — Feedback persistence foundation (dark)

- **Scope:** additive feedback persistence (table + repository/service
  foundation); dedicated dark feature flag (`opportunity_feedback_enabled=False`);
  storage-level tenant/market invariants; one additive migration; tests.
- **Non-goals:** public API; frontend; scoring changes; automatic learning.
- **Entry:** §19 decision gate approved; 3C-A merged.
- **Exit:** migration green; **single Alembic head**; dark persistence tested;
  tenant/market isolation proven (incl. PostgreSQL concurrency).
- **Status:** **COMPLETE — dark feedback persistence merged and verified.**

### 3C-C — Feedback API (feature-gated)

- **Scope:** feature-gated create/read endpoints; authorization (editor gate +
  IDOR); audit logging; OpenAPI + generated TypeScript contract regeneration;
  backend integration tests.
- **Non-goals:** frontend; score changes; worker; live RSS.
- **Entry:** 3C-B merged.
- **Exit:** exact-head CI green; contract clean (no drift); role and IDOR tests
  green; 503-when-disabled verified.
- **Status:** **COMPLETE — feature-gated feedback API merged and verified.**
  Merged via PR #54 through the protected workflow (squash; no admin bypass).
  Reviewed head `5fc7ba9feed873c6969e2d3794bf12a4eec51f5c`; squash-merge SHA
  `0544fa1ebfcfde5a4d671e00032a7519f8375f66`; post-merge push CI `29663244389`
  green on all five jobs (backend **639 passed, 0 skipped**; frontend **53
  passed**), including the PostgreSQL feedback and dedicated feedback-concurrency
  tests. No new migration (single Alembic head `4945b98229e6` unchanged); generated
  contracts clean. The API stays **feature-gated and dark**
  (`opportunity_feedback_enabled = False` — every operation answers 503). See
  `docs/verification/3c-c-feedback-api.md`.

### 3C-D — Feedback UI and closeout

- **Scope:** customer feedback controls; query/cache isolation; four-market
  frontend isolation tests; operations documentation; closeout verification.
- **Non-goals:** automated score learning; production flag enablement; model
  retraining; campaign generation.
- **Entry:** 3C-C merged.
- **Exit:** Phase 3C implementation complete **but dark**; production rollout
  remains a separate decision (§15).
- **Status:** **NOT STARTED.** Residual scope: UI; client integration; cache
  isolation; multi-market UI isolation; runbook; controlled internal rollout
  preparation; final Phase 3C closeout. This docs update does **not** authorize
  3C-D to begin.

## 13. Testing strategy

**Backend (3C-B/3C-C):** model constraints; repository isolation; role
authorization (editor allowed, viewer denied); cross-organization denial;
cross-workspace denial; cross-opportunity denial; market/location mismatch
rejected; valid useful/not-useful capture; invalid reason code (`422`);
duplicate behavior per the selected mutability policy; feature-disabled (`503`);
audit-event creation; sanitization of any accepted string.

**PostgreSQL-gated (`TEST_POSTGRES_URL`):** uniqueness and concurrent-submission
behavior; FK integrity; transaction rollback on conflict; duplicate-current-
feedback race **if** a single-current policy is selected.

**Frontend (3C-D):** submit feedback; selected state; error recovery;
viewer/read-only behavior; cache isolation; Dallas/London/Lagos/Nairobi
separation; no stale rendering between opportunities; feature-disabled state;
accessibility.

**Contracts and migration (3C-B/3C-C):** single Alembic head; `alembic check`;
OpenAPI regeneration; no generated TypeScript drift.

## 14. Observability and audit

Planned structured events (implemented in 3C-B/3C-C, not now):

- `opportunity_feedback_submitted`
- `opportunity_feedback_superseded` (only if edits are permitted)
- `opportunity_feedback_authorization_rejected`
- `opportunity_feedback_invalid_reason`
- `opportunity_feedback_feature_disabled_attempt`

Every event carries the acting user and scoped resource IDs (organization,
workspace, opportunity, intelligence record). **Raw notes are never logged.**
Dashboard/alert recommendations are conceptual until 3C-D and are distinguished
here from the concrete future log events above.

## 15. Rollout plan

- Implementation ships **dark** (`opportunity_feedback_enabled=False`).
- Enable first in a **non-production** environment.
- Exercise a **single internal workspace**: verify write, read, and audit.
- Verify market isolation (four markets independent).
- Verify **no score change** occurs from feedback.
- Enable for a limited customer cohort **only after explicit approval**.
- **Merging any 3C batch does not imply production rollout.**

## 16. Rollback plan

- Turn `opportunity_feedback_enabled` off (kill-switch): writes are refused
  (`503`), consistent with other feature-gated mutations.
- Decide, at enablement time, whether reads remain available while writes are off.
- Existing feedback records are **preserved** (no destructive purge on rollback).
- No effect on intelligence scoring, scheduling, manual scouting, or live RSS.
- Migration rollback is normally avoided; the additive table's `downgrade` exists
  but is used only under incident policy.

## 17. Explicit non-goals

- No opportunity scoring changes; no `SCORING_VERSION` bump for feedback.
- No source-credibility changes.
- No automatic model training; no online learning.
- No automatic rescoring; no recommendation ranking changes.
- No live RSS; no connector work; **no PR #34 changes**.
- No scheduling enablement.
- No campaign or ad generation; no autonomous posting.
- No Batch 5.
- No production rollout.
- No unrestricted free text unless separately approved.

## 18. Relationship to other tracks

- **Live RSS / PR #34.** Preserved and deferred. It is the connector track
  (controlled RSS egress on branch `feat/phase-3b-live-rss-controlled-egress`),
  blocked on outstanding owner/legal decisions, and is **not** a Phase 3C
  dependency. This plan changes nothing about it.
- **Scheduling.** Phase 3B is complete; scheduling remains **dark**. The feedback
  loop does **not** depend on production schedule enablement.
- **Batch 5 (the Phase 3B-track successor) remains undefined.** It is **not**
  included here and **must not** be inferred from Phase 3C.

## 19. Decision gate before 3C-B

**3C-B MUST NOT BEGIN UNTIL THIS DECISION GATE IS APPROVED.**

**Gate status: APPROVED by the product owner on 2026-07-18** (all recommended
defaults accepted). Each decision below is approved as documented in §6. This gate
approval authorizes 3C-B *scoping/implementation planning* only; it does **not**
enable the feature or permit production rollout (§15), which remain separate
decisions.

- [x] Feedback shape — binary useful/not-useful **plus** optional structured reason; no free text (§6)
- [x] Feedback target / version linkage — versioned intelligence-record reference (`analysis_version` + `scoring_version` + `fingerprint`) **and** parent opportunity (§6)
- [x] Allowed submitter roles — owner/admin/marketer (editor gate) (§6)
- [x] Visibility — authorized editors only (owner/admin/marketer) (§6)
- [x] Mutability — immutable audit history; derived "current" projection only if the UI requires it (§6)
- [x] Retention — tied to workspace deletion lifecycle (cascade); no separate purge job initially (§6)
- [x] Reason taxonomy — approved enum (§6)
- [x] Free-text policy — deferred; no unrestricted free text in the first slice (§6)
- [x] Scoring influence — capture only; no scoring impact (§6)
- [x] Feature-flag naming — `opportunity_feedback_enabled` (default `False`) (§6)
- [x] Rollout boundary — first enablement in a single non-production internal workspace (§15)

## 20. Phase 3C success definition

**Overall status: PHASE 3C IN PROGRESS — 3C-A, 3C-B, AND 3C-C COMPLETE; 3C-D NOT
STARTED.** The feedback API is merged but remains dark
(`opportunity_feedback_enabled = False`); no production rollout has begun. Phase 3C
is **not** complete because 3C-D (feedback UI + closeout) has not started.

Phase 3C is complete when:

- Feedback persistence (3C-B) is merged.
- Feedback API (3C-C) is merged.
- Feedback UI (3C-D) is merged.
- Four-market isolation is verified end to end.
- Audit and operations documentation are merged.
- Exact-merge-SHA CI is green.
- The feature **remains dark** unless separately approved for rollout.
- There is **no scoring influence** in the initial release.
