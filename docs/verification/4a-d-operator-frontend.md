# 4A-D — Operator Frontend View + Guard (Verification)

**Phase:** 4A, batch 4A-D — the operator **frontend** batch deferred by the Phase 4A
plan's status-alignment note (`docs/phase-4a-plan.md` §8.19: "The operator frontend
view is deferred to a later 4A-D batch"). Under the plan's original lettering this
scope was 4A-E (§8.13/§8.24); every post-decomposition record calls it 4A-D, and this
doc follows that convention. This is the **final** Phase 4A implementation batch.
**Nature:** frontend-only, additive, dark. New operator route guard + operator
observability page + additive API-layer bindings + operator-conditional navigation +
MSW test handlers + tests + this doc. **Zero backend changes. Zero contract drift
(`npm run gen:types` reproduces `openapi.json`/`schema.d.ts` byte-identically). No
migration. No flag change (`core/config.py` untouched — all three global flags remain
`False`). No capability activation. No dependency change.**
**Branch:** `feat/phase-4a-de-operator-frontend` (from `main` at
`11c0febed72d1d2eef56ca312c4687ac95af0db3`).
**Alembic head:** unchanged single head `98289430a3ec`; 4A-D adds no migration.

## Scope (what shipped)

- `apps/web/src/auth/RequireOperator.tsx` — operator route guard composed **inside**
  `ProtectedRoute` (plan §8.13). The operator signal is the server-authoritative
  `user.is_operator` from `GET /auth/me` — never client-derived. Non-operators get the
  not-found page rendered **in place** (no redirect), so operator routes are not
  enumerable from a customer session, mirroring the backend's non-enumerating 404
  discipline. The guard is UX only: every operator endpoint is independently enforced
  server-side by `require_operator` (plan risk R5).
- `apps/web/src/pages/operations/` — the `/operations` operator observability page
  (`Operations.tsx`, `useOperations.ts`, `__tests__/operations.test.tsx`), showing:
  - **Capability activation** per active workspace: effective state + the deciding
    precedence rule (`decided_by`, rendered as Safety ceiling / Workspace override /
    Global configuration / Secure default per the delivered `DecisionSource` contract —
    the plan's older `global_flag`/`default_disabled` prose names are superseded by the
    contract, per §8.13's "types come from `schema.d.ts`" rule), the global flag, and
    any stored override row (value, reason, honored-or-not).
  - **Override controls**: set (enable/disable) and clear, each behind an explicit
    `ConfirmDialog` (destructive styling on disable/clear), driven by the registry's
    `workspace_enableable` / `workspace_disableable` booleans — never a hard-coded
    capability list. RSS therefore offers no per-workspace enable (the button is
    visibly disabled with an explanation), while disable remains available
    (deny-biased asymmetry). No optimistic UI: success feedback only after the server
    confirms; failures surface an error toast and the state re-renders from the
    invalidated queries.
  - **Queue health / worker fleet / schedules** from `GET /internal/system/overview`
    (stuck + dead-letter counts included) and **telemetry posture** from
    `GET /internal/system/telemetry` — all read-only, secret-free projections.
- API layer (additive): typed endpoint helpers + tenant-scoped query keys embedding
  `organization_id` + `workspace_id` (cache entries can never be reused across
  workspaces), and `types.ts` re-exports of the generated operator contract types.
- Navigation: an `operatorOnly` flag on `NavItem`, filtered in the sidebar via the
  same server-authoritative signal; the entry stays in the array so module-scope
  breadcrumb labels remain stable.
- Tests: 9 new tests (85 total, all passing) — operator happy path (all sections +
  `decided_by`), non-operator deep-link → not-found **plus a request-absence spy
  proving a customer session issues zero `/internal/*` requests**, loading skeletons,
  recoverable per-section error state, confirmed set-override flow, destructive
  clear-override flow, registry-prohibited enable (disabled control), failed mutation
  without pretended success, and a stored-but-un-honored override remaining visible
  and clearable.
- MSW handlers (additive, stateful): mirror the real resolver semantics
  (`global_configuration` when no honored override; `secure_default` for an
  un-honorable row; route-level 422 `capability_override_not_permitted` for an RSS
  enable; `override_id: null` on clear, matching the service). The capability handlers
  are non-enumerating (wrong scope → 404), so every green render also proves the page
  sent the active workspace's real scope.

## Acceptance criteria addressed (plan §8.18)

- **AC 7** (operator can read activation state, stuck/dead-letter, worker fleet,
  telemetry — operator-gated): satisfied via the page's five reads.
- **AC 11** (operator-only frontend view + guard; non-operators see neither data nor
  controls; guard server-authoritative): satisfied; tested both directions, including
  request-absence.
- **AC 12/13** (customer contract unchanged; contract regen additive-only): satisfied
  trivially — this batch changes no backend byte and regen is a zero diff.
- **AC 14** (CI green, no flag enabled): all three flags untouched and `False`; local
  gate green (85/85 tests, lint `--max-warnings 0` clean, `tsc -b` clean, production
  build clean); CI verified on the PR.

## Adversarial review (pre-commit)

An independent adversarial pass challenged unauthorized access, guard bypass, tenant
leakage, hidden activation, success-after-failure, malformed data, mock/production
divergence, plan mismatch, toolchain truth, and a11y. Result: no unauthorized-access,
bypass, tenant-leak, or activation defect. Findings and dispositions:

- **F1 (Medium, fixed + regression-tested):** the page originally drove the stored-row
  display and the Clear affordance from the resolver's `has_override`, which is
  false for a stored-but-un-honored row — hiding a row the operator must be able to
  clear. Now the row's presence drives visibility/clearing; `decided_by` still drives
  the state display, and the un-honored case is labelled explicitly.
- **F2 (fixed):** all operator queries now also gate on `enabled: isOperator`
  (defense-in-depth matching the `Settings.tsx` pattern) so hooks mounted anywhere
  never issue requests for a customer session.
- **F3 (fixed):** the non-operator test now proves request absence, not just render
  absence.
- **F4 (fixed):** MSW clear-mutation mirrors the real service (`override_id: null`).
- **F5 (fixed):** per-capability `aria-label`s on all action buttons; the prohibited
  RSS enable carries its explanation in the accessible name.
- **F6 (fixed):** an empty registry renders an explicit empty state.
- **F7 (accepted, not adopted):** the per-item `jobs/stuck` / `jobs/dead-letter` page
  reads are not consumed — §8.13 requires *summaries*, which `/overview` composes;
  the list endpoints remain available for a later operability batch.
- **F8 (fixed):** removed a vacuous test assertion.
- Noted (out of this batch's minimal scope): no app-wide React error boundary exists
  anywhere in `apps/web/src` (pre-existing); a multi-workspace re-scope test would
  need a second-workspace fixture (query keys embed the scope structurally).

## Boundaries

This batch completes the Phase 4A engineering surface (4A-B, 4A-C, 4A-D all
implemented) but activates nothing: every capability remains dark, no override row
exists in any real environment, `certifies_production=false`, deployment NO.
**Whole Phase 4 remains incomplete** pending the separately-authorized 4B-B canary
and INFRA-9 staging deployment. The first real activation remains a Phase 4B decision
(plan §8.21).
