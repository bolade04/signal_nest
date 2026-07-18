# 3C-C.1 — Customer-safe intelligence-record-id contract addendum

**Phase:** 3C, batch 3C-C.1 (contract addendum that unblocks 3C-D).
**Nature:** one additive response-schema field + one read-service mapping line +
regenerated contract + backend tests + docs — **no migration, no model/column change,
no request-schema change, no feedback-API behavior change, no UI, no flag enabled.**
**Branch:** `feat/3c-c-1-intelligence-record-id-contract` (from `main` at
`c3deabf5f66f98eac954f79f1114e213659701f5`, i.e. immediately after the 3C-C closeout
merge).
**Alembic head:** single head `4945b98229e6` — **unchanged**; 3C-C.1 adds no migration.

## Original blocker

The feature-gated feedback POST
(`POST /api/v1/workspaces/{ws}/opportunities/{opp}/feedback`) requires a mandatory
`intelligence_record_id` (`FeedbackCreate`, `extra="forbid"`). The **only**
customer-facing intelligence surface —
`GET /api/v1/workspaces/{ws}/opportunities/{opp}/intelligence` →
`OpportunityIntelligenceResponse` / `IntelligencePayload` — previously exposed **no**
record id (`app/intelligence/read_service.py:_map_record` mapped every public field
except the id, part of the customer-safe projection that also drops `fingerprint` and
scope columns). Consequently a customer/editor UI could not construct the *first*
feedback submission: the required record id was never revealed by any customer
endpoint, and the feedback-history GET only returns ids for feedback that already
exists (circular). This is the exact 3C-D blocker.

## Authorized decision

Owner-approved **option 1** (2026-07-18): expose a customer-safe `intelligence_record_id`
on the **existing** opportunity-intelligence read response — an additive contract
decision, no new route and no broadened authorization. The id is approved as a
**customer-safe opaque identifier**, exposed only within the already-authorized and
scoped intelligence response.

## Exact response location

The field is placed on **`IntelligencePayload`** — the object representing the persisted
intelligence record (the prompt's preferred shape):

```jsonc
{
  "opportunity_id": "…",
  "intelligence": {
    "intelligence_record_id": "…",   // NEW — 3C-C.1
    "…existing fields…": "unchanged"
  }
}
```

When `intelligence` is `null` (no eligible record) there is no id to reference — the
response remains exactly `{ "opportunity_id": "…", "intelligence": null }`.

- **Type:** `string` (required). Repo convention exposes every id as a plain string —
  `opportunity_id: str`, `FeedbackOut.id: str`, and `FeedbackCreate.intelligence_record_id: str`.
  The record PK is a `String(32)` uuid4 hex (not a canonical dashed UUID), so declaring
  `format: uuid` would be inaccurate and would diverge from the `str` that the feedback
  POST already accepts. `string` keeps the GET→POST round-trip type-consistent and
  avoids alias ambiguity. There is exactly one record-id field; no duplicate.

## Scoped mapping (security rationale)

`_map_record` now copies `intelligence_record_id=record.id` from the **exact**
`SignalIntelligenceRecord` the scoped read service already selected
(`get_latest_for_opportunity`, scoped by `workspace_id` **and** `opportunity_id`,
`accepted` only, deterministic `score_total DESC, created_at DESC, id ASC`). There is:

- no second/unscoped lookup;
- no caller-supplied id;
- no inference of the id from the opportunity id;
- no change to record selection, ordering or "current record" semantics.

The exposed id therefore corresponds exactly to the same record whose `analysis_version`,
`scoring_version`, evidence and other mapped fields are returned. Only the primary key
is exposed — **never** the `fingerprint`, `organization_id`, `workspace_id`,
`normalized_signal_id`, `cluster_key`, `exclusion_hits`, or any other internal column.

## Tests

`apps/api/app/tests/test_intelligence_api.py` — new `TestRecordIdContract` class:

- **Schema:** field required (a payload built without it raises `ValidationError`);
  serializes as a non-empty string; existing payload keys all remain present.
- **Mapper:** mapped id equals the selected record id; id and version provenance are
  read from the *same* record; `_map_record` copies a caller-provided ORM object's own
  id verbatim (no extra lookup / no opportunity-id inference).
- **Route:** the real GET exposes the id while the fingerprint, `normalized_signal_id`
  and every forbidden scope key stay absent; a null payload exposes no id; a
  foreign-workspace path stays a 404.
- **Four-market isolation (Dallas / London / Lagos / Nairobi):** every market's response
  returns only its own record id; all four directed cross-market mismatches asserted on
  the **actual** record ids; distinct opportunities never share an exposed id
  (per-opportunity / per-request isolation).

`apps/api/app/tests/test_feedback_record_id_bootstrap.py` — the contract-unblock proof:

- Through the real endpoints: GET intelligence → read `intelligence_record_id` → POST
  feedback with **only** that id + the binary verdict (no caller-supplied provenance,
  scope, actor or version) → 201; the created feedback is bound to the **same** record
  and the server derived the version provenance from that record. Exactly one persisted
  feedback row, pointing at the same record id.
- The read id is exposed even while feedback is dark; the feedback POST still answers
  `503 capability_unavailable` (3C-C.1 does not enable the feature).

## Validation evidence

- **Ruff:** clean across `app/`.
- **Backend pytest:** **643 passed, 8 skipped** (PostgreSQL-gated), 0 failed.
- **Alembic:** single head `4945b98229e6`, **unchanged**; `alembic check` →
  "No new upgrade operations detected."
- **Contract:** `scripts/gen-types.sh` regenerated `openapi.json` + `schema.d.ts` with
  a **purely additive** change — one new required string field on `IntelligencePayload`;
  zero removals; no route change; no feedback request/response change; no unrelated drift.
- **Frontend:** `eslint` clean; `tsc -b --noEmit` clean; `vitest` **53 passed**.

## Feature-dark behavior (unchanged)

- `opportunity_feedback_enabled = False` — the feedback POST/GET still answer 503.
- `scout_scheduling_enabled = False`; `connector_rss_enabled = False`.
- The intelligence GET exposes the record id regardless of the feedback flag because it
  is part of the already-authorized intelligence response. No feedback UI is added or
  enabled.

## Closeout status

Deliverable is a **DRAFT** 3C-C.1 PR targeting `main`. Do not mark ready, request
review, or merge as part of this batch. **3C-D remains not started**; **Phase 3C
remains in progress**. 3C-D must not resume until 3C-C.1 is independently reviewed,
merged, and exact-merge-SHA verified.

**PHASE 3C-C.1 IMPLEMENTED — INTELLIGENCE RECORD ID CONTRACT, DARK AND VERIFIED (AWAITING REVIEW)**
