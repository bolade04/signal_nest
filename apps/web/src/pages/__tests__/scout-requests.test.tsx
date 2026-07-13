import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { renderApp } from '@/test/utils';

describe('scout requests', () => {
  it('lists every scout request with its status', async () => {
    const screen = renderApp(<App />, { route: '/scout-requests' });
    expect(await screen.findByText(/Dallas demand scan/i)).toBeInTheDocument();
    expect(screen.getByText(/London demand scan/i)).toBeInTheDocument();
    expect(screen.getByText(/Lagos demand scan/i)).toBeInTheDocument();
    expect(screen.getByText(/Nairobi demand scan/i)).toBeInTheDocument();
  });

  it('runs a scout and reports that it was queued', async () => {
    const screen = renderApp(<App />, { route: '/scout-requests', activeLocation: 'loc-dallas' });
    await screen.findByText(/Dallas demand scan/i);

    const [runButton] = screen.getAllByRole('button', { name: /run now/i });
    await screen.user.click(runButton!);
    expect(await screen.findByText(/scout queued/i)).toBeInTheDocument();
  });

  it('opens a scout request detail with its configuration', async () => {
    const screen = renderApp(<App />, { route: '/scout-requests/scout-loc-dallas' });
    expect(await screen.findByRole('heading', { name: /Dallas demand scan/i })).toBeInTheDocument();
    expect(screen.getByText(/Configuration/i)).toBeInTheDocument();
    // Simulated-source disclosure appears on the detail page.
    expect(screen.getByText(/Simulated sources/i)).toBeInTheDocument();
  });
});
