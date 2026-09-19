# Phase 5C — Text Creative Generation

**Parent:** `docs/project-phase-5-plan.md`. **Status:** PLANNED / DOCS-ONLY —
authorizes nothing. **Schedule slot (continuous round, master plan §5):** CF-2
authored D9 PM, its **shared-surface authoring D10 AM** (co-located with the PR #34
adjacency check in one 0.50-day slot — 0.10 + 0.25 = 0.35, master plan §6.3), its
integration cycle D10 PM (only after the operator's recorded
continue decision in the D9 AM window — the D8 PM checkpoint is a **recovery boundary
only, never Phase 5 closeout**, master plan §9); CF-2 review D11 AM; build
**D11 PM–D13 AM** (four rebalanced lanes W1–W4, longest lane 1.57 against a **2.0-day
window — 2.0 days, matching the master day table exactly**); **I-5C integration cycle
D13 PM**; security-weighted **R4** wave D14 AM.
**Text-first: no image, video, or audio generation anywhere in
this milestone.**

## 1. Objective

Generate marketing **text** drafts from an approved recommendation brief, under a
content-review gate that runs on the **generated output**, with tenant identity,
full provenance, sanitization, cost ceilings, and quotas — all dark behind
`creative_generation_enabled` (global default `False`, fail-closed,
`workspace_enableable` per resolved O-3). Per resolved operator decision **O-4
(`ALT_expose_all`)**, `creative_generation_enabled` **is added to
`FeatureFlagsOut` at CF-2**, and the exact-equality reflection test
(`apps/api/app/tests/test_api_isolation.py:140`) **must be updated in the same
CF-2 commit** — omitting it would leave O-4 implemented for only two of the
three Phase 5 flags and would break that test. Build and CI
run mock-only; enabling a real provider is an activation-time act (master plan §10)
and is not part of this milestone.

## 2. Frozen contracts (fixed at CF-2)

**New table `creative_drafts`** (immutable-versioned, the brief pattern):
scope FKs (org/workspace/brief/opportunity, nullable location/campaign);
`draft_version` + `parent_draft_id` edit chain; `channel` (closed vocabulary,
text channels only); `body` (bounded text); authorship for the author≠approver
rule — `created_by` (FK, SET NULL) plus non-FK `created_by_snapshot`; full LLM
provenance columns (`prompt_name`, `prompt_version`, `model_identifier`,
`input_hash`, `trace_id`, `is_simulated`, token usage, `estimated_cost_usd`);
unique `(workspace_id, brief_id, draft_version)`; append-only (`created_at`
only, no `updated_at` — the 5A append-only convention; ORM-update-raises test).

**New table `content_reviews`** (one row per draft version): **explicit tenant
scope columns `organization_id` and `workspace_id`** (the same defence-in-depth
convention every sibling Phase 5 table follows — never rely solely on reaching
tenancy by joining through `creative_drafts`), FK `draft_id` → `creative_drafts`
plus `draft_version`, persisted `ClaimSafetyResult` (risk level, findings),
workspace blocked-claims hits, required-disclaimer resolution, `passed` (bool).
**A draft whose review has `is_blocked` true cannot be approved** — enforced in
the 5B approval service when `subject_type == creative_draft`. `content_reviews`
holds claim-safety compliance evidence, so it inherits the O-6 rider: survival
of these records is subject to the future O-5 retention policy and **must not
mean indefinite retention by default**.

**New table `llm_usage_records`** (the cost/quota substrate — currently absent from
the codebase entirely): per-call row with tenant scope, task, provider, model,
token counts, `estimated_cost_usd`, correlation/trace id. Written at the
`LLMService` seam for **every** call (including existing classify/explain tasks).

**Quota/ceiling settings (all in `Settings`, conservative defaults, fail-closed):**
`llm_daily_cost_ceiling_usd_global`, `llm_daily_cost_ceiling_usd_per_workspace`,
`llm_max_calls_per_workspace_per_day`, `llm_max_output_tokens_per_call`. Quota
evaluation reads `llm_usage_records` **under the per-workspace row-lock pattern
established at `capabilities/service.py` (`_workspace_lock_select`)** so
concurrent durable jobs cannot race past a ceiling; on breach the service
refuses with a typed error (deny, never degrade to unmetered). A static
per-model price table pins the cost computation (mock priced at 0; table
versioned in code). NOTE for the CF-2 field-count reconciliation: this block
plus the flag adds **five** `Settings` fields — against the corrected baseline of
88 (not the stale 87 the inventory document still asserts), the count moves
**89→94** at CF-2 (master plan §14, which records the pre-existing drift as a
blocking precondition on CF-1).

**LLM task:** `generate_creative` added to `prompts.py` (versioned) and
`TASK_SCHEMAS` (bounded output schema). The prompt carries the safety instructions
discipline of `explain_opportunity` (no invented claims, no competitor attacks) and
receives only **sanitized** inputs.

**Generation execution:** a durable job (`JobType` member `creative.generate` —
`JobType` in `jobs/status.py` and the handler-registration import in
`jobs/handlers.py` are **freeze-owned**; the integration owner adds both at
CF-2), idempotency-keyed, using the existing lease/retry machinery; the
synchronous route only enqueues and polls.

**Security hardening at the seam (contract level):**

- Input sanitization at the `LLMService.run` boundary and fail-closed
  `_render` **land in 5A** (see the 5A sub-plan — they harden always-live
  paths and must not wait for 5C); 5C consumes them and adds the
  generation-specific battery below.
- Output-side claim review: `check_claim_safety(generated_text, industry,
  blocked_claims=workspace claims library)` — the engine finally consumes
  `ClaimsLibraryEntry`, and offer-calendar `required_disclaimer` entries are
  threaded into the review.
- **The claims library becomes a protected safety control** the moment it is
  load-bearing for blocking: (a) mutations are gated
  `require_exact_roles(OWNER, ADMIN, COMPLIANCE_REVIEWER)` — a marketer whose
  draft is blocked must not be able to remove the blocking claim; (b) entries
  are soft-retired (append/supersede), never hard-deleted; (c) every mutation
  is audited (`AuditAction` members added at CF-2); (d) a negative test proves
  a marketer cannot delete a blocking claim and regenerate. This work edits the
  claims rows of the generic factory in `campaign_context/routes.py` — owned by
  5C-W2 in this window, and disclosed as a sequential (different-window,
  never concurrent) touch of the same file 5D-W1 later rewrites.

**API (behind the flag, fail-closed):** generate
(`POST /workspaces/{ws}/briefs/{id}/drafts`), list
(`GET /workspaces/{ws}/briefs/{id}/drafts`), fetch-one
(`GET /workspaces/{ws}/drafts/{draft_id}` — the canonical draft handle 5E's
export nests under), revise (new version), review results embedded in draft
responses.

## 3. Entry criteria

1. I-5B integrated, R3 clean (approval gate + audit spine live-in-code).
2. CF-2 reviewed; operator decision O-5 status confirmed — RECORDED 2026-09-01
   as `UNDECIDED_RESERVED_TO_OPERATOR_LEGAL` with the rider that the build
   proceeds with the disabled enforcement mechanism and **no 5C or 5E
   activation is permitted until an explicit retention/deletion policy is
   separately adopted**. O-7 likewise remains reserved: mock-first build, no
   real provider, credential, network call, **or provider-specific
   activation**; no 5C activation until O-7 and the **required**
   threat-model gates are separately resolved.
3. The signal-intelligence threat model reopening is scheduled as part of R4's
   review scope (live-generation threats: injection, contamination, cost abuse).

## 4. Exit criteria (all TRUE with artifacts)

1. Migrations round-trip; single head.
2. With the flag off: every 5C endpoint 503s and no **generation** call path is
   reachable. **Honest behavior-change ledger** (the legacy
   `classify_signal`/`explain_opportunity` calls remain live and reachable
   regardless of the flag): 5C deliberately changes two live paths — (a)
   `llm_usage_records` are written for every LLM call including the legacy
   tasks, and (b) the claims-library mutation gate narrows from the
   owner/admin/marketer editor set to
   `require_exact_roles(OWNER, ADMIN, COMPLIANCE_REVIEWER)`, so marketers lose
   claims write access at merge. Both are disclosed, reviewed, test-covered
   behavior changes; everything else is inert with the flag off (dark-state
   tests).
3. Deterministic mock generation end-to-end: brief → draft → content review →
   (5B) approval, byte-stable under a fixed seed; the mock's `_simulate` inputs
   marker key covered for the new task's failure branches; `is_simulated`
   propagates to draft rows.
4. The 5A sanitization-boundary and fail-closed-render guarantees are
   re-asserted for the `generate_creative` task specifically (nested-marker
   test at the provider-request level; mismatch raises).
5. Claims-library protection: exact-set mutation gate, soft-retire, audit rows,
   and the marketer-cannot-unblock negative test all green.
6. Output-side review blocks approval: a draft tripping a blocked claim or
   `is_blocked` cannot be approved (negative test); required disclaimers resolve.
7. Quotas/ceilings enforced: breaching any ceiling refuses further generation with
   the typed error; usage rows written for every call including legacy tasks.
8. Every draft row carries complete provenance including a service-computed
   `input_hash`; `estimated_cost_usd` populated (0 for mock) from the price table.
9. Isolation negatives: cross-workspace draft/review access non-enumerable;
   usage/quota accounting is per-tenant (no cross-tenant bleed test).
   **Includes a `content_reviews`-specific cross-workspace negative that queries
   the table directly, without joining through `creative_drafts`** — the join
   path is exactly what a future compliance dashboard or incident-response query
   might omit, so the table's own scope columns must be shown to hold.
9a. `creative_generation_enabled` is present in `FeatureFlagsOut` and the
   exact-equality reflection test at
   `apps/api/app/tests/test_api_isolation.py:140` passes with it included
   (resolved O-4, `ALT_expose_all`).
10. No real provider is contacted anywhere in tests or CI (asserted via
    `is_simulated` + provider-selection guards).

## 5. Builder lanes and exclusive path ownership

| Lane | Exclusive owned paths (exact) | Delivers | Lane-days |
|---|---|---|---|
| **5C-W1** Primary domain | **Model moved to the freeze payload (master plan §6.2.0):** every ORM model for a table CF-2 creates — with its constraints, defaults, indexes, relationships, its migration and its `apps/api/app/db/models.py` registration line — is authored by the integration owner in the CF-2 atomic payload, not in this window. This lane retains the schemas, service, routes and tests for that directory. `apps/api/app/creative/` (new, whole directory) **plus, assigned at CF-2 under the cross-milestone touch rule: `apps/api/app/approvals/service.py`** (sole change: blocked-review precondition; originates in 5B-W1's earlier window, and is touched again by **5E-W3** for notification emission in **D20 AM–D22 PM** — a three-window sequential chain, never concurrent, disclosed on all three sides). **Forward-touch disclosure:** `creative/service.py` is touched later by 5D-W3 (jurisdiction snapshot, **D17 AM–D18 AM**) and 5E-W3 (notification emission, **D20 AM–D22 PM**). **Cross-authored slice — tests W2's seam code:** `apps/api/app/tests/test_llm_quota_behaviour.py` | drafts, reviews, generation orchestration, approval-gate hook | **1.57** |
| **5C-W2** Seam | `apps/api/app/llm/` (all files — sequential-touch: 5A-W2 owned `service.py`/`base.py`/`providers_real.py` in **D3 PM–D4 PM**), `apps/api/app/claims/engine.py`, **`apps/api/app/jobs/pipeline.py`** (`check_claim_safety` at `:360` — required by this milestone's claims-engine change and named here explicitly; sequential-touch with 5A-W2, which owned it in D3 PM–D4 PM for the tenant-argument change, different windows, never concurrent), `apps/api/app/campaign_context/routes.py` (claims gating/soft-retire/audit only; sequential with 5D-W1's later **D17 AM–D18 AM** window), **`apps/api/app/jobs/creative_handlers.py`** (new, lane-owned — see §2). **Cross-authored slice — tests W1's domain code:** `apps/api/app/tests/test_creative_generation.py` | usage records, quota/ceiling evaluation under row lock, `generate_creative` task, durable-job handler, claims-library protection | **1.44** |
| **5C-W3** Primary UI | `apps/web/src/pages/creative/` (new, whole directory, **excluding** its `__tests__/`) — **cross-window disclosure: 5E-W3 later adds exactly `ExportDialog.tsx` and `ExportHistory.tsx` to this directory in D20 AM–D22 PM. Different windows, never concurrent; those two files are assigned by exact filename at CF-3 and are not 5C-W3's**. Authors **no** tests | draft editor, version history, review-warnings panel, blocked-state UI | **1.41** |
| **5C-W4** Adversarial + UI verification | `apps/api/app/tests/test_creative_versioning.py`, `test_creative_isolation.py`, `test_llm_quota_isolation.py`, `test_content_review.py`, `test_llm_injection.py`; **W3's UI tests:** `apps/web/src/pages/creative/__tests__/` (whole directory as created here — **cross-window disclosure: 5E-W4 later adds exactly `export-dialog.test.tsx` and `export-history.test.tsx` to it in D20 AM–D22 PM, pairing 5E-W3's two new files. Different windows, never concurrent**) | **the whole adversarial battery** (versioning, isolation incl. the `content_reviews` direct-query cross-workspace test, quota isolation, injection/contamination) plus verification of W3's surfaces | **1.50** |

**Longest lane 1.57 against a 2.0-day window (local float +0.43, spendable only inside
5C).** This is the operative statement; the header block restates it as a **derived**
summary and must be propagated in the same edit. An earlier draft carried this paragraph
twice — the two copies disagreed on whether the former test lane "redistributes" or
"splits four ways" — and then claimed it was stated only once. Per-lane figures
indicative within ±0.1. Utilisation against the 2.0-day window is **79/72/71/75%**
(W1 1.57, W2 1.44, W3 1.41, W4 1.50) — an earlier draft quoted 100/92/90/96%, which
reconciled against neither the window nor the longest lane.

**5C needs no backend-lane merge.** W1 and W2 stay separate and the former 2.15-day test
lane **splits four ways**, its released capacity landing on the backend lanes. The naive
"merge the two lightest backend lanes" would have produced 2.36 lane-days here, *worse*
than the 2.15 it replaced.

Freeze-owned files per master plan §6 (CF-2 adds `creative_generation_enabled` +
capability member + migration, **`system/routes.py` (`FeatureFlagsOut`) with the
exact-equality reflection test `apps/api/app/tests/test_api_isolation.py:140`
updated in the same commit per resolved O-4**, five `Settings` fields with the
secret-inventory 89→94 reconciliation (corrected baseline, master plan §14), the capability pin-test updates in the
same commit as the member per master plan §6, the `JobType` member +
`jobs/handlers.py` import, new `AuditAction` members, and the
`approvals/service.py` lane assignment above).

**CF-2's shared frontend contract surface — 0.10 day, authored D10 AM (master plan
§6.3).** 5C-W3 creates the new top-level page directory
`apps/web/src/pages/creative/`, which cannot be reached without freeze-owned entries
that this sub-plan previously named **zero times**: `apps/web/src/api/types.ts`
aliases for the draft, version and content-review response models;
`apps/web/src/api/endpoints.ts` wrappers for this milestone's routes;
`apps/web/src/api/queryKeys.ts` entries for those wrappers; a
**`apps/web/src/components/layout/nav.ts` entry**; and a matching
**`apps/web/src/App.tsx` route**. Master plan §6.2 records that a route without a
`nav.ts` entry is unreachable and its breadcrumb silently degrades — so without these
the draft editor could be authored but never mounted, exactly the defect already cured
for 5A's `OpportunityDetail.tsx`. All are **freeze-owned, authored by the integration
owner at the D10 AM slot**, never by a builder lane, and the 0.10 charge appears in the
master plan §5 charge table rather than being absorbed silently.

## 6. Out of scope

Any real-provider call or credential; image/video/audio; publishing or scheduling;
export (5E); retention enforcement (mechanism designed in 5E, policy per O-5);
per-tenant provider keys (future); embedding/caching layers.

## 7. Rollback

Additive-only; revert PR(s); migrations downgrade; flag never enabled. The two
changes to existing paths — usage-record writes on the legacy classify/explain
tasks and the claims-library mutation gating — are covered by tests and called
out in PR descriptions; reverting restores prior behavior. (The sanitization
boundary and fail-closed `_render` are 5A deliverables — see that sub-plan's
rollback note.)
