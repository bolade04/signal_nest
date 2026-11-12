import { within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { useLocation } from 'react-router-dom';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { setAuthToken } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import * as api from '@/api/endpoints';
import { navItems } from '@/components/layout/nav';
import { CAMPAIGNS_PATH, campaignPath } from '@/pages/campaigns/campaignVocabulary';
import { contextModel, orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * The I-5D integration cycle (W21 lane 5D-INTEG): `pages/campaigns/` (lane 5D-W3, tested by
 * 5D-W4 in `pages/campaigns/__tests__/`) mounted at `CAMPAIGNS_PATH` and `campaignPath(id)`
 * with its sidebar entry, as the Phase 5 plan §6.3 requires of the cycle that merges a page
 * directory. The campaigns router (5D-W1) is live and no flag gates it, so the entry and the
 * routes are there for every member; who may edit is the role matrix's
 * (`role-control-matrix.test.tsx` C12-C14).
 */

const ROLES: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];
const CAMPAIGN_ID = 'cmp-i5d-1';
const CAMPAIGN_NAME = 'I-5D spring launch';

function LocationProbe() {
  const location = useLocation();
  return <output data-testid="router-location">{location.pathname}</output>;
}

const app = (
  <>
    <App />
    <LocationProbe />
  </>
);

beforeEach(() => {
  orgModel.reset();
  contextModel.reset();
  contextModel.seed('campaigns', CAMPAIGN_ID, { name: CAMPAIGN_NAME, mode: 'grouped', status: 'paused' });
});

describe('I-5D: the campaign pages are mounted', () => {
  it('declares one sidebar entry for exactly the path the pages link to', () => {
    expect(navItems.filter((n) => n.to === CAMPAIGNS_PATH).map((n) => n.label)).toEqual(['Campaigns']);
    expect(campaignPath(CAMPAIGN_ID)).toBe(`${CAMPAIGNS_PATH}/${CAMPAIGN_ID}`);
  });

  it.each(ROLES)('%s: the sidebar entry opens the list, and a campaign opens its own page', async (role) => {
    orgModel.setMembership('org-1', 'user-1', role);
    const screen = renderApp(app, { route: '/' });
    await screen.findByRole('heading', { level: 1, name: 'Overview' });

    const nav = screen.getByRole('navigation', { name: 'Primary' });
    await screen.user.click(within(nav).getByRole('link', { name: 'Campaigns' }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Campaigns' })).toBeInTheDocument();
    expect(screen.getByTestId('router-location').textContent).toBe(CAMPAIGNS_PATH);
    const crumbs = screen.getByRole('navigation', { name: 'Breadcrumb' });
    expect(within(crumbs).getByText('Campaigns')).toBeInTheDocument();

    await screen.user.click(await screen.findByRole('link', { name: CAMPAIGN_NAME }));
    expect(await screen.findByRole('heading', { level: 1, name: CAMPAIGN_NAME })).toBeInTheDocument();
    expect(screen.getByTestId('router-location').textContent).toBe(campaignPath(CAMPAIGN_ID));
    expect(within(screen.getByRole('navigation', { name: 'Primary' })).getByRole('link', { name: 'Campaigns' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('a campaign of no workspace of this member is the page’s not-found, inside the shell', async () => {
    const screen = renderApp(app, { route: campaignPath('cmp-not-here') });
    expect(await screen.findByText(/not in this workspace/i)).toBeInTheDocument();
    expect(screen.getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
  });
});

// The demo user that renderApp signs in is a platform operator (src/test/handlers.ts). Every
// customer is not, so the live entry is also judged for a member who is not an operator, in
// every organization role (W21-REV-5D-INTEG r1 C-1; its mutant RV-W2 made the entry
// operator-only and no other test noticed).
describe('I-5D: the Campaigns entry for a member who is not a platform operator', () => {
  it.each(ROLES)('%s (not an operator): the Campaigns entry is shown and opens the page', async (role) => {
    server.use(
      http.get(`*${API_PREFIX}/auth/me`, () =>
        HttpResponse.json({
          access_token: 'test-token',
          token_type: 'bearer',
          user: {
            id: 'user-2',
            email: 'customer@signalnest.dev',
            full_name: 'Customer',
            is_operator: false,
            email_verified: true,
          },
          memberships: [{ organization_id: 'org-1', role }],
        }),
      ),
    );
    const screen = renderApp(app, { route: '/' });
    await screen.findByRole('heading', { level: 1, name: 'Overview' });
    const nav = screen.getByRole('navigation', { name: 'Primary' });
    // Liveness: this session really is not an operator (the operator-only entry is absent).
    expect(within(nav).queryByRole('link', { name: 'Operations' })).toBeNull();
    await screen.user.click(within(nav).getByRole('link', { name: 'Campaigns' }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Campaigns' })).toBeInTheDocument();
    expect(screen.getByTestId('router-location').textContent).toBe(CAMPAIGNS_PATH);
  });
});

describe('I-5D: the Campaign Context page says of campaigns only what is true', () => {
  it('its Campaigns tab no longer claims that the mode sets how locations are treated', async () => {
    contextModel.reset(); // an empty tab: its blurb shows in the header and the empty state
    const screen = renderApp(app, { route: '/context' });
    await screen.user.click(await screen.findByRole('tab', { name: /^campaigns$/i }));
    // Liveness first, so an absence below cannot mean "nothing rendered".
    expect((await screen.findAllByText(/scouting does not use them yet/i)).length).toBeGreaterThan(0);
    const text = document.body.textContent ?? '';
    expect(text).toContain('Campaign contexts scouts can attach to.');
    // Nothing in scouting or scoring reads a campaign's mode (W21-REV-5DW3 r2 §10).
    expect(text).not.toContain('Mode sets how locations are treated');
  });
});

describe('I-5D: the shared test mock answers campaigns as the dedicated router does', () => {
  it('projects CampaignOut on the list and on GET one: no organization_id or workspace_id', async () => {
    const [listed] = await api.listCampaigns('ws-1');
    const one = await api.getCampaign('ws-1', CAMPAIGN_ID);
    for (const answer of [listed, one]) {
      expect(answer).toMatchObject({ id: CAMPAIGN_ID, name: CAMPAIGN_NAME, mode: 'grouped', status: 'paused' });
      expect(answer).not.toHaveProperty('organization_id');
      expect(answer).not.toHaveProperty('workspace_id');
    }
  });

  it('PATCH replaces the membership with locations of this workspace only, in location-id order', async () => {
    setAuthToken('test-token'); // the demo owner, as renderApp signs in
    const updated = await api.updateCampaign('ws-1', CAMPAIGN_ID, {
      location_ids: ['loc-london', 'loc-dallas', 'loc-london'],
    });
    expect(updated.location_ids).toEqual(['loc-dallas', 'loc-london']);
    expect(updated).not.toHaveProperty('workspace_id');
    // A location of no workspace of this campaign: the router's 404, and nothing changes.
    await expect(api.updateCampaign('ws-1', CAMPAIGN_ID, { location_ids: ['loc-elsewhere'] })).rejects.toMatchObject({
      status: 404,
    });
    expect((await api.getCampaign('ws-1', CAMPAIGN_ID)).location_ids).toEqual(['loc-dallas', 'loc-london']);
    setAuthToken(null);
  });
});
