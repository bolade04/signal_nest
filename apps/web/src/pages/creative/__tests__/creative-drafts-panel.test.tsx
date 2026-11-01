import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import type { ContentReviewOut, CreativeDraftOut, JobOut } from '@/api/types';
import { CreativeDraftsPanel } from '@/pages/creative/CreativeDraftsPanel';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// Project Phase 5C (W21 lane P5C): the draft editor, version history, review-warnings panel
// and blocked-state UI. The panel is not mounted in any route yet (I-5C mounts it); these
// render it directly inside the real providers, against MSW handlers whose shapes are the
// generated contract types (a wrong key fails the type-check).

const WS = 'ws-1';
const BRIEF = 'brief-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const DRAFTS = P(`/workspaces/${WS}/briefs/${BRIEF}/drafts`);

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
    created_at: '2026-11-01T10:00:00Z',
    ...overrides,
  };
}

function draft(version: number, overrides: Partial<CreativeDraftOut> = {}): CreativeDraftOut {
  const id = `draft-${version}`;
  return {
    id,
    brief_id: BRIEF,
    opportunity_id: 'opp-1',
    location_id: null,
    campaign_id: null,
    draft_version: version,
    parent_draft_id: null,
    channel: 'email',
    headline: `Headline ${version}`,
    body: `Body of version ${version}.`,
    call_to_action: 'Learn more',
    prompt_name: 'generate_creative',
    prompt_version: '1.0.0',
    model_identifier: 'mock-1',
    is_simulated: true,
    created_by: 'user-1',
    created_by_snapshot: 'user-1 Demo <demo@signalnest.dev>',
    created_at: '2026-11-01T10:00:00Z',
    review: review({ id: `review-${version}`, draft_id: id, draft_version: version }),
    ...overrides,
  };
}

function job(status: string, overrides: Partial<JobOut> = {}): JobOut {
  return {
    id: 'job-1',
    job_type: 'creative.generate',
    status,
    scout_request_id: null,
    location_id: null,
    attempt_count: 1,
    max_attempts: 5,
    priority: 0,
    last_error_code: null,
    result_summary: null,
    scheduled_for: null,
    started_at: null,
    completed_at: null,
    cancel_requested_at: null,
    cancelled_at: null,
    created_at: '2026-11-01T10:00:00Z',
    updated_at: '2026-11-01T10:00:00Z',
    ...overrides,
  };
}

function capability(enabled: boolean) {
  const calls = { get: 0 };
  server.use(
    http.get(P('/workspaces/:workspaceId/creative-capability'), () => {
      calls.get += 1;
      return HttpResponse.json({ enabled });
    }),
  );
  return calls;
}

function listDrafts(items: CreativeDraftOut[] | (() => CreativeDraftOut[])) {
  const calls = { get: 0 };
  server.use(
    http.get(DRAFTS, () => {
      calls.get += 1;
      const rows = typeof items === 'function' ? items() : items;
      return HttpResponse.json({ items: rows, total: rows.length, limit: 100, offset: 0 });
    }),
  );
  return calls;
}

function asRole(role: string) {
  server.use(
    http.get(P('/auth/me'), () =>
      HttpResponse.json({
        access_token: 'test-token',
        token_type: 'bearer',
        user: { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo', is_operator: false },
        memberships: [{ organization_id: 'org-1', role }],
      }),
    ),
  );
}

function render() {
  return renderApp(<CreativeDraftsPanel workspaceId={WS} briefId={BRIEF} />);
}

describe('CreativeDraftsPanel — dark by default', () => {
  it('renders nothing and requests no draft while the capability is off', async () => {
    const probe = capability(false);
    const calls = listDrafts([draft(1)]);
    const screen = render();
    await waitFor(() => expect(probe.get).toBe(1)); // the gate was asked and answered
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole('heading', { name: 'Drafts' })).toBeNull();
    expect(calls.get).toBe(0);
  });

  it('renders nothing when the capability cannot be read', async () => {
    server.use(
      http.get(P('/workspaces/:workspaceId/creative-capability'), () =>
        HttpResponse.json({ error: { code: 'error', message: 'down' } }, { status: 500 }),
      ),
    );
    const calls = listDrafts([draft(1)]);
    const screen = render();
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole('heading', { name: 'Drafts' })).toBeNull();
    expect(calls.get).toBe(0);
  });

  it('keeps the server 503 as defence in depth for a stale client', async () => {
    capability(true);
    server.use(
      http.get(DRAFTS, () =>
        HttpResponse.json(
          { error: { code: 'capability_unavailable', message: 'Creative generation is not available yet.' } },
          { status: 503 },
        ),
      ),
    );
    const screen = render();
    await new Promise((r) => setTimeout(r, 100));
    expect(screen.queryByRole('heading', { name: 'Drafts' })).toBeNull();
  });
});

describe('CreativeDraftsPanel — version history and review', () => {
  it('lists versions newest first and shows the newest with its review', async () => {
    capability(true);
    listDrafts([draft(2, { parent_draft_id: 'draft-1', channel: 'sms' }), draft(1)]);
    const screen = render();
    const nav = await screen.findByRole('navigation', { name: 'Draft versions' });
    const buttons = within(nav).getAllByRole('button');
    expect(buttons.map((b) => b.textContent)).toEqual([
      expect.stringContaining('Version 2 · SMS · revises version 1'),
      expect.stringContaining('Version 1 · Email'),
    ]);
    expect(buttons[0]).toHaveAttribute('aria-current', 'true');
    const article = screen.getByRole('article', { name: 'Draft version 2' });
    expect(within(article).getByText('Body of version 2.')).toBeInTheDocument();
    expect(within(article).getByText('Simulated')).toBeInTheDocument();
    expect(within(article).getByRole('region', { name: 'Content review' })).toHaveTextContent('Low risk');
    expect(within(article).queryByRole('alert')).toBeNull();
  });

  it('selecting a version shows that version', async () => {
    capability(true);
    listDrafts([draft(2), draft(1)]);
    const screen = render();
    const nav = await screen.findByRole('navigation', { name: 'Draft versions' });
    await screen.user.click(within(nav).getByRole('button', { name: /Version 1/ }));
    expect(within(nav).getByRole('button', { name: /Version 1/ })).toHaveAttribute('aria-current', 'true');
    expect(screen.getByRole('article', { name: 'Draft version 1' })).toHaveTextContent('Body of version 1.');
  });

  it('shows the blocked state, the hits and the flagged wording for a blocked draft', async () => {
    capability(true);
    listDrafts([
      draft(1, {
        review: review({
          risk_level: 'blocked',
          is_blocked: true,
          passed: false,
          blocked_claim_ids: ['claim-1', 'claim-2'],
          findings: [{ category: 'health_claim', risk: 'high', excerpt: 'cures' }],
          warnings: ["Contains a blocked claim: 'cures everything'"],
        }),
      }),
    ]);
    const screen = render();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('This draft is blocked and cannot be approved.');
    expect(alert).toHaveTextContent('It contains 2 claims that your claims library blocks.');
    expect(screen.getByTestId('review-risk')).toHaveTextContent('Blocked');
    expect(screen.getByRole('list', { name: 'Flagged wording' })).toHaveTextContent('“cures” health claim · High risk');
    expect(screen.getByRole('list', { name: 'Review warnings' })).toHaveTextContent('cures everything');
    const nav = screen.getByRole('navigation', { name: 'Draft versions' });
    expect(within(nav).getByText('Blocked')).toBeInTheDocument();
  });

  it('shows a missing required disclaimer as unapprovable', async () => {
    capability(true);
    listDrafts([
      draft(1, {
        review: review({ required_disclaimers: ['Terms apply.'], disclaimers_resolved: false, passed: false }),
      }),
    ]);
    const screen = render();
    expect(await screen.findByRole('alert')).toHaveTextContent(
      'A required disclaimer is missing, so this draft cannot be approved.',
    );
    expect(screen.getByRole('list', { name: 'Required disclaimers' })).toHaveTextContent('Terms apply. (not confirmed)');
  });

  it('lists resolved disclaimers as included', async () => {
    capability(true);
    listDrafts([draft(1, { review: review({ required_disclaimers: ['Terms apply.'] }) })]);
    const screen = render();
    expect(await screen.findByRole('list', { name: 'Required disclaimers' })).toHaveTextContent(
      'Terms apply. (included)',
    );
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('says so when a brief has no drafts yet', async () => {
    capability(true);
    listDrafts([]);
    const screen = render();
    expect(await screen.findByText(/No drafts yet\. Generate one from this brief\./)).toBeInTheDocument();
  });
});

describe('CreativeDraftsPanel — draft editor', () => {
  for (const role of ['viewer', 'reviewer', 'compliance_reviewer']) {
    it(`offers a ${role} no generate control but shows the drafts`, async () => {
      asRole(role);
      capability(true);
      listDrafts([draft(1)]);
      const screen = render();
      await screen.findByRole('navigation', { name: 'Draft versions' });
      expect(screen.queryByRole('group', { name: 'Generate a draft' })).toBeNull();
      expect(screen.queryByRole('button', { name: /Generate draft/ })).toBeNull();
    });
  }

  it('generates a draft through the job and selects the version it created', async () => {
    capability(true);
    let items = [draft(1)];
    const calls = listDrafts(() => items);
    const posts: unknown[] = [];
    server.use(
      http.post(DRAFTS, async ({ request }) => {
        posts.push(await request.json());
        items = [draft(2, { channel: 'sms' }), draft(1)];
        return HttpResponse.json({ job_id: 'job-1' }, { status: 202 });
      }),
      http.get(P(`/workspaces/${WS}/jobs/job-1`), () =>
        HttpResponse.json(job('succeeded', { result_summary: { outcome: 'draft_created', draft_id: 'draft-2' } })),
      ),
    );
    const screen = render();
    await screen.findByRole('navigation', { name: 'Draft versions' });
    await screen.user.selectOptions(screen.getByLabelText('Channel'), 'sms');
    await screen.user.click(screen.getByRole('button', { name: 'Generate draft' }));
    await waitFor(() => expect(screen.getByRole('article', { name: 'Draft version 2' })).toBeInTheDocument());
    expect(posts).toEqual([{ channel: 'sms' }]);
    expect(calls.get).toBeGreaterThanOrEqual(2); // the list was re-read after the job
  });

  it('revises the selected version (a new version with that parent)', async () => {
    capability(true);
    listDrafts([draft(2), draft(1)]);
    const posts: unknown[] = [];
    server.use(
      http.post(DRAFTS, async ({ request }) => {
        posts.push(await request.json());
        return HttpResponse.json({ job_id: 'job-1' }, { status: 202 });
      }),
      http.get(P(`/workspaces/${WS}/jobs/job-1`), () => HttpResponse.json(job('pending'))),
    );
    const screen = render();
    const nav = await screen.findByRole('navigation', { name: 'Draft versions' });
    await screen.user.click(within(nav).getByRole('button', { name: /Version 1/ }));
    await screen.user.click(screen.getByRole('button', { name: 'Revise version 1' }));
    expect(await screen.findByRole('status')).toHaveTextContent('Generating a draft…');
    expect(posts).toEqual([{ channel: 'email', parent_draft_id: 'draft-1' }]);
    expect(screen.getByRole('button', { name: 'Generate draft' })).toBeDisabled();
  });

  it('explains a brief that is not approved', async () => {
    capability(true);
    listDrafts([]);
    server.use(
      http.post(DRAFTS, () =>
        HttpResponse.json(
          { error: { code: 'brief_not_approved', message: 'Approve this brief before generating drafts from it.' } },
          { status: 422 },
        ),
      ),
    );
    const screen = render();
    await screen.findByText(/No drafts yet/);
    await screen.user.click(screen.getByRole('button', { name: 'Generate draft' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Approve this brief before generating drafts from it.');
  });

  it('explains a daily limit at request time', async () => {
    capability(true);
    listDrafts([]);
    server.use(
      http.post(DRAFTS, () =>
        HttpResponse.json(
          { error: { code: 'llm_quota_exceeded', message: 'The daily limit for generated drafts has been reached.' } },
          { status: 429 },
        ),
      ),
    );
    const screen = render();
    await screen.findByText(/No drafts yet/);
    await screen.user.click(screen.getByRole('button', { name: 'Generate draft' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('daily limit');
  });

  it('explains a job that ended without a draft', async () => {
    capability(true);
    listDrafts([]);
    server.use(
      http.post(DRAFTS, () => HttpResponse.json({ job_id: 'job-1' }, { status: 202 })),
      http.get(P(`/workspaces/${WS}/jobs/job-1`), () =>
        HttpResponse.json(job('failed', { last_error_code: 'non_retryable' })),
      ),
    );
    const screen = render();
    await screen.findByText(/No drafts yet/);
    await screen.user.click(screen.getByRole('button', { name: 'Generate draft' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('did not pass the safety screen');
    expect(screen.getByRole('button', { name: 'Generate draft' })).toBeEnabled();
  });
});
