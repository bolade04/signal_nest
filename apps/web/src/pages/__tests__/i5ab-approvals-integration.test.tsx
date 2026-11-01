import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type {
  ApprovalHistoryOut,
  ApprovalOut,
  RecommendationBriefListOut,
  RecommendationBriefOut,
} from '@/api/types';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * The I-5A + I-5B integration cycle (W21 INT5AB): the approval queue mounted at `/approvals`
 * with its sidebar entry, and the 5B decision history mounted in the 5A brief panel.
 *
 * The oracle is the API's policy, written out here rather than read from lib/roles.ts
 * (coordinator rulings, W21 2026-11-01):
 *   brief reads            = every member                          (app/briefs/routes.py)
 *   decision history       = every member but viewer, exact        (app/approvals/routes.py)
 *   decide + queue         = reviewer, exact                       (app/approvals/policy.py)
 *   all of them            = dark unless `recommendation_briefs` resolves enabled for the
 *                            workspace (one switch; the reflection is GET .../brief-capability)
 * Fixtures are typed by the generated contract (`satisfies`).
 */

const WS = 'ws-1';
const OPP = 'opp-loc-dallas-0';
const OPP_TITLE = 'Dallas customers want faster delivery 1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const ROLES: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];

function brief(overrides: Partial<RecommendationBriefOut> = {}): RecommendationBriefOut {
  return {
    id: 'brief-1',
    opportunity_id: OPP,
    intelligence_record_id: `rec-${OPP}`,
    location_id: 'loc-dallas',
    campaign_id: null,
    brief_version: 1,
    parent_brief_id: null,
    analysis_version: '3c',
    scoring_version: '3c.1',
    prompt_name: 'explain_opportunity',
    prompt_version: '1.2.0',
    model_identifier: 'mock-1',
    is_simulated: true,
    created_by: 'user-marketer',
    created_by_snapshot: 'user-marketer Mara Marketer <mara.marketer@example.com>',
    created_at: '2026-11-01T09:00:00Z',
    summary: 'Publish a faster-service post for Dallas commuters.',
    rationale: 'Inference: commuters are choosing on speed.',
    recommended_actions: ['Publish a brand-safe post about faster service.'],
    suggested_angles: [],
    risk_notes: [],
    evidence_refs: [`rec-${OPP}`],
    ...overrides,
  } satisfies RecommendationBriefOut;
}

function approval(overrides: Partial<ApprovalOut> = {}): ApprovalOut {
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
    created_at: '2026-11-01T10:00:00Z',
    ...overrides,
  } satisfies ApprovalOut;
}

interface Backend {
  capabilityRequests: number;
  queueRequests: number;
  historyRequests: number;
  posts: unknown[];
}

/**
 * A small stateful briefs + approvals backend for one workspace. `enabled` is the
 * workspace-effective decision; while it is false every brief and approval endpoint answers
 * the 503 gate (the shipped default handlers do the same). A recorded decision is appended to
 * the history and drops the brief from the queue, as the server does.
 */
function installBackend({
  enabled,
  briefs = [brief()],
  history = [] as ApprovalOut[],
  capabilityStatus = 200,
}: {
  enabled: boolean;
  briefs?: RecommendationBriefOut[];
  history?: ApprovalOut[];
  capabilityStatus?: number;
}): Backend {
  const state: Backend = { capabilityRequests: 0, queueRequests: 0, historyRequests: 0, posts: [] };
  const decisions = [...history];
  const dark = () =>
    HttpResponse.json(
      { error: { code: 'capability_unavailable', message: 'Not available yet.' } },
      { status: 503 },
    );
  const awaiting = () =>
    briefs.filter((b) => !decisions.some((d) => d.subject_id === b.id && d.decision !== 'revoked'));
  server.use(
    http.get(P(`/workspaces/${WS}/brief-capability`), () => {
      state.capabilityRequests += 1;
      if (capabilityStatus !== 200) return HttpResponse.json({}, { status: capabilityStatus });
      return HttpResponse.json({ enabled });
    }),
    http.get(P(`/workspaces/${WS}/opportunities/${OPP}/briefs`), () =>
      enabled
        ? HttpResponse.json({
            items: briefs,
            total: briefs.length,
            limit: 20,
            offset: 0,
          } satisfies RecommendationBriefListOut)
        : dark(),
    ),
    http.get(P(`/workspaces/${WS}/approval-queue`), () => {
      state.queueRequests += 1;
      if (!enabled) return dark();
      const items = awaiting();
      return HttpResponse.json({
        items,
        total: items.length,
        limit: 50,
        offset: 0,
      } satisfies RecommendationBriefListOut);
    }),
    http.get(P(`/workspaces/${WS}/opportunities/:opp/briefs/:brief/approvals`), ({ params }) => {
      state.historyRequests += 1;
      if (!enabled) return dark();
      const items = decisions
        .filter((d) => d.subject_id === params.brief)
        .sort((a, b) => b.decision_seq - a.decision_seq);
      return HttpResponse.json({
        items,
        total: items.length,
        limit: 20,
        offset: 0,
      } satisfies ApprovalHistoryOut);
    }),
    http.post(
      P(`/workspaces/${WS}/opportunities/:opp/briefs/:brief/approvals`),
      async ({ request, params }) => {
        if (!enabled) return dark();
        const body = (await request.json()) as {
          decision: ApprovalOut['decision'];
          subject_version: number;
        };
        state.posts.push(body);
        const recorded = approval({
          id: `appr-${decisions.length + 1}`,
          subject_id: String(params.brief),
          brief_id: String(params.brief),
          subject_version: body.subject_version,
          decision: body.decision,
          decision_seq: decisions.length + 1,
        });
        decisions.push(recorded);
        return HttpResponse.json(recorded, { status: 201 });
      },
    ),
  );
  return state;
}

function asRole(role: ModelRole) {
  orgModel.setMembership('org-1', 'user-1', role);
}

type Screen = ReturnType<typeof renderApp>;

const primaryNav = (screen: Screen) => screen.getByRole('navigation', { name: 'Primary' });

async function settle(screen: Screen) {
  // The shell has rendered and the session's role is known (the sidebar shows Overview).
  const nav = await screen.findByRole('navigation', { name: 'Primary' });
  await within(nav).findByRole('link', { name: 'Overview' });
  await new Promise((r) => setTimeout(r, 50));
}

beforeEach(() => {
  orgModel.reset();
});

describe('I-5B: the approval queue route and its sidebar entry', () => {
  it('a reviewer in an enabled workspace gets the sidebar entry, the route, the queue and a breadcrumb', async () => {
    asRole('reviewer');
    const backend = installBackend({ enabled: true });
    const screen = renderApp(<App />, { route: '/' });
    const nav = await screen.findByRole('navigation', { name: 'Primary' });
    const link = await within(nav).findByRole('link', { name: 'Approvals' });

    await screen.user.click(link);
    expect(await screen.findByRole('heading', { level: 1, name: 'Approvals' })).toBeInTheDocument();
    const list = await screen.findByRole('list', { name: 'Briefs awaiting a decision' });
    expect(list).toHaveTextContent('Publish a faster-service post for Dallas commuters.');
    expect(link).toHaveAttribute('aria-current', 'page');
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' });
    expect(within(crumbs).getByText('Approvals')).toHaveAttribute('aria-current', 'page');
    expect(backend.queueRequests).toBeGreaterThan(0);
  });

  it('a reviewer in a dark workspace gets no entry, and the route is the not-found page with no approval request', async () => {
    asRole('reviewer');
    const backend = installBackend({ enabled: false });
    const screen = renderApp(<App />, { route: '/approvals' });
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
    await settle(screen);
    expect(within(primaryNav(screen)).queryByRole('link', { name: 'Approvals' })).not.toBeInTheDocument();
    expect(screen.queryByRole('heading', { level: 1, name: 'Approvals' })).not.toBeInTheDocument();
    expect(backend.capabilityRequests).toBeGreaterThan(0);
    expect(backend.queueRequests).toBe(0);
  });

  it('fails closed when the capability lookup fails: no entry, not-found, no queue request', async () => {
    asRole('reviewer');
    const backend = installBackend({ enabled: true, capabilityStatus: 500 });
    const screen = renderApp(<App />, { route: '/approvals' });
    expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
    await settle(screen);
    expect(within(primaryNav(screen)).queryByRole('link', { name: 'Approvals' })).not.toBeInTheDocument();
    expect(backend.queueRequests).toBe(0);
  });

  it.each(ROLES.filter((r) => r !== 'reviewer'))(
    'a %s gets no entry and the sidebar asks nothing about briefs; the route explains and asks for no queue',
    async (role) => {
      asRole(role);
      const backend = installBackend({ enabled: true });
      const home = renderApp(<App />, { route: '/' });
      expect(await home.findByRole('heading', { level: 1, name: 'Overview' })).toBeInTheDocument();
      await settle(home);
      expect(within(primaryNav(home)).queryByRole('link', { name: 'Approvals' })).not.toBeInTheDocument();
      expect(backend.capabilityRequests).toBe(0);
      home.unmount();

      const direct = renderApp(<App />, { route: '/approvals' });
      expect(await direct.findByText('Only reviewers decide on briefs')).toBeInTheDocument();
      expect(direct.queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
      expect(backend.queueRequests).toBe(0);
    },
  );
});

describe('I-5B: the decision history in the 5A brief panel', () => {
  it.each(ROLES)('a %s reads the brief; the history shows exactly when the API admits the role', async (role) => {
    asRole(role);
    const backend = installBackend({ enabled: true, history: [approval()] });
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    const panel = await screen.findByRole('region', { name: /recommendation brief/i });
    expect(await within(panel).findByText('Publish a faster-service post for Dallas commuters.')).toBeInTheDocument();

    if (role === 'viewer') {
      await new Promise((r) => setTimeout(r, 50));
      expect(within(panel).queryByRole('heading', { name: 'Approval' })).not.toBeInTheDocument();
      expect(within(panel).queryByText('Approved')).not.toBeInTheDocument();
      expect(within(panel).queryByText(/Rhea Reviewer/)).not.toBeInTheDocument();
      expect(backend.historyRequests).toBe(0);
    } else {
      expect(await within(panel).findByRole('heading', { name: 'Approval' })).toBeInTheDocument();
      const history = within(panel).getByRole('list', { name: 'Decision history' });
      expect(history).toHaveTextContent('Approved');
      expect(history).toHaveTextContent('Current decision');
      expect(history).toHaveTextContent('by Rhea Reviewer');
      expect(history).not.toHaveTextContent('rhea.reviewer@example.com');
    }
    // Revoking is the reviewer's alone (and never the author's: the brief is Mara's here).
    const revoke = within(panel).queryByRole('button', { name: 'Revoke approval' });
    if (role === 'reviewer') expect(revoke).toBeInTheDocument();
    else expect(revoke).not.toBeInTheDocument();
  });

  it('shows no approval heading when the history answers the dark gate', async () => {
    asRole('owner');
    const backend = installBackend({ enabled: true });
    server.use(
      http.get(P(`/workspaces/${WS}/opportunities/:opp/briefs/:brief/approvals`), () => {
        backend.historyRequests += 1;
        return HttpResponse.json(
          { error: { code: 'capability_unavailable', message: 'Not available yet.' } },
          { status: 503 },
        );
      }),
    );
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    const panel = await screen.findByRole('region', { name: /recommendation brief/i });
    await waitFor(() => expect(backend.historyRequests).toBeGreaterThan(0));
    await new Promise((r) => setTimeout(r, 50));
    expect(within(panel).queryByRole('heading', { name: 'Approval' })).not.toBeInTheDocument();
  });
});

describe('I-5A + I-5B: decide in the queue, read the decision on the brief', () => {
  it('a reviewer approves the exact version in the queue and the opportunity page shows the decision', async () => {
    asRole('reviewer');
    const backend = installBackend({ enabled: true });
    const screen = renderApp(<App />, { route: '/approvals' });
    const list = await screen.findByRole('list', { name: 'Briefs awaiting a decision' });

    await screen.user.click(within(list).getByRole('button', { name: 'Approve' }));
    const dialog = await screen.findByRole('dialog');
    await screen.user.click(within(dialog).getByRole('button', { name: 'Approve' }));
    expect(await screen.findByText('Nothing is waiting for a decision')).toBeInTheDocument();
    expect(backend.posts).toEqual([{ decision: 'approved', subject_version: 1 }]);

    // Back in the queue's own data the brief is gone; reach it from the opportunity instead.
    screen.unmount();
    const detail = renderApp(<App />, { route: `/opportunities/${OPP}` });
    expect(await detail.findByRole('heading', { level: 1, name: OPP_TITLE })).toBeInTheDocument();
    const panel = await detail.findByRole('region', { name: /recommendation brief/i });
    const history = await within(panel).findByRole('list', { name: 'Decision history' });
    expect(history).toHaveTextContent('Approved');
    expect(history).toHaveTextContent('Current decision');
  });

  it('each queued brief links to its opportunity, where the brief panel shows it', async () => {
    asRole('reviewer');
    installBackend({ enabled: true });
    const screen = renderApp(<App />, { route: '/approvals' });
    const list = await screen.findByRole('list', { name: 'Briefs awaiting a decision' });
    await screen.user.click(within(list).getByRole('link', { name: 'Open opportunity' }));
    expect(await screen.findByRole('heading', { level: 1, name: OPP_TITLE })).toBeInTheDocument();
    const panel = await screen.findByRole('region', { name: /recommendation brief/i });
    expect(await within(panel).findByText('No decision recorded for this version yet.')).toBeInTheDocument();
  });
});
