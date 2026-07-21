# Phase 4A-C — Governed Per-Workspace Capability Override Foundation

**Status:** `PLANNING — DOCUMENTATION ONLY — IMPLEMENTATION NOT STARTED — ALL CAPABILITIES REMAIN DARK`

> This is a planning and design document. It authorizes no code, no migration, no
> contract change, no dependency change, and no feature-flag activation. Phase 4A-C,
> when implemented under this plan across separate approved branches, ships **dark**:
> it turns nothing on for any customer. Its only new customer-observable behavior is
> *none* — the override store defaults to empty, the resolver resolves to `disabled`
> for every `(capability, workspace)` while the three global flags stay `False`, and
> the resolver is **not** yet wired into the live feature gates. The first real
> activation of any dark capability is deferred to Phase 4B and is a separate,
> explicitly-approved decision.

> **Progress note (2026-07-20).** Delivery has since begun under this plan. The
> **4A-C.1** batch (registry + override model + additive migration, PR #70, merged
> at `47852c7fbd3861011bf2bcfd79719d8ea9a8c6c7`, Alembic head `98289430a3ec`;
> evidence in `docs/verification/4a-c-1-capability-foundation.md`) shipped the
> storage-and-type-safety plane **excluding** the resolver. The next batch, the
> resolver itself, is planned separately as **4A-C.2** in
> `docs/phase-4a-c-2-plan.md`; the override service and operator API follow as
> 4A-C.3/4A-C.4. This renumbering (recorded in the 4A-C.1 verification doc and
> §8.30 of the 4A-C.2 plan) changes no approved scope: persistence remains
> unconsumed, no gate is wired, and all three global flags remain `False` — every
> capability stays dark. The baseline SHAs and revision numbers recorded below are
> the *historical* baseline of **this** planning document and are left unchanged.

Baseline for this plan: `main` at `e897a91e717509cc022e7cf4f9baf72993ef1980`
(Phase 4A-B operator-observability closeout, PR #68), single Alembic head
`4945b98229e6` (11 migration files), and the three product flags all `False`:
`connector_rss_enabled`, `scout_scheduling_enabled`, `opportunity_feedback_enabled`
(`apps/api/app/core/config.py:220,235,242`). PR #34
(`feat/phase-3b-live-rss-controlled-egress`) remains open/draft and untouched.

This batch is the successor to the merged Phase 4A-B (backend read-only operator
observability). Relative to the parent plan's original batch sketch
(`docs/phase-4a-plan.md` §8.19), the executed decomposition consolidated the
resolver + override model + service + migration + operator management API into this
single **4A-C** foundation batch; the operator **frontend** view is deferred to a
later **4A-D** batch, and the first activation is **Phase 4B**.

---

## 8.1 Title and status

- **Phase:** 4A, batch 4A-C — Governed Per-Workspace Capability Override Foundation.
- **Theme:** "Controlled Activation and Operability." Build the *storage* and
  *resolution* plane that lets a future activation be scoped to a single workspace
  instead of flipping a global switch for every tenant at once — then stop.
- **Nature:** additive backend only — one centralized capability resolver, one
  additive `workspace_capability_overrides` table + migration, an operator-only
  override management service + API (set/clear/read), audit integration, additive
  operator-only response schemas, a contract regeneration (additive), backend tests,
  and this doc. **No** enablement, **no** wiring of the resolver into any live
  feature gate, **no** frontend, **no** flag flip.
- **Explicitly out of scope:** enabling feedback, scheduling, or RSS; any customer
  activation; any change to Phase 3 / Phase 4A-B behavior; any operator frontend;
  any consumption of the resolver by the feedback/scheduling/RSS gates.
- **Status:** `PLANNING — DOCUMENTATION ONLY`.

## 8.2 Executive summary

Phase 4A-B gave operators read-only visibility over the durable-job queue, worker
fleet, and scouting schedules (`apps/api/app/system/internal_observability_routes.py`,
merged PR #68). The documented next prerequisite before any dark capability is ever
turned on is a **governed per-workspace override plane**: an operator must be able to
record an *intent* to enable a specific capability for a specific workspace, durably
and auditably, and the platform must have a *single* authoritative function that
resolves the effective state of any `(capability, workspace)` under a strict,
deny-biased precedence — so that a future Phase 4B activation can be scoped to one
internal workspace rather than becoming a global switch.

Phase 4A-C builds exactly that foundation and nothing more. It:

1. Introduces a **centralized capability resolver** (`resolve_capability`) — a pure,
   read-mostly function returning both the effective boolean and the *deciding rule*.
2. Introduces the additive **`workspace_capability_overrides`** table (deny-biased,
   one row per `(workspace, capability)`), plus an operator-only service to
   set/clear an override, every mutation audited via the existing `record_audit`.
3. Exposes an **operator-only management API** under the established
   `/internal/system/*` tier to set, clear, and read overrides and effective state.

Everything ships dark. The override store defaults empty; with all three global flags
`False` and no override rows, every `(capability, workspace)` resolves to **disabled**
through the deny-biased default. Critically, in 4A-C the resolver is **introduced and
unit-tested but not yet consulted by any live gate** — the feedback/scheduling/RSS
enforcement paths are left exactly as they are today. Wiring the gates to consult the
resolver, and any actual enablement, are later, separately-approved batches. Phase
4A-C changes the *control-plane storage and resolution*, not any *runtime behavior*.

## 8.3 Goals

- **G1.** A single authoritative resolver `resolve_capability(capability, *, workspace_id,
  organization_id, settings, db, now)` that returns `(effective_enabled: bool,
  decided_by: Decision)` for any supported capability and workspace.
- **G2.** A deny-biased precedence chain that can *never* enable a capability that a
  higher safety/environment ceiling prohibits, and that treats absent/malformed/unknown
  data as disabled.
- **G3.** An additive `workspace_capability_overrides` table with a unique
  `(workspace_id, capability)` constraint, matching the repo's existing model/migration
  conventions, that round-trips and preserves a single Alembic head.
- **G4.** An operator-only, fully audited service + API to set / clear / read overrides
  and read effective per-workspace state — the *only* new write path, and one that
  never flips a global flag.
- **G5.** Keep everything dark and additive: no live gate consults the resolver yet, no
  behavior change, no flag flipped, no destructive migration, and no contract drift
  beyond additive operator-only fields.

## 8.4 Non-goals

- **N1.** Enabling opportunity feedback, scout scheduling, or the RSS connector. That is
  Phase 4B and beyond, under separate approval.
- **N2.** Wiring the resolver into any live feature gate (`feedback/routes.py:63`,
  `scouting_requests/routes.py:75`, `scouting_requests/schedules.py:284/392/499`,
  `connectors/registry.py:26`). Gate consumption is designed here (§8.16) but
  **deferred** to a later batch.
- **N3.** Any customer-facing activation UI, self-serve toggle, or operator frontend.
  The 4A-C surface is backend-only; the operator frontend is a later 4A-D batch.
- **N4.** Any TTL / expiry / retention / purge worker for overrides or audit rows.
  Overrides are cleared explicitly (§8.28 Q3).
- **N5.** Changing the customer `/system/capabilities` contract *shape*
  (`system/routes.py:87`), Phase 3 semantics, the durable-job lease/fencing model, the
  schedule recurrence math, or the Phase 4A-B observability routes.
- **N6.** Touching PR #34 (`feat/phase-3b-live-rss-controlled-egress`), any Dependabot PR
  (#6, #26, #27, #28, #63–#66), or any dependency version.
- **N7.** A real metrics exporter. `core/metrics.py` stays no-op by default; 4A-C emits
  only bounded, allow-listed counters/logs if anything at all.

## 8.5 Current-state capability flow (real references)

**Global flags (the only gate today).** `apps/api/app/core/config.py` — three
`bool = False` master switches: `connector_rss_enabled` (`:220`),
`scout_scheduling_enabled` (`:235`), `opportunity_feedback_enabled` (`:242`). `Settings`
is a `pydantic-settings` `BaseSettings`; the process-wide accessor is
`get_settings()` guarded by `@lru_cache` (`config.py:526`). It is invoked **directly**
as a function throughout the codebase (never via FastAPI `Depends`), e.g.
`get_settings().opportunity_feedback_enabled`.

**Three distinct enforcement shapes (important — the resolver must eventually unify
these, §8.16):**

- **Opportunity feedback — route guard on read *and* write.**
  `apps/api/app/feedback/routes.py:63` `_require_feedback_feature()` raises a 503
  (`CapabilityUnavailableError`) when the flag is off; applied to both
  `submit_opportunity_feedback` (`:110`) and `list_opportunity_feedback` (`:142`). The
  service layer `apps/api/app/feedback/service.py::create_feedback` has **no** embedded
  flag check — it assumes the route already gated.
- **Scout scheduling — route guard on mutations + inert worker.**
  `apps/api/app/scouting_requests/routes.py:75` `_require_scheduling_feature()` → 503 on
  `create`/`pause`/`resume`/`delete` (`:389/411/435/454`). The tick handler
  `apps/api/app/scouting_requests/schedules.py:284` self-terminates as a no-op when off;
  `:392` and `:499` only enqueue a tick when on. Schedule **reads** are not gated.
- **RSS connector — service-layer selection gate (no route guard).**
  `apps/api/app/connectors/registry.py:26` `_rss_policy()` wraps the flag into a
  `ConnectorPolicy`; when off, `resolve_connector()` returns `None` and the sandbox
  fixture connector stays authoritative.

**Capability disclosure (two tiers, already established).**
- Customer: `apps/api/app/system/routes.py:87` `system_capabilities` → `RuntimeSummaryOut`
  whose `features` is a coarse `FeatureFlagsOut` (`:40`) reflecting only
  `opportunity_feedback_enabled` (`:95`). This is a *global* reflection with no
  per-workspace notion.
- Operator: `apps/api/app/system/internal_routes.py:87` `internal_capabilities` →
  `CapabilitiesOut` (`:43`) — backend topology, operator-gated (`require_operator`).

**There is currently no per-workspace override anywhere** — a flag is a single global
boolean, and every workspace sees the same value.

## 8.6 Proposed architecture (Phase 4A-C)

Three thin, additive layers, backend-only:

1. **Capability registry + resolver** — a new package `apps/api/app/capabilities/`
   with `registry.py` (the closed set of governable capabilities and how each maps to
   its global flag) and `resolver.py` (the pure `resolve_capability(...)` returning the
   effective boolean + `Decision`). The resolver reads the global flag from `Settings`
   and the optional override row; it never mutates any feature behavior.
2. **Override model + service** — `apps/api/app/capabilities/models.py`
   (`WorkspaceCapabilityOverride`) + a new additive migration, and
   `apps/api/app/capabilities/overrides.py` (operator-only set/clear, each audited via
   `record_audit`).
3. **Operator management API** — new operator-only routes composing the resolver +
   override service under the existing `/internal/system/*` prefix, registered in
   `apps/api/app/api/router.py`.

The customer `/system/capabilities` shape is unchanged (§8.15). No live gate consults
the resolver in 4A-C (§8.16). Because every override defaults absent and every global
flag is `False`, the effective state of every capability in every workspace stays
`disabled`.

## 8.7 Centralized capability resolver

A single pure module `apps/api/app/capabilities/resolver.py` is the *only* place that
decides a capability's effective state, mirroring how Phase 4A-B centralized "stuck"
in `apps/api/app/jobs/stuck.py` (one predicate as the single source of truth).

Proposed signature:

```
def resolve_capability(
    capability: Capability,
    *,
    workspace_id: str,
    organization_id: str,
    settings: Settings,
    db: Session,
    now: datetime,
) -> CapabilityResolution: ...
```

- `Capability` is a `StrEnum` from the registry (§8.9), stored/compared by `.value`.
- `CapabilityResolution` is an immutable dataclass/pydantic value:
  `{capability, workspace_id, effective_enabled, decided_by, global_flag, has_override,
  override_value}`. `decided_by` is a `Decision` `StrEnum`
  (`safety_ceiling` / `workspace_override` / `global_flag` / `default_disabled`) so the
  operator surface can always explain *why*.
- **Purity / testability.** The resolver takes an injected `now` and an explicit
  `settings` + `db`, so it is deterministic and unit-testable exactly like `is_job_stuck`.
  It performs at most one indexed lookup (`(workspace_id, capability)`); a batch variant
  `resolve_all_for_workspace(...)` issues one query for the workspace's override rows and
  resolves each registered capability against it.
- **No side effects.** The resolver never writes, never mutates settings, and never
  toggles any global flag. It is read-only.

## 8.8 Resolution precedence (deny-biased)

Evaluated top-down against injected `now`; the first decisive rule wins:

1. **Safety / environment ceiling (hard ceiling).** If a higher-level restriction
   prohibits the capability (see §8.28 Q1 for the concrete ceiling inputs — default
   assumption: an unknown/unregistered capability, or an environment explicitly marked
   as "activation-prohibited"), the result is **disabled**, `decided_by=safety_ceiling`,
   regardless of any override. **An override can never raise a capability above this
   ceiling.**
2. **Explicit valid workspace override.** If a well-formed override row exists for
   `(workspace_id, capability)`, its `enabled` boolean decides — *subject to* rule 1 —
   with `decided_by=workspace_override`.
3. **Global config flag.** Otherwise the global `*_enabled` setting decides, with
   `decided_by=global_flag`.
4. **Hardcoded disabled default.** If nothing above is decisive (should be unreachable
   given rule 3 always decides once reached), the result is **disabled**,
   `decided_by=default_disabled`.

**Invariants (must be enforced and tested):**
- Absence of data = disabled. A missing / malformed / unknown-capability override is
  treated as "no override," never "enabled."
- A workspace override may only *narrow or match* what the ceiling permits.
- Per-workspace isolation: an override for one `(workspace, capability)` never affects
  another workspace or another capability (unique row, tenant-scoped read).
- With all global flags `False` and no override rows, every capability in every
  workspace resolves `disabled` via rule 3 (`global_flag` → false) — the dark default.

## 8.9 Supported capability registry

A closed registry `apps/api/app/capabilities/registry.py` enumerates the *only*
capabilities that may be governed, each bound to its global flag and (eventually) its
enforcement point. This is deliberately a small, explicit allow-list — an unrecognized
capability name hits rule 1 (`safety_ceiling` → disabled), never an override.

Proposed `Capability` `StrEnum` values and their global-flag bindings:

| `Capability` value        | Global flag (`Settings`)          | Enforcement shape today (§8.5)        |
| ------------------------- | --------------------------------- | ------------------------------------- |
| `opportunity_feedback`    | `opportunity_feedback_enabled`    | route guard (read+write), `feedback/routes.py:63` |
| `scout_scheduling`        | `scout_scheduling_enabled`        | route guard (mutations) + inert worker, `scouting_requests/routes.py:75`, `schedules.py:284` |
| `connector_rss`           | `connector_rss_enabled`           | service-layer selection, `connectors/registry.py:26` |

Registry entries carry: the enum value, a human-safe label, the `Settings` attribute
name for the global flag, and a stable string persisted in the `capability` column. The
registry is the single source that both the resolver and the migration's
`CheckConstraint` (§8.10) derive from, so the storable set and the resolvable set can
never drift. Values are stored as `String` by `.value` (repo convention — no native PG
enum, portable to SQLite; see `feedback/models.py:94`, `jobs/models.py:99`).

## 8.10 Database design — `workspace_capability_overrides`

One additive table, chained off the current head, following the newest template
(`apps/api/alembic/versions/20260718_1144-4945b98229e6_add_opportunity_feedback.py`) and
the base mixins in `apps/api/app/db/base.py` (`UUIDPrimaryKeyMixin` → `id String(32)`
default `new_uuid`; `TimestampMixin` → `created_at`/`updated_at DateTime(timezone=True)`
server-default now).

Model `apps/api/app/capabilities/models.py::WorkspaceCapabilityOverride`
(`__tablename__ = "workspace_capability_overrides"`):

| Column             | Type / constraint                                             | Notes |
| ------------------ | ------------------------------------------------------------- | ----- |
| `id`               | `String(32)` PK (`UUIDPrimaryKeyMixin`)                       | uuid4 hex |
| `organization_id`  | `String(32)`, FK `organizations.id` CASCADE, indexed, not null | tenant scope |
| `workspace_id`     | `String(32)`, FK `workspaces.id` CASCADE, indexed, not null   | tenant scope |
| `capability`       | `String(64)`, not null                                        | registry `.value`; `CheckConstraint` to the closed set |
| `enabled`          | `Boolean`, not null                                           | the override value |
| `set_by_user_id`   | `String(32)`, FK `users.id` **SET NULL**, indexed, nullable   | preserve history if user removed (mirrors `feedback.submitted_by_user_id`) |
| `reason`           | `Text`, nullable                                              | optional operator note (bounded, non-secret) |
| `created_at`       | `DateTime(tz)`, server default (`TimestampMixin`)             | |
| `updated_at`       | `DateTime(tz)`, server default (`TimestampMixin`)             | bumped on re-set (upsert) |

Constraints & indexes (declared in `__table_args__`, rendered in the migration with
explicit names, `op.batch_alter_table` for indexes per repo convention):
- **Unique** `uq_workspace_capability_override` on `(workspace_id, capability)` — exactly
  one override per capability per workspace (enables idempotent upsert semantics, §8.11).
- **CheckConstraint** `ck_workspace_capability_override_capability` restricting
  `capability` to the registry's `.value` set (portable SQL `IN (...)`, mirroring the
  polarity check in the feedback migration).
- Auto-named FK indexes `ix_workspace_capability_overrides_organization_id`,
  `ix_workspace_capability_overrides_workspace_id`,
  `ix_workspace_capability_overrides_set_by_user_id` via `batch_op.f(...)`.

No backfill: existing workspaces get zero rows (absence = dark default).

## 8.11 Mutation semantics (set / clear)

`apps/api/app/capabilities/overrides.py`, operator-only, called only from the
operator-gated routes (§8.14):

- **`set_override(db, *, organization_id, workspace_id, capability, enabled, actor_user_id,
  reason=None)`** — idempotent upsert keyed by the unique `(workspace_id, capability)`
  constraint: insert if absent, else update `enabled`/`reason`/`updated_at` in place.
  Validates `capability` against the registry (unknown → 422, never a stored row).
  Validates the workspace belongs to `organization_id` (tenant-consistency; 404 on
  mismatch). Writes one audit row (§8.13). Setting `enabled=True` records the *intent*
  only — it does **not** flip any global flag and, in 4A-C, does not change any live
  gate's decision (§8.16).
- **`clear_override(db, *, organization_id, workspace_id, capability, actor_user_id,
  reason=None)`** — deletes the row if present; a clear on an absent override is a
  no-op success (idempotent). Writes one audit row capturing the prior value.
- **Read** helpers return `CapabilityResolution` records via the resolver, never raw
  rows, so every read is filtered through the deny-biased precedence.

All writes flow through the request's `Session` and rely on the caller's transaction
boundary (`record_audit` uses `db.flush()`, `audit/service.py:37`), so the override row
and its audit entry commit atomically.

## 8.12 Concurrency model

- **Upsert races.** The unique `(workspace_id, capability)` constraint is the
  serialization point. Two concurrent `set_override` calls resolve to one row: the
  loser catches the integrity error and retries as an update (read-modify-write under the
  unique key), or uses an `INSERT ... ON CONFLICT DO UPDATE` where the dialect supports it
  with a portable fallback for SQLite tests. Last write wins deterministically on
  `updated_at`.
- **Set/clear races.** A concurrent clear + set converge to a single terminal state (row
  present or absent); the audit log preserves the full ordered history regardless of the
  converged row, so no mutation is lost from the record.
- **Read/write consistency.** The resolver reads within the caller's transaction/session,
  so a read issued after a committed write in the same request observes it. Cross-request
  visibility is immediate (no caching in 4A-C — §8.17).
- **No long locks.** All operations are single-row, indexed, and bounded; no table scan,
  no advisory lock, no worker coordination.

## 8.13 Audit model

Every mutation writes exactly one `AuditLog` row through the existing
`apps/api/app/audit/service.py::record_audit(...)` (the same mechanism the schedule and
feedback services use, e.g. `feedback/service.py:126`). Fields:

- `action` ∈ {`capability_override.set`, `capability_override.cleared`} (dotted taxonomy,
  matching `opportunity_feedback.created` / `scout_schedule.*`).
- `entity_type="workspace_capability_override"`, `entity_id`=row id.
- `organization_id` / `workspace_id` = the target scope; `actor_user_id` = the operator.
- `previous_state` / `new_state` = `{capability, enabled}` snapshots (JSON), so the
  transition is fully reconstructable; `reason` carried through when supplied.

Read endpoints write no audit rows. Because `record_audit` flushes within the caller's
session, the audit row is atomic with the override mutation.

## 8.14 API design (operator-only, additive)

New operator-gated routes (`Depends(require_operator)`; `auth/dependencies.py:57`) in a
new module `apps/api/app/system/internal_capabilities_routes.py`, registered in
`apps/api/app/api/router.py` after the existing internal routers (mirroring how
`internal_observability_router` was added in 4A-B). All read-only responses use additive,
secret-free operator schemas (bounded enums, booleans, safe ids — never a URL,
credential, payload, or token), consistent with `TelemetryStatusOut` / `JobOperatorOut`.

Proposed routes under `/internal/system`:

```
GET    /internal/system/capabilities/registry                 -> the governable capability set
GET    /internal/system/capabilities/effective                -> effective state per (capability, workspace)
                                                                  (filterable by workspace_id / capability)
GET    /internal/system/capabilities/overrides                -> operator listing of stored override rows
PUT    /internal/system/capabilities/overrides                -> set/upsert one override (audited)
DELETE /internal/system/capabilities/overrides                -> clear one override (audited)
```

- **PUT / DELETE are the only new write paths.** They write/delete an override *row*;
  they never flip a global flag, and in 4A-C they never change any live gate decision.
- **Route ordering / bounds.** Static sub-paths precede any parametric path; list reads
  clamp `limit`/`offset` via FastAPI `Query` (out-of-range → 422), matching 4A-B.
- **Validation.** Unknown `capability` → 422 (registry-checked); workspace/org mismatch
  → 404; anonymous → 401; authenticated non-operator → 403.
- **Response schemas.** New Pydantic models (`CapabilityRegistryOut`,
  `CapabilityEffectiveOut` = the `CapabilityResolution` projection, `CapabilityOverrideOut`,
  `CapabilityOverridePageOut`) declared alongside the routes.

## 8.15 System-capabilities integration (disclosure)

- **Customer `/system/capabilities` (unchanged in shape).** `system/routes.py:87`
  continues to return `RuntimeSummaryOut.features = FeatureFlagsOut` reflecting the
  *global* flags only. It is **not** extended with per-workspace effective values in
  4A-C (that would be a customer-contract change; deferred). While everything is dark it
  stays `false` everywhere, so no customer-visible drift occurs.
- **Operator `/internal/system/capabilities` (unchanged).** The existing `CapabilitiesOut`
  topology read (`internal_routes.py:87`) is left as-is; the new *effective-state* read
  is the dedicated `/internal/system/capabilities/effective` route (§8.14), keeping
  topology and activation-state concerns separate.
- **Contract regeneration.** `npm run gen:types` (`scripts/gen-types.sh`) regenerates
  `apps/api/openapi.json` + `apps/web/src/api/schema.d.ts` **additively** (new operator
  paths + schemas only; zero removals; idempotent second run). The customer
  `FeatureFlagsOut`/`RuntimeSummaryOut` shapes are byte-unchanged.

## 8.16 Route / worker gate integration (designed, deferred)

This is the most safety-critical boundary of 4A-C, and it is deliberately **not crossed
here**. The three live enforcement points (§8.5) are left exactly as they are; none
imports or calls the resolver in 4A-C.

The *design* for a future batch (documented now so the resolver's shape is right):

- **Feedback** (`feedback/routes.py:63`): replace the direct
  `get_settings().opportunity_feedback_enabled` check with
  `resolve_capability(Capability.opportunity_feedback, workspace_id=..., ...).effective_enabled`.
  Because the deny-biased chain returns the global flag when no override exists, behavior
  is *identical* while dark, and becomes per-workspace once an override is set and the
  gate is wired.
- **Scheduling** (`scouting_requests/routes.py:75` + worker `schedules.py:284/392/499`):
  same substitution at the route guard and the tick gate, resolved against the schedule's
  owning workspace.
- **RSS** (`connectors/registry.py:26`): resolve against the scout-request's workspace
  when selecting a connector.

Wiring these — and any activation — is a later, separately-approved batch and/or Phase
4B. 4A-C ships the resolver *unconsumed* so it can be exhaustively tested in isolation
before it ever gates a real request.

## 8.17 Caching strategy

- **No caching in 4A-C.** The resolver reads live per call. Override changes are visible
  immediately across requests; there is no cache to invalidate, no staleness window, and
  no risk of a cached "enabled" outliving a clear. This matches the deny-biased safety
  posture (a stale cache must never keep a capability enabled after it is cleared).
- **Cost.** At most one indexed single-row lookup per resolution, or one indexed
  per-workspace query for the batch variant — negligible, and dark code paths do not call
  the resolver at all in 4A-C.
- **Future option (documented, not built).** If a hot gate path later needs it, a small
  request-scoped memo (per-request, never cross-request) may be added — but only
  deny-biased and per-request, never a process-global TTL cache that could mask a clear.

## 8.18 Security & privacy model

- **Deny-biased default.** Absence / malformed / unknown data = disabled (§8.8).
- **No customer path to enable.** Overrides are set only via operator-gated
  (`require_operator`) audited routes. No tenant role (`_ROLE_RANK` up to OWNER) can set
  or read an override; the operator management API is a separate tier.
- **Ceiling is absolute.** Rule 1 is evaluated first and cannot be overridden.
- **Tenant isolation.** Override reads/writes are scoped by `organization_id` +
  `workspace_id` with FK integrity; an override in one tenant/workspace is never
  observable or effective in another (unique per-workspace row, tenant-scoped query),
  matching the isolation discipline verified in 4A-B and the feedback suite.
- **Secret-free surfaces.** Operator responses carry bounded enums, booleans, safe ids,
  and the optional non-secret `reason` note only — never URLs, credentials, payloads,
  tokens, trace context, or raw errors, matching `TelemetryStatusOut`/`JobOperatorOut`.
- **Audit completeness.** Every set/clear is attributable (actor + previous/new state).

## 8.19 Metrics & structured logging

- **Logging.** Each mutation emits one bounded structured event via
  `apps/api/app/core/logging.py::log_event(...)` (as `feedback/service.py:135` does),
  e.g. `capability_override_set` / `capability_override_cleared` with
  `outcome`, `workspace_id`, and `capability` — never secrets or free-form payloads.
- **Metrics.** Default backend stays `NoOpMetrics` (`core/metrics.py`); nothing is
  emitted by default or in tests. If any counter is added it must reuse an existing
  `METRIC_NAMES` entry and only `ALLOWED_LABELS` (bounded cardinality — `operation`,
  `outcome`, etc.; never ids/paths). Introducing a new metric name/label beyond that
  bounded policy is out of scope.
- **Correlation.** Mutations run inside the existing `CorrelationMiddleware`
  (`core/middleware.py:82`), so request/trace ids are already bound; no new correlation
  plumbing is added.

## 8.20 Migration plan

- **One additive migration**, single new head chained off `4945b98229e6`
  (`down_revision = '4945b98229e6'`), named per convention
  `YYYYMMDD_HHMM-<rev>_add_workspace_capability_overrides.py` under
  `apps/api/alembic/versions/`.
- **Structure.** `op.create_table(...)` with `sa.Column` entries (String(32) ids,
  String(64) `capability`, `Boolean` `enabled`, `Text` `reason`, `DateTime(timezone=True)`
  timestamps with `server_default=sa.text('(CURRENT_TIMESTAMP)')`), a
  `sa.ForeignKeyConstraint(...)` per FK with explicit `ondelete` (CASCADE for org/ws,
  SET NULL for user), `sa.PrimaryKeyConstraint('id')`,
  `sa.UniqueConstraint('workspace_id','capability', name='uq_workspace_capability_override')`,
  a `sa.CheckConstraint(...)` for the capability allow-list; indexes created inside
  `with op.batch_alter_table(...) as batch_op` using `batch_op.f('ix_...')`.
- **Downgrade** drops the indexes (reverse order) then the table. No other subsystem
  reads the table, so downgrade loses no other data.
- **Guarantees.** Migration must round-trip (`upgrade`→`downgrade`→`upgrade`), keep a
  **single** head, and pass `alembic check` with no autogen drift.

## 8.21 Testing strategy

Backend, self-contained SQLite (`get_db` overridden, `PRAGMA foreign_keys=ON`, direct
row inserts), operator vs non-operator users via `create_access_token`, following the
4A-B and feedback suites:

- **Resolver precedence units (pure, clock-injected):** all four rules; ceiling beats a
  conflicting override; a valid override beats the global flag; global flag decides with
  no override; unknown capability → `safety_ceiling`/disabled; malformed/absent override
  treated as disabled; with all flags `False` + no rows every capability resolves
  `default`/`global_flag` → disabled.
- **Override service:** set inserts, re-set upserts (unique constraint, `updated_at`
  bump), clear deletes, clear-absent is idempotent success; unknown capability → 422;
  org/workspace mismatch → 404; each mutation writes exactly one `AuditLog` with correct
  `previous_state`/`new_state`/`actor_user_id` (`select(AuditLog)` assertion).
- **Four-market isolation (Dallas / London / Lagos / Nairobi):** an override in one
  workspace never changes another market's effective state; reads are tenant-scoped;
  cross-tenant access → 404.
- **Operator authorization:** every new route → 401 anonymous, 403 non-operator.
- **Dark-by-default:** with shipped defaults the effective-state read reports every
  capability disabled in every workspace, and no global flag is enabled.
- **Concurrency:** simulated upsert race converges to one row + full audit history.
- **Contract:** `npm run gen:types` yields only additive operator-only schema changes,
  idempotent second run, customer `/system/capabilities` shape byte-unchanged.
- Full backend suite + frontend suite stay green; single Alembic head; `alembic check`
  clean; `ruff` clean.

## 8.22 Rollback design

- **Code rollback.** The batch is purely additive (new package, new routes, new schemas,
  new migration, new tests); reverting the merge removes all of it with no dependency on
  it from any existing path (the resolver is unconsumed by live gates — §8.16), so revert
  is clean.
- **Data rollback.** The Alembic `downgrade` drops `workspace_capability_overrides` and
  its indexes; no other table references it, so the drop is safe and self-contained.
- **Operational rollback.** Because nothing consumes the resolver and no flag is flipped,
  there is no runtime state to unwind — clearing all override rows (or downgrading)
  returns the system to the exact pre-4A-C state, which is already the customer-visible
  state (all dark).
- **Safety.** No destructive change to any existing table/column; a single head is
  preserved so rollback never strands a divergent migration graph.

## 8.23 Rollout plan

Phase 4A-C rolls out **nothing** to customers. Its "rollout" is making the override
storage + resolver + operator management API available. No global flag is flipped, no
live gate consults the resolver, and the operator surface is operator-only. At the end
of 4A-C the platform is in exactly the same *customer* state as after 4A-B — all
capabilities dark — but operators can now durably and auditably record a per-workspace
override *intent*, and the platform has one authoritative resolver ready to be wired in
and exercised in Phase 4B.

## 8.24 Acceptance criteria (binary checklist)

1. [ ] `resolve_capability(...)` returns `(effective_enabled, decided_by)` for any
   registered `(capability, workspace)`.
2. [ ] Precedence is exactly: safety ceiling → workspace override → global flag →
   disabled default; first decisive rule wins.
3. [ ] A workspace override can never enable a capability the safety ceiling prohibits
   (tested).
4. [ ] An unknown/unregistered capability resolves `disabled` via `safety_ceiling`
   (never stored, never enabled).
5. [ ] A malformed/absent override is treated as "no override," never "enabled."
6. [ ] With shipped defaults (all flags `False`, no rows) every capability in every
   workspace resolves disabled.
7. [ ] `Capability` and `Decision` are `StrEnum`s stored/compared by `.value`.
8. [ ] The registry is the single source binding each capability to its global flag and
   its persisted string; resolver + migration `CheckConstraint` derive from it.
9. [ ] Additive table `workspace_capability_overrides` exists with the documented columns.
10. [ ] Unique `(workspace_id, capability)` constraint enforces one override per
    capability per workspace.
11. [ ] `CheckConstraint` restricts `capability` to the registry allow-list.
12. [ ] FK `ondelete` policies match spec (CASCADE org/ws, SET NULL user).
13. [ ] Migration round-trips (`upgrade`→`downgrade`→`upgrade`).
14. [ ] Single Alembic head preserved; `alembic check` clean.
15. [ ] `set_override` upserts idempotently on the unique key and bumps `updated_at`.
16. [ ] `clear_override` deletes when present and is an idempotent success when absent.
17. [ ] Unknown-capability mutation → 422; org/workspace mismatch → 404.
18. [ ] Every mutation writes exactly one `AuditLog` with correct action /
    `previous_state` / `new_state` / `actor_user_id`.
19. [ ] Override row and audit row commit atomically (same session/flush).
20. [ ] All new routes require an operator: 401 anonymous, 403 non-operator.
21. [ ] Read endpoints write no audit rows.
22. [ ] Operator can read the registry, effective per-workspace state, and stored
    overrides, all operator-gated.
23. [ ] Effective-state reads flow through the resolver (never raw rows).
24. [ ] Four-market isolation holds for overrides and reads.
25. [ ] No live feature gate (feedback/scheduling/RSS) consults the resolver in 4A-C.
26. [ ] No global flag is flipped; all three remain `False`.
27. [ ] Operator response schemas are secret-free (no URL/credential/payload/token).
28. [ ] Customer `/system/capabilities` contract shape is byte-unchanged and reports
    every capability `false` while dark.
29. [ ] Contract regeneration yields only additive operator-only changes; idempotent.
30. [ ] No caching: an override change is visible immediately across requests; a clear
    never leaves a capability effectively enabled.
31. [ ] `ruff` clean; full backend suite + frontend suite green.
32. [ ] All five CI jobs pass; no dependency, PR #34, or Dependabot PR touched.

## 8.25 Implementation decomposition (sub-batches)

Each sub-batch is its own branch + PR through the protected workflow, reviewed and
exact-merge-SHA verified, shipping dark. They may also be delivered as one cohesive
4A-C PR if the reviewer prefers a single reviewable unit; the decomposition below is the
logical ordering either way.

- **4A-C.1 — Registry + resolver (pure, no storage).** `capabilities/registry.py` +
  `capabilities/resolver.py` + precedence unit tests. The resolver treats a
  not-yet-present override as absent (reads a zero-row table or an injected empty set).
  Additive; no routes; no migration consumed by anything.
- **4A-C.2 — Override model + migration.** `capabilities/models.py` + the additive
  migration; model-registry import so metadata sees the table; round-trip + `alembic
  check` tests. Single head.
- **4A-C.3 — Override service + audit.** `capabilities/overrides.py` (set/clear, upsert,
  validation, `record_audit`); service + isolation + audit tests.
- **4A-C.4 — Operator management API + contract.** `system/internal_capabilities_routes.py`
  + router registration + additive schemas; regenerate `openapi.json` + `schema.d.ts`;
  authorization + bounds + secret-free tests.
- **4A-C.5 — Verification doc + closeout.** `docs/verification/4a-c-capability-overrides.md`
  capturing evidence (ruff, pytest counts, single head, idempotent contract, dark-state).

The resolver ships **unconsumed** by live gates throughout (§8.16).

## 8.26 File-level implementation map (for the implementation batches)

Backend (new):
- `apps/api/app/capabilities/__init__.py`
- `apps/api/app/capabilities/registry.py` — `Capability` enum + registry (4A-C.1).
- `apps/api/app/capabilities/resolver.py` — `resolve_capability` + `Decision` +
  `CapabilityResolution` (4A-C.1).
- `apps/api/app/capabilities/models.py` — `WorkspaceCapabilityOverride` (4A-C.2).
- `apps/api/alembic/versions/<new>_add_workspace_capability_overrides.py` (4A-C.2).
- `apps/api/app/capabilities/overrides.py` — set/clear service + audit (4A-C.3).
- `apps/api/app/system/internal_capabilities_routes.py` — operator routes + schemas
  (4A-C.4).

Backend (touched, additive):
- `apps/api/app/api/router.py` — include the new operator router (after
  `internal_observability_router`).
- The model-registry import path (whatever imports models for `Base.metadata`) so the
  new table is created (4A-C.2).

Contract (regenerated, additive):
- `apps/api/openapi.json`, `apps/web/src/api/schema.d.ts` via `npm run gen:types` (4A-C.4).

Tests (new, under `apps/api/app/tests/`):
- `test_capability_resolver.py` (precedence units), `test_capability_overrides.py`
  (service + audit + isolation), `test_capability_overrides_api.py` (operator auth,
  bounds, secret-free, dark-by-default).

Docs:
- `docs/phase-4a-c-plan.md` (this file), `docs/verification/4a-c-capability-overrides.md`
  (4A-C.5).

**Explicitly NOT touched:** `feedback/routes.py`, `scouting_requests/routes.py`,
`scouting_requests/schedules.py`, `connectors/registry.py`, `system/routes.py`,
`core/config.py` (no flag change), the 4A-B observability routes, PR #34, Dependabot PRs.

## 8.27 Risks & mitigations

- **R1 — Precedence bug enables a capability unintentionally.** Mitigation: deny-biased
  default, ceiling-first evaluation, exhaustive precedence tests, dark defaults, resolver
  unconsumed by live gates in 4A-C.
- **R2 — Operator surface leaks sensitive data.** Mitigation: reuse secret-free operator
  schema conventions; explicit "no URL/credential/payload/token/identifier" tests.
- **R3 — Contract drift breaks the customer contract.** Mitigation: additive-only
  operator schemas; CI drift gate; customer `/system/capabilities` shape byte-unchanged.
- **R4 — Migration not single-head / not reversible.** Mitigation: chain off
  `4945b98229e6`, round-trip test, `alembic check`, batch-safe ops.
- **R5 — An override outlives its intent (stale enable).** Mitigation: no caching; explicit
  clear; deny-biased precedence; audit trail of every set/clear.
- **R6 — Storable set drifts from resolvable set.** Mitigation: single registry drives both
  the resolver and the migration `CheckConstraint`; unknown capability → 422 on write and
  `safety_ceiling`/disabled on read.
- **R7 — Accidental gate wiring or flag flip slips in.** Mitigation: §8.16/§8.26 name the
  files that must NOT change; acceptance criteria 25/26 assert no gate consumption and no
  flag flip; review + tests enforce.

## 8.28 Open decisions

- **Q1 — Concrete safety-ceiling inputs (rule 1).** Is the ceiling environment-driven
  (e.g. an "activation-prohibited" environment), config-driven, or both? Default working
  assumption for the plan: unknown/unregistered capability is always ceiling-blocked;
  a broader environment ceiling is defined before 4A-C.1 lands.
- **Q2 — One PR or five sub-batches (§8.25)?** Default: reviewer's preference; the plan
  supports either. Recommend a single cohesive 4A-C PR given the parts are tightly
  coupled and all dark.
- **Q3 — Expiry/`valid_until` column?** Default: explicit clear only, no TTL worker
  (N4). Revisit only if Phase 4B needs time-boxed activations.
- **Q4 — Should the customer `/system/capabilities` ever reflect per-workspace effective
  state?** Default: no in 4A-C (keeps the customer contract unchanged); revisit as a
  deliberate, separately-approved customer-contract change if/when a capability is
  actually activated.
- **Q5 — Route grouping.** New routes under `/internal/system/capabilities/*` (chosen) vs
  a sibling `/internal/capabilities/*`. Naming only; both operator-gated. Chosen to sit
  beside the existing `/internal/system/capabilities` topology read.

## 8.29 CI / protected-merge requirements

Every 4A-C implementation PR must:

- Pass all five required CI jobs (`.github/workflows/ci.yml`): Frontend quality, Backend
  quality, Migrations and API contract, Container build and security, Integration smoke.
- Keep a **single** Alembic head and pass `alembic check` (round-trip in "Migrations and
  API contract").
- Produce **no** contract drift beyond additive operator-only schema (`npm run gen:types`
  then `git diff --exit-code` on `openapi.json` + `schema.d.ts`).
- Merge only via the protected squash workflow under the repository ruleset (1 approval,
  stale-approval dismissal, approval-after-push, review-thread resolution, zero bypass
  actors); no admin bypass, no auto-merge.
- Be exact-merge-SHA verified (post-merge `push` CI green on the merge commit) before the
  batch is considered complete.

## 8.30 Phase 4B entry criteria

Phase 4B (first real activation — opportunity feedback in one internal workspace) may
begin only when *all* hold:

- Phase 4A-C is merged and exact-merge-SHA verified, all dark.
- The resolver + override store + operator management API are verified by tests and by an
  operator dry-run that sets and clears an override and changes **no** customer-visible
  behavior (because no gate consumes the resolver yet).
- The deny-biased precedence (incl. the safety ceiling) is verified by tests.
- A separate, explicitly-approved batch has wired the resolver into the relevant live
  gate(s) — still dark — and a separate, explicitly-approved Phase 4B plan/branch
  authorizes the scoped enablement of `opportunity_feedback_enabled` for a single named
  internal workspace.

Phase 4A-C authorizes neither the gate wiring nor Phase 4B.

## 8.31 Definition of done (Phase 4A-C)

Phase 4A-C is done when: the registry, resolver, override table + migration, override
service, and operator management API are merged and exact-merge-SHA verified; the
acceptance criteria (§8.24, all 32) pass; a single Alembic head is preserved; the
customer contract is byte-unchanged in shape and reports every capability disabled; the
resolver is **not** consumed by any live feature gate; all three global flags remain
`False`; and **no capability has been enabled for any customer**. Phase 4A-C authorizes
no rollout, no gate wiring, and no flag activation — the first activation is a separate
Phase 4B decision.

## 8.32 Batch delivery progress (per-batch, evidence-preserving)

This section tracks the delivery of the renumbered 4A-C sub-batches (see §8.30 of
`docs/phase-4a-c-2-plan.md` for the renumbering rationale). It appends progress only and
rewrites no historical SHA or closeout evidence.

- **4A-C.1 — capability foundation (registry + model + migration):** merged (PR #70).
  Verification: `docs/verification/4a-c-1-capability-foundation.md`. All dark.
- **4A-C.2 — deny-biased capability resolver:** merged (PR #72). The resolver ships
  **unconsumed** — no live gate imports it. Verification:
  `docs/verification/4a-c-2-capability-resolver.md`. All dark.
- **4A-C.3 — governed override service:** **planning underway** — see
  `docs/phase-4a-c-3-plan.md`. Service-module-only; no route, no schema, no migration, no
  resolver wiring, no real override record. The resolver stays unconsumed and every
  capability remains dark.
- **4A-C.4 — operator management API + contract:** **planning underway** — see
  `docs/phase-4a-c-4-plan.md`. Implementation not started. The plan is additive
  operator-only routes that expose the merged resolver + override service under
  `/internal/system/capabilities/*`; it flips no flag (all three remain `False`), adds no
  migration (single head `98289430a3ec` preserved), wires the resolver into no live gate,
  and leaves PR #34 untouched. It records that this batch introduces the first *sanctioned*
  consumer of the resolver/service — the operator management API, which is **not** a live
  gate — so the dark-state guards are reframed accordingly. Every capability remains dark.

---

**Status:** `PLANNING — DOCUMENTATION ONLY — IMPLEMENTATION NOT STARTED — ALL CAPABILITIES REMAIN DARK`
