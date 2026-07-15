# Signal-intelligence threat model (Phase 3B Batch 3)

**Scope:** the deterministic, offline intelligence core
(`apps/api/app/intelligence/`) and its advisory pipeline annotation. No live
egress, no model call, no schema/contract change.

_Independent-review status: NO INDEPENDENT THIRD-PARTY REVIEW COMPLETED._

## 1. Trust boundaries

| Zone | Trust | Notes |
|------|-------|-------|
| Signal `content` (from connectors) | **Untrusted** | Adversarial text; may contain injection markers, control chars, HTML, huge input |
| `BusinessContext` (tenant profiles) | Trusted (tenant-scoped) | Never blended across tenants; passed in by the caller |
| Intelligence core | Trusted code | Pure functions; no I/O, no `eval`, no network, no model |
| `ingest_metadata["intelligence"]` | Trusted-at-rest | Advisory only; not fed back into scoring/decisions |

The core treats **all signal text as hostile** and everything it emits as
"derived from hostile input" until sanitized.

## 2. Assets

- Integrity of opportunity scoring/decisions (must not be steerable by source text).
- Tenant isolation (no cross-tenant/market blending).
- Determinism (identical input → identical output; the explainability contract).
- Absence of data exfiltration (no source/customer text leaving the process).

## 3. Threats and mitigations

### T1 — Prompt injection via source text
An attacker embeds `ignore previous instructions / system prompt / you are now …`
hoping a downstream model or tool obeys it.
**Mitigation:** `sanitize_text()` **defangs** every marker by quoting it
(`[quoted:…]`) before any span is recorded or any text is stored/displayed. The
default enricher makes **no** model call, so there is no model to steer. The
`injection_laced` evaluation case asserts the genuine pain point still scores while
the marker is neutralized. **Residual risk:** low.

### T2 — Exfiltration of source/customer text to an external model
**Mitigation:** `ModelEnricher.enrich()` raises; it is never selected by default or
in tests. `get_enricher("model")` returns the disabled stub. Enabling a real model
provider is a separate, explicitly-approved change. **Residual risk:** low
(fails closed).

### T3 — Denial of service via oversized/pathological input
**Mitigation:** the excerpt is capped at `MAX_EXCERPT_CHARS` (400) before matching;
all matchers are linear lexicon/regex scans with no catastrophic backtracking
(fixed alternations, no nested quantifiers). Control characters are stripped.
**Residual risk:** low.

### T4 — Cross-tenant / cross-market blending
**Mitigation:** the core is stateless and operates on a single `AnalysisInput` +
one tenant's `BusinessContext`; it holds no cross-request state except an
optional, caller-owned per-run dedupe set. `market_fit` uses the caller-provided
`inside_scout_area`; out-of-market signals are rejected `OUT_OF_MARKET`. The
pipeline's existing tenant/workspace/location guards are unchanged. **Residual
risk:** low.

### T5 — Fabricated commercial signal / inference presented as fact
**Mitigation:** hard type-level separation — `SignalFacts` carries only observed
fields; all inference lives in `ExtractedIntelligence` with evidence spans, a named
method and a confidence. `INSUFFICIENT_EVIDENCE` rejects candidates with no
supporting spans. `is_simulated` is propagated. **Residual risk:** low.

### T6 — Code execution / SSRF via source content
**Mitigation:** no `eval`/`exec`, no `subprocess`, no URL fetching, no
deserialization of source content. Extraction is pure string matching.
**Residual risk:** negligible.

### T7 — Non-determinism corrupting the explainability contract
**Mitigation:** no randomness, no clocks, no dict-ordering dependence (cluster keys
sorted; lexicon ties broken by weight then content hash). A determinism test
re-runs every evaluation case and asserts identical `as_dict()`. **Residual risk:**
low.

### T8 — Annotation failure breaking ingestion
**Mitigation:** `_intelligence_annotation()` wraps the whole analysis in a
try/except that logs and returns `{"error": "annotation_unavailable"}`; the
annotation is advisory and never gates persistence. **Residual risk:** low.

### T9 — Sensitive data in logs/metrics
**Mitigation:** the core emits no metrics and logs only a coarse error string on
annotation failure (no source text, no identifiers). The bounded-cardinality
metrics policy is unchanged. **Residual risk:** low.

## 4. Assumptions

- Callers pass correctly tenant-scoped `BusinessContext` and `inside_scout_area`
  (enforced upstream by the pipeline's isolation guards).
- The deterministic enricher is the only one enabled until a model provider is
  separately approved.

## 5. Out of scope

Live transport hardening (Batch 2), connector legal/ToS review, model-provider
security (deferred), and the frontend surface.
