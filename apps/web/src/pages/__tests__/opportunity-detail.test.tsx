import { describe, expect, it } from 'vitest';
import { App } from '@/App';
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
});
