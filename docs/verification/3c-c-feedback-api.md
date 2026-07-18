# 3C-C — Feature-gated Opportunity Feedback API

**Phase:** 3C, batch 3C-C (feature-gated API).
**Nature:** additive request/response schemas + two feature-gated endpoints + router
registration + backend tests + regenerated contract + docs — **no new migration, no
schema/model change, no UI, no scoring change, no feature enabled.**
**Branch:** `feat/3c-c-feedback-api` (from `main` at
`69d32ec80f7e8c4f1d2623070891917405324289`, i.e. immediately after the 3C-B merge).
**Alembic head:** single head `4945b98229e6` — **unchanged**; 3C-C adds no migration.

Markets exercised: **Dallas (TX, USA), London (UK), Lagos (Nigeria), Nairobi
(Kenya)** — four fully independent tenants (org + workspace + members + opportunity +
immutable intelligence record each) proving cross-tenant feedback isolation at the HTTP
boundary.

## Scope & non-goals

3C-C exposes the customer-facing surface on top of the dark 3C-B persistence: two
feature-gated endpoints, strict request validation, a customer-safe response
projection, editor-only authorization, IDOR-safe scoping, audit integration and a full
HTTP test suite. It deliberately does **not**: add any migration or model column; add
any frontend; enable `opportunity_feedback_enabled` (or any flag); influence scoring,
ranking, rescoring, model training, prompts, or any cross-workspace/cross-market signal;
add workers or scheduled processing.

## Endpoints

Both nested under an opportunity, both editor-gated **and** feature-gated:

```
POST /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/feedback  -> 201
GET  /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/feedback  -> 200
```

- **Feature gate** — while `opportunity_feedback_enabled` is off (the dark default),
  *every* operation, read and write alike, answers `503 capability_unavailable`.
  (Unlike the scheduling reads, feedback history is also gated: nothing about the loop
  is exposed until the feature is deliberately enabled.)
- **Authorization** — both submit and read require an editor role
  (`owner`/`admin`/`marketer`, via `require_role`). Unauthenticated → 401; a
  non-member → 403; a `viewer` member → 403 on both operations.
- **Scope / IDOR** — the opportunity is resolved within the path workspace
  (unknown/cross-workspace → 404, a hidden IDOR). The target intelligence record must
  live in the same workspace (missing *and* cross-workspace both → 404, so record ids
  can't be probed); the 3C-B service then enforces that it belongs to *this*
  opportunity's exact tenant scope, rejecting a sibling-opportunity record as 422.
- **Capture-only** — writes delegate to the append-only `create_feedback` service
  (which copies provenance from the record, polarity-checks the reason, audits and
  logs); reads are a bounded, reverse-chronological projection that mutates nothing.

## Approved product decisions realized

The owner-approved (2026-07-18) decisions are encoded at the boundary:

- **Feedback shape** — required binary `is_useful` plus an *optional* structured
  `reason_code` from the closed `FeedbackReason` vocabulary; **no free text**.
- **Client-supplied fields** — only `intelligence_record_id`, `is_useful` and the
  optional `reason_code`. Tenant scope, actor attribution and version provenance are
  all derived server-side; the request schema is `extra="forbid"`, so any attempt to
  smuggle `organization_id` / `workspace_id` / `opportunity_id` / `fingerprint` /
  `submitted_by_user_id` / version fields is rejected 422.
- **Roles** — `owner`/`admin`/`marketer` for **both** submit and read.
- **Append-only** — every submit inserts a new row; no update/delete/upsert endpoint.
- **Response projection** — exposes the judgement, optional reason, actor, and the
  *public-safe* provenance (`analysis_version`, `scoring_version`) — never the raw
  `fingerprint` or the internal `organization_id`/`workspace_id` scope columns.

## Deliverables

### Schemas
- `apps/api/app/feedback/schemas.py` — `FeedbackCreate` (`extra="forbid"`;
  `intelligence_record_id` + `is_useful` + optional `reason_code` enum), `FeedbackOut`
  (customer-safe projection; `from_attributes`), `FeedbackHistoryOut` (limit/offset
  page envelope).

### Routes
- `apps/api/app/feedback/routes.py` — `submit_opportunity_feedback` (POST, 201) and
  `list_opportunity_feedback` (GET, paginated). `_require_feedback_feature()` → 503;
  `EDITORS = require_role(OWNER, ADMIN, MARKETER)`; `_get_scoped_opportunity` /
  `_get_scoped_record` (404 hidden-IDOR); reverse-chronological history
  (`created_at DESC, id DESC`) with `limit ∈ [1,100]`, `offset ≥ 0`.
- `apps/api/app/api/router.py` — registers `feedback_router` in the aggregate router.

### Contract
- `apps/api/openapi.json` and `apps/web/src/api/schema.d.ts` regenerated via
  `scripts/gen-types.sh` — **purely additive**: one new path and three new schemas
  (`FeedbackCreate`, `FeedbackOut`, `FeedbackHistoryOut`); zero removals, no change to
  any existing path or schema.

### Tests
- `apps/api/app/tests/test_opportunity_feedback_api.py` — **33 tests** over a
  self-contained four-market graph (`get_db` overridden, SQLite FKs enforced):
  feature-gate 503 on both operations, safe-projection contract + forbidden-key
  assertions, provenance-copied-from-record, strict request validation
  (unknown/smuggled field, missing `is_useful`, unknown reason), polarity 422,
  append-only history + pagination (limit clamp 422, empty page), authN/authZ
  (401/403 non-member, 403 viewer, 201/200 marketer + admin), scope/IDOR
  (unknown/cross-workspace opportunity, unknown/cross-workspace record, sibling
  opportunity 422), four-market isolation (each captures its own, history never leaks,
  all cross-market record pairs rejected), audit emission, capture-only regression
  (score untouched, no new intelligence record), non-mutating reads, and route
  registration.
- `apps/api/app/tests/test_opportunity_feedback_concurrency.py` — **dedicated
  feedback concurrency test** (PG-gated): 24 concurrent captures on the *same*
  opportunity + record all persist as distinct rows (append-only, no lost write, no
  spurious conflict). This addresses the 3C-B observation that no dedicated feedback
  concurrency coverage existed.

### Docs
- This verification doc; a minimal status note added to `docs/phase-3c-plan.md`
  (3C-C batch marked delivered as a draft PR, feature still dark).

## Validation evidence

- **Ruff:** clean across `app/`.
- **Backend pytest:** **631 passed, 8 skipped** (PostgreSQL-gated), 0 failed — stable
  across default random ordering and deterministic (`-p no:randomly`) ordering.
- **New suites:** `test_opportunity_feedback_api.py` 33 passed;
  `test_opportunity_feedback_concurrency.py` 1 skipped (PG-gated).
- **Alembic:** single head `4945b98229e6`, **unchanged** — 3C-C adds no migration.
- **Contract:** `scripts/gen-types.sh` regenerated `openapi.json` + `schema.d.ts` with
  only additive feedback changes (one path, three schemas; no removals, no drift in
  existing surface).
- **Frontend:** `eslint` clean; `tsc -b --noEmit` clean; `vitest` 53 passed.

### Test-harness note (rate limiter)

The app mounts a naive in-memory fixed-window rate limiter (240 req / 60 s, keyed by
client host). Because every `TestClient` request shares one host and one in-process
bucket, this module's request volume (notably the cross-market pair matrix) would leave
cumulative residue that 429s later suites. The `test_opportunity_feedback_api.py`
harness therefore clears that shared bucket around each of its tests, leaving no
residue — a test-only hygiene measure that touches no production behavior.

## Governance & safety posture (unchanged)

- `opportunity_feedback_enabled = False` (dark) — every feedback operation answers 503.
- `scout_scheduling_enabled = False`; `connector_rss_enabled = False`.
- No worker, scheduler, or scoring path reads or writes feedback.
- No production rollout; no feature enabled.

## Closeout status

Deliverable is a **DRAFT** 3C-C PR targeting `main`. Do not mark ready, request review,
or merge as part of this batch. Feedback remains dark
(`opportunity_feedback_enabled = False`). 3C-D (feedback UI + closeout) is a separate,
later batch and is not started here.

## Post-merge closeout

The draft above was subsequently reviewed and merged through the protected workflow.
Recorded post-merge evidence:

- **PR #54 final state:** MERGED.
- **Reviewer:** `adesenden` (write access; not the author; independent of the pusher).
- **Reviewed head:** `5fc7ba9feed873c6969e2d3794bf12a4eec51f5c`.
- **Merge timestamp:** `2026-07-18T22:19:45Z`.
- **Squash-merge SHA:** `0544fa1ebfcfde5a4d671e00032a7519f8375f66`.
- **Merge method:** squash. **Admin bypass:** not used (branch protection satisfied
  normally — 1 approval, latest-push approval, resolved threads).
- **Post-merge CI run:** `29663244389` (event `push`, head
  `0544fa1ebfcfde5a4d671e00032a7519f8375f66` — exact merge-SHA match), status
  completed, conclusion success — **all five jobs green** (Frontend quality, Backend
  quality, Migrations and API contract, Container build and security, Integration
  smoke).
- **Backend total:** 639 passed, 0 skipped.
- **Frontend total:** 53 passed.
- **Feedback API tests executed:** yes. **PostgreSQL feedback tests executed:** yes.
  **Dedicated feedback-concurrency test executed:** yes. **Four-market isolation tests
  executed:** yes. No required tests skipped.
- **Alembic head unchanged:** `4945b98229e6`. **Contract drift:** none.
- **Feature flag still false:** `opportunity_feedback_enabled = False`. Scheduling and
  live RSS also still false (`scout_scheduling_enabled = False`,
  `connector_rss_enabled = False`).
- **Branch cleanup:** local and remote `feat/3c-c-feedback-api` branches deleted;
  worktree clean; local and origin `main` synchronized at the merge SHA.
- **3C-D not started.**

**PHASE 3C-C COMPLETE — FEEDBACK API DARK AND VERIFIED**
