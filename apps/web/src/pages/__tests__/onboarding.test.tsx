import { waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { renderApp } from '@/test/utils';

describe('onboarding wizard', () => {
  it('walks through steps and requires a business name before continuing', async () => {
    const screen = renderApp(<App />, { route: '/onboarding' });

    // Step 1: presence path — every path, including "brand new", is offered.
    expect(await screen.findByText(/how do customers find you today/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /brand new/i })).toBeInTheDocument();

    await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));

    // Step 2: business basics — continue is gated until a name is entered.
    const nameInput = await screen.findByLabelText(/business \/ brand name/i);
    const continueBtn = screen.getByRole('button', { name: /save & continue/i });
    expect(continueBtn).toBeDisabled();

    await screen.user.type(nameInput, 'Acme Coffee');
    expect(continueBtn).toBeEnabled();
  });

  it('autosaves the draft to localStorage so progress is not lost', async () => {
    const screen = renderApp(<App />, { route: '/onboarding' });
    await screen.findByText(/how do customers find you today/i);
    await screen.user.click(screen.getByRole('button', { name: /save & continue/i }));
    const nameInput = await screen.findByLabelText(/business \/ brand name/i);
    await screen.user.type(nameInput, 'Persisted Brand');

    await waitFor(() => {
      const draft = localStorage.getItem('signalnest-onboarding-ws-1');
      expect(draft).toBeTruthy();
      expect(draft).toContain('Persisted Brand');
    });
  });
});
