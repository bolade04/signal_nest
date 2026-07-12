import { within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { renderApp } from '@/test/utils';

describe('locations', () => {
  it('populates the edit dialog from the selected location', async () => {
    // Regression guard for the effect→render-phase migration of the form-reset
    // logic in LocationDialog. Opening the dialog for a specific location must
    // seed the form with that location's data via the guarded render-phase reset
    // (now keyed on the location id rather than the object reference).
    const screen = renderApp(<App />, { route: '/locations', activeLocation: 'loc-dallas' });

    const editButtons = await screen.findAllByRole('button', { name: /edit & service area/i });
    // Fixtures list Dallas first (see test/handlers CITIES order).
    await screen.user.click(editButtons[0]!);

    const dialog = await screen.findByRole('dialog', { name: /edit location/i });
    expect(within(dialog).getByLabelText(/location name/i)).toHaveValue('Dallas');
  });
});
