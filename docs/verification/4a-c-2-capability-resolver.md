# 4A-C.2 — Centralized, Deny-Biased Capability Resolver

**Phase:** 4A, batch 4A-C.2 (the decision plane of the Phase 4A-C governed
per-workspace capability foundation).
**Nature:** additive, read-only backend logic + unit tests only — one new pure
module `apps/api/app/capabilities/resolver.py`, one immutable result type
(`CapabilityResolution`), one bounded decision-source enum (`DecisionSource`), one
new test module, one inverted dark-state guard, and this doc.
**No resolver wiring into any live gate, no override service, no route/schema, no
contract change, no migration, no `core/config.py`/flag change, no dependency
change, no frontend, no capability activation, no real override record.**
**Branch:** `feat/phase-4a-c-2-capability-resolver` (from `main` at the Phase
4A-C.1 merge).
**Alembic head:** unchanged single head `98289430a3ec` (12 migrations); no
migration added.

## Scope & non-goals

This batch adds only the *decision* plane: a single, centralized, pure resolver
that computes the effective state of one `(capability, workspace)` pair under a
strict deny-biased precedence and returns the boolean plus the deciding rule. It
deliberately does **not**: wire the resolver into `feedback/routes.py`,
`scouting_requests/routes.py`, `scouting_requests/schedules.py`, or
`connectors/registry.py`; add the override set/clear service (4A-C.3); add any
operator/customer route or schema (4A-C.4); regenerate the API contract; change
`core/config.py` or flip any flag; add a migration; add caching; add logging or
metrics; touch the frontend; or create any real override record. The resolver
ships **unconsumed** and every capability remains dark.

## Deliverables

### Resolver (`apps/api/app/capabilities/resolver.py`)
- `DecisionSource` — a bounded `StrEnum` of the four precedence outcomes:
  `safety_ceiling`, `workspace_override`, `global_configuration`, `secure_default`.
- `CapabilityResolution` — a `frozen=True, slots=True` immutable, secret-free
  dataclass carrying `capability`, `workspace_id`, `effective_enabled`,
  `decided_by`, `global_flag`, `has_override`, `override_value`
  (`override_value is None` iff `has_override is False`). No reason text, actor id,
  timestamps, URLs, payloads, tokens, or raw errors.
- `_ceiling_blocks(capability)` — the pure rule-1 helper; blocks only an
  unknown/unregistered capability (absent from `CAPABILITY_REGISTRY`). No
  registered capability is environment-blocked by default; the rule-1 slot is
  reserved for a later environment signal without reordering precedence.
- `decide_capability(*, capability, workspace_id, global_flag, override_value)` —
  the **pure**, DB-free precedence function (the primary unit-test surface,
  mirroring `app.jobs.stuck.is_job_stuck`). Applies rules 1→4:
  1. **Safety ceiling** → disabled/`safety_ceiling` for unknown/forged capability.
  2. **Honored workspace override** → an `enabled=False` override is honored for a
     `workspace_disableable` capability; an `enabled=True` override is honored only
     for a `workspace_enableable` capability. An **un-honorable** override (e.g. an
     enable on RSS, which is not workspace-enableable) is deny-biased: it does
     **not** enable and does **not** fall to the global flag — it resolves to
     disabled/`secure_default`.
  3. **Global configuration** → the bound global `*_enabled` flag decides when no
     honored override exists.
  4. **Secure default** → disabled (the un-honorable-override path above).
- `_load_override(session, *, capability, organization_id, workspace_id)` — issues
  exactly one indexed query for the `(workspace_id, capability.value)` row (unique
  constraint ⇒ at most one). Returns `None` (treated as "no honored override")
  when no row exists **or** when the stored `organization_id` mismatches the passed
  scope (deny-biased in-memory tenant validation).
- `resolve_capability(*, session, settings, capability, organization_id,
  workspace_id)` — the thin keyword-only I/O wrapper: short-circuits the ceiling
  for an unknown/forged capability; else reads the bound flag via
  `getattr(settings, policy.global_flag_attr)` (registry-derived, never a
  hardcoded name), loads + tenant-validates the override, and delegates to
  `decide_capability`. Never raises for a governance outcome; opens no session of
  its own; performs no write; toggles no flag.
- Explicit `__all__`.

### Tests (`apps/api/app/tests/test_capability_resolver.py`, 21 tests)
- **Pure precedence (`decide_capability`, no DB):** ceiling blocks an
  unknown/forged capability over a would-be enable + `True` flag; enable override
  decides over a `False` flag; disable override decides over a `True` flag; an RSS
  enable override is un-honorable → `secure_default`/disabled; an RSS disable
  override is honored; the global flag decides both ways with no override
  (parametrized).
- **DB-backed (`resolve_capability`, engine-scoped SQLite + `PRAGMA
  foreign_keys=ON`):** happy-path enable via a seeded override; disable over a
  `True` flag; no row → global flag; tenant mismatch (row org ≠ passed org) →
  absent → global flag; per-workspace isolation; per-capability isolation; a
  persisted RSS enable row never enables; exactly one `SELECT` per resolution
  (asserted via a `before_cursor_execute` counter).
- **Dark-by-default + registry coupling:** with shipped defaults every registered
  capability resolves disabled/`global_configuration`; every capability's
  `global_flag_attr` resolves on `Settings` and is `False`.
- **Result shape/secret-free:** frozen (mutation raises `FrozenInstanceError`);
  fields are exactly the documented set; `override_value is None` iff no override;
  `DecisionSource` is the bounded four-member set.

### Inverted dark-state guard
- `apps/api/app/tests/test_workspace_capability_override_migration.py` — the single
  legitimate edit to an existing test:
  `test_no_resolver_module_shipped_in_this_batch` →
  `test_resolver_ships_but_stays_unconsumed_by_live_gates`, which now asserts the
  resolver imports cleanly **and** that none of the four live gate modules contains
  `capabilities.resolver` / `resolve_capability`.

### Docs
- This verification doc.

## Plan-vs-implementation-prompt discrepancy (recorded per prompt §3)

The implementation prompt requested a broader tenant-validation posture than the
merged plan authorizes — specifically an authoritative `Workspace`-existence load
with org-ownership confirmation and a typed tenant/infra error taxonomy. The
merged plan `docs/phase-4a-c-2-plan.md` §8.7/§8.11/§8.14 scopes this narrower: the
resolver raises only for a genuine programmer error, achieves deny-bias through the
**precedence shape** (no broad `except`-to-disabled), and validates tenancy by an
in-memory org-match on the loaded override row — deferring the authoritative
`Workspace`-load org-of-workspace check to the service layer (4A-C.3/4A-C.4).
**Per prompt §3, this implementation follows the merged plan** (the smaller,
approved scope): no separate error module, no `Workspace` load. Scope was not
silently expanded.

One faithful minor reconciliation within the plan: the plan's §8.13 sketch of
`decide_capability` omits `workspace_id`, but `CapabilityResolution` requires it
(§8.8), so `workspace_id` is a keyword parameter of `decide_capability`. Also, the
plan's rule-2 prose is read together with acceptance item #9 ("an override may only
narrow or match what the ceiling permits") and the registry's
`workspace_enableable` bit, so an un-honorable enable (RSS) resolves to
`secure_default` rather than enabling — this keeps all four `DecisionSource`
outcomes reachable and the dark default provable.

## Validation evidence (local)

- **Ruff:** clean across `app/capabilities/resolver.py`,
  `app/tests/test_capability_resolver.py`, and the edited migration test.
- **Backend pytest:** **751 passed, 10 skipped** (PostgreSQL-gated), 0 failed
  (was 730 passed at 4A-C.1; +21 new resolver tests). The resolver suite is 21
  tests; the inverted migration guard passes.
- **Alembic:** single head `98289430a3ec`; `alembic current` at head; 12
  migrations; `alembic check` reports "No new upgrade operations detected" — no
  migration added, no drift.
- **Contract:** `npm run gen:types` regenerated `openapi.json` + `schema.d.ts` with
  **zero diff** — this batch adds no path or schema.
- **Dark-state:** `connector_rss_enabled`, `scout_scheduling_enabled`,
  `opportunity_feedback_enabled` all `bool = False` in `core/config.py` (unchanged).
  A repo grep confirms `capabilities.resolver` / `resolve_capability` /
  `decide_capability` appear **only** in the resolver module, its own test, and the
  non-consumption assertion in the migration test — no live gate imports it.

## Governance & safety posture (unchanged)

- All three global capability flags remain `False` — every capability is **dark**.
- The resolver is **unconsumed**: no feedback, scheduling, or RSS gate imports or
  calls it; the three enforcement points keep their direct global-flag checks.
- No override service, no operator/customer route, no contract change, no
  migration, no flag flip, no real override record. The override table stays empty;
  absence resolves disabled.
- No worker, scheduler, connector, or scoring behaviour changed.

## Closeout status

Deliverable is a **DRAFT** 4A-C.2 PR targeting `main`. Do not mark ready, request
review, or merge as part of this batch. The override set/clear service + audit
(4A-C.3) and the operator management API + contract (4A-C.4) are separate, later,
explicitly-approved batches and are not started here.
