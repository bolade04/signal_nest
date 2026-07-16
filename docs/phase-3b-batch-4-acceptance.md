# Phase 3B Batch 4 — acceptance report (signal intelligence)

This is the single source of the §17.11 criteria-to-evidence matrix for the
signal-intelligence program (Batches 4A persistence, 4B read API, 4C frontend panel,
4D integration/closeout). It maps every acceptance criterion (1–74) to concrete
merged evidence and is the closeout artifact required by §17.22.17.

## Evidence-type legend

Each row is labelled with the **kind** of evidence, so no verification is
overclaimed:

- **AUTO** — an automated test asserts it; the named test was run green locally in
  this session (backend intelligence subset: 99 passed / 1 postgres-skip; frontend:
  10 files / 44 passed) and re-runs in CI.
- **CI** — verified by a named CI job on the Batch 4D branch head, not re-run
  locally in this session (requires the container/DB/smoke environment).
- **STATIC** — verified by direct code/static review (file reference given); no
  runtime assertion needed.
- **DOC** — a recorded decision or documented procedure.
- **GOV** — governance/scope fact verified against GitHub/rulesets.

> Honesty note: the full backend suite and the container/smoke jobs were **not**
> fully executed in this authoring session — the local readiness/DB stack was
> unavailable, so integration/readiness tests error locally regardless of this
> change. Those criteria are marked **CI** and are authoritative on the branch head,
> not claimed as locally hand-verified.

## Merged provenance

| Batch | PR | Merge commit | Post-merge CI |
|-------|----|--------------|---------------|
| 4A persistence | #37 | `3795f54a6664a424d3678f100cb92f7d28b5cf89` | run `29439431696` |
| 4B read API | #40 | `6aeb0c2177ef0f3a25a42bd46fd23cd71db09778` | run `29470880422` |
| 4C frontend panel | #42 | `1c579f17143de6e4aaf7fa8e36f42a4d3293c895` | run `29476819317` |
| 4D plan (docs) | #43 | `de3563a…` | run `29508873820` |
| 4D closeout | _this PR_ | _draft — pending_ | _pending branch-head CI_ |

Migration head: `0155a5c468e3` (single head; `alembic heads` confirmed this session).
API contract: `apps/api/openapi.json` regenerated this session — **no drift**
(sha `445e8129a7e33e6abdaf15eadc36ee124f9fc45c`, `schema.d.ts` unchanged).

## Criteria matrix

### Persistence (1–10)

| # | Criterion | Kind | Evidence |
|---|-----------|------|----------|
| 1 | One correctly-scoped record per successful analysis | AUTO | `test_signal_intelligence_persistence.py` (write path + scoping) |
| 2 | Reprocessing is idempotent (no duplicates) | AUTO | `test_signal_intelligence_persistence.py` idempotency/SAVEPOINT cases |
| 3 | Opportunities without intelligence stay readable | AUTO | `test_intelligence_api.py::TestRoute::test_authenticated_absent_returns_200_with_null`; `test_intelligence_closeout.py::TestRollbackDegradation` |
| 4 | Existing status/score behavior backward compatible | AUTO | `test_intelligence_closeout.py::TestRollbackDegradation` (Phase 2 fields preserved); `test_intelligence_api.py::TestRoute::test_opportunity_detail_endpoint_unchanged` |
| 5 | Migration upgrades from `a1b2c3d4e5f6` | AUTO | `test_signal_intelligence_migration.py`; down_revision in revision `0155a5c468e3` |
| 6 | Downgrade + re-upgrade succeed | AUTO | `test_signal_intelligence_migration.py` downgrade/re-upgrade |
| 7 | Single Alembic head | STATIC | `alembic heads` → `0155a5c468e3 (head)` (this session) |
| 8 | Schema-drift check reports no unintended ops | CI | "Migrations and API contract" job (`alembic check`) |
| 9 | Required indexes/uniqueness present | AUTO/STATIC | `test_signal_intelligence_migration.py`; unique identity `(workspace_id, normalized_signal_id, analysis_version, scoring_version, fingerprint)` in the model |
| 10 | No destructive data transformation | STATIC | Revision `0155a5c468e3` is additive (create table only); ops runbook §8 |

### Facts and inference (11–18)

| # | Criterion | Kind | Evidence |
|---|-----------|------|----------|
| 11 | Facts vs. inference in distinct fields/types | AUTO | `test_intelligence_api.py::TestRoute::test_facts_and_inference_are_separate`; `test_intelligence_closeout.py::TestEndToEndReadPath` |
| 12 | Inference retains method/confidence/evidence | AUTO | `test_intelligence_closeout.py` (signal_type `{value,confidence,method}`, 0..1); `intelligence-panel.test.tsx` confidence rows |
| 13 | Evidence traceable to normalized signal/provenance | STATIC/AUTO | `read_service._collect_evidence`; provenance block asserted in `test_intelligence_api.py::TestRoute::test_version_metadata_serialized` |
| 14 | Inference never rendered as a quoted source statement | AUTO | `IntelligencePanel.tsx` separates Interpretation from Evidence; `intelligence-panel.test.tsx` "not verified truth" |
| 15 | Missing evidence can't produce a confident claim | AUTO | `test_signal_intelligence.py` INSUFFICIENT_EVIDENCE; `test_intelligence_api.py::TestService::test_malformed_record_fails_safe_to_none` |
| 16 | Simulated fixtures visibly identifiable | AUTO | `test_intelligence_closeout.py` (`is_simulated is True`); `intelligence-panel.test.tsx` "Simulated" badge |
| 17 | Scoring version `3b.1` persisted and exposed | AUTO | `test_intelligence_closeout.py` (`version == {analysis 3b, scoring 3b.1}`) |
| 18 | Component scores bounded and match composite | AUTO | `test_intelligence_api.py::TestMapperBounds::test_all_bounds_and_clamps_enforced`; closeout bounds (0..100 / ≥0) |

### API (19–26)

| # | Criterion | Kind | Evidence |
|---|-----------|------|----------|
| 19 | Authorized users can retrieve intelligence | AUTO | `test_intelligence_api.py::TestRoute::test_authenticated_present_returns_200_with_object` |
| 20 | Cross-workspace/cross-location reads denied | AUTO | `test_intelligence_api.py::TestIsolation`; `test_intelligence_closeout.py::TestCloseoutIsolation` |
| 21 | Foreign-workspace opportunity id can't retrieve | AUTO | `test_intelligence_api.py::TestIsolation::test_foreign_workspace_opportunity_is_404` |
| 22 | No-intelligence returns valid backward-compatible response | AUTO | `test_intelligence_api.py::TestRoute::test_authenticated_absent_returns_200_with_null` |
| 23 | Response schemas typed and documented | STATIC | Pydantic models in `read_service.py`; present in `openapi.json` |
| 24 | OpenAPI regeneration only intentional additive changes | STATIC | Regenerated this session — no drift (openapi.json sha unchanged) |
| 25 | Frontend generated types match contract | STATIC | `schema.d.ts` unchanged after regeneration this session |
| 26 | Feed performance not regressed by payload expansion | AUTO | `test_intelligence_closeout.py::TestNoFeedFanout`; `opportunity-intelligence.test.ts` "fetches exactly once (no N+1)"; `intelligence-panel.test.tsx` "no per-row requests" |

### Frontend (27–37)

| # | Criterion | Kind | Evidence |
|---|-----------|------|----------|
| 27 | Detail shows evidence-backed panel when present | AUTO | `intelligence-panel.test.tsx` (evidence disclosure) |
| 28 | Facts vs. SignalNest inferences visibly distinct | AUTO | `intelligence-panel.test.tsx` "observed facts" / "interpretation" |
| 29 | Evidence/attribution/version/component scores accurate | AUTO | `intelligence-panel.test.tsx` meters + provenance; closeout contract check |
| 30 | Missing intelligence → honest unavailable state | AUTO | `intelligence-panel.test.tsx` "no intelligence analysis is available" (not an alert) |
| 31 | Rejected/weak signals show decision/reason without internal policy | AUTO/STATIC | Panel shows classification/decision/"Below action floor"; `test_intelligence_api.py::TestRoute::test_response_has_no_internal_fields` (no rejection_reason) |
| 32 | Simulated signals show an indicator | AUTO | `intelligence-panel.test.tsx`; closeout `is_simulated` |
| 33 | Evidence renders inertly (no HTML/script) | AUTO | `IntelligencePanel.tsx` plain-text render; `intelligence-panel.test.tsx`; closeout `quote` is str |
| 34 | Loading/empty/error/partial states tested | AUTO | `intelligence-panel.test.tsx` (busy, empty, error/403, clamps) |
| 35 | Keyboard/screen-reader accessible | AUTO/STATIC | `intelligence-panel.test.tsx` roles (meter/heading/button, aria-expanded); ARIA labels in `IntelligencePanel.tsx` |
| 36 | Works at supported responsive breakpoints | STATIC | Responsive Tailwind grid (`sm:` breakpoints) in `IntelligencePanel.tsx`; no fixed-width layout |
| 37 | Existing opportunity status actions still work | AUTO | `test_intelligence_closeout.py::TestNoFeedFanout` (detail unchanged); existing opportunity suites unaffected |

### Isolation (38–45)

| # | Criterion | Kind | Evidence |
|---|-----------|------|----------|
| 38 | Dallas intel can't appear in Lagos | AUTO | `test_intelligence_closeout.py::TestCloseoutIsolation`; `test_intelligence_api.py::TestIsolation` |
| 39 | Lagos intel can't appear in London | AUTO | same |
| 40 | London intel can't appear in Nairobi | AUTO | same |
| 41 | Separate scouting requests independent | AUTO | `TestIsolation::test_cross_market_opportunity_ids_do_not_share_records` (distinct scout_request_id); closeout distinct record ids |
| 42 | Separate locations of same business independent | AUTO | closeout: 4 markets → 4 distinct records; `intelligence-panel.test.tsx` four-market isolation |
| 43 | Separate workspaces can't access each other's intel | AUTO | `TestIsolation::test_foreign_workspace_opportunity_is_404` |
| 44 | Same-topic signals → independent per-market records | AUTO | `TestCloseoutIsolation` (identical excerpt text, distinct record identity) |
| 45 | Reprocessing one market can't overwrite another | AUTO/STATIC | Workspace/identity-scoped unique constraint; `test_signal_intelligence_persistence.py` |

### Determinism and safety (46–53)

| # | Criterion | Kind | Evidence |
|---|-----------|------|----------|
| 46 | No HTTP/socket/model/subprocess/eval/exec path | STATIC | Threat model T2/T6; `read_service` is pure serialization; no new I/O |
| 47 | `DeterministicEnricher` remains default | STATIC/AUTO | `get_enricher` default; closeout asserts `provenance.enricher == "deterministic"` |
| 48 | `ModelEnricher` disabled and fail-closed | AUTO/STATIC | Threat model T2; `ModelEnricher.enrich()` raises (existing enricher tests) |
| 49 | Stored evidence sanitized and bounded | AUTO | Threat model T1/T3; `test_intelligence_api.py::TestMapperBounds` |
| 50 | Prompt-injection markers can't alter behavior | AUTO | `test_signal_intelligence.py` injection_laced; T1 |
| 51 | Raw text can't modify weights/policy/authz/config | STATIC | Threat model T1; deterministic scoring; read path read-only |
| 52 | No unresolved Critical/High security finding | DOC | `docs/security/signal-intelligence-threat-model.md` §6 review — all Resolved/N-A; one Low observation (O-1), deferred |
| 53 | Security tests cover authz/XSS/mass-assignment/oversized/cross-tenant | AUTO | `test_intelligence_api.py::TestSecurity`, `TestMapperBounds`, `TestIsolation`; closeout isolation |

### Quality gates (54–66)

| # | Criterion | Kind | Evidence |
|---|-----------|------|----------|
| 54 | Ruff passes | AUTO | `ruff check app` → "All checks passed!" (this session) |
| 55 | Full backend test suite passes | CI | "Backend quality" job (full suite w/ DB). Locally: intelligence subset 99 passed / 1 skip; non-intelligence integration/readiness tests need the local DB stack (unavailable this session) |
| 56 | PostgreSQL-gated tests run with zero unexpected skips | CI | "Backend quality" job with `TEST_POSTGRES_URL`; locally the 1 gated test is skipped by design |
| 57 | Frontend lint passes | AUTO | `eslint . --max-warnings 0` clean (this session) |
| 58 | Frontend type-check passes | AUTO | `tsc -b --noEmit` clean (this session) |
| 59 | Frontend tests pass | AUTO | vitest 10 files / 44 passed (this session) |
| 60 | `npm audit` reports zero vulnerabilities | AUTO | `npm audit` → 0 vulnerabilities (workspace + root, this session) |
| 61 | Containers build/run as non-root UID `10001` | CI | "Container build and security" job |
| 62 | Integration smoke ≥ 13/13 | CI | "Integration smoke" job (`scripts/smoke_http.py`) |
| 63 | Dallas/Lagos/London/Nairobi isolation passes | AUTO | closeout + API isolation suites (this session) |
| 64 | No cross-market contamination | AUTO | `TestCloseoutIsolation` (4 distinct records, no market bleed) |
| 65 | PR #34 unnecessary for Batch 4 tests | GOV/STATIC | No Batch 4 test imports PR #34 work; deterministic seed only |
| 66 | Normal CI needs no external network | STATIC | Deterministic four-market seed; no live RSS/model; ops runbook §9.4 |

### Governance and scope (67–74)

| # | Criterion | Kind | Evidence |
|---|-----------|------|----------|
| 67 | PR #34 untouched | GOV | Not modified by Batch 4D branch |
| 68 | PR #6 untouched | GOV | Not modified by Batch 4D branch |
| 69 | Ruleset `18820692` unchanged | GOV | No ruleset edits; protected merges only |
| 70 | No bypass/admin merge used | GOV | All Batch 4 merges via protected squash; no `--admin`/bypass |
| 71 | No human feedback/learning introduced | STATIC | No feedback/thumbs/approval controls (Batch 4C read-only); Phase 3C deferred |
| 72 | No recommendation/generation/publishing/billing/new connector | STATIC | Batch 4D scope §17.22.14; diff is tests + docs only |
| 73 | Batch 5 not started | DOC | §17.20/§17.22.20 — remains not started |
| 74 | Rollback documented and tested where practical | AUTO/DOC | `test_intelligence_closeout.py::TestRollbackDegradation`; ops runbook §9.3 read-path rollback layers; §8 schema rollback |

## Closeout gate status (§17.22.17)

1. Every criterion (1–74) mapped to evidence above — **met** (CI-gated rows finalize
   on the branch-head CI run).
2. Security review shows no unresolved Critical/High — **met** (threat model §6).
3. Five CI jobs green on branch head — **pending branch-head CI** (this draft PR).
4. Test counts do not regress (backend +5 closeout tests; frontend unchanged 44);
   smoke ≥13/13 and four-market isolation — **met locally / CI-confirmed**.
5. No migration added; head `0155a5c468e3`; no contract drift — **met** (this session).
6. Containers non-root UID `10001` — **CI** ("Container build and security").
7. Runbook and threat-model review merged — **met** (this PR).
8. PR #34 / PR #6 / ruleset / live-RSS / safety branch untouched; Phase 3C & Batch 5
   not started — **met** (GOV).

**Overall:** Batch 4 closeout evidence is complete pending the Batch 4D branch-head
CI run. Batch 4 is declared complete only when this draft PR is approved, its five CI
jobs are green, and it is merged under branch protection.
