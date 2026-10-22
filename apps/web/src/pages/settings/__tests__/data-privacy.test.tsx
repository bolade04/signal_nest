import { waitFor, within, type RenderResult } from '@testing-library/react';
import type userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import type {
  AccountDeletionOut,
  AccountDeletionPreviewOut,
  AccountDeletionRequest,
  AccountExportOut,
  OrganizationDeletionOut,
  OrganizationDeletionRequest,
} from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

/**
 * P6-PRIV-1 — data export and deletion in Settings, against the stateful backend model.
 *
 * Who is offered what is written from the BACKEND rules, never read back from
 * lib/roles.ts: every signed-in account may export and delete itself
 * (`get_current_user`); organization export and deletion are exactly OWNER
 * (`require_exact_organization_roles(OWNER)`, apps/api/app/privacy/routes.py). The
 * role is configured before render and awaited through a probe, so "absent" never
 * means "not loaded yet". The fixtures below are typed with the generated contract,
 * so a shape the API does not return does not compile.
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const SIX: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];
type Screen = RenderResult & { user: ReturnType<typeof userEvent.setup> };

const VERIFICATION: AccountDeletionOut['verification'] = {
  references_cleared: true,
  subject_type: 'account',
  subject_id: 'user-1',
  tables_checked: 37,
  reference_columns_checked: 10,
  reference_findings: [],
  retained_anonymised: [],
  content_scan: 'operator',
  generated_at: '2026-10-20T00:00:00+00:00',
};
const VERIFIED_EMAIL: AccountDeletionPreviewOut['email_keyed_data'] = {
  status: 'included',
  note: 'Your email address is verified.',
};
const UNVERIFIED_EMAIL: AccountDeletionPreviewOut['email_keyed_data'] = {
  status: 'excluded_unverified',
  note: 'Your email address is not verified.',
};
const LEAVES: AccountDeletionPreviewOut = {
  can_delete: true,
  organizations: [
    { organization_id: 'org-1', organization_name: 'Demo Org', role: 'marketer', member_count: 2, outcome: 'membership_removed' },
  ],
  email_keyed_data: VERIFIED_EMAIL,
};
const BLOCKED: AccountDeletionPreviewOut = {
  can_delete: false,
  organizations: [
    { organization_id: 'org-1', organization_name: 'Demo Org', role: 'owner', member_count: 2, outcome: 'blocked_sole_owner' },
  ],
  email_keyed_data: VERIFIED_EMAIL,
};
const RECEIPT: AccountDeletionOut = {
  user_id: 'user-1',
  organizations: LEAVES.organizations,
  deleted_organizations: [],
  counts: { deleted: { users: 1 }, nulled: {}, anonymised: {} },
  verification: VERIFICATION,
  email_keyed_data: VERIFIED_EMAIL,
};
const ORG_RECEIPT: OrganizationDeletionOut = {
  organization_id: 'org-1',
  workspace_ids: ['ws-1'],
  counts: { deleted: { organizations: 1 }, nulled: {}, anonymised: {} },
  verification: { ...VERIFICATION, subject_type: 'organization', subject_id: 'org-1' },
};
const EXPORT: AccountExportOut = {
  schema_version: 1,
  generated_at: '2026-10-20T00:00:00+00:00',
  subject: { type: 'account', user_id: 'user-1', organization_id: null },
  email_keyed_data: VERIFIED_EMAIL,
  profile: { id: 'user-1', email: 'demo@signalnest.dev' },
  memberships: [],
  sessions: [],
  password_reset_requests: [],
  email_verification_requests: [],
  invitations_received: [],
  invitations_sent: [],
  feedback_submitted: [],
  capability_overrides_set: [],
  audit_events_performed: [],
};

function RoleProbe() {
  const { memberships } = useAuth();
  const { organizationId } = useWorkspace();
  const role = memberships.find((m) => m.organization_id === organizationId)?.role ?? '';
  return <span data-testid="prv-role-probe">{`${organizationId ?? ''}:${role}`}</span>;
}

async function settingsAs(role: ModelRole): Promise<Screen> {
  orgModel.setMembership('org-1', 'user-1', role);
  const screen = renderApp(
    <>
      <App />
      <RoleProbe />
    </>,
    { route: '/settings' },
  );
  // Generous: the first render of the file pays the module warm-up on a loaded host.
  await waitFor(() => expect(screen.getByTestId('prv-role-probe')).toHaveTextContent(`org-1:${role}`), {
    timeout: 5000,
  });
  // The members area is a read surface for every role: the page has rendered.
  await screen.findByRole('table', { name: /members of/i });
  return screen;
}

function accountCard(screen: Screen) {
  return within(screen.getByRole('heading', { name: 'Account' }).parentElement!.parentElement!);
}

let deleteBodies: AccountDeletionRequest[] = [];
let orgDeleteBodies: OrganizationDeletionRequest[] = [];

function serve(preview: AccountDeletionPreviewOut, deletion: () => Response) {
  server.use(
    http.get(P('/account/deletion-preview'), () => HttpResponse.json(preview)),
    http.post(P('/account/delete'), async ({ request }) => {
      deleteBodies.push((await request.json()) as AccountDeletionRequest);
      return deletion();
    }),
    http.post(P('/organizations/:orgId/delete'), async ({ request }) => {
      orgDeleteBodies.push((await request.json()) as OrganizationDeletionRequest);
      return HttpResponse.json(ORG_RECEIPT);
    }),
    http.get(P('/account/export'), () => HttpResponse.json(EXPORT)),
  );
}

beforeEach(() => {
  orgModel.reset();
  deleteBodies = [];
  orgDeleteBodies = [];
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('who is offered what', () => {
  it.each(SIX)('every account may export and delete itself (%s)', async (role) => {
    const screen = await settingsAs(role);
    const card = accountCard(screen);
    expect(card.getByRole('button', { name: /download my data/i })).toBeEnabled();
    expect(card.getByRole('button', { name: /delete account/i })).toBeEnabled();
  });

  it.each(SIX)('organization export and deletion are offered to exactly the owner (%s)', async (role) => {
    const screen = await settingsAs(role);
    const offered = role === 'owner';
    expect(screen.queryAllByRole('button', { name: /delete organization/i })).toHaveLength(offered ? 1 : 0);
    expect(screen.queryAllByRole('button', { name: /download organization data/i })).toHaveLength(offered ? 1 : 0);
  });
});

describe('account deletion', () => {
  it('shows the impact, sends exactly the password and DELETE, and signs this tab out', async () => {
    serve(LEAVES, () => HttpResponse.json(RECEIPT));
    const screen = await settingsAs('marketer');
    await screen.user.click(accountCard(screen).getByRole('button', { name: /delete account/i }));
    const dialog = await screen.findByRole('dialog', { name: /delete account/i });
    expect(await within(dialog).findByText('You will leave Demo Org.')).toBeInTheDocument();

    await screen.user.type(within(dialog).getByLabelText(/^current password/i), 'my password 1');
    await screen.user.type(within(dialog).getByLabelText(/type delete to confirm/i), 'DELETE');
    await screen.user.click(within(dialog).getByRole('button', { name: /delete my account/i }));

    expect(await screen.findByText('Your account has been deleted.')).toBeInTheDocument();
    expect(deleteBodies).toEqual([{ current_password: 'my password 1', confirmation: 'DELETE' }]);
    expect(localStorage.getItem('signalnest-token')).toBeNull();
  });

  it('refuses to send without the typed confirmation', async () => {
    serve(LEAVES, () => HttpResponse.json(RECEIPT));
    const screen = await settingsAs('marketer');
    await screen.user.click(accountCard(screen).getByRole('button', { name: /delete account/i }));
    const dialog = await screen.findByRole('dialog', { name: /delete account/i });
    await within(dialog).findByText('You will leave Demo Org.');
    await screen.user.type(within(dialog).getByLabelText(/^current password/i), 'my password 1');
    await screen.user.type(within(dialog).getByLabelText(/type delete to confirm/i), 'delete');
    await screen.user.click(within(dialog).getByRole('button', { name: /delete my account/i }));
    expect(await within(dialog).findByText('Type DELETE, in capitals, to confirm')).toBeInTheDocument();
    expect(deleteBodies).toEqual([]);
  });

  it.each([
    ['unverified', UNVERIFIED_EMAIL, true],
    ['verified', VERIFIED_EMAIL, false],
  ] as const)('says when an %s address leaves data found only by it', async (_label, email, shown) => {
    serve({ ...LEAVES, email_keyed_data: email }, () => HttpResponse.json(RECEIPT));
    const screen = await settingsAs('marketer');
    await screen.user.click(accountCard(screen).getByRole('button', { name: /delete account/i }));
    const dialog = await screen.findByRole('dialog', { name: /delete account/i });
    await within(dialog).findByText('You will leave Demo Org.');
    const notice = within(dialog).queryByText(/your email address is not verified/i);
    if (shown) expect(notice).toBeInTheDocument();
    else expect(notice).not.toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /delete my account/i })).toBeEnabled();
  });

  it('a sole owner is told why and cannot submit', async () => {
    serve(BLOCKED, () => HttpResponse.json(RECEIPT));
    const screen = await settingsAs('owner');
    await screen.user.click(accountCard(screen).getByRole('button', { name: /delete account/i }));
    const dialog = await screen.findByRole('dialog', { name: /delete account/i });
    expect(
      await within(dialog).findByText(/you are the only owner of demo org, which has other members/i),
    ).toBeInTheDocument();
    expect(within(dialog).getByRole('button', { name: /delete my account/i })).toBeDisabled();
  });

  it('a wrong password is a field error and the session stays', async () => {
    serve(LEAVES, () =>
      HttpResponse.json(
        { error: { code: 'current_password_incorrect', message: 'The current password is incorrect.' } },
        { status: 422 },
      ),
    );
    const screen = await settingsAs('marketer');
    await screen.user.click(accountCard(screen).getByRole('button', { name: /delete account/i }));
    const dialog = await screen.findByRole('dialog', { name: /delete account/i });
    await within(dialog).findByText('You will leave Demo Org.');
    await screen.user.type(within(dialog).getByLabelText(/^current password/i), 'wrong one');
    await screen.user.type(within(dialog).getByLabelText(/type delete to confirm/i), 'DELETE');
    await screen.user.click(within(dialog).getByRole('button', { name: /delete my account/i }));
    expect(await within(dialog).findByText('The current password is incorrect.')).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBe('test-token');
  });
});

describe('export', () => {
  it('downloads the account export as a JSON file', async () => {
    serve(LEAVES, () => HttpResponse.json(RECEIPT));
    const blobs: Blob[] = [];
    const create = vi.fn((blob: Blob) => {
      blobs.push(blob);
      return 'blob:export';
    });
    const revoke = vi.fn();
    Object.assign(URL, { createObjectURL: create, revokeObjectURL: revoke });
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
    const screen = await settingsAs('viewer');
    await screen.user.click(accountCard(screen).getByRole('button', { name: /download my data/i }));
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(blobs).toHaveLength(1);
    expect(revoke).toHaveBeenCalledWith('blob:export');
    const text = await new Promise<string>((resolve) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result));
      reader.readAsText(blobs[0]!);
    });
    expect(JSON.parse(text)).toEqual(EXPORT);
  });
});

describe('organization deletion', () => {
  it('requires the exact name, then sends exactly the password and the name', async () => {
    serve(LEAVES, () => HttpResponse.json(RECEIPT));
    const screen = await settingsAs('owner');
    await screen.user.click(screen.getByRole('button', { name: /delete organization/i }));
    const dialog = await screen.findByRole('dialog', { name: /delete organization/i });
    await screen.user.type(within(dialog).getByLabelText(/^current password/i), 'owner password');
    await screen.user.type(within(dialog).getByLabelText(/^organization name/i), 'demo org');
    await screen.user.click(within(dialog).getByRole('button', { name: /^delete organization$/i }));
    expect(await within(dialog).findByText('Type the organization’s name exactly to confirm.')).toBeInTheDocument();
    expect(orgDeleteBodies).toEqual([]);

    await screen.user.clear(within(dialog).getByLabelText(/^organization name/i));
    await screen.user.type(within(dialog).getByLabelText(/^organization name/i), 'Demo Org');
    await screen.user.click(within(dialog).getByRole('button', { name: /^delete organization$/i }));
    await waitFor(() =>
      expect(orgDeleteBodies).toEqual([{ current_password: 'owner password', confirm_organization_name: 'Demo Org' }]),
    );
    expect(await screen.findByText('Demo Org was deleted')).toBeInTheDocument();
  });
});
