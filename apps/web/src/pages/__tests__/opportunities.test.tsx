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

  it('clears the search box when the active location changes', async () => {
    // Regression guard for the WorkspaceContext/Opportunities state migration:
    // switching markets must reset per-market filter state so one location's
    // search never leaks into another's feed.
    const screen = renderApp(<App />, { route: '/opportunities', activeLocation: 'loc-dallas' });
    await screen.findByText(/Dallas customers want faster delivery 1/i);
    // Scope to the page body: the shell mounts only after the session resolves.
    const main = within(screen.container.querySelector('#main-content') as HTMLElement);

    // The page filter search shares its label with the header global search, so
    // scope to the main region.
    const search = main.getByLabelText(/search opportunities/i);
    await screen.user.type(search, 'faster');
    expect(search).toHaveValue('faster');

    // The switchers are duplicated for desktop + mobile; drive the first one.
    const locationFilter = screen.getAllByRole('combobox', { name: /location filter/i })[0]!;
    await screen.user.click(locationFilter);
    const listbox = await screen.findByRole('listbox');
    await screen.user.click(within(listbox).getByRole('option', { name: /London/i }));

    // The search draft resets on the location switch, and only London shows.
    await waitFor(() => expect(search).toHaveValue(''));
    await main.findByText(/London customers want faster delivery 1/i);
    expect(main.queryByText(/Dallas customers/i)).not.toBeInTheDocument();
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
