import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse, delay } from 'msw';
import { MemoryRouter } from 'react-router-dom';
import { describe, expect, it } from 'vitest';
import { API_PREFIX } from '@/api/config';
import { queryKeys } from '@/api/queryKeys';
import type {
  NotificationFeedOut,
  NotificationOut,
  NotificationPreferencesOut,
} from '@/api/types';
import { DropdownMenu, DropdownMenuContent, DropdownMenuTrigger } from '@/components/ui/dropdown-menu';
import { NotificationFeed } from '@/pages/notifications/NotificationFeed';
import { NotificationPreferencesForm } from '@/pages/notifications/NotificationPreferences';
import { server } from '@/test/server';

// Project Phase 5E, I-5E fold of review R-5E-B r1 (finding F-B-2): the per-user cache
// dimension at the four places `useNotifications.ts` uses it, at component level.
//
// `cf3-query-keys.test.ts` pins the key FACTORY. These tests pin that the hooks pass the
// signed-in user to it: the feed query, the mark-read cache write, the preferences query and
// the preferences cache write. ONE QueryClient that is never cleared and never goes stale (the
// worst case for reuse; the app clears its client on every user change) serves user A and then
// user B in the same workspace. B is never shown anything of A's, B's own request is sent,
// B's writes land under B's key only, and the cache holds exactly one entry per user: a hook
// that passed a constant for the user would share one entry, or write a third.
//
// Started from the reviewer's probe `zz-r5eb-cache-probe.test.tsx` (its five cases are kept);
// the two cache-write cases and the exact-entry assertions are added here.

const WS = 'ws-1';
const P = (path: string) => `*${API_PREFIX}${path}`;
const BLOCKED = 'A draft you requested was blocked by its content review.';
const CAUGHT_UP = "You're all caught up.";
const CODES = [
  'brief.created',
  'approval.decided',
  'draft.generated',
  'review.blocked',
  'export.completed',
] as const;

function note(id: string, code: NotificationOut['event_code']): NotificationOut {
  return { id, event_code: code, subject_id: `s-${id}`, created_at: '2026-11-14T10:00:00Z', read_at: null };
}

const feed = (items: NotificationOut[]): NotificationFeedOut => ({
  items,
  total: items.length,
  unread: items.filter((item) => item.read_at === null).length,
  limit: 20,
  offset: 0,
});

const preferences = (off: string | null): NotificationPreferencesOut => ({
  items: CODES.map((event_code) => ({ event_code, enabled: event_code !== off })),
});

const reflectionOn = () =>
  http.get(P(`/workspaces/:ws/notifications-analytics-capability`), () =>
    HttpResponse.json({ enabled: true }),
  );

/** A client that keeps every entry and never refetches one: nothing but the key separates users. */
const sticky = () =>
  new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: Infinity, staleTime: Infinity } },
  });

function mount(client: QueryClient, ws: string, userId: string, what: 'feed' | 'prefs' = 'feed') {
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        {what === 'feed' ? (
          <DropdownMenu open>
            <DropdownMenuTrigger>bell</DropdownMenuTrigger>
            <DropdownMenuContent>
              <NotificationFeed workspaceId={ws} userId={userId} />
            </DropdownMenuContent>
          </DropdownMenu>
        ) : (
          <NotificationPreferencesForm workspaceId={ws} userId={userId} />
        )}
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

/** Every cached query key that ends in `leaf`, in the order the entries were created. */
function entries(client: QueryClient, leaf: string): unknown[] {
  return client
    .getQueryCache()
    .getAll()
    .map((query) => query.queryKey)
    .filter((key) => key[key.length - 1] === leaf);
}

describe('one uncleared cache, two users of one workspace', () => {
  it('the feed query: user B is never shown user A’s cached feed and sends their own request', async () => {
    const client = sticky();
    let who = 'A';
    let feedRequests = 0;
    server.use(
      reflectionOn(),
      http.get(P(`/workspaces/:ws/notifications`), async () => {
        feedRequests += 1;
        if (who === 'B') {
          await delay(150);
          return HttpResponse.json(feed([]));
        }
        return HttpResponse.json(feed([note('a1', 'review.blocked')]));
      }),
    );
    const first = mount(client, WS, 'user-a');
    expect(await screen.findByText(BLOCKED)).toBeInTheDocument();
    expect(client.getQueryData(queryKeys.notifications(WS, 'user-a'))).toBeTruthy();
    first.unmount();

    who = 'B';
    const before = feedRequests;
    mount(client, WS, 'user-b');
    // While B's own request is pending, nothing of A's is on screen.
    expect(screen.queryByText(BLOCKED)).not.toBeInTheDocument();
    expect(await screen.findByText(CAUGHT_UP)).toBeInTheDocument();
    expect(screen.queryByText(BLOCKED)).not.toBeInTheDocument();
    expect(feedRequests).toBe(before + 1);
    // A's entry is still in the (uncleared) cache, under A's key only; B has their own.
    expect(client.getQueryData<NotificationFeedOut>(queryKeys.notifications(WS, 'user-a'))?.items).toHaveLength(1);
    expect(client.getQueryData<NotificationFeedOut>(queryKeys.notifications(WS, 'user-b'))?.items).toEqual([]);
    expect(entries(client, 'notifications')).toEqual([
      queryKeys.notifications(WS, 'user-a'),
      queryKeys.notifications(WS, 'user-b'),
    ]);
  });

  it('the mark-read cache write: user B’s answer lands in user B’s feed only', async () => {
    const client = sticky();
    const user = userEvent.setup();
    let who = 'A';
    const posts: string[] = [];
    server.use(
      reflectionOn(),
      http.get(P(`/workspaces/:ws/notifications`), () =>
        HttpResponse.json(
          feed(who === 'A' ? [note('a1', 'review.blocked')] : [note('b1', 'draft.generated')]),
        ),
      ),
      http.post(P(`/workspaces/:ws/notifications/:id/read`), ({ params }) => {
        posts.push(String(params.id));
        return HttpResponse.json({
          ...note(String(params.id), 'draft.generated'),
          read_at: '2026-11-14T11:00:00Z',
        });
      }),
    );
    const first = mount(client, WS, 'user-a');
    expect(await screen.findByText(BLOCKED)).toBeInTheDocument();
    first.unmount();
    const cachedForA = client.getQueryData<NotificationFeedOut>(queryKeys.notifications(WS, 'user-a'));
    expect(cachedForA?.unread).toBe(1);

    who = 'B';
    mount(client, WS, 'user-b');
    await user.click(await screen.findByText('Your creative draft is ready.'));
    await waitFor(() => expect(posts).toEqual(['b1']));
    // The answer replaced the row in B's cached page and cleared B's unread count.
    await waitFor(() =>
      expect(client.getQueryData<NotificationFeedOut>(queryKeys.notifications(WS, 'user-b'))).toMatchObject({
        unread: 0,
        items: [{ id: 'b1', read_at: '2026-11-14T11:00:00Z' }],
      }),
    );
    // A's cached page is the same object as before: nothing was written to it.
    expect(client.getQueryData(queryKeys.notifications(WS, 'user-a'))).toBe(cachedForA);
    // And no third feed entry exists (a write under a key that names no user would make one).
    expect(entries(client, 'notifications')).toEqual([
      queryKeys.notifications(WS, 'user-a'),
      queryKeys.notifications(WS, 'user-b'),
    ]);
  });

  it('the preferences query: user B is never shown user A’s cached preferences', async () => {
    const client = sticky();
    let who = 'A';
    let requests = 0;
    server.use(
      reflectionOn(),
      http.get(P(`/workspaces/:ws/notification-preferences`), async () => {
        requests += 1;
        if (who === 'B') await delay(150);
        return HttpResponse.json(preferences(who === 'A' ? 'brief.created' : null));
      }),
    );
    const first = mount(client, WS, 'user-a', 'prefs');
    expect(await screen.findByRole('switch', { name: 'New briefs to review' })).not.toBeChecked();
    expect(client.getQueryData(queryKeys.notificationPreferences(WS, 'user-a'))).toBeTruthy();
    first.unmount();

    who = 'B';
    const before = requests;
    mount(client, WS, 'user-b', 'prefs');
    // While B's own request is pending, A's choices are not on screen.
    expect(screen.queryByRole('switch', { name: 'New briefs to review' })).not.toBeInTheDocument();
    expect(await screen.findByRole('switch', { name: 'New briefs to review' })).toBeChecked();
    expect(requests).toBe(before + 1);
    expect(entries(client, 'notification-preferences')).toEqual([
      queryKeys.notificationPreferences(WS, 'user-a'),
      queryKeys.notificationPreferences(WS, 'user-b'),
    ]);
  });

  it('the preferences cache write: user B’s answer replaces user B’s preferences only', async () => {
    const client = sticky();
    const user = userEvent.setup();
    let who = 'A';
    const puts: unknown[] = [];
    server.use(
      reflectionOn(),
      http.get(P(`/workspaces/:ws/notification-preferences`), () =>
        HttpResponse.json(preferences(who === 'A' ? 'brief.created' : null)),
      ),
      http.put(P(`/workspaces/:ws/notification-preferences`), async ({ request }) => {
        puts.push(await request.json());
        return HttpResponse.json(preferences('export.completed'));
      }),
    );
    const first = mount(client, WS, 'user-a', 'prefs');
    expect(await screen.findByRole('switch', { name: 'New briefs to review' })).not.toBeChecked();
    first.unmount();
    const cachedForA = client.getQueryData(queryKeys.notificationPreferences(WS, 'user-a'));
    expect(cachedForA).toEqual(preferences('brief.created'));

    who = 'B';
    mount(client, WS, 'user-b', 'prefs');
    const exportsSwitch = await screen.findByRole('switch', { name: 'Exports of your drafts' });
    expect(exportsSwitch).toBeChecked();
    await user.click(exportsSwitch);
    await waitFor(() =>
      expect(puts).toEqual([{ items: [{ event_code: 'export.completed', enabled: false }] }]),
    );
    // The server's answer is now B's cached preferences, and what B is shown.
    await waitFor(() =>
      expect(client.getQueryData(queryKeys.notificationPreferences(WS, 'user-b'))).toEqual(
        preferences('export.completed'),
      ),
    );
    await waitFor(() =>
      expect(screen.getByRole('switch', { name: 'Exports of your drafts' })).not.toBeChecked(),
    );
    // A's are untouched, and no third preferences entry exists.
    expect(client.getQueryData(queryKeys.notificationPreferences(WS, 'user-a'))).toBe(cachedForA);
    expect(entries(client, 'notification-preferences')).toEqual([
      queryKeys.notificationPreferences(WS, 'user-a'),
      queryKeys.notificationPreferences(WS, 'user-b'),
    ]);
  });
});

describe('one uncleared cache, one user, two workspaces', () => {
  it('the other workspace’s cached feed is not shown', async () => {
    const client = sticky();
    let requests = 0;
    server.use(
      reflectionOn(),
      http.get(P(`/workspaces/:ws/notifications`), async ({ params }) => {
        requests += 1;
        if (params.ws === 'ws-2') {
          await delay(150);
          return HttpResponse.json(feed([]));
        }
        return HttpResponse.json(feed([note('w1', 'draft.generated')]));
      }),
    );
    const first = mount(client, 'ws-1', 'user-a');
    expect(await screen.findByText('Your creative draft is ready.')).toBeInTheDocument();
    first.unmount();
    mount(client, 'ws-2', 'user-a');
    expect(screen.queryByText('Your creative draft is ready.')).not.toBeInTheDocument();
    expect(await screen.findByText(CAUGHT_UP)).toBeInTheDocument();
    expect(requests).toBe(2);
  });

  it('the reflection of one workspace never opens another workspace’s feed', async () => {
    const client = sticky();
    const asked: string[] = [];
    server.use(
      http.get(P(`/workspaces/:ws/notifications-analytics-capability`), ({ params }) =>
        HttpResponse.json({ enabled: params.ws === 'ws-1' }),
      ),
      http.get(P(`/workspaces/:ws/notifications`), ({ params }) => {
        asked.push(String(params.ws));
        return HttpResponse.json(feed([]));
      }),
    );
    const first = mount(client, 'ws-1', 'user-a');
    expect(await screen.findByText(CAUGHT_UP)).toBeInTheDocument();
    first.unmount();
    mount(client, 'ws-2', 'user-a');
    expect(
      await screen.findByText("Notifications aren't available in this workspace."),
    ).toBeInTheDocument();
    await waitFor(() => expect(asked).toEqual(['ws-1']));
  });
});

describe('the key factory', () => {
  it('every notification key carries the workspace, then the user; the reflection carries no user', () => {
    expect(queryKeys.notifications('w', 'u')).toEqual(['workspaces', 'w', 'users', 'u', 'notifications']);
    expect(queryKeys.notificationPreferences('w', 'u')).toEqual([
      'workspaces',
      'w',
      'users',
      'u',
      'notification-preferences',
    ]);
    expect(queryKeys.notificationsAnalyticsCapability('w')).toEqual([
      'workspaces',
      'w',
      'notifications-analytics-capability',
    ]);
    // No two-user collision through a separator-free id pair.
    expect(queryKeys.notifications('w', 'ab')).not.toEqual(queryKeys.notifications('wa', 'b'));
  });
});
