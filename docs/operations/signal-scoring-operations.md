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
