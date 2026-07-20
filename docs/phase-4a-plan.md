# Phase 4A — Operator Observability and Controlled Activation Foundation

**Status:** `PLANNING — DOCUMENTATION ONLY — IMPLEMENTATION NOT STARTED — ALL CAPABILITIES REMAIN DARK`

> This is a planning and design document. It authorizes no code, no migration, no
> contract change, no dependency change, and no feature-flag activation. Phase 4A,
> when implemented under a separate approved plan and separate branches, ships
> **dark**: it turns nothing on. The first real activation of any dark capability
> is deferred to Phase 4B and is a separate, explicitly-approved decision.

Baseline for this plan: `main` at `16c0d283f7b9d1717afac32aa9e5f54a311cc12b`
(Phase 3 closeout, PR #62), single Alembic head `4945b98229e6`, and the three
product flags all `False`:
`opportunity_feedback_enabled`, `scout_scheduling_enabled`, `connector_rss_enabled`
(`apps/api/app/core/config.py:220,235,242`).

---

## 8.1 Title and status

- **Phase:** 4A — Operator Observability and Controlled Activation Foundation.
- **Theme:** "Controlled Activation and Operability" — build the operator-facing
  visibility and the per-workspace activation-control machinery that must exist
  *before* any dark capability is ever enabled, then stop. Phase 4A enables nothing.
- **Nature:** additive backend observability surfaces, an additive per-workspace
  capability-override model (dark, deny-biased), an operator-only frontend
  observability view, tests, and docs.
- **Explicitly out of scope:** enabling feedback, scheduling, or RSS; any customer
  activation; any change to Phase 3 dark behavior.
- **Status:** `PLANNING — DOCUMENTATION ONLY`.

## 8.2 Executive summary

Phase 3 delivered three verified-but-dark capabilities — opportunity feedback,
scout scheduling, and the live-RSS connector (the last still blocked on owner/legal
gates on branch `feat/phase-3b-live-rss-controlled-egress`, PR #34). The documented
minimum prerequisite before any of these flags is turned on is **operator
observability**: an operator must be able to see queue health, worker-fleet health,
telemetry posture, and per-capability activation state before, during, and after an
activation, and be able to scope any future activation to a single workspace rather
than flipping a global switch for every tenant at once.

Phase 4A builds exactly that foundation and nothing more. It:

1. Extends the existing operator-only `/internal/system/*` tier with capability /
   activation-state introspection built on the already-present job, worker, and
   telemetry diagnostics.
2. Introduces a **per-workspace capability-override** model with a strict,
   deny-biased precedence chain, persisted in a new additive table, so a future
   activation can be scoped to one internal workspace (Phase 4B) instead of globally.
3. Adds an operator-only frontend observability view.

Every surface ships dark: the override model defaults to "no override," and with all
three global flags `False` the effective state of every capability in every workspace
remains disabled. Phase 4A changes *visibility and control plumbing*, not *behavior*.

## 8.3 Goals

- **G1.** Give operators a single authoritative, secret-free read of each dark
  capability's *effective activation state* per workspace (disabled / override-enabled
  / globally-enabled), plus *why* (which precedence rule decided it).
- **G2.** Surface queue, worker-fleet, and telemetry health to operators in one
  coherent view (reusing existing `/internal/system/*` diagnostics), including a
  precise, testable definition of a "stuck" job and dead-letter visibility.
- **G3.** Introduce a per-workspace capability-override mechanism whose precedence can
  never enable a capability that a higher-level safety/environment restriction
  prohibits (deny-biased).
- **G4.** Provide an operator-only frontend view for the above, gated by a real
  server-authoritative operator check.
- **G5.** Keep everything dark and additive: no behavior change, no flag flipped, no
  destructive migration, no contract drift beyond additive operator-only fields.

## 8.4 Non-goals

- **N1.** Enabling opportunity feedback, scout scheduling, or the RSS connector. That
  is Phase 4B and beyond, under separate approval.
- **N2.** Any customer-facing activation UI or self-serve capability toggle. Overrides
  are operator-set only.
- **N3.** A real metrics exporter / hosted telemetry backend. `core/metrics.py` stays
  no-op by default; Phase 4A only *reports posture*, it does not wire an exporter.
- **N4.** Touching PR #34 (`feat/phase-3b-live-rss-controlled-egress`), the dependabot
  PRs (#6, #26, #27, #28, #63–#66), or any dependency version.
- **N5.** Changing Phase 3 semantics, the durable-job lease/fencing model, or the
  schedule recurrence math.
- **N6.** Any TTL/retention/purge worker for audit or feedback data.

## 8.5 Current-state architecture (real references)

**Two-tier disclosure model (already established).**
- Public/customer tier: `apps/api/app/system/routes.py` — `GET /system/health`,
  `/system/readiness`, `/system/capabilities`. `system_capabilities`
  (`routes.py:87`) requires auth and returns `RuntimeSummaryOut` whose
  `features` is a coarse `FeatureFlagsOut` reflection
  (`opportunity_feedback_enabled` only, `routes.py:40,95`). This is what the web app
  reads to avoid probing a 503 endpoint.
- Operator tier: `apps/api/app/system/internal_routes.py` —
  `router = APIRouter(prefix="/internal/system", tags=["internal"])`
  (`internal_routes.py:31`). Every route depends on `require_operator`. Existing
  routes: `/capabilities` (`:87`), `/readiness` (`:93`), `/telemetry` (`:104`),
  `/jobs` (`:129`, cross-tenant queue diagnostics — status counts + recent
  `JobOperatorOut`), `/workers` (`:153`, fleet diagnostics).

**Authorization.** `apps/api/app/auth/dependencies.py`:
- `require_operator` (`dependencies.py:57`) raises `PermissionDeniedError` when
  `not user.is_operator`; operator status is server-controlled, never client-derived.
- `TenantContext` + `get_tenant_context` (`:83`) and `require_role(*allowed)` (`:102`)
  with `_ROLE_RANK` (VIEWER=0 … OWNER=4) drive tenant-scoped role checks.
- `is_operator` is a real persisted user column (migration
  `20260713_0300-…_add_user_is_operator.py`).

**Feature flags.** `apps/api/app/core/config.py:220/235/242` — all three `bool = False`,
each documented as a dark master switch. There is currently **no per-workspace
override**; a flag is a single global boolean.

**Durable jobs.** `apps/api/app/jobs/models.py` — `Job` (`jobs`) and `JobEvent`
(`job_events`). Job carries the lifecycle/lease columns (`status`, `available_at`,
`lease_expires_at`, `heartbeat_at`, `attempt_count`, `max_attempts` default 5,
`last_error_code`, …) with claim/lease indexes (`ix_jobs_claim`, `ix_jobs_lease`,
`ix_jobs_tenant`). Customer vs operator job views are split in
`apps/api/app/jobs/schemas.py` (`JobOut` vs `JobOperatorOut` / `JobDiagnosticsOut`).

**Worker fleet.** `apps/api/app/jobs/worker_registry.py` — `WorkerRegistry`
(`worker_registrations`) with generation-token fencing, derived stale detection
(`find_stale`, `active_count`, `stale_count`), operator reads used by
`/internal/system/workers`.

**Telemetry.** `apps/api/app/core/metrics.py` — provider-neutral, no-op by default,
bounded cardinality (`ALLOWED_LABELS`, `METRIC_NAMES`), runtime-failure isolation
(`telemetry_failure_count`, `exporter_status`). `/internal/system/telemetry` already
reports `TelemetryStatusOut` (logging format, metrics/exporter status, failure count,
tracing posture) — all bounded enums + counts, no identifiers.

**Audit.** `apps/api/app/audit/models.py` (`AuditLog`: org/workspace/actor, `action`,
`entity_type`/`entity_id`, `previous_state`/`new_state`, `context`) and
`apps/api/app/audit/service.py::record_audit(...)`. Already used by the schedule
service (`scouting_requests/schedules.py` writes `scout_schedule.created/paused/
resumed/deleted`) — the established pattern for auditing sensitive mutations.

**Frontend.** `apps/web/src/App.tsx` — React Router 6 route tree behind a single
`ProtectedRoute` (`apps/web/src/auth/ProtectedRoute.tsx`) that only distinguishes
authenticated vs not. There is **no operator-only route guard yet**. Capability flags
are consumed via `apps/web/src/pages/opportunities/useFeedback.ts:34`
(`query.data?.features?.opportunity_feedback_enabled ?? false`) and surfaced in
`Settings.tsx`. Generated contract types live in `apps/web/src/api/schema.d.ts`
(`FeatureFlagsOut` at `:1392`).

**Contract + CI.** `.github/workflows/ci.yml` runs the five required jobs — Frontend
quality, Backend quality, Migrations and API contract (round-trips migrations,
`alembic check`, regenerates `openapi.json`/`schema.d.ts` and fails on drift),
Container build and security, Integration smoke. Contract regeneration is
`npm run gen:types` (`scripts/gen-types.sh`).

## 8.6 Proposed architecture (Phase 4A)

Phase 4A adds three thin layers on top of the existing tiers, all additive:

1. **Capability-state service** — a pure, read-mostly module (proposed
   `apps/api/app/capabilities/service.py`) that resolves, for a given
   `(capability, workspace)`, the *effective* enabled/disabled state and the
   *deciding precedence rule*. It reads the global flag from settings and the optional
   per-workspace override row; it never mutates behavior of the underlying feature.
2. **Per-workspace override model** — a new additive table
   `workspace_capability_overrides` (§8.10, §8.14) plus a small service to set/clear an
   override (operator-only, audited via `record_audit`).
3. **Operator observability surface** — new operator-only routes under the existing
   `/internal/system/*` (or a sibling `/internal/capabilities/*`) that compose the
   capability-state service with the existing job/worker/telemetry diagnostics, plus
   an operator-only frontend view.

The customer `/system/capabilities` reflection is unchanged in Phase 4A except (if
needed) the *effective* per-workspace value it already reports stays consistent with
the precedence chain — but since every override defaults to absent and every global
flag is `False`, the customer-visible value stays `false` everywhere.

## 8.7 Per-workspace capability-override design (precedence)

**Precedence chain (deny-biased), evaluated top-down; first decisive rule wins:**

1. **Safety/environment restriction (hard ceiling).** If a higher-level restriction
   prohibits the capability (e.g. an environment gate, or a global "capability not
   permitted" condition), the capability is **disabled** regardless of any override.
   An override can never raise a capability above this ceiling.
2. **Explicit valid workspace override.** If a valid, non-expired override row exists
   for `(workspace, capability)`, its boolean decides the state — *subject to* rule 1.
3. **Global config flag.** Otherwise the global `*_enabled` setting decides.
4. **Hardcoded disabled default.** If nothing above is decisive, the capability is
   **disabled**.

**Invariant (must be enforced and tested):** a workspace override may only ever *narrow*
or *match* what the safety ceiling permits — it may enable a capability only when rule 1
does not prohibit it. Enabling in one workspace never affects another (per-workspace
row, tenant-scoped read). With all global flags `False` and no override rows, every
workspace resolves to **disabled** through rule 4 — this is the dark default.

The resolver returns both the effective boolean and a `decided_by` enum
(`safety_ceiling` / `workspace_override` / `global_flag` / `default_disabled`) so the
operator view can explain *why*.

## 8.8 Safety model

- **Deny-biased default.** Absence of data = disabled. A missing/expired/malformed
  override is treated as "no override," never as "enabled."
- **No customer path to enable.** Overrides are set only through operator-gated,
  audited endpoints. No customer role (`_ROLE_RANK` up to OWNER) can set an override.
- **Ceiling cannot be overridden.** Rule 1 is evaluated first and is absolute.
- **Tenant isolation.** Override reads/writes are scoped by `organization_id` +
  `workspace_id`; an override in one tenant/workspace is never observable by another,
  matching the existing isolation discipline (Job/JobEvent tenant indexes, scoped
  schedule queries).
- **Audit every mutation.** Every set/clear writes an `AuditLog` row via `record_audit`
  with previous/new state and actor.
- **Secret-free surfaces.** Operator reads report bounded enums, counts, and
  activation state only — never URLs, credentials, payloads, or identifiers beyond the
  already-permitted safe ids, matching `TelemetryStatusOut` / `JobOperatorOut` rules.

## 8.9 Observability data model (what operators can see)

Composed read, all from existing sources plus the new resolver:

- **Per-capability activation state** (new): `{capability, workspace_id, effective_enabled,
  decided_by, global_flag, has_override, override_value}`.
- **Queue health** (existing `/internal/system/jobs`): `status_counts` + recent
  `JobOperatorOut`.
- **Stuck-job summary** (new, derived — §8.10): counts of jobs meeting the stuck
  predicate.
- **Dead-letter visibility** (new, derived — §8.11): count + recent dead-lettered jobs.
- **Worker fleet** (existing `/internal/system/workers`): status counts, active/stale.
- **Telemetry posture** (existing `/internal/system/telemetry`): `TelemetryStatusOut`.

## 8.10 Stuck-job definition (precise, testable)

A job is **stuck** iff *all* hold, evaluated against an injected clock `now`:

- its `status` is a non-terminal, in-flight status (claimed/running class — the same
  active statuses the schedule service treats as in-flight), **and**
- it holds a lease (`lease_expires_at` is not null) whose deadline has passed
  (`lease_expires_at < now`) **or** its `heartbeat_at` is older than the configured
  stale threshold, **and**
- it is not cancel-requested/cancelled and has not reached a terminal state.

This mirrors the worker-registry "stale is derived from heartbeat age, not owned"
principle (`worker_registry.py`) and the job lease model (`jobs/models.py`
`ix_jobs_lease`). The count is computed live so it is accurate regardless of any sweep
cadence. The exact status set and threshold source (`worker_stale_after_seconds` vs a
dedicated job-lease bound) are an open question (§8.23) to resolve against
`jobs/store.py` during implementation.

## 8.11 Dead-letter visibility

`core/metrics.py` already defines `JOBS_DEAD_LETTERED_TOTAL`, so dead-lettering is a
real terminal outcome in the job lifecycle. Phase 4A adds an operator-only read that
surfaces the count and a page of recent dead-lettered jobs (via `JobOperatorOut`,
which already carries `last_error_code`/`last_error_summary` safely). No requeue /
retry action is added in Phase 4A (that is a later, separately-approved operability
action). Read-only visibility only.

## 8.12 API design (operator-only, additive)

All new routes are operator-gated (`Depends(require_operator)`) and live under the
existing internal prefix. Proposed additions:

- `GET /internal/system/capabilities/activation` → list of per-capability,
  per-workspace effective-state records (optionally filtered by `workspace_id`), each
  with `decided_by`. Read-only.
- `GET /internal/system/jobs/stuck` → stuck-job summary per §8.10. Read-only.
- `GET /internal/system/jobs/dead-letter` → dead-letter count + recent page. Read-only.
- `PUT /internal/system/capabilities/override` → set a workspace override
  `{workspace_id, capability, enabled}`; audited; subject to the §8.7 ceiling.
- `DELETE /internal/system/capabilities/override` → clear an override; audited.

The two mutation routes are the *only* new write paths and they never enable a
capability globally — they write override rows the resolver consults under the
deny-biased precedence. All response models follow the existing secret-free operator
schema conventions (bounded enums, counts, safe ids). New Pydantic models are declared
alongside the routes (mirroring `TelemetryStatusOut`, `CapabilitiesOut`).

The existing customer `GET /system/capabilities` contract is unchanged in shape; its
per-workspace effective values remain `false` while everything is dark.

## 8.13 Frontend design (operator-only view)

- **Operator route guard (new):** a `RequireOperator` wrapper (proposed
  `apps/web/src/auth/RequireOperator.tsx`) composed inside the existing
  `ProtectedRoute`, driven by a server-authoritative operator signal (extend the
  current-user/session read; the backend is the source of truth — the client never
  self-declares operator). Non-operators get a not-found/redirect, never the data.
- **Operator observability page (new):** a read-only page (e.g. under an
  `/operations` route in `App.tsx`) showing capability activation state per workspace,
  queue/stuck/dead-letter summaries, worker fleet, and telemetry posture — all from the
  new + existing `/internal/system/*` endpoints via TanStack Query.
- **Override controls (operator-only):** set/clear a workspace override, with an
  explicit confirmation; the UI shows `decided_by` so the operator sees why a state is
  what it is. Controls are disabled/hidden for non-operators.
- Types come from regenerated `schema.d.ts`; no hand-written contract types.

## 8.14 Database / migration plan

One additive migration, single new head chained off `4945b98229e6`
(`down_revision = '4945b98229e6'`), following the established additive, batch-safe
pattern (see `20260718_1144-4945b98229e6_add_opportunity_feedback.py`).

Proposed table **`workspace_capability_overrides`**:

| Column | Type | Notes |
| --- | --- | --- |
| `id` | String(32) PK | uuid4 hex, repo convention |
| `organization_id` | String(32), FK organizations, CASCADE, indexed | tenant scope |
| `workspace_id` | String(32), FK workspaces, CASCADE, indexed | tenant scope |
| `capability` | String(64), not null | enum-constrained value (feedback/scheduling/rss) |
| `enabled` | Boolean, not null | the override value |
| `set_by_user_id` | String(32), FK users, SET NULL | preserve history if user deleted |
| `reason` | Text, nullable | optional operator note |
| `created_at` / `updated_at` | DateTime(tz), server default | `TimestampMixin` |

- **Unique constraint** on `(workspace_id, capability)` — one override per capability
  per workspace.
- **No backfill.** Absence = "no override" = dark default. Existing workspaces get no
  rows.
- **Downgrade** drops the table + indexes. No other subsystem reads it, so dropping
  loses no other data.
- Migration must round-trip (`upgrade`→`downgrade`→`upgrade`) and pass `alembic check`
  with no drift, keeping a **single** head.

## 8.15 Audit logging

Every override set/clear calls `record_audit(...)` with:
`action` ∈ {`capability_override.set`, `capability_override.cleared`},
`entity_type="workspace_capability_override"`, `entity_id`=row id,
`previous_state`/`new_state` capturing the boolean and capability, `actor_user_id`=the
operator, workspace/org scoped. This reuses `apps/api/app/audit/service.py` exactly as
the schedule service does. Read endpoints write no audit rows.

## 8.16 Metrics / tracing

No exporter is wired. Phase 4A only *reports* posture through the existing
`/internal/system/telemetry` (`TelemetryStatusOut`). If any new metric emission is
added for override changes, it must use an existing `METRIC_NAMES` entry and only
`ALLOWED_LABELS`; introducing a new metric name/label is out of scope unless it stays
within the bounded-cardinality policy (`core/metrics.py`). Default backend remains
`NoOpMetrics`, so nothing is emitted in tests or by default.

## 8.17 Testing strategy

- **Precedence unit tests:** all four rules, including the invariant that a workspace
  override can never exceed the safety ceiling; with all flags `False` and no rows,
  every capability resolves disabled (`default_disabled`).
- **Override service tests:** set/clear idempotency, unique `(workspace, capability)`,
  audit row written with correct previous/new state and actor.
- **Four-market isolation** (Dallas / London / Lagos / Nairobi): an override in one
  workspace never changes another market's effective state; reads are tenant-scoped.
- **Operator authorization tests:** every new route rejects a non-operator
  (`require_operator`), and the customer contract stays unchanged/secret-free.
- **Stuck-job + dead-letter tests:** pinned-clock construction of a leased-but-expired
  job asserts it is counted stuck; a terminal job is not; dead-letter count matches.
- **Dark-by-default test:** with the shipped defaults the whole observability surface
  reports every capability disabled and no flag is enabled.
- **Frontend tests:** operator guard hides the page/controls for non-operators; the
  page renders states from mocked `/internal/system/*`; MSW handlers extended
  (`apps/web/src/test/handlers.ts`) additively.
- **Contract test:** `npm run gen:types` produces only additive operator-only schema
  changes, no drift on customer contract.
- Backend suite and frontend suite must stay green; single Alembic head; `alembic
  check` clean.

## 8.18 Acceptance criteria (binary checklist)

1. [ ] A capability-state resolver returns `(effective_enabled, decided_by)` for any
   `(capability, workspace)`.
2. [ ] Precedence is exactly: safety ceiling → workspace override → global flag →
   disabled default; first decisive rule wins.
3. [ ] A workspace override can never enable a capability the safety ceiling prohibits
   (tested).
4. [ ] With shipped defaults (all flags `False`, no override rows) every capability in
   every workspace resolves disabled.
5. [ ] New additive table `workspace_capability_overrides` exists with a unique
   `(workspace_id, capability)` constraint; migration round-trips; single Alembic head;
   `alembic check` clean.
6. [ ] Override set/clear are operator-only and write an `AuditLog` row each.
7. [ ] Operator can read per-workspace activation state, stuck-job summary,
   dead-letter summary, worker-fleet health, and telemetry posture, all operator-gated.
8. [ ] Stuck-job predicate matches §8.10 and is computed against an injected clock.
9. [ ] Every new endpoint rejects non-operators and exposes no secrets.
10. [ ] Four-market isolation holds for overrides and reads.
11. [ ] Operator-only frontend view + guard exists; non-operators cannot see data or
    controls; the guard is server-authoritative.
12. [ ] Customer `/system/capabilities` contract shape is unchanged; effective values
    stay `false` while dark.
13. [ ] Contract regeneration yields only additive operator-only changes; no drift.
14. [ ] All five CI jobs pass; no flag enabled; all three flags still `False`.

## 8.19 Implementation batches

- **4A-A — Planning (this document).** Merged draft plan; no code.
- **4A-B — Capability-state resolver + precedence (backend, dark).** Pure resolver +
  unit tests; no routes, no table yet (resolver reads global flag + a not-yet-present
  override treated as absent). Additive.
- **4A-C — Override model + service + migration (backend, dark).** New table, service,
  audit, isolation tests. Additive, single head.
- **4A-D — Operator observability + override routes (backend, dark).** New operator-only
  routes composing resolver + existing diagnostics + stuck/dead-letter reads; contract
  regenerated additively.
- **4A-E — Operator frontend view + guard (frontend, dark).** `RequireOperator`, the
  operations page, override controls, tests, MSW handlers.

Each batch is its own branch and PR through the protected workflow, reviewed and
exact-merge-SHA verified, shipping dark. 4A-B..E begin only after 4A-A merges.

## 8.20 Rollout plan

Phase 4A rolls out **nothing** to customers. Its "rollout" is purely making operator
tooling available. No global flag is flipped. The observability surface is
operator-only. At the end of Phase 4A the platform is in exactly the same *customer*
state as after Phase 3 closeout — all capabilities dark — but operators now have the
visibility and per-workspace control plane required to *consider* a scoped activation
in Phase 4B.

## 8.21 Phase 4B entry criteria

Phase 4B (first real activation — opportunity feedback in one internal workspace) may
begin only when *all* hold:

- Phase 4A batches 4A-B..E are merged and exact-merge-SHA verified, all dark.
- The operator observability view shows correct per-workspace activation state,
  queue/stuck/dead-letter health, worker fleet, and telemetry posture.
- The per-workspace override precedence (incl. the safety ceiling) is verified by
  tests and by an operator dry-run that changes no customer-visible behavior.
- A separate, explicitly-approved Phase 4B plan and branch exist authorizing the scoped
  enablement of `opportunity_feedback_enabled` for a single named internal workspace.

Phase 4A does not authorize Phase 4B.

## 8.22 Risks

- **R1 — Precedence bug enables a capability unintentionally.** Mitigation: deny-biased
  default, ceiling-first evaluation, exhaustive precedence tests, dark defaults.
- **R2 — Operator surface leaks sensitive data.** Mitigation: reuse the established
  secret-free operator schemas (bounded enums/counts/safe ids), explicit "no
  URL/credential/payload/identifier" tests.
- **R3 — Contract drift breaks the customer contract.** Mitigation: additive-only
  operator schemas; CI contract-drift gate; customer `/system/capabilities` shape
  unchanged.
- **R4 — Migration is not single-head / not reversible.** Mitigation: chain off
  `4945b98229e6`, round-trip test, `alembic check`.
- **R5 — Frontend operator guard is client-spoofable.** Mitigation: server-authoritative
  operator check; the client guard is only UX; the backend `require_operator` is the
  real gate.
- **R6 — Stuck-job definition is inaccurate.** Mitigation: clock-injected, live-derived
  predicate mirroring the existing lease/heartbeat model; explicit tests.

## 8.23 Open questions

- **Q1.** Exact in-flight status set and threshold source for the stuck-job predicate —
  resolve against `apps/api/app/jobs/store.py` and `jobs/status.py` during 4A-D.
- **Q2.** Should the safety ceiling (rule 1) be environment-driven, config-driven, or
  both? Define the concrete ceiling inputs before 4A-B.
- **Q3.** Do the new activation/stuck/dead-letter routes belong under
  `/internal/system/*` or a sibling `/internal/capabilities/*` and `/internal/jobs/*`
  grouping? Naming only; both are operator-gated.
- **Q4.** Whether the customer `/system/capabilities` should ever reflect a
  per-workspace override once one exists, or continue reflecting only the global flag.
  Default assumption: it may reflect the *effective* value, but stays `false` while
  dark.
- **Q5.** Does the override table need an expiry/`valid_until` column, or is explicit
  clear sufficient? Default: explicit clear only (no TTL worker — see N6).

## 8.24 File-level implementation map (proposed, for later batches)

Backend (new):
- `apps/api/app/capabilities/__init__.py`
- `apps/api/app/capabilities/service.py` — resolver + precedence (4A-B).
- `apps/api/app/capabilities/models.py` — `WorkspaceCapabilityOverride` (4A-C).
- `apps/api/app/capabilities/overrides.py` — set/clear service + audit (4A-C).
- `apps/api/alembic/versions/<new>_add_workspace_capability_overrides.py` (4A-C).
- New operator routes in `apps/api/app/system/internal_routes.py` **or** a new
  `apps/api/app/system/internal_capabilities_routes.py` registered in
  `apps/api/app/api/router.py` (4A-D).

Backend (touched, additive):
- `apps/api/app/system/internal_routes.py` — add activation/stuck/dead-letter reads.
- `apps/api/app/api/router.py` — include any new router.
- `apps/api/app/db/base` model registry import if a new model module is added.

Frontend (new):
- `apps/web/src/auth/RequireOperator.tsx` (4A-E).
- `apps/web/src/pages/operations/…` operator observability page + hooks (4A-E).

Frontend (touched, additive):
- `apps/web/src/App.tsx` — add the operator route under `ProtectedRoute`.
- `apps/web/src/api/schema.d.ts` + `apps/api/openapi.json` — regenerated via
  `npm run gen:types`.
- `apps/web/src/test/handlers.ts` — additive MSW handlers.

Tests: new backend test modules under `apps/api/app/tests/` (precedence, overrides,
isolation, operator auth, stuck/dead-letter) and frontend tests under the new page's
`__tests__/`.

## 8.25 CI / protected-merge requirements

Every Phase 4A implementation PR must:

- Pass all five required CI jobs (`.github/workflows/ci.yml`): Frontend quality,
  Backend quality, Migrations and API contract, Container build and security,
  Integration smoke.
- Keep a **single** Alembic head and pass `alembic check` (migration round-trip in the
  "Migrations and API contract" job).
- Produce **no** contract drift beyond additive operator-only schema (the job runs
  `npm run gen:types` then `git diff --exit-code` on `openapi.json` + `schema.d.ts`).
- Merge only via the protected squash workflow under ruleset `18820692` (1 approval,
  stale-approval dismissal, approval-after-push, review-thread resolution, zero bypass
  actors); no admin bypass, no auto-merge.
- Be exact-merge-SHA verified (post-merge `push` CI green on the merge commit) before
  the batch is considered complete.

## 8.26 Definition of done (Phase 4A)

Phase 4A is done when: 4A-B..E are merged and exact-merge-SHA verified; the acceptance
criteria (§8.18) all pass; a single Alembic head is preserved; the customer contract is
unchanged in shape and reports every capability disabled; all three global flags remain
`False`; the operator observability + per-workspace override control plane exists and is
tested; and **no capability has been enabled for any customer**. Phase 4A authorizes no
rollout and no flag activation — the first activation is a separate Phase 4B decision.

---

**Status:** `PLANNING — DOCUMENTATION ONLY — IMPLEMENTATION NOT STARTED — ALL CAPABILITIES REMAIN DARK`
