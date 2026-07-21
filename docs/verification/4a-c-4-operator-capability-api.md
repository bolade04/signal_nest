# 4A-C.4 — Operator Capability-Governance API (Verification & Closeout)

**Phase:** 4A, batch 4A-C.4 (the operator management-API surface of the Phase 4A-C
governed per-workspace capability foundation), closed out by sub-batch **4A-C.4.6**.
**Nature:** additive operator-only routes + Pydantic contracts + tests + additive
OpenAPI/`schema.d.ts` regen + this doc only. No flag flip, no migration, no
`core/config.py` change, no live-gate wiring, no dependency change, no frontend feature,
no capability activation, no real override record.
**Baseline SHA (4A-C.4 fully merged into `main`):**
`808c2af7d3a054fce59bdf897dbec53ed5f81b81` (squash-merge of PR #85, the final route
slice 4A-C.4.5). Local `main == origin/main` at this SHA; worktree clean.
**4A-C.4.6 nature:** documentation-only — this verification doc plus the parent §8.32
progress note. It changes no application code, test, migration, or contract.
**Alembic head:** unchanged single head `98289430a3ec` (12 migrations); no migration
added by any 4A-C.4 sub-batch.

## Scope & non-goals

Phase 4A-C.4 shipped the **operator management API** — the sanctioned read/write surface
over the merged capability control plane (registry → resolver → override service) — under
`/internal/system/capabilities/*`, in ordered, independently-reviewed sub-batches. Every
route lives in the single module `app/system/internal_capabilities_routes.py`, is
operator-gated (`require_operator`: 401 anonymous / 403 non-operator), and returns only
bounded, secret-free governance metadata.

- **4A-C.4.0 (PR #80)** — the 4A-C.4 planning doc (`docs/phase-4a-c-4-plan.md`); no code.
- **4A-C.4.1 (PR #81)** — `GET /capabilities/registry`: a pure projection of the closed
  registry (`iter_capabilities()` + `get_policy()`) in canonical order. Touches no
  database; consumes neither the resolver nor the override service. Router first mounted
  here (`api/router.py`).
- **4A-C.4.2 (PR #82)** — `GET /capabilities/effective`: per-`(capability, workspace)`
  effective state through the merged deny-biased resolver `resolve_capability`. The
  **first sanctioned production consumer of the resolver** — a read, not a live gate.
- **4A-C.4.3 (PR #83)** — `GET /capabilities/overrides`: a tenant-scoped, bounded,
  newest-first page of stored override rows via `list_capability_overrides`. The **first
  sanctioned consumer of the override service**, consuming only its **read** plane.
- **4A-C.4.4 (PR #84)** — `PUT /capabilities/overrides`: records enable/disable intent
  for one `(capability, workspace)` via `set_capability_override`. The **first write
  path**; server-derived actor id; idempotent, audited upsert under the service's
  `SELECT … FOR UPDATE`/SAVEPOINT critical section.
- **4A-C.4.5 (PR #85)** — `DELETE /capabilities/overrides`: clears any recorded override
  via `clear_capability_override` (idempotent delete-or-no-op, single `.cleared` audit).
  The **second write path**.
- **4A-C.4.6 (this batch)** — this consolidated verification/closeout doc + the parent
  §8.32 progress note. Documentation-only.

It deliberately does **not**: flip any global flag; wire the resolver or service into any
live gate (`feedback/routes.py`, `scouting_requests/routes.py`,
`scouting_requests/schedules.py`, `connectors/registry.py`); change `core/config.py`; add
a migration; alter the customer-facing contract shape; ship a frontend feature; touch
PR #34 (OPEN/DRAFT, Dependabot) or PR #6; or create any real override record. The
operator surface ships as the **only** sanctioned consumer of the resolver/service, and
every capability remains dark.

## The operator control surface (five routes, one module)

All five routes are `require_operator`-gated and secret-free:

| Method | Path | Consumes | Plane |
| --- | --- | --- | --- |
| `GET` | `/capabilities/registry` | registry only | stateless read |
| `GET` | `/capabilities/effective` | resolver `resolve_capability` | read |
| `GET` | `/capabilities/overrides` | service `list_capability_overrides` | read |
| `PUT` | `/capabilities/overrides` | service `set_capability_override` | write (upsert) |
| `DELETE` | `/capabilities/overrides` | service `clear_capability_override` | write (clear) |

- **Authorization.** Every route depends on `require_operator` — 401 for anonymous
  callers, 403 for authenticated non-operators, 200 for operators. Authorization tests
  cover all three outcomes per route.
- **Server-derived operator identity.** The set and clear write paths record
  `actor_user_id` from the authenticated operator (`operator.id`), never from the request
  body/query, so no override is recorded anonymously or under a spoofed identity.
- **Authoritative tenant validation + non-enumeration.** The effective, overrides, set,
  and clear routes validate the operator-supplied tenant scope (the workspace must exist
  and be owned by the supplied organization) before touching override state. A
  cross-tenant or absent workspace maps to a **non-enumerating 404** sharing the generic
  `not_found` code, so a caller cannot distinguish "exists but not yours" from "does not
  exist." The effective route validates in-route (`_validate_effective_scope`, which
  mirrors the service's `_validate_tenant` semantics without importing it); the override
  routes delegate validation into the service.
- **HTTP method separation.** Reads are `GET`; the mutation verbs are `PUT` (set/upsert)
  and `DELETE` (clear). The clear route takes its scope + typed `capability` as query
  params (avoiding DELETE-with-body friction, matching the read routes).
- **Set / update behavior.** `PUT` delegates to `set_capability_override`: deny-biased
  registry policy (an `enabled=True` for a non-`workspace_enableable` capability such as
  RSS → 422 with a `.rejected` audit and no row), bounded reason validation (over-length →
  422), unknown-capability → 422 by the typed enum before any service call, and an
  idempotent upsert. `created`/`changed` let the caller distinguish a real write from an
  idempotent re-PUT (which writes no new audit).
- **Clear behavior + idempotent no-op.** `DELETE` delegates to
  `clear_capability_override`: deny-biased and always permitted (removing an override can
  only relax toward the secure default). An existing override is deleted (`changed=True`,
  one `.cleared` audit); an absent override is an idempotent success (`changed=False`)
  writing no row and emitting no audit.
- **Governance delegation.** The routes implement no precedence, policy, persistence, or
  audit logic of their own. Each is a thin adapter that validates scope and delegates
  every decision to the merged resolver/service; the reads open no transaction, and the
  writes open no transaction of their own — they use the request-scoped session so an
  override row and its single audit row commit atomically at the request boundary.
- **Auditability.** Only the write paths emit audit: `set` emits `.created`/`.updated`/
  `.rejected`; `clear` emits `.cleared` (or nothing on a no-op). The reads emit no audit.

## Contract & schema status

Every route-adding sub-batch (4A-C.4.1–4.5) regenerated `openapi.json` + `schema.d.ts`
additively, with an idempotent second run. The additions are the new operator paths and
their bounded, secret-free response/request models
(`CapabilityRegistryOut`/`…ItemOut`, `CapabilityEffectiveListOut`/`…Out`,
`CapabilityOverridePageOut`/`CapabilityOverrideOut`, `CapabilityOverrideSetIn`,
`CapabilityOverrideMutationOut`). The **customer-facing** contract shape is unchanged and
still reports every capability disabled. 4A-C.4.6 adds no path or schema — a contract
regen here is a **zero diff**.

## Validation evidence

### Real-PostgreSQL CI (from the exact merged heads)

Every 4A-C.4 sub-batch merged only after all five required CI jobs (Backend quality,
Container build and security, Frontend quality, Migrations and API contract, Integration
smoke) passed on the exact head against a real `postgres:16` service with
`TEST_POSTGRES_URL` configured (zero PG skips in CI), and each was approved by reviewer
**adesenden** on the exact candidate head before a protected squash merge (no admin
bypass, no auto-merge, no force-push).

| Sub-batch | PR | Head SHA | Merge SHA | Merged (UTC) | Files |
| --- | --- | --- | --- | --- | --- |
| 4A-C.4.0 plan | #80 | — | `5a53c20` | 2026-07-21T03:30:28Z | docs |
| 4A-C.4.1 registry | #81 | `cae7fc50` | `2b0dc63f` | 2026-07-21T04:05:35Z | 5 |
| 4A-C.4.2 effective | #82 | `802deb91` | `6900a5b6` | 2026-07-21T05:11:48Z | 6 |
| 4A-C.4.3 override list | #83 | `1452cdc6` | `e8df914b` | 2026-07-21T05:59:16Z | 6 |
| 4A-C.4.4 override set | #84 | `8dce0677` | `ae479460` | 2026-07-21T09:31:22Z | 4 |
| 4A-C.4.5 override clear | #85 | `39fcad53` | `808c2af7` | 2026-07-21T10:07:38Z | 4 |

- **Final pre-merge CI (PR #85 head `39fcad53`)** — run `29819831466`: all five jobs
  succeeded on the exact candidate head.
- **Post-merge `push` on `main` (merge SHA `808c2af7`)** — run `29821019108`: all five
  jobs succeeded; Backend quality ran against `postgres:16` with `TEST_POSTGRES_URL` set —
  **933 passed, 0 skipped**; Frontend quality — 15 test files / 76 tests + build;
  Integration smoke — 13 checks. The zero-skip Backend collection confirms the
  PostgreSQL-gated cases (including 4A-C.4.5's case-23 end-to-end mutation smoke) were
  executed on the exact merged head, not skipped or deselected.

### Local (4A-C.4.6, documentation-only)

- No application code, test, migration, or contract changed in 4A-C.4.6; there is nothing
  new to run. The authoritative test/CI evidence is the merged-head CI recorded above.
- **PostgreSQL is CI-only, not local.** PostgreSQL-gated tests skip locally when
  `TEST_POSTGRES_URL` is unset; real-PostgreSQL execution is proven **only** by the GitHub
  CI runs above (`29819831466`, `29821019108` and the per-sub-batch runs), never by local
  SQLite.
- **Alembic:** single head `98289430a3ec`; 12 migrations; no migration added by any
  4A-C.4 sub-batch; no drift.
- **Diff shape:** the 4A-C.4.6 change set is exactly this verification doc plus the
  §8.32 progress-note edit in `docs/phase-4a-c-plan.md` — documentation only.

## Consumer-boundary verification

The operator router `app/system/internal_capabilities_routes.py` is the **sole**
production consumer of both the resolver and the override service. The AST import-boundary
guards in `test_capability_override_service.py` (and the resolver's guard suite) enforce
that no live-gate/worker/scheduler/connector module imports
`app.capabilities.resolver` or `app.capabilities.service`; the reframed 4A-C.4 allow-list
recognizes the operator router as the one sanctioned consumer. The operator API is **not a
live gate** — it gates no customer request and toggles no flag.

## Dark-state verification

- **Global flags.** All three global capability flags — `connector_rss_enabled`,
  `scout_scheduling_enabled`, `opportunity_feedback_enabled` — remain `bool = False` in
  `core/config.py` (lines 220 / 235 / 242). Every capability is **dark**.
- **Persistence-vs-activation split.** Recording override intent (`PUT`) or clearing it
  (`DELETE`) is **not activation**. A persisted enable on a `workspace_enableable`
  capability is honored by the resolver **alone** (`decided_by=workspace_override`,
  `effective_enabled=True`) while its bound global flag stays `False` — and because no
  live gate consumes the resolver, nothing is globally activated. Clearing returns
  effective state to the dark global default. RSS is not `workspace_enableable`, so the
  service refuses an enable set and RSS resolves disabled regardless of any set/clear.
- **No live consumer.** No feedback, scheduling, RSS, worker, connector, or scheduler path
  imports or invokes the resolver or the service. The operator API reads/records intent
  but consumes no live product flow.
- **No real record.** No real override row was created; the override table holds only
  throwaway test rows in CI. Absence resolves disabled.

## Governance & safety posture (unchanged)

- Operator-only authorization on every route (401 anon / 403 non-operator), server-derived
  actor attribution on both writes, authoritative tenant validation with non-enumerating
  404 on every tenant-scoped route, bounded and secret-free response/request contracts,
  deny-biased policy enforcement delegated to the merged service, and transactional audit
  on the write paths.
- Single Alembic head `98289430a3ec` preserved; customer contract shape byte-unchanged and
  reporting every capability disabled; no dependency change; PR #34 (Dependabot) and PR #6
  untouched.

## Operational limitations & deferred behavior

- The surface records and reports governance **intent**; it does not — and in Phase 4A-C
  cannot — activate any capability for any customer. The first activation is a separate
  Phase 4B decision (a flag flip + deliberate live-gate wiring), explicitly out of scope
  here.
- No customer-facing UI or route consumes overrides; the operator API is the only surface
  and is internal/operator-gated.
- No rate limiting or bulk-mutation endpoints are introduced; mutations are one
  `(capability, workspace)` pair per request.

## Rollback posture

Each sub-batch merge (PRs #81–#85) is an independent, single-parent revert point on
`main`. Because the surface is additive, operator-gated, and consumes no live flow,
reverting any slice removes only operator-facing governance metadata/mutation capability
and cannot re-activate or de-activate any customer capability (all remain dark regardless).
The feature branches are preserved (not deleted) for each merged PR.

## Closeout status

Phase 4A-C.4 is **complete and merged** through PR #85 (merge SHA
`808c2af7d3a054fce59bdf897dbec53ed5f81b81`), exact-merge-SHA verified by CI run
`29821019108` (all five jobs green, 933 backend tests / 0 skips). The operator capability
control plane — registry read, effective read, override list read, override set, override
clear — is live, operator-gated, secret-free, tenant-isolated, non-enumerating, audited on
writes, and delegates all governance to the merged resolver/service. A single Alembic head
is preserved, the customer contract shape is unchanged, the resolver/service are consumed
only by the operator router (no live gate), all three global flags remain `False`, and no
capability has been activated for any customer. **Every capability remains dark.**

This 4A-C.4.6 deliverable is **documentation-only** and completes the Phase 4A-C.4
closeout: it records the merged-and-verified operator capability control plane and adds no
code, test, migration, contract, configuration, or dependency change. Committing this
documentation activates **no** capability — all three global flags remain `False`, no live
gate consumes the resolver/service, and every capability remains dark. Any later activation
(the first flip of a dark capability) requires separate, explicit authorization under
Phase 4B, which is a separate, later, explicitly-approved batch and is **not** started here.
