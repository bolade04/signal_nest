# Phase 4B-A verification — dark, fail-closed opportunity-feedback capability-gate wiring

**Status:** `PHASE 4B-A IMPLEMENTED ON DRAFT PR — NOT MERGED — GLOBAL FLAG REMAINS FALSE — NO REAL OVERRIDE OR RUNTIME IDENTITY CREATED — OPPORTUNITY FEEDBACK REMAINS DARK`

> This record documents the Phase 4B-A implementation for review. It authorizes no flag
> flip, no override, and no activation. Phase 4B-B (single-workspace activation) requires
> its own separate operational authorization and is **not started**.

Authoritative spec: `docs/phase-4b-plan.md` §6 (dark gate wiring), §7 (observability),
§8 (acceptance criteria). Baseline: Phase 4B-0 merge SHA `6e7185f`.

## 1. Scope of change

The opportunity-feedback authorization gate is routed through the existing deny-biased
capability resolver, becoming the **second** sanctioned resolver consumer (after the
operator router). The change is behaviorally dark: with the global flag `False` and no
override, the resolver returns disabled via `GLOBAL_CONFIGURATION` for every workspace, so
the gate rejects exactly as the previous raw-flag check did.

Files changed (app + tests + this doc only — no migration, contract, frontend, or workflow):

- `apps/api/app/feedback/routes.py` — gate rewired to `resolve_capability`.
- `apps/api/app/tests/test_feedback_capability_gate.py` — new focused gate tests.
- `apps/api/app/tests/test_capability_override_types.py` — import-boundary guard reframed.
- `apps/api/app/tests/test_workspace_capability_override_migration.py` — guard reframed.
- `apps/api/app/tests/test_capability_override_service.py` — allow-list guard reframed.

## 2. Gate behavior

`_require_feedback_feature(db, ctx)` calls
`resolve_capability(session=db, settings=get_settings(), capability=OPPORTUNITY_FEEDBACK,
organization_id=ctx.organization.id, workspace_id=ctx.workspace.id)`. Scope is the
**server-resolved** tenant context — never client-supplied. Both endpoints (submit, list)
call the gate before any read or write.

- **Fail-closed (D2):** only an explicit `effective_enabled is True` opens the gate. Any
  resolver / DB / override-storage exception is logged distinctly
  (`opportunity_feedback_gate_failed`, level ERROR) and re-raised (5xx) **before any write**
  — never swallowed into an enable.
- **Denial contract preserved:** a non-enabled resolution raises
  `CapabilityUnavailableError` → 503 `capability_unavailable`, unchanged from before.
- **Observability:** each decision emits a structured, secret-free
  `opportunity_feedback_gate_decided` event (capability, scoped ids, `effective_enabled`,
  `decided_by`, `global_flag`, `has_override`) — no override reason note, no feedback
  content.

## 3. Import-boundary guards

Three guards enforcing the "resolver unconsumed by live gates" invariant were reframed to
sanction exactly `app/feedback/routes.py` as a resolver consumer while keeping
`app/scouting_requests/routes.py`, `app/scouting_requests/schedules.py`, and
`app/connectors/registry.py` forbidden:

- `test_capability_override_types.py::test_resolver_consumed_only_by_the_sanctioned_feedback_gate`
- `test_workspace_capability_override_migration.py::test_resolver_ships_but_stays_unconsumed_by_live_gates`
- `test_capability_override_service.py::test_only_sanctioned_modules_consume_the_service_or_resolver`
  (AST allow-list now maps the operator router → both control-plane modules, the feedback
  gate → resolver **only**; feedback importing the service would fail here).

## 4. Test coverage (maps to §6 "Required 4B-A tests")

- No override + global `False` → rejected, no write.
- Explicit disable override → rejected.
- Enable override for workspace A → A alone passes the gate (submit 201 / history 200).
- Sibling workspace B (same org) → rejected.
- Workspace in another org → rejected (tenant isolation).
- Tenant-mismatched override row → not honored.
- Clearing the override → restores rejection.
- Resolver / storage failure → never enables; rejected pre-write (fail-closed; asserts no
  feedback row written).
- Setting a feedback override does not alter `scout_scheduling` / `connector_rss`
  resolutions.
- Scheduling and RSS live gates remain unmodified (guards above).
- **Expiry:** not applicable — the current override model has no TTL.

## 5. Validation results

- Focused gate + guard suites: pass (74 passed, 5 skipped).
- Full backend suite: **929 passed, 14 skipped, 0 failed**.
- `ruff check app`: clean.
- Alembic: single head `98289430a3ec` (no new migration).
- API contract (`scripts/gen-types.sh` + `git diff --exit-code`): no drift on
  `apps/api/openapi.json` or `apps/web/src/api/schema.d.ts`.

## 6. Dark-state and isolation confirmation

- All three global flags remain `False` (`opportunity_feedback_enabled`,
  `scout_scheduling_enabled`, `connector_rss_enabled`).
- No real `WorkspaceCapabilityOverride` created; no runtime canary org/workspace identity
  resolved or committed anywhere.
- `/system/capabilities` frontend reflection unchanged (still reads the raw global flag) —
  intentional backend-first canary; to be re-checked in 4B-B.
- No migration, contract, frontend, or workflow change.
- Phase 4B-B **not started**; requires separate operational authorization.
- PR #34 (live RSS) and PR #6 (Dependabot) untouched.

## 7. Remaining gates

This tranche is implemented on a **draft** PR. It has not been marked ready, reviewed,
approved, or merged. Merging 4B-A does not authorize runtime activation (§17). The next
authorized action after independent review is a protected squash-merge of 4B-A; activation
remains a later, separately-authorized Phase 4B-B decision.
