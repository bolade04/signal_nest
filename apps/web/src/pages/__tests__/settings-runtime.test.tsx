import { describe, expect, it } from 'vitest';
import { App } from '@/App';
import { renderApp } from '@/test/utils';

describe('settings runtime status', () => {
  it('shows the zero-dependency local runtime and per-capability readiness', async () => {
    const screen = renderApp(<App />, { route: '/settings' });

    // The Runtime card renders once the capabilities query resolves.
    expect(await screen.findByText('Runtime')).toBeInTheDocument();

    // Local mode is surfaced explicitly (not blended with production).
    expect(await screen.findByText(/local \(zero-dependency\)/i)).toBeInTheDocument();

    // Every backend capability is listed and reported ready.
    for (const name of ['database', 'queue', 'cache', 'vector', 'storage', 'llm']) {
      expect(screen.getByText(name)).toBeInTheDocument();
    }
    expect(screen.getAllByText(/^ready$/i).length).toBeGreaterThanOrEqual(6);
  });
});
