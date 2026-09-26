import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { act, fireEvent, waitFor, within, type RenderResult } from '@testing-library/react';
import type userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { ApiError, setAuthToken } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import { useAuth } from '@/auth/AuthContext';
import {
  formatDate,
  inviteError,
  isForbidden,
  isStaleMemberError,
  removeErrorMessage,
  revokeErrorMessage,
  roleChangeErrorMessage,
  soleOwnerId,
} from '@/pages/settings/member-admin';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { emulateExitAnimations, renderApp, renderProductionApp } from '@/test/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

/**
 * The administrator's side of P6-AUTH-1 in Settings (§13–§15, §27–§33, §44, §45,
 * §51, §52, §71–§80, §83), against the stateful backend model.
 *
 * Expectations are written from the BACKEND rules (members.py, invitations.py,
 * organizations/routes.py), never read back from lib/roles.ts. The actor's role is
 * configured in the model BEFORE render and awaited through a probe, so an absent
 * control always means "hidden for this role", never "not loaded yet".
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const ORIGIN = 'http://localhost:3000';
type Screen = RenderResult & { user: ReturnType<typeof userEvent.setup> };

const ROSTER = {
  owner: { name: 'Olivia Owner', email: 'olivia.owner@example.com', id: 'user-owner-2' },
  admin: { name: 'Adam Admin', email: 'adam.admin@example.com', id: 'user-admin' },
  marketer: { name: 'Mara Marketer', email: 'mara.marketer@example.com', id: 'user-marketer' },
  reviewer: { name: 'Rhea Reviewer', email: 'rhea.reviewer@example.com', id: 'user-reviewer' },
  compliance_reviewer: { name: 'Cora Compliance', email: 'cora.compliance@example.com', id: 'user-compliance' },
  viewer: { name: 'Vic Viewer', email: 'vic.viewer@example.com', id: 'user-viewer' },
} as const;
const LABEL: Record<ModelRole, string> = {
  owner: 'Owner',
  admin: 'Admin',
  marketer: 'Marketer',
  reviewer: 'Reviewer',
  compliance_reviewer: 'Compliance Reviewer',
  viewer: 'Viewer',
};
const SIX: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];

let cache: QueryClient | null = null;
function Probes() {
  const qc = useQueryClient();
  const { memberships } = useAuth();
  const { organizationId } = useWorkspace();
  useEffect(() => {
    cache = qc;
  }, [qc]);
  const role = memberships.find((m) => m.organization_id === organizationId)?.role ?? '';
  return <span data-testid="m4-role-probe">{`${organizationId ?? ''}:${role}`}</span>;
}

async function settled(screen: Screen, orgId: string, role: ModelRole) {
  await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent(`${orgId}:${role}`));
  // The member list itself has rendered (a read surface for every role).
  await screen.findByRole('table', { name: /members of/i });
}

async function renderSettingsAs(role: ModelRole, { roster = true } = {}) {
  if (roster) orgModel.seedRoster('org-1');
  orgModel.setMembership('org-1', 'user-1', role);
  const screen = renderApp(
    <>
      <App />
      <Probes />
    </>,
    { route: '/settings' },
  );
  await settled(screen, 'org-1', role);
  return screen;
}

const memberRow = (screen: Screen, name: string) =>
  within(screen.getByRole('table', { name: /members of/i }))
    .getByText(name)
    .closest('tr') as HTMLElement;

async function createInvitationAsOwner(email: string, role: 'admin' | 'marketer' | 'reviewer' | 'compliance_reviewer' | 'viewer') {
  setAuthToken('test-token');
  try {
    return await api.createInvitation('org-1', { email, role });
  } finally {
    setAuthToken(null);
  }
}

async function openSelectOptions(screen: Screen, trigger: HTMLElement): Promise<string[]> {
  await screen.user.click(trigger);
  const listbox = await screen.findByRole('listbox');
  return within(listbox)
    .getAllByRole('option')
    .map((o) => o.textContent?.trim() ?? '');
}

const count = (method: string, re: RegExp) => orgModel.count(method, re);

beforeEach(() => {
  orgModel.reset();
  cache = null;
});

afterEach(() => {
  expect(orgModel.violations()).toEqual([]);
});

// ---- §13 / §14 / §44 / §45: create, one-time link, copy -----------------------

describe('creating an invitation yields a one-time, fragment-only link (§13, §14, §44, §45)', () => {
  it('builds <origin>/invite#token=abc123 from the create response and copies exactly that', async () => {
    orgModel.setNextInvitationToken('abc123');
    const screen = await renderSettingsAs('owner');

    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    // §45: no delivery is promised before the invitation exists either.
    expect(dialog).toHaveTextContent(/doesn.t deliver invitations by email/i);
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'nia.new@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));

    const result = await screen.findByRole('dialog', { name: /invitation created/i });
    expect(result).toHaveTextContent('No email was sent.');
    expect(result).toHaveTextContent('Copy this link and send it securely to nia.new@example.com.');
    const link = within(result).getByLabelText(/invitation link/i);
    expect(link).toHaveValue(`${ORIGIN}/invite#token=abc123`);
    expect((link as HTMLInputElement).value).not.toContain('?');

    await screen.user.click(within(result).getByRole('button', { name: /copy invitation link/i }));
    expect(await navigator.clipboard.readText()).toBe(`${ORIGIN}/invite#token=abc123`);
    expect(within(result).getByText(/link copied/i)).toBeInTheDocument();
    // The created invitation is the model's, with the role the dialog defaulted to.
    expect(orgModel.invitations().map((i) => [i.email, i.role, i.token])).toEqual([
      ['nia.new@example.com', 'viewer', 'abc123'],
    ]);
  });

  it('cannot show the link again once closed: no token in the page, the list, or the cache (§14, §30)', async () => {
    orgModel.setNextInvitationToken('abc123');
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'nia.new@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    const result = await screen.findByRole('dialog', { name: /invitation created/i });
    expect(result).toHaveTextContent(/can.t be shown again after you close this window/i);
    // Even WHILE the link is on screen, the only copy is the dialog's own state: the
    // create mutation (whose result carried the token) is already out of the cache.
    expect(within(result).getByLabelText(/invitation link/i)).toHaveValue(`${ORIGIN}/invite#token=abc123`);
    const mutationCacheText = () =>
      JSON.stringify(cache!.getMutationCache().getAll().map((m) => [m.state.variables, m.state.data, m.state.context]));
    await waitFor(() => expect(mutationCacheText()).not.toContain('abc123'));

    await screen.user.click(within(result).getByRole('button', { name: /^done$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // The pending row: email, role, created, expires, revoke — and nothing to copy.
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    const row = within(pending).getByText('nia.new@example.com').closest('tr')!;
    expect(within(row).getByText('Viewer')).toBeInTheDocument();
    expect(within(row).getAllByRole('time')).toHaveLength(2);
    expect(within(row).getAllByRole('button').map((b) => b.textContent?.trim())).toEqual(['Revoke']);
    expect(document.documentElement.outerHTML).not.toContain('abc123');
    expect(document.documentElement.outerHTML).not.toContain('#token=');
    // The create mutation carried the token in its result; it must not outlive the dialog.
    await waitFor(() => {
      const retained = JSON.stringify(
        cache!.getMutationCache().getAll().map((m) => [m.state.variables, m.state.data, m.state.context]),
      );
      expect(retained).not.toContain('abc123');
    });
    expect(JSON.stringify(cache!.getQueryCache().getAll().map((q) => [q.queryKey, q.state.data]))).not.toContain('abc123');
  });

  it('never claims delivery anywhere in the rendered flow (§45, §83 dynamic)', async () => {
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    const formText = dialog.textContent ?? '';
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'nia.new@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    const resultText = (await screen.findByRole('dialog', { name: /invitation created/i })).textContent ?? '';
    const pageText = document.body.textContent ?? '';

    for (const text of [formText, resultText, pageText]) {
      const withoutNegation = text.replace(/No email (is|was) sent\./g, '');
      expect(withoutNegation).not.toMatch(DELIVERY);
      expect(withoutNegation).not.toMatch(/we(?:'ve| have) emailed|check (?:their|your) inbox/i);
    }
    expect(resultText).toMatch(/no email was sent/i);
    expect(resultText).toMatch(/copy/i);
  });
});

// ---- §83 static -----------------------------------------------------------------

// §83's four words, as words: `emailError` is not "mailer".
const DELIVERY = /\bsent\b|\bresend\b|\bemail invitations?\b|\bmailers?\b/i;

describe('no new UI source implies automatic delivery (§83 static)', () => {
  it('the pattern catches what it claims to (positive instances)', () => {
    for (const line of ['Invitation sent to a@b.c', 'Resend invitation', 'We send an email invitation', 'mailer.deliver()']) {
      expect(DELIVERY.test(line), line).toBe(true);
    }
    for (const line of ['const emailError = x;', 'Copy this link and send it securely to a@b.c.']) {
      expect(DELIVERY.test(line), line).toBe(false);
    }
  });

  const sources = import.meta.glob(
    ['/src/pages/settings/**/*.{ts,tsx}', '/src/pages/Settings.tsx', '/src/pages/invite/**/*.{ts,tsx}', '!/src/**/__tests__/**'],
    { query: '?raw', import: 'default', eager: true },
  ) as Record<string, string>;

  it('finds the new files, then no delivery vocabulary in them', () => {
    expect(Object.keys(sources)).toEqual(
      expect.arrayContaining([
        '/src/pages/Settings.tsx',
        '/src/pages/invite/InvitePage.tsx',
        '/src/pages/settings/Invitations.tsx',
      ]),
    );
    const offenders: string[] = [];
    for (const [path, text] of Object.entries(sources)) {
      if (path.includes('/__tests__/')) continue;
      text.split('\n').forEach((line, i) => {
        const cleaned = line.replace(/No email (is|was) sent\./g, '');
        if (DELIVERY.test(cleaned)) offenders.push(`${path}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(offenders).toEqual([]);
  });
});

// ---- §15 / §71 / §76: the members surface -------------------------------------

describe('the members surface (§15, §71, §76)', () => {
  it('lists name, email, human role label and joined date, with the organization-wide copy', async () => {
    const screen = await renderSettingsAs('owner');
    expect(screen.getByText('Roles apply to the whole organization and all of its workspaces.')).toBeInTheDocument();
    for (const [role, person] of Object.entries(ROSTER)) {
      const row = memberRow(screen, person.name);
      expect(within(row).getByText(person.email)).toBeInTheDocument();
      expect(within(row).getByText(LABEL[role as ModelRole])).toBeInTheDocument();
      expect(within(row).getByRole('time')).toBeInTheDocument();
    }
    // Human labels only: the raw enum never reaches the page.
    expect(document.body.textContent).not.toContain('compliance_reviewer');
    expect(within(memberRow(screen, 'Demo Marketer')).getByText('(you)')).toBeInTheDocument();
  });
});

// ---- §51: Settings × six roles --------------------------------------------------

describe('Settings under each of the six roles (§51)', () => {
  it.each(SIX)('%s', async (role) => {
    const admin = role === 'owner' || role === 'admin';
    const screen = await renderSettingsAs(role);

    // Member list: visible to everyone, all seven members.
    const table = screen.getByRole('table', { name: /members of/i });
    expect(within(table).getAllByRole('row')).toHaveLength(8); // header + 7
    // Organization administration: exactly owner and admin.
    expect(screen.queryAllByRole('button', { name: /invite member/i })).toHaveLength(admin ? 1 : 0);
    expect(screen.queryAllByRole('button', { name: /^change role for /i }).length > 0).toBe(admin);
    expect(screen.queryAllByRole('button', { name: /^remove /i }).length > 0).toBe(admin);
    expect(screen.queryAllByRole('heading', { name: /pending invitations/i }).length > 0 || screen.queryAllByText(/^pending invitations$/i).length > 0).toBe(admin);
    // Workspaces + New: exactly owner and admin (a marketer is refused).
    expect(screen.queryAllByRole('button', { name: /^(\+\s*)?new$/i })).toHaveLength(admin ? 1 : 0);
    // The invitation list is an admin-only route: never requested for anyone else.
    if (admin) await waitFor(() => expect(count('GET', /\/organizations\/org-1\/invitations$/)).toBeGreaterThan(0));
    else expect(count('GET', /\/organizations\/org-1\/invitations$/)).toBe(0);
  });
});

// ---- §52 / §28 / §29 / §77 / §78: actor × target -------------------------------

describe('member management by actor and target (§52)', () => {
  const OWNER_OPTIONS = ['Owner', 'Admin', 'Marketer', 'Reviewer', 'Compliance Reviewer', 'Viewer'];
  const ADMIN_OPTIONS = ['Admin', 'Marketer', 'Reviewer', 'Compliance Reviewer', 'Viewer'];
  const cases: [ModelRole, keyof typeof ROSTER | 'self', string[] | null][] = [
    ['owner', 'owner', OWNER_OPTIONS], // another owner: allowed while two owners exist
    ['owner', 'admin', OWNER_OPTIONS],
    ['owner', 'marketer', OWNER_OPTIONS],
    ['owner', 'reviewer', OWNER_OPTIONS],
    ['owner', 'compliance_reviewer', OWNER_OPTIONS],
    ['owner', 'viewer', OWNER_OPTIONS],
    ['owner', 'self', null],
    ['admin', 'owner', null], // an admin may not touch an owner
    ['admin', 'admin', ADMIN_OPTIONS],
    ['admin', 'marketer', ADMIN_OPTIONS],
    ['admin', 'reviewer', ADMIN_OPTIONS],
    ['admin', 'compliance_reviewer', ADMIN_OPTIONS],
    ['admin', 'viewer', ADMIN_OPTIONS],
    ['admin', 'self', null],
  ];

  it.each(cases)('%s → %s', async (actor, target, options) => {
    if (actor === 'admin') {
      // A second owner besides Olivia: otherwise "Olivia is the only owner" hides her
      // actions on its own and an admin → owner cell could never fail for the right reason.
      orgModel.addUser({ id: 'user-owner-3', email: 'otto.owner@example.com', full_name: 'Otto Owner' });
      orgModel.setMembership('org-1', 'user-owner-3', 'owner', '2026-02-09T00:00:00Z');
    }
    const screen = await renderSettingsAs(actor);
    const name = target === 'self' ? 'Demo Marketer' : ROSTER[target].name;
    const row = memberRow(screen, name);
    const change = within(row).queryByRole('button', { name: /^change role for /i });
    const remove = within(row).queryByRole('button', { name: /^remove /i });

    if (options === null) {
      expect(change).not.toBeInTheDocument();
      expect(remove).not.toBeInTheDocument();
      return;
    }
    expect(remove).toBeInTheDocument();
    await screen.user.click(change!);
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    expect(await openSelectOptions(screen, within(dialog).getByRole('combobox'))).toEqual(options);
    if (actor === 'admin') {
      // Absent, not disabled (§75/§77): no element for owner exists at all.
      expect(document.querySelectorAll('[role="option"][data-value="owner"], option[value="owner"]')).toHaveLength(0);
    }
  });
});

// ---- §27 / §75: invite role options --------------------------------------------

describe('invite role options never include Owner (§27, §75)', () => {
  it.each(['owner', 'admin'] as const)('%s sees exactly the five invitable roles', async (actor) => {
    const screen = await renderSettingsAs(actor);
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    const options = await openSelectOptions(screen, within(dialog).getByRole('combobox'));
    expect(options).toEqual(['Admin', 'Marketer', 'Reviewer', 'Compliance Reviewer', 'Viewer']);
    expect(options.some((o) => /owner/i.test(o))).toBe(false);
    expect(document.querySelectorAll('[role="option"][data-value="owner"], option[value="owner"]')).toHaveLength(0);
  });
});

// ---- §31 / §73: revoke ---------------------------------------------------------

describe('revoking a pending invitation (§31, §73)', () => {
  it('asks first, sends nothing on cancel, then revokes and refreshes the list', async () => {
    await createInvitationAsOwner('pending.person@example.com', 'reviewer');
    const screen = await renderSettingsAs('owner');
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    const revoke = within(pending).getByRole('button', { name: /revoke invitation for pending.person@example.com/i });

    await screen.user.click(revoke);
    let confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    await screen.user.click(within(confirm).getByRole('button', { name: /^cancel$/i }));
    expect(count('DELETE', /\/invitations\//)).toBe(0);

    await screen.user.click(within(pending).getByRole('button', { name: /revoke invitation for/i }));
    confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    await screen.user.click(within(confirm).getByRole('button', { name: /^revoke invitation$/i }));

    expect(await screen.findByText(/no pending invitations/i)).toBeInTheDocument();
    expect(count('DELETE', /\/invitations\//)).toBe(1);
    expect(await screen.findByText(/invitation revoked/i)).toBeInTheDocument();
  });
});

// ---- §29 / §33 / §73: removal ---------------------------------------------------

describe('removing a member (§29, §33, §73)', () => {
  it('asks first, then removes, and the actor stays signed in', async () => {
    const screen = await renderSettingsAs('owner');
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
    let confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    expect(confirm).toHaveTextContent(/mara marketer/i);
    await screen.user.click(within(confirm).getByRole('button', { name: /^cancel$/i }));
    expect(count('DELETE', /\/members\//)).toBe(0);

    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
    confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));

    await waitFor(() => expect(screen.queryByText('mara.marketer@example.com')).not.toBeInTheDocument());
    expect(count('DELETE', /\/members\/user-marketer$/)).toBe(1);
    expect(localStorage.getItem('signalnest-token')).toBe('test-token');
    expect(screen.getByRole('table', { name: /members of/i })).toBeInTheDocument();
  });
});

// ---- §32: role change ------------------------------------------------------------

describe('changing a role (§28, §32)', () => {
  it('sends only the new role and shows what the server now holds', async () => {
    const screen = await renderSettingsAs('owner');
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Reviewer' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));

    await waitFor(() => expect(within(memberRow(screen, 'Mara Marketer')).getByText('Reviewer')).toBeInTheDocument());
    const put = orgModel.requests().filter((r) => r.method === 'PUT' && r.path.endsWith('/members/user-marketer/role'));
    expect(put.map((r) => r.body)).toEqual([{ role: 'reviewer' }]);
    expect(orgModel.memberships().find((m) => m.user_id === 'user-marketer')?.role).toBe('reviewer');
  });
});

// ---- §79: last owner and the stale race ----------------------------------------

describe('last owner and stale snapshots (§79)', () => {
  it('the only owner cannot be demoted or removed, with an explanation', async () => {
    const screen = await renderSettingsAs('owner', { roster: false });
    const self = memberRow(screen, 'Demo Marketer');
    expect(within(self).queryByRole('button')).not.toBeInTheDocument();
    expect(self).toHaveTextContent(/only owner/i);
  });

  it('a real stale race: the actor was demoted meanwhile, so the server refuses (403) and the UI catches up', async () => {
    const screen = await renderSettingsAs('owner');
    // Olivia demotes the demo actor behind this screen's back.
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'admin');
    await screen.user.click(within(memberRow(screen, 'Olivia Owner')).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Admin' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));

    // The refusal is explained somewhere the actor can see it (inline or a toast) —
    // a dialog that simply closes reads as success.
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:admin'));
    expect(
      await screen.findByText(/only an owner can change an owner|your role(, or theirs,)? may have changed|no longer have permission/i),
    ).toBeInTheDocument();
    expect(orgModel.memberships().find((m) => m.user_id === 'user-owner-2')?.role).toBe('owner');
    // Close with Cancel, not Escape: a toast above the dialog takes the first Escape.
    if (document.body.contains(dialog)) await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    // The refusal refreshed the session: the actor is now an admin, so owner rows lose their actions.
    await waitFor(() =>
      expect(within(memberRow(screen, 'Olivia Owner')).queryByRole('button', { name: /^change role for /i })).not.toBeInTheDocument(),
    );
  });

  it('a stale invite: the actor lost administration meanwhile, and the refusal is explained', async () => {
    const screen = await renderSettingsAs('owner');
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'marketer');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'late@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));

    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:marketer'));
    expect(await screen.findByText(/no longer have permission|role may have changed|not permitted/i)).toBeInTheDocument();
    expect(orgModel.invitations()).toHaveLength(0);
    await waitFor(() => expect(screen.queryByRole('button', { name: /invite member/i })).not.toBeInTheDocument());
  });

  it('organization_last_owner from the server is explained specifically (stub: unreachable in a consistent model)', async () => {
    const screen = await renderSettingsAs('owner');
    server.use(
      http.put(P('/organizations/org-1/members/:userId/role'), () =>
        HttpResponse.json(
          { error: { code: 'organization_last_owner', message: "The organization's last owner cannot be demoted or removed." } },
          { status: 409 },
        ),
      ),
      http.delete(P('/organizations/org-1/members/:userId'), () =>
        HttpResponse.json(
          { error: { code: 'organization_last_owner', message: "The organization's last owner cannot be demoted or removed." } },
          { status: 409 },
        ),
      ),
    );
    await screen.user.click(within(memberRow(screen, 'Olivia Owner')).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Viewer' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
    expect(await within(dialog).findByText(/only owner, so their role can.t be changed/i)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Request failed \(|organization_last_owner/);
    // Close with Cancel, not Escape: a toast above the dialog takes the first Escape.
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /change role/i })).not.toBeInTheDocument());

    await screen.user.click(within(memberRow(screen, 'Olivia Owner')).getByRole('button', { name: /^remove /i }));
    const confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
    expect(await screen.findByText(/only owner, so they can.t be removed/i)).toBeInTheDocument();
    expect(memberRow(screen, 'Olivia Owner')).toBeInTheDocument();
  });
});

// ---- §17: keys ------------------------------------------------------------------

describe('member and invitation query keys are organization-scoped (§17, §43 structural)', () => {
  it('embeds the organization id, never a global list, and nothing else', () => {
    expect(queryKeys.organizationMembers('org-x')).toEqual(['organizations', 'org-x', 'members']);
    expect(queryKeys.organizationInvitations('org-x')).toEqual(['organizations', 'org-x', 'invitations']);
    expect(queryKeys.organizationMembers('org-a')).not.toEqual(queryKeys.organizationMembers('org-b'));
    expect(queryKeys.organizationInvitations('org-a')).not.toEqual(queryKeys.organizationInvitations('org-b'));
  });
});

// ---- §80 / §16: organization A never leaks into B --------------------------------

describe('organization-scoped member and invitation caches (§16, §80)', () => {
  function addSecondOrganization(actorRole: ModelRole) {
    orgModel.addOrganization({ id: 'org-2', name: 'Second Org', slug: 'second-org' }, [
      { id: 'ws-2', name: 'Second Workspace', slug: 'second-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    orgModel.addUser({ id: 'user-zed', email: 'zed.second@example.com', full_name: 'Zed Second' });
    orgModel.setMembership('org-2', 'user-zed', 'marketer');
    orgModel.setMembership('org-2', 'user-1', actorRole);
  }

  async function switchOrganization(screen: Screen, name: string) {
    const [trigger] = screen.getAllByRole('combobox', { name: /^organization$/i });
    await screen.user.click(trigger!);
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name }));
  }

  it('never shows organization A members while organization B loads, and hides B-forbidden controls', async () => {
    orgModel.seedRoster('org-1');
    addSecondOrganization('viewer');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/organizations/org-2/members'), async () => {
        await held;
        return undefined;
      }),
    );
    const screen = renderApp(
      <>
        <App />
        <Probes />
      </>,
      { route: '/settings' },
    );
    await settled(screen, 'org-1', 'owner');
    expect(screen.getByText('mara.marketer@example.com')).toBeInTheDocument();

    await switchOrganization(screen, 'Second Org');
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-2:viewer'));
    // While B's list is still in flight, nothing of A's is on screen.
    expect(screen.queryByText('mara.marketer@example.com')).not.toBeInTheDocument();
    expect(screen.queryByText('olivia.owner@example.com')).not.toBeInTheDocument();
    act(() => release());
    expect(await screen.findByText('zed.second@example.com')).toBeInTheDocument();
    // Role-derived controls follow the active organization: a viewer in B.
    expect(screen.queryByRole('button', { name: /invite member/i })).not.toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /^change role for /i })).not.toBeInTheDocument();
    expect(count('GET', /\/organizations\/org-2\/invitations$/)).toBe(0);
  });

  it('never shows organization A pending invitations while organization B loads its own', async () => {
    addSecondOrganization('admin');
    await createInvitationAsOwner('only.in.a@example.com', 'viewer');
    setAuthToken('test-token');
    try {
      await api.createInvitation('org-2', { email: 'only.in.b@example.com', role: 'viewer' });
    } finally {
      setAuthToken(null);
    }
    let hold = false;
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/organizations/org-2/invitations'), async () => {
        if (hold) await held;
        return undefined;
      }),
    );
    const screen = renderApp(
      <>
        <App />
        <Probes />
      </>,
      { route: '/settings' },
    );
    await settled(screen, 'org-1', 'owner');
    const a = await screen.findByRole('table', { name: /pending invitations/i });
    expect(within(a).getByText('only.in.a@example.com')).toBeInTheDocument();

    hold = true;
    await switchOrganization(screen, 'Second Org');
    await settled(screen, 'org-2', 'admin');
    // B's list is still in flight: A's invitation must not stand in for it.
    expect(screen.queryByText('only.in.a@example.com')).not.toBeInTheDocument();
    act(() => release());
    const b = await screen.findByRole('table', { name: /pending invitations/i });
    expect(within(b).getByText('only.in.b@example.com')).toBeInTheDocument();
    expect(screen.queryByText('only.in.a@example.com')).not.toBeInTheDocument();
  });

  // ---- §80, systematically: every organization-A cache or session side effect ----
  //
  // One row per side-effect site and branch (success, refusal, stale refusal, plain
  // failure) of OrganizationMembers.tsx and Invitations.tsx. Every row starts from the
  // same state — organization B's members, invitations and workspaces cached in this
  // tab — performs ONE action in A, then asserts:
  //   * the row's own positive instance (A's list re-read, the session re-read or NOT);
  //   * B untouched: not invalidated, not refetched, not reset (same data, same
  //     dataUpdatedAt).
  // See SR/m4/SIDE-EFFECT-INVENTORY.md for the site → row → mutant map.

  interface Snapshot {
    aMembers: number;
    aInvitations: number;
    aWorkspaces: number;
    session: number;
  }
  interface Row {
    name: string;
    act: (screen: Screen) => Promise<void>;
    expectA: (screen: Screen, before: Snapshot) => Promise<void>;
  }
  const aMembersGets = () => count('GET', /\/organizations\/org-1\/members$/);
  const aInvitationGets = () => count('GET', /\/organizations\/org-1\/invitations$/);
  const sessionGets = () => count('GET', /\/auth\/me$/);
  const confirmIn = (screen: Screen) => screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));

  async function changeRole(screen: Screen, name: string, to: string) {
    await screen.user.click(within(memberRow(screen, name)).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: to }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
    return dialog;
  }
  async function removeMember(screen: Screen, name: string) {
    await screen.user.click(within(memberRow(screen, name)).getByRole('button', { name: /^remove /i }));
    await screen.user.click(within(await confirmIn(screen)).getByRole('button', { name: /^remove member$/i }));
  }
  async function revokeInvitation(screen: Screen, email: string) {
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    await screen.user.click(within(pending).getByRole('button', { name: new RegExp(`revoke invitation for ${email}`, 'i') }));
    await screen.user.click(within(await confirmIn(screen)).getByRole('button', { name: /^revoke invitation$/i }));
  }
  async function invite(screen: Screen, email: string) {
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), email);
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    return dialog;
  }
  const expectSessionReread = async (before: Snapshot) =>
    waitFor(() => expect(sessionGets(), 'session re-read').toBeGreaterThan(before.session));
  const expectSessionNotReread = async (before: Snapshot) => {
    await new Promise((resolve) => setTimeout(resolve, 150));
    expect(sessionGets(), 'session must not be re-read for a non-stale refusal').toBe(before.session);
  };

  let failedSessionReads = 0;
  const ROWS: Row[] = [
    {
      name: 'change a role (success)',
      act: async (screen) => void (await changeRole(screen, 'Mara Marketer', 'Viewer')),
      expectA: async (screen, before) => {
        await waitFor(() => expect(within(memberRow(screen, 'Mara Marketer')).getByText('Viewer')).toBeInTheDocument());
        expect(aMembersGets()).toBeGreaterThan(before.aMembers);
        await expectSessionNotReread(before);
      },
    },
    {
      name: 'change a role, refused as stale (403)',
      act: async (screen) => {
        orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'admin');
        await changeRole(screen, 'Olivia Owner', 'Admin');
      },
      expectA: async (screen, before) => {
        expect(await screen.findByText(/only an owner can change an owner/i)).toBeInTheDocument();
        await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:admin'));
        await expectSessionReread(before);
        await waitFor(() => expect(aMembersGets()).toBeGreaterThan(before.aMembers));
      },
    },
    {
      name: 'change a role, failing for a non-stale reason (500)',
      act: async (screen) => {
        server.use(
          http.put(P('/organizations/org-1/members/:userId/role'), () =>
            HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 }),
          ),
        );
        await changeRole(screen, 'Mara Marketer', 'Reviewer');
      },
      expectA: async (screen, before) => {
        const dialog = screen.getByRole('dialog', { name: /change role/i });
        expect(await within(dialog).findByText(/the server stumbled/i)).toBeInTheDocument();
        // Not a stale picture: no toast, no session re-read, no list re-read.
        expect(screen.queryByText(/could not change role/i)).not.toBeInTheDocument();
        await expectSessionNotReread(before);
        expect(aMembersGets()).toBe(before.aMembers);
      },
    },
    {
      name: 'revoke an invitation (success)',
      act: (screen) => revokeInvitation(screen, 'pending.person@example.com'),
      expectA: async (screen, before) => {
        expect(await screen.findByText(/no pending invitations/i)).toBeInTheDocument();
        expect(aInvitationGets()).toBeGreaterThan(before.aInvitations);
        await expectSessionNotReread(before);
      },
    },
    {
      name: 'revoke, refused: already revoked meanwhile (409)',
      act: async (screen) => {
        orgModel.invitationRevokedBehindTheUi('pending.person@example.com');
        await revokeInvitation(screen, 'pending.person@example.com');
      },
      expectA: async (screen, before) => {
        expect(await screen.findByText(/could not revoke invitation/i)).toBeInTheDocument();
        // The refused confirmation still closes (Invitations.tsx wraps mutateAsync in try/catch).
        await waitFor(() => expect(screen.queryByRole('dialog', { name: /revoke this invitation/i })).not.toBeInTheDocument());
        // The refusal re-read A's list: the row is gone because the server says so.
        expect(await screen.findByText(/no pending invitations/i)).toBeInTheDocument();
        expect(aInvitationGets()).toBeGreaterThan(before.aInvitations);
        await expectSessionNotReread(before);
      },
    },
    {
      name: 'revoke, refused as stale (403)',
      act: async (screen) => {
        orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'marketer');
        await revokeInvitation(screen, 'pending.person@example.com');
      },
      expectA: async (screen, before) => {
        expect(await screen.findByText(/could not revoke invitation/i)).toBeInTheDocument();
        await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:marketer'));
        await expectSessionReread(before);
        await waitFor(() => expect(screen.queryByRole('table', { name: /pending invitations/i })).not.toBeInTheDocument());
      },
    },
    {
      name: 'remove, refused as stale, and the session re-read itself fails (500)',
      act: async (screen) => {
        orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'admin');
        failedSessionReads = 0;
        server.use(
          http.get(P('/auth/me'), () => {
            failedSessionReads += 1;
            return HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 });
          }),
        );
        await removeMember(screen, 'Olivia Owner');
      },
      expectA: async (screen, before) => {
        expect(await screen.findByText(/could not remove member/i)).toBeInTheDocument();
        // onStale ran (A's list re-read) and tried the session, which failed: the failure is
        // swallowed — nothing else is invalidated, cleared or signed out.
        await waitFor(() => expect(aMembersGets()).toBeGreaterThan(before.aMembers));
        await waitFor(() => expect(failedSessionReads).toBeGreaterThan(0));
        await new Promise((resolve) => setTimeout(resolve, 150));
        expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:owner');
        expect(localStorage.getItem('signalnest-token')).toBe('test-token');
        expect(screen.getByRole('table', { name: /members of demo org/i })).toBeInTheDocument();
      },
    },
    {
      name: 'create, failing for a non-stale reason (500)',
      act: async (screen) => {
        server.use(
          http.post(P('/organizations/org-1/invitations'), () =>
            HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 }),
          ),
        );
        await invite(screen, 'plain.failure@example.com');
      },
      expectA: async (screen, before) => {
        const dialog = screen.getByRole('dialog', { name: /invite member/i });
        expect(await within(dialog).findByText(/the server stumbled/i)).toBeInTheDocument();
        // Not a stale picture and not a list-changing refusal: no toast, no re-reads.
        expect(screen.queryByText(/could not create invitation/i)).not.toBeInTheDocument();
        await expectSessionNotReread(before);
        expect(aInvitationGets()).toBe(before.aInvitations);
        expect(aMembersGets()).toBe(before.aMembers);
      },
    },
    {
      name: 'remove a member (success)',
      act: (screen) => removeMember(screen, 'Rhea Reviewer'),
      expectA: async (screen, before) => {
        await waitFor(() => expect(screen.queryByText('rhea.reviewer@example.com')).not.toBeInTheDocument());
        expect(aMembersGets()).toBeGreaterThan(before.aMembers);
        await expectSessionNotReread(before);
      },
    },
    {
      name: 'remove, refused as stale (403)',
      act: async (screen) => {
        orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'admin');
        await removeMember(screen, 'Olivia Owner');
      },
      expectA: async (screen, before) => {
        expect(await screen.findByText(/could not remove member/i)).toBeInTheDocument();
        await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:admin'));
        await expectSessionReread(before);
        await waitFor(() => expect(aMembersGets()).toBeGreaterThan(before.aMembers));
      },
    },
    {
      name: 'remove, failing for a non-stale reason (500)',
      act: async (screen) => {
        server.use(
          http.delete(P('/organizations/org-1/members/:userId'), () =>
            HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 }),
          ),
        );
        await removeMember(screen, 'Rhea Reviewer');
      },
      expectA: async (screen, before) => {
        expect(await screen.findByText(/could not remove member/i)).toBeInTheDocument();
        expect(screen.getByText('rhea.reviewer@example.com')).toBeInTheDocument();
        await expectSessionNotReread(before);
        expect(aMembersGets()).toBe(before.aMembers);
      },
    },
    {
      name: 'create an invitation (success)',
      act: async (screen) => {
        await invite(screen, 'isolation.check@example.com');
        const result = await screen.findByRole('dialog', { name: /invitation created/i });
        await screen.user.click(within(result).getByRole('button', { name: /^done$/i }));
      },
      expectA: async (screen, before) => {
        const pending = await screen.findByRole('table', { name: /pending invitations/i });
        expect(await within(pending).findByText('isolation.check@example.com')).toBeInTheDocument();
        expect(aInvitationGets()).toBeGreaterThan(before.aInvitations);
        await expectSessionNotReread(before);
      },
    },
    {
      name: 'create, refused: a pending invitation appeared meanwhile (409)',
      act: async (screen) => {
        orgModel.invitationCreatedBehindTheUi('org-1', 'late.pending@example.com', 'viewer', 'user-owner-2');
        await invite(screen, 'late.pending@example.com');
      },
      expectA: async (screen, before) => {
        expect(await screen.findByText(/already has a pending invitation|invitation is already pending/i)).toBeInTheDocument();
        // The refusal re-read A's pending list, which now shows what the server holds.
        await waitFor(() => expect(aInvitationGets()).toBeGreaterThan(before.aInvitations));
        await screen.user.click(within(screen.getByRole('dialog', { name: /invite member/i })).getByRole('button', { name: /^cancel$/i }));
        const pending = await screen.findByRole('table', { name: /pending invitations/i });
        expect(await within(pending).findByText('late.pending@example.com')).toBeInTheDocument();
        await expectSessionNotReread(before);
      },
    },
    {
      name: 'create, refused: the person joined meanwhile (409)',
      act: async (screen) => {
        orgModel.addUser({ id: 'user-late', email: 'late.member@example.com', full_name: 'Late Member' });
        orgModel.setMembership('org-1', 'user-late', 'viewer', '2026-03-01T00:00:00Z');
        await invite(screen, 'late.member@example.com');
      },
      expectA: async (screen, before) => {
        expect(await screen.findByText(/already belongs to a member/i)).toBeInTheDocument();
        await waitFor(() => expect(aMembersGets()).toBeGreaterThan(before.aMembers));
        await screen.user.click(within(screen.getByRole('dialog', { name: /invite member/i })).getByRole('button', { name: /^cancel$/i }));
        expect(await screen.findByText('late.member@example.com')).toBeInTheDocument();
        await expectSessionNotReread(before);
      },
    },
    {
      name: 'create, refused as stale (403)',
      act: async (screen) => {
        orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'marketer');
        await invite(screen, 'late@example.com');
      },
      expectA: async (screen, before) => {
        expect(await screen.findByText(/no longer have permission|role may have changed|not permitted/i)).toBeInTheDocument();
        await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:marketer'));
        await expectSessionReread(before);
        await waitFor(() => expect(screen.queryByRole('button', { name: /invite member/i })).not.toBeInTheDocument());
      },
    },
    {
      name: 'create a workspace (success)',
      act: async (screen) => {
        await screen.user.click(screen.getByRole('button', { name: /^(\+\s*)?new$/i }));
        const dialog = await screen.findByRole('dialog', { name: /create workspace/i });
        await screen.user.type(within(dialog).getByLabelText(/workspace name/i), 'Growth Workspace');
        await screen.user.click(within(dialog).getByRole('button', { name: /^create$/i }));
      },
      expectA: async (screen, before) => {
        // Settings.tsx createWs: the list is re-read, the new workspace becomes active,
        // and the form is reset for next time.
        await waitFor(() => expect(count('GET', /\/organizations\/org-1\/workspaces$/)).toBeGreaterThan(before.aWorkspaces));
        const created = orgModel.workspacesOf('org-1').find((w) => w.name === 'Growth Workspace')!;
        await waitFor(() => expect(localStorage.getItem('signalnest-active-workspace')).toBe(created.id));
        expect((await screen.findAllByText('Growth Workspace')).length).toBeGreaterThan(0);
        await screen.user.click(screen.getByRole('button', { name: /^(\+\s*)?new$/i }));
        const again = await screen.findByRole('dialog', { name: /create workspace/i });
        expect(within(again).getByLabelText(/workspace name/i)).toHaveValue('');
        await screen.user.click(within(again).getByRole('button', { name: /^cancel$/i }));
        await expectSessionNotReread(before);
      },
    },
  ];

  it.each(ROWS)('$name in A leaves organization B untouched', async (row) => {
    orgModel.seedRoster('org-1');
    addSecondOrganization('owner');
    // Created before render: the fixture helper swaps the client's global token.
    await createInvitationAsOwner('pending.person@example.com', 'viewer');
    const screen = renderProductionApp(<App />, '/settings', { token: 'test-token', probe: <Probes /> });
    await settled(screen, 'org-1', 'owner');

    // Visit B so its members, invitations and workspaces are cached, then come back to A.
    await switchOrganization(screen, 'Second Org');
    await settled(screen, 'org-2', 'owner');
    await screen.findByText('zed.second@example.com');
    await waitFor(() => expect(count('GET', /\/organizations\/org-2\/invitations$/)).toBeGreaterThan(0));
    await switchOrganization(screen, 'Demo Org');
    await settled(screen, 'org-1', 'owner');
    await screen.findByRole('table', { name: /pending invitations/i });

    const parts = ['members', 'invitations', 'workspaces'] as const;
    const b = Object.fromEntries(
      parts.map((part) => {
        const state = cache!.getQueryState(['organizations', 'org-2', part]);
        // Liveness: B's entry really is cached, so "untouched" is a measurement.
        expect(state?.status, `B ${part} cached`).toBe('success');
        return [part, { at: state!.dataUpdatedAt, gets: count('GET', new RegExp(`/organizations/org-2/${part}$`)) }];
      }),
    ) as Record<(typeof parts)[number], { at: number; gets: number }>;
    const before: Snapshot = {
      aMembers: aMembersGets(),
      aInvitations: aInvitationGets(),
      aWorkspaces: count('GET', /\/organizations\/org-1\/workspaces$/),
      session: sessionGets(),
    };

    await row.act(screen);
    await row.expectA(screen, before);

    for (const part of parts) {
      const state = cache!.getQueryState(['organizations', 'org-2', part]);
      expect(state?.isInvalidated, `B ${part} invalidated`).toBe(false);
      expect(state?.status, `B ${part} reset or removed`).toBe('success');
      expect(state?.dataUpdatedAt, `B ${part} data replaced`).toBe(b[part].at);
      expect(count('GET', new RegExp(`/organizations/org-2/${part}$`)), `B ${part} refetched`).toBe(b[part].gets);
    }
  });
});

// ---- §81: sign out, then another user signs in on the same tab --------------------

/**
 * Records every piece of text ADDED to the page while it runs, so a value that is
 * shown for a single commit and replaced in the next is still caught.
 */
function watchForText(needles: string[]) {
  const hits: string[] = [];
  const check = (text: string | null | undefined) => {
    for (const needle of needles) if (text?.includes(needle)) hits.push(needle);
  };
  const observer = new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'characterData') check(record.target.textContent);
      record.addedNodes.forEach((node) => check(node.textContent));
    }
  });
  observer.observe(document.body, { subtree: true, childList: true, characterData: true });
  return {
    hits,
    stop: () => {
      observer.takeRecords().forEach((record) => record.addedNodes.forEach((node) => check(node.textContent)));
      observer.disconnect();
    },
  };
}

describe('a sign-out ends the previous user\'s data in this tab (§81)', () => {
  it('the next user never sees, or is served from cache, anything of the previous user', async () => {
    // Bob owns an organization Alice does not belong to; Alice belongs only to Demo Org.
    orgModel.addOrganization({ id: 'org-b', name: 'Bayside Bakery', slug: 'bayside' }, [
      { id: 'ws-b', name: 'Bayside Workspace', slug: 'bayside-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    orgModel.addUser({ id: 'user-bob', email: 'bob@example.com', full_name: 'Bob Baker', password: 'bob password 1' });
    orgModel.addUser({ id: 'user-bea', email: 'bea.bayside@example.com', full_name: 'Bea Bayside' });
    orgModel.addUser({ id: 'user-alice', email: 'alice@example.com', full_name: 'Alice Next', password: 'alice password 1' });
    orgModel.setMembership('org-b', 'user-bob', 'owner');
    orgModel.setMembership('org-b', 'user-bea', 'viewer');
    orgModel.setMembership('org-1', 'user-alice', 'marketer');

    const screen = renderProductionApp(<App />, '/settings', { token: orgModel.signIn('user-bob'), probe: <Probes /> });
    await settled(screen, 'org-b', 'owner');
    expect(screen.getByText('bea.bayside@example.com')).toBeInTheDocument();
    await screen.findByRole('table', { name: /pending invitations/i }).catch(() => screen.findByText(/no pending invitations/i));
    // Liveness: Bob's organization data really is cached in this tab before he signs out.
    expect(cache!.getQueryState(['organizations', 'org-b', 'members'])?.status).toBe('success');
    const bobMembersAt = cache!.getQueryState(['organizations', 'org-b', 'members'])!.dataUpdatedAt;

    const [account] = screen.getAllByRole('button', { name: /account menu/i });
    await screen.user.click(account!);
    await screen.user.click(await screen.findByRole('menuitem', { name: /sign out/i }));
    const email = await screen.findByLabelText(/^email/i);

    const watch = watchForText(['Bayside Bakery', 'Bayside Workspace', 'bea.bayside@example.com', 'bob@example.com']);
    const signInStartedAt = Date.now();
    const requestsBefore = orgModel.requests().length;
    const answers: { path: string; status: number }[] = [];
    const onAnswer = ({ request, response }: { request: Request; response: Response }) => {
      answers.push({ path: new URL(request.url).pathname, status: response.status });
    };
    server.events.on('response:mocked', onAnswer);
    await screen.user.type(email, 'alice@example.com');
    await screen.user.type(screen.getByLabelText(/^password/i), 'alice password 1');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));

    // (b) No cache entry survives from before the sign-out: every entry that holds data
    //     or an error got it after Alice began signing in. (Bob's entries were written
    //     well before that, so a surviving one would be caught — shown by bobMembersAt.)
    //     Judged FIRST, at the first member list rendered after sign-in (a point reached
    //     whether or not anything survived), and again once her session has settled.
    expect(bobMembersAt).toBeLessThan(signInStartedAt);
    const expectNothingFromBeforeSignIn = (when: string) => {
      const stale = cache!
        .getQueryCache()
        .getAll()
        .filter(
          (q) =>
            (q.state.dataUpdatedAt > 0 && q.state.dataUpdatedAt < signInStartedAt) ||
            (q.state.errorUpdatedAt > 0 && q.state.errorUpdatedAt < signInStartedAt),
        )
        .map((q) => JSON.stringify(q.queryKey));
      expect(stale, when).toEqual([]);
    };
    await waitFor(() => expect(window.location.pathname).toBe('/settings'));
    await screen.findByRole('table', { name: /members of/i });
    expectNothingFromBeforeSignIn('first member list after sign-in');

    await settled(screen, 'org-1', 'marketer');
    await screen.findByText('alice@example.com', { selector: 'td' });
    watch.stop();
    server.events.removeListener('response:mocked', onAnswer);
    expectNothingFromBeforeSignIn('settled session');
    expect(cache!.getQueryState(['organizations'])!.dataUpdatedAt).toBeGreaterThanOrEqual(signInStartedAt);

    // (a) Nothing of Bob's rendered for Alice, not even for one commit.
    expect(watch.hits).toEqual([]);

    // (c) The organization list is Alice's alone, and her organization is the active one.
    expect((cache!.getQueryData(['organizations']) as { id: string }[]).map((o) => o.id)).toEqual(['org-1']);
    expect(localStorage.getItem('signalnest-active-org')).toBe('org-1');

    // (d) Disclosed pre-existing residual #6 (same on base 5cb44bf; WorkspaceContext is
    //     outside 6B-3B): WorkspaceContext keeps the previous user's selected organization
    //     and workspace ids through a sign-out, so Alice's session fires a few requests
    //     naming Bob's org-b / ws-b before the fallback moves her to org-1. Every one of
    //     them carried HER session, and a membership-aware backend refused it — so no
    //     data can cross from Bob to Alice.
    const afterSignIn = orgModel.requests().slice(requestsBefore);
    const namingBob = afterSignIn.filter((r) => /org-b|ws-b/.test(r.path));
    expect(namingBob.map((r) => r.userId).filter((id) => id !== 'user-alice')).toEqual([]);
    const bobAnswers = answers.filter((a) => /org-b|ws-b/.test(a.path));
    expect(bobAnswers.filter((a) => a.status !== 403)).toEqual([]);
    expect(afterSignIn.some((r) => r.method === 'GET' && r.path.endsWith('/organizations') && r.userId === 'user-alice')).toBe(true);
  });
});

// ---- §19 / §81: a session re-read that is overtaken by a sign-out ------------------

describe('a stale-refusal session re-read never outlives a sign-out (§19, §81)', () => {
  async function staleRefreshInFlight() {
    orgModel.seedRoster('org-1');
    const screen = renderProductionApp(<App />, '/settings', { token: 'test-token', probe: <Probes /> });
    await settled(screen, 'org-1', 'owner');
    // From now on every /auth/me waits: the one onStale sends is the in-flight re-read.
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let heldReads = 0;
    server.use(
      http.get(P('/auth/me'), async () => {
        heldReads += 1;
        await held;
        return undefined;
      }),
    );
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'admin');
    await screen.user.click(within(memberRow(screen, 'Olivia Owner')).getByRole('button', { name: /^remove /i }));
    const confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
    await waitFor(() => expect(heldReads).toBeGreaterThan(0));
    // The user signs out while that re-read is still in flight.
    const [account] = screen.getAllByRole('button', { name: /account menu/i });
    await screen.user.click(account!);
    await screen.user.click(await screen.findByRole('menuitem', { name: /sign out/i }));
    await screen.findByLabelText(/^email/i);
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    return { screen, release };
  }

  it('stays signed out when the overtaken answer arrives', async () => {
    const { screen, release } = await staleRefreshInFlight();
    act(() => release());
    // Give the stale answer every chance to land.
    await new Promise((resolve) => setTimeout(resolve, 300));
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    // No membership role is back (the org id itself survives a sign-out: residual #6).
    expect(screen.getByTestId('m4-role-probe')).toHaveTextContent(/:$/);
    expect(screen.getByLabelText(/^email/i)).toBeInTheDocument();
  });

  it('a refusal that lands after a sign-out in ANOTHER tab never restores the session', async () => {
    orgModel.seedRoster('org-1');
    const screen = renderProductionApp(<App />, '/settings', { token: 'test-token', probe: <Probes /> });
    await settled(screen, 'org-1', 'owner');
    // The removal's answer (a stale 403) is held while the user signs out in another tab.
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let removals = 0;
    server.use(
      http.delete(P('/organizations/org-1/members/:userId'), async () => {
        removals += 1;
        await held;
        return undefined;
      }),
    );
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'admin');
    await screen.user.click(within(memberRow(screen, 'Olivia Owner')).getByRole('button', { name: /^remove /i }));
    const confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
    await waitFor(() => expect(removals).toBe(1));
    // Another tab of the same browser signs out: the stored session (shared) is gone.
    localStorage.removeItem('signalnest-token');
    const sessionReads = orgModel.count('GET', /\/auth\/me$/);

    act(() => release());
    expect(await screen.findByText(/could not remove member/i)).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 300));
    // This tab's stale refusal must not read a session back and store it again.
    expect(orgModel.count('GET', /\/auth\/me$/)).toBe(sessionReads);
    expect(localStorage.getItem('signalnest-token')).toBeNull();
  });

  it('keeps the NEXT user signed in as themselves when the overtaken answer arrives', async () => {
    orgModel.addUser({ id: 'user-alice', email: 'alice@example.com', full_name: 'Alice Next', password: 'alice password 1' });
    orgModel.setMembership('org-1', 'user-alice', 'viewer');
    const { screen, release } = await staleRefreshInFlight();
    await screen.user.type(screen.getByLabelText(/^email/i), 'alice@example.com');
    await screen.user.type(screen.getByLabelText(/^password/i), 'alice password 1');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    await waitFor(() => expect(localStorage.getItem('signalnest-token')).toMatch(/^access-user-alice-/));
    const alicesToken = localStorage.getItem('signalnest-token');
    act(() => release());
    await new Promise((resolve) => setTimeout(resolve, 300));
    // Demo's overtaken answer never replaces Alice's session.
    expect(localStorage.getItem('signalnest-token')).toBe(alicesToken);
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:viewer'));
  });
});

describe('an expired session ends cleanly (AuthContext 401 handler)', () => {
  it('signs the user out on the next refused request, clearing the session', async () => {
    const screen = await renderSettingsAs('owner');
    orgModel.expireSession('test-token');
    // Any organization request now answers 401; a role change is one.
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Viewer' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));

    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    expect(screen.getByTestId('m4-role-probe')).toHaveTextContent(/:$/);
  });

  it('a 401 while already signed out changes nothing on the page (the handler\'s token guard)', async () => {
    // Evidence for the guard's equivalence: signed out, a wrong password answers 401
    // and the invitation in progress stays exactly where it was.
    addExistingInvitee();
    const token = await createInvitationAsOwner('ola@example.com', 'reviewer').then((i) => i.token);
    const screen = renderProductionApp(<App />, `/invite#token=${token}`, { probe: <Probes /> });
    await screen.findByText(/create your account to join demo org/i);
    await screen.user.click(screen.getByRole('button', { name: /sign in to accept/i }));
    await screen.user.type(await screen.findByLabelText(/^password/i), 'wrong password');
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    expect(await screen.findByText(/incorrect email or password/i)).toBeInTheDocument();
    expect(screen.getByLabelText(/^email/i)).toHaveValue('ola@example.com');
    expect(window.location.pathname).toBe('/invite');
    expect(localStorage.getItem('signalnest-token')).toBeNull();
  });
});

function addExistingInvitee() {
  orgModel.addUser({ id: 'user-ola', email: 'ola@example.com', full_name: 'Ola Returning', password: 'ola password 12' });
}

// ---- dialog state after a refusal ----------------------------------------------------

describe('a refused role change leaves nothing behind when the dialog is reopened', () => {
  it('reopening after a failure starts clean', async () => {
    const screen = await renderSettingsAs('owner');
    server.use(
      http.put(P('/organizations/org-1/members/:userId/role'), () =>
        HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 }),
      ),
    );
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    let dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Reviewer' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
    expect(await within(dialog).findByText(/the server stumbled/i)).toBeInTheDocument();
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    dialog = await screen.findByRole('dialog', { name: /change role/i });
    expect(within(dialog).queryByText(/the server stumbled/i)).not.toBeInTheDocument();
    expect(within(dialog).getByRole('combobox')).toHaveTextContent('Marketer');
    expect(within(dialog).getByRole('button', { name: /save role/i })).toBeDisabled();
  });
});

// ---- dialog state: pending guards, one-time result, form reset ---------------------

describe('invite and change-role dialogs keep their state honest', () => {
  it('the invite dialog cannot be dismissed while the invitation is being created', async () => {
    orgModel.setNextInvitationToken('abc123');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(P('/organizations/org-1/invitations'), async () => {
        await held;
        return undefined;
      }),
    );
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'nia.new@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    expect(await within(dialog).findByRole('button', { name: /creating/i })).toBeDisabled();
    // Escape while the request is out would leave the one-time link nowhere to go.
    await screen.user.keyboard('{Escape}');
    expect(screen.getByRole('dialog', { name: /invite member/i })).toBeInTheDocument();
    act(() => release());
    const result = await screen.findByRole('dialog', { name: /invitation created/i });
    expect(within(result).getByLabelText(/invitation link/i)).toHaveValue(`${ORIGIN}/invite#token=abc123`);
  });

  it('reopening the invite dialog after Done shows a fresh, empty form, never the old link', async () => {
    orgModel.setNextInvitationToken('abc123');
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    let dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'nia.new@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    const result = await screen.findByRole('dialog', { name: /invitation created/i });
    await screen.user.click(within(result).getByRole('button', { name: /^done$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    dialog = await screen.findByRole('dialog', { name: /invite member/i });
    expect(screen.queryByRole('dialog', { name: /invitation created/i })).not.toBeInTheDocument();
    expect(document.documentElement.outerHTML).not.toContain('abc123');
    expect(within(dialog).getByLabelText(/^email/i)).toHaveValue('');
    expect(within(dialog).getByRole('combobox')).toHaveTextContent('Viewer');
  });

  it('the change-role dialog cannot be dismissed while the change is in flight', async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.put(P('/organizations/org-1/members/:userId/role'), async () => {
        await held;
        return undefined;
      }),
    );
    const screen = await renderSettingsAs('owner');
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Viewer' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
    expect(await within(dialog).findByRole('button', { name: /saving/i })).toBeDisabled();
    await screen.user.keyboard('{Escape}');
    expect(screen.getByRole('dialog', { name: /change role/i })).toBeInTheDocument();
    act(() => release());
    expect(await screen.findByText(/role updated/i)).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /change role/i })).not.toBeInTheDocument());
    expect(within(memberRow(screen, 'Mara Marketer')).getByText('Viewer')).toBeInTheDocument();
  });
});

// ---- an open editor dialog closes when a session re-read takes the right away --------

describe('editor dialogs close when a session re-read revokes the right (latches)', () => {
  const LATCHES = [
    {
      site: 'Settings.tsx:54 create-workspace dialog',
      open: async (screen: Screen) => {
        await screen.user.click(screen.getByRole('button', { name: /^(\+\s*)?new$/i }));
        return screen.findByRole('dialog', { name: /create workspace/i });
      },
      page: null as RegExp | null,
    },
    {
      site: 'Locations.tsx:30-34 location dialog',
      open: async (screen: Screen) => {
        await screen.findByText('Dallas');
        await screen.user.click(screen.getAllByRole('button', { name: /^edit & service area$/i })[0]!);
        return screen.findByRole('dialog', { name: /edit location/i });
      },
      page: /^locations$/i,
    },
    {
      site: 'ScoutRequests.tsx:45 new-scout dialog',
      open: async (screen: Screen) => {
        await screen.findByText('Dallas demand scan');
        await screen.user.click(screen.getByRole('button', { name: /new scout request/i }));
        return screen.findByRole('dialog');
      },
      page: /^scout requests$/i,
    },
    {
      site: 'CampaignContext.tsx:405 add-context dialog',
      open: async (screen: Screen) => {
        await screen.findByText(/no products yet/i);
        await screen.user.click(screen.getAllByRole('button', { name: /^add product$/i })[0]!);
        return screen.findByRole('dialog');
      },
      page: /^campaign context$/i,
    },
  ];

  it('Locations.tsx:30-34 with no dialog open: the gate closes, and nothing opens', async () => {
    const screen = await renderSettingsAs('owner');
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'viewer');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/auth/me'), async () => {
        await held;
        return undefined;
      }),
    );
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
    const confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const [link] = screen.getAllByRole('link', { name: /^locations$/i });
    await screen.user.click(link!);
    await screen.findByText('Dallas');
    expect(screen.getAllByRole('button', { name: /^add location$/i }).length).toBeGreaterThan(0);

    act(() => release());
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:viewer'));
    await waitFor(() => expect(screen.queryAllByRole('button', { name: /^add location$/i })).toHaveLength(0));
    expect(screen.getAllByRole('button', { name: /^view details & service area$/i }).length).toBeGreaterThan(0);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('Settings.tsx:54 a dropped create-workspace dialog does not spring back when the right returns (organization switch)', async () => {
    orgModel.addOrganization({ id: 'org-2', name: 'Second Org', slug: 'second-org' }, [
      { id: 'ws-2', name: 'Second Workspace', slug: 'second-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    orgModel.setMembership('org-2', 'user-1', 'owner');
    const screen = await renderSettingsAs('owner');
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'viewer');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/auth/me'), async () => {
        await held;
        return undefined;
      }),
    );
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
    const confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await screen.user.click(screen.getByRole('button', { name: /^(\+\s*)?new$/i }));
    expect(await screen.findByRole('dialog', { name: /create workspace/i })).toBeInTheDocument();

    act(() => release());
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:viewer'));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // The right returns with the other organization, where the actor is an owner.
    const [trigger] = screen.getAllByRole('combobox', { name: /^organization$/i });
    await screen.user.click(trigger!);
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Second Org' }));
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-2:owner'));
    // Positive instance: the gate is open again here…
    expect(await screen.findByRole('button', { name: /^(\+\s*)?new$/i })).toBeEnabled();
    // …and the dialog dropped in Demo Org did not come back by itself.
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(orgModel.count('POST', /\/organizations\/org-2\/workspaces$/)).toBe(0);
  });

  /** Queue the session re-reads: each one waits for its own release, then answers from the model. */
  function holdSessionReads() {
    const releases: (() => void)[] = [];
    let reads = 0;
    server.use(
      http.get(P('/auth/me'), async () => {
        reads += 1;
        await new Promise<void>((resolve) => releases.push(resolve));
        return undefined;
      }),
    );
    return {
      reads: () => reads,
      releaseNext: () => act(() => releases.shift()!()),
    };
  }

  it('OrganizationMembers.tsx:101 an open removal confirm closes when a session re-read revokes administration, sends nothing, and does not spring back', async () => {
    const screen = await renderSettingsAs('owner');
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'viewer');
    const session = holdSessionReads();
    // Two stale actions, each refused (403), each with its session re-read held.
    for (const [index, name] of (['Mara Marketer', 'Vic Viewer'] as const).entries()) {
      await screen.user.click(within(memberRow(screen, name)).getByRole('button', { name: /^remove /i }));
      const confirm = await screen.findByRole('dialog', { name: new RegExp(`remove ${name}`, 'i') });
      await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
      await waitFor(() => expect(session.reads()).toBe(index + 1));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    }
    // Still an owner as far as this tab knows: a third removal is offered and opened.
    await screen.user.click(within(memberRow(screen, 'Rhea Reviewer')).getByRole('button', { name: /^remove /i }));
    expect(await screen.findByRole('dialog', { name: /remove rhea reviewer/i })).toBeInTheDocument();

    // The first re-read answers: a viewer. The confirm closes; nothing offers the removal.
    session.releaseNext();
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:viewer'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /remove rhea reviewer/i })).not.toBeInTheDocument());
    expect(screen.queryAllByRole('button', { name: /^remove member$/i })).toHaveLength(0);

    // Administration returns before the second re-read answers: an owner again.
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'owner');
    session.releaseNext();
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:owner'));
    // Positive instance: the row actions are back (by text: an open modal would hide their roles)…
    await waitFor(() => expect(within(memberRow(screen, 'Rhea Reviewer')).getByText('Remove')).toBeInTheDocument());
    await new Promise((resolve) => setTimeout(resolve, 50));
    // …and the dropped confirm did not come back by itself.
    expect(screen.queryByRole('dialog', { name: /remove rhea reviewer/i })).not.toBeInTheDocument();
    expect(count('DELETE', /\/members\/user-reviewer$/)).toBe(0);
    expect(orgModel.memberships().some((m) => m.organization_id === 'org-1' && m.user_id === 'user-reviewer')).toBe(true);
  });

  it('Invitations.tsx:405 a revoke confirm dropped by a demotion does not spring back when administration returns', async () => {
    await createInvitationAsOwner('pending.person@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'viewer');
    const session = holdSessionReads();
    // Two stale actions, each refused (403), each with its session re-read held.
    for (const [index, name] of (['Mara Marketer', 'Vic Viewer'] as const).entries()) {
      await screen.user.click(within(memberRow(screen, name)).getByRole('button', { name: /^remove /i }));
      const confirm = await screen.findByRole('dialog', { name: new RegExp(`remove ${name}`, 'i') });
      await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
      await waitFor(() => expect(session.reads()).toBe(index + 1));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    }
    // Still an owner as far as this tab knows: the revoke confirm opens.
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    await screen.user.click(within(pending).getByRole('button', { name: /revoke invitation for pending.person@example.com/i }));
    expect(await screen.findByRole('dialog', { name: /revoke this invitation/i })).toBeInTheDocument();

    // The first re-read answers: a viewer. The invitation list and its confirm go.
    session.releaseNext();
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:viewer'));
    await waitFor(() => expect(screen.queryByRole('table', { name: /pending invitations/i })).not.toBeInTheDocument());
    expect(screen.queryByRole('dialog', { name: /revoke this invitation/i })).not.toBeInTheDocument();

    // Administration returns before the second re-read answers: an owner again.
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'owner');
    session.releaseNext();
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:owner'));
    // Positive instance: the invitation is listed again (by text: an open modal would hide the table's role)…
    expect(await screen.findByText('pending.person@example.com')).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    // …and the dropped confirm did not come back by itself.
    expect(screen.queryByRole('dialog', { name: /revoke this invitation/i })).not.toBeInTheDocument();
    expect(screen.getByRole('table', { name: /pending invitations/i })).toBeInTheDocument();
    expect(count('DELETE', /\/invitations\//)).toBe(0);
    expect(orgModel.invitations().map((i) => i.email)).toContain('pending.person@example.com');
  });

  /** Two stale removals while the tab still believes it is an owner: two session re-reads, both held. */
  async function twoHeldRereads(screen: Screen) {
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'viewer');
    const session = holdSessionReads();
    for (const [index, name] of (['Mara Marketer', 'Vic Viewer'] as const).entries()) {
      await screen.user.click(within(memberRow(screen, name)).getByRole('button', { name: /^remove /i }));
      const confirm = await screen.findByRole('dialog', { name: new RegExp(`remove ${name}`, 'i') });
      await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
      await waitFor(() => expect(session.reads()).toBe(index + 1));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    }
    return session;
  }
  async function goTo(screen: Screen, page: RegExp) {
    const [link] = screen.getAllByRole('link', { name: page });
    await screen.user.click(link!);
  }
  async function demoteThenPromote(screen: Screen, session: ReturnType<typeof holdSessionReads>, whileViewer?: () => Promise<void>) {
    session.releaseNext();
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:viewer'));
    if (whileViewer) await whileViewer();
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'owner');
    session.releaseNext();
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:owner'));
  }

  it('CampaignContext.tsx:405 a remove confirm dropped by a demotion does not spring back when editing returns', async () => {
    server.use(
      http.get(P('/workspaces/:ws/products'), () =>
        HttpResponse.json([{ id: 'prod-1', kind: 'products', name: 'Signature Cut', description: null }]),
      ),
    );
    const screen = await renderSettingsAs('owner');
    const session = await twoHeldRereads(screen);
    await goTo(screen, /^campaign context$/i);
    await screen.findByText('Signature Cut');
    await screen.user.click(screen.getByRole('button', { name: /^remove product$/i }));
    expect(await screen.findByRole('dialog', { name: /remove this product/i })).toBeInTheDocument();

    await demoteThenPromote(screen, session, async () => {
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    });
    // Positive instance: the editor control is back (by text: an open modal hides roles)…
    await waitFor(() => expect(screen.getAllByLabelText(/^remove product$/i).length).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 50));
    // …and the dropped confirm did not come back by itself.
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(count('DELETE', /\/products\//)).toBe(0);
  });

  it('CampaignContext.tsx:405 an add dialog dropped by a demotion does not spring back when editing returns', async () => {
    const screen = await renderSettingsAs('owner');
    const session = await twoHeldRereads(screen);
    await goTo(screen, /^campaign context$/i);
    await screen.findByText(/no products yet/i);
    await screen.user.click(screen.getAllByRole('button', { name: /^add product$/i })[0]!);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();

    await demoteThenPromote(screen, session, async () => {
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    });
    await waitFor(() => expect(screen.getAllByText(/add product/i).length).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('ScoutRequests.tsx:45 a new-scout dialog dropped by a demotion does not spring back when editing returns', async () => {
    const screen = await renderSettingsAs('owner');
    const session = await twoHeldRereads(screen);
    await goTo(screen, /^scout requests$/i);
    await screen.findByText('Dallas demand scan');
    await screen.user.click(screen.getByRole('button', { name: /new scout request/i }));
    expect(await screen.findByRole('dialog', { name: /new scout request/i })).toBeInTheDocument();

    await demoteThenPromote(screen, session, async () => {
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    });
    await waitFor(() => expect(screen.getAllByText(/new scout request/i).length).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
  });

  it('Locations.tsx:30-34 a viewer can still open the read-only view after the gate closes, and a promotion leaves it open', async () => {
    const screen = await renderSettingsAs('owner');
    const session = await twoHeldRereads(screen);
    await goTo(screen, /^locations$/i);
    await screen.findByText('Dallas');

    await demoteThenPromote(screen, session, async () => {
      await waitFor(() => expect(screen.queryAllByRole('button', { name: /^add location$/i })).toHaveLength(0));
      // After the gate closed, the read-only view still opens (and stays open).
      await screen.user.click(screen.getAllByRole('button', { name: /^view details & service area$/i })[0]!);
      expect(await screen.findByRole('dialog', { name: /location details/i })).toBeInTheDocument();
      await new Promise((resolve) => setTimeout(resolve, 50));
      expect(screen.getByRole('dialog', { name: /location details/i })).toBeInTheDocument();
    });
    // The gate re-opened with the dialog open: it stays open, now as the editor.
    expect(await screen.findByRole('dialog', { name: /edit location/i })).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByRole('dialog', { name: /edit location/i })).toBeInTheDocument();
  });

  it.each(LATCHES)('$site', async (latch) => {
    const screen = await renderSettingsAs('owner');
    // Demoted to viewer behind this screen; the session re-read that reveals it is held.
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'viewer');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let sessionReads = 0;
    server.use(
      http.get(P('/auth/me'), async () => {
        sessionReads += 1;
        await held;
        return undefined;
      }),
    );
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
    const confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
    await waitFor(() => expect(sessionReads).toBeGreaterThan(0));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    if (latch.page) {
      const [link] = screen.getAllByRole('link', { name: latch.page });
      await screen.user.click(link!);
    }
    // Still an owner as far as this tab knows: the editor dialog opens.
    const dialog = await latch.open(screen);
    expect(dialog).toBeInTheDocument();

    act(() => release());
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:viewer'));
    // The latch drops the open dialog; it does not stay open as an editor surface.
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
  });
});

// ---- §72 / §74: accessibility and states ----------------------------------------

describe('accessibility and loading / error / empty states (§72, §74)', () => {
  it('labels the invite form, moves focus into the dialog, and associates errors', async () => {
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await waitFor(() => expect(dialog.contains(document.activeElement)).toBe(true));
    const email = within(dialog).getByLabelText(/^email/i);
    expect(within(dialog).getByRole('combobox')).toHaveAccessibleName(/role/i);
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(email).toHaveAccessibleDescription(/enter an email address/i));
    expect(email).toHaveAttribute('aria-invalid', 'true');
  });

  it('shows loading, then a retryable error, for the member list', async () => {
    let fail = true;
    server.use(
      http.get(P('/organizations/org-1/members'), () =>
        fail
          ? HttpResponse.json({ error: { code: 'internal_error', message: 'Something broke.' } }, { status: 500 })
          : undefined,
      ),
    );
    orgModel.seedRoster('org-1');
    const screen = renderApp(
      <>
        <App />
        <Probes />
      </>,
      { route: '/settings' },
    );
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent(/something broke/i);
    fail = false;
    await screen.user.click(within(alert).getByRole('button', { name: /try again/i }));
    expect(await screen.findByText('mara.marketer@example.com')).toBeInTheDocument();
  });

  it('says so when no invitations are pending', async () => {
    const screen = await renderSettingsAs('admin');
    expect(await screen.findByText(/no pending invitations/i)).toBeInTheDocument();
  });
});

// ---- coverage closure on the 6B-3B lines of the members area (SR/m4/COVERAGE.md) ----

/** A response whose body breaks off mid-read: `fetch` resolves, reading the body throws. */
function brokenBody(status: number) {
  return new HttpResponse(
    new ReadableStream({
      start(controller) {
        controller.error(new TypeError('The connection was reset.'));
      },
    }),
    { status, headers: { 'Content-Type': 'text/plain' } },
  );
}

describe('the members area on its less travelled paths', () => {
  it('an empty member list says so, instead of drawing an empty table', async () => {
    server.use(http.get(P('/organizations/org-1/members'), () => HttpResponse.json([])));
    orgModel.setMembership('org-1', 'user-1', 'owner');
    const screen = renderApp(
      <>
        <App />
        <Probes />
      </>,
      { route: '/settings' },
    );
    expect(await screen.findByText('No members to show.')).toBeInTheDocument();
    expect(screen.queryByRole('table', { name: /members of/i })).not.toBeInTheDocument();
  });

  it('a join date the server sent malformed or empty reads as a dash, never "Invalid Date"', async () => {
    orgModel.seedRoster('org-1');
    server.use(
      http.get(P('/organizations/org-1/members'), () =>
        HttpResponse.json([
          { user_id: 'user-1', email: 'test@example.com', full_name: 'Test User', role: 'owner', created_at: '2026-09-24T10:00:00Z' },
          { user_id: 'user-marketer', email: 'mara.marketer@example.com', full_name: 'Mara Marketer', role: 'marketer', created_at: 'not-a-date' },
          { user_id: 'user-viewer', email: 'vic.viewer@example.com', full_name: 'Vic Viewer', role: 'viewer', created_at: '' },
        ]),
      ),
    );
    const screen = await renderSettingsAs('owner', { roster: false });
    expect(within(memberRow(screen, 'Mara Marketer')).getByText('—')).toBeInTheDocument();
    expect(within(memberRow(screen, 'Vic Viewer')).getByText('—')).toBeInTheDocument();
    expect(within(memberRow(screen, 'Test User')).queryByText('—')).not.toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Invalid Date/);
  });

  it('a role refused by the server is shown on the role field, and only choosing a role clears it (stub: unreachable in a consistent model)', async () => {
    server.use(
      http.post(P('/organizations/org-1/invitations'), () =>
        HttpResponse.json(
          { error: { code: 'invitation_role_forbidden', message: 'You cannot invite a member with a role ranked above your own.' } },
          { status: 403 },
        ),
      ),
    );
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'ria@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));

    const role = within(dialog).getByRole('combobox');
    const email = within(dialog).getByLabelText(/^email/i);
    await waitFor(() => expect(role).toHaveAccessibleDescription(/you cannot invite with that role/i));
    expect(role).toHaveAttribute('aria-invalid', 'true');
    expect(email).not.toHaveAttribute('aria-invalid', 'true');
    // One message, on the role field: not repeated as a form-level error.
    expect(within(dialog).getAllByRole('alert')).toHaveLength(1);
    // A 403 re-reads the session; the actor is still an owner, so the dialog stays.
    await waitFor(() => expect(orgModel.count('GET', /\/auth\/me$/)).toBeGreaterThan(0));
    expect(screen.getByRole('dialog', { name: /invite member/i })).toBe(dialog);

    // Editing the OTHER field leaves the role error in place.
    await screen.user.type(email, 'n');
    expect(role).toHaveAccessibleDescription(/you cannot invite with that role/i);
    // Choosing a role clears it.
    await screen.user.click(role);
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Reviewer' }));
    await waitFor(() => expect(role).not.toHaveAccessibleDescription(/you cannot invite with that role/i));
    expect(role).not.toHaveAttribute('aria-invalid', 'true');
  });

  it('an email already invited is shown on the email field, and only editing the email clears it', async () => {
    await createInvitationAsOwner('dup@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    const email = within(dialog).getByLabelText(/^email/i);
    await screen.user.type(email, 'dup@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(email).toHaveAccessibleDescription(/an invitation is already pending/i));
    expect(orgModel.invitations()).toHaveLength(1);

    // Choosing a role leaves the email error in place.
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Marketer' }));
    expect(email).toHaveAccessibleDescription(/an invitation is already pending/i);
    // Editing the email clears it.
    await screen.user.type(email, 'x');
    await waitFor(() => expect(email).not.toHaveAccessibleDescription(/an invitation is already pending/i));
    expect(email).not.toHaveAttribute('aria-invalid', 'true');
  });

  it('when the browser refuses the clipboard, it says so and selects the link for a manual copy', async () => {
    orgModel.setNextInvitationToken('tok-manual-copy');
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'manual@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    const result = await screen.findByRole('dialog', { name: /invitation created/i });
    const link = within(result).getByLabelText(/invitation link/i) as HTMLInputElement;
    const expected = `${ORIGIN}/invite#token=tok-manual-copy`;

    const refuse = vi
      .spyOn(navigator.clipboard, 'writeText')
      .mockRejectedValueOnce(new DOMException('Write permission denied.', 'NotAllowedError'));
    try {
      await screen.user.click(within(result).getByRole('button', { name: /copy invitation link/i }));
      expect(refuse).toHaveBeenCalledWith(expected);
    } finally {
      refuse.mockRestore();
    }
    expect(await within(result).findByText(/couldn.t copy automatically/i)).toBeInTheDocument();
    expect(within(result).queryByText(/link copied/i)).not.toBeInTheDocument();
    expect(document.activeElement).toBe(link);
    expect([link.selectionStart, link.selectionEnd]).toEqual([0, expected.length]);
  });

  it('a Copy activated without moving focus (assistive technology) still selects a link that already had focus', async () => {
    orgModel.setNextInvitationToken('tok-at-copy');
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'at@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    const result = await screen.findByRole('dialog', { name: /invitation created/i });
    const link = within(result).getByLabelText(/invitation link/i) as HTMLInputElement;
    act(() => link.focus());
    link.setSelectionRange(0, 0);
    const refuse = vi
      .spyOn(navigator.clipboard, 'writeText')
      .mockRejectedValueOnce(new DOMException('Write permission denied.', 'NotAllowedError'));
    try {
      // A virtual click: the button is activated, focus stays on the link.
      fireEvent.click(within(result).getByRole('button', { name: /copy invitation link/i }));
      expect(await within(result).findByText(/couldn.t copy automatically/i)).toBeInTheDocument();
    } finally {
      refuse.mockRestore();
    }
    expect(document.activeElement).toBe(link);
    expect([link.selectionStart, link.selectionEnd]).toEqual([0, link.value.length]);
  });

  it('focusing the link selects all of it', async () => {
    orgModel.setNextInvitationToken('tok-focus');
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'focus@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    const result = await screen.findByRole('dialog', { name: /invitation created/i });
    const link = within(result).getByLabelText(/invitation link/i) as HTMLInputElement;
    link.setSelectionRange(0, 0);
    expect(document.activeElement).not.toBe(link);
    act(() => link.focus());
    expect([link.selectionStart, link.selectionEnd]).toEqual([0, link.value.length]);
  });

  it('a role change that never reaches the server shows the client\'s own message, and keeps the dialog', async () => {
    const screen = await renderSettingsAs('owner');
    server.use(http.put(P('/organizations/org-1/members/:userId/role'), () => HttpResponse.error()));
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Viewer' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
    expect(await within(dialog).findByText('Network error — could not reach the server.')).toBeInTheDocument();
    expect(screen.getByRole('dialog', { name: /change role/i })).toBe(dialog);
    expect(orgModel.memberships().find((m) => m.organization_id === 'org-1' && m.user_id === 'user-marketer')?.role).toBe('marketer');
  });

  it('a removal whose answer breaks off mid-read gets the generic message, never a raw error', async () => {
    const screen = await renderSettingsAs('owner');
    server.use(http.delete(P('/organizations/org-1/members/:userId'), () => brokenBody(500)));
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
    const confirm = await screen.findByRole('dialog', { name: /remove mara marketer/i });
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
    expect(await screen.findByText('Something went wrong. Please try again.')).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/connection was reset/i);
    expect(memberRow(screen, 'Mara Marketer')).toBeInTheDocument();
  });

  it('the change-role form, submitted before a new role is picked, sends nothing', async () => {
    const screen = await renderSettingsAs('owner');
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    expect(within(dialog).getByRole('button', { name: /save role/i })).toBeDisabled();
    // The Save button is disabled; a submit can still come from the form itself.
    fireEvent.submit(dialog.querySelector('form')!);
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(count('PUT', /\/members\/user-marketer\/role$/)).toBe(0);
    expect(screen.getByRole('dialog', { name: /change role/i })).toBe(dialog);
  });
});

describe('member-admin display helpers', () => {
  const coded = (status: number, code: string) =>
    new ApiError(`Server said ${code}.`, status, { error: { code, message: `Server said ${code}.` } }, null);

  it('fall back to generic copy for a failure the API client did not produce', () => {
    for (const describeError of [removeErrorMessage, roleChangeErrorMessage, revokeErrorMessage]) {
      expect(describeError(new TypeError('Failed to fetch'))).toBe('Something went wrong. Please try again.');
      expect(describeError(new ApiError('Network error — could not reach the server.', 0, null, null))).toBe(
        'Network error — could not reach the server.',
      );
      expect(describeError(coded(500, 'internal_error'))).toBe('Server said internal_error.');
      expect(describeError(coded(403, 'permission_denied'))).toMatch(/no longer have permission/i);
    }
    expect(inviteError(new TypeError('Failed to fetch'))).toEqual({
      field: 'form',
      message: 'Something went wrong. Please try again.',
    });
  });

  it('put each invite error on the field it concerns', () => {
    expect(inviteError(coded(409, 'invitation_pending_exists')).field).toBe('email');
    expect(inviteError(coded(409, 'invitation_already_member')).field).toBe('email');
    expect(inviteError(coded(403, 'invitation_owner_forbidden')).field).toBe('role');
    expect(inviteError(coded(403, 'invitation_role_forbidden')).field).toBe('role');
    expect(inviteError(coded(500, 'internal_error'))).toEqual({ field: 'form', message: 'Server said internal_error.' });
    expect(inviteError(new ApiError('Network error — could not reach the server.', 0, null, null)).field).toBe('form');
  });

  it('formatDate: a missing or unreadable date is a dash', () => {
    expect(formatDate(null)).toBe('—');
    expect(formatDate(undefined)).toBe('—');
    expect(formatDate('')).toBe('—');
    expect(formatDate('not-a-date')).toBe('—');
    expect(formatDate('2026-09-24T10:00:00Z')).toMatch(/2026/);
  });
});

describe('an organization switch starts the members area afresh (Settings.tsx:272 key)', () => {
  it('a removal still in flight in one organization never disables or closes a confirm opened in the next', async () => {
    orgModel.addOrganization({ id: 'org-2', name: 'Second Org', slug: 'second-org' }, [
      { id: 'ws-2', name: 'Second Workspace', slug: 'second-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    orgModel.seedRoster('org-2');
    orgModel.setMembership('org-2', 'user-1', 'owner');
    const screen = await renderSettingsAs('owner');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    const heldDeletes: string[] = [];
    server.use(
      http.delete(P('/organizations/org-1/members/:userId'), async ({ params }) => {
        heldDeletes.push(String(params.userId));
        await held;
        return undefined;
      }),
    );
    // Demo Org: a removal is confirmed, and its request hangs.
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
    const first = await screen.findByRole('dialog', { name: /remove mara marketer/i });
    await screen.user.click(within(first).getByRole('button', { name: /^remove member$/i }));
    await waitFor(() => expect(heldDeletes).toEqual(['user-marketer']));
    expect(within(first).getByRole('button', { name: /^remove member$/i })).toBeDisabled();
    // Escape still dismisses it while the request is in flight.
    await screen.user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    // The actor moves to Second Org and starts a removal there.
    const [trigger] = screen.getAllByRole('combobox', { name: /^organization$/i });
    await screen.user.click(trigger!);
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Second Org' }));
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-2:owner'));
    await screen.findByRole('table', { name: /members of second org/i });
    await screen.user.click(within(memberRow(screen, 'Rhea Reviewer')).getByRole('button', { name: /^remove /i }));
    const second = await screen.findByRole('dialog', { name: /remove rhea reviewer/i });
    expect(second).toHaveTextContent(/second org/i);
    // Demo Org's pending removal does not hold this one's buttons…
    expect(within(second).getByRole('button', { name: /^remove member$/i })).toBeEnabled();
    expect(within(second).getByRole('button', { name: /^cancel$/i })).toBeEnabled();

    // …and when it finally answers, it does not close this one.
    act(() => release());
    expect(await screen.findByText(/mara marketer no longer has access to demo org/i)).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByRole('dialog', { name: /remove rhea reviewer/i })).toBe(second);
    expect(within(second).getByRole('button', { name: /^remove member$/i })).toBeEnabled();
    expect(count('DELETE', /\/organizations\/org-2\/members\//)).toBe(0);
    await screen.user.click(within(second).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(count('DELETE', /\/organizations\/org-2\/members\//)).toBe(0);
  });
});

describe('a confirm button pressed while its dialog is still closing does nothing', () => {
  it('remove: Cancel, then "Remove member" during the exit animation sends nothing and reports nothing', async () => {
    const screen = await renderSettingsAs('owner');
    const animations = emulateExitAnimations();
    try {
      await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
      const confirm = await screen.findByRole('dialog', { name: /remove mara marketer/i });
      const removeButton = within(confirm).getByRole('button', { name: /^remove member$/i });
      await screen.user.click(within(confirm).getByRole('button', { name: /^cancel$/i }));
      // Closed, and still on screen for its exit animation.
      expect(confirm).toHaveAttribute('data-state', 'closed');
      expect(removeButton).toBeInTheDocument();
      expect(removeButton).toBeEnabled();
      fireEvent.click(removeButton);
      await new Promise((resolve) => setTimeout(resolve, 50));
      animations.finish();
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(count('DELETE', /\/members\//)).toBe(0);
      expect(screen.queryByText(/could not remove member/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
      expect(memberRow(screen, 'Mara Marketer')).toBeInTheDocument();
    } finally {
      animations.restore();
    }
  });

  it('revoke: Cancel, then "Revoke invitation" during the exit animation sends nothing and reports nothing', async () => {
    await createInvitationAsOwner('closing.person@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    const animations = emulateExitAnimations();
    try {
      await screen.user.click(within(pending).getByRole('button', { name: /revoke invitation for closing.person@example.com/i }));
      const confirm = await screen.findByRole('dialog', { name: /revoke this invitation/i });
      const revokeButton = within(confirm).getByRole('button', { name: /^revoke invitation$/i });
      await screen.user.click(within(confirm).getByRole('button', { name: /^cancel$/i }));
      expect(confirm).toHaveAttribute('data-state', 'closed');
      expect(revokeButton).toBeInTheDocument();
      expect(revokeButton).toBeEnabled();
      fireEvent.click(revokeButton);
      await new Promise((resolve) => setTimeout(resolve, 50));
      animations.finish();
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(count('DELETE', /\/invitations\//)).toBe(0);
      expect(screen.queryByText(/could not revoke/i)).not.toBeInTheDocument();
      expect(screen.queryByText(/something went wrong/i)).not.toBeInTheDocument();
      expect(within(pending).getByText('closing.person@example.com')).toBeInTheDocument();
    } finally {
      animations.restore();
    }
  });
});

// ---- mutation closure: the members area's small behaviours are pinned (SR/m4/CAMPAIGN-V2) ----

/**
 * Every string held in the committed hook state of the rendered React tree. Reads
 * the fiber tree React attaches to the container; DOM nodes and functions are skipped.
 */
function committedHookStrings(container: Element): string[] {
  const key = Object.keys(container).find((k) => k.startsWith('__reactContainer$'));
  if (!key) throw new Error('no React root on this container');
  type Fiber = { child: Fiber | null; sibling: Fiber | null; memoizedState: unknown; tag: number };
  const out: string[] = [];
  const seen = new WeakSet<object>();
  const collect = (value: unknown, depth: number) => {
    if (typeof value === 'string') {
      out.push(value);
      return;
    }
    if (!value || typeof value !== 'object' || depth > 8 || value instanceof Node) return;
    if (seen.has(value)) return;
    seen.add(value);
    for (const v of Object.values(value as Record<string, unknown>)) if (typeof v !== 'function') collect(v, depth + 1);
  };
  const walk = (fiber: Fiber | null) => {
    for (let f = fiber; f; f = f.sibling) {
      // Function components (0), class components (1), forwardRef (11), memo (14/15): their state.
      if ([0, 1, 11, 14, 15].includes(f.tag)) {
        for (let hook = f.memoizedState as { memoizedState?: unknown; baseState?: unknown; next?: unknown } | null; hook && typeof hook === 'object'; hook = (hook.next ?? null) as typeof hook) {
          collect(hook.memoizedState, 0);
          collect(hook.baseState, 0);
          if (hook.next === hook) break;
        }
      }
      walk(f.child);
    }
  };
  walk((container as unknown as Record<string, Fiber>)[key]!);
  return out;
}

describe('the members area keeps its small promises', () => {
  it('an admin sees the organization\'s only owner marked as such, among many members', async () => {
    const screen = await renderSettingsAs('admin');
    // Seeded roster: Olivia is the only owner; Demo Marketer (the actor) is an admin.
    expect(memberRow(screen, 'Olivia Owner')).toHaveTextContent(/only owner/i);
    expect(memberRow(screen, 'Adam Admin')).not.toHaveTextContent(/only owner/i);
  });

  it.each([
    ['owner', 5],
    ['admin', 5],
    ['marketer', 4],
    ['viewer', 4],
  ] as const)('the member table has an actions column only for administrators (%s)', async (role, columns) => {
    const screen = await renderSettingsAs(role);
    expect(within(screen.getByRole('table', { name: /members of/i })).getAllByRole('columnheader')).toHaveLength(columns);
  });

  it('closing a removal or revoke confirm returns focus to the button that opened it', async () => {
    await createInvitationAsOwner('focus.person@example.com', 'viewer');
    await createInvitationAsOwner('second.person@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    const remove = within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i });
    await screen.user.click(remove);
    const confirm = await screen.findByRole('dialog', { name: /remove mara marketer/i });
    await screen.user.click(within(confirm).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(document.activeElement).toBe(remove));

    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    const revoke = within(pending).getByRole('button', { name: /revoke invitation for focus.person@example.com/i });
    await screen.user.click(revoke);
    const revokeConfirm = await screen.findByRole('dialog', { name: /revoke this invitation/i });
    await screen.user.click(within(revokeConfirm).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(document.activeElement).toBe(revoke));
    // A second opener gets the focus back, not the first one.
    const revokeSecond = within(pending).getByRole('button', { name: /revoke invitation for second.person@example.com/i });
    await screen.user.click(revokeSecond);
    const second = await screen.findByRole('dialog', { name: /revoke this invitation/i });
    await screen.user.click(within(second).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(document.activeElement).toBe(revokeSecond));
    expect(count('DELETE', /./)).toBe(0);
  });

  it('a closed invite dialog forgets its server error: reopening starts clean', async () => {
    await createInvitationAsOwner('dup@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    let dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'dup@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(within(dialog).getByLabelText(/^email/i)).toHaveAccessibleDescription(/an invitation is already pending/i));
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());

    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    dialog = await screen.findByRole('dialog', { name: /invite member/i });
    expect(within(dialog).getByLabelText(/^email/i)).not.toHaveAccessibleDescription(/an invitation is already pending/i);
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
  });

  it('a resubmitted invitation clears the previous refusal while it runs', async () => {
    await createInvitationAsOwner('dup@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    const email = within(dialog).getByLabelText(/^email/i);
    await screen.user.type(email, 'dup@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(email).toHaveAccessibleDescription(/an invitation is already pending/i));

    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let posts = 0;
    server.use(
      http.post(P('/organizations/org-1/invitations'), async () => {
        posts += 1;
        await held;
        return undefined;
      }),
    );
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(posts).toBe(1));
    expect(within(dialog).getByRole('button', { name: /creating/i })).toBeDisabled();
    expect(email).not.toHaveAccessibleDescription(/an invitation is already pending/i);
    act(() => release());
    // The same refusal again, now shown afresh.
    await waitFor(() => expect(email).toHaveAccessibleDescription(/an invitation is already pending/i));
  });

  it('a form-level failure is announced on the invite form itself', async () => {
    server.use(
      http.post(P('/organizations/org-1/invitations'), () =>
        HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 }),
      ),
    );
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    const form = dialog.querySelector('form')!;
    expect(form).not.toHaveAttribute('aria-describedby');
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'five@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('The server stumbled.');
    expect(form).toHaveAttribute('aria-describedby', alert.id);
    expect(form).toHaveAccessibleDescription('The server stumbled.');
  });

  it('after Done, no component state holds the one-time link (§14)', async () => {
    orgModel.setNextInvitationToken('tok-state-scan-8f2c');
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'scan@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    const result = await screen.findByRole('dialog', { name: /invitation created/i });
    // Positive instance: while the result is shown, the scan finds the link in state.
    expect(committedHookStrings(screen.container).some((v) => v.includes('tok-state-scan-8f2c'))).toBe(true);
    await screen.user.click(within(result).getByRole('button', { name: /^done$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(committedHookStrings(screen.container).filter((v) => v.includes('tok-state-scan-8f2c'))).toEqual([]);
  });

  it('the change-role form never submits natively, clears its error on a new pick, and reopens clean after success', async () => {
    const screen = await renderSettingsAs('owner');
    const submits: boolean[] = [];
    const onSubmit = (event: Event) => submits.push(event.defaultPrevented);
    window.addEventListener('submit', onSubmit);
    try {
      let failNext = true;
      server.use(
        http.put(P('/organizations/org-1/members/:userId/role'), () => {
          if (!failNext) return undefined;
          failNext = false;
          return HttpResponse.error();
        }),
      );
      const opener = within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i });
      await screen.user.click(opener);
      let dialog = await screen.findByRole('dialog', { name: /change role/i });
      expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
      await screen.user.click(within(dialog).getByRole('combobox'));
      await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Viewer' }));
      await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
      expect(await within(dialog).findByText('Network error — could not reach the server.')).toBeInTheDocument();
      expect(submits).toEqual([true]);
      // A new pick clears the error.
      await screen.user.click(within(dialog).getByRole('combobox'));
      await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Reviewer' }));
      await waitFor(() => expect(within(dialog).queryByText('Network error — could not reach the server.')).not.toBeInTheDocument());
      // Fail again, then succeed: the dialog closes…
      failNext = true;
      await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
      expect(await within(dialog).findByText('Network error — could not reach the server.')).toBeInTheDocument();
      await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
      await waitFor(() => expect(screen.queryByRole('dialog', { name: /change role/i })).not.toBeInTheDocument());
      await waitFor(() => expect(within(memberRow(screen, 'Mara Marketer')).getByText('Reviewer')).toBeInTheDocument());
      expect(submits.every(Boolean)).toBe(true);
      // …and reopens clean: no old error, nothing picked, so nothing to save.
      await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
      dialog = await screen.findByRole('dialog', { name: /change role/i });
      expect(within(dialog).queryByText('Network error — could not reach the server.')).not.toBeInTheDocument();
      expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
      expect(within(dialog).getByRole('button', { name: /save role/i })).toBeDisabled();
    } finally {
      window.removeEventListener('submit', onSubmit);
    }
  });

  it('the create-workspace dialog closes on Cancel and creates nothing', async () => {
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /^(\+\s*)?new$/i }));
    const dialog = await screen.findByRole('dialog', { name: /create workspace/i });
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /create workspace/i })).not.toBeInTheDocument());
    expect(orgModel.count('POST', /\/organizations\/org-1\/workspaces$/)).toBe(0);
  });
});

describe('member-admin stale-refusal and sole-owner helpers', () => {
  const coded = (status: number, code: string) => new ApiError(`Server said ${code}.`, status, { error: { code, message: 'x' } }, null);

  it('isStaleMemberError: any 403, and the two stale-snapshot conflicts, and nothing else', () => {
    expect(isStaleMemberError(coded(403, 'permission_denied'))).toBe(true);
    expect(isStaleMemberError(coded(403, 'member_role_ceiling'))).toBe(true);
    expect(isStaleMemberError(coded(409, 'organization_last_owner'))).toBe(true);
    expect(isStaleMemberError(coded(404, 'member_not_found'))).toBe(true);
    expect(isStaleMemberError(coded(400, 'member_self_removal_forbidden'))).toBe(false);
    expect(isStaleMemberError(coded(500, 'internal_error'))).toBe(false);
    expect(isStaleMemberError(new ApiError('Network error — could not reach the server.', 0, null, null))).toBe(false);
    expect(isStaleMemberError(new TypeError('Failed to fetch'))).toBe(false);
  });

  it('soleOwnerId: the one owner, or null when there are none or several', () => {
    expect(soleOwnerId([{ user_id: 'a', role: 'owner' }, { user_id: 'b', role: 'admin' }, { user_id: 'c', role: 'viewer' }])).toBe('a');
    expect(soleOwnerId([{ user_id: 'a', role: 'owner' }, { user_id: 'b', role: 'owner' }])).toBeNull();
    expect(soleOwnerId([{ user_id: 'b', role: 'admin' }, { user_id: 'c', role: 'viewer' }])).toBeNull();
    expect(soleOwnerId([{ user_id: 'x', role: 'Owner' }, { user_id: 'c', role: 'viewer' }])).toBeNull();
    expect(soleOwnerId([])).toBeNull();
  });
});

// ---- panel-4 closure: frozen derivations, stale session × fresh list, cache effects ----

describe('the members area follows the session and the list as they change', () => {
  async function pickerOptions(screen: Screen, dialog: HTMLElement) {
    await screen.user.click(within(dialog).getByRole('combobox'));
    const options = within(await screen.findByRole('listbox'))
      .getAllByRole('option')
      .map((o) => o.textContent?.trim() ?? '');
    await screen.user.keyboard('{Escape}');
    return options;
  }

  it('a demotion from owner to admin, seen by a session re-read, stops offering Owner (F1)', async () => {
    const screen = await renderSettingsAs('owner');
    // Positive instance: as an owner, the picker offers Owner.
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    let dialog = await screen.findByRole('dialog', { name: /change role/i });
    expect(await pickerOptions(screen, dialog)).toEqual(['Owner', 'Admin', 'Marketer', 'Reviewer', 'Compliance Reviewer', 'Viewer']);
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /change role/i })).not.toBeInTheDocument());

    // Demoted to admin behind this screen; an action on an owner is refused and re-reads the session.
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'admin');
    await screen.user.click(within(memberRow(screen, 'Olivia Owner')).getByRole('button', { name: /^change role for /i }));
    dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Admin' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:admin'));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /change role/i })).not.toBeInTheDocument());

    // Now an admin: Mara is still manageable, and Owner is no longer offered.
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    dialog = await screen.findByRole('dialog', { name: /change role/i });
    expect(await pickerOptions(screen, dialog)).toEqual(['Admin', 'Marketer', 'Reviewer', 'Compliance Reviewer', 'Viewer']);
    // The invite options were the five invitable roles throughout.
    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const invite = await screen.findByRole('dialog', { name: /invite member/i });
    expect(await pickerOptions(screen, invite)).toEqual(['Admin', 'Marketer', 'Reviewer', 'Compliance Reviewer', 'Viewer']);
    expect(orgModel.count('PUT', /\/members\/user-marketer\/role$/)).toBe(0);
  });

  async function awayAndBack(screen: Screen) {
    const [locations] = screen.getAllByRole('link', { name: /^locations$/i });
    await screen.user.click(locations!);
    await screen.findByText('Dallas');
    const [settings] = screen.getAllByRole('link', { name: /^settings$/i });
    await screen.user.click(settings!);
    return screen.findByRole('table', { name: /members of/i });
  }

  it('a stale owner session never offers actions on the organization\'s only owner in a fresh list (F2)', async () => {
    const screen = await renderSettingsAs('owner');
    // Positive instance: while two owners exist, the actor may manage Olivia.
    expect(within(memberRow(screen, 'Olivia Owner')).getByRole('button', { name: /^change role for /i })).toBeInTheDocument();
    expect(within(memberRow(screen, 'Olivia Owner')).getByRole('button', { name: /^remove /i })).toBeInTheDocument();

    // Olivia demotes the actor behind this screen; nothing is refused, so the session is not re-read.
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'admin');
    await awayAndBack(screen);
    // Fresh list: the actor is an admin and Olivia the only owner. Stale session: still an owner.
    await waitFor(() => expect(within(memberRow(screen, 'Demo Marketer')).getByText('Admin')).toBeInTheDocument());
    expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:owner');
    const olivia = memberRow(screen, 'Olivia Owner');
    expect(olivia).toHaveTextContent(/only owner/i);
    expect(within(olivia).queryByRole('button')).not.toBeInTheDocument();
    // Other members are still offered to the (stale) owner session; the server stays the authority.
    expect(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i })).toBeInTheDocument();
    expect(orgModel.count('PUT', /./)).toBe(0);
    expect(orgModel.count('DELETE', /./)).toBe(0);
  });

  it('a stale admin session follows the fresh list: a member promoted to owner meanwhile gets no actions', async () => {
    const screen = await renderSettingsAs('admin');
    expect(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i })).toBeInTheDocument();
    orgModel.changeRoleBehindTheUi('org-1', 'user-marketer', 'owner');
    await awayAndBack(screen);
    await waitFor(() => expect(within(memberRow(screen, 'Mara Marketer')).getByText('Owner')).toBeInTheDocument());
    expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:admin');
    expect(within(memberRow(screen, 'Mara Marketer')).queryByRole('button')).not.toBeInTheDocument();
    // Two owners now: neither is marked as the only one.
    expect(memberRow(screen, 'Mara Marketer')).not.toHaveTextContent(/only owner/i);
    expect(memberRow(screen, 'Olivia Owner')).not.toHaveTextContent(/only owner/i);
  });

  it('"you" is decided by account, not by name: a namesake is an ordinary member', async () => {
    orgModel.addUser({ id: 'user-twin', email: 'twin@example.com', full_name: 'Demo Marketer' });
    orgModel.setMembership('org-1', 'user-twin', 'marketer');
    const screen = await renderSettingsAs('owner');
    const table = screen.getByRole('table', { name: /members of/i });
    const byEmail = (email: string) => within(table).getByText(email).closest('tr') as HTMLElement;
    await waitFor(() => expect(byEmail('twin@example.com')).toBeInTheDocument());
    const self = byEmail('demo@signalnest.dev');
    const twin = byEmail('twin@example.com');
    expect(self).toHaveTextContent('(you)');
    expect(within(self).queryByRole('button')).not.toBeInTheDocument();
    expect(twin).not.toHaveTextContent('(you)');
    expect(within(twin).getByRole('button', { name: /^change role for /i })).toBeInTheDocument();
    expect(within(twin).getByRole('button', { name: /^remove /i })).toBeInTheDocument();
  });

  it('creating an invitation re-reads only the invitation list, not the members or the workspaces', async () => {
    const screen = await renderSettingsAs('owner');
    await waitFor(() => expect(orgModel.count('GET', /\/organizations\/org-1\/invitations$/)).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 50));
    const before = {
      members: orgModel.count('GET', /\/organizations\/org-1\/members$/),
      workspaces: orgModel.count('GET', /\/organizations\/org-1\/workspaces$/),
      invitations: orgModel.count('GET', /\/organizations\/org-1\/invitations$/),
    };
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'only.list@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await screen.findByRole('dialog', { name: /invitation created/i });
    await waitFor(() => expect(orgModel.count('GET', /\/organizations\/org-1\/invitations$/)).toBe(before.invitations + 1));
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(orgModel.count('GET', /\/organizations\/org-1\/members$/)).toBe(before.members);
    expect(orgModel.count('GET', /\/organizations\/org-1\/workspaces$/)).toBe(before.workspaces);
  });

  it('a revoke refused as stale re-reads the invitation list, so a later re-grant shows it fresh', async () => {
    orgModel.seedRoster('org-1');
    orgModel.setMembership('org-1', 'user-1', 'owner');
    await createInvitationAsOwner('keep.pending@example.com', 'viewer');
    await createInvitationAsOwner('gone.meanwhile@example.com', 'viewer');
    // The production client (30 s staleTime): a list is re-read only when marked stale.
    const screen = renderProductionApp(<App />, '/settings', { token: 'test-token', probe: <Probes /> }) as Screen;
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:owner'));
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    await within(pending).findByText('gone.meanwhile@example.com');

    // Behind this screen: the actor is demoted, and another administrator revokes one invitation.
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'viewer');
    orgModel.invitationRevokedBehindTheUi('gone.meanwhile@example.com');
    const releases: (() => void)[] = [];
    let reads = 0;
    server.use(
      http.get(P('/auth/me'), async () => {
        reads += 1;
        await new Promise<void>((resolve) => releases.push(resolve));
        return undefined;
      }),
    );
    // Two stale actions while the tab still believes it is an owner: a revoke, then a removal.
    await screen.user.click(within(pending).getByRole('button', { name: /revoke invitation for keep.pending@example.com/i }));
    const confirm = await screen.findByRole('dialog', { name: /revoke this invitation/i });
    await screen.user.click(within(confirm).getByRole('button', { name: /^revoke invitation$/i }));
    await waitFor(() => expect(reads).toBe(1));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
    const second = await screen.findByRole('dialog', { name: /remove mara marketer/i });
    await screen.user.click(within(second).getByRole('button', { name: /^remove member$/i }));
    await waitFor(() => expect(reads).toBe(2));

    act(() => releases.shift()!());
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:viewer'));
    await waitFor(() => expect(screen.queryByRole('table', { name: /pending invitations/i })).not.toBeInTheDocument());
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'owner');
    act(() => releases.shift()!());
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:owner'));
    const again = await screen.findByRole('table', { name: /pending invitations/i });
    await within(again).findByText('keep.pending@example.com');
    expect(within(again).queryByText('gone.meanwhile@example.com')).not.toBeInTheDocument();
    // Both refused actions changed nothing on the server.
    expect(orgModel.invitations().some((i) => i.email === 'keep.pending@example.com' && !i.revoked_at)).toBe(true);
    expect(orgModel.memberships().some((m) => m.organization_id === 'org-1' && m.user_id === 'user-marketer')).toBe(true);
  });

  it('a returning owner is offered "+ New" for the persisted organization while the organization list loads', async () => {
    orgModel.setMembership('org-1', 'user-1', 'owner');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/organizations'), async () => {
        await held;
        return undefined;
      }),
    );
    const screen = renderApp(
      <>
        <App />
        <Probes />
      </>,
      { route: '/settings', activeOrganization: 'org-1' },
    );
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:owner'));
    expect(await screen.findByRole('button', { name: /^(\+\s*)?new$/i })).toBeEnabled();
    act(() => release());
    await screen.findByRole('table', { name: /members of/i });
    expect(screen.getByRole('button', { name: /^(\+\s*)?new$/i })).toBeEnabled();
  });
});

// ---- panel-4 closure (u5 survivors): mounted pages, lists that never blank, small promises ----

let navigateTo: ((to: string) => void) | null = null;
function NavProbe() {
  const navigate = useNavigate();
  useEffect(() => {
    navigateTo = navigate;
  }, [navigate]);
  return null;
}

const SCHEDULE_404 = http.get(P('/workspaces/:ws/scout-requests/:id/schedule'), () =>
  HttpResponse.json({ error: { code: 'not_found', message: 'no schedule' } }, { status: 404 }),
);
const RUNNING_JOB = http.get(P('/workspaces/:ws/jobs'), () =>
  HttpResponse.json({
    items: [
      {
        id: 'job-running-1',
        job_type: 'scout_run',
        status: 'running',
        attempt_count: 1,
        max_attempts: 3,
        priority: 0,
        scout_request_id: 'scout-loc-dallas',
        location_id: 'loc-dallas',
        last_error_code: null,
        result_summary: null,
        scheduled_for: null,
        started_at: '2026-06-01T12:00:00Z',
        completed_at: null,
        cancel_requested_at: null,
        cancelled_at: null,
        created_at: '2026-06-01T12:00:00Z',
        updated_at: '2026-06-01T12:00:00Z',
      },
    ],
    total: 1,
    limit: 10,
    offset: 0,
  }),
);

describe('a page already open follows a session re-read that revokes editing', () => {
  const buttonsNamed = (screen: Screen, name: RegExp) => screen.queryAllByRole('button', { name });
  const PAGES = [
    {
      page: 'Onboarding (finish)',
      open: async (screen: Screen) => {
        act(() => navigateTo!('/onboarding'));
        await screen.user.click(await screen.findByRole('button', { name: /save & continue/i }));
        const name = await screen.findByLabelText(/business \/ brand name/i);
        if (!(name as HTMLInputElement).value) await screen.user.type(name, 'Mounted Brand');
        while (screen.queryByRole('button', { name: /save & continue/i })) {
          await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));
        }
      },
      controls: [/finish onboarding/i],
    },
    {
      page: 'Opportunity detail',
      open: async (screen: Screen) => {
        act(() => navigateTo!('/opportunities/opp-loc-dallas-0'));
        await screen.findByRole('heading', { name: /dallas customers want faster delivery 1/i });
      },
      controls: [/^save$/i, /^monitor$/i, /^mark actioned$/i, /^ignore$/i],
    },
    {
      page: 'Scout request detail and its jobs',
      handlers: [SCHEDULE_404, RUNNING_JOB],
      open: async (screen: Screen) => {
        act(() => navigateTo!('/scout-requests/scout-loc-dallas'));
        await screen.findByRole('heading', { name: 'Dallas demand scan' });
        await screen.findByText(/^running$/i);
      },
      controls: [/run now/i, /^pause$/i, /^cancel$/i],
    },
  ];

  it.each(PAGES)('$page', async (row) => {
    if (row.handlers) server.use(...row.handlers);
    orgModel.seedRoster('org-1');
    orgModel.setMembership('org-1', 'user-1', 'owner');
    const screen = renderApp(
      <>
        <App />
        <Probes />
        <NavProbe />
      </>,
      { route: '/settings' },
    ) as Screen;
    await settled(screen, 'org-1', 'owner');
    orgModel.changeRoleBehindTheUi('org-1', 'user-1', 'viewer');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/auth/me'), async () => {
        await held;
        return undefined;
      }),
    );
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
    const confirm = await screen.findByRole('dialog', { name: /remove mara marketer/i });
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await row.open(screen);
    // Still an owner as far as this tab knows: the editor controls are there (positive instance).
    for (const control of row.controls) expect(buttonsNamed(screen, control).length, String(control)).toBeGreaterThan(0);

    act(() => release());
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:viewer'));
    for (const control of row.controls) {
      await waitFor(() => expect(buttonsNamed(screen, control), String(control)).toHaveLength(0));
    }
  });
});

describe('a list being refreshed keeps showing what it knows (no blank flash)', () => {
  function holdGets(pattern: RegExp) {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let armed = false;
    let seen = 0;
    const onStart = ({ request }: { request: Request }) => {
      if (armed && request.method === 'GET' && pattern.test(new URL(request.url).pathname)) seen += 1;
    };
    server.events.on('request:start', onStart);
    server.use(
      http.get(P('/organizations/:orgId/:part'), async ({ request }) => {
        if (armed && pattern.test(new URL(request.url).pathname)) await held;
        return undefined;
      }),
    );
    return {
      arm: () => {
        armed = true;
      },
      seen: () => seen,
      release: () => {
        server.events.removeListener('request:start', onStart);
        act(() => release());
      },
    };
  }

  it('after a role change: the member list stays on screen while it is re-read', async () => {
    const screen = await renderSettingsAs('owner');
    const hold = holdGets(/\/organizations\/org-1\/members$/);
    hold.arm();
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Viewer' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
    await waitFor(() => expect(hold.seen()).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.getByText('rhea.reviewer@example.com')).toBeInTheDocument();
    hold.release();
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /change role/i })).not.toBeInTheDocument());
    await waitFor(() => expect(within(memberRow(screen, 'Mara Marketer')).getByText('Viewer')).toBeInTheDocument());
  });

  it('after creating an invitation: the pending list stays on screen while it is re-read', async () => {
    await createInvitationAsOwner('already.pending@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    await screen.findByText('already.pending@example.com');
    const hold = holdGets(/\/organizations\/org-1\/invitations$/);
    hold.arm();
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'brand.new@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await screen.findByRole('dialog', { name: /invitation created/i });
    await waitFor(() => expect(hold.seen()).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.getByText('already.pending@example.com')).toBeInTheDocument();
    hold.release();
    await waitFor(() => expect(screen.getByText('brand.new@example.com')).toBeInTheDocument());
  });

  it('after "already pending": the pending list stays on screen while it is re-read', async () => {
    await createInvitationAsOwner('dup@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    await screen.findByText('dup@example.com');
    const hold = holdGets(/\/organizations\/org-1\/invitations$/);
    hold.arm();
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'dup@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(within(dialog).getByLabelText(/^email/i)).toHaveAccessibleDescription(/already pending/i));
    await waitFor(() => expect(hold.seen()).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 30));
    expect(screen.getAllByText('dup@example.com').length).toBeGreaterThan(0);
    expect(document.querySelector('table[aria-label], table')).not.toBeNull();
    const pendingCell = Array.from(document.querySelectorAll('td')).find((td) => td.textContent === 'dup@example.com');
    expect(pendingCell).toBeDefined();
    hold.release();
  });

  it('after "already a member": the member list stays on screen while it is re-read', async () => {
    const screen = await renderSettingsAs('owner');
    const hold = holdGets(/\/organizations\/org-1\/members$/);
    hold.arm();
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'rhea.reviewer@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(within(dialog).getByLabelText(/^email/i)).toHaveAccessibleDescription(/already belongs to a member/i));
    await waitFor(() => expect(hold.seen()).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 30));
    const memberCell = Array.from(document.querySelectorAll('td')).find((td) => td.textContent === 'mara.marketer@example.com');
    expect(memberCell).toBeDefined();
    hold.release();
  });

  it('after a revoke: the pending list stays on screen while it is re-read', async () => {
    await createInvitationAsOwner('stays@example.com', 'viewer');
    await createInvitationAsOwner('goes@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    await within(pending).findByText('goes@example.com');
    const hold = holdGets(/\/organizations\/org-1\/invitations$/);
    hold.arm();
    await screen.user.click(within(pending).getByRole('button', { name: /revoke invitation for goes@example.com/i }));
    const confirm = await screen.findByRole('dialog', { name: /revoke this invitation/i });
    await screen.user.click(within(confirm).getByRole('button', { name: /^revoke invitation$/i }));
    await waitFor(() => expect(hold.seen()).toBeGreaterThan(0));
    await new Promise((resolve) => setTimeout(resolve, 30));
    const stayCell = Array.from(document.querySelectorAll('td')).find((td) => td.textContent === 'stays@example.com');
    expect(stayCell).toBeDefined();
    hold.release();
    await waitFor(() => expect(screen.queryByText('goes@example.com')).not.toBeInTheDocument());
    expect(screen.getByText('stays@example.com')).toBeInTheDocument();
  });
});

describe('the members area keeps its smaller promises (u5 closure)', () => {
  it('editing any field clears a form-level failure', async () => {
    server.use(
      http.post(P('/organizations/org-1/invitations'), () =>
        HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 }),
      ),
    );
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'five@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The server stumbled.');
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'x');
    await waitFor(() => expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument());
    // …and choosing a role clears it too.
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    expect(await within(dialog).findByRole('alert')).toHaveTextContent('The server stumbled.');
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Reviewer' }));
    await waitFor(() => expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument());
  });

  it('the one-time link result says nothing about copying until a copy is tried', async () => {
    const screen = await renderSettingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    await screen.user.type(within(dialog).getByLabelText(/^email/i), 'quiet@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    const result = await screen.findByRole('dialog', { name: /invitation created/i });
    expect(within(result).queryByText(/couldn.t copy automatically/i)).not.toBeInTheDocument();
    expect(within(result).queryByText(/link copied/i)).not.toBeInTheDocument();
    expect(within(result).getByRole('status')).toHaveTextContent(/^$/);
  });

  it('the revoke confirmation names the invitation it will stop', async () => {
    await createInvitationAsOwner('named@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    await screen.user.click(within(pending).getByRole('button', { name: /revoke invitation for named@example.com/i }));
    const confirm = await screen.findByRole('dialog', { name: /revoke this invitation/i });
    expect(confirm).toHaveAccessibleDescription(
      'The invitation link for named@example.com will stop working. You can create a new invitation afterwards.',
    );
  });

  it.each(['viewer', 'marketer'] as const)('a %s gets no action cells at all (not even "only owner" or hidden text)', async (role) => {
    const screen = await renderSettingsAs(role, { roster: false });
    orgModel.seedRoster('org-1');
    const table = await screen.findByRole('table', { name: /members of/i });
    for (const row of within(table).getAllByRole('row').slice(1)) {
      expect(row.children, row.textContent ?? '').toHaveLength(4);
    }
    expect(within(table).queryByText(/only owner/i)).not.toBeInTheDocument();
    expect(within(table).queryByText(/no actions available/i)).not.toBeInTheDocument();
  });

  it('mounting the members area never throws in the background (return-focus with no opener)', async () => {
    const errors: string[] = [];
    const onError = (event: ErrorEvent) => errors.push(String(event.error ?? event.message));
    // Timers run on Node here: an exception thrown in one surfaces on the process, not the window.
    const node = (globalThis as unknown as { process: { on(e: string, f: (err: unknown) => void): void; off(e: string, f: (err: unknown) => void): void } }).process;
    const onUncaught = (err: unknown) => errors.push(String(err));
    window.addEventListener('error', onError);
    node.on('uncaughtException', onUncaught);
    try {
      const screen = await renderSettingsAs('owner');
      await new Promise((resolve) => setTimeout(resolve, 50));
      // A dialog closed by the latch (not by its own buttons) has an opener; the first mount has none.
      await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^remove /i }));
      const confirm = await screen.findByRole('dialog', { name: /remove mara marketer/i });
      await screen.user.click(within(confirm).getByRole('button', { name: /^cancel$/i }));
      await new Promise((resolve) => setTimeout(resolve, 50));
    } finally {
      window.removeEventListener('error', onError);
      node.off('uncaughtException', onUncaught);
    }
    expect(errors).toEqual([]);
  });

  it('isForbidden counts only an API refusal, never a lookalike object', () => {
    expect(isForbidden(new ApiError('Refused.', 403, null, null))).toBe(true);
    expect(isForbidden({ status: 403 })).toBe(false);
    expect(isForbidden(Object.assign(new Error('Refused.'), { status: 403 }))).toBe(false);
    expect(isForbidden(new ApiError('Gone.', 404, null, null))).toBe(false);
  });
});

// ---- final closure: refresh scope and a rejected stored session ----

describe('each refresh re-reads only its own list', () => {
  async function counts() {
    await new Promise((resolve) => setTimeout(resolve, 60));
    return {
      members: orgModel.count('GET', /\/organizations\/org-1\/members$/),
      invitations: orgModel.count('GET', /\/organizations\/org-1\/invitations$/),
      workspaces: orgModel.count('GET', /\/organizations\/org-1\/workspaces$/),
    };
  }

  it('a role change re-reads the member list only', async () => {
    const screen = await renderSettingsAs('owner');
    await waitFor(() => expect(orgModel.count('GET', /\/organizations\/org-1\/invitations$/)).toBeGreaterThan(0));
    const before = await counts();
    await screen.user.click(within(memberRow(screen, 'Mara Marketer')).getByRole('button', { name: /^change role for /i }));
    const dialog = await screen.findByRole('dialog', { name: /change role/i });
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Viewer' }));
    await screen.user.click(within(dialog).getByRole('button', { name: /save role/i }));
    await waitFor(() => expect(screen.queryByRole('dialog', { name: /change role/i })).not.toBeInTheDocument());
    const after = await counts();
    expect(after.members).toBeGreaterThan(before.members);
    expect([after.invitations, after.workspaces]).toEqual([before.invitations, before.workspaces]);
  });

  it('"already pending" re-reads the invitation list only; "already a member" the member list only', async () => {
    await createInvitationAsOwner('dup@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    await screen.findByText('dup@example.com');
    let before = await counts();
    await screen.user.click(screen.getByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    const email = within(dialog).getByLabelText(/^email/i);
    await screen.user.type(email, 'dup@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(email).toHaveAccessibleDescription(/already pending/i));
    let after = await counts();
    expect(after.invitations).toBeGreaterThan(before.invitations);
    expect([after.members, after.workspaces]).toEqual([before.members, before.workspaces]);

    before = after;
    await screen.user.clear(email);
    await screen.user.type(email, 'rhea.reviewer@example.com');
    await screen.user.click(within(dialog).getByRole('button', { name: /create invitation/i }));
    await waitFor(() => expect(email).toHaveAccessibleDescription(/already belongs to a member/i));
    after = await counts();
    expect(after.members).toBeGreaterThan(before.members);
    expect([after.invitations, after.workspaces]).toEqual([before.invitations, before.workspaces]);
  });

  it('a revoke re-reads the invitation list only', async () => {
    await createInvitationAsOwner('goes@example.com', 'viewer');
    const screen = await renderSettingsAs('owner');
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    await within(pending).findByText('goes@example.com');
    const before = await counts();
    await screen.user.click(within(pending).getByRole('button', { name: /revoke invitation for goes@example.com/i }));
    const confirm = await screen.findByRole('dialog', { name: /revoke this invitation/i });
    await screen.user.click(within(confirm).getByRole('button', { name: /^revoke invitation$/i }));
    await waitFor(() => expect(screen.queryByText('goes@example.com')).not.toBeInTheDocument());
    const after = await counts();
    expect(after.invitations).toBeGreaterThan(before.invitations);
    expect([after.members, after.workspaces]).toEqual([before.members, before.workspaces]);
  });
});

describe('a stored session the server no longer accepts', () => {
  it('ends on the sign-in page, not on a loading screen', async () => {
    server.use(
      http.get(P('/auth/me'), () =>
        HttpResponse.json({ error: { code: 'unauthorized', message: 'Session expired.' } }, { status: 401 }),
      ),
    );
    const screen = renderApp(<App />, { route: '/settings' });
    expect(await screen.findByRole('button', { name: /^sign in$/i })).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBeNull();
  });
});

// ---- final adjudication: actions reach the ACTIVE organization, never the first membership ----

describe('member and invitation actions reach the active organization, not the first membership', () => {
  function MembershipOrder() {
    const { memberships } = useAuth();
    return <span data-testid="m4-first-membership">{memberships[0]?.organization_id ?? ''}</span>;
  }

  /**
   * Erin is an OWNER of her own agency (org-a, her FIRST membership) and of Demo Org (org-1);
   * Tom belongs to both. She makes Demo Org the active organization.
   */
  async function erinActiveInDemoOrg(): Promise<Screen> {
    orgModel.addOrganization({ id: 'org-a', name: 'Erin Agency', slug: 'erin-agency' }, [
      { id: 'ws-a', name: 'Agency Workspace', slug: 'agency-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    orgModel.addUser({ id: 'user-erin', email: 'erin@example.com', full_name: 'Erin Owner' });
    orgModel.addUser({ id: 'user-tom', email: 'tom@example.com', full_name: 'Tom Both' });
    orgModel.setMembership('org-a', 'user-erin', 'owner');
    orgModel.setMembership('org-1', 'user-erin', 'owner');
    orgModel.setMembership('org-a', 'user-tom', 'marketer');
    orgModel.setMembership('org-1', 'user-tom', 'marketer');
    const screen = renderProductionApp(<App />, '/settings', {
      token: orgModel.signIn('user-erin'),
      probe: (
        <>
          <Probes />
          <MembershipOrder />
        </>
      ),
    }) as Screen;
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent(/^org-(a|1):owner$/));
    if (!screen.getByTestId('m4-role-probe').textContent!.startsWith('org-1')) {
      await screen.user.click(screen.getAllByRole('combobox', { name: /^organization$/i })[0]!);
      await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: 'Demo Org' }));
    }
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent('org-1:owner'));
    await screen.findByRole('table', { name: /members of demo org/i });
    // The precondition that makes this test mean something: the active organization is NOT the first membership.
    expect(screen.getByTestId('m4-first-membership')).toHaveTextContent(/^org-a$/);
    return screen;
  }
  const writesBy = (user: string, re: RegExp) =>
    orgModel
      .requests()
      .filter((r) => r.userId === user && r.method === 'DELETE' && re.test(r.path))
      .map((r) => r.path.replace(API_PREFIX, ''));
  const roleOf = (org: string, user: string) =>
    orgModel.memberships().find((m) => m.organization_id === org && m.user_id === user)?.role ?? null;

  it('revoking an invitation in the active organization revokes it there, and only there', async () => {
    const demo = await createInvitationAsOwner('pending.demo@example.com', 'viewer');
    orgModel.invitationCreatedBehindTheUi('org-a', 'pending.agency@example.com', 'viewer', 'user-erin');
    const screen = await erinActiveInDemoOrg();
    const pending = await screen.findByRole('table', { name: /pending invitations/i });
    await within(pending).findByText('pending.demo@example.com');
    expect(within(pending).queryByText('pending.agency@example.com')).not.toBeInTheDocument();

    await screen.user.click(within(pending).getByRole('button', { name: /revoke invitation for pending.demo@example.com/i }));
    const confirm = await screen.findByRole('dialog', { name: /revoke this invitation/i });
    await screen.user.click(within(confirm).getByRole('button', { name: /^revoke invitation$/i }));
    await waitFor(() => expect(writesBy('user-erin', /\/invitations\//)).toHaveLength(1));

    expect(writesBy('user-erin', /\/invitations\//)).toEqual([`/organizations/org-1/invitations/${demo.id}`]);
    expect(writesBy('user-erin', /\/organizations\/org-a\//)).toEqual([]);
    // Revoked in Demo Org, and gone from Demo Org's list.
    const inModel = (email: string) => orgModel.invitations().find((i) => i.email === email)!;
    expect(inModel('pending.demo@example.com').organization_id).toBe('org-1');
    expect(inModel('pending.demo@example.com').revoked_at).not.toBeNull();
    await waitFor(() => expect(within(pending).queryByText('pending.demo@example.com')).not.toBeInTheDocument());
    expect(await screen.findByText(/invitation revoked/i)).toBeInTheDocument();
    // The first membership's organization is untouched.
    expect(inModel('pending.agency@example.com').organization_id).toBe('org-a');
    expect(inModel('pending.agency@example.com').revoked_at).toBeNull();
    expect([roleOf('org-a', 'user-erin'), roleOf('org-a', 'user-tom')]).toEqual(['owner', 'marketer']);
  });

  it('removing a member in the active organization removes them there, and only there', async () => {
    const screen = await erinActiveInDemoOrg();
    const table = screen.getByRole('table', { name: /members of demo org/i });
    const tomRow = within(table).getByText('tom@example.com').closest('tr') as HTMLElement;
    await screen.user.click(within(tomRow).getByRole('button', { name: /^remove tom both$/i }));
    const confirm = await screen.findByRole('dialog', { name: /remove tom both/i });
    expect(confirm).toHaveTextContent(/will lose access to demo org/i);
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove member$/i }));
    await waitFor(() => expect(writesBy('user-erin', /\/members\//)).toHaveLength(1));

    expect(writesBy('user-erin', /\/members\//)).toEqual(['/organizations/org-1/members/user-tom']);
    expect(writesBy('user-erin', /\/organizations\/org-a\//)).toEqual([]);
    // Removed from Demo Org only; still a marketer in the first membership's organization.
    expect(roleOf('org-1', 'user-tom')).toBeNull();
    expect(roleOf('org-a', 'user-tom')).toBe('marketer');
    expect(roleOf('org-a', 'user-erin')).toBe('owner');
    // The success copy names the active organization, and that is where Tom is gone.
    expect(await screen.findByText('Tom Both no longer has access to Demo Org.')).toBeInTheDocument();
    await waitFor(() =>
      expect(within(screen.getByRole('table', { name: /members of demo org/i })).queryByText('tom@example.com')).not.toBeInTheDocument(),
    );
  });
});
