# 3C-D — Feedback UI and Rollout Readiness (Closeout Verification)

Verifies the Phase 3C-D deliverable: a feature-gated, role-aware,
append-only **opportunity-feedback UI** integrated over the already-merged
feedback API, with multi-market isolation and controlled-rollout documentation.
The feature ships **dark** and no rollout is executed here.

Branch: `feat/3c-d-feedback-ui-rollout-readiness`. Draft PR only — no reviewer,
no ready-for-review, no merge, no flag enablement.

## Acceptance hardening (supersedes the original transport decision)

A focused acceptance-gap audit found two gaps in the first implementation and both
are now closed. **The sections below marked _superseded_ describe the original
approach and are retained only for history; the hardened behaviour is authoritative.**

- **Gap A — a disabled deployment still issued one feedback request.** The original
  design used the feedback history GET as the capability probe, so while the flag
  was off an editor's panel mounted, fired one GET, received `503`, then hid. The
  requirement is that a disabled feature issues **zero** feedback requests (no GET
  probe, no POST, no background prefetch). **Fix:** a small **additive, read-only**
  capability reflection — `features.opportunity_feedback_enabled` on the existing
  authenticated `GET /system/capabilities` (`RuntimeSummaryOut`), sourced from the
  existing `Settings.opportunity_feedback_enabled`. The UI reads this **before**
  issuing any feedback request; while dark the history query is disabled and no
  feedback GET/POST is ever sent. The backend `503` is retained purely as
  defence-in-depth for a stale client whose cached capability is ahead of a
  mid-session rollback. This is a purely additive contract change (no migration, no
  new endpoint, no new authorization model, no customer-settable toggle) — the
  regenerated `openapi.json` / `schema.d.ts` diff is additive-only.
- **Gap B — stale-context protection was only structurally asserted.** The
  record-scoped query key, scoped mutation invalidation, and
  `key={intelligence_record_id}` remount existed, but the switch/pending scenarios
  were not **directly** exercised. **Fix:** direct tests now cover a record rebind
  while the dialog is open (pending verdict discarded, zero POST), a submission
  pending across a record switch (resolves into only its own scope), a slow history
  response landing after a switch (never appears in the newly-bound view), and an
  unmount while a submission is pending (exactly one POST, no throw).

**New/updated tests (exact files):**

- `apps/web/src/pages/opportunities/__tests__/feedback-panel.test.tsx` — per-role
  zero-request-while-dark tests (owner/admin/marketer/viewer, asserting `GET==0 &&
  POST==0`); a defence-in-depth test (capability enabled but backend GET `503` →
  panel hides, fetched at most once, no retry loop); a double-submit test (submit
  disabled while pending → exactly one POST).
- `apps/web/src/pages/opportunities/__tests__/feedback-panel.isolation.test.tsx` —
  the four direct stale-context tests described above, plus the pre-existing
  four-market isolation and scoped-submission tests (each now enabling the
  capability reflection as a mount precondition).
- `apps/api/app/tests/test_api_isolation.py` — `/system/capabilities` still coarse
  and secret-free with `features == {opportunity_feedback_enabled: False}` by
  default; a new test asserts it reflects `True` when the setting is enabled.

## Scope and non-scope

**In scope (this batch):** typed frontend client integration; feature-gated +
role-aware rendering; append-only immutable history; loading/success/error/retry
UX; record-scoped query keys and scoped mutation invalidation; stale-context
protection; four-market UI isolation tests; rollout runbook + this verification
doc + phase-plan status update.

**Explicitly out of scope (unchanged):** no migration; no new dependency; no CI
change; no feature enablement (all three flags — `opportunity_feedback_enabled`,
`scout_scheduling_enabled`, `connector_rss_enabled` — remain `False`); no
scoring/worker/scheduling/connector coupling; no rollout execution; Phase 4 /
Batch 5 not begun.

**Deliberate additive exception (acceptance hardening):** to close Gap A the
backend `RuntimeSummaryOut` gains a read-only `features.opportunity_feedback_enabled`
boolean and `openapi.json` / `schema.d.ts` are regenerated additively. This is the
only backend / contract change and it adds no endpoint, migration, authorization
model, or customer-settable toggle.

## Feature-gate transport decision (_superseded_ — original server-gated "Option A")

> **Superseded by the acceptance hardening above.** This section records the
> original design, which probed a `503` and is no longer how the UI gates.

The runtime summary originally exposed **no feature-flag surface**, so the UI was
**server-gated**: the feedback history GET doubled as the capability probe; while
the flag was off the API answered `503 capability_unavailable` and the client
treated `503`/`403` as "render nothing." This meant a disabled deployment still
issued **one** feedback GET per editor mount — the acceptance gap now closed by the
additive capability reflection.

## Feature-gate transport decision (authoritative — capability-reflected)

- The UI reads `features.opportunity_feedback_enabled` from the already-fetched,
  cached `GET /system/capabilities` (`useFeedbackCapability`, shared query key with
  Settings — no extra network cost) **before** issuing any feedback request.
- While dark the history query is **disabled** (`enabled: capability.isEnabled`), so
  **zero** feedback GET/POST is ever sent; the panel renders nothing.
- The backend `503 capability_unavailable` on the feedback routes is retained as
  **defence-in-depth** only, for a stale client whose cached capability is ahead of
  a mid-session rollback. The client still treats `503`/`403` as "render nothing".
- Enabling remains a single backend flag flip; the shipped client needs **no
  rebuild** (the capability reflection updates within its 60s staleness window).

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
  existing tests keep the panel hidden; default `/system/capabilities` handler now
  returns `features: { opportunity_feedback_enabled: false }`.

**Acceptance-hardening additions (edited):**

- `apps/api/app/system/routes.py` — additive read-only `FeatureFlagsOut` and
  `RuntimeSummaryOut.features`, sourced from `Settings.opportunity_feedback_enabled`.
- `apps/api/openapi.json`, `apps/web/src/api/schema.d.ts` — regenerated additively.
- `apps/web/src/pages/opportunities/useFeedback.ts` — `useFeedbackCapability`
  (pre-request gate off the shared runtime-summary query); `useFeedbackHistory`
  gains an `enabled` flag so it never fires while dark.
- `apps/web/src/pages/opportunities/FeedbackPanel.tsx` — consults the capability
  gate first and returns `null` while dark before any feedback request.

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
| Feature-gated (dark by default) | Pre-request capability gate `useFeedbackCapability`; while dark the history query is disabled and panel returns `null`. Tests: per-role "issues no feedback request while dark" (`GET==0 && POST==0`). |
| Disabled → zero feedback network activity | `enabled: capability.isEnabled` on the history query; role gate precedes all hooks. Tests: owner/admin/marketer/viewer all assert `GET==0 && POST==0`. |
| Defence-in-depth on stale client | Capability enabled but backend GET `503` → panel hides, fetched at most once, no retry. Test: "surfaces the panel only after the capability reflects enabled (defence-in-depth 503)". |
| Role-aware (editors only; viewer hidden) | `EDITOR_ROLES` gate in `OpportunityFeedbackPanel`. Tests: viewer hidden; editor sees controls. |
| Binary verdict + optional structured reason; no free text | `feedbackReasons.ts` closed vocabulary; dialog has toggle buttons only. Test: "offers only polarity-correct reasons". |
| Polarity-filtered reasons | `reasonsForVerdict(isUseful)`. Test: positive verdict shows positive reasons only, and vice-versa. |
| Append-only immutable events; "Feedback recorded" copy | Toast title "Feedback recorded"; history list has no edit/delete. Test: "append-only history … no edit/delete controls". |
| Loading / success / error / retry UX | Skeleton while loading; toasts on success/error; `ErrorState` retry for non-gate errors. Tests: submit error toast; "Try again" on 429. |
| Record-scoped query key + scoped invalidation | `queryKeys.opportunityFeedback`; mutation invalidates only that key. Test: `feedback-query-key.test.ts`. |
| Stale-context protection (directly tested) | `key={intelligence_record_id}` remount + record-scoped key + mutation scope closure, now exercised directly: record rebind while dialog open (pending verdict discarded, zero POST); submission pending across a switch (own-scope only); slow response after switch (never surfaces in new view); unmount while pending (exactly one POST, no throw). Tests in `feedback-panel.isolation.test.tsx`. |
| Four-market isolation | `feedback-panel.isolation.test.tsx` (Dallas/London/Lagos/Nairobi): each panel shows only its own history; a Dallas submit never touches London. |

## Validation evidence

- **Frontend lint:** `npm run lint` — clean (eslint `--max-warnings 0`).
- **Frontend type-check:** `npm run type-check` — clean (`tsc -b --noEmit`).
- **Frontend tests:** `npm run test` — 15 files, 76 tests, all pass. The default
  `/system/capabilities` handler reports the feature dark, so prior suites keep the
  panel hidden; hardened suites enable the capability reflection explicitly.
- **Backend lint/tests:** `.venv/bin/ruff check app` clean; `.venv/bin/pytest -q`
  644 passed, 8 skipped, including the extended `/system/capabilities` coverage.
- **No migration:** single Alembic head `4945b98229e6` (add_opportunity_feedback)
  unchanged; `.venv/bin/alembic check` reports no new upgrade operations.
- **Additive contract change only:** `scripts/gen-types.sh` regenerates
  `openapi.json` / `schema.d.ts` with an additive-only diff (`FeatureFlagsOut` and
  the `RuntimeSummaryOut.features` field); re-running the script is idempotent (no
  further drift).
- **Backend change is minimal and additive:** limited to
  `apps/api/app/system/routes.py` (the read-only `features` reflection) and its
  tests; no persistence, scoring, worker, scheduling, or connector code touched.
- **Flags remain dark:** `opportunity_feedback_enabled`, `scout_scheduling_enabled`,
  and `connector_rss_enabled` all stay `False`.

## Exact-head CI status

The feedback/capability implementation is validated locally as above, but the
exact-head CI on the draft PR (`#58`) is **currently red — not green**. The single
failing job (Backend quality) is caused by a **pre-existing, unrelated
scheduling-worker wall-clock defect** in files this batch does not touch
(`apps/api/app/jobs/handlers.py`, `apps/api/app/scouting_requests/schedules.py`,
`apps/api/app/tests/test_scout_schedule_worker_integration.py`). It is tracked
separately in issue **#59** and is not a blocker of the feedback work itself. 3C-D
remains **awaiting independent review and merge**; Phase 3C remains **in progress**
until the external scheduling blocker is resolved and exact-head CI can be green.

## Rollout readiness

The feature is implementation-complete and **dark**. Production rollout remains a
separate, explicitly-approved decision (phase-3C plan §15). Enabling is a single
API flag flip (`OPPORTUNITY_FEEDBACK_ENABLED=true`) with an immediate,
data-preserving kill-switch — see
[../operations/opportunity-feedback-rollout.md](../operations/opportunity-feedback-rollout.md).
No rollout is executed by this batch.
