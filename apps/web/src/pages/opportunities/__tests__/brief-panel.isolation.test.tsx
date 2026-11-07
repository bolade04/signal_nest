// Project Phase 5A, 5A-W4 UI verification of the brief panel: the fail-closed capability read,
// the panel inside the real OpportunityDetail page, no cross-opportunity bleed, and an author
// display that never leaks an address or account id. Adopted as written from the adversarial
// battery of review W21-REV-P5A-FEATURE r1 (condition C4; brief-panel.rev-adversarial.test.tsx,
// sha256 9129391a...47ce).
import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type { RecommendationBriefListOut, RecommendationBriefOut } from '@/api/types';
import { briefAuthorName, OpportunityBriefPanel } from '@/pages/opportunities/BriefPanel';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const WS = 'ws-1';
const OPP = 'opp-loc-dallas-0';
const OPP2 = 'opp-loc-dallas-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const briefsUrl = (opp: string) => P(`/workspaces/${WS}/opportunities/${opp}/briefs`);

function brief(opp: string, version: number, o: Partial<RecommendationBriefOut> = {}): RecommendationBriefOut {
  return {
    id: `${opp}-brief-${version}`,
    opportunity_id: opp,
    intelligence_record_id: `rec-${opp}`,
    location_id: null,
    campaign_id: null,
    brief_version: version,
    parent_brief_id: null,
    analysis_version: '3c',
    scoring_version: '3c.1',
    prompt_name: 'explain_opportunity',
    prompt_version: '1.2.0',
    model_identifier: 'mock-1',
    is_simulated: true,
    created_by: 'u-9',
    created_by_snapshot: 'u-9 Riley Author <riley.author@example.com>',
    created_at: '2026-11-01T09:00:00Z',
    summary: `SUMMARY ${opp} v${version}`,
    rationale: 'Inference text.',
    recommended_actions: [],
    suggested_angles: [],
    risk_notes: [],
    evidence_refs: [],
    ...o,
  };
}
function Switcher() {
  const [opp, setOpp] = useState(OPP);
  return (
    <div>
      <button type="button" onClick={() => setOpp(OPP2)}>
        switch-opportunity
      </button>
      <OpportunityBriefPanel workspaceId={WS} opportunityId={opp} />
    </div>
  );
}

const page = (items: RecommendationBriefOut[]): RecommendationBriefListOut => ({
  items,
  total: items.length,
  limit: 20,
  offset: 0,
});

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

function countBriefRequests() {
  const seen = { briefs: 0, capability: 0 };
  server.events.on('request:start', ({ request }) => {
    const path = new URL(request.url).pathname;
    if (path.includes('/briefs')) seen.briefs += 1;
  });
  server.events.on('response:mocked', ({ request }) => {
    if (new URL(request.url).pathname.endsWith('/brief-capability')) seen.capability += 1;
  });
  return seen;
}

describe('5A-W4: the panel inside the real OpportunityDetail page', () => {
  it('is absent from the page in the shipped (dark) configuration and sends no brief request', async () => {
    const seen = countBriefRequests();
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    await screen.findByRole('heading', { name: /Dallas customers want faster delivery 1/i });
    await waitFor(() => expect(seen.capability).toBeGreaterThanOrEqual(1));
    await new Promise((r) => setTimeout(r, 50));
    expect(screen.queryByRole('heading', { name: /recommendation brief/i })).toBeNull();
    expect(seen.briefs).toBe(0);
    server.events.removeAllListeners();
  });

  it('mounts in the page when the workspace resolves enabled, read-only for a viewer', async () => {
    asRole('viewer');
    server.use(
      http.get(P(`/workspaces/${WS}/brief-capability`), () => HttpResponse.json({ enabled: true })),
      http.get(briefsUrl(OPP), () => HttpResponse.json(page([brief(OPP, 1)]))),
    );
    const screen = renderApp(<App />, { route: `/opportunities/${OPP}` });
    expect(await screen.findByRole('heading', { name: /recommendation brief/i })).toBeInTheDocument();
    expect(await screen.findByText(`SUMMARY ${OPP} v1`)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /generate/i })).toBeNull();
    // Name only: no address and no account id anywhere in the page.
    expect(document.body.textContent).not.toContain('riley.author@example.com');
    expect(document.body.textContent).not.toContain('u-9 ');
  });
});

describe('5A-W4: the capability gate fails closed', () => {
  for (const [label, handler] of [
    ['a 500', () => HttpResponse.json({ error: { code: 'internal_error', message: 'x' } }, { status: 500 })],
    ['a 403 (non-member)', () => HttpResponse.json({ error: { code: 'permission_denied', message: 'x' } }, { status: 403 })],
    ['a non-boolean enabled', () => HttpResponse.json({ enabled: 'true' })],
    ['an empty object', () => HttpResponse.json({})],
  ] as const) {
    it(`hides the panel and sends no brief request on ${label}`, async () => {
      asRole('owner');
      server.use(http.get(P(`/workspaces/${WS}/brief-capability`), handler));
      const seen = countBriefRequests();
      const screen = renderApp(<OpportunityBriefPanel workspaceId={WS} opportunityId={OPP} />);
      await waitFor(() => expect(seen.capability).toBeGreaterThanOrEqual(1));
      await new Promise((r) => setTimeout(r, 50));
      expect(screen.queryByText(/recommendation brief/i)).toBeNull();
      expect(seen.briefs).toBe(0);
      server.events.removeAllListeners();
    });
  }
});

describe('5A-W4: no cross-opportunity bleed', () => {
  it('never shows one opportunity’s briefs under another, and a selected version does not carry over', async () => {
    asRole('owner');
    let release!: () => void;
    const slow = new Promise<void>((r) => {
      release = r;
    });
    server.use(
      http.get(P(`/workspaces/${WS}/brief-capability`), () => HttpResponse.json({ enabled: true })),
      http.get(briefsUrl(OPP), () => HttpResponse.json(page([brief(OPP, 2), brief(OPP, 1)]))),
      http.get(briefsUrl(OPP2), async () => {
        await slow;
        return HttpResponse.json(page([brief(OPP2, 1)]));
      }),
    );
    const screen = renderApp(<Switcher />);
    expect(await screen.findByText(`SUMMARY ${OPP} v2`)).toBeInTheDocument();
    const versions = screen.getByRole('navigation', { name: /brief versions/i });
    await screen.user.click(within(versions).getByRole('button', { name: 'Version 1' }));
    expect(await screen.findByText(`SUMMARY ${OPP} v1`)).toBeInTheDocument();

    await screen.user.click(screen.getByRole('button', { name: 'switch-opportunity' }));
    // While OPP2 is loading nothing of OPP may be shown.
    await waitFor(() => expect(screen.queryByText(new RegExp(`SUMMARY ${OPP} v`))).toBeNull());
    release();
    expect(await screen.findByText(`SUMMARY ${OPP2} v1`)).toBeInTheDocument();
    expect(screen.queryByText(new RegExp(`SUMMARY ${OPP} v`))).toBeNull();
  });
});

describe('5A-W4: author display never leaks an address or id', () => {
  it.each([
    ['0123abcd Dana Marketer <dana@example.com>', 'Dana Marketer'],
    ['abc  Two  Spaces  <x@y.z>', 'Two  Spaces'],
    ['abc <x@y.z>', 'Unknown author'],
    ['abc Name <first.last+tag@sub.example.co.uk>', 'Name'],
    ['[erased]', 'a deleted account'],
  ])('%s -> %s', (snapshot, expected) => {
    const shown = briefAuthorName(snapshot);
    expect(shown).toBe(expected);
    expect(shown).not.toContain('@');
  });
});
