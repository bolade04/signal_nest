# AWS staging internal tenant and access plan (Phase 4B-C · INFRA-8)

- **Status:** PLANNING / GAP ANALYSIS ONLY. Per the INFRA-8 entry in
  [phase-4b-c-infra-plan.md](../phase-4b-c-infra-plan.md): expected repository
  areas are `docs/` (application-gap implementation happens only in a
  **separate reviewed PR**), expected external resources are **"none created
  by this planning tranche"**, live provisioning-surface verification happens
  **"only in INFRA-9, under authorization"**, and the exact stop boundary is
  **"no tenant/user/session created outside an authorized run."** Authoring
  and merging this document creates no tenant, user, membership, session,
  role assignment, or any other record, anywhere.
- **Authoritative parents:**
  [aws-staging-runtime-contract.md](./aws-staging-runtime-contract.md) §I
  (locked tenant topology, session independence, supported-surfaces rule),
  [aws-staging-observability-incident-readiness.md](./aws-staging-observability-incident-readiness.md)
  §6/§7/§9 (observer access requirements this plan implements against;
  evidence destination; redaction matrix),
  [4B-B evidence template](../verification/4b-b-feedback-canary.md), and the
  application code cited by file below (evidence, not restatement).
- **Sanitization rule (public repository):** this plan uses **logical aliases
  only**. No live tenant/user/membership/subject identifier, credential,
  token, or link may ever appear in this document, its future revisions, or
  any committed evidence. The historical "G2" gate label has **no referent**
  on current main and is not used.

## 1. Scope and boundaries

**In scope (this document):** the internal tenant topology, the identity/role
inventory contract, the ordered provisioning sequence over **supported
application surfaces only**, the gap register (missing surfaces → separate
reviewed PRs), the security contract with existing-test evidence, restricted-
evidence binding, rollback limits, and the INFRA-9 execution handoff.

**Out of scope (each separately owned):** executing any step here (INFRA-9,
fresh authorization); implementing any gap (separate reviewed PRs per the
phase plan); IAM/infra changes (flagged only); INFRA-7's redaction matrix,
retention, and incident framework (locked — referenced, never restated);
canary activation (its own later authorization). **No SQL provisioning,
ever** — a missing surface is flagged as an application gap, "not worked
around with SQL" (runtime contract §I, verbatim rule).

## 2. Internal tenant topology (locked by runtime contract §I)

Two internal organizations, three internal workspaces, no customer data:

| Logical alias | Kind | Holds | Purpose |
| --- | --- | --- | --- |
| `ORG_CANARY` | organization | `TARGET_CANARY`, `SAME_ORG_SIBLING` | The canary org: target workspace + same-org isolation control |
| `ORG_CONTROL` | organization | `CROSS_ORG_CONTROL` | Cross-organization isolation control |

Workspace aliases are the runtime contract's own logical names; the contract
requires two organizations without naming them, so `ORG_CANARY`/`ORG_CONTROL`
are **this plan's own derived aliases** for those two required organizations
(no count or workspace name is changed). Organizations are
SignalNest-controlled, contain no customer identity or integration, and
exist only after the INFRA-9-authorized run executes §4.

## 3. Identity and role inventory contract

The application's real role vocabulary (`apps/api/app/core/enums.py`):
`owner`, `admin`, `marketer`, `reviewer`, `viewer`, `compliance_reviewer` —
plus the org-membership-independent **operator** trust boundary
(`User.is_operator`, server-controlled). There is **no literal "editor" or
"observer" role**: "editor" is the shipped Phase-3C capability tier
`EDITORS = owner|admin|marketer` (`apps/api/app/feedback/routes.py:57`), and
"observer" is an access pattern (read-only), not an enum value.

| Alias | Principal type | Login | Roles / trust | Provisionable via supported surfaces today? | Notes |
| --- | --- | --- | --- | --- | --- |
| `IDENT-OP` (executing operator) | synthetic internal human | yes | `is_operator=True`; no org membership required (the `/internal/system/*` plane is tenant-independent) | **NO — GAP-1** | Runs the override PUT/DELETE plane during the authorized window |
| `IDENT-OBS` (independent observer) | synthetic internal human | yes | `is_operator=True`, used **read-only by convention** (GET planes only) | **NO — GAP-1 (+GAP-3)** | Distinct person, distinct account, distinct session from `IDENT-OP` |
| `IDENT-EDIT` (feedback-exercising member) | synthetic internal human | yes | registrant of `ORG_CANARY` → `owner` (∈ EDITORS tier) | **YES — end-to-end today** | Registers org, creates both `ORG_CANARY` workspaces, runs the feedback smoke test |
| `IDENT-VIEW` (viewer, 403-gate check) | synthetic internal human | yes | `viewer` member of `ORG_CANARY` | **NO — GAP-2** | Exercises the 4B-B "editor-role gate enforced (viewer 403)" line |
| `IDENT-CTRL` (cross-org control owner) | synthetic internal human | yes | registrant of `ORG_CONTROL` → `owner` | **YES — end-to-end today** | Creates `CROSS_ORG_CONTROL`; exercises cross-org denial |

**The "three independent sessions"** (runtime contract §I) are
`IDENT-OP`, `IDENT-OBS`, and `IDENT-EDIT` — three distinct users, three
separate logins, three bearer tokens, no shared cookies/tokens/client state.
`IDENT-VIEW`/`IDENT-CTRL` hold their own additional independent sessions for
their specific checks. Uniqueness key = email (409-guarded by
`auth/service.py:26-28`); idempotency key = the logical alias→email mapping
held in the restricted execution record; synthetic aliases use the reserved
`@example.com` convention already uniform across the test suite — **never a
real personal address**. Every *generated* identifier (user/org/workspace/
membership ids, subject ids, tokens, credentials) is **restricted evidence**
(§7). No identity here is a customer, service account, AWS IAM role, database
role, or GitHub identity — those are distinct planes and never conflated.

## 4. Provisioning sequence (supported surfaces only — executed only under INFRA-9 authorization)

Ordered, idempotent, fail-closed. Every step is an application API call; no
SQL, no seed script in staging, no direct DB write, ever.

0. **Preconditions:** staging stack deployed and healthy (INFRA-9); explicit
   environment verification (the operator confirms the base URL is
   SIGNALNEST_STAGING — a process control; the API has no wrong-environment
   guard of its own); pre-check via supported reads that **no unexpected
   record** already occupies the alias namespace — a 409 on a supposedly
   fresh alias is a STOP/investigate signal, never retried around
   (fail-report-don't-adopt).
1. `POST /auth/register` × `IDENT-EDIT` → creates the user + `ORG_CANARY` +
   `owner` membership (`auth/service.py:25-43`; open, ungated, sends no
   email — no mail transport exists in the codebase).
2. `POST /organizations/{ORG_CANARY}/workspaces` × 2 (as `IDENT-EDIT`) →
   `TARGET_CANARY`, `SAME_ORG_SIBLING` (member-gated,
   `organizations/routes.py:55-79`).
3. `POST /auth/register` × `IDENT-CTRL` → user + `ORG_CONTROL` + `owner`.
4. `POST /organizations/{ORG_CONTROL}/workspaces` × 1 → `CROSS_ORG_CONTROL`.
5. **`IDENT-VIEW` provisioning — blocked by GAP-2** (no add-member /
   role-assignment surface exists). Executes only after the GAP-2 separate PR
   ships a supported, owner/admin-gated membership surface.
6. **`IDENT-OP` / `IDENT-OBS` operator grant — blocked by GAP-1** (no
   supported `is_operator` grant surface exists; see the first-operator
   bootstrap decision in §5). Register the two users via the supported
   surface; the operator-trust grant itself awaits GAP-1's resolution.
7. `POST /auth/login` per identity → independent bearer sessions; distinct
   client profiles; no token or account is ever reused across roles.
8. Verification reads (all supported): member workspace listings, the
   operator effective/overrides GET plane (post-GAP-1), and the staging
   equivalents of §6's assertions performed through the 4B-B template's
   isolation matrix (the local test suite itself runs in CI, not against
   staging — the template is the staging verification vehicle).

Credentials are generated out-of-band at execution time, never typed into
Claude/CI/chat, never committed, and are restricted values from birth.

## 5. Gap register (the core deliverable — each item = a separate reviewed PR or later tranche; none is fixed here)

| Gap | Evidence | Impact | Disposition / recommendation |
| --- | --- | --- | --- |
| **GAP-1: no `is_operator` grant surface.** The flag is server-controlled with no HTTP surface anywhere that sets it; only the dev/test-gated demo seed writes `True` (`organizations/models.py:19-25`, `db/seed.py:212-219`). | grep-verified; `require_operator` consumes only | Blocks `IDENT-OP` and `IDENT-OBS`; without it the override plane and the observer read plane are unreachable in staging | Separate PR: a supported, operator-gated operator-grant surface — which surfaces the **first-operator bootstrap decision** (an operator-gated grant cannot mint the first operator). Options for that single targeted human decision: (a) an environment-boot bootstrap path with explicit one-time authorization semantics, or (b) extending the seed's env-gate to a staging-bootstrap mode. A third alternative — a one-time direct database write — was considered and **REJECTED**: it violates this plan's own "no SQL provisioning, ever" rule and is **not an available option at any authorization tier**. This plan recommends (a). |
| **GAP-2: no member/role-assignment surface.** Registration hardcodes `owner` of a brand-new org (`auth/service.py:41`); no invite/add-member/role-change endpoint exists in any router. | grep-verified across all `routes.py` | Blocks `IDENT-VIEW` (the 4B-B viewer-403 line) and any multi-member org; also means role escalation is currently *structurally impossible* (a clean security property to preserve) | Separate PR: an owner/admin-gated add-member + role-assignment surface using the existing `Role` enum and rank map (`auth/dependencies.py:23-30`), with positive/negative authorization tests mirroring `test_opportunity_feedback_api.py` |
| **GAP-3: no observer primitive.** The effective/overrides read plane is operator-gated; no read-only-observer construct exists distinct from operator. | `internal_capabilities_routes.py` (all `require_operator`) | The observer must be a second operator identity | **Recommended acceptance for 4B-B:** `IDENT-OBS` = second `is_operator` account used read-only **by convention**, with the convention enforced procedurally (observer never calls PUT/DELETE; evidence template §15 records the read-path proof) — consistent with INFRA-7 §7's access requirements. A dedicated read-only observer role is deliberately deferred (heavier than the canary needs); revisit before any production design. |
| **GAP-4: no deactivation/revocation/membership-removal surface.** `is_active` is only ever set at construction; nothing writes it `False`; no membership-removal endpoint; stateless JWTs have no revocation list. | `organizations/models.py:18`, `auth/service.py:50-51`, `auth/dependencies.py:52` | "Rollback via supported surfaces" is today limited to: stop using credentials + natural token expiry (`access_token_expire_minutes`). A live synthetic identity cannot be revoked through any supported surface. | Separate PR (pre-canary recommended): a supported deactivation surface. Until it exists, INFRA-9's runbook must not claim a revoke-via-API rollback path. |
| **GAP-5: observer AWS-side read access.** INFRA-7 §7 requires observer read access to CloudWatch log groups/alarms/dashboard and CloudTrail; the merged `iam` module scopes only service roles (execution/task) — no human operator/observer IAM path exists. | `infra/aws/modules/iam` contract | Observer cannot see the CloudWatch surface without an AWS-side grant | Flag only: a separately authorized IAM/console-access tranche (NOT an INFRA-8 or unreviewed `iam`-module change). |

**Notes (recorded, not gaps):** `POST /organizations/{id}/workspaces` has no
role floor (any member may create — acceptable for the internal topology;
worth a floor when GAP-2 ships); no auth-specific rate limiting/lockout
exists on `/auth/register`/`/auth/login` beyond the generic per-client
`RateLimitMiddleware`; registration writes an `auth.register` audit row but
login writes no audit event (flag for INFRA-9's evidence wants); a dedicated
cross-org **workspace-id substitution** negative test (authenticated outsider
probing another org's workspace id) does not yet exist — recommended as a
separate test addition before INFRA-9's surface verification.

## 6. Security contract (enforced today — evidence, not aspiration)

- **Server-side tenant derivation:** workspace scope is a **path parameter**
  resolved through membership (`get_tenant_context` →
  `auth/dependencies.py:83-99`; `_membership` :71-80 → 403). No
  workspace/org header is read anywhere in the app (the only trusted headers
  are `Authorization` and the tracing ids). Client-invented tenant scope is
  structurally impossible.
- **Session independence:** stateless HS256 bearer JWTs
  (`core/security.py:33-43`); every login mints an independent token; no
  server-side session state. Operationally binding: three distinct users,
  three separate logins, separate client profiles; reusing one token or one
  account across roles is prohibited.
- **No client-writable escalation:** `RegisterRequest` carries no role field;
  registration always yields `owner` of a **new, isolated** org and
  `is_operator=False`. No surface changes another user's role or the
  operator flag. Synthetic identities therefore hold exactly
  owner-of-their-own-sandbox and nothing more — no admin/superuser/
  infrastructure/deployment/secret authority of any kind.
- **Existing-test evidence (cited, reused — not re-authored):**
  cross-tenant + role-gate + non-member + unauthenticated coverage in
  `test_opportunity_feedback_api.py` (viewer 403 :439-442; non-member 403
  :435-437; marketer/admin allowed :444/:449; cross-workspace 404 :465/:474;
  no cross-market leakage :490-496); HTTP-level isolation in
  `test_api_isolation.py`; operator gating 401/403/200 in
  `test_operator_capabilities_effective_api.py:185-191`; dark-default 503 in
  `test_feedback_capability_gate.py`; flags-remain-False assertion in
  `test_operator_capabilities_effective_api.py:231`.
- **No email/notification side effects:** the codebase contains no mail
  transport; `POST /auth/register` writes to the database and returns a
  token — nothing is sent, no verification loop exists.
- **Schema sufficiency:** everything above uses existing columns and
  constraints at alembic head `98289430a3ec` (12 migrations). **No migration
  is needed or permitted by this tranche.** The gaps in §5 are missing API
  surfaces over existing schema.

## 7. Restricted-evidence binding

Bound to the current taxonomy (INFRA-7 doc §6/§9 + the 4B-B template header;
"G2" is stale and unused): live tenant/user/membership/subject identifiers,
credentials, tokens, and any link resolving to a live synthetic identity or
session are **restricted** — they never enter this repository, Git history,
PR text, CI logs/artifacts, agent/Claude transcripts, tofu plans/state, or
project memory, in any form including encodings, hashes, or fragments. Filled
execution records live only in the INFRA-7 §6 secure evidence destination
(concrete system = the standing operator decision, selected before the canary
is scheduled). The application's own `audit_logs` table is an authorized
in-app destination for ids per the INFRA-7 redaction matrix — distinct from
committed evidence.

## 8. INFRA-9 execution handoff (what "done" looks like at execution time)

Prerequisites before the §4 sequence may run: INFRA-9 stack deployed and
healthy; GAP-1 and GAP-2 surfaces shipped via their separate reviewed PRs
(GAP-4 recommended); the first-operator bootstrap decision made and recorded;
the evidence destination selected; explicit fresh human authorization naming
the run. Then: execute §4, capture the restricted execution record in the
approved destination, re-verify §6's assertions against the running staging
app (the phase plan's "provisioning-surface verification... only in
INFRA-9"), and complete the 4B-B template's identity/observer/contact lines.
Rollback of a partial run is limited by GAP-4 and must follow
fail-report-don't-adopt for any unexpected record.

## 9. What merging this plan does and does not do

Does: lock the internal tenant topology binding, the five-identity inventory
and three-session model, the supported-surface provisioning sequence, the
five-gap register with dispositions and the single first-operator bootstrap
decision to be made, the security contract with test-evidence citations, and
the restricted-evidence binding.

Does not: create any tenant, user, membership, session, or role assignment;
touch application code, infrastructure, schemas, or workflows; run anything
live; implement any gap; change any flag (all five remain `False`); alter
alembic (`98289430a3ec`/12). Every live step occurs only inside the INFRA-9
authorization; every gap fix occurs only in its own separately reviewed PR.
