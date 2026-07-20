# 4A-C.1 — Capability Registry, Override Model, and Additive Migration

**Phase:** 4A, batch 4A-C.1 (first production batch of the Phase 4A-C governed
per-workspace capability override foundation).
**Nature:** additive persistence + type-safety foundation only — one closed,
immutable capability registry + one `WorkspaceCapabilityOverride` model + one
additive Alembic migration + model registration + backend tests + this doc.
**No resolver, no precedence execution, no service, no route, no schema, no
contract change, no config/flag change, no frontend, no capability activation.**
**Branch:** `feat/phase-4a-c-1-capability-foundation` (from `main` at
`3897d61037853bc741dd5248958d1667652d2203`, the Phase 4A-C plan merge).
**Alembic head:** advances from `4945b98229e6` to a single head `98289430a3ec`
via one additive migration; no existing migration is altered.

## Scope & non-goals

This batch establishes only the storage and type-safety plane for governed
workspace capability overrides. It deliberately does **not**: define the
capability resolver or any precedence execution; add the override set/clear
service; add any operator or customer route/schema; regenerate the API contract;
change `core/config.py` or flip any flag; wire anything into the feedback,
scheduling, or RSS gates; add any frontend; or create any real override record.
Every capability remains dark.

### Batch-decomposition note (recorded per plan reconciliation)

The merged plan `docs/phase-4a-c-plan.md` §8.25 lists the *logical* decomposition
as **4A-C.1 = registry + resolver (pure, no storage)** and **4A-C.2 = override
model + migration**. This implementation batch was scoped as **"4A-C.1 — Registry,
model, and migration"**, i.e. it consolidates the registry with the model +
migration and **excludes the resolver**. That is a strict subset of the approved
plan: the resolver, override service, and operator API remain later, separately
approved batches. The plan's §8.25 note that "the resolver ships **unconsumed** by
live gates throughout (§8.16)" is upheld trivially here — no resolver exists yet
and nothing consults any override.

## Deliverables

### Capability registry (type-safety foundation)
- `apps/api/app/capabilities/__init__.py` — package docstring; foundation-only,
  ships dark.
- `apps/api/app/capabilities/registry.py`:
  - `Capability` `StrEnum` — the closed set `opportunity_feedback` /
    `scout_scheduling` / `connector_rss` (persisted `.value`, repo convention; no
    native PG enum).
  - `CapabilityPolicy` — a `frozen=True, slots=True` dataclass of immutable
    governance metadata: `label`, `global_flag_attr` (bound to the exact
    `Settings` flag), `workspace_enableable`, `workspace_disableable`,
    `subject_to_safety_ceiling`, `requires_workspace_context`,
    `future_activation_phase`.
  - `CAPABILITY_REGISTRY` — a read-only `MappingProxyType`; `iter_capabilities`
    (deterministic declaration order), `get_policy`, strict `capability_from_value`
    (raises `UnknownCapabilityError`, a `ValueError`), `is_known_capability`, and
    `persisted_values` (sorted — the single source the migration's check
    constraint derives from).
  - Governance policy: RSS is **not** `workspace_enableable` (its activation is a
    global connector-policy/legal decision, service-layer selection — not a
    per-workspace toggle) but stays `workspace_disableable` for deny-biased safety;
    the two route-guarded capabilities are both workspace-enableable and
    -disableable.

### Override model
- `apps/api/app/capabilities/models.py::WorkspaceCapabilityOverride`
  (`__tablename__ = "workspace_capability_overrides"`), on `UUIDPrimaryKeyMixin`
  (`id String(32)` uuid4 hex) + `TimestampMixin` (`created_at`/`updated_at`):
  - Scope FKs `organization_id`, `workspace_id` → CASCADE, indexed, not null.
  - `capability String(64)` not null, DB-restricted to the closed registry set by
    `ck_workspace_capability_override_capability` (derived from
    `persisted_values()`).
  - `enabled Boolean` not null — the recorded intent.
  - `set_by_user_id` → `users.id` **SET NULL**, indexed, nullable (deleting a user
    forgets authorship without destroying the override).
  - `reason Text` nullable — optional non-secret operator note.
  - `uq_workspace_capability_override` unique on `(workspace_id, capability)` —
    one override per capability per workspace (idempotent upsert for a later batch).
  - **Tenant-integrity honesty:** organization/workspace consistency is a
    service-layer concern (deferred to a later batch). The `workspaces` table has
    no `(id, organization_id)` unique key, so a composite tenant FK is not
    available; this mirrors the `opportunity_feedback` precedent of independent
    scope FKs rather than falsely claiming DB enforcement.
- `apps/api/app/db/models.py` — registers `WorkspaceCapabilityOverride`
  (`# noqa: F401`, alphabetical placement) so `Base.metadata` sees the table.

### Migration
- `apps/api/alembic/versions/20260720_1259-98289430a3ec_add_workspace_capability_overrides.py`
  — one additive migration chained off `4945b98229e6`. Autogenerated against a
  schema at head (so it matches the model exactly), then documented. Creates the
  table, its three FK indexes (`batch_alter_table` per repo convention), the unique
  constraint and the check constraint. No backfill (absence = dark default). No
  existing table touched. `downgrade` surgically drops only the new table.

### Tests (36 total: 34 passing + 2 PostgreSQL-gated)
- `apps/api/app/tests/test_capability_registry.py` (**13**) — closed value set and
  exact flag bindings; deterministic enumeration; every bound global flag dark;
  frozen/immutable policies and read-only registry mapping; RSS non-enableable but
  disableable; route-guarded capabilities enableable; strict `capability_from_value`
  / `is_known_capability`; sorted `persisted_values` matching the enum.
- `apps/api/app/tests/test_workspace_capability_override_model.py` (**14**) — table
  metadata (name, columns, nullability), declared unique + check constraints, FK
  `ondelete` semantics, check constraint covering the exact registry set; and,
  against SQLite with `PRAGMA foreign_keys=ON` scoped to the test engine only:
  happy-path insert/read-back, optional reason/actor, uniqueness per
  `(workspace, capability)`, same capability across distinct workspaces, check
  constraint rejects an unknown capability, not-null `enabled`, workspace-delete
  CASCADE, organization-delete CASCADE, and actor-delete SET NULL preserving the
  override.
- `apps/api/app/tests/test_workspace_capability_override_migration.py` (**7 + 2
  gated**) — real Alembic CLI upgrade creates table + indexes, `alembic check`
  reports no model drift, single head `98289430a3ec`, surgical downgrade preserves
  business data, re-upgrade restores; dark-state regression that all three global
  flags remain `False` and that `app.capabilities.resolver` does **not** import
  (resolver deferred); plus PostgreSQL-gated (`TEST_POSTGRES_URL`) check-constraint
  and workspace-cascade invariants.
- `apps/api/app/tests/test_opportunity_feedback_migration.py` — the single-head
  assertion updated from `4945b98229e6` to `98289430a3ec` (the only edit forced by
  advancing the head; the prior-revision downgrade target is unchanged).

### Docs
- This verification doc.

## Validation evidence (local)

- **Ruff:** clean across the new `app/capabilities` package, the three new test
  modules, and the migration.
- **Backend pytest:** **730 passed, 10 skipped** (PostgreSQL-gated), 0 failed. New
  suites: registry **13**, model **14**, migration **7 passed + 2 gated-skipped**.
- **Alembic:** single head `98289430a3ec`; `alembic check` reports no drift;
  upgrade → downgrade (to `4945b98229e6`) → re-upgrade round-trip clean; downgrade
  is surgical (only the new table dropped).
- **Contract:** `npm run gen:types` regenerated `openapi.json` + `schema.d.ts` with
  **zero diff** — this batch adds no path or schema, so the customer/operator
  contract is byte-unchanged.
- **Frontend:** `eslint` clean; `tsc -b --noEmit` clean; `vitest` **76 passed** —
  unchanged (no frontend touched).

## Governance & safety posture (unchanged)

- `opportunity_feedback_enabled = False`; `scout_scheduling_enabled = False`;
  `connector_rss_enabled = False` — all capabilities remain **dark**.
- No resolver, no precedence, no service, no override API, no gate wiring, no real
  override record. The table defaults empty; absence = dark.
- No worker, scheduler, connector, or scoring behaviour changed.
- No production rollout; no feature enabled.

## Closeout status

Deliverable is a **DRAFT** 4A-C.1 PR targeting `main`. Do not mark ready, request
review, or merge as part of this batch. The resolver (with precedence), the
override set/clear service + audit, and the operator management API + contract are
separate, later, explicitly-approved 4A-C batches and are not started here.
