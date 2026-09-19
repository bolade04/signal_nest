# Phase 5B — Approval Workflow and Audit Spine

**Parent:** `docs/project-phase-5-plan.md`. **Status:** PLANNED / DOCS-ONLY —
authorizes nothing. **Schedule slot (continuous round, master plan §5):**
contracts frozen in CF-1 (D1–D2); build **D6 AM–D7 AM** (four rebalanced lanes
W1–W4, longest lane 1.38 against a 1.5-day window); **I-5B integration cycle D7 PM**;
security-weighted **R3** wave D8 AM; the early-stop checkpoint record (**a recovery
boundary only — never Phase 5 closeout**) is authored **D8 PM** after R3 closes, so
it can cite R3's verdict and the post-merge SHA; the **operator continue-window is
D9 AM**, charged its own half-day; CF-2 is authored D9 PM only after the recorded
continue decision.

## 1. Objective

Make "a customer's authorized reviewer approved this" a **provable statement**:
append-only approvals bound to exact brief versions, an exact-set approver gate, a
minimal role-assignment surface, and an audit spine with a closed action vocabulary
and a typed, filterable read API. This milestone cures the two authorization
defects found by the definition audit (rank-floor widening; unreachable reviewer
role) without changing `require_role` semantics anywhere else.

## 2. Frozen contracts (fixed at CF-1)

**New dependency `require_exact_roles(*roles)`** (in `auth/dependencies.py`,
additive): passes **only** when `ctx.role` is exactly in the listed set — no rank
arithmetic. Applied to: the new approval-decision endpoints, the role-assignment
endpoints, and (per operator decision O-1, RESOLVED 2026-09-01:
`REC_require_exact_roles_incl_audit_route_cure`) the existing audit read route
`audit/routes.py`, curing the live over-permission where marketers and reviewers
pass a `require_role(OWNER, ADMIN, COMPLIANCE_REVIEWER)` floor. `require_role` is
left untouched at every other call site in this round.

**Role assignment (minimal surface — organization-scoped, because that is what
the data model holds):** role lives on `organization_members.role` (unique on
`(organization_id, user_id)`), and `get_tenant_context` derives every
workspace's role from the organization membership. There is no workspace-member
table, so a role grant is **organization-wide**: granting `reviewer` confers
approver authority in every workspace of the organization. The endpoints and the
UI must say so; introducing workspace-scoped roles is a possible future change,
not 5B.

- `PUT /organizations/{org_id}/members/{user_id}/role` — exact-set
  owner/admin gate; assigns one of the closed `Role` vocabulary; audited.
  **Privilege ceiling (enforced + negative-tested):** no actor may assign a
  role ranked above their own; only an `OWNER` may grant or revoke `OWNER`;
  cannot demote the last owner; cannot change one's own role (self-elevation,
  including by-proxy owner-minting from an admin, is blocked by the ceiling).
- `GET /organizations/{org_id}/members` — list memberships and roles.
  **Gate (stated explicitly rather than left for the builder to infer):**
  readable by any authenticated member of that organization; it exposes
  member identity and role within an org the caller already belongs to.
  **Non-members receive 403, not 404** — verified: `_membership`
  (`apps/api/app/auth/dependencies.py:71-80`) and `_assert_member`
  (`apps/api/app/organizations/routes.py:18-27`) both raise
  `PermissionDeniedError`, which `apps/api/app/core/errors.py:34-36` maps to
  **403**. Only the `PUT` sibling carries the exact-set owner/admin gate.
- `Role.REVIEWER` becomes the approver role (operator decision O-2, RESOLVED
  2026-09-01: `REC_Role_REVIEWER_plus_org_scoped_assignment`). No invite
  flow in this phase — assignment applies to existing members only.

**New table `approvals`** (append-only; the `OpportunityFeedback` discipline — a
change of mind is a new row):

- Scope: `id`, `organization_id`, `workspace_id` (FK shape per operator decision
  O-6, RESOLVED 2026-09-01: compliance records survive workspace deletion via
  FK-less scope columns, with the rationale recorded; per the O-6 rider,
  survival is subject to the future O-5 retention policy and must not mean
  indefinite retention by default), `subject_type` (closed: `recommendation_brief` in 5B; `creative_draft`
  added in CF-2), `subject_id`, `subject_version` (binds the decision to an exact
  immutable version), `brief_id` FK for 5B subjects.
- Decision: `decision` (closed `ApprovalDecision`: `approved`, `rejected`,
  `revoked`), `reason_code` (closed vocabulary with the frozenset polarity
  pattern), `note` (bounded text), `decided_by` (FK, SET NULL) **plus a non-FK
  `decided_by_snapshot`** (immutable string: user id + display identity at
  decision time — the approver's identity on this compliance record survives
  user deletion), `decided_role` (snapshot), `is_simulated`.
- Constraints: portable check constraints on `decision`/`reason_code`/
  `subject_type`; `UniqueConstraint` on `(subject_type, subject_id,
  subject_version, decision_seq)` with a monotonic `decision_seq` whose
  allocation is serialized per subject via the row-lock pattern already
  established at `capabilities/service.py` (`_workspace_lock_select`) so
  concurrent decisions cannot race the sequence.
- **The controlling decision for any subject version is the row with the
  highest `decision_seq`** — every downstream consumer (5E export above all)
  gates on the latest decision, never on "an approved row exists".
- **Capability gating:** the approval endpoints are gated by the same
  `recommendation_briefs_enabled` capability as the brief surfaces (fail-closed
  resolver pattern) — in 5B, approvals act only on briefs, so the customer-facing
  5A+5B surface shares one dark switch.
- **Separation of duties (enforced in service + tested):** the acting user must
  hold the approver role via `require_exact_roles`, and must not be the author of
  the subject version being approved.

**Audit spine hardening:**

- **Closed `AuditAction` registry** (pattern: `capabilities/registry.py` — frozen
  entries in a `MappingProxyType`): every existing literal action string is
  enumerated; the one interpolated site (`opportunity.{status}`) is replaced by
  per-status registry members. `record_audit` validates the action against the
  registry (service-layer enforcement; no retrofit DB constraint in this round —
  existing rows keep their values).
- **Single-seam append-only:** `AuditLog` construction remains only inside
  `record_audit` (`audit/service.py`) — a test pins this with an AST/grep-level
  assertion; no update/delete path exists anywhere; the module docstring states
  the append-only guarantee and its current enforcement level.
  **Disclosed gap in the append-only claim (verified):** `AuditLog` inherits
  `TimestampMixin` (`apps/api/app/audit/models.py:12`), which defines
  `updated_at` with `onupdate=utcnow` (`apps/api/app/db/base.py:33-39`). The
  audit table therefore carries a **mutation-tracking column with an update
  hook**, which is not what an append-only table should have. 5B must either
  drop `updated_at` from `AuditLog` in the CF-1 migration or document precisely
  why it is retained, and must add an **ORM-update-raises test for
  `audit_logs`** — the 5A append-only convention specifies such a test for new
  tables only, so the audit table currently has none. **DB-level
  immutability (restricted DB role or trigger) is explicitly out of scope** and
  recorded as a separate infra follow-up (operator decision O-10, RESOLVED
  2026-09-01: `REC_app_layer_append_only_document_db_level_as_separate_infra`)
  because the runtime app role currently owns the database.
- **Typed read API:** `AuditLogOut` response model (surfacing `previous_state`/
  `new_state`), filters (`entity_type`, `entity_id`, `action`, time range),
  cursor pagination; gated by `require_exact_roles(OWNER, ADMIN,
  COMPLIANCE_REVIEWER)`.
- **Login auditing:** `auth.login` success and failure actions added (register is
  already audited); no new auth mechanics otherwise.
- Approval writes and their audit rows commit **in the same transaction**
  (the capability-override precedent).

## 3. Entry criteria

1. I-5A integrated and R2 clean (briefs exist).
2. Operator decisions O-1, O-2, O-6, O-10 — RESOLVED 2026-09-01
   (`DECISIONS.PP5-O1-O11.md`).

## 4. Exit criteria (all TRUE with artifacts)

1. Migration round-trips in CI; single head preserved.
2. `require_exact_roles` behaves as an exact set (tests: every role in/out of the
   set, including the marketer-vs-audit-route regression test that fails under the
   old floor semantics).
3. Role assignment works, is audited, refuses last-owner demotion,
   self-change, above-own-rank assignment, and non-owner OWNER grants (each a
   negative test); an **organization** can provably hold a `reviewer` member,
   and the org-wide effect of a grant is asserted in a test spanning two
   workspaces of one organization.
4. Approvals are append-only, bind exact subject versions, and enforce
   author≠approver (negative test).
5. Every audit write passes `AuditAction` validation; the single-construction-site
   pin test is green; the audit read route returns typed, filtered, paginated
   results and appears in the OpenAPI contract (drift gate green).
6. Login success/failure audited (tests).
7. Isolation negatives distinguish **two classes**, because the repository applies
   a different convention to each and conflating them makes the criterion
   unsatisfiable by the natural implementation:
   (a) **entity-id-level** cross-workspace or cross-org mismatch — an approval
   `subject_id`, audit filter, or member `user_id` that exists in another tenant —
   answers the uniform **non-enumerating 404** (the
   `capabilities/service.py:95-99` pattern), not 403, so "exists but not yours" is
   indistinguishable from "does not exist";
   (b) **the organization-membership gate itself** — the caller holds no role in
   the target organization at all — answers **403**, via the universal
   `PermissionDeniedError` convention (`auth/dependencies.py:71-80`,
   `organizations/routes.py:18-27`, mapped at `core/errors.py:34-36`). Asserting
   404 for class (b) would contradict every other route in the codebase; it is the
   existing convention, not a regression, and this round does not change it.
8. Early-stop checkpoint record producible (a **recovery boundary only — never
   Phase 5 closeout**, and it may not assert `PROJECT_PHASE_5_BUILD_COMPLETE`):
   with `recommendation_briefs_enabled`
   off, all 5A+5B **customer surfaces** (briefs + approvals) are dark and
   fail-closed, and the checkpoint record **names the deliberately live,
   un-flagged deltas** exactly as master plan §9 enumerates them (audit-route
   narrowing, org-scoped role assignment, typed audit read API + login audit,
   5A LLM-seam hardening). This criterion deliberately does not claim blanket
   inertness — 5B carries no feature flag of its own, and its administrative
   surfaces ship live.

## 5. Builder lanes and exclusive path ownership

| Lane | Exclusive owned paths (exact) | Delivers | Lane-days |
|---|---|---|---|
| **5B-W1** Primary domain | **Model moved to the freeze payload (master plan §6.2.0):** every ORM model for a table CF-1 creates — with its constraints, defaults, indexes, relationships, its migration and its `apps/api/app/db/models.py` registration line — is authored by the integration owner in the CF-1 atomic payload, not in this window. This lane retains the schemas, service, routes and tests for that directory. `apps/api/app/approvals/` (new, whole directory). **Forward-touch disclosure:** `approvals/service.py` is touched later by 5C-W1 (blocked-review precondition, **D11 PM–D13 AM**) and 5E-W3 (notification emission, **D20 AM–D22 PM**) — different windows, never concurrent. **Cross-authored slice — tests W2's seam code:** `apps/api/app/tests/test_audit_registry.py`, `test_audit_read_api.py` | approvals model/service/schemas/routes, `decision_seq` under `_workspace_lock_select`, latest-decision rule, author≠approver | **1.34** |
| **5B-W2** Seam | `apps/api/app/auth/dependencies.py`, `auth/routes.py`, `apps/api/app/audit/` (all files incl. new `actions.py`), `apps/api/app/organizations/routes.py`, `apps/api/app/opportunities/routes.py` (sole change: per-status registry members replacing the interpolated action). **Cross-authored slice — tests W1's domain code:** `apps/api/app/tests/test_approvals_behaviour.py` | `require_exact_roles`, audit-route cure, org-scoped role assignment + privilege ceiling, closed `AuditAction` registry, typed paginated audit read, login auditing | **1.31** |
| **5B-W3** Primary UI | `apps/web/src/pages/approvals/` (new, whole directory, **excluding** its `__tests__/`); `apps/web/src/pages/settings/` (new, whole directory, **excluding** its `__tests__/`); **`apps/web/src/pages/Settings.tsx`** (host surface that `apps/web/src/pages/settings/MembersList.tsx` and `apps/web/src/pages/settings/MemberRoleDialog.tsx` link from — named exactly; both are inside the whole-directory claim above). Authors **no** tests | approval queue, decision UI, member role-management UI | **1.30** |
| **5B-W4** Adversarial + UI verification | `apps/api/app/tests/test_role_assignment.py`, `test_audit_isolation.py`, `test_approvals_isolation.py`; **W3's UI tests:** `apps/web/src/pages/approvals/__tests__/`, `apps/web/src/pages/settings/__tests__/`, `apps/web/src/pages/__tests__/settings-runtime.test.tsx` | **the whole adversarial battery** (role-ceiling negatives, audit and approval isolation) plus verification of W3's surfaces | **1.38** |

**Longest lane 1.38 against a 1.5-day window (local float +0.12 — the round's tightest
window, spendable only inside 5B).** This is the operative statement; the header block
restates it as a **derived** summary and must be propagated in the same edit. An earlier
draft carried this paragraph twice and then claimed it was stated only once — both the
duplication and the false uniqueness claim are corrected here.
Per-lane figures indicative within ±0.1. Utilisation against the 1.5-day window is
**89/87/87/92%** (W1 1.34, W2 1.31, W3 1.30, W4 1.38) — an earlier draft quoted
97/95/91/100%, which reconciled against neither the window nor the longest lane.

Both UI surfaces sit in W3 and both test sets in W4, so the shared frontend contract
files are needed by exactly one lane; the `types.ts`/`endpoints.ts` entries for **both**
the approvals and settings surfaces are authored into the **CF-1 payload** by the
integration owner. Those files are freeze-owned, never lane-owned (master plan §6.3).

Freeze-owned files per master plan §6 — including `apps/web/src/api/types.ts`,
`endpoints.ts` and **`queryKeys.ts`**, which are freeze-owned at every site and never
lane-owned (master plan §6.3). Note 5B-W2 owns `audit/routes.py` — the
route-gate change there is O-1's cure and is inside this lane's exclusive paths.
Frontend tests are authored by **W4**, never by the lane that owns the page
(master plan §6.1: W3 authors no tests for its own code). After I-5B,
`apps/api/app/audit/actions.py` (the closed `AuditAction` registry) joins the
freeze-owned set per master plan §6 — later milestones add actions only at CF-*
gates via the integration owner.

## 6. Out of scope

Creative generation (5C); export (5E); DB-privilege/trigger changes (O-10
follow-up); invite/registration changes; JWT/session redesign (the 12h-token and
revocation gaps are recorded as a pre-activation hardening follow-up, not 5B
scope); any `require_role` change beyond adding the new dependency.

## 7. Rollback

Additive-only; revert PR(s); migration downgrades cleanly; flags never enabled.
The audit-route gate change is behavior-affecting (narrowing) and is called out in
its PR description explicitly; reverting restores the prior (wider) behavior.
