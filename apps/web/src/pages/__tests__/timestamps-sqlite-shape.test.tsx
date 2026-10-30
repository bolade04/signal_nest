import { within } from '@testing-library/react';
import { getResponse, http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { handlers } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

/**
 * UI-AUDIT F6 / P7, rendered: the scout list against the timestamp shape the API really
 * serves in SQLite (local/demo) mode — naive UTC with microseconds and no offset, as
 * captured from the local preview at 7a183a12 (P6-W10-UIFIX-01 raw/010, raw/013). The shared
 * fixtures carry `Z` timestamps (the PostgreSQL shape), which is why this passed CI.
 *
 * The viewer is put in America/Chicago (UTC-5 on these dates) so the result does not depend
 * on the machine's zone; Node re-reads `TZ` at run time.
 */

const env = (globalThis as unknown as { process: { env: Record<string, string | undefined> } }).process.env;
const ORIGINAL_TZ = env.TZ;
afterEach(() => {
  env.TZ = ORIGINAL_TZ;
});

/** "2026-10-29T14:05:34.066648": the SQLite serialization of a UTC instant `msAgo` old. */
function naiveUtc(msAgo: number): string {
  return new Date(Date.now() - msAgo).toISOString().replace(/\.(\d{3})Z$/, '.$1648');
}

describe('scout list with SQLite-shaped timestamps (UI-AUDIT P7)', () => {
  it('a run five minutes old reads "last run 5 minutes ago" for a viewer in Chicago', async () => {
    env.TZ = 'America/Chicago';
    expect(Intl.DateTimeFormat().resolvedOptions().timeZone).toBe('America/Chicago');
    const lastRun = naiveUtc(5 * 60_000 + 10_000);
    expect(lastRun).toMatch(/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}$/);
    server.use(
      http.get(`*${API_PREFIX}/workspaces/:ws/scout-requests`, async ({ request }) => {
        const rows = (await (await getResponse(handlers, request))!.json()) as Record<string, unknown>[];
        return HttpResponse.json(
          rows.map((r) => ({ ...r, last_run_at: lastRun, created_at: naiveUtc(3_600_000), updated_at: lastRun })),
        );
      }),
    );
    const screen = renderApp(<App />, { route: '/scout-requests' });
    const first = await screen.findByRole('link', { name: /dallas/i });
    const card = first.closest('.rounded-lg') as HTMLElement;
    expect(within(card).getByText(/last run/)).toHaveTextContent('last run 5 minutes ago');
    expect(screen.queryByText(/last run in \d+ hours?/)).toBeNull();
  });
});
