# Phase 4A-C.4 — Operator Capability-Override Management API + Contract (Plan)

**Status:** `PLANNING — DOCUMENTATION ONLY — IMPLEMENTATION NOT STARTED — ALL CAPABILITIES REMAIN DARK`

> This is a planning and design document for the final implementation sub-batch of
> Phase 4A-C (the operator management API + contract, parent §8.14/§8.25/§8.26/§8.32).
> It authorizes **no** production code, **no** route, **no** schema, **no** migration,
> **no** contract regeneration, **no** dependency change, **no** flag flip, and **no**
> capability activation. When implemented under this plan across separate approved
> branches, 4A-C.4 ships **dark**: it exposes an operator-only surface to read and
> mutate per-workspace override *intent*, but flips no global flag and wires the
> resolver into no live feature gate — every `(capability, workspace)` still resolves
> `disabled` while the three global flags stay `False`.

## 1. Status and safety posture

- **Phase:** 4A, batch 4A-C, sub-batch **4A-C.4** — Operator Capability-Override
  Management API + Contract.
- **Nature:** additive backend HTTP surface only — one new operator-gated router module
  (`app/system/internal_capabilities_routes.py`), additive Pydantic response/request
  schemas, one router registration line, an **additive** OpenAPI/type regeneration, and
  backend tests. No new business logic (the registry, resolver, model, migration, and
  set/clear/read service already exist and are merged); this batch only *exposes* the
  merged service and resolver through an operator-gated API.
- **Safety posture:** deny-biased and dark. The API is `require_operator`-gated (no
  customer path). Mutations flow **only** through the merged
  `set_capability_override` / `clear_capability_override`, preserving their tenant
  validation, policy gate, audit, and `FOR UPDATE`/SAVEPOINT concurrency. The
  effective-state read flows through the merged `resolve_capability`. No global flag is
  flipped; no live feature gate (feedback/scheduling/RSS) consumes the resolver.
- **This planning PR implements nothing** (see §34).

## 2. Background and verified baseline

- **Baseline:** `main` at `b38fb4a8e2c29a4d04496c12ab7c54d216e3cd09` (Phase 4A-C.3.6
  squash-merge of PR #79); `origin/main` equal; worktree clean; 0/0 ahead/behind.
- **Post-merge CI:** run `29796050281` (`push`, exact merge SHA) — all five jobs
  succeeded; Backend quality ran against a healthy `postgres:16` with
  `TEST_POSTGRES_URL` set: `821 passed, 0 skipped`.
- **Alembic:** single head `98289430a3ec`; 12 migrations; `alembic check` clean.
- **Merged prerequisites (all satisfied):**
  - 4A-C.1 — registry (`app/capabilities/registry.py`), override model
    (`app/capabilities/models.py`), additive migration. Table
    `workspace_capability_overrides` exists with the unique constraint
    `uq_workspace_capability_override` and check constraint
    `ck_workspace_capability_override_capability`.
  - 4A-C.2 — deny-biased resolver (`app/capabilities/resolver.py`):
    `resolve_capability(*, session, settings, capability, organization_id, workspace_id)`
    → `CapabilityResolution` with `DecisionSource`. Unconsumed by any live gate.
  - 4A-C.3.1–3.6 — typed errors (`app/capabilities/errors.py`), result models
    (`app/capabilities/results.py`), and the full override service
    (`app/capabilities/service.py`): `get_capability_override`,
    `list_capability_overrides`, `set_capability_override`,
    `clear_capability_override`, with authoritative tenant validation, deny-biased
    policy enforcement, bounded reason validation, idempotent upsert/clear,
    transactional audit, and `SELECT … FOR UPDATE` + `begin_nested` SAVEPOINT
    concurrency (real-PostgreSQL proven).
- **Not yet satisfied (this batch delivers it):** an operator-facing HTTP surface to
  read the registry, read effective per-workspace state, and read/set/clear overrides.
  The service and resolver currently have **no consumer at all**.

## 3. Exact objective

Expose the merged capability resolver and override service through a **new,
operator-gated, additive** API under the established `/internal/system/*` tier, and
regenerate the contract additively — matching parent plan §8.14. Concretely, ship the
five routes:

```
GET    /internal/system/capabilities/registry    -> the governable capability set (static)
GET    /internal/system/capabilities/effective    -> effective (capability, workspace) state via the resolver
GET    /internal/system/capabilities/overrides     -> operator listing of stored override rows (paged)
PUT    /internal/system/capabilities/overrides     -> set/upsert one override (audited) — the first new write path
DELETE /internal/system/capabilities/overrides     -> clear one override (audited)
```

All five require `require_operator`; PUT/DELETE are the only new write paths and never
flip a global flag or change any live gate decision.

## 4. In-scope behavior

- A new router module `app/system/internal_capabilities_routes.py` (mirroring
  `app/system/internal_observability_routes.py`), registered in `app/api/router.py`
  after `internal_observability_router`.
- Additive operator-safe Pydantic schemas: `CapabilityRegistryItemOut`,
  `CapabilityRegistryOut`, `CapabilityEffectiveOut`, `CapabilityOverrideOut`,
  `CapabilityOverridePageOut`, and request bodies `CapabilityOverrideSetIn` /
  `CapabilityOverrideClearIn` (recommended shapes in §17).
- The five routes above, each `Depends(require_operator)`, delegating to the merged
  service/resolver — no new persistence, decision, or audit logic in the route layer.
- Bounded pagination on the two list reads (clamped `limit`/`offset`, matching 4A-B).
- Additive contract regeneration (`openapi.json` + `schema.d.ts`).
- Backend tests: operator authorization (401/403), route bounds, secret-free responses,
  dark-by-default effective read, policy-rejection mapping, tenant-mismatch mapping,
  set/clear round-trip, and a real-PostgreSQL end-to-end mutation smoke.
- A reframed dark-state / no-live-consumer guard suite (§26) plus a verification doc.

## 5. Explicit non-goals

- **N1.** No wiring of the resolver into any live feature gate — `feedback/routes.py`,
  `scouting_requests/routes.py`, `scouting_requests/schedules.py`,
  `connectors/registry.py` remain byte-for-byte unchanged. (Parent §8.16; acceptance
  #25.)
- **N2.** No global-flag change — `connector_rss_enabled`, `scout_scheduling_enabled`,
  `opportunity_feedback_enabled` remain `False` in `core/config.py`. (Acceptance #26.)
- **N3.** No capability activation for any customer; no Phase 4B work.
- **N4.** No change to the customer `/system/capabilities` contract *shape*
  (`system/routes.py`). (Parent §8.15/N5; acceptance #28.)
- **N5.** No migration; no model/registry/resolver/service source change (the service is
  already the sole write path). Alembic head and count unchanged.
- **N6.** No operator **frontend** (that is the later 4A-D batch). The generated
  `schema.d.ts` regenerates additively, but no `apps/web` component/page is authored.
- **N7.** No dependency change; no Dependabot PR touched; PR #34 untouched and not
  assumed to merge.
- **N8.** No new metric name/label beyond existing bounded policy (parent §8.19); no
  caching (parent §8.17).

## 6. Current architecture

- **Global flags** (`core/config.py:220/235/242`) are the only real gate today; the
  three live enforcement points read them directly (feedback route guard, scheduling
  route guard + inert tick, RSS connector selection).
- **Capability control plane (merged, unconsumed):** registry → resolver → override
  model/migration → override service. The service raises `SignalNestError` subclasses
  (`CapabilityOverrideNotPermittedError` → 422 `capability_override_not_permitted`;
  `CapabilityTenantMismatchError` → 404 `not_found`, non-enumerating) and returns typed
  results (`OverrideMutation`, `OverridePage`) / ORM rows.
- **Operator tier (established):** `/internal/system/*`, `Depends(require_operator)`
  (401 anonymous, 403 non-operator), secret-free Pydantic outputs
  (`internal_observability_routes.py` is the template).
- **No capability API exists yet.** A repo search finds `internal_capabilities_routes`
  and the `/capabilities/{registry,effective,overrides}` paths only in the planning docs.

## 7. Proposed architecture

Three thin, additive changes, backend-only:

1. **`app/system/internal_capabilities_routes.py`** — a new `APIRouter(prefix=
   "/internal/system", tags=["internal"])` (or a `/internal/system/capabilities`
   sub-prefix) holding the five routes, each `Depends(require_operator)` +
   `Depends(get_db)`, delegating to the merged resolver/service. Static sub-paths only
   (`/registry`, `/effective`, `/overrides`), no path parameters, so route ordering is
   unambiguous.
2. **Additive schemas** — operator-safe Pydantic models (bounded enums/booleans/safe
   ids + the non-secret `reason`), declared alongside the routes (house style).
3. **`app/api/router.py`** — one import + one entry appended to the `include_router`
   loop, after `internal_observability_router`.

The route layer is a pure adapter: it converts operator-supplied scope + typed
`Capability` into service/resolver calls and projects typed results into secret-free
schemas. It adds no decision, persistence, or audit logic.

## 8. Trust and tenant boundaries

- **Operator-only.** Every route is `require_operator` — a server-controlled
  `user.is_operator` attribute, never client-derived. No tenant role can reach these
  routes. (Acceptance #20.)
- **Cross-tenant by design, tenant-validated per call.** Like `internal_jobs_list`, the
  operator supplies `organization_id` + `workspace_id`; the merged service
  authoritatively validates workspace ownership (`_validate_tenant`) before any read or
  write, and a cross-tenant workspace maps to a non-enumerating 404. The route must pass
  the operator-supplied ids **verbatim** to the service/resolver — never an implicit
  "current org."
- **Effective read tenancy.** `resolve_capability` applies deny-biased in-memory tenant
  validation (`_load_override` returns `None` on org mismatch). The effective route must
  pass both ids so a stored override for org A is never rendered under org B.

## 9. Resolver / service / consumer relationship

- **This batch introduces the FIRST sanctioned consumer** of both the resolver and the
  override service: the operator management API. This is the central design point.
- **It is not a live gate.** A live *gate* changes customer-visible runtime behavior
  (feedback/scheduling/RSS). The operator management route only reads/writes override
  *intent* and reads effective state; it gates no customer request and flips no flag.
- Therefore, after 4A-C.4: "resolver unconsumed **by live gates**" and "service has no
  **live-gate** consumer" remain true, but "service/resolver has **no consumer at
  all**" becomes false — by design (parent acceptance #23 *requires* effective reads to
  flow through the resolver). The 4A-C.3.6 AST guard
  (`test_no_production_module_imports_the_override_service`), which forbade *any*
  production import, MUST be reframed to allow exactly the operator route while still
  failing if any live-gate/worker/scheduler/connector imports it (§26). The
  resolver-unconsumed guard (`test_resolver_remains_unconsumed_by_live_gates`) already
  scans only the four live-gate files and stays valid **unchanged**.

## 10. Capability evaluation order

Unchanged — owned entirely by the merged `resolve_capability` / `decide_capability`
(parent §8.8): safety ceiling → honored workspace override → global configuration →
secure default. The route surfaces the resolver's `decided_by`, `global_flag`,
`has_override`, and `override_value` so an operator can always see *why*. The route
implements no precedence of its own.

## 11. Global-flag and override precedence

Unchanged. The effective read reports `global_flag` (the bound `*_enabled` value) and
`decided_by` separately so the operator surface **distinguishes persistence from
activation**: an enabled override on a `workspace_enableable` capability shows
`has_override=True, decided_by=workspace_override, effective_enabled=True` while its
`global_flag` stays `False` — i.e. persisted intent that the resolver alone would honor,
with no live gate consuming it, so nothing is globally active.

## 12. Deny / fail-closed behavior

- **No fail-open in the route.** Service/resolver errors propagate to the standard
  `SignalNestError` envelope; no route has a broad `except` that could return
  enabled/permissive. The effective read reflects the resolver's deny-biased result.
- **Unknown capability → 422** before any service call, by typing the request/query
  `capability` field as the `Capability` `StrEnum` (Pydantic rejects an unknown value
  with 422 automatically; no stored row).
- **Policy denial → 422**: an `enabled=True` PUT for RSS (not `workspace_enableable`) is
  rejected by the merged `set_capability_override` (`capability_override_not_permitted`);
  the route must not bypass it (no direct row writes).
- **Tenant mismatch / absent workspace → 404** (non-enumerating), from the service.

## 13. Registry-policy enforcement

The route never re-implements policy. It relies on:
- the registry-typed `Capability` field (closed allow-list → unknown 422),
- `set_capability_override`'s deny-biased `workspace_enableable`/`workspace_disableable`
  gate (RSS enable → 422),
- the DB `CheckConstraint` as the final backstop.
The `GET /registry` route is a pure projection of `iter_capabilities()` + `get_policy()`
(label, `global_flag_attr`, `workspace_enableable`, `workspace_disableable`,
`future_activation_phase`) — read-only, no DB.

## 14. Audit and observability requirements

- **Writes:** PUT/DELETE inherit the service's existing audit — exactly one `AuditLog`
  per real change (`workspace_capability_override.created` / `.updated` / `.rejected` /
  `.cleared`) with bounded, secret-free `previous_state`/`new_state`; idempotent no-ops
  and absent-clears write none. The route adds **no** new audit. (Acceptance #18, #21.)
- **Reads:** the three GET routes write no audit rows. (Acceptance #21.)
- **Logging:** the service already emits bounded `log_event` records; the route adds
  none beyond the standard request/correlation middleware. No new metric.

## 15. Transaction requirements

Unchanged — the route uses the request-scoped `Session` (`Depends(get_db)`); the merged
service flushes but never commits, so the override row and its audit row commit
atomically at the request boundary (parent §8.13/§8.20). The route opens no nested
transaction and performs no manual commit.

## 16. Concurrency behavior

Unchanged — `set_capability_override` / `clear_capability_override` already acquire a
`SELECT … FOR UPDATE` workspace-row lock (real-PostgreSQL proven) backed by the unique
constraint + `begin_nested` SAVEPOINT retry. The route adds no concurrency logic. A
route-level real-PostgreSQL smoke (§25) proves the end-to-end path preserves the
single-row + audit invariant.

## 17. API and schema impact (additive; recommendations labeled)

**Response schemas (operator-safe; bounded enums/booleans/safe ids + non-secret
`reason`):**

- `CapabilityRegistryItemOut` — `{capability: Capability, label: str,
  global_flag_attr: str, workspace_enableable: bool, workspace_disableable: bool,
  future_activation_phase: str}` (projection of `CapabilityPolicy`; **recommendation:**
  omit `global_flag_attr` if the reviewer considers the internal attribute name
  operator-noise — it is non-secret but internal; default is to include it for
  explainability).
- `CapabilityRegistryOut` — `{items: list[CapabilityRegistryItemOut]}`.
- `CapabilityEffectiveOut` — the `CapabilityResolution` projection:
  `{capability, workspace_id, effective_enabled, decided_by: DecisionSource,
  global_flag, has_override, override_value}`. Secret-free by construction.
- `CapabilityOverrideOut` — the stored-row projection:
  `{id, organization_id, workspace_id, capability, enabled, reason, set_by_user_id,
  created_at, updated_at}` (all non-secret; `reason` is the bounded operator note).
- `CapabilityOverridePageOut` — `{items: list[CapabilityOverrideOut], total, limit,
  offset}`.

**Request bodies:**

- `CapabilityOverrideSetIn` (PUT body) — `{organization_id: str, workspace_id: str,
  capability: Capability, enabled: bool, reason: str | None = None}`.
- Clear input — **recommendation:** to avoid DELETE-with-body client friction, take
  `organization_id`, `workspace_id`, `capability` as `Query` params on DELETE (matching
  the operator list-read style); a symmetric JSON body is the alternative. Flagged in
  §33 as an open decision (naming/ergonomics only; both are operator-gated and
  behavior-identical).

**Status mapping:** 200 reads; 200 PUT (upsert result), 200/204 DELETE (idempotent —
**recommendation:** 200 with an `OverrideMutation` projection so the caller learns
`changed`); 401 anonymous; 403 non-operator; 404 tenant mismatch/absent workspace; 422
unknown capability / policy denial / over-length reason.

**Contract:** `npm run gen:types` regenerates `apps/api/openapi.json` +
`apps/web/src/api/schema.d.ts` **additively** (new operator paths + schemas only; zero
removals; idempotent second run). Customer `FeatureFlagsOut`/`RuntimeSummaryOut` shapes
byte-unchanged.

## 18. Frontend impact

None authored. The only `apps/web` change is the **generated** `schema.d.ts` additive
regeneration (a contract artifact, required to keep the CI contract gate green). No
component, page, hook, or route is added — the operator UI is the later 4A-D batch.

## 19. Worker / scheduler / connector impact

None. No worker, scheduler, or connector path is touched or imports the new router. The
three live enforcement points are byte-for-byte unchanged.

## 20. Migration impact

None. The `workspace_capability_overrides` table and its constraints already exist.
Alembic head stays `98289430a3ec`; migration count stays 12; `alembic check` stays
clean. This batch adds no migration.

## 21. Contract-generation impact

Additive only, and **per sub-batch that adds a route** (the CI "Migrations and API
contract" job runs `npm run gen:types` then `git diff --exit-code` on `openapi.json` +
`schema.d.ts`, so every route-adding PR must ship the regenerated artifacts). Second run
idempotent; customer contract shape unchanged.

## 22. Feature-flag and rollout strategy

- **Flags:** all three remain `False` throughout; 4A-C.4 flips none.
- **Rollout to customers:** none. The "rollout" is making the operator management API
  available. At the end of 4A-C.4 the customer-visible state is identical to today (all
  dark); operators can durably, auditably read and record per-workspace override intent
  and inspect effective state + the deciding rule.
- **Enablement of the surface:** the routes are live as soon as merged, but gated to
  operators and inert for customers; no capability becomes active.

## 23. Rollback strategy

- **Code:** purely additive (new router module, new schemas, one router-registration
  line, regenerated contract, new tests). Reverting the merge removes the routes and
  regenerates the contract to its prior additive state. Nothing else depends on the new
  router.
- **Data:** none created outside throwaway test rows. Clearing any override row (or a
  later downgrade — unchanged here) returns to the empty, all-dark default.
- **Per sub-batch:** each sub-batch is an independent revert point; reverting the
  mutation-route sub-batches leaves the read routes intact and vice versa.
- **No destructive change:** no table/column/flag altered; single Alembic head preserved.

## 24. Test matrix (numbered)

Backend, self-contained SQLite (`get_db` overridden, `PRAGMA foreign_keys=ON`), operator
vs non-operator users via `create_access_token`, following the 4A-B / feedback suites,
except where a case is marked **[PG]** (real PostgreSQL, gated on `TEST_POSTGRES_URL`).

1. `GET /registry` returns exactly `iter_capabilities()` with correct policy fields;
   read-only; writes no audit.
2. `GET /registry` → 401 anonymous, 403 non-operator.
3. `GET /effective` with shipped defaults reports every `(capability, sample workspace)`
   `effective_enabled=False`, `decided_by=global_configuration`, `global_flag=False`
   (dark-by-default).
4. `GET /effective` reflects a service-persisted enable: after a PUT enable for
   `opportunity_feedback`, effective shows `has_override=True,
   decided_by=workspace_override, effective_enabled=True`, `global_flag=False`
   (persistence-vs-activation split).
5. `GET /effective` for an RSS enable attempt: PUT is rejected (case 12), so RSS effective
   stays `False`.
6. `GET /effective` per-workspace / per-capability filter narrows correctly; tenant
   mismatch → 404 (non-enumerating).
7. `GET /effective` → 401 anonymous, 403 non-operator.
8. `GET /overrides` returns a tenant-scoped, clamped, newest-first page; out-of-range
   `limit`/`offset` clamp (or 422 per `Query` bounds); writes no audit.
9. `GET /overrides` cross-tenant workspace → 404; another workspace's rows never appear.
10. `GET /overrides` → 401 anonymous, 403 non-operator.
11. `PUT /overrides` insert then idempotent re-PUT: first `created=True, changed=True`
    with exactly one `AuditLog` `.created`; identical re-PUT `changed=False` with no new
    audit; changed re-PUT `.updated`.
12. `PUT /overrides` RSS `enabled=True` → 422 `capability_override_not_permitted`; no row;
    a `.rejected` audit row is written by the service.
13. `PUT /overrides` unknown capability value → 422 (Pydantic enum rejection); no service
    call, no row.
14. `PUT /overrides` over-length `reason` → 422; no row.
15. `PUT /overrides` cross-tenant/absent workspace → 404 (non-enumerating).
16. `PUT /overrides` → 401 anonymous, 403 non-operator.
17. `DELETE /overrides` removes an existing override (`changed=True`, one `.cleared`
    audit); effective returns to dark default.
18. `DELETE /overrides` on an absent override is an idempotent success (`changed=False`,
    no audit).
19. `DELETE /overrides` → 401 anonymous, 403 non-operator; tenant mismatch → 404.
20. Four-market isolation (Dallas / London / Lagos / Nairobi): an override set via PUT in
    one workspace never changes another market's effective read.
21. Secret-free: no response field carries a URL, credential, payload, token, trace/
    correlation id, or worker identity; only bounded enums/booleans/safe ids + `reason`.
22. Route ordering / method separation: GET/PUT/DELETE on `/overrides` dispatch
    correctly; static sub-paths never collide.
23. **[PG]** End-to-end mutation smoke on real PostgreSQL: PUT then GET effective then
    DELETE preserves the single-row + audit invariant (proves the route path over the
    `FOR UPDATE`/SAVEPOINT service on the real dialect).
24. Reframed guards (§26): the no-live-consumer guard allows exactly the operator router
    and still fails if any live-gate/worker/scheduler/connector imports the service or
    resolver.
25. Contract: `npm run gen:types` yields only additive operator-only schema/paths;
    idempotent second run; customer `/system/capabilities` shape byte-unchanged.

## 25. Real-PostgreSQL verification requirements

- Case 23 runs under `@pytest.mark.skipif(not os.getenv("TEST_POSTGRES_URL"), ...)`,
  mirroring the 4A-C.3.5 `_pg_only` gate, and asserts `engine.dialect.name ==
  "postgresql"`.
- The merged concurrency proofs (three `_pg_only` convergence tests + the always-run
  `FOR UPDATE` compile proof) remain in place and continue to execute in CI.
- Execution is CI-only (`TEST_POSTGRES_URL` unset locally). Proof is the CI collection
  count with **zero** skips on the exact head, exactly as recorded for 4A-C.3.5/3.6.
- **No SQLite-only claim of PostgreSQL behavior.** Any statement about PostgreSQL
  execution cites the specific CI run.

## 26. Dark-state and activation guards (reframed)

The safety-critical edit of this batch. The 4A-C.3.6 guard
`test_no_production_module_imports_the_override_service` forbade **any** production
import of the service — correct while the service had no route. 4A-C.4 intentionally adds
the first consumer, so:

- **Reframe** the service-consumer guard to a **live-gate** guard: parse every production
  module and assert none of the *live-gate / worker / scheduler / connector* set
  (`feedback/routes.py`, `scouting_requests/routes.py`, `scouting_requests/schedules.py`,
  `connectors/registry.py`, plus the job/worker modules) imports `app.capabilities.service`
  or `app.capabilities.resolver`; and assert the **only** production consumer of the
  service/resolver is `app/system/internal_capabilities_routes.py` (an explicit
  allow-list of one). A self-check ensures the scan actually covers the live-gate set and
  the operator router.
- **Keep unchanged** `test_resolver_remains_unconsumed_by_live_gates` — it already scans
  only the four live-gate files (which stay clean); the operator router is deliberately
  not in that set.
- **Dark-state coupling** retained: with shipped defaults every capability resolves
  disabled via `global_configuration`; a persisted enable is honored by the resolver
  alone (`workspace_override`) while its `global_flag` stays `False`; RSS can never be
  raised by an override; clearing returns to the dark default.

## 27. CI requirements

Every 4A-C.4 sub-batch PR must:
- Pass all five required jobs (Frontend quality, Backend quality, Migrations and API
  contract, Container build and security, Integration smoke).
- Keep a single Alembic head; `alembic check` clean (no migration added).
- Produce no contract drift beyond additive operator-only schema (`gen:types` +
  `git diff --exit-code` on `openapi.json` + `schema.d.ts`).
- Run Backend quality against real `postgres:16` with `TEST_POSTGRES_URL`; zero skips on
  the exact head.
- Merge only via the protected squash workflow (1 approval, stale-approval dismissal,
  approval-after-push, thread resolution, zero bypass); exact-merge-SHA verified.

## 28. Documentation / runbook requirements

- A verification/closeout doc `docs/verification/4a-c-4-operator-capability-api.md`
  (evidence: ruff, pytest counts, single head, additive+idempotent contract, dark-state,
  reframed guards, real-PostgreSQL CI run ids).
- A minimal, factual progress note appended to parent `docs/phase-4a-c-plan.md` §8.32 for
  4A-C.4 (§7 of this plan; documentation-status only, not "implemented").
- **Recommendation:** a short operator runbook stub describing set/clear/effective usage
  and the persistence-vs-activation distinction, so an operator dry-run (parent §8.30
  Phase 4B entry) has a reference. Flagged as recommendation, not a requirement.

## 29. Sub-batch sequence (recommended)

Smallest independently-reviewable slices, each one security/behavior boundary, each
shipping dark, each independently committable and revertible. (A leaner 3-batch
alternative — reads / mutations / verification — is acceptable per parent §8.28 Q2; the
6-way split below is the recommended default and mirrors the 4A-C.3.x cadence.)

- **4A-C.4.1 — Router skeleton + registry read.** New `internal_capabilities_routes.py`
  with `GET /registry` only (pure registry projection; no DB, no resolver, no service),
  router registration, schemas for the registry, contract regen, operator-auth tests.
  Consumes neither resolver nor service → dark-state/no-consumer guards **unchanged and
  green**. The cleanest first slice.
- **4A-C.4.2 — Effective-state read (first resolver consumer).** `GET /effective` +
  `CapabilityEffectiveOut`; **reframe** the guard suite (§26) to allow the operator
  router as a resolver consumer while keeping live gates forbidden; dark-by-default and
  persistence-vs-activation tests; contract regen.
- **4A-C.4.3 — Override list read (first service-read consumer).** `GET /overrides` +
  `CapabilityOverrideOut`/`CapabilityOverridePageOut`; extend the reframed guard to allow
  the service-read import; bounds/clamp, tenant-scope, secret-free tests; contract regen.
- **4A-C.4.4 — Override set (PUT) — first write path.** `PUT /overrides` +
  `CapabilityOverrideSetIn`; delegates to `set_capability_override`; policy-rejection
  (422), unknown-capability (422), over-length reason (422), tenant-mismatch (404),
  idempotent-upsert, audit-row tests; contract regen.
- **4A-C.4.5 — Override clear (DELETE).** `DELETE /overrides`; delegates to
  `clear_capability_override`; idempotent-clear + audit tests; **[PG]** end-to-end
  mutation smoke (case 23); contract regen.
- **4A-C.4.6 — Verification doc + closeout.** `docs/verification/4a-c-4-...md`,
  consolidated reframed guard suite, dark-state proof, contract idempotency, parent §8.32
  progress note; draft PR.

### Per-sub-batch skeleton (applies to each above)

- **Objective / production behavior:** as named.
- **Files changed:** `internal_capabilities_routes.py` (+ `api/router.py` in 4A-C.4.1
  only), the relevant test module, and — for every route-adding slice — `openapi.json` +
  `schema.d.ts`.
- **Files that must remain unchanged:** `capabilities/{registry,resolver,models,
  service,errors,results}.py`, `core/config.py`, the four live-gate modules,
  `system/routes.py`, all migrations.
- **PostgreSQL:** none until 4A-C.4.5's case 23; concurrency proofs unchanged.
- **Migration:** none in any sub-batch.
- **Contract:** additive regen in every route-adding sub-batch; idempotent.
- **Flag / consumer state:** flags `False` throughout; resolver/service consumed only by
  the operator router; no live gate consumes either.
- **Entry criteria:** prior sub-batch merged + exact-SHA green (or `main` at 4A-C.3.6 for
  4A-C.4.1); worktree clean; single head; flags `False`.
- **Exit criteria:** route(s) live and operator-gated; tests green; contract additive +
  idempotent; guards green (reframed where noted); dark.
- **Rollback point:** the sub-batch merge is an independent revert point.
- **Dependencies:** 4A-C.4.2 depends on 4A-C.4.1 (shared module); 4A-C.4.3–4.5 depend on
  the reframed guard from 4A-C.4.2; 4A-C.4.6 depends on all.
- **Remains dark / independently committable:** yes to both.

## 30. Per-sub-batch acceptance criteria

Each sub-batch: (a) its named route(s) live and `require_operator` (401/403 tested);
(b) additive contract regen with idempotent second run; (c) single Alembic head,
`alembic check` clean; (d) all three flags `False`; (e) no live-gate/worker/scheduler/
connector consumes the resolver or service; (f) ruff clean; (g) all five CI jobs green
on the exact head with zero PG skips; (h) secret-free responses; (i) PR #34 / Dependabot
untouched; (j) dark — no capability operationally active.

## 31. Full Phase 4A-C.4 acceptance criteria

1. [ ] The five routes exist under `/internal/system/capabilities/*`, each
   `require_operator` (401 anonymous, 403 non-operator).
2. [ ] `GET /registry` projects exactly the closed registry with correct policy fields;
   no DB, no audit.
3. [ ] `GET /effective` flows through `resolve_capability` (never raw rows) and reports
   `effective_enabled`, `decided_by`, `global_flag`, `has_override`, `override_value`.
4. [ ] With shipped defaults every `(capability, workspace)` effective read is disabled
   via `global_configuration`; all flags `False`.
5. [ ] A persisted enable is reflected as `workspace_override` with `global_flag=False`
   (persistence-vs-activation split); RSS enable is refused and stays disabled.
6. [ ] `GET /overrides` is tenant-scoped, clamped, newest-first; writes no audit.
7. [ ] `PUT /overrides` delegates to `set_capability_override`: idempotent upsert, policy
   denial → 422 `capability_override_not_permitted`, unknown capability → 422, over-length
   reason → 422, tenant mismatch/absent → 404; exactly one `AuditLog` per real change.
8. [ ] `DELETE /overrides` delegates to `clear_capability_override`: deletes when present
   (one `.cleared` audit), idempotent success when absent (no audit).
9. [ ] Override row + audit row commit atomically (request-scoped session/flush).
10. [ ] Four-market isolation holds for effective reads, list reads, and mutations.
11. [ ] Read routes write no audit rows.
12. [ ] Operator responses are secret-free (no URL/credential/payload/token/trace id).
13. [ ] Customer `/system/capabilities` contract shape byte-unchanged; reports every
   capability `false` while dark.
14. [ ] Contract regeneration yields only additive operator-only paths/schemas;
   idempotent second run.
15. [ ] No live feature gate consumes the resolver; the only production consumer of the
   resolver/service is the operator router (reframed guard passes).
16. [ ] No global flag flipped; all three remain `False`.
17. [ ] Single Alembic head `98289430a3ec`; 12 migrations; `alembic check` clean; no
   migration added.
18. [ ] `ruff` clean; full backend + frontend suites green; all five CI jobs green with
   zero PG skips on the exact head; real-PostgreSQL end-to-end mutation smoke executes.
19. [ ] PR #34, Dependabot PRs, and all dependencies untouched.
20. [ ] Verification doc present; parent §8.32 progress note added (documentation-only).

## 32. Preserved-work boundaries

Must NOT change: `capabilities/{registry,resolver,models,service,errors,results}.py`;
the migration set; `core/config.py` (no flag change); the four live-gate modules;
`system/routes.py` (customer contract shape); the 4A-B observability routes; PR #34
(`feat/phase-3b-live-rss-controlled-egress`); any Dependabot PR; any dependency version;
branch protection / rulesets. The `test_resolver_remains_unconsumed_by_live_gates` guard
stays green unchanged.

## 33. Known risks and unresolved decisions

**Risks → prevention → verification** (specific to the API layer):

- **Guard obsolescence when the first consumer lands** → reframe to a live-gate guard +
  single-consumer allow-list (§26) → reframed guard test + self-check (case 24).
- **Fail-open in the route** → no broad `except`; propagate `SignalNestError`; effective
  read reflects deny-biased resolver → error-path tests (cases 12–15, 6).
- **Policy/flag bypass at the route** → route calls only the merged service (no direct
  row writes); typed `Capability` field → RSS-enable 422 (case 12), unknown 422 (case 13).
- **Wrong org/workspace context** → pass operator-supplied ids verbatim; service
  `_validate_tenant` → tenant-mismatch 404 + isolation tests (cases 9,15,19,20).
- **Cross-tenant leakage on effective read** → pass both ids to the resolver → isolation
  test (case 20).
- **Stale reads** → no caching (parent §8.17); resolver reads live → set-then-effective
  (case 4).
- **Monitoring can't tell persistence from activation** → expose `decided_by` +
  `global_flag` + `has_override` → case 4.
- **Contract drift** → additive-only; CI `git diff --exit-code`; idempotent → case 25.
- **SQLite-passes-PostgreSQL-fails** → real-PG end-to-end smoke (case 23) + retained
  concurrency proofs.
- **Accidental live-gate wiring / flag flip** → §32 preserved set; acceptance #15/#16;
  reframed guard.
- **Hidden PR #34 dependency** → the router imports only merged capability modules; PR
  #34 not assumed to merge.
- **DELETE-with-body client friction** → recommend `Query` params for DELETE (§17).

**Unresolved decisions:**

- **Q1 — DELETE input shape:** `Query` params (recommended) vs symmetric JSON body.
- **Q2 — Route sub-prefix:** flat `/internal/system` router with
  `/capabilities/{registry,effective,overrides}` paths (matches the existing
  `/internal/system/capabilities` topology read) vs a dedicated
  `APIRouter(prefix="/internal/system/capabilities")`. Naming only.
- **Q3 — Expose `global_flag_attr` on the registry read?** Non-secret internal attribute;
  default include for explainability, reviewer may drop.
- **Q4 — DELETE success code:** 200 with `OverrideMutation` projection (recommended, so
  the caller learns `changed`) vs 204.
- **Q5 — 6-batch vs 3-batch decomposition (§29):** default 6; reviewer may consolidate.
- **Q6 — Operator runbook stub (§28):** recommended, not required.

**Decision-freeze points (when each must be settled — recommendations remain
recommendations until then, never silently promoted to approved requirements):**

- **Q5** must be frozen **before 4A-C.4.1 opens** — it sets the branch/PR count for the
  whole batch.
- **Q2** must be frozen **before 4A-C.4.1 opens** — the router module's prefix is fixed
  at creation and cannot drift between sub-batches.
- **Q1** and **Q4** must be frozen **before 4A-C.4.5 opens** (the DELETE sub-batch), since
  they shape the DELETE request/response contract that is regenerated there.
- **Q3** must be frozen **before 4A-C.4.1 opens** — `GET /registry` and its schema ship in
  that sub-batch.
- **Q6** must be frozen **before 4A-C.4.6 opens** (verification/closeout), where the
  optional runbook stub would land.

None of Q1–Q6 blocks *planning*; each has a safe recommended default and a named freeze
point above. The authoritative scope (parent §8.14) fully defines the batch. No
recommendation in this plan is an approved requirement until frozen by the reviewer at
its stated point.

## 34. Not implemented by this planning PR

This PR creates only planning documentation: this file and a minimal §8.32 progress note
in `docs/phase-4a-c-plan.md`. It writes **no** production code, **no** route, **no**
schema, **no** test, **no** migration, **no** contract regeneration, **no** dependency or
configuration change; it flips **no** flag, activates **no** capability, wires the
resolver into **no** live gate, and touches **no** other PR or branch. Implementation of
4A-C.4.1–4.6 is deferred to separate, explicitly-approved branches. At the close of this
planning PR: `main` unchanged except for these docs, all three flags `False`, the
resolver unconsumed by any live gate, the override service still consumer-free (no route
merged yet), Alembic head `98289430a3ec`, 12 migrations, and every capability dark.

---

**Status:** `PLANNING — DOCUMENTATION ONLY — IMPLEMENTATION NOT STARTED — ALL CAPABILITIES REMAIN DARK`
