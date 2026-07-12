import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { renderApp } from '@/test/utils';

describe('campaign context center', () => {
  it('shows the context tabs and an empty state for products', async () => {
    const screen = renderApp(<App />, { route: '/context' });
    expect(await screen.findByRole('heading', { name: /campaign context/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /products & services/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /claims library/i })).toBeInTheDocument();
    // Default (empty) product list renders an empty-state prompt.
    expect(await screen.findByText(/no products yet/i)).toBeInTheDocument();
  });

  it('opens the add-product dialog', async () => {
    const screen = renderApp(<App />, { route: '/context' });
    await screen.findByRole('heading', { name: /campaign context/i });
    const [addButton] = screen.getAllByRole('button', { name: /add product/i });
    await screen.user.click(addButton!);
    expect(await screen.findByRole('dialog')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: /add product/i })).toBeInTheDocument();
  });
});
