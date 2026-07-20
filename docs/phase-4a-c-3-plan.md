# Phase 4A-C.3 — Governed Workspace Capability Override Service (Plan)

## 8.1 Title & status

**Batch:** Phase 4A-C.3 — Governed Workspace Capability Override Service.
**Nature:** additive, backend service + unit/integration tests only. One new
service module `apps/api/app/capabilities/service.py`, one typed-error addition to
`apps/api/app/capabilities/errors.py` (new, capabilities-local), one typed result
model, and one new test module — plus this plan doc and a minimal,
evidence-preserving doc-alignment note.
**Status:** `PLANNING — DOCUMENTATION ONLY — SERVICE NOT IMPLEMENTED —
RESOLVER UNCONSUMED — PERSISTENCE UNUSED — ALL CAPABILITIES REMAIN DARK`.
**Baseline:** `main` at `5802784020423b94032e387a1311a59e3259b8ab` (Phase 4A-C.2
merge, PR #72). Alembic single head `98289430a3ec` (12 migrations). All three
global capability flags `False`. **No HTTP API. No route. No schema. No contract
regeneration. No real override record.**

This document is a planning artifact. It authorizes no source, test, migration,
contract, dependency, or flag change. Implementation is a later, separately
approved batch and is decomposed in §8.32.

## 8.2 Executive summary

Phase 4A-C.1 landed the **storage** plane (registry + `WorkspaceCapabilityOverride`
model + additive migration). Phase 4A-C.2 landed the **decision** plane: the pure,
deny-biased `resolve_capability` / `decide_capability` in
`apps/api/app/capabilities/resolver.py`, shipped unconsumed. What is still missing
is the **write** plane: nothing can create, update, or clear an override row, and
nothing reads them back for an operator surface.

Phase 4A-C.3 adds that write plane as a **service module only**: a small,
transaction-participating API to read, set, and clear per-workspace capability
overrides, with authoritative tenant validation, deny-biased policy enforcement
(an override may never enable a capability the registry marks
non-`workspace_enableable`, e.g. RSS), idempotent upsert semantics, single-row
concurrency safety, full audit logging, and typed domain errors — mirroring the
established service conventions of `apps/api/app/feedback/service.py` and the
scouting-schedule service.

Critically, this batch ships the service **with no live consumer**: no route
imports it, no resolver call site is wired into any gate, no global flag flips, and
**no real override row is created** (tests seed throwaway rows in temp SQLite and
tear them down). Because every global flag stays `False` and no production override
row exists, `resolve_capability` continues to resolve every capability in every
workspace to **disabled**. Every capability remains dark. The operator/customer
route + schema + contract regeneration that would let a real request reach this
service is the separate, later, explicitly-approved Phase 4A-C.4.

## 8.3 Goals

- **G1.** A single authoritative service module
  `apps/api/app/capabilities/service.py` that is the *only* write path for
  `WorkspaceCapabilityOverride` rows, plus its read-back accessors.
- **G2.** Authoritative tenant validation: every operation loads the concrete
  `Workspace` and confirms `workspace.organization_id == organization_id` before
  touching an override, so a caller can never bind or read an override across a
  tenant boundary (defense in depth over the resolver's in-memory org match).
- **G3.** Deny-biased policy enforcement at the write boundary: a `set` that would
  record `enabled=True` for a capability whose policy is
  `workspace_enableable=False` (RSS) is **rejected** with a typed error — the
  storable intent can never contradict the registry's governance metadata.
- **G4.** Idempotent, race-safe upsert: setting an override is insert-or-update in
  place against the unique `(workspace_id, capability)` constraint, safe under
  concurrent callers, with no duplicate row and no lost update.
- **G5.** Full auditability: every create/update/clear/reject emits a
  `record_audit(...)` entry sharing the caller's transaction, with bounded,
  secret-free event names and states.
- **G6.** Typed domain errors (extending the `SignalNestError` taxonomy) for every
  governance outcome, never `HTTPException` in the service layer.
- **G7.** Keep everything dark and additive: no route, no resolver wiring, no flag
  flip, no schema/migration/contract/dependency change, and **no real override
  row**. The service ships with no live consumer.

## 8.4 Non-goals (explicit prohibitions)

- **N1.** No operator or customer **route/schema** and no OpenAPI/contract
  regeneration — that is Phase 4A-C.4.
- **N2.** No wiring of the resolver (or this service) into any live gate
  (`feedback/routes.py`, `scouting_requests/routes.py`,
  `scouting_requests/schedules.py`, `connectors/registry.py`). The resolver stays
  unconsumed; the service ships with no live consumer.
- **N3.** No new migration; Alembic head stays `98289430a3ec` (12 migrations). No
  model change — in particular, **no approval / change-reference column is added**
  (see §8.18).
- **N4.** No `core/config.py` change and no flag flip; all three capability flags
  remain `False`.
- **N5.** No frontend change. No dependency change. No changes to PR #34 or any
  Dependabot PR.
- **N6.** No creation of any **real** override row. Tests seed throwaway rows in an
  in-memory/temp SQLite (or `TEST_POSTGRES_URL`-gated) database and tear them down.
- **N7.** No caching, no background job, no metrics backend change (the metrics in
  §8.26 are a design recommendation for 4A-C.4's wiring, not implemented here).
- **N8.** No cross-workspace or cross-tenant bulk operation; every operation is
  scoped to exactly one `(organization_id, workspace_id, capability)`.

## 8.5 Current-state architecture (audit findings)

Grounded against the baseline SHA `5802784`:

- **Registry** (`app/capabilities/registry.py`): closed `Capability` StrEnum
  (`opportunity_feedback`, `scout_scheduling`, `connector_rss`); frozen
  `CapabilityPolicy` with `workspace_enableable`, `workspace_disableable`,
  `subject_to_safety_ceiling`, `global_flag_attr`, `requires_workspace_context`,
  `future_activation_phase`; `CAPABILITY_REGISTRY` (`MappingProxyType`);
  `get_policy`, `capability_from_value` (strict → `UnknownCapabilityError`),
  `iter_capabilities`, `persisted_values`. **RSS:** `workspace_enableable=False`,
  `workspace_disableable=True`.
- **Model** (`app/capabilities/models.py`): `WorkspaceCapabilityOverride` — UUID
  PK, `TimestampMixin` (`created_at`/`updated_at`), scope FKs `organization_id`
  (CASCADE), `workspace_id` (CASCADE), `capability` `String(64)` + portable
  `CheckConstraint` over `persisted_values()`, `enabled` Boolean, `set_by_user_id`
  FK `users.id` `SET NULL` (nullable), `reason` `Text` (nullable). Unique
  `(workspace_id, capability)`. **No approval/change-reference column. No version
  column.** Workspaces carry **no `(id, organization_id)` composite key**, so a
  composite tenant FK is unavailable — ownership must be checked in code.
- **Resolver** (`app/capabilities/resolver.py`): pure `decide_capability` +
  `resolve_capability` (keyword-only), `_load_override` issues exactly one
  `session.scalar(select(...))` on `(workspace_id, capability.value)` and returns
  `None` on absence or org mismatch (in-memory tenant validation). `DecisionSource`
  StrEnum; frozen `CapabilityResolution`. Shipped **unconsumed**.
- **Tenant/auth** (`app/auth/dependencies.py`): `TenantContext(user, organization,
  workspace, role)`; `require_operator(user)` raises `PermissionDeniedError` at the
  route boundary if not `is_operator`; `get_tenant_context` loads the `Workspace`
  (`NotFoundError` if missing) + membership check; `require_role(*allowed)` via
  `_ROLE_RANK`.
- **Tenant model** (`app/organizations/models.py`): `Workspace(id, organization_id
  FK CASCADE, name, slug, onboarding_completed, …)` — **no `is_active`/`deleted`
  column**, only `onboarding_completed`. `Organization`/`User` `String(32)` PKs.
  `User.is_operator` server-controlled. `OrganizationMember(organization_id,
  user_id, role)` unique `(organization_id, user_id)`.
- **Errors** (`app/core/errors.py`): `SignalNestError(status_code, code)` base;
  `NotFoundError`(404), `PermissionDeniedError`(403), `AuthError`(401),
  `ConflictError`(409), `ValidationDomainError`(422), configuration/adapter family
  (503). `register_exception_handlers` maps to a `_envelope(code, message,
  details)` with `request_id`.
- **Audit** (`app/audit/service.py`): `record_audit(db, *, organization_id, action,
  actor_user_id=None, workspace_id=None, entity_type=None, entity_id=None,
  reason=None, previous_state=None, new_state=None, context=None) -> AuditLog` —
  `db.add()` + `db.flush()`, **never commits**; caller owns the transaction. Dotted
  free-string event names (e.g. `"opportunity_feedback.created"`).
- **Service conventions** (`app/feedback/service.py`, scouting-schedule service):
  explicit `db: Session` first positional, keyword-only domain args, `flush()` not
  commit, caller owns the transaction, return the flushed ORM row, raise
  `SignalNestError` subclasses (never `HTTPException`), emit `record_audit` +
  `log_event`, role/feature gating deferred to the route.
- **Idempotency** (`app/intelligence/persistence.py`): `db.begin_nested()`
  SAVEPOINT + `IntegrityError` rollback → re-query the existing row.
- **Concurrency** (`app/tests/test_scout_schedule_concurrency.py`):
  `SELECT … FOR UPDATE` on a stable parent (workspace) row, PostgreSQL-gated
  threaded test + an always-run compile proof.
- **Pagination** (`app/scouting_requests/run_history.py`): `DEFAULT_LIMIT=20`,
  `MAX_LIMIT=100`, `_clamp_limit`/`_clamp_offset`, order `created_at DESC, id DESC`.

**Conclusion:** every dependency the service needs exists; the only gaps are the
service module itself and its capabilities-local typed errors + result model. No
schema change is required or permitted.

## 8.6 Module architecture

- **New:** `apps/api/app/capabilities/service.py` — the write plane + read-back
  accessors. Imports from `registry`, `models`, `audit.service`, `core.errors` (or
  the new local error module), `core.logging`, and `organizations.models`
  (`Workspace`). It does **not** import any route module and is **not** imported by
  any route in this batch.
- **New:** `apps/api/app/capabilities/errors.py` — capabilities-local typed domain
  errors that subclass the shared `SignalNestError` taxonomy (§8.15). Kept local to
  the package (not in `core/errors.py`) so the additive surface is contained and
  easy to review; each error still maps through the shared envelope.
- **New:** result model(s) live in `service.py` (or a sibling
  `capabilities/results.py` if the file grows) — a frozen, secret-free dataclass
  describing a mutation outcome (§8.14).
- **New:** `apps/api/app/tests/test_capability_override_service.py` — the full test
  matrix (§8.29).
- **Unchanged:** `resolver.py`, `registry.py`, `models.py`, `core/config.py`, every
  route module, the frontend, and the migration set.

Rationale: a dedicated module mirrors the one-module-per-write-path house style
(`feedback/service.py`) and keeps the resolver pure (read-only) and the service
transactional (write) as two clean planes.

## 8.7 Public interface

All functions are `db`-first, keyword-only for domain args, and caller-owns-commit.
Actor is always explicit (§8.16). Proposed signatures:

```python
def get_capability_override(
    db: Session, *, organization_id: str, workspace_id: str, capability: Capability
) -> WorkspaceCapabilityOverride | None: ...

def list_capability_overrides(
    db: Session, *, organization_id: str, workspace_id: str,
    limit: int = DEFAULT_LIMIT, offset: int = 0,
) -> OverridePage: ...

def set_capability_override(
    db: Session, *, organization_id: str, workspace_id: str, capability: Capability,
    enabled: bool, actor_user_id: str, reason: str | None = None,
) -> OverrideMutation: ...

def clear_capability_override(
    db: Session, *, organization_id: str, workspace_id: str, capability: Capability,
    actor_user_id: str,
) -> OverrideMutation: ...
```

`Capability` is the typed enum (callers at the route boundary convert an
untrusted string via `capability_from_value`, which raises
`UnknownCapabilityError` → mapped to a validation error; the service accepts only
the typed enum so an unknown capability cannot reach persistence).

## 8.8 Read operations

- **`get_capability_override`** — authoritative tenant validation (§8.12), then one
  indexed `session.scalar(select(...))` on `(workspace_id, capability.value)`.
  Returns the ORM row or `None`. Read-only; no flush; never raises for absence.
- **`list_capability_overrides`** — tenant validation, then a `count(*)` + a page of
  rows ordered `created_at DESC, id DESC`, clamped via `_clamp_limit`
  (`DEFAULT_LIMIT=20`, `MAX_LIMIT=100`) / `_clamp_offset` (mirrors
  `run_history.py`). Returns a typed `OverridePage(items, total, limit, offset)`.
  Scoped strictly to the one workspace; never returns another workspace's rows.

Reads never mutate and never emit audit entries.

## 8.9 Set operation (`set_capability_override`)

Ordered steps (fail-closed, first failing gate wins):

1. **Tenant validation** (§8.12) — load `Workspace`, confirm ownership; else
   `NotFoundError` / `CapabilityTenantMismatchError`.
2. **Policy enforcement** (§8.11) — `enabled=True` requires
   `policy.workspace_enableable`; `enabled=False` requires
   `policy.workspace_disableable`; otherwise raise
   `CapabilityOverrideNotPermittedError`. RSS-enable is rejected here.
3. **Reason validation** (§8.17) — normalize/length-bound the optional note.
4. **Concurrency lock** (§8.22) — `SELECT … FOR UPDATE` the workspace row so
   concurrent sets on the same `(workspace, capability)` serialize.
5. **Idempotent upsert** (§8.21) — if a row exists, update `enabled`/`reason`/
   `set_by_user_id` in place (capturing `previous_state`); else insert. Use the
   `begin_nested()` SAVEPOINT + `IntegrityError` fallback so a lost race under the
   unique constraint re-reads and updates rather than raising.
6. **`db.flush()`** — id-bearing row, no commit.
7. **Audit** (§8.19/§8.20) — `workspace_capability_override.created` or `.updated`
   with bounded `previous_state`/`new_state`.
8. Return `OverrideMutation(row, created: bool, changed: bool)`.

Idempotent no-op: setting the identical `(enabled, reason)` that already exists is
a success returning `changed=False` and emits **no** audit mutation entry (or an
explicit `.unchanged` — see §8.37 open decision D3).

## 8.10 Clear operation (`clear_capability_override`)

1. Tenant validation (§8.12).
2. Concurrency lock on the workspace row (§8.22).
3. Look up the `(workspace, capability)` row. If absent → idempotent success
   returning `OverrideMutation(row=None, created=False, changed=False)` and **no**
   audit mutation (clearing a non-existent override is a benign no-op; deny-biased:
   absence already resolves disabled).
4. If present → capture `previous_state`, `db.delete(row)`, `db.flush()`.
5. Audit `workspace_capability_override.cleared` with the prior state.
6. Return `OverrideMutation(row=None, created=False, changed=True)`.

Clearing never enables anything; after a clear the resolver falls back to the
global flag (currently `False` → disabled).

## 8.11 Policy enforcement (deny-biased, registry-derived)

The write boundary enforces the registry's governance metadata so a stored intent
can never contradict it:

- `enabled=True` is permitted **only** if `get_policy(capability).workspace_enableable`.
  For `CONNECTOR_RSS` (`workspace_enableable=False`) this raises
  `CapabilityOverrideNotPermittedError` → the row is never written.
- `enabled=False` is permitted **only** if `workspace_disableable` (true for all
  current capabilities).
- This mirrors the resolver's rule-2 deny bias (an un-honorable enable resolves to
  `secure_default`), but at **write** time: rather than silently storing an
  un-honorable intent that the resolver would ignore, the service refuses to store
  it at all — so the persisted set is always self-consistent with the registry.
- A rejected set emits `workspace_capability_override.rejected` (§8.19) for
  operator traceability and raises a typed error; **no row is written**.

## 8.12 Authoritative tenant validation

Stronger than the resolver's in-memory org match. Every operation:

1. Loads the concrete workspace: `workspace = db.get(Workspace, workspace_id)`.
2. If `workspace is None` → `NotFoundError` (the workspace does not exist).
3. If `workspace.organization_id != organization_id` →
   `CapabilityTenantMismatchError` (a 404-mapped, non-enumerating error so a caller
   cannot probe cross-tenant existence — see §8.27).

Because `Workspace` has **no `is_active`/soft-delete column** (only
`onboarding_completed`), there is no active-state check to add; existence +
ownership is the authoritative gate. Because workspaces carry **no `(id,
organization_id)` composite key**, ownership is a code-level comparison, not an FK.
This authoritative load is the piece the resolver deliberately deferred to the
service layer (resolver plan §8.11 discrepancy note).

## 8.13 Resolver alignment (single lookup shape)

Two options considered:

- **Option A — resolver delegates to the service.** Rejected: it would make the
  pure, DB-light resolver depend on the transactional write module and its
  authoritative `Workspace` load, coupling the read-hot decision path to heavier
  I/O and inverting the dependency (decision plane → write plane).
- **Option B — a shared read helper.** The single-row override lookup keyed on
  `(workspace_id, capability.value)` is expressed once and reused by both
  `resolver._load_override` and `service.get_capability_override`, so the two
  planes never drift on *how* an override row is found.

**Recommendation: Option B, implemented conservatively.** In 4A-C.3, keep the
resolver **byte-for-byte unchanged** (it is merged and unconsumed; touching it
would expand scope and risk the dark guarantee). Instead, have the new service
read via its own thin `get_capability_override`, and record in this plan that a
*future* refactor (no earlier than 4A-C.4, when a live consumer exists) may extract
the shared single-row selector. This keeps 4A-C.3 strictly additive while
documenting the intended convergence. The two lookups are already semantically
identical (same unique key, same tenant-mismatch → absence bias), so no behavioral
drift exists at rest.

## 8.14 Typed result models

Frozen, `slots=True`, secret-free dataclasses (mirroring `CapabilityResolution`):

- **`OverrideMutation`** — `capability: Capability`, `workspace_id: str`,
  `created: bool`, `changed: bool`, `enabled: bool | None` (the resulting stored
  value, `None` after a clear), and `override_id: str | None`. No reason text, no
  actor id, no timestamps in the *result* type (they live on the ORM row / audit
  log, not the operator-facing summary).
- **`OverridePage`** — `items: tuple[WorkspaceCapabilityOverride, ...]`,
  `total: int`, `limit: int`, `offset: int` (list read-back).

These are the shapes a 4A-C.4 route would serialize; defining them now keeps the
route batch thin.

## 8.15 Typed domain errors

New `apps/api/app/capabilities/errors.py`, each subclassing `SignalNestError` so
`register_exception_handlers` renders them through the standard envelope:

- **`CapabilityOverrideNotPermittedError(ValidationDomainError)`** — an override
  contradicts the registry policy (e.g. RSS enable). Maps to 422, code
  `capability_override_not_permitted`.
- **`CapabilityTenantMismatchError(NotFoundError)`** — workspace exists but is not
  owned by the passed organization (mapped to 404 to avoid cross-tenant
  enumeration; §8.27).
- (Workspace-absent reuses the shared `NotFoundError`.)

No new HTTP status codes; no `HTTPException` in the service. Every raise is a typed
governance outcome the caller (future route) can translate deterministically.

## 8.16 Actor validation

- `actor_user_id` is a **required** keyword arg on `set`/`clear` (no default, no
  implicit "system" actor). The service records *who* changed the override for
  audit provenance.
- `require_operator` is a **route** concern (4A-C.4), not repeated in the service —
  matching the house rule that role/feature gating lives at the boundary
  (`feedback/service.py` leaves editor-gating to its route). The service trusts that
  its caller has already authorized the actor, but still *requires* the actor id so
  no override is ever written anonymously.
- The service does **not** load or validate the user row (that would couple write to
  auth); `set_by_user_id` is an FK with `SET NULL`, so a later user deletion is
  handled by the schema, not the service.

## 8.17 Reason validation

- `reason` is optional (`str | None`), stored in the nullable `Text` column.
- Normalize: strip surrounding whitespace; treat empty/whitespace-only as `None`.
- Bound the length (recommend a module constant `MAX_REASON_LEN = 500`) and raise
  `ValidationDomainError` if exceeded — a defensive, non-secret cap so the operator
  note stays a short justification, not a payload.
- The reason is a **non-secret** operator note; it is stored on the row and passed
  to `record_audit(reason=…)` but is **never** placed in metrics labels or in the
  `OverrideMutation` result (§8.14, §8.26).

## 8.18 Approval / change reference

The merged model has **no approval or change-reference column** (§8.5). Therefore:

- **4A-C.3 does NOT add one** — adding a column is a migration, explicitly out of
  scope (N3), and would change the Alembic head.
- Change provenance in this batch is carried by (a) `set_by_user_id` on the row and
  (b) the audit log entries (§8.19) with `previous_state`/`new_state`.
- A formal approval/change-ticket reference (e.g. a required change-request id) is
  **deferred**: recorded here as open decision **D1** (§8.37) and, if approved,
  would be its own additive migration batch (a hypothetical 4A-C.3.x or a 4A-C.4
  concern) — never a casual column add inside this service batch.

## 8.19 Audit events

Dotted, bounded event names via `record_audit(action=…)` with
`entity_type="workspace_capability_override"`:

- `workspace_capability_override.created` — first-time set.
- `workspace_capability_override.updated` — in-place change of an existing row.
- `workspace_capability_override.cleared` — deletion of an existing row.
- `workspace_capability_override.rejected` — a policy-denied set (§8.11); no row
  written, but the *attempt* is recorded for operator traceability (entity_id may be
  `None`).

Each entry carries `organization_id`, `workspace_id`, `actor_user_id`, and bounded
`previous_state`/`new_state` dicts containing only `{capability, enabled}` (plus the
override id where one exists). The optional `reason` is passed via `record_audit`'s
`reason=` field, not embedded in state dicts.

## 8.20 Audit atomicity

- `record_audit` `flush`es but **never commits**; the service `flush`es but never
  commits (house rule). Therefore the override mutation **and** its audit entry
  share the **caller's single transaction**.
- Consequence: if the caller's commit fails, both the override change and its audit
  entry roll back together — there is never an audited change that did not persist,
  nor a persisted change with no audit trail.
- The service emits the audit entry **after** the successful `flush` of the mutation
  (so a failed mutation raises before any audit write), matching
  `feedback/service.py` ordering.

## 8.21 Idempotency

- The unique `(workspace_id, capability)` constraint makes `set` an upsert. The
  service first reads the existing row (under the §8.22 lock); if present it updates
  in place, else inserts.
- As a race backstop, the insert path runs inside `db.begin_nested()` (SAVEPOINT);
  on `IntegrityError` (a concurrent insert won) it rolls back the savepoint,
  re-reads the now-present row, and updates it — mirroring
  `intelligence/persistence.py`. No duplicate row, no lost update.
- Repeated identical `set` calls converge to the same single row; a `set` that
  changes nothing returns `changed=False` (§8.9). Repeated `clear` calls are
  idempotent no-ops after the first (§8.10).

## 8.22 Concurrency

- Serialize concurrent mutations on the same workspace with a
  `SELECT … FOR UPDATE` on the **workspace row** (a stable parent), acquired before
  the upsert/delete — mirroring `test_scout_schedule_concurrency.py`'s
  `_lock_workspace_for_cap`.
- On SQLite the `FOR UPDATE` clause compiles and is a harmless no-op (single-writer);
  the real serialization backstop is the unique constraint + the SAVEPOINT retry
  (§8.21). On PostgreSQL the row lock provides true serialization.
- Tests: a `TEST_POSTGRES_URL`-gated threaded test asserting two concurrent
  `set`/`clear` callers converge without duplicate rows or lost updates, plus an
  **always-run compile proof** that the locking statement compiles (mirroring
  `TestCapLockCompiles`).

## 8.23 Transaction boundaries

- The service **never** calls `commit`, `rollback` (except within its own
  `begin_nested` SAVEPOINT), or opens its own session/engine. It receives an
  explicit `db: Session` and participates in the caller's unit of work.
- The caller (a future 4A-C.4 route via the request-scoped session dependency) owns
  `commit`/`rollback`. This matches every existing service and keeps the mutation +
  audit atomic (§8.20).

## 8.24 Read-after-write

- `set`/`clear` return the flushed state (`OverrideMutation`), so the caller has the
  authoritative result within the same transaction without a re-select.
- A subsequent `get_capability_override` in the same transaction observes the
  flushed change (SQLAlchemy identity map / flushed pending state), giving a caller
  a consistent read-after-write without committing.

## 8.25 Logging

- Use `app.core.logging.get_logger("signalnest.capabilities")` + `log_event(...)`
  with `outcome="success"|"rejected"` mirroring `feedback/service.py`.
- Log fields are **bounded and secret-free**: `capability` (enum value),
  `workspace_id`, `enabled`, `created`/`changed`, and event outcome. **Never** log
  the `reason` note, actor email, tokens, or raw error text.
- One structured event per mutation; reads are not logged (or logged at debug only).

## 8.26 Metrics

- **Design recommendation only** (no metrics backend change in this batch, N7): if
  4A-C.4 wires a counter, use bounded labels only — `capability` (3 values) and
  `outcome` (`created`/`updated`/`cleared`/`rejected`/`unchanged`).
- **Never** label by `organization_id`, `workspace_id`, `actor_user_id`, or
  `reason` (unbounded cardinality + privacy). Recorded here so the later batch
  inherits the constraint.

## 8.27 Security

- **IDOR / cross-tenant defense:** authoritative workspace load + ownership check
  (§8.12); a tenant mismatch returns a **404-mapped** error, not 403, so a caller
  cannot distinguish "exists but not yours" from "does not exist" (no cross-tenant
  enumeration). This mirrors the resolver's deny-biased absence treatment.
- **Policy tamper-resistance:** the registry is a `MappingProxyType` of frozen
  policies; the service reads it read-only and can never store an intent that
  violates it (§8.11).
- **No privilege inference:** the service requires an explicit `actor_user_id` but
  does not itself grant operator rights — the route's `require_operator` is the
  authorization gate (§8.16).
- **No secret surface:** result model, logs, and metrics are secret-free; the reason
  note is bounded and lives only on the row + audit log.

## 8.28 Privacy

- The only free-text is the bounded `reason` operator note; it is retained on the
  row (CASCADE-deleted with the workspace/org) and in the audit log, and is excluded
  from results, logs, and metrics.
- Actor attribution is `SET NULL` on user deletion — deleting a user forgets *who*
  set an override without destroying the override, matching the model's stated
  retention posture.
- No customer PII is introduced; overrides describe governance intent, not personal
  data.

## 8.29 Full test matrix

New `apps/api/app/tests/test_capability_override_service.py` (engine-scoped SQLite
`PRAGMA foreign_keys=ON`, throwaway rows, plus `TEST_POSTGRES_URL`-gated
concurrency):

**Set — happy paths**
1. First set `enabled=True` on an enableable capability creates a row; returns
   `created=True, changed=True`; audit `.created`.
2. Set `enabled=False` on an existing enabled row updates in place; audit
   `.updated`; `previous_state` captured.
3. Idempotent re-set of identical `(enabled, reason)` → `changed=False`, no
   duplicate row, no `.updated` audit (or `.unchanged` per D3).
4. Set with a `reason` stores the stripped note; empty/whitespace reason → `None`.

**Set — policy denials**
5. RSS `enabled=True` → `CapabilityOverrideNotPermittedError`; **no row written**;
   audit `.rejected`.
6. (Sanity) RSS `enabled=False` is permitted (disableable) → row written.
7. Over-long reason → `ValidationDomainError`; no row written.

**Tenant validation**
8. Unknown `workspace_id` → `NotFoundError`.
9. Workspace owned by a different org → `CapabilityTenantMismatchError` (404-mapped),
   no row written, no cross-tenant leak.
10. Correct org/workspace succeeds.

**Clear**
11. Clear an existing row deletes it; audit `.cleared`; resolver then falls back to
    the (False) global flag → disabled.
12. Clear a non-existent override → idempotent no-op, `changed=False`, no audit.

**Read**
13. `get_capability_override` returns the row / `None`; tenant-validated.
14. `list_capability_overrides` returns only the workspace's rows, ordered
    `created_at DESC, id DESC`, clamped to `MAX_LIMIT`, with correct `total`.
15. Per-workspace and per-capability isolation (a set in ws-A never appears in ws-B).

**Idempotency / concurrency**
16. Upsert race backstop: simulate `IntegrityError` on insert → SAVEPOINT rollback →
    update the existing row (single row survives).
17. `TEST_POSTGRES_URL`-gated: two concurrent `set` callers on the same
    `(workspace, capability)` converge to one row, no lost update.
18. Always-run compile proof that the `SELECT … FOR UPDATE` workspace-lock statement
    compiles.

**Audit atomicity / transaction**
19. Mutation + audit share one transaction: a forced caller rollback discards both
    the override and its audit entry.
20. The service never commits (assert no commit on the passed session; caller owns
    it).

**Dark-state coupling**
21. With shipped defaults, after any `set`/`clear`, `resolve_capability` still
    resolves the capability **disabled** whenever the global flag is `False`
    (a per-workspace enable override only *would* enable if a live gate consumed the
    resolver — which none does; this asserts the persistence-vs-activation split).
22. No route module imports `capabilities.service` (guard, mirroring the resolver
    non-consumption guard) — the service ships with no live consumer.

## 8.30 Schema & contract impact

- **No migration.** No model change. Alembic single head stays `98289430a3ec`
  (12 migrations); `alembic check` must report no new upgrade operations.
- **No OpenAPI/contract change.** `npm run gen:types` must produce a **zero diff**
  (this batch adds no path or schema).
- **No `core/config.py` change**; the three flags stay `False`.

## 8.31 No-live-consumer boundary

- No route imports the service (asserted by test #22).
- The resolver stays byte-for-byte unchanged and unconsumed (§8.13); the existing
  `test_resolver_ships_but_stays_unconsumed_by_live_gates` guard remains green.
- No global flag flips; the override table holds no **real** row (only throwaway
  test rows). `resolve_capability` continues to resolve every capability disabled in
  production.

## 8.32 Implementation decomposition

Small, independently reviewable sub-batches (each additive, each green before the
next):

- **4A-C.3.1** — `capabilities/errors.py` (typed errors) + result dataclasses +
  their unit tests (shape/immutability, error status/code mapping).
- **4A-C.3.2** — read plane: `get_capability_override`, `list_capability_overrides`
  + tenant validation helper + tests (#8–#10, #13–#15).
- **4A-C.3.3** — `set_capability_override`: policy enforcement + reason validation +
  idempotent upsert + audit + tests (#1–#7, #16).
- **4A-C.3.4** — `clear_capability_override` + tests (#11–#12).
- **4A-C.3.5** — concurrency: `SELECT … FOR UPDATE` workspace lock + compile proof +
  PG-gated threaded test (#17–#18); atomicity tests (#19–#20).
- **4A-C.3.6** — dark-state + no-consumer guards (#21–#22) + verification doc
  `docs/verification/4a-c-3-override-service.md`.

(These may be delivered as one PR with ordered commits, matching the 4A-C.2 style,
or split — decided at implementation time; the plan does not pre-authorize either.)

## 8.33 File-level map

- **New:** `apps/api/app/capabilities/service.py` (write plane + reads).
- **New:** `apps/api/app/capabilities/errors.py` (typed domain errors).
- **New (optional):** `apps/api/app/capabilities/results.py` (result dataclasses, if
  not colocated in `service.py`).
- **New:** `apps/api/app/tests/test_capability_override_service.py`.
- **New (implementation batch):** `docs/verification/4a-c-3-override-service.md`.
- **Unchanged:** `resolver.py`, `registry.py`, `models.py`, `core/config.py`,
  `core/errors.py`, every route, the migration set, the frontend, the contract.

## 8.34 Acceptance criteria (50 items)

1. `capabilities/service.py` is the only write path for
   `WorkspaceCapabilityOverride`.
2. `set_capability_override` exists with the §8.7 signature (db-first, keyword-only,
   explicit required `actor_user_id`).
3. `clear_capability_override` exists with the §8.7 signature.
4. `get_capability_override` returns the row or `None`.
5. `list_capability_overrides` returns a tenant-scoped, clamped, ordered page.
6. Every operation performs authoritative tenant validation (load `Workspace`,
   check ownership).
7. Unknown workspace → `NotFoundError`.
8. Cross-tenant workspace → `CapabilityTenantMismatchError`, 404-mapped.
9. No operation returns or mutates another workspace's rows.
10. RSS `enabled=True` set is rejected with `CapabilityOverrideNotPermittedError`.
11. A rejected set writes **no** row.
12. A rejected set emits a `.rejected` audit entry.
13. `enabled=True` is permitted only for `workspace_enableable` capabilities.
14. `enabled=False` is permitted only for `workspace_disableable` capabilities.
15. First set creates a row (`created=True`).
16. Subsequent set updates in place (no duplicate row).
17. Idempotent identical set returns `changed=False`.
18. Set captures `previous_state` on update.
19. Upsert is race-safe via `begin_nested()` SAVEPOINT + `IntegrityError` fallback.
20. Concurrency serialized via `SELECT … FOR UPDATE` on the workspace row.
21. Locking statement has an always-run compile proof.
22. PG-gated concurrency test proves convergence with no lost update.
23. Reason is stripped; empty/whitespace → `None`.
24. Over-length reason → `ValidationDomainError`; no row written.
25. `actor_user_id` is required (no default/system actor).
26. Service does not call `require_operator` (route concern).
27. Clear of an existing row deletes it and emits `.cleared`.
28. Clear of a missing override is an idempotent no-op, no audit.
29. `.created`/`.updated`/`.cleared`/`.rejected` audit event names used exactly.
30. Audit entries carry bounded, secret-free `previous_state`/`new_state`.
31. `reason` is passed via `record_audit(reason=…)`, not embedded in state dicts.
32. Mutation + audit share the caller's single transaction (rollback discards both).
33. Service never commits, never opens its own session/engine.
34. Service returns flushed state (read-after-write within the transaction).
35. Typed result models are frozen, `slots=True`, secret-free.
36. All service errors subclass `SignalNestError`; no `HTTPException`.
37. Logs are bounded/secret-free; `reason` is never logged.
38. Registry read is read-only; no policy mutation.
39. Resolver is byte-for-byte unchanged and remains unconsumed.
40. No route imports `capabilities.service` (guard test).
41. No new migration; Alembic head stays `98289430a3ec` (12 migrations).
42. `alembic check` reports no drift.
43. `npm run gen:types` → zero contract diff.
44. `core/config.py` unchanged; all three flags `False`.
45. `resolve_capability` still resolves every capability disabled in production
    (no real override row, flags false).
46. No frontend change; no dependency change; PR #34 / Dependabot untouched.
47. Full test matrix (§8.29) passes; ruff clean.
48. No real override row is created (only throwaway test rows).
49. Verification doc records baseline SHA, Alembic head, dark-state evidence, and
    any plan-vs-prompt discrepancy.
50. The batch is delivered as a DRAFT PR; not marked ready/merged without explicit
    authorization.

## 8.35 Rollback

- The batch is additive (new files only). Rollback is `git revert` of the merge or
  dropping the new modules/tests — no migration to reverse, no data to unwind, no
  flag to reset. Because no real override row is written and no live path consumes
  the service, reverting has zero runtime effect on any tenant.

## 8.36 Risks & mitigations

- **R1 — accidental live wiring.** Mitigation: the §8.29 #22 no-consumer guard + the
  existing resolver-unconsumed guard fail CI if a route imports the service.
- **R2 — scope creep into a migration** (approval column, resolver refactor).
  Mitigation: §8.18 defers the approval field; §8.13 keeps the resolver unchanged;
  N3 forbids any migration.
- **R3 — concurrency correctness on SQLite.** Mitigation: correctness rests on the
  unique constraint + SAVEPOINT retry (portable); the row lock is the PG backstop,
  proven by the gated test + compile proof.
- **R4 — cross-tenant leak.** Mitigation: authoritative workspace load + 404-mapped
  mismatch (§8.12, §8.27) + isolation tests.
- **R5 — audit/mutation divergence.** Mitigation: shared transaction, flush-not-
  commit, audit-after-flush ordering (§8.20) + rollback test #19.
- **R6 — persisted intent contradicting policy.** Mitigation: write-time policy
  enforcement (§8.11) refuses to store an un-honorable intent.

## 8.37 Open decisions

- **D1 — approval/change reference.** Add a required change-reference column later?
  Defer; if approved it is its own additive migration batch, never folded into this
  service batch (§8.18).
- **D2 — result vs ORM return.** `set`/`clear` return the typed `OverrideMutation`
  (recommended) vs the raw ORM row. Recommend the typed result for a stable,
  secret-free 4A-C.4 serialization surface (§8.14).
- **D3 — unchanged-set audit.** Emit a `.unchanged` audit entry on an idempotent
  no-op set, or emit nothing? Recommend emit nothing (audit records *changes*), but
  leave for implementation review.
- **D4 — shared single-row selector extraction.** When to converge
  `resolver._load_override` and `service.get_capability_override` onto one helper?
  Recommend no earlier than 4A-C.4 (§8.13).

## 8.38 Phase 4A-C.4 entry criteria

4A-C.4 (operator/customer management API + contract) may begin only once:

- 4A-C.3 is merged; the service + tests are green on the exact merge SHA.
- The resolver remains unconsumed and the service has no live consumer.
- Alembic head is still `98289430a3ec`; contract diff is still zero.
- All three flags remain `False`; no real override row exists.
- 4A-C.4 is separately, explicitly approved; it adds the route/schema, regenerates
  the contract, enforces `require_operator`, and only then may a real override row be
  created — still behind dark global flags until Phase 4B activation.

## 8.39 Definition of done (planning)

- This plan (`docs/phase-4a-c-3-plan.md`) exists, is implementation-ready, and
  covers §8.1–§8.39.
- A minimal, evidence-preserving doc-alignment note records 4A-C.2 complete /
  unconsumed and 4A-C.3 planning underway, without rewriting historical SHAs or
  closeout evidence (§9).
- The diff is **documentation-only**: no Python/TS/test/migration/OpenAPI/
  generated-type/dependency/config/flag change (§10).
- Alembic head `98289430a3ec` (12 migrations), all flags `False`, resolver
  unconsumed — all reconfirmed.
- Delivered as a DRAFT documentation PR titled
  `docs(phase-4a): plan governed override service (4A-C.3)`; kept draft (no
  review/ready/merge).

---

**Status:** `PLANNING — DOCUMENTATION ONLY — SERVICE NOT IMPLEMENTED —
RESOLVER UNCONSUMED — PERSISTENCE UNUSED — ALL CAPABILITIES REMAIN DARK`
