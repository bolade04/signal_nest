# Phase 5A — Recommendation Foundation

**Parent:** `docs/project-phase-5-plan.md` (binding for naming, exclusions, conduct,
schedule, and decisions). **Status:** PLANNED / DOCS-ONLY — authorizes nothing.
**Schedule slot (continuous round, master plan §5):** CF-1 authored D1, its shared-surface
authoring D2 AM and integration cycle D2 PM; **R1** wave D3 AM; build **D3 PM–D4 PM**
(four rebalanced lanes W1–W4, longest lane 1.15 against a 1.5-day window); **I-5A
integration cycle D5 AM**; **R2** wave D5 PM.

## 1. Objective

Introduce a first-class, **immutable, versioned recommendation-brief entity** bound
to the exact intelligence evidence it was derived from, persist the LLM provenance
that is currently computed and discarded, and land the LLM-service corrections that
every later milestone depends on — all dark behind `recommendation_briefs_enabled`
(global default `False`, fail-closed).

## 2. Frozen contracts (fixed at CF-1)

**New table `recommendation_briefs`** — modeled on `SignalIntelligenceRecord`'s
immutability discipline (`apps/api/app/intelligence/records.py`):

- Identity/scope: `id` (String(32) UUID), `organization_id`, `workspace_id`
  (String(32), FK, CASCADE), `opportunity_id` (FK), `intelligence_record_id` (FK —
  binds the brief to the exact immutable evidence record), `location_id` (nullable
  FK), `campaign_id` (nullable FK, workspace-validated at write time).
- Versioning/provenance: `brief_version` (int, monotonic per opportunity),
  `parent_brief_id` (nullable self-FK), `analysis_version`, `scoring_version`
  (copied from the intelligence record at capture — never caller-supplied),
  `prompt_name`, `prompt_version`, `model_identifier`, `input_hash`, `trace_id`,
  `is_simulated` (bool).
- Authorship (required for 5B's author≠approver rule): `created_by` (FK to
  users, `SET NULL`) **plus** a non-FK `created_by_snapshot` (immutable string:
  user id + display identity at capture time), so authorship on this
  compliance-relevant record survives user deletion.
- Content (bounded JSON, facts/inference separation preserved): `summary`,
  `rationale`, `recommended_actions`, `suggested_angles`, `risk_notes`,
  `evidence_refs`.
- `UniqueConstraint(workspace_id, opportunity_id, brief_version)`. Rows are
  **append-only**: no update or delete path is written; a revised brief is a new
  row with `parent_brief_id` set. Append-only tables in Project Phase 5 carry
  `created_at` **only** — no `updated_at`/`onupdate` column (deliberate
  departure from `TimestampMixin`, whose `onupdate` contradicts append-only) —
  and a model-level test asserts that an ORM update raises.
- Conventions otherwise: portable types only (String/JSON/Text; no JSONB, no
  native enums), `UUIDPrimaryKeyMixin`, portable `CheckConstraint`s rendered
  deterministically from Python sources of truth.

**No `BriefStatus` enum.** A brief's display status (draft / superseded /
approved / rejected) is **derived** from the approval spine (5B) and the
`parent_brief_id` chain at read time; no status enum or column is frozen at
CF-1, so there is no contract without a consumer.

**Flag/capability:** `recommendation_briefs_enabled: bool = False` in `Settings`;
`Capability.RECOMMENDATION_BRIEFS` + `CapabilityPolicy` entry; new Alembic
migration widening the workspace-override check constraint (SQLite
`batch_alter_table` rebuild — the established pattern). Enableability and public
reflection are RESOLVED (2026-09-01, `DECISIONS.PP5-O1-O11.md`): per O-3,
`workspace_enableable=True` with global default `False`; per O-4
(`ALT_expose_all`), the flag is added to `FeatureFlagsOut` at CF-1 with the
exact-equality reflection test updated in the same commit.

**API (all behind the flag, fail-closed via the `_require_feedback_feature`
pattern):**

- `GET  /workspaces/{ws}/opportunities/{id}/briefs` — list (workspace-scoped).
- `POST /workspaces/{ws}/opportunities/{id}/briefs` — generate/capture a brief.
- `GET  /workspaces/{ws}/opportunities/{id}/briefs/{brief_id}` — fetch one.
- Hidden-IDOR 404 discipline and non-enumeration identical to the feedback router.

**LLM service corrections (contract level).** These land in 5A — not 5C —
because the two existing LLM call sites sit on always-live pipeline paths behind
no Phase 5 flag, and the sanctioned 5A+5B early stop must not ship a knowingly
fail-open seam:

- `LLMService.run` gains a required tenant argument (`organization_id`,
  `workspace_id`) — plumbed through both existing call sites.
- Provenance persistence: the `LLMResponse` fields (`prompt_name`,
  `prompt_version`, `input_hash`, `is_simulated`, `trace_id`, usage) are written to
  the brief row; `input_hash` is computed **in the service layer** so real
  providers cannot silently omit it.
- **Input sanitization at the service boundary:** `sanitize_text` is applied
  inside `LLMService.run` to every string input, **recursively over nested
  containers** (lists/dicts of strings — e.g. the live `audiences: list[str]`
  input), so every task inherits it; a nested-marker test proves it at the
  provider-request level.
- **Fail-closed prompt rendering:** `providers_real._render`'s
  unrendered-template fallback is removed; a render failure raises and nothing
  is sent.
- A `get_llm_service()` accessor replaces direct import of the module-level
  singleton at call sites (the singleton may remain as the default instance);
  the retry sleep gains an injectable clock.
- Metrics vocabulary extension (closed sets in the freeze-owned
  `core/metrics.py`): LLM call count/duration/token metrics with provider/task
  labels drawn from closed vocabularies — no tenant identifiers in labels.

## 3. Entry criteria

1. Implementation authorization exists and names this plan, **and Phase 4B-B,
   INFRA-9 live deployment, and Phase 4B-C are completed or formally
   dispositioned under separate authority** (master plan §13). 5A is the
   round's first gate, so this blocker is restated here rather than left only
   in the master plan.
2. Operator decisions O-1, O-3, O-4, O-6 — RESOLVED 2026-09-01
   (`DECISIONS.PP5-O1-O11.md`); the CF-1 payload encodes the resolved values.
3. Baseline clean; CF-1 authored by the integration owner and passed R1.

## 4. Exit criteria (all must be TRUE with artifacts)

1. Migration applies and downgrades cleanly in CI's round-trip gate; single Alembic
   head preserved.
2. All capability-governance tests extended and green — **updated inside the
   CF-1 payload by the integration owner, same commit as the capability member**
   (master plan §6): value-set/order pins, default-false assertions,
   dark-by-default resolver and migration tests, the `future_activation_phase`
   assertion extended for the new member using the existing milestone-grained
   convention (value `"5A"`, matching the `"4B"` granularity — an explicit,
   reviewed test change), `FeatureFlagsOut` (freeze-owned) updated per resolved
   O-4 `ALT_expose_all` — the flag IS added, so its exact-equality test
   (`apps/api/app/tests/test_api_isolation.py:140`) is updated in the same
   CF-1 commit — and the
   secret-inventory field count moved 88→89 in the same change — **against the CORRECTED baseline of 88**. The
   inventory document still asserts 87 and is stale (`migration_mode` landed
   2026-07-28, after that document's last update); clearing that pre-existing
   drift is a **blocking precondition on CF-1 under separate authority**, not
   something CF-1's own commit performs (master plan §14) (G5
   reconciliation).
3. Brief create/list/fetch works behind the flag in tests; with the flag off every
   brief endpoint answers 503 `capability_unavailable`; resolver errors deny.
4. Brief rows are immutable (no update/delete path exists; a test asserts the
   service exposes none) and carry complete persisted provenance from a
   deterministic mock run.
5. First behavioural tests for `app/llm/` exist and are green: a shared
   `conftest.py` with a seeded `MockLLMProvider` fixture; determinism (same seed +
   inputs ⇒ identical output); every failure branch of the mock's `_simulate`
   inputs marker key (timeout / rate_limit / refusal / provider_error /
   malformed / low_confidence); retry with the injectable clock; `TASK_SCHEMAS`
   validation failure; a golden test over `PROMPTS` that fails if a template
   changes without a version bump; the sanitization-boundary test (nested marker
   provably defanged at the provider-request level); the fail-closed `_render`
   test (mismatch raises, nothing sent).
6. Isolation negatives: cross-workspace brief access 404s non-enumerably;
   `campaign_id` on a brief is validated against the workspace.
7. Frontend brief panel renders from generated types; contract drift gate green.
8. No new flag defaults true anywhere. **Honest behavior-change ledger:** the
   LLM-seam corrections of §2 (tenant argument through both live call sites,
   `get_llm_service()` accessor, injectable retry clock, boundary sanitization,
   fail-closed `_render`) are deliberate changes to **always-live, un-flagged
   pipeline paths** — disclosed here and named in the master plan §9 early-stop
   record; each is test-covered and behavior-preserving for well-formed inputs
   (sanitization defangs rather than drops; render failures that previously sent
   a broken template now raise). Every *other* existing behavior is unchanged
   with flags off, and the brief/approval customer surface is fully dark.

## 5. Builder lanes and exclusive path ownership

| Lane | Exclusive owned paths (exact) | Delivers | Lane-days |
|---|---|---|---|
| **5A-W1** Primary domain | **Model moved to the freeze payload (master plan §6.2.0):** the ORM model for a table this milestone's own contract freeze creates is authored by the integration owner in that freeze's atomic payload, together with its migration and its `apps/api/app/db/models.py` registration line. This lane retains the schemas, service, routes and tests. Lane paths: ~~`apps/api/app/briefs/models.py`~~ (→ CF-1 payload), `briefs/schemas.py`, `briefs/service.py`, `briefs/__init__.py`, `briefs/routes.py` (all new). **Cross-authored slice — tests W2's seam code:** `apps/api/app/tests/test_llm_service.py`, `test_llm_provider.py` | brief entity, service, routes, immutability discipline, provenance capture, flag gate | **1.10** |
| **5A-W2** Seam | `apps/api/app/llm/service.py`, `llm/base.py`, `llm/providers_real.py`; **`apps/api/app/jobs/pipeline.py`** (tenant arg `:211`/`:587`, `get_llm_service()` import `:40` — atomic with the `llm/service.py` signature change. **Forward-touch disclosure: this file is touched again by 5C-W2 in D11 PM–D13 AM for the `check_claim_safety` change at `:360`. Different windows, sequential, never concurrent — and disclosed on BOTH sides as master plan §6.2 requires**). **Cross-authored slice — tests W1's domain code:** `apps/api/app/tests/test_briefs_model.py`, `test_briefs_api.py` | tenant arg, `input_hash`, sanitization boundary, fail-closed `_render`, injectable clock | **1.05** |
| **5A-W3** Primary UI | `apps/web/src/pages/opportunities/BriefPanel.tsx` (new); **`apps/web/src/pages/OpportunityDetail.tsx`** (mount point `:303` — ownership covers creation **and** reachable integration). Authors **no** tests | brief UI, reachable from the opportunity detail page | **0.95** |
| **5A-W4** Adversarial + UI verification | `apps/api/app/tests/fixtures/` (new package, one module per subject area) **and `apps/api/app/tests/conftest.py` (new — created in this window by this lane, carrying the seeded `MockLLMProvider` fixture that exit criterion 5 requires; it becomes freeze-owned only AFTER I-5A, holding imports thereafter. An earlier draft named it only in its post-I-5A freeze-owned state, leaving it unowned during the window in which it must actually be written — the same unowned-artifact defect found in the 5E analytics slice)** — **cross-window disclosure: 5E-W1 later adds `apps/api/app/tests/fixtures/analytics.py` to this package in D20 AM–D22 PM. Different windows, never concurrent; 5A-W4's claim covers the package as created here, not files added by later milestones. Note also that this package is `apps/api/app/tests/fixtures/` and is NOT matched by the freeze-owned governance glob `/tests/fixtures/*`, which is anchored at the repository root (master plan §6.2)**; `apps/api/app/tests/test_briefs_isolation.py`; **W3's UI tests:** `apps/web/src/pages/opportunities/__tests__/brief-panel.test.tsx`, `brief-panel.isolation.test.tsx`, `apps/web/src/pages/__tests__/opportunity-detail.test.tsx` | **the whole adversarial battery** plus verification of W3's surfaces | **1.15** |

**Longest lane 1.15 against a 1.5-day window (local float +0.35, spendable only inside
5A).** Utilisation 73/70/63/77% of the window. Per-lane figures are indicative within
±0.1; the milestone total and the longest lane are the load-bearing quantities. This is
the **operative** statement; the header block restates the window as a **derived**
summary and must be propagated in the same edit. An earlier draft carried this paragraph
twice with divergent sequential-touch days — which is how stale day ranges survived
correction here — and then claimed it was stated only once.

`types.ts`/`endpoints.ts` entries for the brief surface are authored into the **CF-1
payload** by the integration owner. Those files, together with `queryKeys.ts`, `nav.ts`
and `App.tsx`, are **freeze-owned and never lane-owned** (master plan §6.3), so no lane
waits on or contends for them. An earlier draft stated that `queryKeys.ts` is "owned
sequentially by the frontend lane of each milestone"; that is the withdrawn lane-owned
model and is **not** operative.

**Sequential-touch disclosures** (day ranges counted from the master plan §5 table):
`apps/api/app/briefs/service.py` originates here and is touched later by **5D-W3**
(jurisdiction snapshot, D17 AM–D18 AM) and **5E-W3** (notification emission,
D20 AM–D22 PM); `apps/api/app/llm/` is owned here and again by **5C-W2**
(D11 PM–D13 AM); **`apps/api/app/jobs/pipeline.py` is owned here (tenant argument) and
again by 5C-W2 (`check_claim_safety` at `:360`, D11 PM–D13 AM)**. All are different
windows, never concurrent, and each is disclosed on both sides.

The capability governance **pin-test updates** (value-set/order,
`future_activation_phase`, flag-reflection) are **not** lane work: they are part
of the CF-1 payload, same commit as the capability member, authored by the
integration owner (master plan §6) — CF-1 could not merge green otherwise.

Freeze-owned files (enums, config, **metrics**, registry, router registration,
**`system/routes.py` (`FeatureFlagsOut`)**, migrations, OpenAPI/types,
secret-inventory doc) are edited only by the integration owner at CF-1/I-5A, per
the master plan §6.

## 6. Explicitly out of scope for 5A

Approvals and any approval state (5B); creative generation and any new prompt task
(5C); jurisdiction changes (5D); notifications/analytics/export (5E); any change to
`require_role` semantics; any DB-privilege change; any provider credential.

## 7. Rollback

Additive-only: revert the PR(s); the migration downgrades cleanly (verified in CI);
the flag was never enabled anywhere, so no runtime state exists.
