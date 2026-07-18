# 3C-B — Opportunity Feedback Persistence Foundation (dark)

**Phase:** 3C, batch 3C-B (dark-deployed persistence foundation).
**Nature:** additive persistence + service + tests + docs — **no public API, no
OpenAPI/contract change, no UI, no scoring change, no feature enabled.**
**Branch:** `feat/3c-b-feedback-persistence-foundation` (from `main` at
`421e43e78dc26a8d8da202c6244fe1148b5a5a5f`).
**Alembic head:** single head `4945b98229e6` (one new additive migration on top of
`b2c3d4e5f6a7`).

Markets exercised: **Dallas (TX, USA), London (UK), Lagos (Nigeria), Nairobi
(Kenya)** — four independent tenants proving cross-market feedback isolation.

## Scope & non-goals

3C-B delivers the durable, inert foundation the future feedback API (3C-C) and UI
(3C-D) will build on: a persistence model, one additive migration, a capture-only
service, and full tests. It deliberately does **not**: expose any REST endpoint,
schema, OpenAPI route or generated TypeScript contract; add any frontend; enable
`opportunity_feedback_enabled` (or any other flag); influence scoring, ranking,
rescoring, model training, prompts, or any cross-workspace / cross-market signal;
add analytics aggregation, workers, or scheduled processing.

## Approved product decisions realized

The owner-approved (2026-07-18) decisions are encoded exactly:

- **Feedback shape** — required binary `is_useful` plus an *optional* structured
  reason; **no free text** (no note/comment/details column exists).
- **Feedback target** — a direct FK to the immutable intelligence record
  (`intelligence_record_id → signal_intelligence_records.id`) **and** the parent
  opportunity (`opportunity_id`). The version strings
  (`analysis_version` / `scoring_version` / `fingerprint`) are stored as a
  **provenance snapshot copied from the target record at capture time** — never
  primary identity, never caller-supplied.
- **Reason taxonomy** — the approved closed enum (`FeedbackReason`): positive
  `useful_insight`, `strong_evidence`, `commercially_relevant`, `correct_market`;
  negative `irrelevant`, `wrong_market`, `weak_evidence`, `duplicate`, `outdated`,
  `not_commercially_useful`, `other`.
- **Polarity** — a positive reason is valid only with `is_useful=True`, a negative
  only with `is_useful=False`, a `None` reason always valid. Enforced in the domain
  service **and** by a portable DB check constraint
  (`ck_opportunity_feedback_reason_polarity`).
- **Mutability** — append-only: every capture inserts a new row; nothing is updated
  or overwritten. A change of mind is another row.
- **Retention** — tied to the workspace deletion lifecycle
  (`organization`/`workspace`/`opportunity`/`intelligence-record` FKs `CASCADE`);
  no TTL, no purge worker. `submitted_by_user_id` is `SET NULL` so deleting a user
  forgets the author without destroying the immutable record.
- **Scoring influence** — none (capture-only).
- **Feature flag** — `opportunity_feedback_enabled: bool = False` (dark by default).
- **Submitter roles / visibility** — no write or read endpoint ships in 3C-B; the
  service supports actor attribution (`submitted_by_user_id`) for the future
  editor-gated API. Role/feature gating lives at the future route boundary (3C-C),
  mirroring how the scouting-schedule service leaves gating to its route.

## Deliverables

### Configuration
- `apps/api/app/core/config.py` — new dark flag `opportunity_feedback_enabled=False`.

### Domain vocabulary
- `apps/api/app/core/enums.py` — `FeedbackReason` StrEnum plus
  `POSITIVE_FEEDBACK_REASONS` / `NEGATIVE_FEEDBACK_REASONS` polarity sets.

### Persistence model
- `apps/api/app/feedback/models.py` — `OpportunityFeedback` (table
  `opportunity_feedback`) inheriting the standard UUID-PK + timestamp mixins.
  Scope FKs, judgement columns, provenance snapshot, and the portable polarity
  check constraint. Registered for Alembic metadata in `apps/api/app/db/models.py`.

### Migration
- `apps/api/alembic/versions/20260718_1144-4945b98229e6_add_opportunity_feedback.py`
  — one additive migration: creates the table, six scope indexes, and the check
  constraint. `down_revision = b2c3d4e5f6a7`. Purely additive; downgrade is
  surgical (drops only the new table/indexes/constraint).

### Service
- `apps/api/app/feedback/service.py` — `create_feedback(...)`: scope-integrity /
  IDOR validation (the record must belong to the opportunity's exact tenant scope),
  reason normalization + polarity check, provenance derived from the record,
  append-only insert (`db.add` + `db.flush`, caller commits), actor attribution,
  audit (`opportunity_feedback.created`) and structured log
  (`opportunity_feedback_created`).

### Tests
- `apps/api/app/tests/test_opportunity_feedback.py` — 17 tests: happy-path capture
  (positive/negative/`None` reason), provenance copied-from-record, string-reason
  coercion, unknown-reason rejection, append-only, polarity (domain guard + DB
  backstop), scope/IDOR (cross-market and sibling-opportunity rejection), full
  four-market isolation (each binds to its own scope; all 12 cross-market pairs
  rejected), workspace-deletion cascade, user-deletion nulls attribution, and a
  capture-only regression (score untouched, no new intelligence record).
- `apps/api/app/tests/test_opportunity_feedback_migration.py` — Alembic lifecycle
  (upgrade creates table+indexes; `check` reports no drift; single head; surgical
  downgrade preserves business data; re-upgrade restores) plus two
  `TEST_POSTGRES_URL`-gated tests: the polarity check constraint and the
  workspace-deletion cascade enforced by real PostgreSQL.

## Validation evidence

- **Ruff:** clean across `app/`.
- **Backend pytest:** 598 passed, 7 skipped (PostgreSQL-gated), 0 failed.
- **New suites:** `test_opportunity_feedback.py` 17 passed;
  `test_opportunity_feedback_migration.py` 5 passed, 2 skipped (PG-gated).
- **Alembic:** single head `4945b98229e6`; upgrade/downgrade/re-upgrade clean on a
  throwaway SQLite DB; `alembic check` reports no model drift; the migration also
  applied cleanly to the pre-existing populated local dev DB
  (`b2c3d4e5f6a7 → 4945b98229e6`), proving it is additive-safe on live data.
- **Contract:** `scripts/gen-types.sh` produces **no** `openapi.json` /
  `schema.d.ts` drift — 3C-B adds no API surface.
- **Frontend:** `eslint` clean; `tsc -b --noEmit` clean; `vitest` 53 passed.

## Governance & safety posture (unchanged)

- `opportunity_feedback_enabled = False` (new, dark).
- `scout_scheduling_enabled = False`; `connector_rss_enabled = False`.
- No API, UI, worker, scheduler, or scoring path reads or writes this table yet.
- No production rollout; no feature enabled.

## Closeout status

Deliverable is a **DRAFT** 3C-B PR targeting `main`. Do not mark ready, request
review, or merge as part of this batch. Feedback capture remains dark
(`opportunity_feedback_enabled = False`). 3C-C (feature-gated API) is a separate,
later batch and is not started here.
