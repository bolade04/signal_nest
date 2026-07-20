# Phase 4A-C.2 — Centralized, Deny-Biased Capability Resolver (Plan)

## 8.1 Title & status

**Batch:** Phase 4A-C.2 — Centralized, Deny-Biased Capability Resolver.
**Nature:** additive, read-only backend logic + unit tests only. One new pure
module `apps/api/app/capabilities/resolver.py`, one immutable result type, one
bounded decision-source enum, and one new test module — plus this plan doc and a
minimal, evidence-preserving doc-alignment note.
**Status:** `PLANNING — DOCUMENTATION ONLY — RESOLVER NOT IMPLEMENTED —
PERSISTENCE UNUSED — ALL CAPABILITIES REMAIN DARK`.
**Baseline:** `main` at `47852c7fbd3861011bf2bcfd79719d8ea9a8c6c7` (Phase 4A-C.1
merge, PR #70). Alembic single head `98289430a3ec` (12 migrations). All three
global capability flags `False`.

This document is a planning artifact. It authorizes no source, test, migration,
contract, dependency, or flag change. Implementation is a later, separately
approved batch.

## 8.2 Executive summary

Phase 4A-C.1 landed the storage-and-type-safety plane for governed per-workspace
capability overrides: a closed, immutable `Capability` registry
(`apps/api/app/capabilities/registry.py`), the `WorkspaceCapabilityOverride`
model + additive migration (`apps/api/app/capabilities/models.py`,
`.../alembic/versions/…98289430a3ec…`), and comprehensive tests. That batch
consumed nothing: no resolver reads the overrides and no live gate consults them.
Every capability is dark.

Phase 4A-C.2 adds the **decision plane**: a single, centralized, pure function
that computes the *effective* state of one `(capability, workspace)` pair under a
strict, deny-biased precedence — safety ceiling → workspace override → global
configuration → secure default — and returns both the boolean and the *deciding
rule*. It mirrors the Phase 4A-B "one predicate is the single source of truth"
pattern established by `apps/api/app/jobs/stuck.py` (`is_job_stuck`).

Critically, this batch ships the resolver **unconsumed**. No feedback,
scheduling, or RSS gate imports or calls it (§8.16 of `docs/phase-4a-c-plan.md`).
It exists to be exhaustively unit-tested in isolation *before* it ever gates a
real request. Because every global flag is `False` and the override table is
empty, the resolver resolves every capability in every workspace to **disabled**
via the secure default / global-configuration rule — the dark default is
preserved and provable.

## 8.3 Goals

- **G1.** A single authoritative resolver
  `resolve_capability(*, session, settings, capability, organization_id,
  workspace_id) -> CapabilityResolution` that is the *only* place a capability's
  effective state is decided.
- **G2.** A strict, deny-biased precedence chain that can *never* enable a
  capability the safety ceiling prohibits, and that resolves to disabled on any
  absence, malformation, or error.
- **G3.** An immutable, minimal, secret-free result type carrying the effective
  boolean and a bounded decision-source enum so an operator surface can always
  explain *why* a capability resolved as it did.
- **G4.** Purity and testability equivalent to `is_job_stuck`: explicit
  `settings` + `session` inputs, at most one indexed lookup, no writes, no global
  state, deterministic.
- **G5.** Keep everything dark and additive: no live gate consults the resolver;
  no global flag flips; no schema, migration, contract, or dependency change; the
  override table stays empty.

## 8.4 Non-goals (explicit prohibitions)

- **N1.** No wiring of the resolver into any live gate (`feedback/routes.py`,
  `scouting_requests/routes.py`, `scouting_requests/schedules.py`,
  `connectors/registry.py`). The resolver ships unconsumed.
- **N2.** No override **service** (set/clear/upsert/audit) — that is Phase 4A-C.3.
- **N3.** No operator or customer **route/schema** and no contract regeneration —
  that is Phase 4A-C.4.
- **N4.** No new migration; Alembic head stays `98289430a3ec`. No model change.
- **N5.** No `core/config.py` change and no flag flip; all three remain `False`.
- **N6.** No caching layer (see §8.17). No request-scoped memo in this batch.
- **N7.** No frontend change. No dependency change. No changes to PR #34 or any
  Dependabot PR.
- **N8.** No creation of any real override row (tests seed their own throwaway
  rows in an in-memory/temp SQLite DB and tear them down).

## 8.5 Current-state architecture (audit findings)

- **Registry (present, merged).** `apps/api/app/capabilities/registry.py`
  exposes the closed `Capability` `StrEnum` (`opportunity_feedback`,
  `scout_scheduling`, `connector_rss`), the frozen `CapabilityPolicy` dataclass
  (`slots=True, frozen=True`) with `global_flag_attr`, `workspace_enableable`,
  `workspace_disableable`, `subject_to_safety_ceiling`,
  `requires_workspace_context`, `future_activation_phase`; the read-only
  `CAPABILITY_REGISTRY` (`MappingProxyType`); and the accessors
  `iter_capabilities()` (declaration order), `get_policy()`,
  `capability_from_value()` (strict, raises `UnknownCapabilityError`),
  `is_known_capability()`, and `persisted_values()` (sorted). RSS is
  `workspace_enableable=False` (global connector-policy/legal decision) but
  `workspace_disableable=True` (deny-biased). Every capability is
  `subject_to_safety_ceiling=True`.
- **Override model (present, merged).**
  `apps/api/app/capabilities/models.py::WorkspaceCapabilityOverride`, table
  `workspace_capability_overrides`: `id String(32)` PK, `organization_id`
  (FK→organizations CASCADE, indexed), `workspace_id` (FK→workspaces CASCADE,
  indexed), `capability String(64)` (check-constrained to the registry set),
  `enabled Boolean` (not null), `set_by_user_id` (FK→users SET NULL, indexed,
  nullable), `reason Text` (nullable), plus timestamps. Unique
  `(workspace_id, capability)`. **No** `(id, organization_id)` composite tenant
  FK exists on `workspaces` (only `uq_ws_org_slug` on `(organization_id, slug)`),
  so org/workspace consistency is not DB-enforced — the resolver must treat it as
  a validation concern (§8.10).
- **Tenancy models.** `Workspace.organization_id` (FK→organizations CASCADE) is
  the authoritative org membership of a workspace; `Organization` and `User` are
  `String(32)` PKs. There is no cross-org workspace sharing.
- **Global config.** `apps/api/app/core/config.py` `Settings` holds
  `connector_rss_enabled` (line 220), `scout_scheduling_enabled` (line 235),
  `opportunity_feedback_enabled` (line 242), all `bool = False`. `get_settings()`
  is `@lru_cache` and is called directly (never via FastAPI `Depends`). The
  resolver takes `settings` as an explicit parameter (purity), reading the bound
  flag via `getattr(settings, policy.global_flag_attr)`.
- **DB session convention.** `sqlalchemy.orm.Session`, `select(...)` +
  `session.scalar(...)`, portable to SQLite (local) and PostgreSQL (full). The
  resolver receives an explicit `Session` (never opens its own).
- **Pure-classifier precedent.** `apps/api/app/jobs/stuck.py` is the canonical
  shape to mirror: a pure predicate (`is_job_stuck`) that is clock/settings
  injected, plus SQL helpers kept in lockstep, plus an explicit `__all__`. The
  resolver is the same shape: one pure decision function, plus a thin DB-lookup
  helper, kept in lockstep with the registry.
- **Logging/metrics.** `apps/api/app/core/logging.py::log_event(...)` for bounded
  structured events; default metrics backend is `NoOpMetrics`. The resolver is a
  pure read and, in this batch, is unconsumed — so it emits **no** logs and
  **no** metrics by default (see §8.19).
- **Tests.** Existing capability tests
  (`test_capability_registry.py`, `test_workspace_capability_override_model.py`,
  `test_workspace_capability_override_migration.py`) establish the SQLite-with-
  `PRAGMA foreign_keys=ON` engine-scoped fixture pattern and the dark-state
  regression guard `test_no_resolver_module_shipped_in_this_batch` (asserts
  `app.capabilities.resolver` raises `ModuleNotFoundError`). **That guard must be
  retired/inverted by this batch** (§8.9, §8.21): once the resolver ships, the
  regression flips to asserting the resolver imports *and* remains unconsumed by
  live gates.

## 8.6 Resolver architecture

`apps/api/app/capabilities/resolver.py` is a single pure module. It contains:

1. `DecisionSource` — a bounded `StrEnum` naming the deciding rule.
2. `CapabilityResolution` — an immutable (`frozen=True, slots=True`) dataclass
   value carrying the effective boolean and the decision provenance.
3. `resolve_capability(...)` — the public entry point (§8.7), which performs at
   most one indexed override lookup and delegates the decision to a pure
   in-memory function.
4. `decide_capability(...)` — a **pure**, DB-free decision function (§8.13) that
   takes already-fetched primitives (global flag, optional override value,
   ceiling verdict) and returns the `CapabilityResolution`. This is the unit-test
   surface; it mirrors `is_job_stuck` (no session, fully deterministic).
5. A thin `_load_override(...)` helper that issues the single indexed query
   `select(WorkspaceCapabilityOverride).where(workspace_id==…, capability==…)`.
6. An explicit `__all__`.

Separation of `resolve_capability` (does I/O) from `decide_capability` (pure)
keeps the precedence exhaustively unit-testable without a database, exactly as
`stuck.py` separates `is_job_stuck` (pure) from `_stuck_conditions`/`count_stuck`
(DB).

## 8.7 Public interface (conceptual)

```python
def resolve_capability(
    *,
    session: Session,
    settings: Settings,
    capability: Capability,
    organization_id: str,
    workspace_id: str,
) -> CapabilityResolution: ...
```

- **Keyword-only** for call-site clarity and to prevent positional mistakes at a
  safety-critical boundary.
- `capability` is a `Capability` enum member (not a raw string). A string entry
  point, if ever needed by a route, converts via
  `capability_from_value(...)` first and lets `UnknownCapabilityError` surface —
  but the *resolver core* only accepts the typed enum, so an unknown value can
  never reach the precedence logic as "some string."
- `organization_id` / `workspace_id` are `str` (the repo's `String(32)` uuid4-hex
  ids). The conceptual signature is sometimes written with `UUID`; the concrete
  repo type is the hex `str` — this plan uses `str` to match the persisted
  columns and the rest of the codebase. (Reconciliation noted in §8.28 Q3.)
- Returns a `CapabilityResolution`; never raises for a *governance* outcome
  (deny-biased: absence/error → disabled, not an exception). It may raise only for
  a genuine programmer error (e.g. a non-`Capability` argument), never for
  business state.

## 8.8 Result type (`CapabilityResolution`)

An immutable, minimal, secret-free value:

```python
@dataclass(frozen=True, slots=True)
class CapabilityResolution:
    capability: Capability
    workspace_id: str
    effective_enabled: bool
    decided_by: DecisionSource
    global_flag: bool
    has_override: bool
    override_value: bool | None
```

- `effective_enabled` is the single answer a future gate consumes.
- `decided_by` explains *why* (bounded enum, operator-safe).
- `global_flag`, `has_override`, `override_value` are provenance fields an
  operator effective-state read (Phase 4A-C.4) can render — all non-secret
  primitives. `override_value` is `None` when `has_override` is `False`.
- **No** free-form strings, ids beyond the workspace id, timestamps, URLs,
  payloads, tokens, or error text. Secret-free by construction.

`DecisionSource` bounded values (the four precedence outcomes, §8.9):
`safety_ceiling`, `workspace_override`, `global_configuration`, `secure_default`.

## 8.9 Exact precedence (deny-biased)

Evaluated top-down; the first decisive rule wins.

1. **Safety ceiling (hard).** If the safety ceiling prohibits the capability
   (§8.10), the result is **disabled**, `decided_by=safety_ceiling`, regardless of
   any override or global flag. *An override can never raise a capability above
   the ceiling.*
2. **Explicit workspace override.** Else, if a well-formed override row exists for
   `(workspace_id, capability)` and it passes tenant validation (§8.11), its
   `enabled` boolean decides, `decided_by=workspace_override`. (An override may
   only narrow or match what the ceiling permits — rule 1 already ran.)
3. **Global configuration.** Else the bound global `*_enabled` flag decides,
   `decided_by=global_configuration`.
4. **Secure default.** If nothing above is decisive (defensive; unreachable given
   rule 3 always decides once reached because the flag is a concrete bool), the
   result is **disabled**, `decided_by=secure_default`.

**Naming reconciliation.** The merged `docs/phase-4a-c-plan.md` §8.8 drafted the
enum as `global_flag` / `default_disabled`. This plan uses the clearer
`global_configuration` / `secure_default`. The first two members
(`safety_ceiling`, `workspace_override`) are unchanged. Final naming is settled at
review (§8.28 Q1); the *semantics* are identical either way.

**Invariants (must be enforced and tested):**
- Absence of data = disabled. A missing / malformed / unknown-capability /
  tenant-mismatched override is treated as "no override," never "enabled."
- A workspace override may only narrow or match what the ceiling permits.
- Per-workspace isolation: an override for one `(workspace, capability)` never
  affects another workspace or another capability.
- With all global flags `False` and no override rows, every capability in every
  workspace resolves **disabled** via rule 3 (`global_configuration` → false) —
  the provable dark default.

## 8.10 Safety ceiling (rule 1)

The ceiling is the deny-biased hard cap evaluated first. Its inputs in this batch
are deliberately minimal and derive from the registry, not from new config:

- **Unknown / unregistered capability.** Because the public entry point accepts a
  typed `Capability`, an unknown *string* is rejected upstream by
  `capability_from_value`. Defensively, `decide_capability` still treats any value
  absent from `CAPABILITY_REGISTRY` as ceiling-blocked (`safety_ceiling` →
  disabled) so the resolver is safe even if called with a future/forged member.
- **`subject_to_safety_ceiling` policy bit.** Every registered capability today
  has `subject_to_safety_ceiling=True`, meaning it is *eligible* to be ceiling-
  blocked. In this batch the ceiling does **not** block any registered capability
  by default (there is no environment-prohibited signal wired yet); it only blocks
  unknown/unregistered capabilities. A broader environment-driven ceiling (e.g. an
  "activation-prohibited" environment) is an explicitly deferred open decision
  (§8.28 Q2) — the resolver's shape reserves the ceiling as rule 1 so that signal
  can be added later without reordering precedence.

The ceiling is expressed as a small pure helper `_ceiling_blocks(capability)`
returning `bool`, kept trivially testable. No config change accompanies it.

## 8.11 Tenant validation

- The resolver receives both `organization_id` and `workspace_id`. A well-formed
  override is one whose stored `(organization_id, workspace_id)` matches the
  caller's scope. If the override row's `organization_id` does not match the
  passed `organization_id`, the override is treated as **absent** (rule 2 does not
  fire; evaluation falls through to the global flag). This is deny-biased: a
  cross-tenant or inconsistent row can never enable a capability.
- Because `workspaces` has no `(id, organization_id)` composite key, the resolver
  does **not** attempt a DB-level join guarantee; it validates in memory against
  the passed scope. The authoritative org-of-workspace check (loading the
  `Workspace` and confirming `workspace.organization_id == organization_id`) is a
  **service-layer** responsibility (Phase 4A-C.3/4A-C.4) at the write and route
  boundary. The resolver's job is to *never* honor a row whose recorded org does
  not match the scope it was asked about.
- The resolver performs **no** membership/authorization decision (that is the
  route's `require_operator` / tenant-scope concern). It answers only "is this
  capability effectively on for this workspace?".

## 8.12 Override lookup

- Exactly one indexed query per resolution:
  `select(WorkspaceCapabilityOverride).where(
     WorkspaceCapabilityOverride.workspace_id == workspace_id,
     WorkspaceCapabilityOverride.capability == capability.value)`
  resolved with `session.scalar(...)`. The unique `(workspace_id, capability)`
  constraint guarantees at most one row.
- The `capability` column is compared by `.value` (repo convention).
- A future batch variant `resolve_all_for_workspace(...)` (documented, **not
  built here**) would issue one query for all of a workspace's override rows and
  resolve each registered capability against that in-memory set — reserved for the
  operator effective-state read (4A-C.4), out of scope now.

## 8.13 Pure decision function

`decide_capability(*, capability, global_flag, override_value) ->
CapabilityResolution` is DB-free and deterministic:

- Inputs are already-fetched primitives: the `Capability`, the resolved
  `global_flag: bool` (from `getattr(settings, policy.global_flag_attr)`), and
  `override_value: bool | None` (already tenant-validated by the caller; `None`
  means "no honored override").
- It applies §8.9 rules 1→4 in order and constructs the immutable result.
- It is the primary unit-test surface (mirrors `is_job_stuck`): every precedence
  branch is exercised without a database.

`resolve_capability` is the thin I/O wrapper: it looks up the policy, reads the
global flag from `settings`, loads + tenant-validates the override row, then calls
`decide_capability`. This keeps all branching logic pure and all I/O in one tiny,
easily-reviewed function.

## 8.14 DB-error behavior (deny-biased)

- The resolver does not catch generic exceptions to mask bugs. However, the
  precedence is arranged so that any *absence* — no row, `None`, tenant mismatch —
  is disabled, never enabled.
- If a caller wraps the resolver and a DB error propagates, the caller's failure
  mode is a request error (5xx), **not** a silent enable — because the resolver
  never returns `effective_enabled=True` except on an explicit, well-formed,
  tenant-matched `enabled=True` override or an explicit global flag `True`. A
  failed lookup that raised would abort before any positive decision. This is the
  correct deny-biased posture: errors fail closed by aborting, never by defaulting
  to enabled.
- No broad `except Exception: return disabled` is introduced (that would mask real
  faults). The safety comes from the precedence shape, not from swallowing errors.

## 8.15 Resolver remains unconsumed

No file in `apps/api/app/{feedback,scouting_requests,connectors,system}` is
modified. The three live enforcement points keep their direct
`get_settings().<flag>` checks unchanged. The only new imports of the resolver in
this batch are from its own test module. This is asserted by acceptance criteria
(§8.24 items 24–26) and by the inverted dark-state regression test (§8.21).

## 8.16 Caching decision

**No caching in this batch (recommended and adopted).** The resolver reads live
per call: at most one indexed single-row lookup. Rationale:
- An override change (a future set/clear) must be visible immediately; a stale
  cache that outlived a clear could keep a capability effectively enabled —
  directly violating the deny-biased posture.
- Dark code paths do not call the resolver at all in this batch, so there is no
  hot path to optimize yet.
- A future request-scoped memo (per-request only, never a process-global TTL) may
  be considered *only* if a wired gate later shows measured cost — and only
  deny-biased. Documented, not built (§8.28 Q4).

## 8.17 Logging & metrics

- **Logging.** The resolver is a pure read and is unconsumed in this batch, so it
  emits **no** structured log events by default. (Mutation logging —
  `capability_override_set/_cleared` via `log_event` — belongs to the override
  service, Phase 4A-C.3.) Introducing resolver-call logging now would add noise on
  a code path that nothing calls.
- **Metrics.** No new metric names or labels. Default backend stays `NoOpMetrics`;
  nothing is emitted by default or in tests. Any future counter must reuse an
  existing `METRIC_NAMES` entry and only `ALLOWED_LABELS` (bounded cardinality) —
  out of scope here.

## 8.18 Security model

- **Deny-biased default.** Absence / malformation / tenant-mismatch / unknown
  capability = disabled.
- **Ceiling is absolute.** Rule 1 runs first and cannot be overridden.
- **No customer path to enable.** The resolver reads only; it exposes no write and
  no route. No tenant role can influence its output beyond the (operator-only,
  future) override rows it reads.
- **Tenant isolation.** Reads are scoped by `(workspace_id, capability)` and
  tenant-validated against the passed `organization_id`; an override in one
  tenant/workspace is never effective in another.
- **Programmer-error surfacing.** A non-`Capability` argument is a bug and may
  raise; business state never raises.

## 8.19 Privacy model

- `CapabilityResolution` carries only bounded primitives (enums, booleans, the
  workspace id). No `reason` text, no actor id, no timestamps, no URLs, no
  payloads, no tokens, no raw errors are included — so the value is safe to render
  on an operator surface and safe to log if a future consumer ever chooses to.
- The resolver reads the override row's `enabled` and scope columns only; it never
  reads or returns the `reason` note or `set_by_user_id` attribution.

## 8.20 Testing strategy

New module `apps/api/app/tests/test_capability_resolver.py`, self-contained
(engine-scoped SQLite with `PRAGMA foreign_keys=ON`, mirroring
`test_workspace_capability_override_model.py`). Coverage:

- **Pure precedence units (`decide_capability`, no DB, clock-free):**
  - ceiling blocks an unknown/unregistered capability → `safety_ceiling`/disabled,
    even with a would-be override `enabled=True` and global flag `True`;
  - a well-formed override decides over a conflicting global flag
    (`enabled=True` with flag `False` → enabled/`workspace_override`;
    `enabled=False` with flag `True` → disabled/`workspace_override`);
  - global flag decides with no override (`True`→enabled, `False`→disabled,
    `global_configuration`);
  - secure-default branch is disabled/`secure_default` (defensive path).
- **DB-backed `resolve_capability`:**
  - happy-path enable via a seeded override row;
  - happy-path disable via a seeded `enabled=False` override even when the global
    flag is `True`;
  - no-row → falls through to the global flag;
  - tenant mismatch (override row org ≠ passed org) → treated as absent → global
    flag decides (deny-biased);
  - same capability in two workspaces resolves independently (isolation);
  - exactly one query issued per resolution (assert via a query counter or a
    single-row seed + behavior).
- **Dark-by-default regression:** with shipped defaults (all three flags `False`,
  zero override rows) every registered capability resolves disabled in a sample
  workspace; and every registry-bound flag is `False`.
- **Result-shape/secret-free:** `CapabilityResolution` is frozen (mutation
  raises), fields are exactly the documented set, `override_value is None` when
  `has_override is False`, and no field carries free-form/secret data.
- **Inverted dark-state guard:** update
  `test_workspace_capability_override_migration.py::
  test_no_resolver_module_shipped_in_this_batch` to its post-resolver form —
  assert `importlib.import_module("app.capabilities.resolver")` **succeeds** and
  that no live gate module imports it (grep-style import check, or an explicit
  assertion that `feedback/routes.py` et al. still call `get_settings()` directly).
  This is the single legitimate edit to an existing test forced by shipping the
  resolver.
- **Suite health:** full backend `pytest` green; `ruff` clean; single Alembic head
  `98289430a3ec`; `alembic check` clean; frontend suite untouched and green.

## 8.21 Contract & migration impact (none)

- **No migration.** Alembic head stays `98289430a3ec`; migration count stays 12.
  The resolver reads the existing table; it adds no column, index, or constraint.
- **No contract change.** No route or schema is added; `npm run gen:types`
  produces a zero diff on `openapi.json` + `schema.d.ts`. The customer
  `/system/capabilities` and operator `/internal/system/capabilities` surfaces are
  byte-unchanged.
- **No dependency change.** No new package; `requirements`/lockfiles untouched.
- **No flag change.** `core/config.py` untouched; all three flags remain `False`.

## 8.22 Implementation decomposition (4A-C.2.1–4A-C.2.5)

Delivered as one cohesive, reviewable batch (recommended, given the parts are
tightly coupled and all dark), but logically ordered as:

- **4A-C.2.1 — Decision types.** `DecisionSource` `StrEnum` +
  `CapabilityResolution` frozen dataclass in `resolver.py`. No logic yet.
- **4A-C.2.2 — Pure decision function.** `decide_capability(...)` +
  `_ceiling_blocks(...)`; the full §8.9 precedence, DB-free. Pure unit tests.
- **4A-C.2.3 — DB-backed resolver.** `resolve_capability(...)` +
  `_load_override(...)` (single indexed lookup) + tenant validation (§8.11).
  DB-backed tests.
- **4A-C.2.4 — Dark-state regression flip.** Invert the resolver-absence guard to
  the resolver-present-but-unconsumed guard; add the dark-by-default resolver
  regression.
- **4A-C.2.5 — Verification note.** Append the evidence note to
  `docs/verification/` (or extend the 4A-C.1 doc) — ruff, pytest counts, zero
  contract diff, single head, dark-state — and this plan's closeout.

## 8.23 File-level map

New:
- `apps/api/app/capabilities/resolver.py` — `DecisionSource`,
  `CapabilityResolution`, `resolve_capability`, `decide_capability`,
  `_ceiling_blocks`, `_load_override`, `__all__`.
- `apps/api/app/tests/test_capability_resolver.py` — precedence + DB-backed +
  dark-state + secret-free tests.

Touched (additive/minimal):
- `apps/api/app/tests/test_workspace_capability_override_migration.py` — invert
  the single `test_no_resolver_module_shipped_in_this_batch` guard (§8.20).
- `docs/verification/…` — a short 4A-C.2 evidence note (§8.22 4A-C.2.5).

**Explicitly NOT touched:** `feedback/routes.py`,
`scouting_requests/routes.py`, `scouting_requests/schedules.py`,
`connectors/registry.py`, `system/routes.py`,
`system/internal_routes.py`, `core/config.py`, `capabilities/registry.py`,
`capabilities/models.py`, `db/models.py`, any Alembic version file,
`openapi.json`, `schema.d.ts`, any dependency manifest, PR #34, any Dependabot PR.

## 8.24 Acceptance criteria (42 items)

Precedence & purity
1. [ ] A single resolver `resolve_capability(*, session, settings, capability,
   organization_id, workspace_id)` is the only place effective state is decided.
2. [ ] Precedence is exactly ceiling → override → global configuration → secure
   default, first decisive rule wins.
3. [ ] Rule 1 (safety ceiling) is evaluated first and cannot be overridden by any
   override or global flag.
4. [ ] An unknown/unregistered capability resolves `safety_ceiling`/disabled even
   with a would-be `enabled=True` override and a `True` global flag.
5. [ ] A well-formed override decides over a conflicting global flag
   (`workspace_override`).
6. [ ] The global flag decides when no honored override exists
   (`global_configuration`).
7. [ ] The secure-default branch yields disabled/`secure_default`.
8. [ ] Absence of data = disabled (missing/malformed/tenant-mismatched override →
   "no override", never enabled).
9. [ ] A workspace override may only narrow or match what the ceiling permits.
10. [ ] With all flags `False` and no rows, every capability in every workspace
    resolves disabled.
11. [ ] `decide_capability` is pure (no DB, deterministic) and is the unit-test
    surface, mirroring `is_job_stuck`.
12. [ ] `resolve_capability` performs at most one indexed override lookup.
13. [ ] The resolver never writes, mutates settings, or toggles any flag.
14. [ ] The resolver opens no session of its own; it uses the injected `Session`.

Types & provenance
15. [ ] `DecisionSource` is a bounded `StrEnum`
    (`safety_ceiling`/`workspace_override`/`global_configuration`/`secure_default`).
16. [ ] `CapabilityResolution` is `frozen=True, slots=True` and immutable
    (mutation raises).
17. [ ] Its fields are exactly `capability, workspace_id, effective_enabled,
    decided_by, global_flag, has_override, override_value`.
18. [ ] `override_value is None` iff `has_override is False`.
19. [ ] The result carries no free-form/secret data (no reason, actor, URL,
    payload, token, raw error).
20. [ ] `capability` compares/stores by `.value`; the resolver core accepts a
    typed `Capability`, not a raw string.

Tenant isolation & registry coupling
21. [ ] A cross-tenant override (row org ≠ passed org) is treated as absent
    (deny-biased fall-through).
22. [ ] An override for one `(workspace, capability)` never affects another
    workspace or capability.
23. [ ] The resolver derives the bound global flag from the registry
    (`policy.global_flag_attr`), never a hardcoded flag name.

Unconsumed & dark
24. [ ] No live gate (`feedback/routes.py`, `scouting_requests/routes.py`,
    `scouting_requests/schedules.py`, `connectors/registry.py`) imports or calls
    the resolver.
25. [ ] The three global flags remain `False`; `core/config.py` is unchanged.
26. [ ] The only non-test import of the resolver anywhere is none (it is
    unconsumed); its only importer is its own test module.
27. [ ] The dark-state regression is inverted to assert the resolver imports and
    remains unconsumed.

No-change guarantees
28. [ ] No new migration; Alembic head stays `98289430a3ec`; count stays 12.
29. [ ] `capabilities/models.py`, `registry.py`, `db/models.py` are unchanged.
30. [ ] No route/schema added; `npm run gen:types` yields zero diff.
31. [ ] Customer `/system/capabilities` and operator
    `/internal/system/capabilities` surfaces are byte-unchanged.
32. [ ] No dependency change; no lockfile change.
33. [ ] No frontend change; frontend suite untouched and green.
34. [ ] No caching layer or process-global memo is introduced.
35. [ ] No metric name/label added; default `NoOpMetrics`; nothing emitted in
    tests.
36. [ ] The resolver emits no log events by default (unconsumed pure read).

Quality gates
37. [ ] `ruff` clean across the new module and test.
38. [ ] Full backend `pytest` green (new resolver suite passing; existing suites
    unaffected except the single inverted guard).
39. [ ] `alembic check` clean; single head preserved.
40. [ ] All five required CI jobs pass on the exact tested head.
41. [ ] PR #34 and every Dependabot PR are untouched.
42. [ ] The batch is exact-merge-SHA verifiable (post-merge `push` CI green on the
    merge commit) before it is considered complete.

## 8.25 Rollback design

Purely additive: one new module + one new test + one inverted assertion + one doc
note. Reverting the merge removes all of it with no dependency from any other
subsystem (the resolver is unconsumed). No migration to reverse; no data written;
no flag flipped; no contract to regenerate. Rollback is a clean `git revert` of
the squash commit.

## 8.26 Risks & mitigations

- **R1 — Precedence bug enables a capability unintentionally.** Mitigation:
  ceiling-first evaluation; deny-biased fall-through; exhaustive pure precedence
  tests; resolver unconsumed; dark defaults proven by regression.
- **R2 — Tenant-mismatch honored as enable.** Mitigation: explicit in-memory
  org-scope validation (§8.11); deny-biased fall-through test; per-workspace
  isolation test across two tenants.
- **R3 — Accidental gate wiring slips in.** Mitigation: §8.23 names the files that
  must NOT change; acceptance items 24–27; inverted dark-state guard asserts
  non-consumption.
- **R4 — Contract/migration drift.** Mitigation: no route/schema/migration added;
  zero-diff `gen:types`; `alembic check`; single-head assertion unchanged at
  `98289430a3ec`.
- **R5 — A cache masks a future clear.** Mitigation: no caching adopted (§8.16);
  live per-call read.
- **R6 — Storable set drifts from resolvable set.** Mitigation: the resolver
  derives the flag binding and the known-capability check from the single registry
  source; unknown capability → `safety_ceiling`/disabled.
- **R7 — Error swallowed into a silent enable.** Mitigation: no broad
  `except`-to-disabled; safety comes from precedence shape; errors fail closed by
  aborting, never by defaulting to enabled (§8.14).

## 8.27 Open decisions

- **Q1 — Final `DecisionSource` naming.** This plan uses
  `global_configuration`/`secure_default`; the merged 4A-C plan drafted
  `global_flag`/`default_disabled`. Semantics identical; final spelling settled at
  review. Default: adopt this plan's clearer names.
- **Q2 — Broader environment safety ceiling.** Should rule 1 also block on an
  "activation-prohibited" environment/config signal? Default: **not now** — the
  ceiling blocks only unknown/unregistered capabilities in this batch; the rule-1
  slot is reserved so an environment signal can be added later without reordering
  precedence.
- **Q3 — `str` vs `UUID` in the signature.** The repo persists ids as
  `String(32)` hex `str`. Default: keep `str` to match columns and the rest of the
  codebase; treat any "UUID" phrasing as conceptual.
- **Q4 — Request-scoped memo.** Add later only if a wired gate shows measured
  cost, and only per-request/deny-biased. Default: no memo now.
- **Q5 — Batch shape.** One cohesive PR vs five sub-batches. Default: one cohesive
  PR (parts tightly coupled, all dark), decomposition §8.22 as the internal
  ordering.

## 8.28 Entry criteria (for the implementation batch)

The 4A-C.2 implementation batch may begin only when all hold:
- Phase 4A-C.1 is merged and exact-merge-SHA verified (met: PR #70 at
  `47852c7`, head `98289430a3ec`, all dark).
- The registry and override model are present and unchanged.
- This plan is approved.
- The batch stays additive + read-only + unconsumed, with no flag/contract/
  migration/dependency change.

## 8.29 Definition of done (4A-C.2)

Done when: `resolver.py` (`DecisionSource`, `CapabilityResolution`,
`resolve_capability`, `decide_capability`) is merged and exact-merge-SHA verified;
the 42 acceptance criteria pass; the resolver is exhaustively unit-tested and
consumed by nothing but its own tests; the dark-state guard is inverted to assert
the resolver imports yet stays unwired; a single Alembic head `98289430a3ec` is
preserved with a zero-diff contract; all three global flags remain `False`; and
**no capability has been enabled for any customer**. This batch authorizes no gate
wiring, no override service, no operator API, and no flag activation — those are
later, separately-approved batches (4A-C.3, 4A-C.4) and Phase 4B.

## 8.30 Relationship to the merged 4A-C plan

`docs/phase-4a-c-plan.md` §8.25 originally grouped the resolver with the registry
under "4A-C.1" and the model+migration under "4A-C.2". The delivered 4A-C.1 batch
(PR #70) instead shipped **registry + model + migration** and *excluded* the
resolver (recorded in `docs/verification/4a-c-1-capability-foundation.md`). This
plan therefore labels the **resolver-only** batch as **4A-C.2**, with the override
service and operator API following as **4A-C.3** and **4A-C.4** — a strict
renumbering that changes no approved scope: every part still ships dark, the
resolver still ships unconsumed (merged plan §8.16), and no gate is wired and no
flag is flipped until an explicit later batch and Phase 4B.

---

**Status:** `PLANNING — DOCUMENTATION ONLY — RESOLVER NOT IMPLEMENTED —
PERSISTENCE UNUSED — ALL CAPABILITIES REMAIN DARK`
