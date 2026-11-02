import { useIsFetching, useQueryClient } from '@tanstack/react-query';
import { act, waitFor, within } from '@testing-library/react';
import { delay, http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { queryKeys } from '@/api/queryKeys';
import type { ContentReviewOut, CreativeChannel, CreativeDraftOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { CreativeDraftsPanel } from '@/pages/creative/CreativeDraftsPanel';
import { CHANNELS } from '@/pages/creative/creativeLabels';
import { useCreativeCapability } from '@/pages/creative/useCreative';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

// Project Phase 5C-W4 (W21 lane 5CW4): the adversarial verification of W3's surfaces — the
// draft editor, version history, review-warnings panel and blocked state — for every role and
// every capability state, against the API's own rules:
//
// * generating is `require_role(OWNER, ADMIN, MARKETER)` (apps/api/app/creative/routes.py,
//   `EDITORS`); reading drafts is every member; deciding on a draft is the 5B reviewer route,
//   which this panel does not offer (the decision UI is an I-5C follow-up);
// * every draft route answers 503 while `creative_generation` is not enabled for the
//   workspace, so the panel must render nothing and send nothing then.
//
// The UI must never offer a control the API refuses, never send a body the API's strict
// schema refuses, and never render stored text as markup.
//
// Settled state before every role- or capability-dependent assertion (PR #300 CI run
// 41461159943): the panel's inputs arrive from three independent requests — the capability,
// the drafts list and the session (/auth/me, then the organization list) — in any order.
// Asserting presence or absence before all of them have landed races (an absence passes
// vacuously; a presence fails on a slow runner). Every such assertion here first waits for a
// probe rendered in the same providers, reading the same contexts and query cache the panel
// reads, to report the settled state; and the role × state matrix runs in both response
// orders (the session first, and the session last).

const WS = 'ws-1';
const BRIEF = 'brief-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const DRAFTS = P(`/workspaces/${WS}/briefs/${BRIEF}/drafts`);
const CAPABILITY = P('/workspaces/:workspaceId/creative-capability');

/** The roles the generate route admits (apps/api/app/creative/routes.py `EDITORS`). */
const API_GENERATORS = new Set(['owner', 'admin', 'marketer']);
const ROLES = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];

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
    created_by: 'user-2',
    created_by_snapshot: 'user-2 Writer <writer@example.com>',
    created_at: '2026-11-01T10:00:00Z',
    review: review({ id: `review-${version}`, draft_id: id, draft_version: version }),
    ...overrides,
  };
}

type Memberships = { organization_id: string; role: string }[];

/** The session; ``delayMs`` makes it land after the capability and the drafts list. */
function session(memberships: Memberships, delayMs = 0) {
  server.use(
    http.get(P('/auth/me'), async () => {
      if (delayMs) await delay(delayMs);
      return HttpResponse.json({
        access_token: 'test-token',
        token_type: 'bearer',
        user: { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo', is_operator: false },
        memberships,
      });
    }),
  );
}

/** Which response lands last: the session (/auth/me) or the capability reflection. */
type Order = 'session-last' | 'capability-last';
const ORDERS: Order[] = ['session-last', 'capability-last'];
const LATE_MS = 150;

type CapabilityState = 'enabled' | 'disabled' | 'error' | 'forbidden' | 'pending' | 'list503';

/** The backend for one capability state; returns its request log. */
function backend(
  state: CapabilityState,
  items: CreativeDraftOut[] = [draft(2, { parent_draft_id: 'draft-1' }), draft(1)],
  capabilityDelayMs = 0,
) {
  const log = { capability: 0, list: 0, post: 0, posts: [] as unknown[] };
  server.use(
    http.get(CAPABILITY, async () => {
      log.capability += 1;
      if (capabilityDelayMs) await delay(capabilityDelayMs);
      if (state === 'pending') {
        await delay('infinite');
      }
      if (state === 'error') return HttpResponse.json({ error: { code: 'error', message: 'down' } }, { status: 500 });
      if (state === 'forbidden') {
        return HttpResponse.json(
          { error: { code: 'permission_denied', message: 'You are not a member of this organization.' } },
          { status: 403 },
        );
      }
      return HttpResponse.json({ enabled: state === 'enabled' || state === 'list503' });
    }),
    http.get(DRAFTS, () => {
      log.list += 1;
      if (state === 'list503') {
        return HttpResponse.json(
          { error: { code: 'capability_unavailable', message: 'Creative generation is not available yet.' } },
          { status: 503 },
        );
      }
      return HttpResponse.json({ items, total: items.length, limit: 100, offset: 0 });
    }),
    http.post(DRAFTS, async ({ request }) => {
      log.post += 1;
      log.posts.push(await request.json());
      await delay(50);
      return HttpResponse.json({ job_id: 'job-1' }, { status: 202 });
    }),
    http.get(P(`/workspaces/${WS}/jobs/job-1`), () =>
      HttpResponse.json({
        id: 'job-1',
        job_type: 'creative.generate',
        status: 'pending',
        scout_request_id: null,
        location_id: null,
        attempt_count: 0,
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
      }),
    ),
  );
  return log;
}

/**
 * Reports what the panel's decision reads, from the same providers and query cache: the
 * session, the active organization, the role there (the panel's own lookup), the capability
 * reflection (the same query, shared) and the drafts list's state (read from the cache, never
 * fetched here). Test-only; renders plain text, no role.
 */
function Probe() {
  const { status, memberships } = useAuth();
  const { organizationId } = useWorkspace();
  const capability = useCreativeCapability(WS);
  const fetchingDrafts = useIsFetching({ queryKey: queryKeys.briefDrafts(WS, BRIEF) });
  const drafts = useQueryClient().getQueryState(queryKeys.briefDrafts(WS, BRIEF));
  const role = memberships.find((m) => m.organization_id === organizationId)?.role ?? 'none';
  const draftsState = fetchingDrafts > 0 ? 'fetching' : (drafts?.status ?? 'idle');
  return (
    <span data-testid="probe">
      {`session=${status} org=${organizationId ?? '-'} role=${role} capability=${capability.status} drafts=${draftsState}`}
    </span>
  );
}

function render() {
  return renderApp(
    <>
      <CreativeDraftsPanel workspaceId={WS} briefId={BRIEF} />
      <Probe />
    </>,
  );
}

const settle = () => act(() => new Promise<void>((r) => setTimeout(r, 80)));

/**
 * Wait until every input of the panel's decision has landed and been rendered: the session,
 * the active organization (org-1) and the role there, the capability reflection, and the
 * drafts list's final state. Then one more tick, so the assertions that follow read the
 * committed DOM of that state — an absence after this is evidence, not a race.
 */
async function loaded(
  screen: ReturnType<typeof render>,
  role: string,
  capability: 'enabled' | 'disabled' | 'loading',
  drafts: 'success' | 'error' | 'idle',
) {
  await waitFor(
    () =>
      expect(screen.getByTestId('probe')).toHaveTextContent(
        `session=authenticated org=org-1 role=${role} capability=${capability} drafts=${drafts}`,
      ),
    { timeout: 5000 },
  );
  await settle();
}

function decisionControls(screen: ReturnType<typeof render>) {
  return screen.queryAllByRole('button', { name: /approve|reject|revoke|decide/i });
}

const DARK_STATES = ['disabled', 'error', 'forbidden', 'pending', 'list503'] as const;
/** The capability and drafts state each case settles in (what `loaded` waits for). */
const SETTLED: Record<CapabilityState, ['enabled' | 'disabled' | 'loading', 'success' | 'error' | 'idle']> = {
  enabled: ['enabled', 'success'],
  disabled: ['disabled', 'idle'],
  error: ['disabled', 'idle'],
  forbidden: ['disabled', 'idle'],
  pending: ['loading', 'idle'],
  list503: ['enabled', 'error'],
};

function arrange(role: string, state: CapabilityState, order: Order) {
  session([{ organization_id: 'org-1', role }], order === 'session-last' ? LATE_MS : 0);
  return backend(state, undefined, order === 'capability-last' ? LATE_MS : 0);
}

describe('5C-W4 — the UI never offers a control the API refuses (role × capability state)', () => {
  for (const order of ORDERS) {
    for (const role of [...ROLES, 'superuser']) {
      it(`${role}, ${order}: enabled — drafts for every member, the editor only where the API generates`, async () => {
        const log = arrange(role, 'enabled', order);
        const screen = render();
        await loaded(screen, role, 'enabled', 'success');
        const nav = screen.getByRole('navigation', { name: 'Draft versions' });
        expect(within(nav).getAllByRole('button')).toHaveLength(2);
        const generates = API_GENERATORS.has(role);
        expect(screen.queryByRole('group', { name: 'Generate a draft' }) !== null).toBe(generates);
        expect(screen.queryByRole('button', { name: 'Generate draft' }) !== null).toBe(generates);
        expect(screen.queryByRole('button', { name: /Revise version/ }) !== null).toBe(generates);
        expect(decisionControls(screen)).toEqual([]);
        expect(log.post).toBe(0);
      });

      for (const state of DARK_STATES) {
        it(`${role}, ${order}: ${state} — renders nothing and sends no draft request`, async () => {
          const log = arrange(role, state, order);
          const screen = render();
          const [capability, drafts] = SETTLED[state];
          await loaded(screen, role, capability, drafts);
          expect(log.capability).toBeGreaterThanOrEqual(1);
          expect(screen.queryByRole('heading', { name: 'Drafts' })).toBeNull();
          expect(screen.queryByRole('group', { name: 'Generate a draft' })).toBeNull();
          expect(screen.queryAllByRole('button')).toEqual([]);
          expect(log.post).toBe(0);
          if (state === 'list503') expect(log.list).toBeGreaterThanOrEqual(1);
          else expect(log.list).toBe(0);
        });
      }
    }
  }

  for (const order of ORDERS) {
    it(`reads the role of the ACTIVE organization, not of another membership (${order})`, async () => {
      // An owner elsewhere and a viewer here: the API refuses this member's generate request.
      session(
        [
          { organization_id: 'org-2', role: 'owner' },
          { organization_id: 'org-1', role: 'viewer' },
        ],
        order === 'session-last' ? LATE_MS : 0,
      );
      backend('enabled', undefined, order === 'capability-last' ? LATE_MS : 0);
      const screen = render();
      await loaded(screen, 'viewer', 'enabled', 'success');
      expect(screen.getByRole('navigation', { name: 'Draft versions' })).toBeInTheDocument();
      expect(screen.queryByRole('group', { name: 'Generate a draft' })).toBeNull();
    });

    it(`offers the editor to a marketer here who is only a viewer elsewhere (${order})`, async () => {
      session(
        [
          { organization_id: 'org-2', role: 'viewer' },
          { organization_id: 'org-1', role: 'marketer' },
        ],
        order === 'session-last' ? LATE_MS : 0,
      );
      backend('enabled', undefined, order === 'capability-last' ? LATE_MS : 0);
      const screen = render();
      await loaded(screen, 'marketer', 'enabled', 'success');
      expect(screen.getByRole('group', { name: 'Generate a draft' })).toBeInTheDocument();
    });

    it(`offers nothing to a member with no membership in the active organization (${order})`, async () => {
      session([{ organization_id: 'org-2', role: 'owner' }], order === 'session-last' ? LATE_MS : 0);
      backend('enabled', undefined, order === 'capability-last' ? LATE_MS : 0);
      const screen = render();
      await loaded(screen, 'none', 'enabled', 'success');
      expect(screen.getByRole('navigation', { name: 'Draft versions' })).toBeInTheDocument();
      expect(screen.queryByRole('group', { name: 'Generate a draft' })).toBeNull();
    });
  }
});

describe('5C-W4 — the draft editor sends only what the strict API body accepts', () => {
  it('a double click sends one request (one request, one job)', async () => {
    session([{ organization_id: 'org-1', role: 'marketer' }]);
    const log = backend('enabled');
    const screen = render();
    const button = await screen.findByRole('button', { name: 'Generate draft' });
    await screen.user.dblClick(button);
    await screen.user.click(button);
    await waitFor(() => expect(log.post).toBe(1));
    await settle();
    expect(log.post).toBe(1);
    expect(screen.getByRole('button', { name: 'Generate draft' })).toBeDisabled();
    expect(screen.getByRole('button', { name: /Revise version/ })).toBeDisabled();
    expect(screen.getByLabelText('Channel')).toBeDisabled();
  });

  for (const channel of CHANNELS) {
    it(`sends exactly { channel } for ${channel}, and the parent only when revising`, async () => {
      session([{ organization_id: 'org-1', role: 'owner' }]);
      const log = backend('enabled');
      const screen = render();
      await loaded(screen, 'owner', 'enabled', 'success');
      await screen.user.selectOptions(screen.getByLabelText('Channel'), channel);
      await screen.user.click(screen.getByRole('button', { name: 'Generate draft' }));
      await waitFor(() => expect(log.post).toBe(1));
      expect(log.posts[0]).toEqual({ channel });
    });
  }

  it('revises exactly the selected version', async () => {
    session([{ organization_id: 'org-1', role: 'admin' }]);
    const log = backend('enabled');
    const screen = render();
    await loaded(screen, 'admin', 'enabled', 'success');
    const nav = screen.getByRole('navigation', { name: 'Draft versions' });
    await screen.user.click(within(nav).getByRole('button', { name: /Version 1/ }));
    await screen.user.click(screen.getByRole('button', { name: 'Revise version 1' }));
    await waitFor(() => expect(log.post).toBe(1));
    expect(log.posts[0]).toEqual({ channel: 'email', parent_draft_id: 'draft-1' });
    for (const body of log.posts as Record<string, unknown>[]) {
      expect(Object.keys(body).every((k) => k === 'channel' || k === 'parent_draft_id')).toBe(true);
      expect(CHANNELS).toContain(body.channel as CreativeChannel);
    }
  });

  it('shows static copy for a refusal, never the server message', async () => {
    session([{ organization_id: 'org-1', role: 'marketer' }]);
    backend('enabled');
    server.use(
      http.post(DRAFTS, () =>
        HttpResponse.json(
          { error: { code: 'permission_denied', message: 'internal detail: role table v7' } },
          { status: 403 },
        ),
      ),
    );
    const screen = render();
    await screen.user.click(await screen.findByRole('button', { name: 'Generate draft' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('The draft could not be requested. Try again.');
    expect(screen.queryByText(/internal detail/)).toBeNull();
  });

  it('says a foreign or vanished brief or parent is no longer available (the API 404)', async () => {
    session([{ organization_id: 'org-1', role: 'marketer' }]);
    backend('enabled');
    server.use(
      http.post(DRAFTS, () =>
        HttpResponse.json({ error: { code: 'not_found', message: 'Draft not found in this workspace.' } }, { status: 404 }),
      ),
    );
    const screen = render();
    await screen.user.click(await screen.findByRole('button', { name: 'Generate draft' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('That draft or brief is no longer available.');
  });
});

describe('5C-W4 — version history, review panel and blocked state', () => {
  it('never claims a revision of a version that is not on the page', async () => {
    session([{ organization_id: 'org-1', role: 'viewer' }]);
    backend('enabled', [draft(7, { parent_draft_id: 'draft-elsewhere' }), draft(6, { parent_draft_id: 'draft-5' })]);
    const screen = render();
    await loaded(screen, 'viewer', 'enabled', 'success');
    const nav = screen.getByRole('navigation', { name: 'Draft versions' });
    const labels = within(nav).getAllByRole('button').map((b) => b.textContent ?? '');
    expect(labels[0]).not.toMatch(/revises/);
    expect(labels[0]).not.toMatch(/undefined/);
    expect(labels[1]).not.toMatch(/revises/);
  });

  it('names a single blocked claim in the singular', async () => {
    session([{ organization_id: 'org-1', role: 'reviewer' }]);
    backend('enabled', [
      draft(1, { review: review({ risk_level: 'blocked', is_blocked: true, passed: false, blocked_claim_ids: ['c1'] }) }),
    ]);
    const screen = render();
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('It contains a claim that your claims library blocks.');
  });

  it('blocks on named claims even when the stored flag says otherwise, for every role', async () => {
    for (const role of ROLES) {
      session([{ organization_id: 'org-1', role }]);
      backend('enabled', [draft(1, { review: review({ blocked_claim_ids: ['c1'], passed: false, risk_level: 'high' }) })]);
      const screen = render();
      expect(await screen.findByRole('alert')).toHaveTextContent('This draft is blocked and cannot be approved.');
      screen.unmount();
    }
  });

  it('explains a blocked-risk draft with no library hit', async () => {
    session([{ organization_id: 'org-1', role: 'viewer' }]);
    backend('enabled', [draft(1, { review: review({ risk_level: 'blocked', is_blocked: true, passed: false }) })]);
    const screen = render();
    expect(await screen.findByRole('alert')).toHaveTextContent('It contains wording the content review blocks.');
  });

  it('shows a draft with no review as pending, never as passed or blocked', async () => {
    session([{ organization_id: 'org-1', role: 'viewer' }]);
    backend('enabled', [draft(1, { review: null })]);
    const screen = render();
    await loaded(screen, 'viewer', 'enabled', 'success');
    const nav = screen.getByRole('navigation', { name: 'Draft versions' });
    expect(within(nav).getByText('Review pending')).toBeInTheDocument();
    expect(screen.getByText('The content review is not available yet.')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('region', { name: 'Content review' })).toBeNull();
  });

  it('marks every required disclaimer unconfirmed when the review did not resolve them', async () => {
    session([{ organization_id: 'org-1', role: 'viewer' }]);
    backend('enabled', [
      draft(1, {
        review: review({
          required_disclaimers: ['Terms apply.', 'Offer ends Sunday.'],
          disclaimers_resolved: false,
          passed: false,
        }),
      }),
    ]);
    const screen = render();
    const list = await screen.findByRole('list', { name: 'Required disclaimers' });
    expect(within(list).getAllByRole('listitem').map((li) => li.textContent)).toEqual([
      'Terms apply. (not confirmed)',
      'Offer ends Sunday. (not confirmed)',
    ]);
  });

  it('renders stored text and review evidence as text, never as markup', async () => {
    session([{ organization_id: 'org-1', role: 'viewer' }]);
    const payload = '<img src=x onerror="window.__pwned=1">';
    backend('enabled', [
      draft(1, {
        headline: payload,
        body: `Body ${payload}`,
        call_to_action: payload,
        review: review({
          risk_level: 'medium',
          findings: [{ category: 'superlative_best', risk: 'medium', excerpt: payload }],
          warnings: [`Detected ${payload}`],
          required_disclaimers: [payload],
        }),
      }),
    ]);
    const screen = render();
    await loaded(screen, 'viewer', 'enabled', 'success');
    expect(screen.getByRole('article', { name: 'Draft version 1' })).toBeInTheDocument();
    expect(screen.container.querySelector('img')).toBeNull();
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    expect(screen.getAllByText(new RegExp(payload.replace(/[.*+?^${}()|[\]\\]/g, '\\$&'))).length).toBeGreaterThan(0);
  });
});
