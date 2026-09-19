# Phase 5E — Notifications, Status Analytics, Audited Manual Export, and Closeout

**Parent:** `docs/project-phase-5-plan.md`. **Status:** PLANNED / DOCS-ONLY —
authorizes nothing. **Schedule slot (continuous round, master plan §5; O-11
RESOLVED 2026-09-01 — the committed-round/fast-follow split is rejected):**
all 5E contracts are part of the single consolidated **CF-3** freeze (shared-surface
authoring D15 PM, integration cycle D16 AM, reviewed D16 PM); the entire 5E scope —
audited manual export, in-app notifications, status analytics — builds in **one
four-lane window, D20 AM–D22 PM (3.0 days)**. **The 3.0-day window is an operator
decision recorded 2026-09-02**, and **0.50 day of it is a reachable 5E remediation
reserve** that may not be relabelled as tail-only float or used to conceal work
assigned elsewhere. It absorbs the measured analytics test increment of **0.52
lane-days**, charged **once** and distributed only to lanes permitted to own those
artifacts (§5): 5E's lane loads are W1 **2.18**, W2 1.80, W3 1.67, W4 **1.90**, so the
longest lane is **2.18** and the local float is **+0.82**. The **I-5E integration
cycle** runs D23 AM; the **R-5E** scope wave and the **R-final** whole-round wave run
concurrently on the same frozen snapshot D23 PM; the **Phase 5 closeout record** is
produced D24 PM and its integration cycle D25 AM. There is no earlier closeout of any
kind. This header block is a **derived** summary of §5; §5 is operative and any change
must be propagated here in the same edit.

## 1. Objective

Close the Guided Action loop's tail: in-app notifications, derived read-only status
analytics, and **audited manual export** of approved creative text — dark behind
`content_export_enabled` (global default `False`, fail-closed) — then close the
round with a Phase-4-style evidence-backed checklist. Export is manual by
definition: a human downloads/copies reviewed text; nothing publishes anywhere.

## 2. Frozen contracts (all fixed at the consolidated CF-3 freeze — shared surface D15 PM, integration cycle D16 AM, reviewed D16 PM)

**Flag/capability contract:** `content_export_enabled: bool =
False` in `Settings`; **`Capability.CONTENT_EXPORT`** + `CapabilityPolicy` entry
(`workspace_enableable=True` per resolved O-3; `future_activation_phase="5E"`);
per resolved O-4 (`ALT_expose_all`) the flag is added to `FeatureFlagsOut` with
the exact-equality reflection test updated in the same CF-3 commit; new Alembic
migration widening the workspace-override check constraint (the established
`batch_alter_table` pattern); export routes gated through the **deny-biased
capability resolver** (the `_require_feedback_feature` pattern) — never a raw
`settings.content_export_enabled` read, so workspace-override precedence and the
operator safety-ceiling kill switch both apply. Secret-inventory reconciliation
94→95+N in the same change (corrected baseline, master plan §14) — the export flag plus any notification/retention
`Settings` fields, counted from the actual field list at the freeze (master
plan §14).

**Notifications (in-app only):**

- `notifications` table: **fully tenant-scoped** — `organization_id`,
  `workspace_id`, `user_id` columns with FKs; `event_code` from a **closed**
  vocabulary (brief created, approval decided, draft generated, review blocked,
  export completed); subject refs; `read_at`. `notification_preferences` table:
  same three scope columns plus `event_code` — per-user per-event-code opt-out,
  unique on `(workspace_id, user_id, event_code)`.
- **Isolation requirements (tested):** a user reads only their own
  notifications — cross-**user** access within a workspace is the live IDOR
  class here (notifications carry subject refs) and gets its own negative test,
  alongside the standard cross-workspace negative; both answer the uniform
  non-enumerating 404 for entity-id-level misses. (Note the two isolation classes
  distinguished in 5B §4 exit criterion 7: an entity-id mismatch answers a non-enumerating 404,
  whereas the organization-membership gate itself answers **403** via
  `PermissionDeniedError` — `apps/api/app/core/errors.py:34-36`. Do not "fix" the
  403 into a 404; that would be a live-auth regression.)
- API: list + mark-read. Written in the same transaction as the triggering
  event; the emission hooks edit `briefs/`, `approvals/`, and `creative/`
  service files, which are **assigned to lane 5E-W3 at CF-3 under the
  cross-milestone touch rule** (exact file list in §5). The `export.completed`
  emission is authored by lane 5E-W1 inside its own `exports/service.py`
  (single-window single-owner; **5E-W1** codes against the notifications service
  interface frozen at CF-3, which **5E-W3** lands early in the window — an
  intra-window ordering dependency, disclosed in §5. The retired `L1`/`L2` lane
  vocabulary previously used here was replaced by the W1–W4 rebalance in master plan
  §6.1, and as written it also named the wrong lane: `L2` maps to W2, the analytics
  seam, which does not own `apps/api/app/notifications/` — 5E-W3 does).
- Email/SMS are excluded (deliverability, unsubscribe compliance, per-jurisdiction
  consent — out of appetite for text-first Phase 5).
- Frontend: replace the hardcoded placeholder dropdown with the real feed.

**Status analytics (derived, read-only):**

- No new tables. Aggregation endpoints computed from existing rows
  (opportunities, briefs, approvals, drafts, reviews, exports), workspace- and
  location-scoped: counts by state, approval latency, generation/review outcomes,
  per-location comparison.
- **Authorization gate (stated explicitly rather than left for the builder to infer,
  per the standard this plan set for the approver gate).** Analytics reads are gated by
  **`require_exact_roles(OWNER, ADMIN, MARKETER, REVIEWER)`** on top of the workspace
  scope — named here so it is reviewable, and adjustable by the operator at CF-3 in the
  same way as the export role set. **This gate previously did not exist**, while
  5E-W4 was already charged with authoring *"authorization … negatives"* in
  `test_analytics_scope_boundary.py` — a negative test with no requirement to fail
  against. It matters because exit criterion 2(c) makes analytics **live and ungated at
  merge, returning real tenant data**, and the aggregates include **approval latency**
  and generation/review outcomes — the same sensitivity class the audit read route is
  deliberately narrowed for under resolved O-1.
- **The export-history read** `GET /workspaces/{ws}/drafts/{draft_id}/exports` carries
  the same gate. It exposes `exported_by_snapshot`, the immutable compliance identity
  that deliberately survives user deletion, so it is not a plain tenant-scoped read.
- The `RunStats` discipline carries over: incomplete states report `null`, never
  fabricated zeros. Advertising-performance attribution is **excluded** —
  deferred by definition-audit operator decision D-05 ("include operational
  metrics and customer-facing status analytics; defer advertising-performance
  attribution"); the D-series' provenance is recorded in master plan §14.

**Audited manual export:**

- `exports` table: scope FKs; `draft_id` + `draft_version` + `approval_id` —
  an export row binds the **exact approved creative version** and the approval
  that authorized it; `format` (closed: `text/plain`, `text/markdown`, `json`);
  `exported_by` (FK, SET NULL) **plus non-FK `exported_by_snapshot`** (immutable
  string identity at export time — the compliance record survives user
  deletion; per the O-6 rider, survival is subject to the future O-5 retention
  policy and must not mean indefinite retention by default); `is_simulated`.
  Append-only (`created_at` only; ORM-update-raises test — the 5A convention).
  **`jurisdiction_code` is native**: the consolidated CF-3 creates the table
  with the snapshot column, and 5D builds before the export service, so every
  export row ever written carries its jurisdiction snapshot — the rejected
  split's NULL-jurisdiction export cohort does not exist in this shape (see
  the 5D sub-plan §2).
- Service preconditions — **six**, all enforced, each with its own failing negative
  test. **Every one of these negatives is owned by lane 5E-W4**, because master plan
  §6.1 rule 2 keeps the whole security/isolation/negative battery in W4, and exit
  criterion 3 below classifies each of them as a *failing negative test*. W1's and W2's
  cross-authored slices are **behaviour** tests, never negatives. The six, each with
  its owning file:

  1. capability resolves enabled (deny-biased resolver, never a raw flag read) —
     `test_exports_capability_gate.py`;
  2. the draft's latest content review passed and is not blocked —
     `test_exports_refusal_preconditions.py`;
  3. **the latest-decision rule** — the highest-`decision_seq` approval decision for
     that exact `(subject_type, subject_id, subject_version)` is `approved`
     (an approve-then-reject sequence therefore refuses; "an approved row exists"
     is explicitly NOT the predicate) — `test_exports_refusal_preconditions.py`;
  4. the actor holds `require_exact_roles(OWNER, ADMIN, MARKETER)` (the operator may
     adjust the set at CF-3 — it is named so it is reviewable) —
     `test_exports_refusal_preconditions.py`;
  5. **`jurisdiction_code IS NOT NULL` on the exported draft and its brief — an
     explicit refusal, not an assumption** — `test_exports_jurisdiction_refusal.py`;
  6. **the subject is within the caller's tenant scope** — a cross-workspace subject
     is refused with the uniform non-enumerating 404 — `test_exports_isolation.py`.

  **Two defects are corrected here and recorded rather than silently fixed.** An
  earlier version of this block claimed "six" while enumerating only **five** — the
  tenant-scope precondition at (6) was required by exit criterion 3 and by
  `test_exports_isolation.py`'s existence, but was never written down. And it
  attributed every negative to 5E-W4 while naming only three files, so preconditions
  (2), (3) and (4) mapped to **no W4-owned file at all**; the only plausible home,
  `test_exports_preconditions.py`, sat in the 5E-W2 lane, which §6.1 rule 2 forbids for
  a negative battery. `test_exports_refusal_preconditions.py` is the W4-owned file that
  now carries them, and W2's slice is rescoped to behaviour and renamed accordingly
  (§5).

  Precondition (5) is defence-in-depth: the `exports` table
  is born jurisdiction-aware, but "born jurisdiction-aware" is a property of the
  *table*, not of every *row* that reaches it, and a brief created while 5A was
  activated at the D8 recovery boundary (before 5D existed) can carry a NULL
  jurisdiction. The export service refuses such rows rather than inferring or
  defaulting a jurisdiction. The export row and its
  `AuditAction` audit row commit in one transaction.
- API: `POST /workspaces/{ws}/drafts/{draft_id}/exports` (nested under 5C's
  canonical fetch-one draft handle) returns the rendered text inline (no signed
  URLs in this milestone — the unrevocable-signed-URL concern is thereby
  avoided); `GET /workspaces/{ws}/drafts/{draft_id}/exports` export history.

**Retention/deletion mechanism (mechanism only — policy is operator decision O-5):**

- Additive `deleted_at` soft-delete columns on the Phase 5 content tables
  (briefs, drafts, reviews, notifications) and a purge **design module** honoring a
  configurable retention duration per table class. **It creates NO `JobType` member and
  no handler registration, and this is deliberate.** `JobType` is a closed `StrEnum` in
  the freeze-owned `apps/api/app/jobs/status.py` whose own docstring states that
  enqueueing or executing any other type is rejected as a stable, non-retryable error,
  and `apps/api/app/jobs/handlers.py` is the freeze-owned sole registration point. 5C's
  durable job takes the full route — a `JobType` member plus a `jobs/handlers.py`
  import line, both CF-2 integration-owner payload, plus a lane-owned sibling handler
  module. **5E deliberately does not**: the purge mechanism is a plain callable under
  `apps/api/app/retention/` (5E-W1), never enqueued, so it is **unrunnable by
  construction** — there is no `JobType` under which it could be dispatched. That is
  what makes exit criterion 6 provable rather than merely asserted. An earlier version
  of this bullet called it a "durable job", which implied the 5C route and left the
  `JobType` member, the `handlers.py` import and the handler module owned by nobody.
  The mechanism ships **disabled with no default duration**; it cannot run until an
  explicit
  operator-set policy exists. Operator decision O-5 was recorded 2026-09-01 as
  `UNDECIDED_RESERVED_TO_OPERATOR_LEGAL` with the rider that the build proceeds
  with this disabled mechanism and **no 5C or 5E activation is permitted until
  an explicit retention/deletion policy is separately adopted**. Approval and
  export records follow resolved operator decision O-6 (survive as compliance
  records via FK-less scope columns), subject to the O-6 rider: survival is
  governed by the future O-5 policy and must not mean indefinite retention by
  default.

**Phase 5 closeout (end of round, **D24 PM** — the ONLY closeout):** a
Phase-4-style checklist — every exit criterion of 5A–5E TRUE with a cited
artifact; flags all `False` globally with zero overrides; single Alembic head;
contract drift green; whole-round integration suite green; final fresh review
**R-final** verdict recorded verbatim. `PROJECT_PHASE_5_BUILD_COMPLETE` is a
closeout assertion backed by that evidence, never a repo flag, and **it may
appear only in this end-of-round record: no D8 checkpoint, integration record,
wave record, or any other intermediate record may claim
`PROJECT_PHASE_5_BUILD_COMPLETE` or complete Phase 5A–5E closeout** (operator
decision O-11, resolved 2026-09-01).

## 3. Entry criteria

1. I-5C integrated and R4 clean (export needs approved creatives to exist in
   test fixtures); **I-5D integrated and R-5D clean** (the jurisdiction axis
   exists, so export/brief/draft snapshots are native); CF-3 reviewed (**D16 PM**).
   **Both gates are binding and neither may be overlapped:** 5E may not begin from a
   frozen I-5D head while R-5D is pending, because this criterion requires I-5D
   *integrated* — which requires the merge commit — *and* R-5D *clean*. This is why no
   approval wait preceding 5E is treated as parallelizable in the master plan's §5
   schedule.
2. Operator decisions: O-5 status confirmed — RECORDED 2026-09-01 as
   `UNDECIDED_RESERVED_TO_OPERATOR_LEGAL` (mechanism ships disabled; the rider
   blocks 5C/5E **activation** only); O-6 RESOLVED 2026-09-01 (FK-less
   survival with the not-indefinite-by-default rider; affects export/approval
   FK shape — from CF-1).

## 4. Exit criteria (all TRUE with artifacts)

1. Migrations round-trip; single head.
2. **Honest live-surface ledger (corrected — 'every 5E endpoint dark' was false).**
   Only **export** is capability-gated (`content_export_enabled` /
   `Capability.CONTENT_EXPORT`); with that flag off every export endpoint 503s.
   **Notifications and analytics have no flag and no capability** — O-4 fixes the
   Phase 5 flag set at exactly three, so a fourth cannot be introduced inside this
   plan. Their true state at merge is therefore **live, not dark**:
   (a) the notification **list** is live and returns empty until upstream emitters
   fire (those emitters are gated by their parent milestones' flags);
   (b) the notification **mark-read** route is a **live, ungated write** carrying
   the round's only new cross-user IDOR class — it is the highest-risk surface in
   5E and must be treated as such in R-5E. **R-5E carries a second binding
   instruction for the same reason:** `apps/web/src/api/queryKeys.ts` gains a
   **per-user** scope dimension in the CF-3 payload (D15 PM), where every existing
   key is at most workspace-scoped. The key factory is the frontend cache-isolation
   boundary, so this is a **design change to an isolation control, not a
   transcription**, and R-5E must review it as such rather than as a routine key
   addition. It is freeze-owned (master plan §6.3) and covered by the
   contract-equality checker, but neither control substitutes for the review;
   (c) **analytics aggregates over existing `opportunities` rows and therefore
   returns real, non-empty tenant data at merge** — it is live and NOT inert.
   Whether notifications/analytics should be flagged is an **operator decision**
   (a fourth flag would amend O-4) and is recorded here as an open question, not
   resolved by this plan. Notifications for 5A/5B events
   fire only when their parent features are enabled.
3. Export preconditions each have a failing negative test: unapproved draft,
   blocked review, **approve-then-reject (latest decision not approved)**,
   revoked approval, wrong version, wrong role, cross-workspace subject — all
   refuse with the uniform non-enumerating 404 where subject-scoped; the
   success path writes export + audit atomically.
4. Analytics endpoints return correct aggregates on seeded fixtures; incomplete
   states are `null`; cross-workspace and cross-location isolation negatives.
5. Notification feed works; mark-read is idempotent; preferences suppress;
   **cross-user and cross-workspace notification access negatives green** (the
   subject-ref IDOR class).
6. The purge **mechanism** provably cannot run without an explicit policy (test) —
   and, independently, cannot be dispatched at all, because §2 creates no `JobType`
   member for it.
7. Combined whole-round integration suite green; all governance pins
   (value-set/order, dark-by-default, `future_activation_phase="5E"` for the
   export capability, secret-inventory **94→95+N at the consolidated CF-3**,
   counted from the actual `Settings` field list at the freeze) green.
8. Phase 5 closeout checklist complete with artifacts (every 5A–5E exit
   criterion TRUE with a cited artifact); **R-final** verdict recorded
   verbatim. This is the only closeout; no intermediate record qualifies.

## 5. Builder lanes and exclusive path ownership (rebalanced W1–W4, one window D20 AM–D22 PM)

The entire 5E scope builds in a **single four-lane window of 3.0 days (D20 AM–D22 PM)**.
**The window has been grown three times, and every growth is recorded because each
corrected an error or implemented an operator decision rather than absorbing new
scope.**

**First growth, 1.5 → 2.0.** An earlier draft described 5E as 1.5 lane-windows with
"6 lane-days of measured need into 6 of capacity — exactly capacitated, zero slack".
That was wrong twice over: the pre-increment measured need is **6.88 lane-days**
(1.71 + 1.80 + 1.72 + 1.65), not 6.0, and against a 1.5-day window the longest lane of
1.80 carried a **negative** local float of −0.30.

**Second growth, 2.0 → 2.5.** The measured **0.52-lane-day** analytics test increment
(below) pushed the longest lane past 2.0 **under an assignment that has since been
withdrawn as rule-violating** (see the analytics subsection). Under the compliant
assignment the increment lands on W1 and W4, and the longest lane is **2.18** — still
past 2.0, so a 2.0-day window would carry a negative float of −0.18.

**Third growth, 2.5 → 3.0 — an operator decision recorded 2026-09-02, not an
estimating result.** The window is 3.0 days (12.0 lane-days of capacity against 7.55
of need), giving **+0.82 of local float**, of which **0.50 is a designated reachable
5E remediation reserve**. Both parts are spendable only inside 5E and are never
transferable across a contract freeze. The reserve exists because 5E carries the
round's only new cross-user IDOR class (the mark-read route) and is the last milestone
before closeout, so a finding here has the least room to recover.

| Lane | Exclusive owned paths (exact) | Delivers | Lane-days |
|---|---|---|---|
| **5E-W1** Primary domain | **Model moved to the freeze payload (master plan §6.2.0):** every ORM model for a table CF-3 creates — with its constraints, defaults, indexes, relationships, its migration and its `apps/api/app/db/models.py` registration line — is authored by the integration owner in the CF-3 atomic payload, not in this window. This lane retains the schemas, service, routes and tests for that directory. `apps/api/app/exports/` (new, whole directory), `apps/api/app/retention/` (new, whole directory). **Cross-authored slice — tests W3's notification code:** `apps/api/app/tests/test_notifications_feed.py`, `test_notifications_preferences.py`. **Analytics fixture and positive-path tests (cross-authored, W2's code):** `apps/api/app/tests/fixtures/analytics.py` (**cross-window disclosure: the `apps/api/app/tests/fixtures/` package is created by 5A-W4 in D3 PM–D4 PM; this milestone adds exactly this one module to it at CF-3. Different windows, never concurrent. Its registration line in the freeze-owned root `conftest.py` is CF-3 payload authored by the integration owner, not by this lane**), `apps/api/app/tests/test_analytics_aggregates.py`, `apps/api/app/tests/test_analytics_empty_state.py` | export service born jurisdiction-aware, six enforced preconditions incl. the `jurisdiction_code IS NOT NULL` refusal, three render formats, export history, `export.completed` emission, purge design module (disabled, no default policy, no `JobType` member — §2), **plus the analytics seeded fixture and positive-path battery** | **2.18** |
| **5E-W2** Seam | `apps/api/app/analytics/` (new, whole directory) **and** `apps/web/src/pages/analytics/` (new, whole directory, **excluding** its `__tests__/`) — a straddling lane owning one product vertical end-to-end; `apps/web/src/pages/Overview.tsx` (**its test is cross-authored to 5E-W4 — see that row; W2 may not test its own code**). **Cross-authored BEHAVIOUR slice — tests W1's export code:** `apps/api/app/tests/test_exports_service_behaviour.py` (the export success path: three render formats, export history, and the atomic export + audit write. **Renamed from `test_exports_preconditions.py`, which invited the reading that a lane other than W4 owned the precondition *negatives*; under §6.1 rule 2 those are W4's and live in `test_exports_refusal_preconditions.py`**). **Authors no test for its own analytics code** | status analytics aggregates over six entity families with `RunStats` null discipline, plus their dashboards | **1.80** |
| **5E-W3** Primary UI | `apps/api/app/notifications/` (new, whole directory); `apps/api/app/briefs/service.py`, `apps/api/app/approvals/service.py`, `apps/api/app/creative/service.py` (notification emission hooks in the triggering transaction, assigned at CF-3 — all three originate in earlier windows, disclosed on both sides. **`briefs/service.py` and `creative/service.py` are also owned by 5D-W3 in D17 AM–D18 AM — two shared files, not three; `approvals/service.py` is this lane's alone. The windows are strictly separated by I-5D, R-5D and an overflow slot, so the two lanes are never concurrent**); `apps/web/src/pages/notifications/` (new, whole directory, **excluding** its `__tests__/`); `apps/web/src/pages/creative/ExportDialog.tsx` and `apps/web/src/pages/creative/ExportHistory.tsx` (**exactly these two new files** inside the directory 5C-W3 claimed whole in D11 PM–D13 AM — different windows, so the assignment is by exact filename at CF-3, never by prefix; no other file in that directory transfers). **Authors no tests** | notification feed, preferences, per-user scoping, emission hooks, export UI, and the notification feed component the freeze-owned app-shell shell renders | **1.67** |
| **5E-W4** Adversarial + UI verification | `apps/api/app/tests/test_notifications_isolation.py` (**the cross-user IDOR negative — owned here, not by W1 or W3**), `test_exports_isolation.py`, `test_exports_jurisdiction_refusal.py`, `test_exports_capability_gate.py`, **`test_exports_refusal_preconditions.py`** (the blocked-review, latest-decision — unapproved / approve-then-reject / revoked / wrong-version — and wrong-role refusals; §2 preconditions 2, 3 and 4, which previously mapped to no W4 file), `test_analytics_isolation.py`, `test_retention_purge_guard.py`, **`test_analytics_scope_boundary.py`** (authorization and attribution-field-absence negatives — adversarial, so it belongs with this lane); **W2's and W3's UI tests:** `apps/web/src/pages/__tests__/overview.test.tsx` (**exactly this filename** in the flat shared directory — pairing W2's `pages/Overview.tsx`, which no lane previously verified), `apps/web/src/pages/creative/__tests__/export-dialog.test.tsx` and `apps/web/src/pages/creative/__tests__/export-history.test.tsx` (**exactly these two**, pairing W3's two new files above — **cross-window disclosure: the `pages/creative/__tests__/` directory is created by 5C-W4 in D11 PM–D13 AM; this milestone adds exactly these two files at CF-3, by exact filename, never by prefix**), `apps/web/src/pages/analytics/__tests__/` (whole directory — W2's claim on `pages/analytics/` explicitly excludes it), and `apps/web/src/pages/notifications/__tests__/` (whole directory — W3's claim on `pages/notifications/` explicitly excludes it) | **the whole adversarial battery** — cross-**user** IDOR, cross-workspace and cross-location isolation, the NULL-jurisdiction export refusal, the capability-gate negative, the purge mechanism's cannot-run guard, the analytics scope-boundary negatives, the three export refusal preconditions — and verification of W2's and W3's surfaces including `Overview.tsx` | **1.90** |

**Longest lane 2.18 (5E-W1) against a 3.0-day window — local float +0.82, of which
0.50 is the operator-designated reachable 5E remediation reserve. Spendable only
inside 5E.** Total need **7.55** lane-days (W1 2.18 + W2 1.80 + W3 1.67 + W4 1.90)
against 12.0 of capacity. W4 rose 1.70 → 1.90 when two previously unowned obligations
were assigned to it — `test_exports_refusal_preconditions.py` (+0.15) and
`overview.test.tsx` (+0.05). Neither changes the pacing lane: W4 at 1.90 sits 0.28
below W1's 2.18, so the window stays 3.0 and the float stays +0.82.

**Intra-window ordering (the one blocking dependency inside this window).** 5E-W1's
`export.completed` emission codes against the notifications service interface, which
**5E-W3 must land early in the window**. The interface itself is frozen at CF-3, so W1
is never blocked on a contract — only on W3's in-window implementation of it. This is
the only intra-window lane-to-lane ordering constraint in the round, and it is
disclosed here because §2 references it. **This is the
operative statement**; the header block restates the window as a **derived** summary
and must be propagated in the same edit.

**Cross-authoring in 5E is a three-lane cycle, not a pairwise swap — disclosed because
it is easy to misread as pairwise.** W1 tests W3's notification code and W2's analytics
code; W2 tests W1's export code; W4 tests W2's analytics negatives and W2's and W3's UI.
**W3 authors no tests at all**, per the master plan §6.1 rule, which is absolute and
admits no 5E exception. The binding property is preserved in every case — **no lane
authors a test for code it wrote in the same window** — but a reviewer checking only for
reciprocal pairs will not find them and should not read their absence as a defect.

### The analytics test slice — measured, assigned, and charged

A prior review found **no lane authored positive-path tests for the analytics code**:
W2 owned `apps/api/app/analytics/`, W3 authored no tests, and the only analytics test
named anywhere was W4's adversarial `test_analytics_isolation.py`. Exit criterion 4
("analytics endpoints return correct aggregates on seeded fixtures") therefore had **no
owner** — it was self-graded by W2 or unverifiable.

Two independent estimates were taken. The measured **unowned increment is 0.52
lane-days** (range 0.30–0.80) — not the full slice, because the lane table already owned
most analytics test work. New artifacts, now assigned above, **with the 0.52 distributed
across them exactly once**:

| Artifact | Purpose | Owner | Charge |
|---|---|---|---|
| `apps/api/app/tests/fixtures/analytics.py` | the six-family, five-milestone seeded fixture | **5E-W1** | 0.25 |
| `apps/api/app/tests/test_analytics_aggregates.py` | positive-path + aggregation correctness | **5E-W1** | 0.17 |
| `apps/api/app/tests/test_analytics_empty_state.py` | null-not-zero discipline, zero-row case | **5E-W1** | 0.05 |
| `apps/api/app/tests/test_analytics_scope_boundary.py` | authorization + attribution-field-absence negatives | **5E-W4** | 0.05 |
| | | **Total** | **0.52** |

**Ownership is constrained before it is weighted, and the constraint was previously
violated.** All four artifacts test **5E-W2's** production code, so under the master
plan §6.1 cross-authored pairing rule they may be owned by **W1 or W4 only** — never
W2, which authored the code, and **never W3, which under §6.1 authors no tests at
all**. An earlier version of this table assigned the fixture and the empty-state file
to **5E-W3**, which broke that rule; the negatives go to W4 because the adversarial
battery stays whole there, and everything else goes to W1. **The 2.02 longest lane that
once appeared here was produced entirely by the rule violation and has no independent
support.**

Applied to the pre-increment lane loads: W1 1.71 → **2.18** (+0.47), W2 1.80 →
**1.80** (unchanged), W3 1.72 → **1.67** (−0.05, having shed the freeze-owned
`notifications.tsx` shell), W4 1.65 → **1.70** (+0.05). **5E-W2 gains nothing**, which
is correct: it owns the analytics production code and therefore authors none of these
files. The fixture carries the largest share because it dominates the slice (below);
the split is weighted by that measurement, not spread evenly.

**A superseded derivation is recorded here because it inflated the window.** An earlier
draft added the full 0.52 to *every* lane (W1 2.23, W2 2.32, W3 2.24, W4 2.17),
charging **2.08** lane-days for a measured 0.52 increment and growing 5E-W2 despite it
owning none of the new artifacts. Those four figures came from the estimator's
*sensitivity* display — what each lane would reach if that single lane absorbed the
whole increment, demonstrating that none had 0.52 of float inside 2.0 (the largest was
W4's 0.35). They were never a simultaneous assignment. The distribution above is the
operative charge.

**A "why 2.0 is infeasible under ANY split" table has been DELETED from this section,
and the reason is recorded because it matters more than the table did.** That table
purported to prove no assignment could fit a 2.0-day window. It did not: it varied the
*prices* of the four artifacts while holding their *assignment* fixed, and the
assignment it held fixed was the rule-violating one that put the fixture on 5E-W3.
Its conclusion therefore presupposed the violation it should have caught. **No
successor may reinstate it or cite it.**

**One of its rows rested on a fabricated precedent, and that is corrected here.** The
row read *"Local float ≥ +0.20 (as 5B/5C/5D carry)"*. **5B carries +0.12** — see the
master plan §5 ledger and `docs/project-phase-5b-approval-audit-spine.md` §5, which
calls it *"the round's tightest window"*. 5C (+0.43) and 5D (+0.30) do exceed 0.20;
**5B does not**, so the parenthetical was false as written and the "+0.20 precedent"
it asserted does not exist. **The plan's accepted minimum local float is +0.12**, and
the only binding condition is the master plan's: no window carries a *negative* local
float. A fabricated precedent offered in support of a schedule conclusion is a more
serious failure than an arithmetic error, which is why it is recorded rather than
quietly removed.

**This fabrication finding is entirely distinct from the O-11 ceiling attribution in
the master plan §12, which was independently verified line by line and is genuine.**
The two must never be merged: one is a confirmed fabrication with a measured
correction, the other is a settled attribution that a reviewer once wrongly called
fabricated. Merging them would either re-open a settled question or dismiss a real
defect.

The measured facts the deleted table misused remain true and are retained: the fixture
is ~0.25 lane-days because it is 52% scaffolding for one entity family across six, and
the measured increment range is **0.30–0.80** with 0.52 as the point estimate. **The
window is 3.0 days by operator decision, not because any table forced it.**

**The +0.82 float is a designated reserve plus identified downside cover, not idle
slack.** 0.50 of it is the operator-designated reachable 5E remediation reserve. The
same estimator
warns these figures are most likely too **low** if 5E-W4's 1.65 base predates analytics
dashboards as a surface, which would add ~0.28 for the web `__tests__/` and push the
required window toward 2.6–2.7. The float is sized to absorb that specific risk.

**The fixture dominates the slice — with a stated caveat on its own premise.** The
52%-scaffolding calibration below is measured against the repository **as it stands
today**, in which there is no `conftest.py` under `apps/api`. **5A-W4 creates one at
D3 PM–D4 PM, fifteen working days before `analytics.py` is authored**, so the
calibration is measured on a pre-5A baseline and applied to a post-5A state that has
exactly the infrastructure whose absence it measures. It therefore **overstates** the
fixture, and the 0.25 charge is conservative in the schedule's favour. There is no
`conftest.py` anywhere under
`apps/api`, so every test module is self-contained; in the nearest analogue
(`test_scout_run_history_api.py`) lines 1–223 are scaffolding against 224–428
assertions — **52% scaffolding for ONE entity family**, and analytics spans six.
The fixture is therefore assigned as a distinct owned artifact rather than duplicated
across test files.

**Lane-authored positive-path coverage is mandatory**: integration-owner or reviewer
tests do not substitute. W2, which owns the analytics production code, authors **no**
test for it.

**Window sizing — one operative statement, one derived summary.** This document
previously carried this paragraph **three** times with conflicting figures (two of them
asserting a superseded "longest lane 1.80 against a 2.0-day window"). That duplication,
not the arithmetic, is what let stale numbers survive successive corrections: a fix
applied to one copy left the others standing.

The **operative** statement is the one above the analytics subsection — **longest lane
2.18 (5E-W1) against a 3.0-day window, local float +0.82**. The header block at the top of this
document restates the window as a summary and is **derived** from it, not independent of
it; any change to the operative statement must be propagated there in the same edit.
A previous version of this paragraph claimed the figure was "not restated anywhere else
in this document", which was false — the header restates it, and asserting uniqueness
that the document does not have is exactly the kind of unchecked claim this round exists
to remove.

Supporting facts, none of which is an independent sizing statement: per-lane figures are indicative
within ±0.1 — **which is why no half-day window decision in this document rests on a
difference smaller than that tolerance; the 3.0-day window is an operator decision, and
the 2.18 longest lane clears 2.0 by 0.18, comfortably outside ±0.1**; utilisation across
W1–W4 is 73/60/56/63% of the 3.0-day window (2.18/1.80/1.67/1.90 ÷ 3.0, re-derived from the §5 lane table), the
slack being the disclosed downside cover above. 5E is the largest milestone in the round
on both total work and longest lane — not the smallest, as an earlier ordering implied —
and it was measured short under *both* estimators against the original 1.5-day window
(longest lane 1.66 and 2.22). 5E-W2 straddles the stack deliberately; with pure-stack
lanes 5E measures 2.39 before the increment and the round does not fit (master plan §6).
5E's UI surfaces' `types.ts`, `endpoints.ts` and `queryKeys.ts` entries — and, **for
`pages/analytics/` only, a `nav.ts` entry and an `App.tsx` route** — are authored into
the **CF-3 payload** by the integration owner. (`pages/notifications/` correctly gets
neither: it is not a routed page, it is rendered by the freeze-owned app-shell, whose
`notifications.tsx` shell replacement is itself in the CF-3 payload. An earlier version
of this sentence asserted a route and nav entry for *all* 5E surfaces, which
over-claimed.) Those files are authored by the integration owner
— those files are freeze-owned, never lane-owned (master plan §6.3) — so no lane is
blocked on files another lane owns. **This explicitly includes `pages/analytics/`,
which needs a named route and a named nav entry**: master plan §6.2 records that a
route without a `nav.ts` entry is unreachable and its breadcrumb silently degrades.
**It also includes `apps/web/src/components/layout/notifications.tsx`, which is
freeze-owned** — it is app-shell chrome consumed at module scope by
`app-shell.tsx:9`, rendered at `:78`, and mounted on every authenticated route via
`App.tsx:25`, so no builder lane may own it. The 0.05-day shell replacement is charged
to the integration owner in the CF-3 payload; the substantive feed is 5E-W3's
lane-owned `apps/web/src/pages/notifications/`.

(The `deleted_at` soft-delete columns on earlier milestones' tables land in the
consolidated CF-3 migration payload authored by the integration owner —
post-integration contract files are freeze-owned, master plan §6.)

Freeze-owned files per master plan §6 apply unchanged in this window.

## 6. Out of scope

Email/SMS/webhook delivery; signed-URL or file-store export; scheduled or
automated export; ad-platform anything; attribution analytics; retention **policy**
values; activation of any flag.

## 7. Rollback

Additive-only; revert PR(s); migrations downgrade; flags never enabled; the purge
purge mechanism cannot have run — no policy exists and no `JobType` member exists
under which it could be dispatched — so no data-loss surface.
