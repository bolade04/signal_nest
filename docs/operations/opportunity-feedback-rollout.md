# Opportunity Feedback Rollout Runbook (Phase 3C — 3C-D)

Operating the **opportunity-feedback** subsystem: enabling it, rolling it back,
and troubleshooting. Feedback is **dark-deployed** — both the API and the UI ship
disabled behind the `opportunity_feedback_enabled` flag (default `False`), so no
customer can read or submit feedback until an operator explicitly turns it on.

Related runbooks: scheduling in [scout-scheduling-runbook.md](./scout-scheduling-runbook.md);
telemetry in [observability.md](./observability.md); dashboards and alerts in
[dashboards.md](./dashboards.md) and [alerts.md](./alerts.md); migrations in
[migrations.md](./migrations.md).

> There is **no separate frontend flag, build-time toggle, or client bundle
> variant**. The UI is *server-gated*: the feedback history request doubles as the
> capability probe. While the flag is off the API answers `503
> capability_unavailable` and the panel renders **nothing**. Enabling the feature
> is therefore a single backend flag flip — the shipped client needs no rebuild.

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

- **Dark by default.** With the flag off, both feedback endpoints answer `503
  capability_unavailable`. The client treats that 503 (and a `403`) as "hide the
  panel entirely" — no partial UI is ever shown for a capability the user cannot
  use. Turning the flag off is a safe global kill-switch.
- **Append-only and immutable.** Every submission is a new event. There is no
  edit, delete, or replace path in the API or the UI; a prior verdict is never
  overwritten. Operator-facing copy is deliberately **"Feedback recorded,"** never
  "Rating updated."
- **Role-gated.** Only editors (`owner` / `admin` / `marketer`) can read or submit;
  the API `403`s a viewer, and the UI hides the control from a viewer before any
  request is made. The two gates are independent — the UI hide is a courtesy, the
  API gate is authoritative.
- **Record-scoped, no cross-market leakage.** Each panel is keyed by its own
  `intelligence_record_id`; the React Query key and the submit mutation both
  capture that scope, so one market's history or submission can never bleed into
  another's. Verified by the four-market isolation tests
  (Dallas/London/Lagos/Nairobi).
- **No scoring influence.** Capturing feedback changes no score, version, or
  ranking. Enabling the feature cannot alter any customer-visible opportunity
  ordering.

## Enabling feedback (rollout)

Enabling is a single flag flip on the API. **Prerequisite:** the intelligence
read response exposes `intelligence_record_id` (shipped in 3C-C.1) — the UI needs
it to bind and submit feedback.

1. **Announce** a change window; enabling makes the feedback control appear for
   editors and is tenant-visible.
2. **Set the flag** in the API environment and restart so the `get_settings()`
   cache is rebuilt:

   ```bash
   OPPORTUNITY_FEEDBACK_ENABLED=true
   ```

   Only the API carries this flag — feedback has **no worker path**, so no worker
   restart is required.
3. **The shipped client needs no change.** On the next intelligence-panel render,
   the feedback history request returns `200` instead of `503`, and the panel
   reveals itself for editors. No rebuild, redeploy, or per-tenant client toggle
   is involved.
4. **First enablement is a single non-production internal workspace** (§15 of the
   phase-3C plan). Do **not** enable for a customer cohort without explicit
   approval.
5. **Verify** (see Monitoring below): an editor sees the Useful / Not useful
   controls and an (initially empty) history; a submission returns `201` and
   appears as a new immutable entry; a viewer sees nothing; markets stay isolated.

## Rolling back (kill-switch)

Rollback is the reverse flip and is always safe:

1. **Set `OPPORTUNITY_FEEDBACK_ENABLED=false`** on the API and restart.
2. **The UI self-hides.** The next feedback history request returns `503`, and the
   panel renders nothing on the next render — no client action is required.
3. **No data is destroyed.** Existing feedback rows are retained; the endpoints
   simply refuse reads and writes with `503`. Re-enabling later restores the panel
   with full history intact.

There is **no per-tenant toggle**; the flag is global. A per-tenant stop is not
supported in this slice — use the global kill-switch.

## Operator / customer controls

All feedback endpoints require an **editor** role (`owner` / `admin` /
`marketer`) and are **feature-gated** — while the flag is off they answer `503
capability_unavailable`. There is no read-open variant: unlike scheduling, the
feedback **history read is also gated**, because the read doubles as the UI
capability probe.

| Action | Endpoint | Notes |
| --- | --- | --- |
| Read history | `GET  …/opportunities/{id}/feedback` | Editor-gated + feature-gated. `limit` (1–100, default 20) / `offset` (≥0). Doubles as the UI probe. |
| Submit | `POST …/opportunities/{id}/feedback` | Editor-gated + feature-gated. Append-only, `201`. Body: `intelligence_record_id`, `is_useful`, optional `reason_code`. |

Source: `apps/api/app/feedback/routes.py`.

## Monitoring & audit

- **Structured log events** (planned in the backend track, see
  [observability.md](./observability.md)): `opportunity_feedback_submitted`,
  `opportunity_feedback_authorization_rejected`,
  `opportunity_feedback_invalid_reason`,
  `opportunity_feedback_feature_disabled_attempt`. Every event carries the acting
  user and scoped resource IDs (organization, workspace, opportunity, intelligence
  record). **Raw notes are never logged** — there is no free-text field to log.
- **Audit trail:** each submission is itself the durable, append-only record of
  who gave what verdict against which intelligence record; there is no separate
  mutation to audit because feedback is immutable.

### Health checks

| Symptom | Likely cause | Action |
| --- | --- | --- |
| Panel invisible for an editor after enabling | Flag not set on the API, or `get_settings()` cache not rebuilt | Confirm `OPPORTUNITY_FEEDBACK_ENABLED=true`; restart the API. |
| Panel invisible for one user only | That user is a viewer (not an editor) | Expected: viewers are gated out by design. |
| Panel shows a "Try again" error state | A non-gate failure (e.g. `429`/`5xx`) on the history read | Inspect the failing `GET …/feedback`; the 503/403 gates hide, other errors surface a retry. |
| Feedback from one market appears in another | Would indicate a scope-key defect | Cannot occur under the record-scoped query key + isolation tests; investigate immediately if observed. |
| Submission rejected `503` after enabling | Flag off on the serving API instance | Confirm every API instance carries the flag. |

## Abort conditions

Stop the rollout and flip the kill-switch if any of the following is observed:

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
   open scheduling read. This is deliberate: the read is the UI's capability probe,
   so gating it is what hides the panel with no client flag.
2. **The flag is global, not per-tenant.** First enablement is scoped operationally
   (a single internal workspace), not by a per-tenant flag.
3. **Feedback is append-only with no "current" projection in the UI.** The history
   list shows every event; there is intentionally no edit/delete affordance.
4. **Stale-context protection is structural**, via `key={intelligence_record_id}`
   remount plus a record-scoped query key and mutation scope — not a runtime guard.
