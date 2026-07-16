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

## 5. Batch 4A persistence addendum

Batch 4A turns the previously advisory-only annotation into durable, scoped rows
(`signal_intelligence_records`). The threat posture is unchanged; the new surface is
considered against the same boundaries:

- **What is stored:** only the already-sanitized excerpt and the derived
  facts/inference/relevance/score components. No credentials, tokens, raw untrusted
  text beyond the sanitized excerpt, or job payloads are persisted. Payloads are
  additionally length/quantity-bounded (`serialize_candidate`) so a JSON column
  cannot grow without limit (reinforces **T3**).
- **No new egress:** persistence is a local DB write inside the caller-owned
  transaction. No `eval`/`exec`/`subprocess`/network is introduced (**T2**, **T6**
  unchanged).
- **Tenant isolation (T4):** every row carries `organization_id`/`workspace_id`/
  `scout_request_id` and the identity unique constraint is scoped
  `(workspace_id, normalized_signal_id, analysis_version, scoring_version,
  fingerprint)`, so it can never blend rows across workspaces. `attach_opportunity`
  updates only rows already matching the target `workspace_id`.
- **Fact/inference separation (T5):** preserved at rest — `facts`, `inference`,
  `relevance` and `score_components` are stored in distinct columns; nothing
  inferred is recorded as observed fact.
- **Failure containment (T8):** the persist call runs in a SAVEPOINT and is wrapped
  fail-open, so a persistence fault (including a unique-constraint collision from a
  retry/concurrent worker) rolls back only the savepoint and never corrupts
  opportunity creation or ingestion.
- **Not customer-exposed:** no API/OpenAPI/frontend surface reads this table in
  Batch 4A. Rollback is additive-safe (drops only the new table).

_Independent-review status for Batch 4A: NO INDEPENDENT THIRD-PARTY REVIEW COMPLETED._

## 6. Batch 4D read-path security review (API + frontend)

Batch 4B/4C exposed the persisted records read-only, and Batch 4D records the
§17.12 threat-checklist review of that read path. This is the written review the
closeout gate (§17.22.17, acceptance criterion 52) requires. It is a
**verification-and-documentation** review of already-merged code; no production
change is made here.

_Reviewer note: this is an internal engineering review, not an independent
third-party audit. Independent-review status remains: NO INDEPENDENT THIRD-PARTY
REVIEW COMPLETED._

Each checklist item is marked **Resolved** (mitigation exists and is tested),
**N/A** (surface does not exist), or **Observation** (accepted, non-blocking note).
No item is Critical/High-unresolved, so readiness is not blocked.

| # | Threat (§17.12) | Finding | Evidence |
|---|-----------------|---------|----------|
| 1 | Broken object-level authorization | Resolved | Route authorizes the opportunity within the workspace via `get_tenant_context`; non-member → 403, foreign-workspace opportunity → 404. `test_intelligence_api.py::TestRoute/TestIsolation` (`test_non_member_is_forbidden`, `test_foreign_workspace_opportunity_is_404`). |
| 2 | Workspace/tenant leakage | Resolved | Query is workspace-scoped; cross-workspace read denied. `TestIsolation::test_foreign_workspace_opportunity_is_404`; closeout `TestCloseoutIsolation`. |
| 3 | Location/market leakage | Resolved | Four-market isolation asserted at the closeout boundary — each response advertises only its own market; no cross-market bleed across all four. `test_intelligence_closeout.py::TestCloseoutIsolation`; `TestIsolation::test_each_market_returns_its_own_intelligence_only`. |
| 4 | Unsafe opportunity→intelligence linkage | Resolved | `get_latest_for_opportunity` filters by `(workspace_id, opportunity_id)`; every returned record's `opportunity_id` matches the request. `TestCloseoutIsolation`. |
| 5 | ID enumeration | Resolved | Missing/guessed ids are an indistinguishable 404 with no internal detail. `TestRoute::test_missing_opportunity_is_404`; `TestSecurity::test_injection_shaped_ids_are_safe_404`. |
| 6 | Mass assignment | Resolved | Read-only endpoint; mutation verbs → 405. Internal fields (`fingerprint`, `normalized_signal_id`, `organization_id`, `author`, `exclusion_hits`, …) are excluded from the mapped payload. `TestRoute::test_mutation_verbs_not_allowed`, `test_response_has_no_internal_fields`; `TestMapperBounds::test_excluded_fields_absent_from_serialized_payload`. |
| 7 | Stored XSS | Resolved | Evidence/excerpt already sanitized at persistence (`sanitize_text`, T1); the panel renders every excerpt as plain text (`whitespace-pre-wrap`, no `dangerouslySetInnerHTML`). Frontend `intelligence-panel.test.tsx` renders inert text; closeout asserts `evidence[].quote` is a plain string. |
| 8 | Source HTML injection | Resolved | Same as #7 — no HTML surface exists in the mapper or panel. |
| 9 | Unsafe external links | N/A | No URLs are stored in 4A or emitted by the mapper. `TestSecurity::test_no_url_fields_returned`. |
| 10 | Oversized JSON/evidence | Resolved | `_map_record` clamps excerpt (≤2000), evidence count (≤32) and quote length (≤400), hit lists (≤64), score factors (≤32). `TestMapperBounds::test_all_bounds_and_clamps_enforced`. |
| 11 | Unicode/control-character abuse | Resolved | Control chars stripped and markers defanged at persistence (T1/T3); clip/bound applied again in the mapper. Covered by the Batch 3 sanitizer suite + mapper bounds. |
| 12 | Prompt injection | Resolved | Default enricher makes no model call (T1/T2); read path is pure serialization. No new model surface in 4B/4C. |
| 13 | Log injection | Resolved | Read events go through the structured logger + central redactor; only bounded outcome/version fields plus redacted correlation ids are emitted. See Observation O-1 below and ops runbook §9.2. |
| 14 | Score tampering | Resolved | Scores are read-only from immutable persisted rows and clamped to `0..100` on output; the read never writes. `TestMapperBounds` (`score.total` 999→100, `relevance.score` −5→0); closeout asserts bounded ranges. |
| 15 | Evidence tampering | Resolved | Records are immutable/version-aware (Batch 4A, T5); the read maps, never mutates. `TestService::test_no_persistence_mutation_on_read`. |
| 16 | Migration rollback failure | Resolved | Downgrade/re-upgrade tested; single head `0155a5c468e3`; drop is additive-only. `test_signal_intelligence_migration.py`; ops runbook §8. |
| 17 | Duplicate/idempotency race | Resolved | Identity unique constraint + SAVEPOINT insert (Batch 4A). Read path is unaffected (read-only). |
| 18 | Stale or partial data exposure | Resolved | A malformed/partial stored row fails safe to `null` rather than surfacing a partial object or 500. `TestService::test_malformed_record_fails_safe_to_none`; ops runbook §9.1. |
| 19 | Arbitrary raw error exposure | Resolved | Error bodies leak no traceback/SQL/driver/secret text. `TestSecurity::test_error_body_leaks_no_internal_detail`. |

### Observation O-1 — correlation IDs in read-path structured logs (Low, accepted)

The read events (`intelligence_read_absent/malformed/success`) carry `workspace_id`
and `opportunity_id` as structured-log **correlation fields** (same role as
`request_id`/`trace_id`), in addition to the bounded ID-free `outcome`/version
labels. This is consistent with the platform's existing request/trace correlation
and passes through the central redactor. It is an internal operator log, not a
customer-facing surface and not a metric label dimension; no source/evidence text,
raw URLs, arbitrary market names, or exception messages are emitted. **Severity:**
Low/informational — does not block the §17.22.17 closeout gate. **Recommendation
(deferred, no code change in Batch 4D):** if these events are exported to a
metrics/label backend, project away the ID fields there (§17.13 keeps metric labels
ID-free). Recorded here so the claim of §17.13 compliance is accurate rather than
absolute.

**Conclusion:** no unresolved Critical or High finding remains on the intelligence
read path (acceptance criterion 52 met); the one Observation is Low and deferred.

_Independent-review status for Batch 4D: NO INDEPENDENT THIRD-PARTY REVIEW
COMPLETED (internal engineering review only)._

## 7. Out of scope

Live transport hardening (Batch 2), connector legal/ToS review, model-provider
security (deferred), and human-feedback / write actions (Phase 3C). The Batch 4B API
and Batch 4C frontend read surfaces are **in** scope as of the §6 review above.
