import { useQueryClient, type QueryClient } from '@tanstack/react-query';
import { act, fireEvent, waitFor, within, type RenderResult } from '@testing-library/react';
import type userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { useEffect } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { ApiError } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import * as api from '@/api/endpoints';
import { useAuth } from '@/auth/AuthContext';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import { emulateExitAnimations, renderApp, renderProductionApp } from '@/test/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

/**
 * §34/§35/§53 — every formerly ungated mutation control, table-driven across all
 * six organization roles. One row per RENDERED ENTRY SITE of M3's inventory
 * (SR/m3/INVENTORY.md, unit U3: 19 entry sites), with the four opportunity status
 * buttons and all nine campaign-context kinds exercised inside their rows — no
 * "one representative button" shortcut.
 *
 * The oracle is the BACKEND policy, written out literally here rather than read
 * from the frontend's role helper: a test that asked lib/roles.ts what to expect
 * could never catch lib/roles.ts being wrong.
 *   EDITOR    = require_role(OWNER, ADMIN, MARKETER), a rank floor at marketer
 *               (apps/api/app/auth/dependencies.py:23-30, :210-219)
 *   ORG_ADMIN = require_exact_organization_roles(OWNER, ADMIN)
 *               (apps/api/app/organizations/routes.py:75-83)
 *
 * The role is configured in the stateful backend model before render, so /auth/me
 * answers it like the server would. Every absence assertion waits for two witnesses
 * first — the role has resolved for the active organization, and the page's
 * read-only data has rendered — so "absent" can never mean "not loaded yet", and
 * the read surface staying visible is itself asserted for every role (§35).
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const ROLES: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];
const EDITOR = new Set<ModelRole>(['owner', 'admin', 'marketer']);
const ORG_ADMIN = new Set<ModelRole>(['owner', 'admin']);

type Screen = RenderResult & { user: ReturnType<typeof userEvent.setup> };

function RoleProbe() {
  const { memberships } = useAuth();
  const { organizationId } = useWorkspace();
  const role = memberships.find((m) => m.organization_id === organizationId)?.role ?? '';
  return <span data-testid="m4-role-probe">{role}</span>;
}

async function renderAs(role: ModelRole, route: string): Promise<Screen> {
  orgModel.setMembership('org-1', 'user-1', role);
  const screen = renderApp(
    <>
      <App />
      <RoleProbe />
    </>,
    { route },
  );
  await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent(new RegExp(`^${role}$`)));
  return screen;
}

const buttons = (screen: Screen, name: RegExp) => screen.queryAllByRole('button', { name });

function expectOffered(controls: HTMLElement[], count: number) {
  // Positive instance: an allowed role must actually be offered the control, so a
  // gate that hid it from everyone cannot pass as "correctly hidden".
  expect(controls).toHaveLength(count);
  for (const c of controls) expect(c).toBeEnabled();
}

const SCHEDULE_404 = http.get(P('/workspaces/:ws/scout-requests/:id/schedule'), () =>
  HttpResponse.json({ error: { code: 'not_found', message: 'no schedule' } }, { status: 404 }),
);
const NO_LOCATIONS = http.get(P('/workspaces/:ws/locations'), () => HttpResponse.json([]));
const NO_SCOUTS = http.get(P('/workspaces/:ws/scout-requests'), () => HttpResponse.json([]));
const NO_OPPORTUNITIES = http.get(P('/workspaces/:ws/opportunities'), () => HttpResponse.json([]));
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

// One row per context kind. Each kind titles its row from a different field
// (CampaignContext.tsx `title`), so the row fills all of them and every kind's
// title reads "Seeded …".
const SEEDED_CONTEXT = http.get(P('/workspaces/:ws/:kind'), ({ params }) => {
  const title = `Seeded ${String(params.kind)}`;
  return HttpResponse.json([
    {
      id: `row-${String(params.kind)}`,
      name: title,
      label: title,
      text: title,
      channel: title,
      tone: [title],
      source_type: 'seeded_source',
      enabled: true,
    },
  ]);
});

const CONTEXT_TABS = [
  ['Products & services', 'product'],
  ['Audiences', 'audience'],
  ['Competitors', 'competitor'],
  ['Brand voice', 'voice profile'],
  ['Offers', 'offer'],
  ['Claims library', 'claim'],
  ['Campaigns', 'campaign'],
  ['Sources', 'source preference'],
  ['Channels', 'channel preference'],
] as const;

const escape = (s: string) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');

interface Row {
  id: string;
  site: string;
  policy: Set<ModelRole>;
  route: string;
  handlers?: Parameters<typeof server.use>;
  /** Read-only content that must render for EVERY role before the control is judged. */
  ready: (screen: Screen) => Promise<unknown>;
  /** Drive the page to the control and assert it for this role. */
  check: (screen: Screen, allowed: boolean) => Promise<void>;
}

function single(name: RegExp, allowedCount: number): Row['check'] {
  return async (screen, allowed) => {
    const found = buttons(screen, name);
    if (allowed) expectOffered(found, allowedCount);
    else expect(found).toHaveLength(0);
  };
}

const ROWS: Row[] = [
  {
    id: 'C01 Finish onboarding',
    site: 'Onboarding.tsx:421',
    policy: EDITOR,
    route: '/onboarding',
    ready: (s) => s.findByText(/how do customers find you today/i),
    check: async (screen, allowed) => {
      // Walk the wizard to its last step; only the final submit is withheld.
      await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));
      await screen.user.type(await screen.findByLabelText(/business \/ brand name/i), 'Matrix Brand');
      for (let i = 0; i < 4; i += 1) {
        await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));
      }
      await waitFor(() => expect(screen.queryByRole('button', { name: /save & continue/i })).not.toBeInTheDocument());
      const found = buttons(screen, /finish onboarding/i);
      if (allowed) {
        expectOffered(found, 1);
      } else {
        expect(found).toHaveLength(0);
        // The withheld submit is explained, not silently missing.
        expect(screen.getByText(/you do not have permission to finish onboarding/i)).toBeInTheDocument();
      }
    },
  },
  {
    id: 'C02 Add location (page header)',
    site: 'Locations.tsx:39',
    policy: EDITOR,
    route: '/locations',
    ready: (s) => s.findByText('Dallas'),
    check: single(/add location/i, 1),
  },
  {
    id: 'C02 Add location (empty state)',
    site: 'Locations.tsx:94',
    policy: EDITOR,
    route: '/locations',
    handlers: [NO_LOCATIONS],
    ready: (s) => s.findByText(/no locations yet/i),
    check: single(/add location/i, 2),
  },
  {
    id: 'C02 Edit & service area',
    site: 'Locations.tsx:81',
    policy: EDITOR,
    route: '/locations',
    ready: (s) => s.findByText('Dallas'),
    check: async (screen, allowed) => {
      const edit = buttons(screen, /^edit & service area$/i);
      const view = buttons(screen, /^view details & service area$/i);
      if (allowed) {
        expectOffered(edit, 4);
        expect(view).toHaveLength(0);
        return;
      }
      expect(edit).toHaveLength(0);
      // §35: the location's details stay readable, through a dialog with no mutation in it.
      expectOffered(view, 4);
      await screen.user.click(view[0]!);
      const dialog = await screen.findByRole('dialog', { name: /location details/i });
      const name = within(dialog).getByLabelText(/location name/i);
      expect(name).toHaveValue('Dallas');
      expect(name).toHaveAttribute('readonly');
      expect(within(dialog).queryByRole('button', { name: /save|add location|geocode|look ?up|find/i })).not.toBeInTheDocument();
      expect(within(dialog).getAllByRole('button', { name: /close/i }).length).toBeGreaterThan(0);
    },
  },
  {
    id: 'C03 Opportunity status actions (x4)',
    site: 'OpportunityDetail.tsx:151',
    policy: EDITOR,
    route: '/opportunities/opp-loc-dallas-0',
    ready: (s) => s.findByRole('heading', { name: /dallas customers want faster delivery 1/i }),
    check: async (screen, allowed) => {
      for (const label of ['Save', 'Monitor', 'Mark actioned', 'Ignore']) {
        const found = buttons(screen, new RegExp(`^${label}$`, 'i'));
        if (allowed) expectOffered(found, 1);
        else expect(found, label).toHaveLength(0);
      }
    },
  },
  {
    id: 'C04 Add {singular} (panel header) x9 kinds',
    site: 'CampaignContext.tsx:422',
    policy: EDITOR,
    route: '/context',
    handlers: [
      SEEDED_CONTEXT,
    ],
    ready: (s) => s.findByRole('heading', { name: /campaign context/i }),
    check: async (screen, allowed) => {
      for (const [tab, singular] of CONTEXT_TABS) {
        await screen.user.click(screen.getByRole('tab', { name: new RegExp(`^${escape(tab)}$`, 'i') }));
        const panel = await screen.findByRole('tabpanel');
        // Liveness: this kind's seeded row rendered (a read surface, every role).
        await within(panel).findByText(/^Seeded /);
        const found = within(panel).queryAllByRole('button', { name: new RegExp(`^add ${escape(singular)}$`, 'i') });
        if (allowed) expectOffered(found, 1);
        else expect(found, tab).toHaveLength(0);
      }
    },
  },
  {
    id: 'C04 Add {singular} (empty state) x9 kinds',
    site: 'CampaignContext.tsx:464',
    policy: EDITOR,
    route: '/context',
    ready: (s) => s.findByText(/no products yet/i),
    check: async (screen, allowed) => {
      for (const [tab, singular] of CONTEXT_TABS) {
        await screen.user.click(screen.getByRole('tab', { name: new RegExp(`^${escape(tab)}$`, 'i') }));
        const panel = await screen.findByRole('tabpanel');
        await within(panel).findByText(new RegExp(`no ${escape(singular)}s yet`, 'i'));
        const found = within(panel).queryAllByRole('button', { name: new RegExp(`^add ${escape(singular)}$`, 'i') });
        // Header + empty-state button for an editor; neither for anyone else.
        if (allowed) expectOffered(found, 2);
        else expect(found, tab).toHaveLength(0);
      }
    },
  },
  {
    id: 'C05 Remove {singular} x9 kinds',
    site: 'CampaignContext.tsx:444',
    policy: EDITOR,
    route: '/context',
    handlers: [
      SEEDED_CONTEXT,
    ],
    ready: (s) => s.findByRole('heading', { name: /campaign context/i }),
    check: async (screen, allowed) => {
      for (const [tab, singular] of CONTEXT_TABS) {
        await screen.user.click(screen.getByRole('tab', { name: new RegExp(`^${escape(tab)}$`, 'i') }));
        const panel = await screen.findByRole('tabpanel');
        // The seeded row is visible to every role before the remove control is judged.
        await within(panel).findByText(/^Seeded /);
        const found = within(panel).queryAllByRole('button', { name: new RegExp(`^remove ${escape(singular)}$`, 'i') });
        if (allowed) expectOffered(found, 1);
        else expect(found, tab).toHaveLength(0);
      }
    },
  },
  {
    id: 'C06 New scout request (page header)',
    site: 'ScoutRequests.tsx:68',
    policy: EDITOR,
    route: '/scout-requests',
    ready: (s) => s.findByText('Dallas demand scan'),
    check: single(/new scout request/i, 1),
  },
  {
    id: 'C06 New scout request (empty state)',
    site: 'ScoutRequests.tsx:154',
    policy: EDITOR,
    route: '/scout-requests',
    handlers: [NO_SCOUTS],
    ready: (s) => s.findByText(/no scout requests yet/i),
    check: single(/new scout request/i, 2),
  },
  {
    id: 'C07 Run now (list row)',
    site: 'ScoutRequests.tsx:122',
    policy: EDITOR,
    route: '/scout-requests',
    ready: (s) => s.findByText('Nairobi demand scan'),
    check: single(/run now/i, 4),
  },
  {
    id: 'C07 Run now (detail header)',
    site: 'ScoutRequestDetail.tsx:110',
    policy: EDITOR,
    route: '/scout-requests/scout-loc-dallas',
    handlers: [SCHEDULE_404],
    ready: (s) => s.findByRole('heading', { name: 'Dallas demand scan' }),
    check: async (screen, allowed) => {
      await screen.findByText(/dallas customers want faster delivery 1/i);
      await single(/run now/i, 1)(screen, allowed);
    },
  },
  {
    id: 'C07 Run now (no-opportunities empty state)',
    site: 'ScoutRequestDetail.tsx:225',
    policy: EDITOR,
    route: '/scout-requests/scout-loc-dallas',
    handlers: [SCHEDULE_404, NO_OPPORTUNITIES],
    ready: (s) => s.findByText(/no opportunities yet/i),
    check: single(/run now/i, 2),
  },
  {
    id: 'C08 Pause (list row)',
    site: 'ScoutRequests.tsx:134',
    policy: EDITOR,
    route: '/scout-requests',
    ready: (s) => s.findByText('Nairobi demand scan'),
    check: single(/^pause$/i, 3),
  },
  {
    id: 'C08 Pause (detail header)',
    site: 'ScoutRequestDetail.tsx:121',
    policy: EDITOR,
    route: '/scout-requests/scout-loc-dallas',
    handlers: [SCHEDULE_404],
    ready: (s) => s.findByRole('heading', { name: 'Dallas demand scan' }),
    check: single(/^pause$/i, 1),
  },
  {
    id: 'C09 Resume (list row, paused)',
    site: 'ScoutRequests.tsx:130',
    policy: EDITOR,
    route: '/scout-requests',
    ready: (s) => s.findByText('London demand scan'),
    check: single(/^resume$/i, 1),
  },
  {
    id: 'C09 Resume (detail header, paused)',
    site: 'ScoutRequestDetail.tsx:117',
    policy: EDITOR,
    route: '/scout-requests/scout-loc-london',
    handlers: [SCHEDULE_404],
    ready: (s) => s.findByRole('heading', { name: 'London demand scan' }),
    check: single(/^resume$/i, 1),
  },
  {
    id: 'C10 Cancel job',
    site: 'scouts/JobsPanel.tsx:98',
    policy: EDITOR,
    route: '/scout-requests/scout-loc-dallas',
    handlers: [SCHEDULE_404, RUNNING_JOB],
    ready: (s) => s.findByText(/^running$/i),
    check: single(/^cancel$/i, 1),
  },
  {
    id: 'C11 Workspaces + New',
    site: 'Settings.tsx:151',
    policy: ORG_ADMIN,
    route: '/settings',
    ready: (s) => s.findByText(/demo org · 1 workspace/i),
    check: single(/^(\+\s*)?new( workspace)?$/i, 1),
  },
];

beforeEach(() => {
  orgModel.reset();
});

afterEach(() => {
  expect(orgModel.violations()).toEqual([]);
});

describe('formerly ungated mutation controls × six roles (§53)', () => {
  it('covers every rendered entry site of the inventory exactly once', () => {
    // M3 INVENTORY.md unit U3: 19 entry sites (C01-C11). A row added or dropped
    // without the inventory changing is a coverage defect.
    expect(ROWS).toHaveLength(19);
    expect(new Set(ROWS.map((r) => r.site)).size).toBe(19);
  });

  describe.each(ROWS)('$id ($site)', (row) => {
    it.each(ROLES)('%s', async (role) => {
      if (row.handlers) server.use(...row.handlers);
      const screen = await renderAs(role, row.route);
      await row.ready(screen);
      await row.check(screen, row.policy.has(role));
    });
  });
});

// ---- §16: the gates follow the ACTIVE organization -------------------------------

interface SwitchPage {
  page: string;
  route: string;
  handlers?: Parameters<typeof server.use>;
  /** Read-only content of the page, rendered for every role in every organization. */
  ready: (screen: Screen) => Promise<unknown>;
  controls: RegExp[];
  /** Backend policy for these controls (default EDITOR). */
  policy?: Set<ModelRole>;
  /** Brings the page to where its controls live (e.g. the last onboarding step); run after every switch. */
  prepare?: (screen: Screen) => Promise<void>;
}

const SWITCH_PAGES: SwitchPage[] = [
  { page: 'Locations', route: '/locations', ready: (s) => s.findByText('Dallas'), controls: [/^add location$/i, /^edit & service area$/i] },
  {
    page: 'Scout requests',
    route: '/scout-requests',
    ready: (s) => s.findByText('Nairobi demand scan'),
    controls: [/new scout request/i, /run now/i, /^pause$/i, /^resume$/i],
  },
  {
    page: 'Scout request detail (with a running job)',
    route: '/scout-requests/scout-loc-dallas',
    handlers: [SCHEDULE_404, RUNNING_JOB],
    // The job list loads after the header; its running job is part of the read surface.
    ready: async (s) => {
      await s.findByRole('heading', { name: 'Dallas demand scan' });
      await s.findByText(/^running$/i);
    },
    controls: [/run now/i, /^pause$/i, /^cancel$/i],
  },
  {
    // The schedule controls exist only while scheduling is live, so it is switched on here.
    page: 'Scout request detail — schedule (scheduling enabled)',
    route: '/scout-requests/scout-loc-dallas',
    handlers: [
      SCHEDULE_404,
      http.get(P('/system/capabilities'), () =>
        HttpResponse.json({
          app_mode: 'local',
          environment: 'development',
          is_local_mode: true,
          all_configured: true,
          features: { opportunity_feedback_enabled: false, scout_scheduling_enabled: true, connector_rss_enabled: false },
        }),
      ),
    ],
    ready: async (s) => {
      await s.findByRole('heading', { name: 'Dallas demand scan' });
      await s.findByText(/no recurring schedule/i);
    },
    controls: [/^schedule daily$/i, /^schedule weekly$/i],
  },
  {
    page: 'Opportunity detail',
    route: '/opportunities/opp-loc-dallas-0',
    ready: (s) => s.findByRole('heading', { name: /dallas customers want faster delivery 1/i }),
    controls: [/^save$/i, /^monitor$/i, /^mark actioned$/i, /^ignore$/i],
  },
  {
    // FeedbackPanel's own editor gate (the panel renders only for editors, and only
    // while feedback is live for the workspace).
    page: 'Opportunity feedback (feedback enabled)',
    route: '/opportunities/opp-loc-dallas-0',
    handlers: [
      http.get(P('/workspaces/:workspaceId/feedback-capability'), () => HttpResponse.json({ enabled: true })),
      http.get(P('/workspaces/:ws/opportunities/:id/feedback'), () =>
        HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 }),
      ),
    ],
    ready: (s) => s.findByRole('heading', { name: /dallas customers want faster delivery 1/i }),
    controls: [/^useful$/i, /^not useful$/i],
  },
  {
    page: 'Campaign context',
    route: '/context',
    ready: (s) => s.findByText(/no products yet/i),
    controls: [/^add product$/i],
  },
  {
    page: 'Onboarding (review step)',
    route: '/onboarding',
    // The last step has no "Save & continue": being there is the witness (every role can walk it).
    ready: (s) => waitFor(() => expect(s.queryByRole('button', { name: /save & continue/i })).not.toBeInTheDocument()),
    controls: [/finish onboarding/i],
    prepare: async (screen) => {
      // A workspace switch restarts the wizard for that workspace's own draft.
      await screen.findByText(/how do customers find you today/i).catch(() => undefined);
      if (!screen.queryByRole('button', { name: /save & continue/i })) return;
      await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));
      const name = await screen.findByLabelText(/business \/ brand name/i);
      if (!(name as HTMLInputElement).value) await screen.user.type(name, 'Switch Brand');
      while (screen.queryByRole('button', { name: /save & continue/i })) {
        await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));
      }
    },
  },
  {
    page: 'Settings (workspace New)',
    route: '/settings',
    ready: (s) => s.findByText(/ · 1 workspace/i),
    controls: [/^(\+\s*)?new$/i],
    policy: ORG_ADMIN,
  },
];

function addSecondOrganization(role: ModelRole) {
  orgModel.addOrganization({ id: 'org-2', name: 'Second Org', slug: 'second-org' }, [
    { id: 'ws-2', name: 'Second Workspace', slug: 'second-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
  ]);
  orgModel.setMembership('org-2', 'user-1', role);
}

async function switchOrganization(screen: Screen, name: string, workspaceId: string) {
  const [trigger] = screen.getAllByRole('combobox', { name: /^organization$/i });
  await screen.user.click(trigger!);
  await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name }));
  // The switch has reached the workspace level before anything is judged.
  await waitFor(() => expect(localStorage.getItem('signalnest-active-workspace')).toBe(workspaceId));
}

async function expectGates(screen: Screen, page: SwitchPage, role: ModelRole, where: string) {
  await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent(new RegExp(`^${role}$`)));
  if (page.prepare) await page.prepare(screen);
  await page.ready(screen);
  const allowed = (page.policy ?? EDITOR).has(role);
  for (const control of page.controls) {
    if (allowed) {
      await waitFor(() => expect(buttons(screen, control).length, `${where}: ${control} for ${role}`).toBeGreaterThan(0));
    } else {
      // Role resolved and the read surface rendered, so absence is a measurement.
      expect(buttons(screen, control).length, `${where}: ${control} for ${role}`).toBe(0);
    }
  }
}

describe.each(SWITCH_PAGES)('$page re-gates on an organization switch (§16)', (page) => {
  it.each([
    ['owner', 'viewer'],
    ['viewer', 'marketer'],
  ] as const)('%s in Demo Org, %s in Second Org, and back', async (first, second) => {
    if (page.handlers) server.use(...page.handlers);
    orgModel.setMembership('org-1', 'user-1', first);
    addSecondOrganization(second);
    const screen = renderApp(
      <>
        <App />
        <RoleProbe />
      </>,
      { route: page.route },
    );
    await expectGates(screen, page, first, 'Demo Org');

    await switchOrganization(screen, 'Second Org', 'ws-2');
    await expectGates(screen, page, second, 'after switching to Second Org');

    await switchOrganization(screen, 'Demo Org', 'ws-1');
    await expectGates(screen, page, first, 'after switching back');
  });
});

// ---- workspace-page cache side effects (SR/m4/SIDE-EFFECT-INVENTORY.md, W01–W07) ----
//
// The mutations on these pages predate 6B-3B, but they live in the 6B-3B files, so every
// cache side effect they have is pinned the same way as the organization ones: the
// action must re-read what it changed in THIS workspace (a dropped invalidation fails)
// and must leave another workspace's cached entries untouched (a widened one fails).

const SECOND_WORKSPACE = {
  id: 'ws-2',
  name: 'Second Workspace',
  slug: 'second-ws',
  onboarding_completed: true,
  created_at: '2026-01-01T00:00:00Z',
};

let sideCache: QueryClient | null = null;
function SideCacheProbe() {
  const qc = useQueryClient();
  useEffect(() => {
    sideCache = qc;
  }, [qc]);
  return null;
}

// Every request, whichever handler answers it (fixture handlers answer without the model).
let seen: string[] = [];
const onRequest = ({ request }: { request: Request }) => {
  seen.push(`${request.method} ${new URL(request.url).pathname}`);
};
const reads = (path: RegExp) => seen.filter((r) => r.startsWith('GET ') && path.test(r.slice(4))).length;

async function switchWorkspace(screen: Screen, name: string, id: string) {
  const [trigger] = screen.getAllByRole('combobox', { name: /^workspace$/i });
  await screen.user.click(trigger!);
  await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name }));
  await waitFor(() => expect(localStorage.getItem('signalnest-active-workspace')).toBe(id));
}

interface SideRow {
  site: string;
  route: string;
  handlers?: Parameters<typeof server.use>;
  ready: (screen: Screen) => Promise<unknown>;
  /** Reads of THIS workspace that the action must repeat (one per invalidation it owns). */
  own: RegExp[];
  /** Another workspace's entry that must stay exactly as it was. */
  otherKey: readonly unknown[];
  act: (screen: Screen) => Promise<void>;
  after?: (screen: Screen) => Promise<void>;
  /** Reads that must NOT be repeated (e.g. the organization list). */
  untouched?: RegExp[];
}

const SEEDED_ONE_PRODUCT = http.get(P('/workspaces/:ws/products'), () =>
  HttpResponse.json([{ id: 'row-products', name: 'Seeded products' }]),
);
let holdDetail = false;
let releaseDetail: () => void = () => undefined;

const SIDE_ROWS: SideRow[] = [
  {
    site: 'W01 CampaignContext.tsx:266 add a product',
    route: '/context',
    handlers: [http.post(P('/workspaces/:ws/products'), () => HttpResponse.json({ id: 'new-1', name: 'New product' }, { status: 201 }))],
    ready: (s) => s.findByText(/no products yet/i),
    own: [/\/workspaces\/ws-1\/products$/],
    otherKey: ['workspaces', 'ws-2', 'context', 'products'],
    act: async (screen) => {
      const [add] = buttons(screen, /^add product$/i);
      await screen.user.click(add!);
      const dialog = await screen.findByRole('dialog');
      await screen.user.type(within(dialog).getByLabelText(/^name/i), 'New product');
      await screen.user.click(within(dialog).getByRole('button', { name: /^add product$/i }));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    },
  },
  {
    site: 'W02 CampaignContext.tsx:418 remove a product',
    route: '/context',
    handlers: [SEEDED_ONE_PRODUCT, http.delete(P('/workspaces/:ws/products/:id'), () => new HttpResponse(null, { status: 204 }))],
    ready: (s) => s.findByText('Seeded products'),
    own: [/\/workspaces\/ws-1\/products$/],
    otherKey: ['workspaces', 'ws-2', 'context', 'products'],
    act: async (screen) => {
      await screen.user.click(buttons(screen, /^remove product$/i)[0]!);
      const confirm = await screen.findByRole('alertdialog').catch(() => screen.findByRole('dialog'));
      await screen.user.click(within(confirm).getByRole('button', { name: /^remove$/i }));
      await waitFor(() => expect(screen.queryByRole('alertdialog')).not.toBeInTheDocument());
    },
  },
  {
    site: 'W03 Onboarding.tsx:154-164 finish onboarding',
    route: '/onboarding',
    ready: (s) => s.findByText(/how do customers find you today/i),
    own: [
      /\/workspaces\/ws-1\/business-profile$/, // :156 businessProfile(ws)
      /\/workspaces\/ws-1\/brands$/, // :157 brands(ws)
      /\/workspaces\/ws-1\/locations$/, // :158 workspace(ws), a prefix of every ws-1 key
      /\/organizations\/org-1\/workspaces$/, // :160 workspaces(org)
    ],
    otherKey: ['workspaces', 'ws-2', 'business-profile'],
    untouched: [/\/organizations$/],
    act: async (screen) => {
      await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));
      await screen.user.type(await screen.findByLabelText(/business \/ brand name/i), 'Side Effect Brand');
      while (screen.queryByRole('button', { name: /save & continue/i })) {
        await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));
      }
      await waitFor(() => expect(localStorage.getItem('signalnest-onboarding-ws-1')).toContain('Side Effect Brand'));
      await screen.user.click(screen.getByRole('button', { name: /finish onboarding/i }));
    },
    after: async () => {
      await waitFor(() => expect(window.location.pathname).toBe('/')); // :164 navigate('/')
      expect(localStorage.getItem('signalnest-onboarding-ws-1')).toBeNull(); // :154 draft removed
    },
  },
  {
    site: 'W04 OpportunityDetail.tsx:94-98 mark saved',
    route: '/opportunities/opp-loc-dallas-0',
    handlers: [
      http.get(P('/workspaces/ws-1/opportunities/opp-loc-dallas-0'), async () => {
        if (holdDetail) {
          await new Promise<void>((resolve) => {
            releaseDetail = resolve;
          });
        }
        return undefined;
      }),
    ],
    ready: (s) => s.findByRole('heading', { name: /dallas customers want faster delivery 1/i }),
    own: [/\/workspaces\/ws-1\/opportunities\/opp-loc-dallas-0$/],
    otherKey: ['workspaces', 'ws-2', 'opportunities', 'detail', 'opp-loc-dallas-0'],
    act: async (screen) => {
      holdDetail = true;
      await screen.user.click(buttons(screen, /^save$/i)[0]!);
      // :94 setQueryData — the new status shows at once, while the re-read is still out.
      await waitFor(() => expect(buttons(screen, /^save$/i)[0]).toHaveAttribute('aria-pressed', 'true'));
      holdDetail = false;
      act(() => releaseDetail());
    },
  },
  {
    site: 'W06 LocationDialog.tsx:178-179 save a location',
    route: '/locations',
    ready: (s) => s.findByText('Dallas'),
    own: [/\/workspaces\/ws-1\/locations$/, /\/workspaces\/ws-1\/locations\/loc-dallas\/geo-coverage$/],
    otherKey: ['workspaces', 'ws-2', 'locations'],
    act: async (screen) => {
      await screen.user.click(buttons(screen, /^edit & service area$/i)[0]!);
      const dialog = await screen.findByRole('dialog', { name: /edit location/i });
      await waitFor(() => expect(within(dialog).getByLabelText(/location name/i)).toHaveValue('Dallas'));
      await screen.user.click(within(dialog).getByRole('button', { name: /^save changes$/i }));
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    },
  },
  {
    site: 'W07 JobsPanel.tsx:53 cancel a job',
    route: '/scout-requests/scout-loc-dallas',
    handlers: [
      SCHEDULE_404,
      RUNNING_JOB,
      http.post(P('/workspaces/:ws/jobs/:id/cancel'), () => HttpResponse.json({ id: 'job-running-1', status: 'cancel_requested' })),
    ],
    ready: (s) => s.findByText(/^running$/i),
    own: [/\/workspaces\/ws-1\/jobs$/],
    otherKey: ['workspaces', 'ws-2', 'jobs', { scout_request_id: 'scout-loc-dallas', limit: 10 }],
    act: async (screen) => {
      await screen.user.click(buttons(screen, /^cancel$/i)[0]!);
      expect(await screen.findByText(/cancellation requested/i)).toBeInTheDocument();
    },
  },
];

describe('workspace-page cache side effects stay inside their workspace (inventory W01–W07)', () => {
  beforeEach(() => {
    seen = [];
    sideCache = null;
    holdDetail = false;
    server.events.on('request:start', onRequest);
  });
  afterEach(() => {
    server.events.removeListener('request:start', onRequest);
  });

  it.each(SIDE_ROWS)('$site', async (row) => {
    orgModel.addWorkspace('org-1', SECOND_WORKSPACE);
    if (row.handlers) server.use(...row.handlers);
    const screen = renderProductionApp(<App />, row.route, {
      token: 'test-token',
      probe: (
        <>
          <RoleProbe />
          <SideCacheProbe />
        </>
      ),
    });
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent(/^owner$/));
    await row.ready(screen);
    // Visit the second workspace so its entry is cached, then come back.
    await switchWorkspace(screen, 'Second Workspace', 'ws-2');
    await row.ready(screen);
    await waitFor(() => expect(sideCache!.getQueryState(row.otherKey)?.status).toBe('success'));
    await switchWorkspace(screen, 'Demo Workspace', 'ws-1');
    await row.ready(screen);
    const otherAt = sideCache!.getQueryState(row.otherKey)!.dataUpdatedAt;
    const ownBefore = row.own.map((path) => reads(path));
    const untouchedBefore = (row.untouched ?? []).map((path) => reads(path));

    await row.act(screen);

    // Dropped invalidation: each read this workspace owns happens again.
    for (const [i, path] of row.own.entries()) {
      await waitFor(() => expect(reads(path), `re-read ${path}`).toBeGreaterThan(ownBefore[i]!));
    }
    if (row.after) await row.after(screen);
    // Widened invalidation: the other workspace's entry is exactly as it was.
    const other = sideCache!.getQueryState(row.otherKey)!;
    expect(other.isInvalidated, 'other workspace invalidated').toBe(false);
    expect(other.dataUpdatedAt, 'other workspace re-read').toBe(otherAt);
    for (const [i, path] of (row.untouched ?? []).entries()) {
      expect(reads(path), `must not re-read ${path}`).toBe(untouchedBefore[i]);
    }
  });
});

// ---- error-state retry (inventory R-*): "Try again" really re-reads -------------------

interface RetryRow {
  site: string;
  route: string;
  /** The read that fails once; the retry must repeat it and render its answer. */
  fails: string;
  handlers?: Parameters<typeof server.use>;
  recovered: (screen: Screen) => Promise<unknown>;
  /** Reads that retry a 5xx on their own must fail that many times before the error shows. */
  failTimes?: number;
}

const FEEDBACK_ON = http.get(P('/workspaces/:workspaceId/feedback-capability'), () => HttpResponse.json({ enabled: true }));

const RETRY_ROWS: RetryRow[] = [
  { site: 'Locations.tsx:67', route: '/locations', fails: '/workspaces/:ws/locations', recovered: (s) => s.findByText('Dallas') },
  { site: 'CampaignContext.tsx:445', route: '/context', fails: '/workspaces/:ws/products', recovered: (s) => s.findByText(/no products yet/i) },
  {
    site: 'OpportunityDetail.tsx:112',
    route: '/opportunities/opp-loc-dallas-0',
    fails: '/workspaces/:ws/opportunities/opp-loc-dallas-0',
    recovered: (s) => s.findByRole('heading', { name: /dallas customers want faster delivery 1/i }),
  },
  {
    site: 'ScoutRequestDetail.tsx:84',
    route: '/scout-requests/scout-loc-dallas',
    fails: '/workspaces/:ws/scout-requests/scout-loc-dallas',
    handlers: [SCHEDULE_404],
    recovered: (s) => s.findByRole('heading', { name: 'Dallas demand scan' }),
  },
  {
    site: 'ScoutRequestDetail.tsx:218',
    route: '/scout-requests/scout-loc-dallas',
    fails: '/workspaces/:ws/opportunities',
    handlers: [SCHEDULE_404],
    recovered: (s) => s.findByText(/dallas customers want faster delivery 1/i),
  },
  { site: 'ScoutRequests.tsx:107', route: '/scout-requests', fails: '/workspaces/:ws/scout-requests', recovered: (s) => s.findByText('Dallas demand scan') },
  {
    site: 'FeedbackPanel.tsx:163',
    route: '/opportunities/opp-loc-dallas-0',
    fails: '/workspaces/:ws/opportunities/:id/feedback',
    handlers: [
      FEEDBACK_ON,
      http.get(P('/workspaces/:ws/opportunities/:id/feedback'), () => HttpResponse.json({ items: [], total: 0, limit: 20, offset: 0 })),
    ],
    recovered: (s) => s.findByRole('button', { name: /^useful$/i }),
    failTimes: 3, // useFeedback retries a 5xx twice
  },
  {
    site: 'JobsPanel.tsx:75',
    route: '/scout-requests/scout-loc-dallas',
    fails: '/workspaces/:ws/jobs',
    handlers: [SCHEDULE_404, RUNNING_JOB],
    recovered: (s) => s.findByText(/^running$/i),
  },
  {
    site: 'SchedulePanel.tsx:92',
    route: '/scout-requests/scout-loc-dallas',
    fails: '/workspaces/:ws/scout-requests/:id/schedule',
    handlers: [SCHEDULE_404],
    recovered: (s) => s.findByText(/no recurring schedule/i),
    failTimes: 3, // SchedulePanel retries a 5xx twice
  },
  { site: 'Invitations.tsx:418', route: '/settings', fails: '/organizations/:org/invitations', recovered: (s) => s.findByText(/no pending invitations/i) },
];

describe('every error state retries for real (inventory R-*)', () => {
  it.each(RETRY_ROWS)('$site', { timeout: 15_000 }, async (row) => {
    if (row.handlers) server.use(...row.handlers);
    const failTimes = row.failTimes ?? 1;
    let failures = 0;
    server.use(
      http.get(P(row.fails), () => {
        if (failures >= failTimes) return undefined;
        failures += 1;
        return HttpResponse.json({ error: { code: 'internal_error', message: 'Temporarily unavailable.' } }, { status: 500 });
      }),
    );
    const screen = renderApp(<App />, { route: row.route });
    const alert = await screen.findByText('Temporarily unavailable.', undefined, { timeout: 8000 });
    const retry = within(alert.closest('[role="alert"]') as HTMLElement).getByRole('button', { name: /try again/i });
    await screen.user.click(retry);
    await row.recovered(screen);
    expect(failures).toBe(failTimes);
  });
});

// ---- coverage closure on 6B-3B lines of the workspace pages (SR/m4/COVERAGE-TARGET.md) ----

describe('the location dialog works in full for an editor, and reads cleanly for others', () => {
  it('create mode: every field, geocode (partial answer), coverage type, radius, markets, online, cancel', async () => {
    let geocodeCalls = 0;
    server.use(
      http.post(P('/geocode'), () => {
        geocodeCalls += 1;
        // A partial answer: coordinates, but no city / region / country / timezone.
        return HttpResponse.json({ latitude: 6.5244, longitude: 3.3792, city: null, state_province: null, country: null, timezone: null, confidence: 0.4 });
      }),
    );
    // The form stays controlled throughout: React warns on console.error if an input's value turns null.
    const consoleErrors = vi.spyOn(console, 'error');
    const screen = await renderAs('owner', '/locations');
    await screen.findByText('Dallas');
    await screen.user.click(buttons(screen, /^add location$/i)[0]!);
    const dialog = await screen.findByRole('dialog', { name: /add location/i });
    expect(within(dialog).getByText('Not set — enter an address and geocode.')).toBeInTheDocument();
    expect(within(dialog).getByText('Auto-filled by geocoding.')).toBeInTheDocument();
    const name = within(dialog).getByLabelText(/location name/i);
    const address = within(dialog).getByLabelText(/street address/i);
    // Editing placeholders are examples, offered only while editing.
    expect(name).toHaveAttribute('placeholder', 'Dallas Flagship');
    expect(address).toHaveAttribute('placeholder', '123 Main St');
    // Nothing to save without a name.
    const add = within(dialog).getByRole('button', { name: /^add location$/i });
    expect(add).toBeDisabled();
    await screen.user.type(name, 'Lagos Hub');
    expect(name).toHaveValue('Lagos Hub');
    expect(add).toBeEnabled();
    await screen.user.type(address, '1 Marina Road');
    expect(address).toHaveValue('1 Marina Road');
    await screen.user.click(within(dialog).getByRole('button', { name: /geocode/i }));
    expect(await within(dialog).findByText('6.5244, 3.3792')).toBeInTheDocument();
    expect(geocodeCalls).toBe(1);
    // The fields the geocoder did not know stay empty and editable.
    for (const [label, value] of [
      [/^city/i, 'Lagos'],
      [/state \/ province/i, 'Lagos State'],
      [/^country/i, 'Nigeria'],
      [/postal code/i, '101001'],
      [/timezone/i, 'Africa/Lagos'],
      [/currency/i, 'NGN'],
      [/local notes/i, 'Near the port'],
    ] as const) {
      const input = within(dialog).getByLabelText(label);
      expect(input).toHaveValue('');
      await screen.user.type(input, value);
      expect(input).toHaveValue(value);
    }
    await screen.user.type(within(dialog).getByLabelText(/local competitors/i), 'Rival Lagos{Enter}');
    expect(within(dialog).getByText('Rival Lagos')).toBeInTheDocument();

    await screen.user.click(within(dialog).getByRole('tab', { name: /service area|coverage/i }));
    expect(within(dialog).getByText(/drag to set how far out customers come from/i)).toBeInTheDocument();
    const slider = within(dialog).getByRole('slider', { name: /radius/i });
    slider.focus();
    await screen.user.keyboard('{ArrowRight}');
    expect(within(dialog).getByText(/service radius — 26 miles/i)).toBeInTheDocument();
    await screen.user.type(within(dialog).getByLabelText(/included markets/i), 'Ikeja{Enter}');
    await screen.user.type(within(dialog).getByLabelText(/excluded markets/i), 'Abuja{Enter}');
    expect(within(dialog).getByText('Ikeja')).toBeInTheDocument();
    expect(within(dialog).getByText('Abuja')).toBeInTheDocument();
    const online = within(dialog).getByRole('switch', { name: /serve online/i });
    await screen.user.click(online);
    expect(online).toBeChecked();
    await screen.user.click(within(dialog).getByRole('combobox'));
    await screen.user.click(within(await screen.findByRole('listbox')).getByRole('option', { name: /metro area/i }));
    expect(within(dialog).queryByRole('slider')).not.toBeInTheDocument();

    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const controlledWarnings = consoleErrors.mock.calls.map((c) => c.map(String).join(' ')).filter((m) => /controlled|uncontrolled/i.test(m));
    consoleErrors.mockRestore();
    expect(controlledWarnings).toEqual([]);
  });

  it('read-only mode shows "Not set." for a location without coordinates, and closes', async () => {
    const consoleErrors = vi.spyOn(console, 'error');
    server.use(
      // A coverage record with its optional values missing: the dialog shows the defaults.
      http.get(P('/workspaces/:ws/locations/:id/geo-coverage'), ({ params }) =>
        HttpResponse.json({
          id: 'geo-bare',
          location_id: String(params.id),
          coverage_type: 'radius',
          radius_miles: null,
          included_markets: null,
          excluded_markets: null,
          online_global: false,
        }),
      ),
      http.get(P('/workspaces/:ws/locations'), () =>
        HttpResponse.json([
          {
            id: 'loc-bare',
            name: 'Bare Site',
            address: null,
            city: null,
            state_province: null,
            country: null,
            postal_code: null,
            timezone: null,
            currency: null,
            latitude: null,
            longitude: null,
            local_competitors: null,
            local_notes: null,
          },
        ]),
      ),
    );
    const screen = await renderAs('viewer', '/locations');
    await screen.findByText('Bare Site');
    await screen.user.click(buttons(screen, /^view details & service area$/i)[0]!);
    const dialog = await screen.findByRole('dialog', { name: /location details/i });
    expect(within(dialog).getByText('Not set.')).toBeInTheDocument();
    expect(within(dialog).queryByText(/enter an address and geocode/i)).not.toBeInTheDocument();
    // No editing hints in a read-only view: no example placeholders, no instructions.
    expect(within(dialog).getByLabelText(/location name/i).getAttribute('placeholder') ?? '').toBe('');
    expect(within(dialog).getByLabelText(/street address/i).getAttribute('placeholder') ?? '').toBe('');
    expect(within(dialog).queryByText('Auto-filled by geocoding.')).not.toBeInTheDocument();
    await screen.user.click(within(dialog).getByRole('tab', { name: /service area|coverage/i }));
    expect(within(dialog).getByText('Service radius — 25 miles')).toBeInTheDocument();
    expect(within(dialog).queryByText(/drag to set how far out customers come from/i)).not.toBeInTheDocument();
    // The footer Close (text), not the corner X (aria-label only).
    const footerClose = within(dialog)
      .getAllByRole('button', { name: /^close$/i })
      .find((button) => button.textContent?.trim() === 'Close');
    await screen.user.click(footerClose!);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    const valueWarnings = consoleErrors.mock.calls.map((c) => c.map(String).join(' ')).filter((m) => /should not be null|controlled|uncontrolled/i.test(m));
    consoleErrors.mockRestore();
    expect(valueWarnings).toEqual([]);
  });
});

describe('scout controls send what they say, and show a run in flight (editor)', () => {
  function holdRun() {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(P('/workspaces/:ws/scout-requests/:id/run'), async () => {
        await held;
        return undefined;
      }),
    );
    return () => act(() => release());
  }
  const posted = (suffix: string) => seen.filter((r) => r.startsWith('POST ') && r.endsWith(suffix)).length;

  beforeEach(() => {
    seen = [];
    server.events.on('request:start', onRequest);
  });
  afterEach(() => {
    server.events.removeListener('request:start', onRequest);
  });

  it('list rows: pause, resume, run (disabled for that row while it is queued)', async () => {
    const screen = await renderAs('owner', '/scout-requests');
    await screen.findByText('Nairobi demand scan');
    await screen.user.click(buttons(screen, /^pause$/i)[0]!);
    await waitFor(() => expect(posted('/scout-requests/scout-loc-dallas/pause')).toBe(1));
    await screen.user.click(buttons(screen, /^resume$/i)[0]!);
    await waitFor(() => expect(posted('/scout-requests/scout-loc-london/resume')).toBe(1));
    const release = holdRun();
    const [firstRun] = buttons(screen, /run now/i);
    await screen.user.click(firstRun!);
    await waitFor(() => expect(firstRun).toBeDisabled());
    // Only the row being run is busy.
    expect(buttons(screen, /run now/i)[1]).toBeEnabled();
    release();
    await waitFor(() => expect(firstRun).toBeEnabled());
  });

  it('empty list: "New scout request" in the empty state opens the dialog', async () => {
    server.use(NO_SCOUTS);
    const screen = await renderAs('owner', '/scout-requests');
    await screen.findByText(/no scout requests yet/i);
    const [, emptyState] = buttons(screen, /new scout request/i);
    await screen.user.click(emptyState!);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });

  it('detail: pause, resume, and run from the no-opportunities empty state', async () => {
    server.use(SCHEDULE_404, NO_OPPORTUNITIES);
    const screen = await renderAs('owner', '/scout-requests/scout-loc-dallas');
    await screen.findByText(/no opportunities yet/i);
    await screen.user.click(buttons(screen, /^pause$/i)[0]!);
    await waitFor(() => expect(posted('/scout-requests/scout-loc-dallas/pause')).toBe(1));
    const release = holdRun();
    const runs = buttons(screen, /run now/i);
    await screen.user.click(runs[1]!); // the empty-state button
    await waitFor(() => expect(runs[1]).toBeDisabled());
    release();
    await waitFor(() => expect(posted('/scout-requests/scout-loc-dallas/run')).toBe(1));
  });

  it('detail of a paused request: resume', async () => {
    server.use(SCHEDULE_404);
    const screen = await renderAs('owner', '/scout-requests/scout-loc-london');
    await screen.findByRole('heading', { name: 'London demand scan' });
    await screen.user.click(buttons(screen, /^resume$/i)[0]!);
    await waitFor(() => expect(posted('/scout-requests/scout-loc-london/resume')).toBe(1));
  });

  it('campaign context: "Add product" in the empty state opens the dialog', async () => {
    const screen = await renderAs('owner', '/context');
    await screen.findByText(/no products yet/i);
    const [, emptyState] = buttons(screen, /^add product$/i);
    await screen.user.click(emptyState!);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
  });
});

describe('a confirm pressed while its dialog is still closing does nothing (campaign context)', () => {
  it('Cancel, then "Remove" during the exit animation: no request, no error, the item stays', async () => {
    server.use(
      http.get(P('/workspaces/:ws/products'), () =>
        HttpResponse.json([{ id: 'prod-1', kind: 'products', name: 'Signature Cut', description: null }]),
      ),
    );
    const deletes: string[] = [];
    const onStart = ({ request }: { request: Request }) => {
      if (request.method === 'DELETE') deletes.push(new URL(request.url).pathname);
    };
    server.events.on('request:start', onStart);
    // A rejected click handler surfaces nowhere on the page, only as an unhandled rejection.
    const node = (globalThis as unknown as { process: { on(e: string, f: (r: unknown) => void): void; off(e: string, f: (r: unknown) => void): void } }).process;
    const rejections: unknown[] = [];
    const onRejection = (reason: unknown) => {
      rejections.push(reason);
    };
    node.on('unhandledRejection', onRejection);
    const animations = emulateExitAnimations();
    try {
      const screen = await renderAs('owner', '/context');
      await screen.findByText('Signature Cut');
      await screen.user.click(screen.getByRole('button', { name: /^remove product$/i }));
      const confirm = await screen.findByRole('dialog', { name: /remove this product/i });
      const removeButton = within(confirm).getByRole('button', { name: /^remove$/i });
      await screen.user.click(within(confirm).getByRole('button', { name: /^cancel$/i }));
      expect(confirm).toHaveAttribute('data-state', 'closed');
      expect(removeButton).toBeInTheDocument();
      expect(removeButton).toBeEnabled();
      fireEvent.click(removeButton);
      await new Promise((resolve) => setTimeout(resolve, 50));
      animations.finish();
      await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
      expect(deletes).toEqual([]);
      expect(rejections).toEqual([]);
      expect(screen.getByText('Signature Cut')).toBeInTheDocument();
    } finally {
      animations.restore();
      node.off('unhandledRejection', onRejection);
      server.events.removeListener('request:start', onStart);
    }
  });
});

describe('removing a context item (campaign context)', () => {
  it('asks, sends one DELETE, closes the confirm, and the item leaves the list', async () => {
    let products = [{ id: 'prod-1', kind: 'products', name: 'Signature Cut', description: null }];
    let deletes = 0;
    server.use(
      http.get(P('/workspaces/:ws/products'), () => HttpResponse.json(products)),
      http.delete(P('/workspaces/:ws/products/:id'), () => {
        deletes += 1;
        products = [];
        return new HttpResponse(null, { status: 204 });
      }),
    );
    const screen = await renderAs('owner', '/context');
    await screen.findByText('Signature Cut');
    await screen.user.click(screen.getByRole('button', { name: /^remove product$/i }));
    const confirm = await screen.findByRole('dialog', { name: /remove this product/i });
    await screen.user.click(within(confirm).getByRole('button', { name: /^remove$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(deletes).toBe(1);
    expect(await screen.findByText(/no products yet/i)).toBeInTheDocument();
  });
});

describe('empty states speak to what the role can do', () => {
  const NO_LOCATIONS = http.get(P('/workspaces/:ws/locations'), () => HttpResponse.json([]));
  it.each([
    ['owner', 'Add your first market to start scouting.'],
    ['viewer', 'No locations have been added to this workspace yet.'],
  ] as const)('Locations, %s', async (role, copy) => {
    server.use(NO_LOCATIONS);
    const screen = await renderAs(role, '/locations');
    expect(await screen.findByText(/no locations yet/i)).toBeInTheDocument();
    expect(screen.getByText(new RegExp(`^${copy.replace(/[.]/g, '\\.')}`))).toBeInTheDocument();
  });

  it.each([
    ['owner', 'Create your first scout request to start collecting market signals and generating opportunities.'],
    ['viewer', 'No scout requests have been created in this workspace yet.'],
  ] as const)('Scout requests, %s', async (role, copy) => {
    server.use(NO_SCOUTS);
    const screen = await renderAs(role, '/scout-requests');
    expect(await screen.findByText(/no scout requests yet/i)).toBeInTheDocument();
    expect(screen.getByText(copy)).toBeInTheDocument();
  });

  it.each([
    ['owner', 'Run this scout to process fixture signals into scored, explainable opportunities.'],
    ['viewer', 'This scout has not produced any opportunities yet.'],
  ] as const)('Scout request detail, %s', async (role, copy) => {
    server.use(SCHEDULE_404, NO_OPPORTUNITIES);
    const screen = await renderAs(role, '/scout-requests/scout-loc-dallas');
    expect(await screen.findByText(/no opportunities yet/i)).toBeInTheDocument();
    expect(screen.getByText(copy)).toBeInTheDocument();
  });
});

describe('a finished run hands its Run buttons back (scout request detail)', () => {
  it('both Run buttons are busy only while this scout runs, and usable again afterwards', async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let runs = 0;
    server.use(
      SCHEDULE_404,
      NO_OPPORTUNITIES,
      http.post(P('/workspaces/:ws/scout-requests/:id/run'), async () => {
        runs += 1;
        if (runs === 1) await held;
        return undefined;
      }),
    );
    const screen = await renderAs('owner', '/scout-requests/scout-loc-dallas');
    await screen.findByText(/no opportunities yet/i);
    const [header, emptyState] = buttons(screen, /run now/i);
    expect(header).toBeEnabled();
    expect(emptyState).toBeEnabled();
    await screen.user.click(header!);
    await waitFor(() => expect(header).toBeDisabled());
    expect(emptyState).toBeDisabled();
    act(() => release());
    await waitFor(() => expect(header).toBeEnabled());
    expect(emptyState).toBeEnabled();
    // A second, instant run leaves them usable too.
    await screen.user.click(emptyState!);
    await waitFor(() => expect(runs).toBe(2));
    await waitFor(() => expect(emptyState).toBeEnabled());
    expect(header).toBeEnabled();
  });
});

describe('Cancel is offered only for a job that can still be cancelled (editor)', () => {
  const job = (id: string, status: string) => ({
    id,
    job_type: 'scout_run',
    status,
    attempt_count: 1,
    max_attempts: 3,
    priority: 0,
    scout_request_id: 'scout-loc-dallas',
    location_id: 'loc-dallas',
    last_error_code: null,
    result_summary: null,
    scheduled_for: null,
    started_at: '2026-06-01T12:00:00Z',
    completed_at: status === 'running' ? null : '2026-06-01T12:05:00Z',
    cancel_requested_at: null,
    cancelled_at: null,
    created_at: '2026-06-01T12:00:00Z',
    updated_at: '2026-06-01T12:05:00Z',
  });

  it('a running job has Cancel; succeeded, failed and cancelled ones do not', async () => {
    server.use(
      SCHEDULE_404,
      http.get(P('/workspaces/:ws/jobs'), () =>
        HttpResponse.json({
          items: [job('job-run', 'running'), job('job-ok', 'succeeded'), job('job-bad', 'failed'), job('job-off', 'cancelled')],
          total: 4,
          limit: 10,
          offset: 0,
        }),
      ),
    );
    const screen = await renderAs('owner', '/scout-requests/scout-loc-dallas');
    await screen.findByText(/^running$/i);
    await screen.findByText(/^succeeded$/i);
    expect(buttons(screen, /^cancel$/i)).toHaveLength(1);
  });
});

describe('a role string this build does not recognise is treated as no role (fail closed)', () => {
  it('offers no mutation control and no misleading permission note', async () => {
    orgModel.setMembership('org-1', 'user-1', 'auditor' as ModelRole);
    const screen = renderApp(
      <>
        <App />
        <RoleProbe />
      </>,
      { route: '/onboarding' },
    );
    await waitFor(() => expect(screen.getByTestId('m4-role-probe')).toHaveTextContent(/^auditor$/));
    await screen.user.click(await screen.findByRole('button', { name: /save & continue/i }));
    await screen.user.type(await screen.findByLabelText(/business \/ brand name/i), 'Unknown Role Brand');
    while (screen.queryByRole('button', { name: /save & continue/i })) {
      await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));
    }
    expect(buttons(screen, /finish onboarding/i)).toHaveLength(0);
    // The permission note speaks only about a KNOWN role; an unknown one gets nothing.
    expect(screen.queryByText(/you do not have permission to finish onboarding/i)).not.toBeInTheDocument();
    const [locations] = screen.getAllByRole('link', { name: /^locations$/i });
    await screen.user.click(locations!);
    await screen.findByText('Dallas');
    expect(buttons(screen, /add location|edit & service area/i)).toHaveLength(0);
  });
});

describe('an API denial stays safe when a hidden action is invoked anyway (§54)', () => {
  it('a 403 from the API client is a clean ApiError that keeps the session', async () => {
    server.use(
      http.post(P('/workspaces/:ws/locations'), () =>
        HttpResponse.json(
          { error: { code: 'permission_denied', message: "Role 'viewer' is not permitted for this action." } },
          { status: 403 },
        ),
      ),
    );
    const screen = await renderAs('viewer', '/locations');
    await screen.findByText('Dallas');
    // The UI does not offer this to a viewer; invoke it through the client anyway.
    const error = await api
      .createLocation('ws-1', { name: 'Forbidden', country: 'US' } as Parameters<typeof api.createLocation>[1])
      .then(
        () => null,
        (e: unknown) => e,
      );
    expect(error).toBeInstanceOf(ApiError);
    expect((error as ApiError).status).toBe(403);
    expect((error as ApiError).message).toBe("Role 'viewer' is not permitted for this action.");
    // A 403 is not a 401: the session survives and the page keeps rendering.
    expect(localStorage.getItem('signalnest-token')).toBe('test-token');
    expect(screen.getByText('Dallas')).toBeInTheDocument();
  });

  it('a stale editor session that the server refuses gets an error message, not a broken page', async () => {
    // The session still says MARKETER; the server has since refused the role.
    server.use(
      http.post(P('/workspaces/:ws/scout-requests/:id/run'), () =>
        HttpResponse.json(
          { error: { code: 'permission_denied', message: "Role 'viewer' is not permitted for this action." } },
          { status: 403 },
        ),
      ),
    );
    const screen = await renderAs('marketer', '/scout-requests');
    await screen.findByText('Dallas demand scan');
    await screen.user.click(buttons(screen, /run now/i)[0]!);
    expect(await screen.findByText(/could not queue scout/i)).toBeInTheDocument();
    expect(screen.getByText("Role 'viewer' is not permitted for this action.")).toBeInTheDocument();
    expect(screen.getByText('Dallas demand scan')).toBeInTheDocument();
    expect(localStorage.getItem('signalnest-token')).toBe('test-token');
  });
});
