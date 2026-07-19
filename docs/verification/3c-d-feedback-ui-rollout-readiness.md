# 3C-D — Feedback UI and Rollout Readiness (Closeout Verification)

Verifies the Phase 3C-D deliverable: a feature-gated, role-aware,
append-only **opportunity-feedback UI** integrated over the already-merged
feedback API, with multi-market isolation and controlled-rollout documentation.
The feature ships **dark** and no rollout is executed here.

Branch: `feat/3c-d-feedback-ui-rollout-readiness`. Draft PR only — no reviewer,
no ready-for-review, no merge, no flag enablement.

## Scope and non-scope

**In scope (this batch):** typed frontend client integration; feature-gated +
role-aware rendering; append-only immutable history; loading/success/error/retry
UX; record-scoped query keys and scoped mutation invalidation; stale-context
protection; four-market UI isolation tests; rollout runbook + this verification
doc + phase-plan status update.

**Explicitly out of scope (unchanged):** no migration; no backend API change; no
OpenAPI / generated-TypeScript change; no new dependency; no CI change; no feature
enablement (all three flags — `opportunity_feedback_enabled`,
`scout_scheduling_enabled`, `connector_rss_enabled` — remain `False`); no
scoring/worker/scheduling/connector coupling; no rollout execution; Phase 4 /
Batch 5 not begun.

## Feature-gate transport decision (server-gated, "Option A")

The runtime summary (`apps/api/app/system/routes.py`, `RuntimeSummaryOut`) exposes
**no feature-flag surface**, and adding one would violate the no-backend-change /
no-OpenAPI-change constraints. Therefore the UI is **server-gated**:

- The feedback **history GET doubles as the capability probe.** While the flag is
  off the API answers `503 capability_unavailable`
  (`{ error: { code: 'capability_unavailable', message } }`), surfaced through
  `ApiError.status`.
- The client treats `503` (feature dark) and `403` (unauthorized) as **"render
  nothing."** No partial UI is shown for a capability the user cannot use.
- Enabling is a single backend flag flip; the shipped client needs **no rebuild**.

This was evaluated as the only no-backend-change path — **not a blocker**, so
implementation proceeded rather than stopping to report.

## Files changed

**Frontend — client integration (edited):**

- `apps/web/src/api/types.ts` — re-export `FeedbackCreate`, `FeedbackOut`,
  `FeedbackHistoryOut`, `FeedbackReason` from the existing generated schema (no
  schema regeneration).
- `apps/web/src/api/endpoints.ts` — `listOpportunityFeedback` (GET, `limit`/
  `offset`) and `submitOpportunityFeedback` (POST) over the existing route.
- `apps/web/src/api/queryKeys.ts` — `opportunityFeedback(ws, opp, recordId)`
  key, nested under the shared opportunity-detail prefix and scoped by record id.
- `apps/web/src/pages/opportunities/IntelligencePanel.tsx` — mounts the panel with
  `key={payload.intelligence_record_id}` after the provenance details.
- `apps/web/src/test/handlers.ts` — `intelligence_record_id` added to the mock
  intelligence payload; default **dark** (503) feedback GET/POST handlers so
  existing tests keep the panel hidden.

**Frontend — new modules (created):**

- `apps/web/src/pages/opportunities/feedbackReasons.ts` — closed vocabulary:
  positive (`useful_insight`, `strong_evidence`, `commercially_relevant`,
  `correct_market`) and negative (`irrelevant`, `wrong_market`, `weak_evidence`,
  `duplicate`, `outdated`, `not_commercially_useful`, `other`) with polarity
  filtering (`reasonsForVerdict`) and labels (`reasonLabel`). No free-text.
- `apps/web/src/pages/opportunities/useFeedback.ts` — `isFeatureDark`,
  `useFeedbackHistory` (record-scoped query key; 4xx/503 never retried),
  `useSubmitFeedback` (mutation invalidates only the scoped key; "Feedback
  recorded" / "Could not record feedback" toasts).
- `apps/web/src/pages/opportunities/FeedbackPanel.tsx` — role gate
  (`owner`/`admin`/`marketer`); dark/unauthorized → renders nothing; Useful /
  Not useful controls; polarity-filtered reason dialog; append-only history with
  no edit/delete affordance.

**Docs (created/edited):**

- `docs/operations/opportunity-feedback-rollout.md` (created) — rollout runbook.
- `docs/verification/3c-d-feedback-ui-rollout-readiness.md` (created) — this doc.
- `docs/phase-3c-plan.md` (edited) — 3C-D status → implemented/awaiting review;
  overall Phase 3C remains **in progress**.

## Requirements traceability

| Requirement | Evidence |
| --- | --- |
| Feature-gated (dark by default) | `useFeedbackHistory` 503 → `isFeatureDark`; panel returns `null`. Test: "renders nothing while the feature is dark". |
| Role-aware (editors only; viewer hidden) | `EDITOR_ROLES` gate in `OpportunityFeedbackPanel`. Tests: viewer hidden; editor sees controls. |
| Binary verdict + optional structured reason; no free text | `feedbackReasons.ts` closed vocabulary; dialog has toggle buttons only. Test: "offers only polarity-correct reasons". |
| Polarity-filtered reasons | `reasonsForVerdict(isUseful)`. Test: positive verdict shows positive reasons only, and vice-versa. |
| Append-only immutable events; "Feedback recorded" copy | Toast title "Feedback recorded"; history list has no edit/delete. Test: "append-only history … no edit/delete controls". |
| Loading / success / error / retry UX | Skeleton while loading; toasts on success/error; `ErrorState` retry for non-gate errors. Tests: submit error toast; "Try again" on 429. |
| Record-scoped query key + scoped invalidation | `queryKeys.opportunityFeedback`; mutation invalidates only that key. Test: `feedback-query-key.test.ts`. |
| Stale-context protection | `key={intelligence_record_id}` remount + record-scoped key + mutation scope closure. |
| Four-market isolation | `feedback-panel.isolation.test.tsx` (Dallas/London/Lagos/Nairobi): each panel shows only its own history; a Dallas submit never touches London. |

## Validation evidence

- **Frontend lint:** `npm run lint` — clean (eslint `--max-warnings 0`).
- **Frontend type-check:** `npm run type-check` — clean (`tsc -b --noEmit`).
- **Frontend tests:** `npm run test` — all pass (baseline + 15 new: 10 panel, 2
  isolation, 3 query-key). Existing suites unaffected: the default dark 503 handler
  keeps the panel hidden in prior `OpportunityDetail` coverage.
- **No migration:** single Alembic head `4945b98229e6` (add_opportunity_feedback)
  unchanged; no new revision added by this batch.
- **No contract drift:** no `schema.d.ts` / OpenAPI regeneration; the new types are
  re-exports of already-generated symbols. `scripts/gen-types.sh` expected to show
  zero diff.
- **No backend change:** `git status` shows only `apps/web/**` and `docs/**` — no
  `apps/api/**` modification.
- **Flags remain dark:** `opportunity_feedback_enabled`, `scout_scheduling_enabled`,
  and `connector_rss_enabled` all stay `False`.

## Rollout readiness

The feature is implementation-complete and **dark**. Production rollout remains a
separate, explicitly-approved decision (phase-3C plan §15). Enabling is a single
API flag flip (`OPPORTUNITY_FEEDBACK_ENABLED=true`) with an immediate,
data-preserving kill-switch — see
[../operations/opportunity-feedback-rollout.md](../operations/opportunity-feedback-rollout.md).
No rollout is executed by this batch.
