# Signal scoring — operations (Phase 3B Batch 3)

Operational reference for the deterministic signal-intelligence core
(`apps/api/app/intelligence/`). It runs in-process inside the existing
`run_scout_request` job; there is no new service, background thread, network
dependency, or migration.

## 1. What it does at runtime

For each ingested signal the pipeline computes an **advisory** intelligence
annotation and stores it at `NormalizedSignal.ingest_metadata["intelligence"]`.
The annotation contains: `facts`, `intelligence` (with evidence spans), `relevance`,
a versioned `score`, `accepted`, `rationale`, `decision` or `rejection`, and
`cluster_key`. It does **not** change the pipeline's own scores or decisions.

## 2. Configuration

- **Enricher:** deterministic by default. There is no configuration required to run.
  A model-backed enricher is **disabled** and out of scope this batch; do not
  enable it without a separate approved change (it would send source text to an
  external model).
- **No feature flag gates the annotation** — it is inert (advisory) and safe on by
  default. To disable it entirely, revert the branch (no data migration needed).

## 3. Scoring version

Every stored score carries `version` (currently `"3b.1"`, `SCORING_VERSION` in
`app/intelligence/scoring.py`). When weights or factor definitions change:

1. Bump `SCORING_VERSION`.
2. Update the weights table in this doc and the design doc.
3. Update/extend the evaluation dataset expectations if outcomes shift.

Because the version travels with each breakdown, historical annotations remain
interpretable against the formula that produced them; do not reinterpret an old
`3b.0` breakdown with new weights.

## 4. Rejection reason codes

`RejectionReason` (`app/core/enums.py`) — a suppressed signal always carries one:

| Reason | Meaning | Typical operator action |
|--------|---------|-------------------------|
| `policy_blocked` | Claim/compliance safety block | Expected; review claim rules if over-blocking |
| `noise` | Spam/unsafe/bot/engagement-bait | Expected; tune `app/scoring/noise.py` patterns |
| `out_of_context` | Below relevance floor or exclusion hit | Check the tenant's keywords/exclusions |
| `out_of_market` | Outside configured scout area | Check the location's coverage rule |
| `duplicate` | Repeat of an in-run signal | Expected |
| `insufficient_evidence` | No evidence spans extracted | Usually very short/empty content |
| `weak_signal` | Composite score < 25 | Expected low-value tail |

A sudden shift in the rejection mix (e.g. everything becoming `out_of_context`)
usually points at a mis-seeded `BusinessContext`, not the core.

## 5. Observability

The core emits **no** metrics and logs only on annotation failure:
`intelligence_annotation_failed` with a coarse error string (no source text, no
identifiers). If you see these, ingestion is unaffected (the annotation degrades to
`{"error": "annotation_unavailable"}`) but the intelligence read is missing for
those signals — investigate the logged error class.

## 6. Determinism / reproducibility

Given the same signal text and `BusinessContext`, the annotation is byte-identical
across runs and hosts. To reproduce an annotation locally:

```python
from app.intelligence.analyze import analyze_signal
from app.intelligence.models import AnalysisInput
from app.scoring.types import BusinessContext

cand = analyze_signal(AnalysisInput(content="…", source_type="rss_news", market="London"),
                      BusinessContext(keywords=[...], ...))
print(cand.as_dict())
```

The labeled dataset in `app/intelligence/evaluation/` pins expected outcomes;
`test_intelligence_evaluation.py` is the regression guard.

## 7. Batch 4A persistence

Batch 4A additionally **persists** the same deterministic candidate as a durable,
scoped, immutable row in `signal_intelligence_records` (one row per scored
normalized signal per `analysis_version`/`scoring_version`/`fingerprint`). The
advisory `ingest_metadata["intelligence"]` annotation is unchanged and stays the
source of the record, so the two never diverge.

- **Migration:** one additive Alembic revision `0155a5c468e3` (down_revision
  `a1b2c3d4e5f6`); single head. Apply with `python -m app.db.migrate upgrade`. The
  app's startup schema guard refuses to boot if the DB is behind this head, so
  migrate before deploying the code.
- **Idempotency:** the insert runs inside a SAVEPOINT and relies on the unique
  identity constraint as the final concurrency guard — retries and concurrent
  workers converge on one row (no app-level check-then-insert).
- **Failure mode:** persistence is fail-open — a persist error is logged
  (`intelligence_record_persist_failed`) and never blocks ingestion or opportunity
  creation. Opportunity linkage failures log `intelligence_record_link_failed`.
- **Not customer-exposed:** no API/OpenAPI/frontend reads this table in Batch 4A.
- **Retention:** records are retained and versioned; there is no cleanup/deletion
  job. Pre-existing signals get no fabricated backfill — absence of a row is valid
  until a signal is (re)scored after the migration.

## 8. Rollback

Additive and surgical. **Code rollback:** revert the branch (or don't merge the
draft PR). **Schema rollback:** `alembic downgrade a1b2c3d4e5f6` drops only the new
`signal_intelligence_records` table and its indexes; all business, signal,
opportunity and durable-job data are preserved and ingestion continues unchanged
(the Batch 3 advisory annotation still rides `ingest_metadata`).

## 9. Read path — API and frontend (Batch 4B / 4C)

Batches 4B and 4C expose the persisted records read-only. There is **no** new
service, worker, migration, or contract change; the read path is a query over the
existing `signal_intelligence_records` table plus one endpoint and one panel.

- **Endpoint:** `GET
  /api/v1/workspaces/{workspace_id}/opportunities/{opportunity_id}/intelligence`
  (`app/intelligence/read_service.py`, router in `app/intelligence/`). It authorizes
  the opportunity within the workspace via the existing `get_tenant_context` path,
  loads the latest eligible record, maps it into a **bounded** public payload, and
  returns `{opportunity_id, intelligence}`.
- **Frontend:** `apps/web/src/pages/opportunities/IntelligencePanel.tsx` renders the
  payload through the `useOpportunityIntelligence` hook. The query key is scoped by
  `(workspace, opportunity)`, so the compact feed issues **no** per-row intelligence
  calls (no N+1).
- **Read-only:** no mutation verb is served (`POST/PUT/PATCH/DELETE` → 405); the read
  never writes and never mutates the record count.

### 9.1 Response signatures operators will see

| Situation | HTTP | Body | Panel state |
|-----------|------|------|-------------|
| Record present | 200 | `intelligence` object | Full panel |
| No record for the opportunity | 200 | `{opportunity_id, intelligence: null}` | Neutral "no analysis yet" — **not** an error |
| Malformed/partial stored row | 200 | `intelligence: null` | Neutral empty (fails safe; never a 500 or partial object) |
| Unauthenticated | 401 | error | Error state with retry |
| Cross-workspace / non-member | 403 | error | Error state with retry |
| Unknown/guessed opportunity id | 404 | error | Error state with retry |

An **absent** record (`null`) is the normal state for opportunities scored before the
migration or never analysed — it is not an incident. A rising 403/404 rate points at
an authorization or client-routing problem, not the read path itself.

### 9.2 Observability of the read path

`read_service` emits one bounded structured event per read:

| Event | `outcome` | Emitted when |
|-------|-----------|--------------|
| `intelligence_read_absent` | `absent` | No eligible record (valid empty result) |
| `intelligence_read_malformed` | `malformed` | A stored row failed mapping and was failed-safe to `null` |
| `intelligence_read_success` | `success` | A record was mapped and returned |

Each event also carries `duration_ms`, `analysis_version`, and `scoring_version`
(bounded, ID-free label values). For request correlation the events **additionally**
carry `workspace_id` and `opportunity_id` as structured-log fields — the same
correlation role as `request_id`/`trace_id`, run through the central redactor
(`app/core/redaction.py`). These are internal operator logs, **not** metric label
dimensions and **not** customer-facing; no source text, evidence text, raw URLs,
arbitrary market names, or exception messages are emitted. If you export these events
into a metrics backend, key metrics on `outcome`/version only — never on the ID
fields — to keep cardinality bounded (see the threat-model observability note).

### 9.3 Read-path rollback layers

The read path is additive over Batch 4A, so it has independent, ordered rollback
layers (verified in ephemeral tests; see `test_intelligence_closeout.py`):

1. **Frontend panel** — remove/hide `IntelligencePanel` from the opportunity detail.
   The API and data are untouched; the rest of the opportunity experience is intact.
2. **API field** — the endpoint can return `intelligence: null` (or be withdrawn)
   while preserving the nullable, backward-compatible contract. Clients already treat
   `null` as the neutral empty state.
3. **Write-path disablement** — stop persisting new records; existing rows remain
   readable, new opportunities simply read `null`.
4. **Schema** — `alembic downgrade a1b2c3d4e5f6` (§8) as the last resort.

Because the read path is purely additive, dropping the records or withdrawing the
field degrades every opportunity to the neutral empty state **without error** and
without touching Phase 2 opportunity title/score/status/decision.

### 9.4 No live-RSS dependency

The read path and all its tests run entirely against the deterministic four-market
demo seed (Dallas, London, Lagos, Nairobi). It performs **no** live RSS/connector
fetch, no external-model call, and no outbound network I/O; normal CI needs no
network access. Live ingestion remains disabled and out of scope for this batch.
