import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import type {
  ApprovalHistoryOut,
  ApprovalOut,
  RecommendationBriefListOut,
  RecommendationBriefOut,
} from '@/api/types';
import { ApprovalQueuePage } from '@/pages/approvals/ApprovalQueue';
import { BriefDecisions } from '@/pages/approvals/BriefDecisions';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * Phase 5B approval queue and decision UI (W21 P5B), rendered through its own harness:
 * the page is not mounted on a route in this lane (the route lands at I-5B).
 *
 * The backend policy is the oracle, written out here rather than read from lib/roles.ts:
 *   decide + queue  = require_exact_roles(REVIEWER)                (approvals/policy.py)
 *   decision history = every role but viewer, exact                 (approvals/routes.py)
 * Fixtures are typed by the generated contract (`satisfies`), so a field the API does not
 * return, or a missing one, fails the type-check.
 */

const WS = 'ws-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const QUEUE = P(`/workspaces/${WS}/approval-queue`);
const DECISIONS = P(`/workspaces/${WS}/opportunities/:opp/briefs/:brief/approvals`);
const ROLES: ModelRole[] = [
  'owner',
  'admin',
  'marketer',
  'reviewer',
  'compliance_reviewer',
  'viewer',
];

function brief(overrides: Partial<RecommendationBriefOut> = {}): RecommendationBriefOut {
  return {
    id: 'brief-1',
    opportunity_id: 'opp-1',
    intelligence_record_id: 'rec-1',
    location_id: null,
    campaign_id: null,
    brief_version: 1,
    parent_brief_id: null,
    analysis_version: '3c',
    scoring_version: '3c.1',
    prompt_name: 'recommendation_brief',
    prompt_version: '1.0.0',
    model_identifier: 'mock-model',
    is_simulated: true,
    created_by: 'user-marketer',
    created_by_snapshot: 'user-marketer Mara Marketer <mara.marketer@example.com>',
    created_at: '2026-10-30T09:00:00Z',
    summary: 'Open a second coffee cart near the stadium.',
    rationale: 'Match-day footfall doubled in the evidence.',
    recommended_actions: ['Book the pitch for Saturday.'],
    suggested_angles: [],
    risk_notes: ['Weather dependent.'],
    evidence_refs: ['rec-1'],
    ...overrides,
  } satisfies RecommendationBriefOut;
}

function decision(overrides: Partial<ApprovalOut> = {}): ApprovalOut {
  return {
    id: 'appr-1',
    workspace_id: WS,
    subject_type: 'recommendation_brief',
    subject_id: 'brief-1',
    subject_version: 1,
    brief_id: 'brief-1',
    decision: 'approved',
    decision_seq: 1,
    reason_code: null,
    note: null,
    decided_by: 'user-reviewer',
    decided_by_snapshot: 'user-reviewer Rhea Reviewer <rhea.reviewer@example.com>',
    decided_role: 'reviewer',
    is_simulated: true,
    created_at: '2026-10-31T09:00:00Z',
    ...overrides,
  } satisfies ApprovalOut;
}

interface Backend {
  queueRequests: number;
  posts: { url: string; body: unknown }[];
}

/** A small stateful approvals backend: the queue drops a brief once it is decided. */
function installBackend(
  items: RecommendationBriefOut[],
  { history = [] as ApprovalOut[], postStatus = 201, postError = null as null | object } = {},
): Backend {
  const state: Backend = { queueRequests: 0, posts: [] };
  let queue = [...items];
  server.use(
    http.get(QUEUE, () => {
      state.queueRequests += 1;
      return HttpResponse.json({
        items: queue,
        total: queue.length,
        limit: 50,
        offset: 0,
      } satisfies RecommendationBriefListOut);
    }),
    http.get(DECISIONS, () =>
      HttpResponse.json({
        items: history,
        total: history.length,
        limit: 20,
        offset: 0,
      } satisfies ApprovalHistoryOut),
    ),
    http.post(DECISIONS, async ({ request, params }) => {
      const body = await request.json();
      state.posts.push({ url: request.url, body });
      if (postError) return HttpResponse.json(postError, { status: postStatus });
      queue = queue.filter((b) => b.id !== params.brief);
      const b = body as { decision: ApprovalOut['decision']; subject_version: number };
      return HttpResponse.json(
        decision({
          decision: b.decision,
          subject_version: b.subject_version,
          subject_id: String(params.brief),
        }),
        { status: 201 },
      );
    }),
  );
  return state;
}

function asRole(role: ModelRole) {
  orgModel.setMembership('org-1', 'user-1', role);
}

beforeEach(() => {
  orgModel.reset();
});

describe('approval queue — role gate (exact REVIEWER)', () => {
  it.each(ROLES.filter((r) => r !== 'reviewer'))(
    '%s sees why it cannot decide, and no queue request is made',
    async (role) => {
      asRole(role);
      const backend = installBackend([brief()]);
      const screen = renderApp(<ApprovalQueuePage />);
      expect(await screen.findByText('Only reviewers decide on briefs')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
      expect(backend.queueRequests).toBe(0);
    },
  );

  it('a reviewer sees the queue, oldest first, with the brief content and author name', async () => {
    asRole('reviewer');
    installBackend([
      brief(),
      brief({ id: 'brief-2', opportunity_id: 'opp-2', summary: 'Second.' }),
    ]);
    const screen = renderApp(<ApprovalQueuePage />);
    const list = await screen.findByRole('list', { name: 'Briefs awaiting a decision' });
    const cards = within(list).getAllByRole('listitem', { name: undefined });
    expect(cards[0]).toHaveTextContent('Open a second coffee cart near the stadium.');
    expect(cards[0]).toHaveTextContent('Written by Mara Marketer');
    expect(cards[0]).not.toHaveTextContent('mara.marketer@example.com');
    expect(cards[0]).toHaveTextContent('Book the pitch for Saturday.');
    expect(cards[0]).toHaveTextContent('Weather dependent.');
    expect(screen.getByText('2 briefs awaiting a decision')).toBeInTheDocument();
  });
});

describe('approval queue — deciding', () => {
  it('approves the exact version with a polarity-correct reason and a note', async () => {
    asRole('reviewer');
    const backend = installBackend([brief()]);
    const screen = renderApp(<ApprovalQueuePage />);
    await screen.user.click(await screen.findByRole('button', { name: 'Approve' }));
    const dialog = await screen.findByRole('dialog', { name: 'Approve version 1' });
    const reasons = within(dialog).getByRole('group');
    expect(
      within(reasons)
        .getAllByRole('button')
        .map((b) => b.textContent),
    ).toEqual(['Evidence is sufficient', 'Aligned with strategy', 'Other']);
    await screen.user.click(
      within(reasons).getByRole('button', { name: 'Evidence is sufficient' }),
    );
    await screen.user.type(within(dialog).getByLabelText('Note (optional)'), '  Looks right.  ');
    await screen.user.click(within(dialog).getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(backend.posts).toHaveLength(1));
    expect(backend.posts[0]?.url).toContain(
      `/workspaces/${WS}/opportunities/opp-1/briefs/brief-1/approvals`,
    );
    expect(backend.posts[0]?.body).toEqual({
      decision: 'approved',
      subject_version: 1,
      reason_code: 'evidence_sufficient',
      note: 'Looks right.',
    });
    // The queue is refetched and the decided brief has left it.
    expect(await screen.findByText('Nothing is waiting for a decision')).toBeInTheDocument();
  });

  it('a rejection offers only rejection reasons and sends no empty reason or note', async () => {
    asRole('reviewer');
    const backend = installBackend([brief({ brief_version: 3 })]);
    const screen = renderApp(<ApprovalQueuePage />);
    await screen.user.click(await screen.findByRole('button', { name: 'Reject' }));
    const dialog = await screen.findByRole('dialog', { name: 'Reject version 3' });
    expect(
      within(within(dialog).getByRole('group'))
        .getAllByRole('button')
        .map((b) => b.textContent),
    ).toEqual([
      'Insufficient evidence',
      'Off strategy',
      'Brand or compliance risk',
      'Inaccurate',
      'Other',
    ]);
    await screen.user.click(within(dialog).getByRole('button', { name: 'Reject' }));
    await waitFor(() => expect(backend.posts).toHaveLength(1));
    expect(backend.posts[0]?.body).toEqual({ decision: 'rejected', subject_version: 3 });
  });

  it("the author's own brief is listed with the controls disabled and the reason stated", async () => {
    asRole('reviewer');
    const backend = installBackend([
      brief({
        created_by: 'user-1',
        created_by_snapshot: 'user-1 Demo Marketer <demo@signalnest.dev>',
      }),
    ]);
    const screen = renderApp(<ApprovalQueuePage />);
    expect(
      await screen.findByText('You wrote this brief, so another reviewer must decide on it.'),
    ).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Approve' })).toBeDisabled();
    expect(screen.getByRole('button', { name: 'Reject' })).toBeDisabled();
    expect(backend.posts).toHaveLength(0);
  });

  it('recognises the author by the snapshot once the author link is gone', async () => {
    asRole('reviewer');
    installBackend([
      brief({
        created_by: null,
        created_by_snapshot: 'user-1 Demo Marketer <demo@signalnest.dev>',
      }),
    ]);
    const screen = renderApp(<ApprovalQueuePage />);
    expect(await screen.findByRole('button', { name: 'Approve' })).toBeDisabled();
  });

  it("a server refusal is shown with the server's message and nothing is assumed recorded", async () => {
    asRole('reviewer');
    const backend = installBackend([brief()], {
      postStatus: 409,
      postError: {
        error: {
          code: 'approval_stale_version',
          message: "The brief version you decided on is not this brief's version.",
          request_id: 'r-1',
        },
      },
    });
    const screen = renderApp(<ApprovalQueuePage />);
    await screen.user.click(await screen.findByRole('button', { name: 'Approve' }));
    const dialog = await screen.findByRole('dialog');
    await screen.user.click(within(dialog).getByRole('button', { name: 'Approve' }));
    expect(
      await screen.findByText("The brief version you decided on is not this brief's version."),
    ).toBeInTheDocument();
    expect(backend.posts).toHaveLength(1);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    expect(screen.queryByText('Nothing is waiting for a decision')).not.toBeInTheDocument();
  });
});

describe('approval queue — dark and empty', () => {
  it('says approvals are not available while the capability is off (503)', async () => {
    asRole('reviewer');
    server.use(
      http.get(QUEUE, () =>
        HttpResponse.json(
          {
            error: {
              code: 'capability_unavailable',
              message: 'Recommendation briefs and approvals are not available yet.',
              request_id: 'r-2',
            },
          },
          { status: 503 },
        ),
      ),
    );
    const screen = renderApp(<ApprovalQueuePage />);
    expect(
      await screen.findByText('Approvals are not available for this workspace yet'),
    ).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('an empty queue says nothing is waiting', async () => {
    asRole('reviewer');
    installBackend([]);
    const screen = renderApp(<ApprovalQueuePage />);
    expect(await screen.findByText('Nothing is waiting for a decision')).toBeInTheDocument();
  });
});

describe('brief decision history', () => {
  const target = { workspaceId: WS, opportunityId: 'opp-1', briefId: 'brief-1', briefVersion: 1 };
  const authoredByOther = {
    created_by: 'user-marketer',
    created_by_snapshot: brief().created_by_snapshot,
  };
  const history = [
    decision({
      id: 'appr-3',
      decision_seq: 3,
      decision: 'approved',
      reason_code: 'aligned_with_strategy',
      note: 'Back on.',
    }),
    decision({ id: 'appr-2', decision_seq: 2, decision: 'rejected', reason_code: 'off_strategy' }),
    decision({ id: 'appr-1', decision_seq: 1, decision: 'approved' }),
  ];

  it.each(ROLES.filter((r) => r !== 'viewer'))(
    '%s reads it newest first with the current decision marked',
    async (role) => {
      asRole(role);
      installBackend([], { history });
      const screen = renderApp(<BriefDecisions target={target} brief={authoredByOther} />);
      const list = await screen.findByRole('list', { name: 'Decision history' });
      const rows = within(list).getAllByRole('listitem');
      expect(rows).toHaveLength(3);
      expect(rows[0]).toHaveTextContent('Approved');
      expect(rows[0]).toHaveTextContent('Current decision');
      expect(rows[0]).toHaveTextContent('Aligned with strategy');
      expect(rows[0]).toHaveTextContent('by Rhea Reviewer');
      expect(rows[0]).toHaveTextContent('Back on.');
      expect(rows[1]).toHaveTextContent('Rejected');
      expect(rows[1]).not.toHaveTextContent('Current decision');
      // Only the reviewer is offered the revoke of a controlling approval.
      const revoke = screen.queryByRole('button', { name: 'Revoke approval' });
      if (role === 'reviewer') expect(revoke).toBeInTheDocument();
      else expect(revoke).not.toBeInTheDocument();
    },
  );

  it('is not shown to a viewer and makes no request', async () => {
    asRole('viewer');
    let requests = 0;
    server.use(
      http.get(DECISIONS, () => {
        requests += 1;
        return HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 });
      }),
    );
    const screen = renderApp(<BriefDecisions target={target} brief={authoredByOther} />);
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole('list', { name: 'Decision history' })).not.toBeInTheDocument();
    expect(requests).toBe(0);
  });

  it('a reviewer revokes the controlling approval of the exact version', async () => {
    asRole('reviewer');
    const backend = installBackend([], { history });
    const screen = renderApp(<BriefDecisions target={target} brief={authoredByOther} />);
    await screen.user.click(await screen.findByRole('button', { name: 'Revoke approval' }));
    const dialog = await screen.findByRole('dialog', { name: 'Revoke approval version 1' });
    expect(
      within(within(dialog).getByRole('group'))
        .getAllByRole('button')
        .map((b) => b.textContent),
    ).toEqual(['Superseded', 'Circumstances changed', 'Approved in error', 'Other']);
    await screen.user.click(within(dialog).getByRole('button', { name: 'Approved in error' }));
    await screen.user.click(within(dialog).getByRole('button', { name: 'Revoke approval' }));
    await waitFor(() => expect(backend.posts).toHaveLength(1));
    expect(backend.posts[0]?.body).toEqual({
      decision: 'revoked',
      subject_version: 1,
      reason_code: 'approved_in_error',
    });
  });

  it('offers no revoke when the controlling decision is not an approval, or to the author', async () => {
    asRole('reviewer');
    installBackend([], { history: history.slice(1) }); // latest = rejected
    const screen = renderApp(<BriefDecisions target={target} brief={authoredByOther} />);
    await screen.findByRole('list', { name: 'Decision history' });
    expect(screen.queryByRole('button', { name: 'Revoke approval' })).not.toBeInTheDocument();
    screen.unmount();

    installBackend([], { history });
    const mine = renderApp(
      <BriefDecisions
        target={target}
        brief={{ created_by: 'user-1', created_by_snapshot: 'user-1 Demo <demo@signalnest.dev>' }}
      />,
    );
    await mine.findByRole('list', { name: 'Decision history' });
    expect(mine.queryByRole('button', { name: 'Revoke approval' })).not.toBeInTheDocument();
  });

  it('says so when no decision was recorded yet', async () => {
    asRole('owner');
    installBackend([], { history: [] });
    const screen = renderApp(<BriefDecisions target={target} brief={authoredByOther} />);
    expect(
      await screen.findByText('No decision recorded for this version yet.'),
    ).toBeInTheDocument();
  });
});
