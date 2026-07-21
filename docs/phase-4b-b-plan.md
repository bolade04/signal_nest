# SignalNest Phase 4B-B Plan — Single-Workspace Opportunity Feedback Canary Activation

**Status:** `PHASE 4B-B — DOCUMENTATION AND EVIDENCE SCAFFOLDING ONLY — CANARY NOT ACTIVATED — GLOBAL FLAG REMAINS FALSE — NO OVERRIDE CREATED — NO RUNTIME CANARY IDENTITY RESOLVED — OPPORTUNITY FEEDBACK REMAINS DARK`

> This document is a **planning and evidence-scaffolding record only**. It authorizes
> no flag flip, no override creation, no operator mutation, no database write, and no
> capability activation. Every command shown here is a **FUTURE EXECUTION EXAMPLE — DO
> NOT RUN**. Phase 4B-B activation requires its **own separate, explicit operational
> authorization** distinct from merging this document. Authoring or merging this plan
> authorizes nothing beyond the documentation itself. See §6 (Approval and role
> separation) and §14 (Mandatory stop conditions).

Authoritative parent plan: `docs/phase-4b-plan.md` §9 (single-workspace activation),
§10 (activation preflight), §11 (canary verification matrix), §12 (rollback exercise),
§13 (kill-switch model), §14 (stop conditions), §16 (verification commands), §17
(approval gates), §18 (evidence), Appendix D1–D4. Phase 4B-A verification: merged record
at `docs/verification/4b-a-feedback-capability-gate.md`. Phase 4B-A merge SHA:
`4d253f78fce288167c35370d1d9ae3efd8dbecd6`.

---

## 1. Document status and authority

- **Nature:** documentation-only Phase 4B-B planning + restricted-evidence scaffolding.
  This document changes no application code, no test, no configuration, no migration, no
  API contract, no workflow, no feature flag, and no stored override.
- **Scope of authority:** planning, procedure-recording, and evidence-template creation
  only. It does not resolve a runtime canary identity, does not create an override, and
  does not authorize the operator mutation it describes.
- **Separation from execution:** Phase 4B-B *activation* is a later operational tranche
  that requires its own explicit authorization, its own operations/verification branch,
  and its own restricted evidence record. Merging this plan does **not** start it.
- **Governing parent records:** `docs/phase-4b-plan.md` §9–§19 and Appendix D1–D4;
  `docs/phase-4a-c-plan.md` §8.30–§8.31 (Phase 4B entry criteria and Phase 4A-C
  definition of done). This plan operationalizes those criteria; it amends none of the
  historical decisions.
- **Baseline:** Phase 4B-A merge SHA `4d253f78fce288167c35370d1d9ae3efd8dbecd6` (PR #88,
  squash-merged through the protected workflow); post-merge CI green on that exact SHA.
  At this baseline the feedback route consumes the deny-biased resolver, all three global
  flags are `False`, and no override or runtime canary identity exists.

## 2. Objective

Phase 4B-B is the **first controlled live activation** achieved through the capability
control plane. Its objective is narrow and singular:

- enable the **`opportunity_feedback`** capability for **exactly one** named internal
  canary workspace, via a single per-workspace **enable override**;
- keep `opportunity_feedback_enabled` **`False` globally** throughout (no global
  enablement, ever, in this tranche);
- prove **tenant isolation** — no sibling workspace, no other-organization workspace, and
  no customer workspace becomes enabled;
- prove **reversibility** — the scoped override can be cleared and the workspace returns
  immediately to the dark default;
- capture a complete **restricted evidence record** of preflight, mutation, isolation,
  and rollback.

The mechanism is exactly the persistence-vs-activation split Phase 4A-C was built for: a
per-workspace enable override honored by the deny-biased resolver while the global flag
stays `False`, consumed by the single sanctioned live feedback gate wired in Phase 4B-A.

## 3. Scope and non-goals

**In scope (future execution, separately authorized):**

- one `opportunity_feedback` enable override for one verified internal canary workspace,
  created through the operator API;
- the preflight, isolation, and rollback verifications in §§7, 9, 12;
- a restricted evidence record at `docs/verification/4b-b-feedback-canary.md`.

**Explicit non-goals (excluded from all of Phase 4B-B):**

- global opportunity-feedback enablement (the global flag never flips);
- any second workspace, cohort, or customer rollout;
- any `scout_scheduling` or `connector_rss` override or activation — both stay entirely
  dark;
- any frontend / UI change (the `/system/capabilities` reflection stays global-flag-driven
  and is intentionally not made per-workspace here);
- any migration, schema, or API-contract change;
- any direct database mutation of override state (the operator API is the only sanctioned
  mutation path);
- any change to app code, tests, dependencies, CI, or GitHub settings;
- automatic production activation and auto-re-enable after rollback;
- Phase 4C or later rollout; PR #34 (live RSS) and PR #6 (Dependabot) remain untouched.

## 4. Canary identity policy

**Logical designation:** `SignalNest Internal Phase 4B Canary Workspace`. No runtime
`organization_id`, `workspace_id`, UUID, database primary key, operator credential,
secret, token, or email address appears in this repository by design.

Identity-handling rules (from parent §4 / D1):

- The exact runtime `organization_id` and `workspace_id` must be **independently resolved
  and verified against the target environment immediately before activation** — never
  earlier, never guessed, never copied from logs or stale fixtures without authoritative
  re-validation against the live target.
- The identifiers must resolve to **exactly one internal-only workspace**. No partial-name
  matching, no fuzzy resolution.
- Activation must **validate organization/workspace ownership and internal status** before
  the override is created, and must **stop on any ambiguity** (more than one candidate,
  uncertain internal status, or mismatched organization/workspace association).
- The runtime identity-verification evidence is captured **only** in the restricted
  Phase 4B-B verification record (`docs/verification/4b-b-feedback-canary.md`), never in
  this public plan.
- Phase 4B-B is **blocked** until the exact runtime organization/workspace pair has been
  verified in the target environment.

## 5. Capability and precedence model (verified against code)

The deny-biased resolver (`app/capabilities/resolver.py`, `resolve_capability`) decides
each `(capability, workspace)` pair by a fixed precedence, exposed to operators as the
`DecisionSource` string:

1. `safety_ceiling` — absolute off (reserved kill slot); overrides everything.
2. `workspace_override` — an honored, tenant-validated per-workspace override.
3. `global_configuration` — the bound global flag's raw value.
4. `secure_default` — deny-biased fallback.

Key architectural facts (verified, load-bearing for this tranche):

- **Override scope is per-workspace only.** There is **no** `ORGANIZATION_OVERRIDE` and no
  org-wide enable. The stored row's `organization_id` is used **only** to tenant-validate
  the workspace; a cross-organization override row is never honored.
- **`WorkspaceCapabilityOverride`** carries a `UniqueConstraint(workspace_id, capability)`
  and a CHECK constraint restricting `capability` to the closed registry vocabulary;
  persisting a row records **intent**, not activation.
- With all three global flags `False` and no override, every capability resolves disabled
  via `global_configuration`. An enable override on a `workspace_enableable` capability
  yields `has_override=True`, `decided_by=workspace_override`, `effective_enabled=True`
  for that one workspace while `global_flag` stays `False`.
- After Phase 4B-A the feedback gate `_require_feedback_feature`
  (`app/feedback/routes.py`) is the single sanctioned **live** resolver consumer; the
  operator effective-read surface is also sanctioned but is not a live gate. Scheduling
  and RSS live gates do not import the resolver.
- **Fail-closed (D2):** only an explicit `effective_enabled is True` opens the gate; any
  resolver/DB/override-storage failure is logged (`opportunity_feedback_gate_failed`) and
  re-raised before any write. A non-enabled resolution returns `503 capability_unavailable`.

## 6. Approval and role separation

- Merging Phase 4B-0 (the parent plan) did not authorize Phase 4B-A. Merging Phase 4B-A
  did not authorize Phase 4B-B. Merging **this** document does not authorize the canary
  activation it describes.
- Phase 4B-B activation requires a **separate, explicit operational authorization** and
  its own operations/verification branch (name selected only at execution time).
- The operator mutation uses an **operator-gated** API (`require_operator`: 401 anonymous,
  403 non-operator). Attribution is **server-side** — `actor_user_id` is taken from the
  authenticated operator, never a request body — so no override is recorded anonymously or
  under a spoofed identity.
- The person authorizing activation, the operator executing it, and the observer recording
  evidence should be distinguishable in the evidence record (actor + scope + window).

## 7. Activation preflight (future execution — verify before any mutation)

Before creating the override, Phase 4B-B must confirm (parent §10):

- the currently deployed commit **includes the verified Phase 4B-A merge**
  (`4d253f78fce288167c35370d1d9ae3efd8dbecd6`);
- deployment health is normal;
- the operator identity is authorized (operator-gated API; 401/403 enforced);
- the organization/workspace identity is **exact, unambiguous, and internal-only**;
- **no conflicting override** exists for that workspace/capability;
- the **current effective state is disabled** for the target (operator effective-read);
- the **sibling workspace effective state is disabled**;
- **scheduling and RSS effective states are disabled**;
- logging/metrics are available to observe the change;
- rollback operator access is available (ability to `DELETE` the override);
- an explicit activation window and observer are identified.

**FUTURE EXECUTION EXAMPLE — DO NOT RUN.** Effective-read of the target through the
operator surface (placeholders only; no real IDs committed):

```bash
# FUTURE EXAMPLE — DO NOT RUN. Placeholders <ORG_ID>/<WORKSPACE_ID> are resolved and
# verified in the target env at execution time and recorded ONLY in restricted evidence.
GET /internal/system/capabilities/effective?organization_id=<ORG_ID>&workspace_id=<WORKSPACE_ID>&capability=opportunity_feedback
# Expected before activation: effective_enabled=false, decided_by=global_configuration,
# global_flag=false, has_override=false.
```

## 8. Governed future activation procedure (design-level; do not run)

The single scoped mutation is performed through the operator **set** plane — never a
direct DB write:

**FUTURE EXECUTION EXAMPLE — DO NOT RUN.**

```bash
# FUTURE EXAMPLE — DO NOT RUN. One enable override for the verified canary workspace.
PUT /internal/system/capabilities/overrides
Content-Type: application/json
{
  "organization_id": "<ORG_ID>",       # verified in target env at execution time
  "workspace_id": "<WORKSPACE_ID>",    # verified in target env at execution time
  "capability": "opportunity_feedback",
  "enabled": true,
  "reason": "<bounded operator note — no secret, no customer content>"
}
# Expected: created=true (or changed=true), enabled=true, override_id=<returned>.
# Server records action "workspace_capability_override.created" (or ".updated") with the
# operator as actor. Global flag stays False; no other workspace is touched.
```

Constraints enforced by the merged service (not re-implemented at the route):

- exactly **one** `opportunity_feedback` enable override for the canary workspace;
- `opportunity_feedback_enabled` stays **`False` globally**;
- **no** `scout_scheduling` or `connector_rss` override is created;
- **no other workspace** is enabled;
- deny-biased registry policy still applies (an enable for a non-`workspace_enableable`
  capability is refused `422 capability_override_not_permitted`);
- the write is idempotent and audited under the service's `SELECT … FOR UPDATE`/SAVEPOINT
  critical section, committing the override row and its `AuditLog` atomically.

## 9. Verification and isolation matrix (future execution)

Evidence to capture (parent §11) after the single override is created:

- the **target workspace** can submit opportunity feedback (`201`) and read history
  (`200`);
- a **sibling workspace** (same organization) **cannot** (still `503`/disabled);
- a **workspace in another organization cannot** (tenant isolation);
- **unauthorized operator calls remain rejected** (401/403 on the operator API);
- unrelated feedback access controls remain enforced (editor role gate, IDOR/scope
  checks, closed-vocabulary polarity validation);
- an **audit record identifies the authorized actor and the scoped change**;
- **scheduling remains dark**; **RSS remains dark**;
- **no customer workspace becomes enabled**;
- **no cross-location, cross-market, cross-request, or cross-tenant inheritance** occurs
  (the resolver tenant-validates the override's `organization_id`).

**FUTURE EXECUTION EXAMPLE — DO NOT RUN.** Sibling/other-org effective reads must show
`effective_enabled=false`, `decided_by=global_configuration`; only the target shows
`decided_by=workspace_override`, `effective_enabled=true`.

## 10. Feedback smoke-test safety

When exercising the enabled target directly (backend-first canary):

- submit only **synthetic, non-sensitive** verdicts from the closed vocabulary
  (`is_useful` boolean + optional `reason_code`); **never** free text (there is none) and
  **never** real customer content;
- feedback is **append-only and immutable** — there is no edit/delete path; a smoke-test
  submission is a permanent row, so keep it minimal and clearly internal;
- capturing feedback changes **no** opportunity score, version, ranking, or any
  worker/scheduling/connector behavior;
- the `/system/capabilities` frontend reflection still reads the **raw global flag** and
  will report the feature **disabled** even for the enabled canary — this is the intended
  backend-first posture; verification exercises the API directly and records the
  divergence, not a UI reveal;
- **no feedback body or override reason** is ever logged; observability is secret-free
  (§11).

## 11. Audit and observability

Reuse the existing structured logging / audit already on the feedback and override paths
(no new vendor, no new monitoring system):

- **Override mutation audit** (service, `app/capabilities/service.py`): the set plane
  writes `workspace_capability_override.created` or `.updated`; a policy-refused enable
  writes `.rejected`; the clear plane writes `workspace_capability_override.cleared`. Each
  carries the operator actor id and the scoped `{capability, enabled, id}` — no secret, no
  reason echoed into unsafe fields.
- **Feedback gate decision** (`app/feedback/routes.py`): each decision emits
  `opportunity_feedback_gate_decided` (`outcome` allowed/denied, `effective_enabled`,
  `decided_by`, scoped ids) and a failure emits the distinct
  `opportunity_feedback_gate_failed` (ERROR) — distinguishing an intentional denial from a
  resolver/dependency failure.
- Logs/metrics must be sufficient to **detect unexpected cross-workspace activation** —
  i.e. any `effective_enabled=true` / `decided_by=workspace_override` for a workspace other
  than the canary during the window.
- **Never logged:** override reason text, feedback body, customer content, credentials,
  tokens, or trace ids that could carry sensitive data.

## 12. Rollback exercise (future execution)

Required safe sequence (parent §12), rehearsed as part of the canary:

1. Verify the canary is enabled **only** for the target workspace.
2. **Clear the target workspace override** through the operator **clear** plane
   (`DELETE`) — never a direct DB delete.
3. Verify the target **immediately returns to disabled** fallback (`503`).
4. Verify **no other capability or workspace changed**.
5. Verify the **rollback audit record** (`workspace_capability_override.cleared`, actor +
   scope).
6. **Re-enable the canary only if** the separately approved Phase 4B-B execution
   authorization explicitly permits it **and** all rollback checks passed.

**FUTURE EXECUTION EXAMPLE — DO NOT RUN.**

```bash
# FUTURE EXAMPLE — DO NOT RUN. Scoped rollback via the clear plane.
DELETE /internal/system/capabilities/overrides?organization_id=<ORG_ID>&workspace_id=<WORKSPACE_ID>&capability=opportunity_feedback
# Expected: changed=true, enabled=null, override_id=null; one ".cleared" audit.
# Re-read effective: effective_enabled=false, decided_by=global_configuration.
```

Any failure during rollback **ends the activation attempt and triggers incident
handling**; reactivation is **not** automatically authorized.

## 13. Kill-switch hierarchy (D3)

Layered controls, in order of use:

1. **Primary scoped rollback:** clear the one canary workspace override → instant revert
   to disabled for that workspace. *This* is the immediate canary rollback.
2. **Global safety invariant:** `opportunity_feedback_enabled` stays `False` throughout.
   Setting an already-`False` flag to `False` is a standing invariant, **not** an action —
   it is not, by itself, a useful canary rollback.
3. **Resolver safety ceiling:** the deny-biased precedence and the reserved
   `safety_ceiling` slot (`app/capabilities/resolver.py`) remain intact and able to force
   the capability off regardless of any override.
4. **Code rollback:** revert the Phase 4B-A gate-wiring commit if the resolver integration
   itself is defective.

Additional invariants: resolver/storage failure is **fail-closed**; no fallback path may
read the raw global flag to bypass the resolver after 4B-A (once resolver-driven, it stays
resolver-driven).

## 14. Mandatory stop conditions

Halt (and, if mid-activation, roll back) on any of (parent §14):

- uncertain workspace identity; target not demonstrably internal; wrong
  organization/workspace association;
- Phase 4B-A not deployed or not verified on the running commit;
- dirty or unexpected repository/deployment state;
- global flag unexpectedly `True`; scheduling or RSS unexpectedly enabled;
- conflicting or unexpected override present;
- operator authorization failure; resolver failure or ambiguous result;
- audit logging unavailable; unexpected contract behavior;
- any sibling/customer workspace becomes enabled;
- a feedback write occurs following a denied decision;
- rollback cannot be completed and verified;
- unexpected error-rate or authorization regression.

## 15. Evidence requirements

All Phase 4B-B evidence is captured in the **restricted** record
`docs/verification/4b-b-feedback-canary.md` (template scaffolded in this tranche with
placeholders only — no live values). It must record:

- preflight evidence (§7): deployed commit includes the 4B-A merge; identity verification;
  no conflicting override; target/sibling/other-org effective states before the change;
- the exact scoped mutation (§8) and its returned mutation summary;
- the audit record for the create (actor + scope);
- the isolation checks (§9); the feedback smoke-test result (§10);
- the rollback exercise (§12) and its `.cleared` audit;
- the final effective state (target back to disabled) and proof every other capability and
  non-canary workspace remained dark.

Runtime identifiers, if recorded at all, live **only** in this restricted record — never
in the public repository plan.

## 16. Future execution sequence

1. Obtain **separate, explicit operational authorization** for Phase 4B-B activation.
2. Create a dedicated operations/verification branch (name chosen at execution time).
3. Independently resolve and verify the exact runtime canary organization/workspace
   identity in the target environment (§4).
4. Run the activation preflight (§7); stop on any §14 condition.
5. Create the single enable override via the operator `PUT` plane (§8).
6. Run the verification/isolation matrix (§9) and the feedback smoke test (§10).
7. Perform and verify the rollback exercise (§12).
8. Re-enable only if separately authorized and all checks passed.
9. Record all evidence in `docs/verification/4b-b-feedback-canary.md` (restricted).

Neither the operations branch nor the mutation is created now.

## 17. Acceptance criteria (Phase 4B-B execution — future)

Phase 4B-B activation is acceptable only when, at execution time:

- exactly one `opportunity_feedback` enable override exists, scoped to the verified canary
  workspace; `opportunity_feedback_enabled` remains `False` globally;
- no scheduling/RSS override exists; no other workspace is enabled;
- the target passes the gate (`201`/`200`); every sibling/other-org/customer workspace is
  rejected (`503`);
- operator API auth (401/403) and feedback role/IDOR/vocabulary gates remain intact;
- create and clear audit records exist with actor + scope;
- the rollback exercise succeeds and returns the target to the dark default;
- all evidence (§15) is captured in the restricted record;
- no migration, contract, frontend, or workflow change occurred.

**This documentation tranche satisfies none of the above by itself** — it only scaffolds
the plan and evidence template.

## 18. Current stop declaration

This tranche is **documentation and evidence scaffolding only**. As authored:

- all three global flags remain `False`; no override created; no runtime canary identity
  resolved or committed; opportunity feedback remains **dark** and fail-closed;
- no app code, test, migration, contract, frontend, dependency, CI, or GitHub setting is
  changed;
- PR #34 (live RSS) and PR #6 (Dependabot) are untouched;
- Phase 4B-B activation is **not started** and requires separate operational authorization.

The next authorized action after this documentation PR is **independent review**;
activation remains a later, separately-authorized decision.
