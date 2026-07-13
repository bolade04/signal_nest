import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

const P = (path: string) => `*${API_PREFIX}${path}`;

describe('settings runtime status', () => {
  it('shows the coarse runtime summary and operator infrastructure detail', async () => {
    const screen = renderApp(<App />, { route: '/settings' });

    // The Runtime card renders once the summary query resolves.
    expect(await screen.findByText('Runtime')).toBeInTheDocument();

    // Local mode is surfaced explicitly (not blended with production).
    expect(await screen.findByText(/local \(zero-dependency\)/i)).toBeInTheDocument();

    // The seeded demo user is an operator, so the per-capability infrastructure
    // detail (fetched from the internal endpoint) is shown.
    expect(await screen.findByText(/infrastructure \(operator view\)/i)).toBeInTheDocument();
    for (const name of ['database', 'queue', 'cache', 'vector', 'storage', 'llm']) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
    expect(screen.getAllByText(/^ready$/i).length).toBeGreaterThanOrEqual(6);
  });

  it('hides infrastructure detail from a non-operator customer', async () => {
    // A non-operator session never triggers the internal endpoint.
    server.use(
      http.get(P('/auth/me'), () =>
        HttpResponse.json({
          access_token: 'test-token',
          token_type: 'bearer',
          user: { id: 'user-1', email: 'demo@signalnest.dev', full_name: 'Demo', is_operator: false },
          memberships: [{ organization_id: 'org-1', role: 'owner' }],
        }),
      ),
    );

    const screen = renderApp(<App />, { route: '/settings' });

    // The coarse summary is still available.
    expect(await screen.findByText(/local \(zero-dependency\)/i)).toBeInTheDocument();
    // But the operator-only infrastructure section is not rendered.
    expect(screen.queryByText(/infrastructure \(operator view\)/i)).not.toBeInTheDocument();
  });
});
