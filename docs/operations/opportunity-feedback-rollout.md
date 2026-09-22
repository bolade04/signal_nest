# Opportunity Feedback Rollout Runbook (Phase 3C — 3C-D)

Operating the **opportunity-feedback** subsystem: enabling it, rolling it back,
and troubleshooting. Feedback is **dark-deployed** — both the API and the UI ship
disabled by default (global flag `opportunity_feedback_enabled` `False`, no
workspace override), so no customer can read or submit feedback until an operator
explicitly turns it on. The UI is not gated on that flag directly: it follows the
same workspace-effective capability decision the API enforces, which coincides
with the flag only while no override exists.

Related runbooks: scheduling in [scout-scheduling-runbook.md](./scout-scheduling-runbook.md);
telemetry in [observability.md](./observability.md); dashboards and alerts in
[dashboards.md](./dashboards.md) and [alerts.md](./alerts.md); migrations in
[migrations.md](./migrations.md).

> **Activation model update (Phase 4B).** First enablement is **no longer a global flag
> flip**. As of Phase 4B-A the feedback route consults the deny-biased capability resolver,
> and the sanctioned way to enable feedback for the internal canary is a **single
> per-workspace enable override** created through the **operator API**, while
> `opportunity_feedback_enabled` stays **`False` globally**. The controlled procedure is
> defined in [../phase-4b-b-plan.md](../phase-4b-b-plan.md) (with the parent plan
> [../phase-4b-plan.md](../phase-4b-plan.md)). The "single flag flip" descriptions retained
> below document the **historical global mechanism and the standing global kill-switch**
> (which retracts only global enablement — an honored per-workspace override outranks it);
> for the canary, follow the governed override procedure instead. There is **no org-wide
> override** — override scope is per-workspace only. `scout_scheduling` and `connector_rss`
> remain entirely dark and out of scope.

> There is **no separate frontend flag, build-time toggle, or client bundle
> variant**, and there is **no customer-settable toggle**. The single
> `opportunity_feedback_enabled` backend flag drives global enablement, and a
> governed per-workspace override can enable it for one workspace without it. The
> UI reads a **read-only, workspace-effective capability reflection** —
> `GET /workspaces/{workspace_id}/feedback-capability`, resolver-backed, the same
> decision the backend gate enforces — and consults it **before** issuing any
> feedback request. (`GET /system/capabilities` remains raw-global and no longer
> gates this panel.) While the effective decision is disabled, unknown, or still
> resolving, the panel issues **zero** feedback requests (no GET probe, no POST)
> and renders **nothing**. The feedback endpoints additionally
> answer `503 capability_unavailable`, retained purely as **defence-in-depth** for a
> stale client. Enabling — by either route — therefore requires no client rebuild;
> the panel appears on the next mount that reads the reflection. Staleness permits a refetch, it does not trigger one, so a client
> already on the page does not pick the change up on a timer.

## What the subsystem is

**Opportunity feedback** is an append-only, human-in-the-loop signal on a single
immutable **intelligence record**. When live, an authorized editor can:

1. Read the feedback history for an opportunity
   (`GET …/opportunities/{id}/feedback`), scoped in the client to the specific
   `intelligence_record_id` on screen.
2. Submit a binary verdict (**useful** / **not useful**) with an optional
   structured reason from a **closed vocabulary** (no free text). Each submission
   is a new immutable event (`POST …/opportunities/{id}/feedback`, `201`).

Feedback is **capture-only**: it never changes opportunity scoring, source
credibility, ranking, or any worker/scheduling/connector behavior.

Source (backend, already merged): `apps/api/app/feedback/routes.py`,
`apps/api/app/core/enums.py` (reason taxonomy). Source (frontend, 3C-D):
`apps/web/src/pages/opportunities/FeedbackPanel.tsx`,
`apps/web/src/pages/opportunities/useFeedback.ts`.

### Invariants the operator can rely on

- **Dark by default.** While the capability resolves **disabled for a workspace**
  — the shipped default, with the global flag off and no override — the
  reflection reports it disabled, so the UI issues **zero** feedback requests and
  shows nothing.
  Both feedback endpoints also answer `503 capability_unavailable` as
  defence-in-depth; the client treats that 503 (and a `403`) as "hide the panel
  entirely." No partial UI is ever shown for a capability the user cannot use.
  **The global flag is not an unconditional kill-switch.** Resolver precedence is
  safety ceiling → honored workspace override → global configuration → secure
  default, so an honored enable override **outranks** a `false` global flag.
  Setting the flag `false` prevents feedback only for workspaces whose effective
  state is not being raised by such an override. For a workspace enabled through
  a workspace override — which is the documented canary path — the operative
  rollback is **clearing or disabling that scoped override**.
- **Append-only and immutable.** Every submission is a new event. There is no
  edit, delete, or replace path in the API or the UI; a prior verdict is never
  overwritten. Operator-facing copy is deliberately **"Feedback recorded,"** never
  "Rating updated."
- **Role-gated.** Only editors (`owner` / `admin` / `marketer`) can read or submit;
  the API `403`s a viewer, and the UI hides the control from a viewer before any
  request is made (the role gate precedes even the capability query). The two gates
  are independent — the UI hide is a courtesy, the API gate is authoritative.
- **Record-scoped, no cross-market leakage.** Each panel is keyed by its own
  `intelligence_record_id`; the React Query key and the submit mutation both
  capture that scope, so one market's history or submission can never bleed into
  another's. Verified by the four-market isolation tests
  (Dallas/London/Lagos/Nairobi) and direct stale-context tests (record rebind while
  the dialog is open, submission pending across a switch, slow response after a
  switch, unmount while pending).
- **No scoring influence.** Capturing feedback changes no score, version, or
  ranking. Enabling the feature cannot alter any customer-visible opportunity
  ordering.

## Enabling feedback (rollout)

**Current canary path (Phase 4B — governed per-workspace override).** First
enablement is a **single per-workspace enable override**, created through the
operator API, with `opportunity_feedback_enabled` left **`False` globally**. Follow
[../phase-4b-b-plan.md](../phase-4b-b-plan.md) — it requires independent runtime
identity verification, a preflight, isolation checks, and a rollback exercise, and it
authorizes activation only under a separate explicit operational approval.

- Use the operator **set** plane
  `PUT /internal/system/capabilities/overrides` (operator-gated; 401/403 enforced)
  with `capability: opportunity_feedback`, `enabled: true`, and the verified
  `organization_id`/`workspace_id`. **Do not mutate override state directly in the
  database** — the operator API is the only sanctioned mutation path.
- The resolver honors the enable for that one workspace
  (`decided_by=workspace_override`) while the global flag stays `False`. There is
  **no org-wide override**; the row's `organization_id` only tenant-validates the
  workspace.
- **The canary is tenant-visible. Plan for exposure, not for invisibility.**
  Since Phase 6U-1H (`P6-UI-005`) the customer feedback panel no longer decides
  from `GET /system/capabilities`. It reads a dedicated **workspace-effective**
  reflection, `GET /workspaces/{workspace_id}/feedback-capability`, computed
  through the same resolver semantics as the backend feedback gate. An honored
  `opportunity_feedback=true` override therefore makes the feedback UI appear for
  **every eligible editor** (owner / admin / marketer) in that workspace, while
  the raw global flag stays `False`.
  - Setting it from the **Operations** screen invalidates the customer-effective
    cache **in the operator's own browser session only** — that makes the
    operator's own verification view update at once. It does **not** reach other
    users: there is no cross-session invalidation mechanism (no websocket, SSE or
    shared cache). For every other user the change lands on their next mount.
  - Set it by any other route and a client **already on the page keeps its cached
    answer indefinitely**. Staleness is permission to refetch, not a trigger: the
    app disables refetch-on-focus and sets no refetch interval, so a stale entry
    refreshes only on a new observer mount, a reconnect, or an explicit
    invalidate. Expect the change to land on next navigation or reload, not after
    the 60 s staleness window.
  - **This matters most when REVOKING, and the route you revoke by does not help.**
    Clearing an override does not retract the affordance from a client sitting on
    an opportunity-detail page, whichever route you use — the Operations screen
    refreshes only your own view. The panel persists until it remounts, and a
    click then fails with the backend 503. **Assume exposure continues until
    affected users navigate, reload or reconnect.** Be precise about those three:
    a full page reload always refetches; navigation and reconnect refetch only
    once the 60 s `staleTime` has elapsed, because React Query's refetch-on-mount
    and refetch-on-reconnect both additionally require the entry to be stale. So
    the floor on exposure is a remount, not a timer — and for a client that never
    remounts, there is no upper bound at all.
  - `GET /system/capabilities` still reports the **raw global flag** and still
    reads `false` during the canary. It is no longer the authoritative customer
    reflection, so **do not use it to conclude the canary is hidden.**
  - Verify the canary either via the API directly or by confirming an editor in
    the canary workspace now sees the panel.
  - Backend authorization and resolver enforcement remain authoritative, and the
    override activates **no global flag**.

**Prerequisite:** the intelligence read response exposes `intelligence_record_id`
(shipped in 3C-C.1) — the UI needs it to bind and submit feedback.

**Historical global mechanism (retained for reference; not the canary path).** Global
enablement was a single flag flip:

1. **Announce** a change window; enabling makes the feedback control appear for
   editors and is tenant-visible. **This applies equally to a per-workspace canary
   override since 6U-1H** — announce before enabling a canary, not only before a
   global flip.
2. **Set the flag** in the API environment and restart so the `get_settings()`
   cache is rebuilt:

   ```bash
   OPPORTUNITY_FEEDBACK_ENABLED=true
   ```

   Only the API carries this flag — feedback has **no worker path**, so no worker
   restart is required. **For the Phase 4B canary this flag stays `False`** — do not
   set it; use the per-workspace override above.
3. **The shipped client needs no change.** Once the client's cached effective
   reflection is re-read on its next observer mount, the history query becomes active and returns
   `200`, and the panel reveals itself for editors. No rebuild, redeploy, or
   per-tenant client toggle is involved. This applies to the **override** path as
   well as the global flip: under a canary override the workspace-effective
   reflection reports **enabled** for that workspace (the raw global reflection on
   `/system/capabilities` stays `false`, and is not what the panel reads).
4. **First enablement is a single internal workspace.** Under Phase 4B this is
   enforced by the scoped override, not a global flip. Do **not** enable for a
   customer cohort without explicit approval.
5. **Verify** (see Monitoring below): an editor sees the Useful / Not useful
   controls and an (initially empty) history; a submission returns `201` and
   appears as a new immutable entry; a viewer sees nothing; markets stay isolated.

## Rolling back (kill-switch)

**Primary canary rollback (Phase 4B — scoped).** Clear the one canary workspace
override through the operator **clear** plane
`DELETE /internal/system/capabilities/overrides?...&capability=opportunity_feedback`
(operator-gated; not a direct DB delete). The target immediately returns to the dark
default (`503`, `decided_by=global_configuration`); no other capability or workspace
changes; the clear emits a `workspace_capability_override.cleared` audit. Because the
global flag was never flipped, setting it to `False` is **not** the canary rollback —
clearing the scoped override is. **After clearing, exposure does not end immediately:**
a client already on an opportunity-detail page keeps the panel until it remounts
(staleness permits a refetch, it does not trigger one), and clearing from the
Operations screen refreshes only the operator's own session. Assume eligible editors
retain the affordance until they navigate, reload or reconnect; their clicks then fail with the
backend 503. Full sequence in
[../phase-4b-b-plan.md](../phase-4b-b-plan.md) §12.

**Global kill-switch (still available; layered safety).** The reverse global flip
remains a standing control and is always safe to perform — but it is **not
unconditional**: it retracts only what the global flag was granting, and an
honored workspace enable override outranks it. For an override-enabled workspace
this step changes nothing; clear the override instead.

1. **Set `OPPORTUNITY_FEEDBACK_ENABLED=false`** on the API and restart.
2. **New panel mounts stop showing the feature — already-open pages do not, and an
   override-enabled workspace is unaffected entirely.** This step retracts only
   what the *global* flag was granting; a workspace carrying an honored enable
   override keeps feedback enabled regardless (clear the override instead). For
   workspaces without an override, the reflection reports disabled from the next
   observer mount onward. A client
   **already on an opportunity-detail page keeps the panel visible**: staleness
   permits a refetch, it does not trigger one (`refetchOnWindowFocus` is off and
   no refetch interval is set on this query). Requests from such a client are
   refused with `503` (defence-in-depth), so a click fails rather than writing —
   but the affordance itself persists until those users navigate, reload or reconnect.
   **Do not treat the kill-switch as having retracted the UI for active users.**
3. **No data is destroyed.** Existing feedback rows are retained; the endpoints
   simply refuse reads and writes with `503`. Re-enabling later restores the panel
   with full history intact.

Additional layers: the resolver's deny-biased precedence stays intact, but its
`safety_ceiling` slot is **reserved and not yet operable** for a registered
capability — `_ceiling_blocks` fires only for a capability *absent* from
`CAPABILITY_REGISTRY`, and all three are registered. It is not a lever you can
pull today; do not plan a rollback around it. A defective gate integration can be
reverted at the code level (Phase 4B-A commit).

Under Phase 4B a **per-workspace stop is supported** via the scoped override clear
above; there is still **no org-wide toggle** — override scope is per-workspace only.

## Operator / customer controls

All feedback endpoints require an **editor** role (`owner` / `admin` /
`marketer`) and are **feature-gated** — while the capability resolves **disabled
for that workspace** they answer `503 capability_unavailable`. That is the dark
default; an honored workspace enable override changes it for that workspace even
while the global flag stays `False`. There is no read-open variant: unlike scheduling, the
feedback **history read is also gated**. The UI, however, does **not** probe this
gate: it reads the capability reflection first and only issues the history read
once the feature is enabled. The endpoint `503` is defence-in-depth for a stale
client.

| Action | Endpoint | Notes |
| --- | --- | --- |
| Read history | `GET  …/opportunities/{id}/feedback` | Editor-gated + feature-gated. `limit` (1–100, default 20) / `offset` (≥0). Not issued by the UI while the capability reflects disabled. |
| Submit | `POST …/opportunities/{id}/feedback` | Editor-gated + feature-gated. Append-only, `201`. Body: `intelligence_record_id`, `is_useful`, optional `reason_code`. |

Source: `apps/api/app/feedback/routes.py`.

## Monitoring & audit

- **Structured log events** — these are the names the backend actually emits, with
  the fields each one actually carries. Earlier drafts of this runbook listed four
  planned names that were never used, and claimed a uniform field set that no event
  has. (`observability.md` does not yet document this subsystem — it contains no
  `opportunity_feedback` content — so treat the list below as the source of truth.)
  - `opportunity_feedback_created` (`app/feedback/service.py`) — a submission was
    durably recorded.
  - `opportunity_feedback_gate_decided` (`app/feedback/routes.py`) — the capability
    gate admitted or refused an opportunity-nested call.
  - `opportunity_feedback_gate_failed` (`app/feedback/routes.py`) — the resolver
    raised and the request failed closed with a 5xx. **Alert on this.**
  - `opportunity_feedback_capability_reflected` (`app/feedback/routes.py`) — a client
    read the workspace-effective decision. **This is the canary signal:** it is the
    only event showing customer-side reflection traffic, so use it to confirm a
    canary workspace is actually being polled.

  Fields, as emitted — they are **not** uniform, and **no event carries an acting
  user id**, nor is one injected ambiently (the shared log context carries only
  `request_id`, `trace_id`, `job_correlation_id`, `component`, `worker_type`,
  `operation`):

  | Event | Fields |
  | --- | --- |
  | `_created` | `workspace_id`, `opportunity_id`, `intelligence_record_id`, `is_useful`, `reason_code`, `outcome` — **no `organization_id`** |
  | `_gate_decided` | `organization_id`, `workspace_id`, `capability`, `effective_enabled`, `decided_by`, `global_flag`, `has_override`, `outcome` |
  | `_gate_failed` | `organization_id`, `workspace_id`, `capability`, `outcome` |
  | `_capability_reflected` | `organization_id`, `workspace_id`, `capability`, `effective_enabled`, `outcome` |

  **Consequence for triage: you cannot narrow any of these to a single user.** The
  finest grain available is `workspace_id`. **Raw notes are never logged** — there is
  no free-text field to log.
- **Audit trail:** each submission is itself the durable, append-only record of
  who gave what verdict against which intelligence record; there is no separate
  mutation to audit because feedback is immutable.

### Health checks

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Panel invisible for an editor after enabling | On the canary path: no honored override for this workspace. On the global path: flag not set on the API, or `get_settings()` cache not rebuilt. Either path: the client has not remounted the panel since the change (staleness does not trigger a refetch) | Read the effective state first (see the row below), then confirm the setting server-side, then have the user navigate or reload |
| Panel invisible for one user only | Three causes present identically: that user is a viewer (not an editor); that user holds no membership in the resolved organization; or that user's capability reflection is failing — a 5xx hides the panel silently and per-user | Rule the reflection out first: look for `opportunity_feedback_gate_failed` for that `workspace_id` — it is emitted by the reflection route as well as the enforcement gate — and for `opportunity_feedback_capability_reflected` on that `workspace_id`. **Neither can be narrowed to one user: no event carries a user id.** So the log tells you whether reflection is failing for the workspace, not for the person; to separate the two role causes you must read that user's row in the `organization_members` table directly, in the database. **No API or UI surface exposes another user's role:** `GET /api/v1/organizations` and `/auth/me` are self-only, the Settings screen renders the caller's own memberships, and none of the `/internal/system/*` operator routes returns membership. If you have no database access, this distinction cannot be made from the running system. If reflection is healthy for the workspace, viewers and non-members are gated out by design. |
| Panel shows a "Try again" error state | A non-gate failure (e.g. `429`) on the history read | Inspect the failing `GET …/feedback`; the 503/403 gates hide, other errors surface a retry. |
| Feedback from one market appears in another | Would indicate a scope-key defect | Cannot occur under the record-scoped query key + isolation tests; investigate immediately if observed. |
| Submission rejected `503` after enabling | On the canary path: no honored override for this workspace, or one scoped to the wrong workspace/organization. On the global path: flag not set on the serving instance. | Read `GET /internal/system/capabilities/effective?organization_id=…&workspace_id=…&capability=opportunity_feedback` and check `effective_enabled` and `decided_by` — that single call distinguishes `workspace_override` from `global_configuration` and tells you which lever is actually in play. Only then check per-instance settings. |

## Abort conditions

Stop the rollout if any of the following is observed. **Use the rollback that
matches how the workspace was enabled:** for a canary, clear or disable the
workspace override (the global flip does not retract an honored override); for a
globally enabled deployment, set `OPPORTUNITY_FEEDBACK_ENABLED=false`. When in
doubt, do both, and assume already-open pages keep the affordance until they
remount.

- A submission mutates any opportunity score, version, or ranking (there must be
  **no scoring influence**).
- Feedback history or a submission crosses market/record boundaries.
- A viewer can read or submit feedback (role gate breach).
- Any free-text content reaches persistence or logs (the vocabulary is closed by
  design).

## Retained design observations

Accepted as intentional for this slice (documented for operators; full rationale
in [../verification/3c-d-feedback-ui-rollout-readiness.md](../verification/3c-d-feedback-ui-rollout-readiness.md)):

1. **The feedback history read is feature-gated (`503`) while dark**, unlike the
   open scheduling read. The UI does not rely on that `503` to hide: it reads the
   workspace-effective feedback reflection and issues no feedback request while
   dark, unknown, or unresolved. The endpoint `503` is retained as defence-in-depth.
2. **The global flag is global, not per-tenant — but Phase 4B adds a governed
   per-workspace override.** First enablement is now a single per-workspace enable
   override via the operator API while the global flag stays `False` (see the
   Activation model update note above and [../phase-4b-b-plan.md](../phase-4b-b-plan.md));
   override scope is per-workspace only, with **no** org-wide override. There is still
   no customer-settable toggle. **Since 6U-1H the customer UI reflects the
   workspace-effective decision**, not the raw global flag, so an override-enabled
   canary is honored by the backend gate **and is visible to eligible editors of
   that workspace**. The earlier "backend-first / UI stays dark" posture no longer
   holds and must not be relied on when scoping a canary.
3. **Feedback is append-only with no "current" projection in the UI.** The history
   list shows every event; there is intentionally no edit/delete affordance.
4. **Stale-context protection is structural and directly tested**, via
   `key={intelligence_record_id}` remount plus a record-scoped query key and
   mutation scope — exercised by direct tests for record rebind while the dialog is
   open, a submission pending across a switch, a slow response after a switch, and
   unmount while pending.
