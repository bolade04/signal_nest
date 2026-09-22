import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { OpportunityFeedbackPanel } from '@/pages/opportunities/FeedbackPanel';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const WS = 'ws-1';
const OPP = 'opp-loc-dallas-0';
const REC = 'rec-opp-loc-dallas-0';
const P = (path: string) => `*${API_PREFIX}${path}`;
const FEEDBACK = P(`/workspaces/${WS}/opportunities/${OPP}/feedback`);
const CAPABILITIES = P('/system/capabilities');

// Flip the authoritative WORKSPACE-EFFECTIVE feedback reflection the UI reads
// *before* issuing any feedback request. Default handlers report it dark.
// (Not the raw-global runtime summary — that is the 3C-D name and no longer
// what gates this panel; see the note below.)
function enableCapability(enabled = true, globalFlag = false) {
  // 6U-1H: the panel's authoritative gate is the WORKSPACE-EFFECTIVE reflection,
  // not the raw-global runtime summary. The summary is still stubbed alongside it
  // so these tests keep exercising the real app shell, but it no longer decides
  // the panel.
  //
  // The two are DELIBERATELY DECOUPLED, and `globalFlag` defaults to `false`.
  // Driving both from one parameter would force `global_flag === effective`, and
  // the fixture could then never represent the configuration this whole tranche
  // exists for: an honored workspace override with the global flag off. Worse, it
  // would be blind to the very regression it should catch — a panel that went back
  // to reading `features.opportunity_feedback_enabled` would pass every row here.
  // With the default below, every row runs in the override-enabled/flag-false
  // configuration, which is both the canary shape and the one that kills that revert.
  //
  // All three booleans are supplied because `FeatureFlagsOut` requires all three;
  // omitting two asserted a response shape the server cannot return.
  server.use(
    http.get(P('/workspaces/:workspaceId/feedback-capability'), () =>
      HttpResponse.json({ enabled }),
    ),
    http.get(CAPABILITIES, () =>
      HttpResponse.json({
        app_mode: 'local',
        environment: 'development',
        is_local_mode: true,
        all_configured: true,
        features: {
          opportunity_feedback_enabled: globalFlag,
          scout_scheduling_enabled: false,
          connector_rss_enabled: false,
        },
      }),
    ),
  );
}

function feedbackRow(overrides: Record<string, unknown> = {}) {
  return {
    id: 'fb-1',
    opportunity_id: OPP,
    intelligence_record_id: REC,
    is_useful: true,
    reason_code: null,
    submitted_by_user_id: 'user-1',
    analysis_version: '3b',
    scoring_version: '3b.1',
    created_at: '2026-07-17T09:00:00Z',
    ...overrides,
  };
}

// Enable the (otherwise dark) feature: GET returns a page, POST accepts and, if
// `track` is provided, records the request body for assertions.
function enableFeedback(
  items: Array<Record<string, unknown>> = [],
  onPost?: (body: Record<string, unknown>) => void,
) {
  // Enabling the capability reflection is a precondition: without it the panel
  // never mounts the history query, mirroring production behavior.
  enableCapability(true);
  server.use(
    http.get(FEEDBACK, () =>
      HttpResponse.json({ items, total: items.length, limit: 20, offset: 0 }),
    ),
    http.post(FEEDBACK, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      onPost?.(body);
      return HttpResponse.json(
        feedbackRow({
          is_useful: body.is_useful,
          reason_code: body.reason_code ?? null,
        }),
        { status: 201 },
      );
    }),
  );
}

function render() {
  return renderApp(
    <OpportunityFeedbackPanel workspaceId={WS} opportunityId={OPP} intelligenceRecordId={REC} />,
    { route: `/opportunities/${OPP}` },
  );
}

describe('OpportunityFeedbackPanel', () => {
  it('shows the controls and an empty history when enabled for an editor', async () => {
    enableFeedback();
    const screen = render();
    expect(await screen.findByRole('button', { name: /^useful$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /not useful/i })).toBeInTheDocument();
    expect(screen.getByText(/no feedback recorded yet/i)).toBeInTheDocument();
  });

  it('submits a useful verdict with no reason and records it as a new entry', async () => {
    let posted: Record<string, unknown> | null = null;
    let created = false;
    enableCapability(true);
    server.use(
      http.get(FEEDBACK, () =>
        HttpResponse.json(
          created
            ? { items: [feedbackRow()], total: 1, limit: 20, offset: 0 }
            : { items: [], total: 0, limit: 20, offset: 0 },
        ),
      ),
      http.post(FEEDBACK, async ({ request }) => {
        posted = (await request.json()) as Record<string, unknown>;
        created = true;
        return HttpResponse.json(feedbackRow(), { status: 201 });
      }),
    );
    const screen = render();

    await screen.user.click(await screen.findByRole('button', { name: /^useful$/i }));
    // The dialog opens; submit without picking a reason.
    await screen.user.click(await screen.findByRole('button', { name: /submit feedback/i }));

    expect(await screen.findByText('Feedback recorded')).toBeInTheDocument();
    expect(posted).toEqual({ intelligence_record_id: REC, is_useful: true });
    // Append-only: the new immutable entry appears in the history.
    const list = await screen.findByRole('list', { name: /feedback history/i });
    expect(within(list).getByText('Useful')).toBeInTheDocument();
  });

  it('disables submit while pending so a double-click sends exactly one POST', async () => {
    enableCapability(true);
    let postCount = 0;
    let resolvePost!: () => void;
    const gate = new Promise<void>((r) => {
      resolvePost = r;
    });
    server.use(
      http.get(FEEDBACK, () => HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 })),
      http.post(FEEDBACK, async () => {
        postCount += 1;
        // Hold the request open so both clicks land while it is still pending.
        await gate;
        return HttpResponse.json(feedbackRow(), { status: 201 });
      }),
    );
    const screen = render();

    await screen.user.click(await screen.findByRole('button', { name: /^useful$/i }));
    const submitBtn = await screen.findByRole('button', { name: /submit feedback/i });
    await screen.user.click(submitBtn);
    // While pending the button is disabled and relabelled; a second click is a
    // no-op and cannot enqueue a second append-only write.
    expect(await screen.findByRole('button', { name: /recording/i })).toBeDisabled();
    await screen.user.click(screen.getByRole('button', { name: /recording/i }));

    resolvePost();
    expect(await screen.findByText('Feedback recorded')).toBeInTheDocument();
    expect(postCount).toBe(1);
  });

  it('offers only polarity-correct reasons for each verdict', async () => {
    enableFeedback();
    const screen = render();

    // Positive verdict → positive reasons only.
    await screen.user.click(await screen.findByRole('button', { name: /^useful$/i }));
    expect(await screen.findByRole('button', { name: /useful insight/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /strong evidence/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^irrelevant$/i })).not.toBeInTheDocument();
    await screen.user.click(screen.getByRole('button', { name: /cancel/i }));

    // Negative verdict → negative reasons only.
    await screen.user.click(await screen.findByRole('button', { name: /not useful/i }));
    expect(await screen.findByRole('button', { name: /^irrelevant$/i })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /wrong market/i })).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /useful insight/i })).not.toBeInTheDocument();
  });

  it('submits a not-useful verdict with a selected structured reason', async () => {
    let posted: Record<string, unknown> | null = null;
    enableFeedback([], (body) => {
      posted = body;
    });
    const screen = render();

    await screen.user.click(await screen.findByRole('button', { name: /not useful/i }));
    await screen.user.click(await screen.findByRole('button', { name: /wrong market/i }));
    await screen.user.click(screen.getByRole('button', { name: /submit feedback/i }));

    expect(await screen.findByText('Feedback recorded')).toBeInTheDocument();
    expect(posted).toEqual({
      intelligence_record_id: REC,
      is_useful: false,
      reason_code: 'wrong_market',
    });
  });

  it('renders an append-only history with verdict + reason and no edit/delete controls', async () => {
    enableFeedback([
      feedbackRow({ id: 'fb-a', is_useful: true, reason_code: 'useful_insight' }),
      feedbackRow({ id: 'fb-b', is_useful: false, reason_code: 'duplicate' }),
    ]);
    const screen = render();

    const list = await screen.findByRole('list', { name: /feedback history/i });
    const rows = within(list);
    expect(rows.getByText('Useful')).toBeInTheDocument();
    expect(rows.getByText('Not useful')).toBeInTheDocument();
    expect(rows.getByText('Useful insight')).toBeInTheDocument();
    expect(rows.getByText('Duplicate')).toBeInTheDocument();
    // Immutable: nothing to edit, delete or replace.
    expect(screen.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /delete/i })).not.toBeInTheDocument();
  });

  it('surfaces a submit failure as a graceful error toast (feature toggled off mid-session)', async () => {
    enableCapability(true);
    server.use(
      http.get(FEEDBACK, () => HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 })),
      http.post(FEEDBACK, () =>
        HttpResponse.json(
          { error: { code: 'capability_unavailable', message: 'Opportunity feedback is not available yet.' } },
          { status: 503 },
        ),
      ),
    );
    const screen = render();

    await screen.user.click(await screen.findByRole('button', { name: /^useful$/i }));
    await screen.user.click(await screen.findByRole('button', { name: /submit feedback/i }));

    expect(await screen.findByText('Could not record feedback')).toBeInTheDocument();
  });
  it('shows a retryable error state for a genuine (non-gate) failure', async () => {
    // A 429 is a deterministic client error (never retried) that is not the 503
    // gate nor a 403 — so the panel surfaces a retry affordance rather than hiding.
    enableCapability(true);
    server.use(
      http.get(FEEDBACK, () =>
        HttpResponse.json({ error: { code: 'rate_limited', message: 'slow down' } }, { status: 429 }),
      ),
    );
    const screen = render();
    expect(await screen.findByRole('button', { name: /try again/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /feedback/i })).toBeInTheDocument();
  });

  it('stays dark when the raw global flag is ON but the workspace-effective answer is OFF', async () => {
    // THE FOURTH QUADRANT: raw global = TRUE, workspace-effective = FALSE.
    //
    // Before this row, all 11 `enableCapability(...)` call sites across the two
    // panel files passed `enabled = true`, and `opportunity_feedback_enabled:
    // true` appeared in ZERO frontend tests. The suite only ever drove the two
    // values in AGREEMENT, or in the canary direction (raw=false/effective=true).
    // With them agreeing, a regression that reads the raw-global summary is
    // invisible: both sources say the same thing, so reading the wrong one costs
    // nothing observable.
    //
    // This row's detection power does NOT depend on `globalFlag`'s default,
    // because it passes `true` explicitly. If the fixture helper were ever
    // re-coupled (`globalFlag = enabled`), every other row here would silently
    // lose its ability to catch a raw-global regression; this one would not.
    // It does not DETECT re-coupling — an explicit argument still wins — it
    // makes re-coupling harmless.
    let capRequests = 0;
    let historyRequests = 0;
    enableCapability(false, true);
    server.use(
      http.get(P('/workspaces/:workspaceId/feedback-capability'), () => {
        capRequests += 1;
        return HttpResponse.json({ enabled: false });
      }),
      http.get(FEEDBACK, () => {
        historyRequests += 1;
        return HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    const screen = render();

    // Settlement, not a timer: the panel demonstrably asked for and received the
    // workspace-effective answer before anything below is asserted.
    await waitFor(() => expect(capRequests).toBeGreaterThan(0));

    // Hidden means BOTH: no verdict affordance and no feedback section at all.
    expect(screen.queryByRole('button', { name: /^useful$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /feedback/i })).not.toBeInTheDocument();
    // And the history was never requested — the gate ran before the hook fired.
    expect(historyRequests).toBe(0);
  });
});
