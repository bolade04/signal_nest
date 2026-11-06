import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type {
  ApprovalHistoryOut,
  ApprovalOut,
  ContentReviewOut,
  CreativeDraftListOut,
  CreativeDraftOut,
  RecommendationBriefListOut,
  RecommendationBriefOut,
} from '@/api/types';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * The I-5C integration cycle (W21 INT5AB): `pages/creative/` mounted at `/creative` and
 * `/creative/:briefId` with its sidebar entry, the brief panel's link to a brief's drafts, the
 * Generate gate (battery O-5) and the draft decision controls.
 *
 * The oracle is the API's policy, written out here rather than read from lib/roles.ts:
 *   draft reads               = every member              (app/creative/routes.py get_tenant_context)
 *   generate                  = owner, admin, marketer    (EDITORS, require_role) AND every daily
 *                               ceiling usable (503 creative_generation_unconfigured otherwise)
 *                               AND the brief's controlling decision approved (422 otherwise)
 *   draft decision history    = every role but viewer     (require_exact_roles(APPROVAL_HISTORY_READERS))
 *   decide on a draft         = reviewer, exact, never the author; approve refused while the
 *                               stored review did not pass; revoke only while approved
 *   all of them               = dark unless creative_generation resolves enabled for the workspace
 */

const WS = 'ws-1';
const OPP = 'opp-1';
const BRIEF = 'brief-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const ROLES: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];
const EDITORS = new Set<ModelRole>(['owner', 'admin', 'marketer']);

function review(overrides: Partial<ContentReviewOut> = {}): ContentReviewOut {
  return {
    id: 'review-1',
    draft_id: 'draft-1',
    draft_version: 1,
    risk_level: 'low',
    is_blocked: false,
    findings: [],
    warnings: [],
    blocked_claim_ids: [],
    required_disclaimers: [],
    disclaimers_resolved: true,
    passed: true,
    created_at: '2026-11-02T10:00:00Z',
    ...overrides,
  } satisfies ContentReviewOut;
}

function draft(overrides: Partial<CreativeDraftOut> = {}): CreativeDraftOut {
  return {
    id: 'draft-1',
    brief_id: BRIEF,
    opportunity_id: OPP,
    location_id: null,
    campaign_id: null,
    draft_version: 1,
    parent_draft_id: null,
    channel: 'sms',
    headline: '',
    body: 'A quick note: faster mobile pickup in Dallas.',
    call_to_action: 'Plan your visit',
    prompt_name: 'generate_creative',
    prompt_version: '1.0.0',
    model_identifier: 'mock-1',
    is_simulated: true,
    created_by: 'user-marketer',
    created_by_snapshot: 'user-marketer Mara Marketer <mara@example.com>',
    created_at: '2026-11-02T10:00:00Z',
    review: review(),
    ...overrides,
  } satisfies CreativeDraftOut;
}

function decision(overrides: Partial<ApprovalOut> = {}): ApprovalOut {
  return {
    id: 'appr-d1',
    workspace_id: WS,
    subject_type: 'creative_draft',
    subject_id: 'draft-1',
    subject_version: 1,
    brief_id: null,
    draft_id: 'draft-1',
    decision: 'approved',
    decision_seq: 1,
    reason_code: null,
    note: null,
    decided_by: 'user-reviewer',
    decided_by_snapshot: 'user-reviewer Rhea Reviewer <rhea@example.com>',
    decided_role: 'reviewer',
    is_simulated: true,
    created_at: '2026-11-02T11:00:00Z',
    ...overrides,
  } satisfies ApprovalOut;
}

function page<T>(items: T[]) {
  return { items, total: items.length, limit: 20, offset: 0 };
}

interface Backend {
  capabilityRequests: number;
  draftListRequests: number;
  historyRequests: number;
  briefHistoryRequests: number;
  posts: unknown[];
}

/**
 * A stateful creative backend for one workspace. `enabled` is the workspace-effective
 * decision, `configured` the reflection's generation_configured; while dark every draft route
 * answers the 503 gate (the default handlers do the same).
 */
function installCreative({
  enabled = true,
  configured = true,
  capabilityStatus = 200,
  drafts = [draft()],
  history = [] as ApprovalOut[],
  briefDecision = 'approved' as ApprovalOut['decision'] | null,
  post = null as null | { status: number; body: object },
} = {}): Backend {
  const state: Backend = {
    capabilityRequests: 0,
    draftListRequests: 0,
    historyRequests: 0,
    briefHistoryRequests: 0,
    posts: [],
  };
  const decisions = [...history];
  const dark = () =>
    HttpResponse.json(
      { error: { code: 'capability_unavailable', message: 'Not available yet.' } },
      { status: 503 },
    );
  server.use(
    http.get(P(`/workspaces/${WS}/creative-capability`), () => {
      state.capabilityRequests += 1;
      if (capabilityStatus !== 200) return HttpResponse.json({}, { status: capabilityStatus });
      return HttpResponse.json({ enabled, generation_configured: enabled && configured });
    }),
    http.get(P(`/workspaces/${WS}/briefs/${BRIEF}/drafts`), () => {
      state.draftListRequests += 1;
      return enabled ? HttpResponse.json(page(drafts) satisfies CreativeDraftListOut) : dark();
    }),
    http.get(P(`/workspaces/${WS}/opportunities/${OPP}/briefs/${BRIEF}/approvals`), () => {
      state.briefHistoryRequests += 1;
      const items = briefDecision
        ? [decision({ subject_type: 'recommendation_brief', subject_id: BRIEF, brief_id: BRIEF,
            draft_id: null, decision: briefDecision })] // prettier-ignore
        : [];
      return HttpResponse.json(page(items) satisfies ApprovalHistoryOut);
    }),
    http.get(P(`/workspaces/${WS}/drafts/:draftId/approvals`), ({ params }) => {
      state.historyRequests += 1;
      if (!enabled) return dark();
      const items = decisions
        .filter((d) => d.subject_id === params.draftId)
        .sort((a, b) => b.decision_seq - a.decision_seq);
      return HttpResponse.json(page(items) satisfies ApprovalHistoryOut);
    }),
    http.post(P(`/workspaces/${WS}/drafts/:draftId/approvals`), async ({ request, params }) => {
      if (!enabled) return dark();
      const body = (await request.json()) as { decision: ApprovalOut['decision']; subject_version: number };
      state.posts.push(body);
      if (post) return HttpResponse.json(post.body, { status: post.status });
      const recorded = decision({
        id: `appr-${decisions.length + 1}`,
        subject_id: String(params.draftId),
        draft_id: String(params.draftId),
        subject_version: body.subject_version,
        decision: body.decision,
        decision_seq: decisions.length + 1,
      });
      decisions.push(recorded);
      return HttpResponse.json(recorded, { status: 201 });
    }),
  );
  return state;
}

function asRole(role: ModelRole) {
  orgModel.setMembership('org-1', 'user-1', role);
}

type Screen = ReturnType<typeof renderApp>;

async function primaryNav(screen: Screen) {
  const nav = await screen.findByRole('navigation', { name: 'Primary' });
  await within(nav).findByRole('link', { name: 'Overview' });
  return nav;
}

const draftsUrl = `/creative/${BRIEF}?opportunity=${OPP}`;

beforeEach(() => {
  orgModel.reset();
});

describe('I-5C: the creative route and its sidebar entry', () => {
  it.each(ROLES)('a %s in an enabled workspace gets the entry and the landing page', async (role) => {
    asRole(role);
    installCreative();
    const screen = renderApp(<App />, { route: '/' });
    const nav = await primaryNav(screen);
    const link = await within(nav).findByRole('link', { name: 'Creative Drafts' });
    await screen.user.click(link);
    expect(await screen.findByRole('heading', { level: 1, name: 'Creative drafts' })).toBeInTheDocument();
    expect(link).toHaveAttribute('aria-current', 'page');
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' });
    expect(within(crumbs).getByText('Creative Drafts')).toHaveAttribute('aria-current', 'page');
  });

  it.each([
    ['dark', { enabled: false }],
    ['a failed lookup', { capabilityStatus: 500 }],
  ] as const)('%s: no entry, both routes are the not-found page, no draft request', async (_label, opts) => {
    asRole('owner');
    const backend = installCreative(opts);
    for (const route of ['/creative', draftsUrl]) {
      const screen = renderApp(<App />, { route });
      expect(await screen.findByRole('heading', { name: 'Page not found' })).toBeInTheDocument();
      const nav = await primaryNav(screen);
      expect(within(nav).queryByRole('link', { name: 'Creative Drafts' })).not.toBeInTheDocument();
      screen.unmount();
    }
    expect(backend.capabilityRequests).toBeGreaterThan(0);
    expect(backend.draftListRequests).toBe(0);
    expect(backend.historyRequests).toBe(0);
  });
});

describe('I-5C: the brief panel links to the shown version’s drafts', () => {
  const OPP_FIXTURE = 'opp-loc-dallas-0';

  function briefs(enabled: boolean) {
    const brief = {
      id: 'brief-9',
      opportunity_id: OPP_FIXTURE,
      intelligence_record_id: `rec-${OPP_FIXTURE}`,
      location_id: null,
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
      created_by_snapshot: 'user-marketer Mara Marketer <mara@example.com>',
      created_at: '2026-11-02T09:00:00Z',
      summary: 'A brief.',
      rationale: 'Inference.',
      recommended_actions: [],
      suggested_angles: [],
      risk_notes: [],
      evidence_refs: [],
    } satisfies RecommendationBriefOut;
    server.use(
      http.get(P(`/workspaces/${WS}/brief-capability`), () => HttpResponse.json({ enabled: true })),
      http.get(P(`/workspaces/${WS}/opportunities/${OPP_FIXTURE}/briefs`), () =>
        HttpResponse.json(page([brief]) satisfies RecommendationBriefListOut),
      ),
      http.get(P(`/workspaces/${WS}/creative-capability`), () =>
        HttpResponse.json({ enabled, generation_configured: enabled }),
      ),
    );
  }

  it.each([true, false])('creative enabled = %s', async (enabled) => {
    asRole('viewer');
    briefs(enabled);
    const screen = renderApp(<App />, { route: `/opportunities/${OPP_FIXTURE}` });
    const panel = await screen.findByRole('region', { name: /recommendation brief/i });
    await within(panel).findByText('A brief.');
    await new Promise((r) => setTimeout(r, 50));
    const link = within(panel).queryByRole('link', { name: 'Open drafts' });
    if (enabled) {
      expect(link).toHaveAttribute('href', `/creative/brief-9?opportunity=${OPP_FIXTURE}`);
    } else {
      expect(link).not.toBeInTheDocument();
    }
  });
});

describe('I-5C: Generate is offered only when the API would accept it (battery O-5)', () => {
  it.each(ROLES)('a %s, configured and approved', async (role) => {
    asRole(role);
    installCreative();
    const screen = renderApp(<App />, { route: draftsUrl });
    await screen.findByRole('navigation', { name: 'Draft versions' });
    if (EDITORS.has(role)) {
      expect(await screen.findByRole('button', { name: 'Generate draft' })).toBeInTheDocument();
    } else {
      await new Promise((r) => setTimeout(r, 50));
      expect(screen.queryByRole('group', { name: 'Generate a draft' })).not.toBeInTheDocument();
      // A non-editor gets neither the editor nor an editor's explanation.
      expect(
        screen.queryByText(/Generating drafts isn't available|Approve this brief before/),
      ).not.toBeInTheDocument();
    }
  });

  it.each([
    ['generation is not configured', { configured: false }, "Generating drafts isn't available in this workspace yet."],
    ['the brief is not approved', { briefDecision: null }, 'Approve this brief before generating drafts from it.'],
    ['the brief was rejected', { briefDecision: 'rejected' as const }, 'Approve this brief before generating drafts from it.'],
    ['the brief approval was revoked', { briefDecision: 'revoked' as const }, 'Approve this brief before generating drafts from it.'],
  ])('an editor is not offered Generate when %s, and is told why', async (_label, opts, note) => {
    asRole('marketer');
    installCreative(opts);
    const screen = renderApp(<App />, { route: draftsUrl });
    expect(await screen.findByText(note)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Generate draft' })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Revise version/ })).not.toBeInTheDocument();
  });

  it('an editor is not offered Generate without the brief’s opportunity', async () => {
    asRole('owner');
    const backend = installCreative();
    const screen = renderApp(<App />, { route: `/creative/${BRIEF}` });
    expect(await screen.findByText('Open this brief from its opportunity to generate drafts.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Generate draft' })).not.toBeInTheDocument();
    expect(backend.briefHistoryRequests).toBe(0);
  });
});

describe('I-5C: draft decisions match the API for every role', () => {
  it.each(ROLES)('a %s, on a draft whose review passed', async (role) => {
    asRole(role);
    const backend = installCreative();
    const screen = renderApp(<App />, { route: draftsUrl });
    const article = await screen.findByRole('article', { name: 'Draft version 1' });
    if (role === 'viewer') {
      await new Promise((r) => setTimeout(r, 50));
      expect(within(article).queryByRole('heading', { name: 'Approval' })).not.toBeInTheDocument();
      expect(backend.historyRequests).toBe(0);
    } else {
      expect(await within(article).findByRole('heading', { name: 'Approval' })).toBeInTheDocument();
      expect(within(article).getByText('No decision recorded for this version yet.')).toBeInTheDocument();
    }
    const approve = within(article).queryByRole('button', { name: 'Approve' });
    const reject = within(article).queryByRole('button', { name: 'Reject' });
    if (role === 'reviewer') {
      expect(approve).toBeInTheDocument();
      expect(reject).toBeInTheDocument();
    } else {
      expect(approve).not.toBeInTheDocument();
      expect(reject).not.toBeInTheDocument();
    }
    expect(within(article).queryByRole('button', { name: 'Revoke approval' })).not.toBeInTheDocument();
  });

  it('a blocked review: the reviewer may reject but is not offered Approve', async () => {
    asRole('reviewer');
    installCreative({
      drafts: [draft({ review: review({ is_blocked: true, blocked_claim_ids: ['claim-1'], passed: false }) })],
    });
    const screen = renderApp(<App />, { route: draftsUrl });
    const article = await screen.findByRole('article', { name: 'Draft version 1' });
    expect(await within(article).findByRole('button', { name: 'Reject' })).toBeInTheDocument();
    expect(within(article).queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
    expect(within(article).getByText(/content review doesn.t allow it/)).toBeInTheDocument();
  });

  it.each([
    ['a missing disclaimer', review({ disclaimers_resolved: false, passed: false })],
    ['no review yet', null],
  ])('%s: no Approve for the reviewer', async (_label, rev) => {
    asRole('reviewer');
    installCreative({ drafts: [draft({ review: rev })] });
    const screen = renderApp(<App />, { route: draftsUrl });
    const article = await screen.findByRole('article', { name: 'Draft version 1' });
    expect(await within(article).findByRole('button', { name: 'Reject' })).toBeInTheDocument();
    expect(within(article).queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('an approved draft: the reviewer may revoke or reject, not approve again', async () => {
    asRole('reviewer');
    installCreative({ history: [decision()] });
    const screen = renderApp(<App />, { route: draftsUrl });
    const article = await screen.findByRole('article', { name: 'Draft version 1' });
    const history = await within(article).findByRole('list', { name: 'Draft decision history' });
    expect(history).toHaveTextContent('Approved');
    expect(history).toHaveTextContent('Current decision');
    expect(within(article).getByRole('button', { name: 'Revoke approval' })).toBeInTheDocument();
    expect(within(article).getByRole('button', { name: 'Reject' })).toBeInTheDocument();
    expect(within(article).queryByRole('button', { name: 'Approve' })).not.toBeInTheDocument();
  });

  it('the author is never offered a decision, even as a reviewer', async () => {
    asRole('reviewer');
    installCreative({
      drafts: [draft({ created_by: 'user-1', created_by_snapshot: 'user-1 Demo <demo@signalnest.dev>' })],
      history: [decision()],
    });
    const screen = renderApp(<App />, { route: draftsUrl });
    const article = await screen.findByRole('article', { name: 'Draft version 1' });
    expect(await within(article).findByText(/You generated this draft/)).toBeInTheDocument();
    for (const name of ['Approve', 'Reject', 'Revoke approval']) {
      expect(within(article).queryByRole('button', { name })).not.toBeInTheDocument();
    }
  });

  it('the reviewer approves the exact version and the decision shows', async () => {
    asRole('reviewer');
    const backend = installCreative();
    const screen = renderApp(<App />, { route: draftsUrl });
    const article = await screen.findByRole('article', { name: 'Draft version 1' });
    await screen.user.click(await within(article).findByRole('button', { name: 'Approve' }));
    const dialog = await screen.findByRole('dialog');
    await screen.user.click(within(dialog).getByRole('button', { name: 'Approve' }));
    const history = await within(article).findByRole('list', { name: 'Draft decision history' });
    await waitFor(() => expect(history).toHaveTextContent('Approved'));
    expect(backend.posts).toEqual([{ decision: 'approved', subject_version: 1 }]);
    expect(within(article).getByRole('button', { name: 'Revoke approval' })).toBeInTheDocument();
  });

  it('a refusal by the library re-check is explained with static copy, never the server message', async () => {
    asRole('reviewer');
    installCreative({
      post: {
        status: 409,
        body: { error: { code: 'approval_review_blocked', message: 'SERVER-TEXT-NOT-SHOWN' } },
      },
    });
    const screen = renderApp(<App />, { route: draftsUrl });
    const article = await screen.findByRole('article', { name: 'Draft version 1' });
    await screen.user.click(await within(article).findByRole('button', { name: 'Approve' }));
    const dialog = await screen.findByRole('dialog');
    await screen.user.click(within(dialog).getByRole('button', { name: 'Approve' }));
    expect(await screen.findByText(/the claims library changed since it was reviewed/)).toBeInTheDocument();
    expect(screen.queryByText('SERVER-TEXT-NOT-SHOWN')).not.toBeInTheDocument();
  });
});
