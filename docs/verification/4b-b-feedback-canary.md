# Phase 4B-B verification — single-workspace opportunity feedback canary (RESTRICTED)

**Status:** `RESTRICTED EVIDENCE TEMPLATE — PLACEHOLDERS ONLY — NO LIVE CANARY EXECUTED — GLOBAL FLAG FALSE — NO OVERRIDE CREATED — NO RUNTIME IDENTITY RESOLVED — OPPORTUNITY FEEDBACK REMAINS DARK`

> This is an **evidence template**. It records no live activation. Every value below is a
> `<PLACEHOLDER>` to be filled **only** at Phase 4B-B execution time, under a separate
> explicit operational authorization. Until then, no runtime `organization_id`,
> `workspace_id`, UUID, actor id, credential, token, or email appears here. Runtime
> identifiers, if captured, live **only** in the executed (restricted) copy of this
> record — never committed with real values to the public repository.

Authoritative procedure: `docs/phase-4b-b-plan.md`. Parent plan: `docs/phase-4b-plan.md`
§9–§18. Phase 4B-A merge SHA baseline: `4d253f78fce288167c35370d1d9ae3efd8dbecd6`.

---

## 0. Execution header (fill at execution time)

- Authorization reference (separate 4B-B operational approval): `<PLACEHOLDER>`
- Authorizer: `<PLACEHOLDER>` · Executing operator: `<PLACEHOLDER>` · Observer:
  `<PLACEHOLDER>`
- Activation window (start / end, UTC): `<PLACEHOLDER>` / `<PLACEHOLDER>`
- Target environment: `<PLACEHOLDER>`
- Deployed commit at execution: `<PLACEHOLDER>` (must include 4B-A merge
  `4d253f78fce288167c35370d1d9ae3efd8dbecd6`)

## 1. Canary identity verification (§4)

- Logical designation: `SignalNest Internal Phase 4B Canary Workspace`
- Resolved `organization_id`: `<PLACEHOLDER — resolved + verified in target env>`
- Resolved `workspace_id`: `<PLACEHOLDER — resolved + verified in target env>`
- Resolution method (authoritative, non-fuzzy, exactly one match): `<PLACEHOLDER>`
- Internal-only status confirmed: `<PLACEHOLDER: yes/no + how>`
- Organization/workspace ownership validated: `<PLACEHOLDER: yes/no>`
- Ambiguity check (exactly one candidate, no partial match): `<PLACEHOLDER>`

## 2. Preflight (§7)

- Deployed commit includes verified 4B-A merge: `<PLACEHOLDER: yes/no + SHA>`
- Deployment health normal: `<PLACEHOLDER>`
- Operator identity authorized (operator-gated; 401/403 enforced): `<PLACEHOLDER>`
- No conflicting override for (opportunity_feedback, target workspace): `<PLACEHOLDER>`
- Target current effective state disabled: `<PLACEHOLDER: effective_enabled / decided_by>`
- Sibling workspace effective state disabled: `<PLACEHOLDER>`
- Scheduling effective state disabled: `<PLACEHOLDER>`
- RSS effective state disabled: `<PLACEHOLDER>`
- Logging/metrics available: `<PLACEHOLDER>`
- Rollback (DELETE) operator access available: `<PLACEHOLDER>`
- Activation window + observer identified: `<PLACEHOLDER>`

## 3. Pre-mutation effective reads (operator effective-read plane)

- Target `GET …/capabilities/effective?...&capability=opportunity_feedback`:
  `<PLACEHOLDER: effective_enabled=false, decided_by=global_configuration, global_flag=false, has_override=false>`
- Sibling workspace: `<PLACEHOLDER>`
- Other-organization workspace: `<PLACEHOLDER>`
- Scheduling / RSS for target: `<PLACEHOLDER>`

## 4. The scoped mutation (§8 — operator PUT set plane)

- Endpoint used: `PUT /internal/system/capabilities/overrides`
- Request scope: org=`<PLACEHOLDER>` workspace=`<PLACEHOLDER>` capability=`opportunity_feedback`
  enabled=`true` reason=`<PLACEHOLDER — bounded, no secret/customer content>`
- Response: created=`<PLACEHOLDER>` changed=`<PLACEHOLDER>` enabled=`<PLACEHOLDER>`
  override_id=`<PLACEHOLDER>`
- Confirm exactly one override created; global flag still `False`: `<PLACEHOLDER>`

## 5. Create audit record (§11)

- Audit action observed: `workspace_capability_override.created` (or `.updated`):
  `<PLACEHOLDER>`
- Actor id (operator, server-attributed): `<PLACEHOLDER>`
- Scoped payload `{capability, enabled, id}`: `<PLACEHOLDER>`
- No secret / reason text leaked into unsafe fields: `<PLACEHOLDER>`

## 6. Post-mutation effective read — target

- Target: `<PLACEHOLDER: effective_enabled=true, decided_by=workspace_override, global_flag=false, has_override=true, override_value=true>`

## 7. Isolation matrix (§9)

- Target can submit feedback (`201`) and read history (`200`): `<PLACEHOLDER>`
- Sibling workspace (same org) rejected (`503`/disabled): `<PLACEHOLDER>`
- Other-organization workspace rejected (`503`): `<PLACEHOLDER>`
- Unauthorized operator calls rejected (401/403): `<PLACEHOLDER>`
- Feedback editor-role gate enforced (viewer `403`): `<PLACEHOLDER>`
- IDOR/scope checks enforced: `<PLACEHOLDER>`
- Closed-vocabulary polarity validation enforced: `<PLACEHOLDER>`
- Scheduling remains dark: `<PLACEHOLDER>`
- RSS remains dark: `<PLACEHOLDER>`
- No customer workspace enabled: `<PLACEHOLDER>`
- No cross-location/market/request/tenant inheritance: `<PLACEHOLDER>`

## 8. Feedback smoke test (§10 — synthetic, non-sensitive only)

- Submission (`is_useful` + optional `reason_code`, no free text): `<PLACEHOLDER>`
- Returned `201`, new immutable row: `<PLACEHOLDER>`
- No opportunity score/version/ranking changed: `<PLACEHOLDER>`
- `/system/capabilities` reflection still reports disabled (backend-first divergence
  acknowledged): `<PLACEHOLDER>`
- Feedback gate decision event `opportunity_feedback_gate_decided`
  (outcome=allowed, decided_by=workspace_override): `<PLACEHOLDER>`

## 9. Cross-workspace safety scan

- No `effective_enabled=true` / `decided_by=workspace_override` for any workspace other
  than the canary during the window: `<PLACEHOLDER>`

## 10. Rollback exercise (§12 — operator DELETE clear plane)

- Verified canary enabled only for target before clear: `<PLACEHOLDER>`
- `DELETE …/capabilities/overrides?...&capability=opportunity_feedback` response:
  changed=`<PLACEHOLDER>` enabled=`<PLACEHOLDER: null>` override_id=`<PLACEHOLDER: null>`
- Target immediately returns to disabled (`503`; decided_by=global_configuration):
  `<PLACEHOLDER>`
- No other capability or workspace changed: `<PLACEHOLDER>`
- Rollback audit `workspace_capability_override.cleared` (actor + scope): `<PLACEHOLDER>`

## 11. Re-enable decision (only if separately authorized)

- Separate authorization to re-enable present: `<PLACEHOLDER: yes/no>`
- All rollback checks passed: `<PLACEHOLDER>`
- Re-enable performed: `<PLACEHOLDER: yes/no — default no>`

## 12. Final state confirmation

- `opportunity_feedback_enabled` global flag: `<PLACEHOLDER: False>`
- `scout_scheduling_enabled` / `connector_rss_enabled`: `<PLACEHOLDER: False / False>`
- Target final effective state: `<PLACEHOLDER: disabled unless separately re-enabled>`
- Every other capability and non-canary workspace remained dark: `<PLACEHOLDER>`

## 13. Stop-condition log (§14)

- Any stop condition encountered: `<PLACEHOLDER: none / describe + action taken>`
- Incident handling triggered (if rollback failed): `<PLACEHOLDER: n/a>`

## 14. Isolation of unrelated work

- PR #34 (live RSS) untouched: `<PLACEHOLDER>`
- PR #6 (Dependabot) untouched: `<PLACEHOLDER>`

## 15. Sign-off

- Executing operator sign-off: `<PLACEHOLDER>`
- Observer sign-off: `<PLACEHOLDER>`
- Authorizer acknowledgement of evidence: `<PLACEHOLDER>`

---

**Template note:** as committed in the Phase 4B-B documentation tranche, this file contains
**only placeholders** — no live canary was executed, no override was created, no runtime
identity was resolved, and the global flag remains `False`. Opportunity feedback remains
dark.
