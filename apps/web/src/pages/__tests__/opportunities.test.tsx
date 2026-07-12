import { waitFor, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { renderApp } from '@/test/utils';

describe('opportunity feed', () => {
  it('renders scored opportunity cards for the workspace', async () => {
    const screen = renderApp(<App />, { route: '/opportunities' });
    expect(await screen.findByText(/Dallas customers want faster delivery 1/i)).toBeInTheDocument();
    // Human-readable score labels appear alongside the raw numbers.
    expect(screen.getAllByText(/Strong|Moderate|Fair|Low/).length).toBeGreaterThan(0);
    // Simulated data must be clearly labelled.
    expect(screen.getAllByText(/Simulated/i).length).toBeGreaterThan(0);
  });

  it('keeps each location strictly isolated — Dallas shows only Dallas', async () => {
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-dallas' });
    await screen.findByText(/Dallas customers want faster delivery 1/i);
    expect(screen.queryByText(/London customers/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Lagos customers/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Nairobi customers/i)).not.toBeInTheDocument();
  });

  it('keeps each location strictly isolated — London shows only London', async () => {
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-london' });
    await screen.findByText(/London customers want faster delivery 1/i);
    expect(screen.queryByText(/Dallas customers/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Lagos customers/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/Nairobi customers/i)).not.toBeInTheDocument();
  });

  it('filters the feed by classification', async () => {
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-dallas' });
    await screen.findByText(/Dallas customers want faster delivery 1/i);
    // Both a validated (1) and an early (2) card are present initially.
    expect(screen.getByText(/Dallas customers want faster delivery 2/i)).toBeInTheDocument();

    const filter = screen.getByRole('combobox', { name: /filter by classification/i });
    await screen.user.click(filter);
    const listbox = await screen.findByRole('listbox');
    await screen.user.click(within(listbox).getByRole('option', { name: /^Validated$/i }));

    await waitFor(() =>
      expect(screen.queryByText(/Dallas customers want faster delivery 2/i)).not.toBeInTheDocument(),
    );
    expect(screen.getByText(/Dallas customers want faster delivery 1/i)).toBeInTheDocument();
  });
});
