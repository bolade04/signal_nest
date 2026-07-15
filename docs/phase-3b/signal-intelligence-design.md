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
