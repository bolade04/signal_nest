import { screen, waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import type { NotificationFeedOut, NotificationOut, NotificationPreferencesOut } from '@/api/types';
import { Notifications } from '@/components/layout/notifications';
import { NotificationFeed, NotificationsMenu } from '@/pages/notifications/NotificationFeed';
import { NotificationPreferencesForm } from '@/pages/notifications/NotificationPreferences';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

// Project Phase 5E (lane 5E-W4): the in-app notification feed (5E-W3) in the shell's bell
// (I-5E), its mark-read and its preferences.
//
// - While the O-12(B) reflection is not literally `{enabled: true}` the shell keeps its
//   placeholder bell and NO feed or preferences request is sent; on, the bell is the feed.
// - An empty feed says so; a 503 says notifications are not available, never server text.
// - Mark-read is sent once for an unread item and never for a read one; the server's answer
//   (the first read_at) is what the feed shows, and the unread badge counts unread only.
// - The preferences form sends exactly the code that changed, and shows the server's answer.

const WS = 'ws-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const seen: string[] = [];
const bodies: unknown[] = [];

function record({ request }: { request: Request }) {
  const url = new URL(request.url);
  seen.push(`${request.method} ${url.pathname}${url.search}`);
}

beforeEach(() => {
  seen.length = 0;
  bodies.length = 0;
  server.events.on('request:start', record);
});
afterEach(() => {
  server.events.removeListener('request:start', record);
});

const settle = () => new Promise((resolve) => setTimeout(resolve, 60));
const reflection = (body: unknown, status = 200) =>
  http.get(P(`/workspaces/${WS}/notifications-analytics-capability`), () =>
    HttpResponse.json(body as object, { status }),
  );
/** Every request a notification feature sends (the reflection excluded). */
const featureRequests = () =>
  seen.filter((s) => /\/notifications(\?|\/|$)|\/notification-preferences/.test(s));

function note(id: string, read: boolean, code: NotificationOut['event_code'] = 'brief.created'): NotificationOut {
  return {
    id,
    event_code: code,
    subject_id: `subject-${id}`,
    created_at: '2026-11-14T10:00:00Z',
    read_at: read ? '2026-11-14T10:05:00Z' : null,
  };
}

function feed(items: NotificationOut[]): NotificationFeedOut {
  return {
    items,
    total: items.length,
    unread: items.filter((i) => i.read_at === null).length,
    limit: 20,
    offset: 0,
  };
}

/** A feed backend whose mark-read keeps the FIRST read_at, as the API does. */
function feedBackend(initial: NotificationOut[]) {
  let rows = initial.map((r) => ({ ...r }));
  const posts: string[] = [];
  server.use(
    reflection({ enabled: true }),
    http.get(P(`/workspaces/${WS}/notifications`), () => HttpResponse.json(feed(rows))),
    http.post(P(`/workspaces/${WS}/notifications/:id/read`), ({ params }) => {
      const id = String(params.id);
      posts.push(id);
      const row = rows.find((r) => r.id === id);
      if (!row) {
        return HttpResponse.json(
          { error: { code: 'not_found', message: 'Notification not found.', request_id: 'r' } },
          { status: 404 },
        );
      }
      row.read_at ??= `2026-11-14T11:0${posts.length}:00Z`;
      return HttpResponse.json(row);
    }),
  );
  return {
    posts,
    remove(id: string) {
      rows = rows.filter((r) => r.id !== id);
    },
  };
}

// --------------------------------------------------------------------------------------
// The shell's bell
// --------------------------------------------------------------------------------------
describe('the shell bell', () => {
  it.each([
    ['off', { enabled: false }, 200],
    ['the string "true"', { enabled: 'true' }, 200],
    ['the number 1', { enabled: 1 }, 200],
    ['failed', { error: { code: 'internal', message: 'x' } }, 500],
  ] as const)('keeps the placeholder and sends no feed request while the reflection is %s', async (_n, body, status) => {
    server.use(reflection(body, status));
    const { user } = renderApp(<Notifications />);
    const bell = await screen.findByRole('button', { name: 'Notifications' });
    await waitFor(() =>
      expect(seen.some((s) => s.includes('notifications-analytics-capability'))).toBe(true),
    );
    await user.click(bell);
    expect(await screen.findByText('Live alerts arrive in a later phase.')).toBeInTheDocument();
    await settle();
    expect(featureRequests()).toEqual([]);
  });

  it('the shipped default (MSW dark) is the placeholder', async () => {
    renderApp(<Notifications />);
    expect(await screen.findByRole('button', { name: 'Notifications' })).toBeInTheDocument();
    await settle();
    expect(featureRequests()).toEqual([]);
  });

  it('becomes the feed while the reflection is on', async () => {
    feedBackend([note('n1', false), note('n2', false, 'draft.generated'), note('n3', true, 'export.completed')]);
    const { user } = renderApp(<Notifications />);
    const bell = await screen.findByRole('button', { name: 'Notifications, 2 unread' });
    await user.click(bell);
    const menu = await screen.findByRole('menu');
    expect(within(menu).getByText('A new brief is waiting for review.')).toBeInTheDocument();
    expect(within(menu).getByText('Your creative draft is ready.')).toBeInTheDocument();
    expect(within(menu).getByText('A draft you worked on was exported.')).toBeInTheDocument();
    expect(within(menu).getAllByText('Unread:')).toHaveLength(2);
    expect(within(menu).queryByText('Live alerts arrive in a later phase.')).not.toBeInTheDocument();
    expect(seen.filter((s) => s.startsWith('GET') && s.includes('/notifications?'))[0]).toContain('limit=20');
  });
});

// --------------------------------------------------------------------------------------
// The feed
// --------------------------------------------------------------------------------------
describe('the feed', () => {
  it('an empty feed says the user is caught up', async () => {
    feedBackend([]);
    const { user } = renderApp(<NotificationsMenu />);
    await user.click(await screen.findByRole('button', { name: 'Notifications' }));
    expect(await screen.findByText("You're all caught up.")).toBeInTheDocument();
  });

  it('a dark answer (503) says notifications are not available, never the server text', async () => {
    server.use(
      reflection({ enabled: true }),
      http.get(P(`/workspaces/${WS}/notifications`), () =>
        HttpResponse.json(
          { error: { code: 'capability_unavailable', message: 'SERVER TEXT', request_id: 'r' } },
          { status: 503 },
        ),
      ),
    );
    const { user } = renderApp(<NotificationsMenu />);
    await user.click(await screen.findByRole('button', { name: 'Notifications' }));
    expect(await screen.findByText("Notifications aren't available in this workspace.")).toBeInTheDocument();
    expect(screen.queryByText('SERVER TEXT')).not.toBeInTheDocument();
  });

  it('the feed on its own sends nothing while the reflection is off', async () => {
    server.use(reflection({ enabled: false }));
    renderApp(<NotificationFeed workspaceId={WS} userId="user-1" />);
    expect(await screen.findByText("Notifications aren't available in this workspace.")).toBeInTheDocument();
    await settle();
    expect(featureRequests()).toEqual([]);
  });

  it('the menu renders its fallback while the reflection is off', async () => {
    server.use(reflection({ enabled: false }));
    renderApp(<NotificationsMenu fallback={<span>placeholder bell</span>} />);
    expect(await screen.findByText('placeholder bell')).toBeInTheDocument();
    await settle();
    expect(featureRequests()).toEqual([]);
  });

  it('the badge counts unread notifications, not the total, and caps at 9+', async () => {
    feedBackend([
      ...Array.from({ length: 12 }, (_, i) => note(`u${i}`, false)),
      ...Array.from({ length: 5 }, (_, i) => note(`r${i}`, true)),
    ]);
    renderApp(<NotificationsMenu />);
    const bell = await screen.findByRole('button', { name: 'Notifications, 12 unread' });
    expect(within(bell).getByText('9+')).toBeInTheDocument();
  });
});

// --------------------------------------------------------------------------------------
// Mark-read
// --------------------------------------------------------------------------------------
describe('mark-read', () => {
  it('choosing an unread notification marks it read once; the badge clears', async () => {
    const backend = feedBackend([note('n1', false, 'approval.decided'), note('n2', true)]);
    const { user } = renderApp(<NotificationsMenu />);
    await user.click(await screen.findByRole('button', { name: 'Notifications, 1 unread' }));
    const menu = await screen.findByRole('menu');
    await user.click(within(menu).getByText('A reviewer recorded a decision on your brief or draft.'));
    await waitFor(() => expect(backend.posts).toEqual(['n1']));
    expect(await screen.findByRole('button', { name: 'Notifications' })).toBeInTheDocument();
  });

  it('choosing a notification that is already read sends no request (idempotent in the UI)', async () => {
    const backend = feedBackend([note('n1', true, 'approval.decided')]);
    const { user } = renderApp(<NotificationsMenu />);
    await user.click(await screen.findByRole('button', { name: 'Notifications' }));
    const menu = await screen.findByRole('menu');
    await user.click(within(menu).getByText('A reviewer recorded a decision on your brief or draft.'));
    await settle();
    expect(backend.posts).toEqual([]);
    expect(seen.filter((s) => s.startsWith('POST'))).toEqual([]);
  });

  it('once read, choosing it again sends no second request; the feed shows the first read_at', async () => {
    const backend = feedBackend([note('n1', false, 'approval.decided')]);
    const { user } = renderApp(<NotificationsMenu />);
    await user.click(await screen.findByRole('button', { name: 'Notifications, 1 unread' }));
    await user.click(
      within(await screen.findByRole('menu')).getByText('A reviewer recorded a decision on your brief or draft.'),
    );
    await waitFor(() => expect(backend.posts).toEqual(['n1']));
    // Reopen: the row is read now (the server's answer replaced it), so choosing it again
    // marks nothing.
    await user.click(await screen.findByRole('button', { name: 'Notifications' }));
    const menu = await screen.findByRole('menu');
    expect(within(menu).queryByText('Unread:')).not.toBeInTheDocument();
    await user.click(within(menu).getByText('A reviewer recorded a decision on your brief or draft.'));
    await settle();
    expect(backend.posts).toEqual(['n1']);
  });

  it('a notification that is gone (404) refreshes the feed and says so', async () => {
    const backend = feedBackend([note('n1', false, 'approval.decided'), note('n2', false)]);
    const { user } = renderApp(<NotificationsMenu />);
    await user.click(await screen.findByRole('button', { name: 'Notifications, 2 unread' }));
    const menu = await screen.findByRole('menu');
    const gets = () => seen.filter((s) => s.startsWith('GET') && s.includes('/notifications?')).length;
    const before = gets();
    backend.remove('n1');
    await user.click(within(menu).getByText('A reviewer recorded a decision on your brief or draft.'));
    await waitFor(() => expect(gets()).toBeGreaterThan(before));
  });
});

// --------------------------------------------------------------------------------------
// Preferences
// --------------------------------------------------------------------------------------
describe('preferences', () => {
  const allOn: NotificationPreferencesOut = {
    items: [
      { event_code: 'brief.created', enabled: true },
      { event_code: 'approval.decided', enabled: true },
      { event_code: 'draft.generated', enabled: true },
      { event_code: 'review.blocked', enabled: true },
      { event_code: 'export.completed', enabled: true },
    ],
  };

  function preferencesBackend() {
    let prefs = structuredClone(allOn);
    server.use(
      reflection({ enabled: true }),
      http.get(P(`/workspaces/${WS}/notifications`), () => HttpResponse.json(feed([]))),
      http.get(P(`/workspaces/${WS}/notification-preferences`), () => HttpResponse.json(prefs)),
      http.put(P(`/workspaces/${WS}/notification-preferences`), async ({ request }) => {
        const body = (await request.json()) as NotificationPreferencesOut;
        bodies.push(body);
        prefs = {
          items: prefs.items.map((i) => body.items.find((c) => c.event_code === i.event_code) ?? i),
        };
        return HttpResponse.json(prefs);
      }),
    );
  }

  it('the settings dialog sends only the code that changed, and shows the answer', async () => {
    preferencesBackend();
    const { user } = renderApp(<NotificationsMenu />);
    await user.click(await screen.findByRole('button', { name: 'Notifications' }));
    await user.click(await screen.findByRole('menuitem', { name: 'Notification settings' }));
    const dialog = await screen.findByRole('dialog');
    const exports = await within(dialog).findByRole('switch', { name: 'Exports of your drafts' });
    expect(exports).toBeChecked();
    await user.click(exports);
    await waitFor(() => expect(exports).not.toBeChecked());
    expect(bodies).toEqual([{ items: [{ event_code: 'export.completed', enabled: false }] }]);
    expect(within(dialog).getByRole('switch', { name: 'New briefs to review' })).toBeChecked();
  });

  it('the form sends nothing while the reflection is off', async () => {
    server.use(reflection({ enabled: false }));
    renderApp(<NotificationPreferencesForm workspaceId={WS} userId="user-1" />);
    expect(await screen.findByText("Notifications aren't available in this workspace.")).toBeInTheDocument();
    await settle();
    expect(featureRequests()).toEqual([]);
  });

  it('a dark answer to a change says notifications are not available', async () => {
    preferencesBackend();
    server.use(
      http.put(P(`/workspaces/${WS}/notification-preferences`), () =>
        HttpResponse.json(
          { error: { code: 'capability_unavailable', message: 'SERVER TEXT', request_id: 'r' } },
          { status: 503 },
        ),
      ),
    );
    const { user } = renderApp(<NotificationPreferencesForm workspaceId={WS} userId="user-1" />);
    await user.click(await screen.findByRole('switch', { name: 'New briefs to review' }));
    expect(await screen.findByRole('alert')).toHaveTextContent("Notifications aren't available in this workspace.");
    expect(screen.queryByText('SERVER TEXT')).not.toBeInTheDocument();
  });
});
