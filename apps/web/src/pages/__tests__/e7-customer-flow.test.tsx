import { waitFor, within, type RenderResult } from '@testing-library/react';
import type userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { useAuth } from '@/auth/AuthContext';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderProductionApp } from '@/test/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

/**
 * §6 / §7 / §46 / §47 / §87 / §88 — the P6-AUTH-1 customer flow, end to end,
 * through the UI only.
 *
 * An OWNER signs in, invites a second person from Settings, and the one-time link
 * the page shows is what the second person opens — in a fresh browser (unmount,
 * storage cleared) against the same stateful backend model. No test here inserts
 * the second membership or any AuthContext state: the model REFUSES a fixture
 * membership for an invited address (orgModel.setMembership), so the membership
 * asserted below can only have come from the register / accept flow, and the model
 * stamps it `source: 'invitation'`.
 *
 * Black-box on purpose: this file imports only modules that exist on the pre-6B-3B
 * base (5cb44bf), so the §86 counterfactual can run it against the genuine old UI
 * and fail on substance (no invite UI, no /invite route) rather than on imports.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const ORIGIN = 'http://localhost:3000';
type Screen = RenderResult & { user: ReturnType<typeof userEvent.setup> };

function SessionProbe() {
  const { status, memberships } = useAuth();
  const { organizationId } = useWorkspace();
  const role = memberships.find((m) => m.organization_id === organizationId)?.role ?? '';
  const all = memberships.map((m) => `${m.organization_id}=${m.role}`).sort().join(',');
  return (
    <>
      <span data-testid="m4-active">{`${status}|${organizationId ?? ''}|${role}`}</span>
      <span data-testid="m4-memberships">{all}</span>
    </>
  );
}

async function expectActive(screen: Screen, orgId: string, role: string) {
  await waitFor(() => expect(screen.getByTestId('m4-active')).toHaveTextContent(`authenticated|${orgId}|${role}`));
  // Selected by the app and persisted, not merely rendered for a moment.
  expect(localStorage.getItem('signalnest-active-org')).toBe(orgId);
}

async function navigate(screen: Screen, name: RegExp) {
  const [link] = screen.getAllByRole('link', { name });
  await screen.user.click(link!);
}

/** §87 steps 1–4: the OWNER signs in, opens Settings, invites, and receives the link. */
async function ownerInvites(email: string, roleLabel: string): Promise<{ owner: Screen; link: string; token: string }> {
  const owner = renderProductionApp(<App />, '/settings', { probe: <SessionProbe /> });
  // 1. OWNER logs in through the real sign-in form.
  await owner.user.type(await owner.findByLabelText(/^email/i), 'demo@signalnest.dev');
  await owner.user.type(owner.getByLabelText(/^password/i), 'demo1234');
  await owner.user.click(owner.getByRole('button', { name: /^sign in$/i }));
  // 2. Settings, as the owner of Demo Org.
  await waitFor(() => expect(window.location.pathname).toBe('/settings'));
  await expectActive(owner, 'org-1', 'owner');
  // 3. Invite a non-OWNER role.
  await owner.user.click(await owner.findByRole('button', { name: /invite member/i }));
  const dialog = await owner.findByRole('dialog', { name: /invite member/i });
  await owner.user.type(within(dialog).getByLabelText(/^email/i), email);
  await owner.user.click(within(dialog).getByRole('combobox'));
  await owner.user.click(within(await owner.findByRole('listbox')).getByRole('option', { name: roleLabel }));
  await owner.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
  // 4. The one-time manual link, exactly as the backend's token makes it.
  const result = await owner.findByRole('dialog', { name: /invitation created/i });
  expect(result).toHaveTextContent(/no email was sent/i);
  const link = (within(result).getByLabelText(/invitation link/i) as HTMLInputElement).value;
  const issued = orgModel.invitations().filter((i) => i.email === email);
  expect(issued).toHaveLength(1);
  expect(link).toBe(`${ORIGIN}/invite#token=${issued[0]!.token}`);
  await owner.user.click(within(result).getByRole('button', { name: /copy invitation link/i }));
  expect(await navigator.clipboard.readText()).toBe(link);
  await owner.user.click(within(result).getByRole('button', { name: /^done$/i }));
  return { owner, link, token: issued[0]!.token };
}

/** The address the second person pastes into their own browser. */
const pathOf = (link: string) => link.slice(ORIGIN.length);

const inviteeMembership = (email: string, orgId: string) => {
  const user = orgModel.users().find((u) => u.email === email);
  return orgModel.memberships().find((m) => m.user_id === user?.id && m.organization_id === orgId);
};

beforeEach(() => {
  orgModel.reset();
});

afterEach(() => {
  expect(orgModel.violations()).toEqual([]);
  window.history.replaceState(null, '', '/');
});

/** An invitation created over HTTP as the owner (the admin UI is not under test here). */
async function inviteOverHttp(email: string, role: string): Promise<string> {
  const response = await fetch(`${ORIGIN}${API_PREFIX}/organizations/org-1/invitations`, {
    method: 'POST',
    headers: { Authorization: 'Bearer test-token', 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, role }),
  });
  expect(response.status).toBe(201);
  return ((await response.json()) as { token: string }).token;
}

describe('the surfaces E7 depends on exist on their own (§86 limbs)', () => {
  it('/invite is a public route that previews the invitation for a signed-out visitor', async () => {
    const token = await inviteOverHttp('solo@example.com', 'reviewer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    expect(await screen.findByText(/create your account to join demo org/i)).toBeInTheDocument();
    expect(window.location.pathname).toBe('/invite');
  });

  it('/invite is usable while signed in (not bounced into the app)', async () => {
    orgModel.addOrganization({ id: 'org-a', name: 'Erin Agency', slug: 'erin-agency' });
    orgModel.addUser({ id: 'user-erin', email: 'erin@example.com', full_name: 'Erin Existing' });
    orgModel.setMembership('org-a', 'user-erin', 'owner');
    const token = await inviteOverHttp('erin@example.com', 'viewer');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { token: orgModel.signIn('user-erin') });
    expect(await screen.findByRole('heading', { name: /join demo org/i })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/invite');
  });

  it('Settings carries organization member management', async () => {
    const screen = renderProductionApp(<App />, '/settings', { token: 'test-token' });
    const members = await screen.findByRole('table', { name: /members of demo org/i });
    expect(within(members).getByText('demo@signalnest.dev')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /invite member/i })).toBeInTheDocument();
  });
});

describe('E7 through the customer UI (§87, §88)', () => {
  it('mode 1 — a NEW account: owner invites a Marketer, who registers from the link and works as a Marketer', async () => {
    const { owner, link } = await ownerInvites('nia.new@example.com', 'Marketer');
    // No membership exists for the invitee until the invitee acts.
    expect(inviteeMembership('nia.new@example.com', 'org-1')).toBeUndefined();
    owner.unmount();

    // 5. The second person opens the link in their own, signed-out browser.
    const nia = renderProductionApp(<App />, pathOf(link), { probe: <SessionProbe /> });
    // 6. The preview names the organization, the invited email and the role.
    const summary = (await nia.findByText('Invited email')).closest('dl')!;
    expect(within(summary).getByText('Demo Org')).toBeInTheDocument();
    expect(within(summary).getByText('nia.new@example.com')).toBeInTheDocument();
    expect(within(summary).getByText('Marketer')).toBeInTheDocument();
    expect(window.location.hash).toBe('');

    // 7. Registers through the invitation.
    await nia.user.type(nia.getByLabelText(/full name/i), 'Nia New');
    await nia.user.type(nia.getByLabelText(/^password/i), 'long enough password');
    await nia.user.click(nia.getByRole('button', { name: /create account and join/i }));

    // 8. Accepted, by the flow — the model stamps the membership's origin.
    await waitFor(() => expect(inviteeMembership('nia.new@example.com', 'org-1')?.source).toBe('invitation'));
    expect(inviteeMembership('nia.new@example.com', 'org-1')?.role).toBe('marketer');
    // 9. The SessionOut was applied: a new session token, holding the new membership.
    await waitFor(() => expect(nia.getByTestId('m4-memberships')).toHaveTextContent('org-1=marketer'));
    expect(localStorage.getItem('signalnest-token')).toMatch(/^access-user-invited-/);
    // 10. The joined organization is the active one.
    await expectActive(nia, 'org-1', 'marketer');
    // The invite page itself hands over to the app (it does not merely leave the org selected).
    await waitFor(() => expect(window.location.pathname).toBe('/'));

    // 11 + 12. Inside the app, as a Marketer: editor controls yes, administration no.
    await navigate(nia, /^locations$/i);
    await nia.findByText('Dallas');
    expect(nia.getAllByRole('button', { name: /add location/i }).length).toBeGreaterThan(0);
    await navigate(nia, /^settings$/i);
    const members = await nia.findByRole('table', { name: /members of demo org/i });
    const self = within(members).getByText('Nia New').closest('tr')!;
    expect(within(self).getByText('Marketer')).toBeInTheDocument();
    expect(nia.queryByRole('button', { name: /invite member/i })).not.toBeInTheDocument();
    expect(nia.queryByRole('button', { name: /^change role for /i })).not.toBeInTheDocument();
    expect(nia.queryByRole('button', { name: /^(\+\s*)?new$/i })).not.toBeInTheDocument();
    nia.unmount();

    // 11. The OWNER's member list shows the second user with the non-OWNER role.
    const ownerAgain = renderProductionApp(<App />, '/settings', { token: 'test-token', probe: <SessionProbe /> });
    const list = await ownerAgain.findByRole('table', { name: /members of demo org/i });
    await waitFor(() => expect(within(list).getByText('nia.new@example.com')).toBeInTheDocument());
    expect(within(within(list).getByText('Nia New').closest('tr')!).getByText('Marketer')).toBeInTheDocument();
  });

  it('mode 2 — an EXISTING account in org A accepts into org B, holds both, and nothing of A bleeds into B', async () => {
    // Erin already owns her own organization, with a workspace whose data is unmistakably hers.
    orgModel.addOrganization({ id: 'org-a', name: 'Erin Agency', slug: 'erin-agency' }, [
      { id: 'ws-a', name: 'Agency Workspace', slug: 'agency-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    orgModel.addUser({ id: 'user-erin', email: 'erin@example.com', full_name: 'Erin Existing', password: 'erin password 1' });
    orgModel.setMembership('org-a', 'user-erin', 'owner');
    let agencyLocationReads = 0;
    server.use(
      http.get(P('/workspaces/ws-a/locations'), () => {
        agencyLocationReads += 1;
        return HttpResponse.json([
          {
            id: 'loc-agency',
            name: 'Agency HQ',
            address: '9 Agency Way',
            city: 'Agencyville',
            state_province: '',
            country: 'Erinland',
            postal_code: '',
            timezone: 'UTC',
            currency: 'USD',
            latitude: 0,
            longitude: 0,
            local_competitors: [],
            local_notes: '',
          },
        ]);
      }),
    );

    const { owner, link } = await ownerInvites('erin@example.com', 'Admin');
    owner.unmount();

    // Erin is already signed in, working in her own organization.
    const erin = renderProductionApp(<App />, '/locations', { token: orgModel.signIn('user-erin'), probe: <SessionProbe /> });
    await expectActive(erin, 'org-a', 'owner');
    expect(await erin.findByText('Agency HQ')).toBeInTheDocument();
    erin.unmount();

    // She opens the link (same account, signed in) and accepts explicitly.
    const again = renderProductionApp(<App />, pathOf(link), { token: orgModel.signIn('user-erin'), probe: <SessionProbe /> });
    expect(await again.findByRole('heading', { name: /join demo org/i })).toBeInTheDocument();
    expect(orgModel.count('POST', /\/auth\/invitations\/accept$/)).toBe(0);
    await again.user.click(again.getByRole('button', { name: /accept invitation/i }));

    // Session holds A + B; B (the joined organization) is active, as an Admin.
    await waitFor(() => expect(again.getByTestId('m4-memberships')).toHaveTextContent('org-1=admin,org-a=owner'));
    expect(inviteeMembership('erin@example.com', 'org-1')?.source).toBe('invitation');
    await expectActive(again, 'org-1', 'admin');
    await waitFor(() => expect(window.location.pathname).toBe('/'));
    const readsAtJoin = agencyLocationReads;

    // B's workspaces load; A's data never appears under B.
    await navigate(again, /^locations$/i);
    expect(await again.findByText('Dallas')).toBeInTheDocument();
    expect(again.queryByText('Agency HQ')).not.toBeInTheDocument();
    expect(agencyLocationReads).toBe(readsAtJoin);
    await navigate(again, /^settings$/i);
    const members = await again.findByRole('table', { name: /members of demo org/i });
    expect(within(members).getByText('erin@example.com')).toBeInTheDocument();
    // Role-derived controls are B's: an Admin may invite here.
    expect(again.getByRole('button', { name: /invite member/i })).toBeInTheDocument();
    // And the session is still stable in B after everything settled.
    await expectActive(again, 'org-1', 'admin');
  });

  it('a restrictive role (Viewer) gets the member list and no mutation control', async () => {
    const { owner, link } = await ownerInvites('vera.viewer@example.com', 'Viewer');
    owner.unmount();
    const vera = renderProductionApp(<App />, pathOf(link), { probe: <SessionProbe /> });
    await vera.user.type(await vera.findByLabelText(/full name/i), 'Vera Viewer');
    await vera.user.type(vera.getByLabelText(/^password/i), 'long enough password');
    await vera.user.click(vera.getByRole('button', { name: /create account and join/i }));
    await expectActive(vera, 'org-1', 'viewer');
    await waitFor(() => expect(window.location.pathname).toBe('/'));

    await navigate(vera, /^locations$/i);
    await vera.findByText('Dallas');
    expect(vera.queryByRole('button', { name: /add location/i })).not.toBeInTheDocument();
    await navigate(vera, /^scout requests$/i);
    await vera.findByText('Dallas demand scan');
    expect(vera.queryByRole('button', { name: /new scout request|run now|pause|resume/i })).not.toBeInTheDocument();
    await navigate(vera, /^settings$/i);
    const members = await vera.findByRole('table', { name: /members of demo org/i });
    expect(within(members).getByText('Vera Viewer')).toBeInTheDocument();
    expect(vera.queryByRole('button', { name: /invite member|^change role for |^remove /i })).not.toBeInTheDocument();
    const veraId = orgModel.users().find((u) => u.email === 'vera.viewer@example.com')!.id;
    expect(orgModel.requests().filter((r) => r.userId === veraId && /\/invitations$/.test(r.path))).toEqual([]);
  });
});
