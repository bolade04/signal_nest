# Phase 3C-D — Issue #59 Closeout Status

**Classification:**
`IMPLEMENTATION MERGED — TECHNICAL FIX COMPLETE — POST-MERGE CI EVIDENCE MISSING — ISSUE #59 REMAINS OPEN`

| Field | Value |
| --- | --- |
| Issue | #59 |
| PR | #60 |
| Merge SHA | `4cf5bc3fafc24d1712c43124bc0c16ae7a750c9e` |
| Original feature head | `d5990062b1ba09472aeea49f69b877fddd96253f` |
| Pre-merge CI run | `29696153000` |

> This is a **closeout-status record**, not a claim that Phase 3C-D or issue #59
> is fully closed. Issue #59 is deliberately left **OPEN** pending the remaining
> gate documented in section 8.

## 1. Problem statement

Issue #59 concerned **scheduling clock propagation** in the scout schedule
fan-out path.

**Root cause (precise):** `execute_schedule_tick` did not propagate the injected
clock into `run_schedule_tick`. With no `now` threaded through, the fan-out fell
back to the real wall clock (`utcnow()`).

**Effect (technical, scoped):** the scheduled run fanned out by a tick — and its
self-chained successor tick — stamped their timestamps (e.g. the run's
`available_at`) from real UTC rather than the worker's injected clock. Under a
pinned-clock worker integration test, once real UTC advanced past the test's
claim boundary the fanned-out run was never considered due and stayed pending,
so the scout schedule worker suite failed deterministically. The defect was
observed in the injected-clock (test) path; no separate claim is made here about
production-runtime impact beyond the clock-fallback behavior described.

## 2. Implemented correction (PR #60)

PR #60 corrected the clock propagation and kept the change scoped to the defect.
Per the merge commit `4cf5bc3fafc24d1712c43124bc0c16ae7a750c9e` the change is
**four files, +56 / −0**:

| File | Change |
| --- | --- |
| `apps/api/app/jobs/handlers.py` | +1 |
| `apps/api/app/jobs/registry.py` | +7 |
| `apps/api/app/jobs/worker.py` | +6 |
| `apps/api/app/tests/test_scout_schedule_worker_integration.py` | +42 |

- **Injected clock propagation corrected:** one execution-boundary timestamp is
  captured from the worker clock on `HandlerContext` and passed as `now` into
  `run_schedule_tick`, so every fan-out timestamp derives from a single injected
  clock.
- **Production semantics unchanged:** callers that supply no clock keep the
  `utcnow()` default, so scheduling behavior for production callers is unchanged.
- **Schedule worker integration coverage updated:** a focused regression asserts
  that the fan-out's `available_at` and the schedule's `last_tick_at` derive from
  a far-future pinned clock and that the run is immediately claimable at that same
  clock.
- **Scope discipline:** the diff touches only the three jobs modules and the one
  worker integration test. **No feature flag was enabled. No rollout occurred. No
  migration, model, or contract change.** (There are no additions beyond the 56
  inserted lines above; no changes are claimed that are not present in the merge
  commit.)

## 3. Pre-merge verification

- **Exact PR head:** `d5990062b1ba09472aeea49f69b877fddd96253f`.
- **CI run:** `29696153000` — completed **successfully**.
- **All five required jobs passed:**
  1. Frontend quality
  2. Backend quality
  3. Migrations and API contract
  4. Container build and security
  5. Integration smoke
- **Approval:** provided by `adesenden`, matching the exact PR head
  `d5990062b1ba09472aeea49f69b877fddd96253f`.
- **Unresolved review threads:** zero.
- **Protected merge requirements:** satisfied (see section 4).

_(Test totals / individual test names are intentionally omitted here as they are
not re-confirmed from the run logs in this documentation task.)_

## 4. Protected merge outcome

- **Merge method:** squash merge through the protected workflow.
- **Merge time:** `2026-07-19T18:31:26Z`.
- **Merge SHA:** `4cf5bc3fafc24d1712c43124bc0c16ae7a750c9e`.
- **Base branch:** `main`.
- **No admin bypass** was used.
- **No auto-merge** was used.
- **Remote feature branch deleted** after merge.
- **Local `main`** was later fast-forwarded to the merge SHA.
- **Local merged feature branch deleted** after squash-merge verification.

## 5. Post-merge CI anomaly

- The push workflow is **expected to react to pushes to `main`**.
- **No Actions run** was created for the exact merge SHA
  `4cf5bc3fafc24d1712c43124bc0c16ae7a750c9e`.
- **No check-runs** were created for the merge SHA.
- **No check-suites** were created for the merge SHA.
- The **combined status remained `pending` only because zero statuses existed** —
  not because a run was in progress.
- An **additional ~one-hour wait and recheck produced the same result**: Actions
  runs `0`, check-runs `0`, check-suites `0`, combined status `pending` with zero
  statuses.
- **No manual dispatch, rerun, synthetic commit, force push, or other workaround**
  was performed.

**This is not a CI failure.** No post-merge run existed; therefore there was no
post-merge result to evaluate.

## 6. Governance decision

Issue #59 remains **OPEN** as a deliberate governance-preserving decision:

- The implementation and protected merge are complete.
- Pre-merge exact-head CI (run `29696153000`) is green across all five jobs.
- The protocol requires **post-merge exact-SHA CI evidence** before issue closure.
- That evidence **does not exist** for `4cf5bc3fafc24d1712c43124bc0c16ae7a750c9e`.
- Issue closure was therefore **deliberately withheld**.
- **No closeout comment was posted** to issue #59.

This is governance preservation — the protocol gate is intact — **not** an
implementation failure.

## 7. Final repository state

- **Current branch:** `main`.
- **Local `main`:** `4cf5bc3fafc24d1712c43124bc0c16ae7a750c9e`.
- **`origin/main`:** `4cf5bc3fafc24d1712c43124bc0c16ae7a750c9e`.
- **Ahead/behind:** `0 0`.
- **Worktree:** clean before documentation drafting.
- **Merged feature branch:** absent locally and on remote.
- **Safety branch:** `backup/signalnest-phase-1-2-pre-history-stitch` — remains
  **local only**.
- **Live-RSS branch:** `feat/phase-3b-live-rss-controlled-egress` — remains
  **local and remote**.
- **Protected PRs unchanged:** #58, #34, #6.
- **Ruleset `18820692`:** active with **zero bypass actors** (1 required approval,
  stale-approval dismissal enabled, approval required after latest push,
  review-thread resolution required, current user cannot bypass).
- **Feature flags (all `False`):** `opportunity_feedback_enabled=False`,
  `scout_scheduling_enabled=False`, `connector_rss_enabled=False`.
- **Alembic:** single head `4945b98229e6`.

## 8. Remaining closeout gate

Issue #59 may be closed **only after one** of the following is formally accepted by
the project owner:

1. A valid **automatic exact-merge-SHA post-merge CI run** appears for
   `4cf5bc3fafc24d1712c43124bc0c16ae7a750c9e` and all required jobs pass; **or**
2. A **separately approved governance exception** defines acceptable substitute
   evidence.

This document neither invents nor approves such an exception.

## 9. Recommended next action

Open a **separate, controlled investigation** into why the push workflow did not
create a run for the squash merge commit. Areas to inspect (without changing them
in this task):

- `on.push.branches` in the workflow definition;
- path filters (`paths` / `paths-ignore`);
- job-level and workflow-level `if:` conditions;
- the workflow file state **as of the merge SHA**;
- GitHub Actions enablement / repository settings;
- whether the merge commit changed only paths excluded by the workflow;
- GitHub Actions event-delivery evidence.

This investigation must be **separate** from issue #59 implementation closeout.

## 10. Closeout checklist

**Completed:**

- [x] Root cause identified.
- [x] Fix implemented.
- [x] Tests added or updated.
- [x] Exact-head pre-merge CI passed.
- [x] Required approval obtained.
- [x] Protected squash merge completed.
- [x] Local repository synchronized.
- [x] Merged feature branches cleaned up.
- [x] Governance preserved.

**Pending:**

- [ ] Exact-merge-SHA post-merge CI evidence.
- [ ] Issue #59 closeout comment.
- [ ] Issue #59 closure.

## 11. Evidence table

| Evidence | Value |
| --- | --- |
| Issue | #59 |
| PR | #60 |
| Feature head | `d5990062b1ba09472aeea49f69b877fddd96253f` |
| Pre-merge CI | `29696153000` |
| Merge SHA | `4cf5bc3fafc24d1712c43124bc0c16ae7a750c9e` |
| Merge time | `2026-07-19T18:31:26Z` |
| Post-merge Actions runs | 0 |
| Post-merge check-runs | 0 |
| Post-merge check-suites | 0 |
| Issue state | OPEN |
| Ruleset | `18820692` |
| Alembic head | `4945b98229e6` |

---

**Classification:**
`IMPLEMENTATION MERGED — TECHNICAL FIX COMPLETE — POST-MERGE CI EVIDENCE MISSING — ISSUE #59 REMAINS OPEN`
