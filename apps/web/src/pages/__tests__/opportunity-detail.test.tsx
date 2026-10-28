import { getResponse, http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { handlers } from '@/test/handlers';
import { server } from '@/test/server';
import { renderApp } from '@/test/utils';

describe('opportunity detail', () => {
  it('separates observed evidence from AI inference and flags simulated data', async () => {
    const screen = renderApp(<App />, { route: '/opportunities/opp-loc-dallas-0' });

    expect(await screen.findByRole('heading', { name: /Dallas customers want faster delivery 1/i })).toBeInTheDocument();

    // The two epistemically-distinct sections must both be present and labelled.
    expect(screen.getByRole('heading', { name: /observed evidence/i })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /ai inference/i })).toBeInTheDocument();

    // A traceable source link is exposed.
    const sourceLinks = screen.getAllByRole('link', { name: /view source|source/i });
    expect(sourceLinks.length).toBeGreaterThan(0);
    expect(sourceLinks[0]).toHaveAttribute('href', 'https://example.com/article');

    // Known-limitation disclosure and simulated labelling.
    expect(screen.getByText(/Known limitation/i)).toBeInTheDocument();
    expect(screen.getAllByText(/Simulated/i).length).toBeGreaterThan(0);
  });

  it('lets the user change an opportunity status', async () => {
    const screen = renderApp(<App />, { route: '/opportunities/opp-loc-dallas-0' });
    await screen.findByRole('heading', { name: /Dallas customers want faster delivery 1/i });

    await screen.user.click(screen.getByRole('button', { name: /^save$/i }));
    expect(await screen.findByText(/marked as saved/i)).toBeInTheDocument();
  });

  it('renders no link for an evidence source_url outside the http(s) allow-list', async () => {
    // Once live sources exist (P6-DATA-1) these values come from third parties; the
    // page must never hand a javascript:/data: value to href (P6-AUTH-5 residual).
    server.use(
      http.get(`*${API_PREFIX}/workspaces/:ws/opportunities/:id`, async ({ request }) => {
        // Start from the shared fixture's own answer, then poison only the links.
        const original = await getResponse(handlers, request);
        const body = (await original!.json()) as {
          observed_evidence: Array<Record<string, unknown>>;
          validation_evidence: Array<Record<string, unknown>>;
        };
        body.observed_evidence = body.observed_evidence.map((item) => ({ ...item, source_url: 'javascript:alert(1)' }));
        body.validation_evidence = body.validation_evidence.map((item) => ({ ...item, source_url: 'data:text/html,<script>alert(1)</script>' }));
        return HttpResponse.json(body);
      }),
    );
    const screen = renderApp(<App />, { route: '/opportunities/opp-loc-dallas-0' });
    await screen.findByRole('heading', { name: /Dallas customers want faster delivery 1/i });

    // The evidence itself still renders; only the unsafe links are withheld.
    expect(screen.getByText(/reports of rising demand/i)).toBeInTheDocument();
    expect(screen.getByText(/Corroborated by a local news mention/i)).toBeInTheDocument();
    expect(screen.queryByRole('link', { name: /view source|^source$/i })).toBeNull();
    for (const anchor of Array.from(document.querySelectorAll('a[href]'))) {
      expect(anchor.getAttribute('href')).not.toMatch(/^(javascript|data):/i);
    }
  });
});
