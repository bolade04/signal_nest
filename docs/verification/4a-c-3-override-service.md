# 4A-C.3 — Governed Workspace Capability Override Service (Verification & Closeout)

**Phase:** 4A, batch 4A-C.3 (the write/read + audit plane of the Phase 4A-C governed
per-workspace capability foundation), closed out by sub-batch **4A-C.3.6**.
**Nature:** additive backend service + tests + this doc only. No resolver wiring into
any live gate, no override service consumer, no route/schema, no contract change, no
migration, no `core/config.py`/flag change, no dependency change, no frontend, no
capability activation, no real override record.
**Baseline SHA (4A-C.3 merged into `main`):**
`eda3eee5fa52d005e820cfb4969e32f084b4d656` (squash-merge of PR #78).
**4A-C.3.6 branch:** `test/phase-4a-c-3-6-override-verification` (from `main` at the
4A-C.3.5 merge SHA above).
**Alembic head:** unchanged single head `98289430a3ec` (12 migrations); no migration
added by any 4A-C.3 sub-batch.

## Scope & non-goals

Phase 4A-C.3 shipped the governed **service** plane — the only write path for
`WorkspaceCapabilityOverride` rows plus its read-back accessors — in ordered,
independently-reviewed sub-batches:

- **4A-C.3.1** — typed errors (`capabilities/errors.py`) + immutable result models
  (`OverrideMutation`, `OverridePage`) + type-foundation tests.
- **4A-C.3.2** — read plane: `get_capability_override`, `list_capability_overrides`
  + authoritative tenant validation.
- **4A-C.3.3** — `set_capability_override`: deny-biased registry-policy enforcement,
  bounded reason validation, idempotent upsert, `.created`/`.updated`/`.rejected`
  audit.
- **4A-C.3.4** — `clear_capability_override`: idempotent delete-or-no-op, `.cleared`
  audit.
- **4A-C.3.5** — concurrency: a `SELECT … FOR UPDATE` workspace-row lock on every
  mutation, an always-run PostgreSQL compile proof, and three real-PostgreSQL
  convergence tests, backed by the unique constraint + `begin_nested` SAVEPOINT
  retry.
- **4A-C.3.6 (this batch)** — the final dark-state + no-live-consumer guards and this
  verification/closeout doc.

It deliberately does **not**: wire the resolver or the service into `feedback/routes.py`,
`scouting_requests/routes.py`, `scouting_requests/schedules.py`, or
`connectors/registry.py`; add any operator/customer route or schema (4A-C.4);
regenerate the API contract; change `core/config.py` or flip any flag; add a
migration; touch the frontend; or create any real override record. The service ships
**unconsumed** and every capability remains dark.

## 4A-C.3.6 deliverables

### Dark-state coupling guards (§8.29 #21) — `test_capability_override_service.py`
- `test_dark_state_shipped_defaults_resolve_every_capability_disabled` — with shipped
  defaults (all three flags `False`) and no real override row, every registered
  capability resolves disabled via `GLOBAL_CONFIGURATION` in a sample workspace
  (the production baseline, acceptance #45).
- `test_dark_state_service_write_is_inert_without_a_live_consumer` — the
  persistence-vs-activation split: `set_capability_override` persists an enable
  override that the resolver *alone* would honor (`WORKSPACE_OVERRIDE`) while the
  bound global flag stays `False`; because no production module consumes the
  resolver, the capability is never globally activated; `clear_capability_override`
  returns resolution to the dark global default.
- `test_dark_state_rss_stays_disabled_across_service_mutations` — RSS is not
  `workspace_enableable`, so the service rejects an enable set (no row) and RSS
  resolves disabled regardless of any set/clear.

### No-live-consumer guards (§8.29 #22, §8.31) — `test_capability_override_service.py`
- `test_no_production_module_imports_the_override_service` — a precise **AST**
  import-boundary scan (not brittle whole-file string matching) parses every
  production module under `app/` (excluding the test package and the service module
  itself) and asserts none imports `app.capabilities.service` or a symbol from it,
  via absolute **or** relative import. A docstring/comment mention is ignored; only a
  real `import` binds a consumer. A route/worker/connector/scheduler wiring the
  service into a live path fails CI here.
- `test_no_consumer_guard_actually_scans_the_live_gate_modules` — a self-check that
  the scan actually covers the known live-gate modules
  (`capabilities/resolver.py`, `feedback/routes.py`, `scouting_requests/routes.py`,
  `scouting_requests/schedules.py`, `connectors/registry.py`) and excludes the
  service module, so a future consumer cannot slip through an empty/misrouted file
  set.

### Docs
- This verification/closeout doc.

## Plan-vs-implementation-prompt discrepancy (recorded per §8.34 #49)

The implementation prompt's literal phrasing for §8.29 #21 — "resolve_capability
still resolves the capability **disabled** whenever the global flag is `False`" — is
read together with the resolver's authoritative precedence (§8.9): a *honored*
per-workspace enable override on a `workspace_enableable` capability resolves
`effective_enabled=True` via `WORKSPACE_OVERRIDE`, independent of the global flag.
The plan's own parenthetical resolves this: "a per-workspace enable override only
*would* enable if a live gate consumed the resolver — which none does". The guard
tests therefore assert the faithful **persistence-vs-activation split** rather than a
literal-but-false "always disabled" claim: (a) with no real override row every
capability is disabled; (b) a service-persisted enable is honored by the resolver
alone while the global flag stays `False` and no live consumer exists, so nothing is
globally activated; (c) clearing returns to the dark default; (d) RSS can never be
raised by an override at all. This follows the merged plan's intent (the dark-state
coupling / persistence-vs-activation split) without silently expanding or
misrepresenting resolver behavior.

No production code was changed in 4A-C.3.6: the resolver, registry, models,
`core/config.py`, every route, the migration set, and the contract are byte-for-byte
unchanged.

## Validation evidence

### Real-PostgreSQL concurrency (from the exact merged CI runs)
- **4A-C.3.5 PR #78 head** `c28b8e4891f9fa24fac6f582a509a95f78fb5f67` — CI run
  `29793139390`: Backend quality ran against a healthy `postgres:16` service with
  `TEST_POSTGRES_URL` configured; `collected 816 items` → **816 passed, 0 skipped**.
  The three `_pg_only` concurrency tests
  (`test_pg_concurrent_identical_creates_converge_to_one_row`,
  `test_pg_concurrent_sets_converge_without_duplicate_or_lost_update`,
  `test_pg_concurrent_set_and_clear_converge_to_terminal_state`) executed against
  real PostgreSQL and passed, alongside the always-run `FOR UPDATE` compile proof.
- **Post-merge `push` on `main`** at the merge SHA
  `eda3eee5fa52d005e820cfb4969e32f084b4d656` — CI run `29794048847`: all five jobs
  succeeded; Backend quality again ran against `postgres:16` with `TEST_POSTGRES_URL`
  set, `collected 816 items` → **816 passed, 0 skipped**.

Both runs share the same 816-item collection with zero skips, so the PostgreSQL-gated
tests were collected and executed (not skipped, deselected, or xfailed) on the exact
merged head.

### Local (4A-C.3.6 branch)
- **Ruff:** `ruff check` and `ruff format --check` clean on the changed test module.
- **Backend pytest:** changed test module — **45 passed, 3 skipped**; related
  capability suites (resolver, types, service, migration, model) — **99 passed, 5
  skipped**; full backend suite — **808 passed, 13 skipped**. The five new 4A-C.3.6
  guard tests pass. They are dialect-agnostic (SQLite), so no new
  PostgreSQL-dependent test was added.
- **PostgreSQL is CI-only, not local:** the PostgreSQL-gated tests (the 3 concurrency
  tests + the other `_pg_only`/gated cases) are **skipped locally** because
  `TEST_POSTGRES_URL` is unset — that is the entire local skip count. Real-PostgreSQL
  execution is proven **only** by the GitHub CI runs recorded above
  (`29793139390`, `29794048847`), never by local SQLite. No local statement claims
  PostgreSQL execution.
- **Alembic:** single head `98289430a3ec`; 12 migrations; no migration added by
  4A-C.3.6; no drift.
- **Contract:** `npm run gen:types` regenerated `openapi.json` + `schema.d.ts` with a
  **zero diff** — this batch adds no path or schema.

## Governance & safety posture (unchanged)

- All three global capability flags — `connector_rss_enabled`,
  `scout_scheduling_enabled`, `opportunity_feedback_enabled` — remain `bool = False`
  in `core/config.py`. Every capability is **dark**.
- The resolver is byte-for-byte unchanged and remains **unconsumed**; the service has
  **no live consumer** (AST guard). No feedback, scheduling, RSS, worker, connector,
  or scheduler path imports or invokes either module.
- No operator/customer route, no contract change, no migration, no flag flip, no real
  override record. The override table holds only throwaway test rows; absence resolves
  disabled.

## Closeout status

Deliverable is a **DRAFT** 4A-C.3.6 PR from `test/phase-4a-c-3-6-override-verification`
targeting `main` (acceptance §8.34 #50): it is **not** marked ready, merged, or
assigned a reviewer without explicit authorization. Phase 4A-C.4
(operator/customer management API + contract) is a separate, later,
explicitly-approved batch and is not started here. The Phase 4A-C.3 acceptance
criteria (§8.34) are satisfied: the service is the sole write path with tenant
validation, deny-biased policy enforcement, idempotent upsert/clear, transactional
audit, `FOR UPDATE` concurrency serialization proven on real PostgreSQL, an unchanged
single Alembic head, a zero contract diff, an unconsumed resolver, a consumer-free
service, and all capabilities dark.
