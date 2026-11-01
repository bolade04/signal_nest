import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import type { RecommendationBriefListOut, RecommendationBriefOut } from '@/api/types';
import { briefAuthorName, OpportunityBriefPanel } from '@/pages/opportunities/BriefPanel';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const WS = 'ws-1';
const OPP = 'opp-loc-dallas-0';
const REC = 'rec-opp-loc-dallas-0'; // the default intelligence fixture's record id
const P = (path: string) => `*${API_PREFIX}${path}`;
const BRIEFS = P(`/workspaces/${WS}/opportunities/${OPP}/briefs`);

// Typed against the GENERATED contract: an extra or missing key is a compile error, so the
// fixture cannot drift from what the API returns (RecommendationBriefOut).
function brief(version: number, overrides: Partial<RecommendationBriefOut> = {}): RecommendationBriefOut {
  return {
    id: `brief-${version}`,
    opportunity_id: OPP,
    intelligence_record_id: REC,
    location_id: 'loc-dallas',
    campaign_id: null,
    brief_version: version,
    parent_brief_id: version > 1 ? `brief-${version - 1}` : null,
    analysis_version: '3c',
    scoring_version: '3c.1',
    prompt_name: 'explain_opportunity',
    prompt_version: '1.2.0',
    model_identifier: 'mock-1',
    is_simulated: true,
    created_by: 'user-1',
    created_by_snapshot: 'user-1 Dana Marketer <dana@example.com>',
    created_at: '2026-11-01T09:00:00Z',
    summary: `Summary of version ${version}.`,
    rationale: 'Inference: commuters are choosing on speed.',
    recommended_actions: ['Publish a brand-safe post about faster service.'],
    suggested_angles: ['Speed you can count on'],
    risk_notes: ['Use only approved claims.'],
    evidence_refs: [REC, 'ev-1'],
    ...overrides,
  };
}

function page(items: RecommendationBriefOut[]): RecommendationBriefListOut {
  return { items, total: items.length, limit: 20, offset: 0 };
}

// The panel's gate is the WORKSPACE-EFFECTIVE reflection (GET .../brief-capability). The raw
// global flag in the runtime summary is stubbed alongside it and DELIBERATELY DECOUPLED
// (`globalFlag` defaults to false): every enabled row runs in the honored-override /
// flag-off configuration, so a panel that went back to reading
// `features.recommendation_briefs_enabled` fails here (the P6-UI-005 lesson). All four
// flags are supplied because FeatureFlagsOut requires all four.
function capability(enabled: boolean, globalFlag = false) {
  server.use(
    http.get(P(`/workspaces/${WS}/brief-capability`), () => HttpResponse.json({ enabled })),
    http.get(P('/system/capabilities'), () =>
      HttpResponse.json({
        app_mode: 'local',
        environment: 'development',
        is_local_mode: true,
        all_configured: true,
        features: {
          opportunity_feedback_enabled: false,
          scout_scheduling_enabled: false,
          connector_rss_enabled: false,
          recommendation_briefs_enabled: globalFlag,
        },
      }),
    ),
  );
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
  return renderApp(<OpportunityBriefPanel workspaceId={WS} opportunityId={OPP} />, {
    route: `/opportunities/${OPP}`,
  });
}

describe('OpportunityBriefPanel', () => {
  it('renders nothing and sends no brief request while dark (the shipped default)', async () => {
    let briefRequests = 0;
    let capabilityAnswered = 0;
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.includes('/briefs')) briefRequests += 1; // not the reflection
    });
    server.events.on('response:mocked', ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/brief-capability')) capabilityAnswered += 1;
    });
    const screen = render();
    // The gate has been answered (dark) before absence is asserted.
    await waitFor(() => expect(capabilityAnswered).toBe(1));
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole('heading', { name: /recommendation brief/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/recommendation brief/i)).not.toBeInTheDocument();
    expect(briefRequests).toBe(0);
    server.events.removeAllListeners();
  });

  it('stays hidden when only the raw global flag is on (the workspace resolves disabled)', async () => {
    capability(false, true);
    asRole('owner');
    let briefRequests = 0;
    let capabilityAnswered = 0;
    server.events.on('request:start', ({ request }) => {
      if (new URL(request.url).pathname.includes('/briefs')) briefRequests += 1;
    });
    server.events.on('response:mocked', ({ request }) => {
      if (new URL(request.url).pathname.endsWith('/brief-capability')) capabilityAnswered += 1;
    });
    const screen = render();
    await waitFor(() => expect(capabilityAnswered).toBe(1));
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByText(/recommendation brief/i)).not.toBeInTheDocument();
    expect(briefRequests).toBe(0);
    server.events.removeAllListeners();
  });

  it('hides itself when the list answers the 503 gate (the decision changed since)', async () => {
    capability(true);
    asRole('owner');
    let listed = 0;
    server.use(
      http.get(BRIEFS, () => {
        listed += 1;
        return HttpResponse.json(
          { error: { code: 'capability_unavailable', message: 'Recommendation briefs are not available yet.' } },
          { status: 503 },
        );
      }),
    );
    const screen = render();
    await waitFor(() => expect(listed).toBe(1));
    await waitFor(() =>
      expect(screen.queryByText(/recommendation brief/i)).not.toBeInTheDocument(),
    );
    // Not an error state either: the gate is deterministic, so nothing is shown at all.
    expect(screen.queryByRole('button', { name: /retry/i })).not.toBeInTheDocument();
    expect(listed).toBe(1); // never retried
  });

  it('shows the newest version with its provenance and lets the reader switch versions', async () => {
    capability(true);
    asRole('viewer');
    server.use(http.get(BRIEFS, () => HttpResponse.json(page([brief(2), brief(1)]))));
    const screen = render();

    // The panel is a labelled region (its aria-labelledby is not inert on a plain div).
    expect(await screen.findByRole('region', { name: /recommendation brief/i })).toBeInTheDocument();
    const latest = await screen.findByRole('article', { name: /brief version 2/i });
    expect(within(latest).getByText('Summary of version 2.')).toBeInTheDocument();
    expect(within(latest).getByText('Version 2 of 2')).toBeInTheDocument();
    expect(within(latest).getByText('Simulated')).toBeInTheDocument();
    expect(within(latest).getByText('Publish a brand-safe post about faster service.')).toBeInTheDocument();
    expect(within(latest).getByText('Dana Marketer')).toBeInTheDocument();
    // The snapshot's account id and address are not shown.
    expect(screen.queryByText(/dana@example\.com/)).not.toBeInTheDocument();
    expect(within(latest).getByText('explain_opportunity 1.2.0')).toBeInTheDocument();

    const versions = screen.getByRole('navigation', { name: /brief versions/i });
    await screen.user.click(within(versions).getByRole('button', { name: 'Version 1' }));
    expect(await screen.findByRole('article', { name: /brief version 1/i })).toBeInTheDocument();
    expect(screen.getByText('Summary of version 1.')).toBeInTheDocument();
  });

  it('offers no generate control to a member who cannot capture', async () => {
    capability(true);
    asRole('reviewer');
    server.use(http.get(BRIEFS, () => HttpResponse.json(page([brief(1)]))));
    const screen = render();
    expect(await screen.findByText('Summary of version 1.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /generate/i })).not.toBeInTheDocument();
  });

  it('lets an editor generate the first brief from the bound intelligence record', async () => {
    capability(true);
    asRole('marketer');
    let created = false;
    const posted: unknown[] = [];
    server.use(
      http.get(BRIEFS, () => HttpResponse.json(page(created ? [brief(1)] : []))),
      http.post(BRIEFS, async ({ request }) => {
        posted.push(await request.json());
        created = true;
        return HttpResponse.json(brief(1), { status: 201 });
      }),
    );
    const screen = render();
    expect(await screen.findByText(/no brief has been generated yet/i)).toBeInTheDocument();
    const generate = await screen.findByRole('button', { name: /^generate brief$/i });
    await waitFor(() => expect(generate).toBeEnabled());
    await screen.user.click(generate);

    expect(await screen.findByText('Brief version 1 saved')).toBeInTheDocument();
    expect(posted).toEqual([{ intelligence_record_id: REC }]);
    expect(await screen.findByText('Summary of version 1.')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /generate a new version/i })).toBeInTheDocument();
  });

  it('sends exactly one capture for a double click while it is pending', async () => {
    capability(true);
    asRole('owner');
    let posts = 0;
    let release!: () => void;
    const gate = new Promise<void>((r) => {
      release = r;
    });
    server.use(
      http.get(BRIEFS, () => HttpResponse.json(page([]))),
      http.post(BRIEFS, async () => {
        posts += 1;
        await gate;
        return HttpResponse.json(brief(1), { status: 201 });
      }),
    );
    const screen = render();
    const generate = await screen.findByRole('button', { name: /^generate brief$/i });
    await waitFor(() => expect(generate).toBeEnabled());
    await screen.user.click(generate);
    const pending = await screen.findByRole('button', { name: /generating/i });
    expect(pending).toBeDisabled();
    await screen.user.click(pending);
    release();
    expect(await screen.findByText('Brief version 1 saved')).toBeInTheDocument();
    expect(posts).toBe(1);
  });

  it('reports a refused capture with the server message and stores nothing', async () => {
    capability(true);
    asRole('owner');
    server.use(
      http.get(BRIEFS, () => HttpResponse.json(page([]))),
      http.post(BRIEFS, () =>
        HttpResponse.json(
          {
            error: {
              code: 'brief_withheld',
              message: 'The generated brief did not pass the safety screen, so nothing was stored.',
            },
          },
          { status: 422 },
        ),
      ),
    );
    const screen = render();
    const generate = await screen.findByRole('button', { name: /^generate brief$/i });
    await waitFor(() => expect(generate).toBeEnabled());
    await screen.user.click(generate);
    expect(await screen.findByText('No brief was saved')).toBeInTheDocument();
    expect(screen.getByText(/did not pass the safety screen/i)).toBeInTheDocument();
    expect(screen.getByText(/no brief has been generated yet/i)).toBeInTheDocument();
  });

  it('disables capture when the opportunity has no intelligence record', async () => {
    capability(true);
    asRole('owner');
    server.use(
      http.get(BRIEFS, () => HttpResponse.json(page([]))),
      http.get(P(`/workspaces/${WS}/opportunities/${OPP}/intelligence`), () =>
        HttpResponse.json({ opportunity_id: OPP, intelligence: null }),
      ),
    );
    const screen = render();
    expect(await screen.findByText(/needs this opportunity's signal intelligence/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /^generate brief$/i })).toBeDisabled();
  });
});

describe('briefAuthorName', () => {
  it('shows only the name from the frozen snapshot format', () => {
    expect(briefAuthorName('0123abcd Dana Q. Marketer <dana@example.com>')).toBe('Dana Q. Marketer');
  });

  it('names an erased author without an identity', () => {
    expect(briefAuthorName('[erased]')).toBe('a deleted account');
  });
});
