import { within } from '@testing-library/react';
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

// Demote the demo OWNER to another role for the role-gate tests.
function asRole(role: string) {
  server.use(
    http.get(P('/auth/me'), () =>
      HttpResponse.json({
        access_token: 'test-token',
        token_type: 'bearer',
        user: { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo', is_operator: true },
        memberships: [{ organization_id: 'org-1', role }],
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
  it('renders nothing while the feature is dark (default 503)', async () => {
    // The default handler answers 503 capability_unavailable.
    const screen = render();
    // Give the query a tick to settle, then assert no feedback affordance.
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole('heading', { name: /feedback/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^useful$/i })).not.toBeInTheDocument();
  });

  it('renders nothing for a view-only member even when the feature is enabled', async () => {
    asRole('viewer');
    enableFeedback();
    const screen = render();
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole('button', { name: /^useful$/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/no feedback recorded yet/i)).not.toBeInTheDocument();
  });

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

  it('renders nothing on a 403 (unauthorized)', async () => {
    server.use(
      http.get(FEEDBACK, () =>
        HttpResponse.json({ error: { code: 'forbidden', message: 'no' } }, { status: 403 }),
      ),
    );
    const screen = render();
    await new Promise((r) => setTimeout(r, 0));
    expect(screen.queryByRole('button', { name: /^useful$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { name: /feedback/i })).not.toBeInTheDocument();
  });

  it('shows a retryable error state for a genuine (non-gate) failure', async () => {
    // A 429 is a deterministic client error (never retried) that is not the 503
    // gate nor a 403 — so the panel surfaces a retry affordance rather than hiding.
    server.use(
      http.get(FEEDBACK, () =>
        HttpResponse.json({ error: { code: 'rate_limited', message: 'slow down' } }, { status: 429 }),
      ),
    );
    const screen = render();
    expect(await screen.findByRole('button', { name: /try again/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /feedback/i })).toBeInTheDocument();
  });
});
