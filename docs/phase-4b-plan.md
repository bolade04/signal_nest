# SignalNest Phase 4B Plan — Opportunity Feedback Internal Canary Activation

**Status:** `PHASE 4B-0 PLANNING RECORD — DOCUMENTATION ONLY — AUTHORIZES NO CODE, NO FLAG, NO OVERRIDE, NO ACTIVATION — ALL CAPABILITIES REMAIN DARK`

> This document authorizes **planning only**. It does not wire any gate, flip any
> flag, create any override, or activate any capability. Each implementation tranche
> (Phase 4B-A) and the activation tranche (Phase 4B-B) requires its **own separate
> execution branch, pull request, review, and protected merge / operational
> authorization**. Merging this plan does **not** authorize Phase 4B-A; merging
> Phase 4B-A does **not** authorize Phase 4B-B. See §17 (Approval gates).

---

## 1. Title and document status

**SignalNest Phase 4B Plan — Opportunity Feedback Internal Canary Activation.**

- **Nature:** documentation-only planning record (Phase 4B-0).
- **Scope of authority:** planning and decision-recording only. This document itself
  changes no application code, no test, no configuration, no migration, no contract,
  no workflow, no feature flag, and no stored override.
- **Tranche separation:** Phase 4B-A (dark gate wiring) and Phase 4B-B (single-workspace
  activation) are defined here but **not** implemented here. Each proceeds only under
  its own separate, explicitly-approved branch/PR (4B-A) or operational authorization
  (4B-B).
- **Governing parent record:** `docs/phase-4a-c-plan.md` §8.30 (Phase 4B entry criteria)
  and §8.31 (Phase 4A-C definition of done). This plan operationalizes those criteria;
  it does not amend the historical Phase 4A-C decisions.

## 2. Baseline and prerequisites

At the baseline for this plan:

- **Phase 4A-C.4 verified merge SHA:** `6b980761164d188898e816a5eb58599be2310eab`
  (PR #86, squash-merged through the protected workflow).
- **Post-merge CI:** run `29826176808` completed successfully on that exact SHA
  (all five required jobs green).
- **All three global capability flags are `False`** (shipped defaults):
  `connector_rss_enabled`, `scout_scheduling_enabled`, `opportunity_feedback_enabled`
  (`apps/api/app/core/config.py`).
- **Stored overrides are behaviorally inert:** no live gate consumes the resolver, so a
  stored override records intent only and activates no product behavior.
- **The capability resolver's only current production consumer** is the internal
  operator capability API (`apps/api/app/system/internal_capabilities_routes.py` —
  effective read), which is **not** a live behavioral gate.
- **Isolation:** PR #34 (live RSS controlled egress, `feat/phase-3b-live-rss-controlled-egress`)
  and PR #6 (Dependabot) remain untouched and out of scope.
- **No Phase 4B code or activation exists** at this baseline; the feedback route gate
  still reads the raw global flag directly.

## 3. Phase 4B objective

Phase 4B is the **first controlled live use of the capability resolver**. It is:

- limited to the **`opportunity_feedback`** capability;
- limited ultimately to **one named internal canary workspace**;
- achieved **without any global enablement** (the global flag stays `False`);
- **not** a second workspace, **not** a customer cohort, **not** a broad rollout;
- **not** an activation of `scout_scheduling` or `connector_rss` (both remain entirely
  dark and out of scope).

The mechanism is a per-workspace **enable override** honored by the deny-biased resolver
while the global flag remains `False` — exactly the persistence-vs-activation split the
Phase 4A-C control plane was built to support.

## 4. Canary workspace identity

**Logical designation:** `SignalNest Internal Phase 4B Canary Workspace`.

Identity-handling rules:

- The **exact runtime `organization_id` and `workspace_id` must be independently
  resolved and verified against the target environment immediately before Phase 4B-B** —
  never earlier, never guessed.
- The identifiers must resolve to **exactly one internal-only workspace**. No partial-name
  matching, no fuzzy resolution.
- **Activation must stop on any ambiguity** (more than one candidate, uncertain internal
  status, or mismatched organization/workspace association).
- Activation must **validate organization/workspace ownership and internal status** before
  the override is created.
- No identifier may be **copied from logs or stale fixtures** without authoritative
  validation against the live target environment.
- The runtime identity-verification evidence must be **captured in the Phase 4B-B
  verification record** (restricted operational evidence), **not** embedded in this public
  repository plan.
- **No UUID, database primary key, operator credential, secret, token, or email address**
  appears in this plan by design.
- **Phase 4B-A does not require the runtime workspace ID.** It only wires the gate; it is
  proven dark with the shipped configuration, using synthetic test workspaces.
- **Phase 4B-B is blocked** until the exact runtime organization/workspace pair has been
  verified.

*Repository evidence note:* `docs/operations/opportunity-feedback-rollout.md` §"Enabling
feedback" and its retained observations already state that "first enablement is a single
non-production internal workspace." No canonical internal-workspace **name or identifier**
is committed anywhere in the repository (correctly), so the logical designation above stands
and no sensitive identifier is introduced.

## 5. Current architecture and consumer boundary

**Verified current flow (all dark):**

```
global flag (Settings.opportunity_feedback_enabled = False)
  + stored WorkspaceCapabilityOverride (none in production)
        │
        ▼
  resolve_capability()      (app/capabilities/resolver.py — deny-biased precedence)
        │
        ▼
  SOLE production consumer:
  app/system/internal_capabilities_routes.py  (operator effective-read — NOT a live gate)
```

The live feedback gate today does **not** consult the resolver:

```
feedback request
  → _require_feedback_feature()   (app/feedback/routes.py)
  → reads get_settings().opportunity_feedback_enabled   (raw global flag)
  → 503 capability_unavailable while dark
```

**Planned Phase 4B-A addition:**

```
feedback request
  → authenticated organization/workspace context (path workspace + tenant context)
  → _require_feedback_feature()
  → resolve_capability(OPPORTUNITY_FEEDBACK, organization, workspace)
  → allow or reject BEFORE any feedback write
```

Service-layer or post-write gating is **not** selected: the existing route guard
`_require_feedback_feature` is the **narrowest pre-write boundary**, consistent with the
house rule that role/feature gating lives at the route (see `app/feedback/service.py`
module docstring). Gating deeper (in `create_feedback`) would be later and would violate
that boundary; gating post-write is unsafe.

## 6. Phase 4B-A — Dark gate wiring

**Objective:** make the feedback route the second *sanctioned* resolver consumer, while
remaining globally and behaviorally dark.

Specification:

- Replace the raw global-setting decision inside `_require_feedback_feature`
  (`app/feedback/routes.py`) with `resolve_capability(OPPORTUNITY_FEEDBACK, organization,
  workspace)`, using the **authenticated request's organization/workspace identity** (the
  path workspace resolved within the tenant context).
- **Preserve pre-write rejection:** the gate must reject before any feedback row is written,
  exactly as today.
- **Adopt fail-closed behavior** (D2): a resolver, database, override-storage, or
  capability-decision failure must **never** enable the feature. A failure rejects the
  request before any write.
- **Do not create any override.** **Do not change any flag.**
- **Reframe the import-boundary guard only enough to sanction `app/feedback/routes.py`**
  as a resolver consumer (currently `test_capability_override_types.py::
  test_resolver_remains_unconsumed_by_live_gates` forbids it). The reframed guard must
  **keep `app/scouting_requests/routes.py`, `app/scouting_requests/schedules.py`, and
  `app/connectors/registry.py` forbidden** as live resolver consumers.
- **Add structured, secret-free decision observability** (see §7). Never log sensitive
  feedback content or override reason text.
- **Preserve public API response contracts** unless repository evidence proves a contract
  change is necessary and it is separately authorized. The disabled decision must continue
  to return the repository-standard safe response (today `503 capability_unavailable`); a
  resolver/dependency failure must be distinguishable from an intentional disabled decision
  in observability, without inventing a new public response shape in this tranche.
- **Prove no behavioral change under the shipped dark configuration:** with the global flag
  `False` and no override present, `resolve_capability` returns disabled via the
  `GLOBAL_CONFIGURATION` rule, so every workspace is still rejected — identical to today.

**Frontend-reflection consideration (carry into 4B-A design / 4B-B verification):** the
customer UI reveals the feedback panel from a **read-only reflection** of
`features.opportunity_feedback_enabled` on `GET /system/capabilities`
(`docs/operations/opportunity-feedback-rollout.md`). Wiring only the backend route gate to
the resolver does **not** by itself change that reflection (which reads the raw global
flag, still `False`). Therefore, after 4B-A, the canary workspace's **backend** gate would
honor an enable override while the **frontend reflection** would still report disabled.
This is acceptable and intentional for a backend-first canary (verification in 4B-B may
exercise the API directly), but the interaction must be explicitly acknowledged in the 4B-A
design and re-checked in 4B-B. Changing the `/system/capabilities` reflection to be
per-workspace resolver-driven is **out of scope** for 4B-A and, if ever desired, is a
separately discovered and authorized contract change.

### Required 4B-A tests

- No override + global `False` → feedback **rejected**.
- Explicit **disable** override → feedback rejected.
- Valid **enable** override for workspace A → workspace A **alone** passes the gate.
- Sibling workspace B (same organization) → remains rejected.
- Workspace in **another organization** → remains rejected (tenant isolation).
- **Clearing** the override → restores rejection.
- **Expiring** an override → restores fallback behavior *if* override expiration is
  supported (the current model has no TTL; if unsupported, record it as not-applicable
  rather than inventing expiry).
- Resolver / storage failure → **never enables**; request rejected pre-write (fail-closed).
- Feedback is rejected **before any write** occurs.
- **Unrelated capability resolutions remain unchanged** (setting a feedback override does
  not alter `scout_scheduling` / `connector_rss` resolutions).
- **Scheduling and RSS live gates remain unmodified** (their modules still do not import
  the resolver).
- **Operator API authentication and authorization remain intact** (401/403 unchanged).
- **Import-boundary guards allow only the newly sanctioned feedback consumer** and keep the
  other three modules forbidden.
- **Existing feedback API behavior remains compatible** when the capability is enabled in a
  test (submit `201`, history read, role/scope/IDOR behavior unchanged).
- **Full CI and contract-drift checks pass** (§16).

**Test-created overrides do not constitute runtime activation.** Tests operate on synthetic
sessions and synthetic workspaces; they never touch the production environment, the shipped
flags, or the runtime canary workspace.

## 7. Phase 4B-A observability

A deliberate, secret-free observability design must cover:

- the **capability identifier** (`opportunity_feedback`);
- **organization/workspace identifiers** in the repository-approved safe form (scoped IDs,
  no secrets — consistent with existing feedback log events);
- the **enabled/disabled result**;
- the **`decided_by` / source** where available (`SAFETY_CEILING` / `WORKSPACE_OVERRIDE` /
  `GLOBAL_CONFIGURATION` / `SECURE_DEFAULT`);
- an explicit distinction between an **intentional denial** and a **resolver/dependency
  failure**;
- a **request correlation identifier** if one is already supported;
- **no override reason** or any field that could carry sensitive data;
- **no feedback body or customer content** (the feedback vocabulary is closed; free text is
  never logged today and must remain unlogged);
- **metrics or structured logs sufficient to detect unexpected cross-workspace activation**
  (e.g. an enabled decision for any workspace other than the canary during 4B-B).

Do not prescribe a new vendor or external monitoring system; reuse the existing structured
logging / audit mechanisms already used by the feedback path
(`docs/operations/observability.md`, `app/feedback/service.py` `log_event` / `record_audit`).

## 8. Phase 4B-A acceptance criteria

- All three global flags remain `False`.
- **No runtime override is created.**
- No migration; single Alembic head preserved.
- **No contract drift** (`npm run gen:types` → clean `git diff` on `openapi.json` +
  `schema.d.ts`).
- No frontend change.
- No scheduling or RSS change.
- All relevant tests and **all five CI jobs pass**.
- **All workspaces remain rejected under the actual shipped configuration** (flag `False`,
  no override).
- **Resolver failure cannot fail open.**
- 4B-A is **separately reviewed and merged through the protected workflow before Phase
  4B-B** begins.

## 9. Phase 4B-B — Single-workspace activation

A later, **separately authorized operational tranche** that:

- starts **only after** 4B-A is merged and post-merge CI is verified on the merge SHA;
- **independently verifies the exact runtime organization/workspace identity** (§4);
- **verifies the target is internal-only**;
- uses the **authenticated internal operator API** (the `PUT` override-set plane) to perform
  the change — no direct database mutation;
- creates **exactly one** `opportunity_feedback` **enable** override for the canary
  workspace;
- keeps **`opportunity_feedback_enabled = False` globally**;
- creates **no** scheduling or RSS override;
- enables **no other workspace**;
- **verifies an audit record** for the scoped change (actor + scope);
- conducts **isolation checks** (§11);
- conducts **rollback verification** (§12);
- records all evidence in a **Phase 4B-B verification document**
  (`docs/verification/4b-b-*.md`).

This plan contains **no executable credentials, tokens, real IDs, or ready-to-run
production mutation command**. The operator action is described at the design level only.

## 10. Activation preflight (Phase 4B-B)

Before creating the override, Phase 4B-B must confirm:

- the **currently deployed commit includes the verified 4B-A merge**;
- **deployment health is normal**;
- the **operator identity is authorized** (operator-gated API, 401/403 enforced);
- the **organization/workspace identity is exact and unambiguous** and resolves to one
  internal-only workspace;
- **no conflicting override exists** for that workspace/capability;
- the **current effective state is disabled** for the target (via the operator effective-read
  API);
- the **sibling workspace effective state is disabled**;
- **scheduling and RSS effective states are disabled**;
- **logging/metrics are available** to observe the change;
- **rollback operator access is available** (ability to `DELETE` the override);
- an explicit **activation window and observer** are identified.

## 11. Canary verification matrix (Phase 4B-B)

Evidence to capture:

- the **target workspace can submit opportunity feedback successfully** (`201`) and read
  history;
- a **sibling workspace cannot** (still `503`/disabled);
- a **workspace in another organization cannot**;
- **unauthorized operator calls remain rejected** (401/403 on the operator API);
- **unrelated feedback access controls remain enforced** (editor role gate, IDOR/scope
  checks, polarity validation);
- an **audit record identifies the authorized actor and the scoped change**;
- **scheduling remains dark**;
- **RSS remains dark**;
- **no customer workspace becomes enabled**;
- **no cross-location, cross-market, cross-request, or cross-tenant inheritance** occurs
  (the resolver tenant-validates the override's `organization_id`).

## 12. Rollback exercise (Phase 4B-B)

Required safe sequence:

1. Verify the canary is enabled **only** for the target workspace.
2. **Clear the target workspace override** through the authorized operator boundary
   (`DELETE` override-clear plane).
3. Verify the target **immediately returns to disabled** fallback behavior (`503`).
4. Verify **no other capability or workspace changed**.
5. Verify the **rollback audit record**.
6. **Re-enable the canary only if** the separately approved Phase 4B-B execution
   authorization explicitly permits it **and** all rollback checks passed.

Any failure during rollback **ends the activation attempt and triggers incident handling**.
Reactivation is **not** automatically authorized.

## 13. Kill-switch model

Layered controls (D3):

1. **Primary scoped rollback:** clear the one canary workspace override → instant revert to
   disabled for that workspace. *This* is the immediate canary rollback.
2. **Global safety control:** `opportunity_feedback_enabled` remains `False` throughout the
   rollout. Setting an already-`False` flag to `False` is **not**, by itself, a useful
   canary rollback — it is a standing invariant, not an action.
3. **Resolver safety ceiling:** the deny-biased precedence and the reserved safety-ceiling
   slot (`app/capabilities/resolver.py`) must remain intact and able to force the capability
   off regardless of override.
4. **Code rollback:** revert the Phase 4B-A gate-wiring commit if the resolver integration
   itself is defective.

Additional invariants:

- Resolver / storage failure is **fail-closed**.
- **No fallback path may read the raw global flag to bypass the resolver after 4B-A** — once
  the gate is resolver-driven, it stays resolver-driven (no silent revert to the raw-flag
  decision at runtime).

## 14. Stop conditions

Halt (and, if mid-activation, roll back) on any of:

- uncertain workspace identity;
- target is not demonstrably internal;
- wrong organization/workspace association;
- 4B-A not deployed or not verified;
- dirty or unexpected repository/deployment state;
- global flag unexpectedly `True`;
- scheduling or RSS unexpectedly enabled;
- conflicting or unexpected override present;
- operator authorization failure;
- resolver failure or ambiguous result;
- audit logging unavailable;
- unexpected contract behavior;
- any sibling/customer workspace becomes enabled;
- a feedback write occurs following a denied decision;
- rollback cannot be completed and verified;
- unexpected error-rate or authorization regression.

## 15. Non-goals

Explicitly excluded from all of Phase 4B (4B-A + 4B-B):

- global opportunity-feedback enablement;
- any second workspace;
- external customer rollout;
- frontend rollout / UI change;
- scheduling activation;
- RSS activation or any PR #34 work;
- Dependabot PR #6;
- migrations;
- schema or contract changes (unless separately discovered and authorized);
- broad refactoring of capability infrastructure;
- background worker or scheduler changes;
- automatic production activation;
- auto-re-enable after rollback;
- Phase 4C or later rollout.

## 16. Required repository verification commands

Exact commands, discovered from `.github/workflows/ci.yml` and repository scripts (do not
invent; some require services/secrets as noted):

- **Focused backend feedback/capability tests:**
  `apps/api/.venv/bin/python -m pytest apps/api/app/tests/test_capability_resolver.py apps/api/app/tests/test_opportunity_feedback_api.py apps/api/app/tests/test_capability_override_types.py`
  (adjust the selection to the 4B-A test set).
- **Full backend suite:** `bash scripts/run-tests-api.sh`.
- **Ruff:** `apps/api/.venv/bin/python -m ruff check apps/api`.
- **PostgreSQL-backed tests:** run the backend suite with
  `TEST_POSTGRES_URL=postgresql+psycopg://signalnest:signalnest@localhost:5432/signalnest_test`
  (CI provisions `postgres:16`; requires a running Postgres locally).
- **Migration head/check cycle:** `alembic upgrade head` → `alembic check` →
  `alembic downgrade base` → `alembic upgrade head`.
- **Contract generation + clean-diff:** `npm run gen:types` then
  `git diff --exit-code apps/api/openapi.json apps/web/src/api/schema.d.ts`.
- **Frontend (as required by CI):** `npm run lint`, `npm run type-check`, `npm run test`,
  `npm run build`, `npm run test:ci-pipefail`.
- **Container/security + integration smoke:** the "Container build and security" and
  "Integration smoke" CI jobs (require Docker/Buildx; run via CI).

## 17. Approval gates and tranche separation

- Merging the **Phase 4B-0** plan does **not** authorize Phase 4B-A implementation
  automatically.
- **Phase 4B-A** needs a separate branch, PR, review, and protected merge.
- Merging **Phase 4B-A** does **not** authorize runtime activation automatically.
- **Phase 4B-B** needs a separate, explicit operational authorization.
- Any broader rollout needs another separately approved phase.

## 18. Evidence and closeout requirements

- **Phase 4B-0:** this plan authored, reviewed, and merged through the protected workflow;
  D1–D4 resolved; capabilities remain dark.
- **Phase 4B-A:** candidate SHA, test results, PR review, protected merge, and post-merge CI
  on the merge SHA; proof of no contract drift, no migration, no flag/override change, and
  no behavioral change under shipped configuration.
- **Phase 4B-B:** preflight evidence (§10), the exact scoped mutation, the audit record, the
  isolation checks (§11), the rollback exercise (§12), and the final effective state — all
  captured in a restricted `docs/verification/4b-b-*.md` record.
- **Across all tranches:** proof that every other capability and every non-canary workspace
  remained dark.

## 19. Proposed branch and PR sequence

- **Phase 4B-0:** `docs/phase-4b-plan` (this tranche).
- **Phase 4B-A:** `feat/phase-4b-a-feedback-gate-wiring` (created only when 4B-A is
  separately authorized).
- **Phase 4B-B:** a separately approved operations/verification branch name, selected only
  **after** 4B-A closes.

Neither later branch is created now.

## 20. Definition of done (Phase 4B-0)

Phase 4B-0 is complete only when:

- the plan exists and resolves D1–D4 at the proper level;
- the logical canary designation (`SignalNest Internal Phase 4B Canary Workspace`) is
  recorded;
- runtime ID verification remains an explicit Phase 4B-B prerequisite;
- fail-closed behavior is selected (D2);
- rollback and kill-switch layers are defined (D3, §12–§13);
- Phase 4B-A and Phase 4B-B are separated (§17);
- testing, observability, isolation, and stop conditions are explicit;
- all capabilities remain dark;
- the documentation PR is reviewed and merged through the protected workflow.

**Phase 4B itself is not complete at the end of Phase 4B-0.**

---

## Appendix — Recorded decisions

**D1 — Initial workspace.** Logical designation `SignalNest Internal Phase 4B Canary
Workspace`. No runtime UUID/PK/credential/secret/token/email is committed. The exact
`organization_id`/`workspace_id` must be independently resolved and verified against the
target environment immediately before Phase 4B-B and recorded in the restricted 4B-B
verification evidence. Phase 4B-A does not need the runtime ID; Phase 4B-B is blocked until
it is verified.

**D2 — Dependency failure posture: fail-closed.** A resolver, database, override-storage, or
capability-decision failure must never enable opportunity feedback. The future gate rejects
before any write and returns the repository-standard safe error response. Observability
distinguishes an intentional disabled decision from an unexpected resolver/dependency
failure. No new public response contract is introduced in this tranche; 4B-A preserves
current public response shapes unless a contract change is proven necessary and separately
authorized.

**D3 — Kill switch and rollback (layered):** (1) primary scoped rollback = clear the canary
workspace override; (2) global `opportunity_feedback_enabled` stays `False` throughout;
(3) resolver deny-biased / safety-ceiling behavior preserved; (4) code rollback = revert the
4B-A wiring commit if the integration is defective. Setting an already-`False` global flag to
`False` is not, by itself, a useful canary rollback.

**D4 — Required sequence:** Phase 4B-0 (approve this plan) → Phase 4B-A (wire the gate to the
resolver, keep all capabilities dark, merge and verify independently) → Phase 4B-B (verify
exact internal org/workspace identity, create one scoped enable override, verify isolation,
perform and verify a rollback exercise, re-enable the same canary only if separately
authorized). Any second workspace, customer rollout, broader canary, global-flag change, or
another capability's activation requires a later separately authorized phase.
