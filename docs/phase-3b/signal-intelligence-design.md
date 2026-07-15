# Signal intelligence and opportunity scoring — design (Phase 3B Batch 3)

**Status: DRAFT.** Additive to the Phase 2 pipeline. No schema change, no API
contract change, no live egress, no dependency on the Batch 2 live-connector
branch (PR #34).

_Independent-review status: NO INDEPENDENT THIRD-PARTY REVIEW COMPLETED._

## 1. Purpose and non-goals

This batch adds a **deterministic, offline signal-intelligence core**
(`apps/api/app/intelligence/`) that turns a normalized, market-scoped signal into
an explainable, evidence-backed `OpportunityCandidate`. It reuses — never replaces
— the existing Phase 2 relevance/validation/decision engines, so the product keeps
a single definition of each rule.

**Non-goals (unchanged from the batch brief):** live RSS transport or any network
egress; additional connectors; generative marketing/ad/media creation; publishing;
customer connector config; billing; performance analytics; trend forecasting;
autonomous external actions; nondeterministic external AI in normal tests; sending
customer/source data to external models; blending tenants/workspaces/locations/
markets; anything in Batch 4.

## 2. Why this is additive (current-state)

The Phase 2 pipeline (`app/jobs/pipeline.py` + `app/scoring/*`) already scores
opportunities. The genuine gaps this batch fills are:

| Concern | Before | This batch |
|---------|--------|------------|
| Signal typing | LLM-only (`classify_signal`) | Deterministic, offline extractor (no model call) |
| Facts vs inference | mixed on `Opportunity` | typed `SignalFacts` (literal) vs `ExtractedIntelligence` (inference + evidence spans + method + confidence) |
| Scoring version | none | `SCORING_VERSION` stamped on every breakdown |
| Rejections | silent `continue` | `RejectionReason` enum + structured rationale |
| Enrichment provider | n/a | provider-neutral seam; deterministic default, model adapter **disabled** |
| Clustering | empty `app/clustering/` | deterministic key-based clustering |
| Evaluation | none | labeled dataset with expected outcomes, asserted in tests |

## 3. Pipeline

```
AnalysisInput
   │  enrich (deterministic; no model, no network)
   ▼
SignalFacts  +  ExtractedIntelligence        ← facts vs inference, evidence spans
   │  assess_relevance (reuses score_relevance, action-floor 40)
   ▼
BusinessRelevance
   │  evaluate_noise (reused)  +  score_candidate (versioned, 8 factors)
   ▼
IntelligenceScore
   │  evaluate_rejection (ordered, short-circuiting reason codes)
   ▼
OpportunityCandidate   → accepted (decide()) | rejected (RejectionReason)
   │  cluster_key (deterministic)
   ▼
cluster_candidates
```

Every stage is a pure function; identical input always yields identical output.

## 4. Domain models (`models.py`)

- **`SignalFacts`** — only literally-observable fields (source_type, market,
  author, language, counts, sanitized excerpt, cross-source counts, engagement).
  No inference.
- **`EvidenceSpan`** — `(start, end, quote, method)` into the *sanitized* excerpt.
  Every inference references ≥1 span.
- **`InferredAttribute`** — `(value, confidence, method, evidence)`.
- **`ExtractedIntelligence`** — inferred signal_type / pain_point_dna / sentiment /
  buying-intent / competitor-dissatisfaction, each evidence-backed. Never conflated
  with facts.
- **`BusinessRelevance`** — score + matched hits + `below_action_floor`.
- **`IntelligenceScore`** — `version`, `total`, `classification`, per-factor
  breakdown.
- **`OpportunityCandidate`** — the output; exactly one of `decision` /
  `rejection`, plus a human `rationale`, `cluster_key`, `is_simulated`.

## 5. Provider-neutral enrichment (`enrichment.py`)

`Enricher` protocol with `DeterministicEnricher` (default; offline) and a disabled
`ModelEnricher` whose `enrich()` **raises**. `get_enricher()` returns the
deterministic one by default; selecting `"model"` yields the disabled stub, so a
misconfiguration fails loudly rather than silently emitting network traffic. No
customer/source text ever reaches an external model in normal operation or CI.

## 6. Deterministic extraction (`extraction.py`)

Lexicon + regex matchers over the **sanitized** excerpt. `sanitize_text()` strips
control characters and defangs prompt-injection markers by quoting them
(`ignore previous instructions` → `[quoted:ignore previous instructions]`). No
`eval`, no network, no model. See the threat model for the full analysis.

## 7. Versioned scoring (`scoring.py`)

`SCORING_VERSION = "3b.1"`. Composite 0..100 from eight clamped factors:

| Factor | Weight | Source |
|--------|-------:|--------|
| source_quality | 15 | `_SOURCE_CREDIBILITY` prior |
| recency | 10 | age decay over 90 days |
| evidence_strength | 20 | cross-source + duplicate counts |
| urgency | 10 | buying intent / complaint signal type |
| business_fit | 20 | `score_relevance` |
| market_fit | 10 | in-scout-area (1/0) |
| commercial_usefulness | 10 | `score_validation` (+ intent bonus) |
| confidence | 5 | evidence quantity + signal clarity |

Weights sum to 100 (asserted at import). Classification reuses
`classify_opportunity`. The breakdown embeds `version` so a stored score is always
interpretable.

## 8. Rejection (`rejection.py`)

Ordered, short-circuiting rules → one `RejectionReason` + rationale:
`POLICY_BLOCKED` → `NOISE` → `OUT_OF_CONTEXT` (exclusion or below floor) →
`OUT_OF_MARKET` → `DUPLICATE` → `INSUFFICIENT_EVIDENCE` → `WEAK_SIGNAL`
(composite < 25). `None` means accepted.

## 9. Clustering (`clustering.py`)

Stable key = pain-point DNA → signal type → `"general"`. `cluster_candidates`
returns keys sorted for deterministic iteration. No embeddings, no randomness.

## 10. Pipeline integration

`_intelligence_annotation()` in `app/jobs/pipeline.py` attaches
`candidate.as_dict()` to `NormalizedSignal.ingest_metadata["intelligence"]`. It is
**advisory only** — it does not feed back into the pipeline's own
scoring/decision path, and any failure is swallowed to `{"error": ...}` so an
annotation can never break ingestion. All existing outputs are byte-identical
(428 backend tests unchanged).

## 11. Deferred decisions

- **Persistence:** a dedicated `signal_intelligence` table (with the versioned
  breakdown and evidence spans as first-class columns) is deferred to avoid schema
  churn this batch. Today the annotation rides existing JSON.
- **API/frontend exposure:** deferred; no contract change this batch.
- **Model-backed enrichment:** deferred and disabled; requires separate approval
  because it would send source text to an external model.

## 12. Testing

- `app/tests/test_signal_intelligence.py` — sanitization/defanging, facts-vs-
  inference, extraction determinism, versioned+bounded scoring, factor math, every
  rejection reason, clustering stability, disabled model enricher.
- `app/tests/test_intelligence_evaluation.py` — asserts the labeled dataset
  reproduces expected accept/reject + reason, and that analysis is deterministic.

## 13. Rollback

`main` @ `fe78b39`. Every commit additive; no migration, no contract change.
Reverting the branch (or not merging the draft PR) fully restores current behavior.

---

# Batch 4A — persistence foundation (architecture decision record)

**Status: BATCH 4A IN DRAFT PR — PENDING REVIEW.** Additive to Batch 3. One
additive migration, no API contract change, no OpenAPI regen, no frontend change,
no live egress. Baseline `main` @ `f022a233edded293ab626c16e45e9b40ce601d02`,
single alembic head `a1b2c3d4e5f6`.

_Independent-review status: NO INDEPENDENT THIRD-PARTY REVIEW COMPLETED._

## 14. Purpose (Batch 4A)

Batch 3 (§11) deferred persistence: the deterministic `OpportunityCandidate`
today rides existing JSON at `NormalizedSignal.ingest_metadata["intelligence"]`,
which is advisory-only, unqueryable, unversioned as a row, and lost to any
consumer that does not read that one blob. Batch 4A makes the Batch 3 intelligence
a **first-class, scoped, immutable, version-aware record**
(`signal_intelligence_records`) associated with its normalized signal and — when
one exists — its opportunity, without changing any Batch 3 scoring, any API
contract, or any frontend behavior. It **stops before** Batch 4B (API exposure).

## 15. What is persisted

One row per `(NormalizedSignal, analysis_version, scoring_version, fingerprint)`.
The row is derived from the **same** `OpportunityCandidate` that produces the
advisory annotation — computed once, so the stored record and the annotation can
never diverge (preserves the Batch 3 fact/inference separation on disk):

- **Scope (typed FK columns, isolation-preserving):** `organization_id`,
  `workspace_id`, `scout_request_id`, `normalized_signal_id`; `opportunity_id`
  (nullable — attached later when the cluster's opportunity exists);
  `location_id` (nullable, `SET NULL`).
- **Identity / versions (typed columns):** `analysis_version`, `scoring_version`
  (`candidate.score.version` = `"3b.1"`, never re-derived), `fingerprint`
  (deterministic content fingerprint of the sanitized excerpt).
- **Outcome (typed columns):** `accepted`, `classification`, `decision`
  (nullable), `rejection_reason` (nullable), `cluster_key`, `is_simulated`,
  `evidence_count`, `score_total`.
- **Payloads (bounded JSON, facts vs inference kept separate):** `facts`,
  `inference`, `relevance`, `score_components`, `rationale`, `provenance`
  (enricher name + versions). Serialization is bounded (capped list lengths and
  string lengths) reusing the already-sanitized Batch 3 excerpt.

## 16. Key architecture decisions

1. **Immutable, version-aware rows.** No destructive overwrite; no mutable global
   `is_current` flag (avoids a read-modify-write race). A re-score under a new
   version writes a *new* row; historical rows stay interpretable against the
   version they carry. Nothing is deleted; no cleanup job.
2. **DB unique constraint is the final concurrency guard.**
   `UniqueConstraint(workspace_id, normalized_signal_id, analysis_version,
   scoring_version, fingerprint)`. Persistence is a concurrency-safe idempotent
   insert (insert inside a SAVEPOINT; on `IntegrityError` roll back the savepoint
   and treat the existing row as authoritative), **not** an app-level
   check-then-insert.
3. **Fail-open on persistence, never corrupt ingestion.** The insert runs in a
   nested transaction (savepoint); a persistence failure rolls back only that
   savepoint and is logged (`intelligence_record_persist_failed`, coarse error
   only), leaving the advisory `ingest_metadata` annotation and the opportunity
   creation path untouched. Persistence is default-on (deterministic fixtures +
   migration present); there is no new feature flag.
4. **Correct pipeline boundary.** Persist per normalized signal *after*
   normalization + analysis + sanitization (`db.flush()` gives `norm.id`); attach
   `opportunity_id` in `_build_opportunities` *after* the opportunity is flushed,
   via a scoped repository update keyed by `(workspace_id, normalized_signal_id)`
   — so an intelligence row is never linked across workspaces.
5. **Rejected candidates are persisted** (with `accepted=False` + reason) but are
   **not** exposed to customers; customer exposure is Batch 4B/4C.
6. **Pure domain stays pure.** The ORM model lives in a new module
   (`app/intelligence/records.py`); `app/intelligence/models.py` remains framework-
   free dataclasses. Serialization lives in `app/intelligence/persistence.py`.

## 17. Migration

One additive migration from `a1b2c3d4e5f6` (single head): `CREATE TABLE
signal_intelligence_records` + its indexes + the unique constraint. No existing
table is altered or dropped; no backfill is fabricated (pre-existing signals
simply have no intelligence row until re-scored). `upgrade` / `downgrade`
(drops only the new table) / re-`upgrade` are exercised by a migration-lifecycle
test against a throwaway SQLite DB via the real Alembic CLI, matching
`test_worker_migration.py`.

## 18. Testing (Batch 4A)

Domain/serialization (bounded payloads, facts/inference kept separate),
repository (idempotent re-insert returns the same row; savepoint isolation on
conflict), pipeline integration (record persisted per signal; `opportunity_id`
attached; advisory annotation still byte-identical; persistence failure does not
break ingestion), four-market isolation (no cross-market/workspace row linkage),
security (no raw untrusted text beyond the sanitized excerpt; no secrets), and
the migration lifecycle. A PostgreSQL-gated test (`TEST_POSTGRES_URL`) asserts the
unique constraint rejects a concurrent duplicate.

## 19. Rollback (Batch 4A)

Additive. Revert the branch (or don't merge the draft PR): `downgrade` drops only
`signal_intelligence_records`; no other schema or data is touched and the Batch 3
advisory annotation continues to work unchanged.
