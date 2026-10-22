import { waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { orgModel } from '@/test/handlers';
import { displayedAt, shownAt } from '@/test/responsive';
import { renderProductionApp } from '@/test/utils';

/**
 * The app-shell header across viewport widths (P6-UI-011, P6-UI-015).
 *
 * jsdom lays nothing out, so these tests read which controls Tailwind's display
 * classes show at each width (`@/test/responsive`) and the flex contract that lets the
 * first row give way. Whether the row actually fits is a rendered fact: it was measured
 * in a real browser for this change (the UI lane's evidence set), not here.
 *
 * Width bands: below 768 (phone), 768–1023 (portrait tablet; no sidebar), 1024–1279
 * (sidebar shown, so the header gets 256px less), 1280–1535, 1536 and up.
 */

const WIDTHS = [360, 390, 640, 767, 768, 900, 1023, 1024, 1180, 1280, 1440, 1535, 1536, 1920] as const;

beforeEach(() => {
  orgModel.reset();
});

async function openShell(path = '/scout-requests/scout-loc-dallas') {
  const screen = renderProductionApp(<App />, path, { token: 'test-token' });
  const header = await screen.findByRole('banner');
  await within(header).findAllByRole('combobox', { name: 'Location filter' });
  const rows = Array.from(header.children);
  const rowOf = (el: Element) => rows.findIndex((row) => row.contains(el)) + 1;
  return { screen, header, rows, rowOf };
}

describe('the header context switchers (P6-UI-011)', () => {
  it.each(['Workspace', 'Location filter'])(
    'shows exactly one %s switcher at every width: in the compact strip below 1536px, inline from 1536px',
    async (name) => {
      const { header, rowOf } = await openShell();
      const switchers = within(header).getAllByRole('combobox', { name });
      for (const width of WIDTHS) {
        const shown = shownAt(switchers, width);
        expect(shown, `${name} at ${width}px`).toHaveLength(1);
        expect(rowOf(shown[0]!), `${name} row at ${width}px`).toBe(width >= 1536 ? 1 : 2);
      }
    },
  );

  it('places a member of two organizations\' Organization switcher with the other two', async () => {
    orgModel.addOrganization({ id: 'org-2', name: 'Second Org', slug: 'second-org' }, [
      { id: 'ws-2', name: 'Second Workspace', slug: 'second-ws', onboarding_completed: true, created_at: '2026-01-01T00:00:00Z' },
    ]);
    orgModel.setMembership('org-2', 'user-1', 'owner');
    const { header, rowOf } = await openShell();
    const organizations = await within(header).findAllByRole('combobox', { name: 'Organization' });
    for (const width of WIDTHS) {
      const shown = shownAt(organizations, width);
      expect(shown, `Organization at ${width}px`).toHaveLength(1);
      expect(rowOf(shown[0]!), `Organization row at ${width}px`).toBe(width >= 1536 ? 1 : 2);
    }
  });

  it('lets the breadcrumb trail give way in the first row instead of pushing controls off-screen', async () => {
    const { header, rows } = await openShell();
    const row = rows[0]!;
    const trail = within(header).getByRole('navigation', { name: 'Breadcrumb' });
    expect(row).toContainElement(trail);

    // Every other item of the first row keeps its size; the trail alone may shrink, to nothing.
    for (const item of Array.from(row.children)) {
      if (item === trail) continue;
      expect(item, item.outerHTML.slice(0, 120)).toHaveClass('shrink-0');
    }
    expect(trail).toHaveClass('min-w-0');
    const list = within(trail).getByRole('list');
    expect(list).toHaveClass('min-w-0');
    const crumbs = within(list).getAllByRole('listitem');
    expect(crumbs.map((c) => c.textContent).slice(0, 2)).toEqual(['Overview', 'Scout Requests']);
    expect(crumbs).toHaveLength(3);
    for (const crumb of crumbs) {
      expect(crumb).toHaveClass('min-w-0');
      // The label truncates on one line rather than wrapping into a taller row.
      const label = crumb.querySelector('a, [aria-current="page"]')!;
      expect(label).toHaveClass('truncate');
      const chevron = crumb.querySelector('svg');
      if (chevron) expect(chevron).toHaveClass('shrink-0');
    }
  });

  it('bounds the signed-in name beside the avatar, so a long name truncates', async () => {
    const { header } = await openShell();
    const [account] = within(header).getAllByRole('button', { name: /account menu/i });
    const name = within(account!).getByText('Demo Marketer');
    expect(name).toHaveClass('truncate');
    expect(Array.from(name.classList).some((c) => /^max-w-/.test(c))).toBe(true);
  });
});

describe('header search below 1024px (P6-UI-015)', () => {
  it('offers exactly one search control at every width: a button below 1024px, the field from 1024px', async () => {
    const { header } = await openShell('/');
    const field = within(header).getByRole('searchbox', { name: 'Search opportunities' });
    const button = within(header).getByRole('button', { name: 'Search opportunities' });
    for (const width of WIDTHS) {
      expect(shownAt([field, button], width), `at ${width}px`).toEqual([width >= 1024 ? field : button]);
    }
  });

  it('opens a search dialog from the button that takes the term to the opportunity feed', async () => {
    const { screen, header } = await openShell('/');
    const button = within(header).getByRole('button', { name: 'Search opportunities' });
    expect(displayedAt(button, 390)).toBe(true);

    await screen.user.click(button);
    const dialog = await screen.findByRole('dialog', { name: 'Search opportunities' });
    const box = within(dialog).getByRole('searchbox', { name: 'Search opportunities' });
    expect(box).toHaveFocus();
    await screen.user.type(box, '  Dallas  {Enter}');

    await waitFor(() => expect(window.location.pathname).toBe('/opportunities'));
    expect(window.location.search).toBe('?search=Dallas');
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    expect(await screen.findByRole('heading', { name: /^opportunities$/i })).toBeInTheDocument();
  });

  it('goes to the unfiltered feed when submitted empty, as the field does', async () => {
    const { screen, header } = await openShell('/');
    await screen.user.click(within(header).getByRole('button', { name: 'Search opportunities' }));
    const dialog = await screen.findByRole('dialog', { name: 'Search opportunities' });
    await screen.user.type(within(dialog).getByRole('searchbox', { name: 'Search opportunities' }), '{Enter}');
    await waitFor(() => expect(window.location.pathname).toBe('/opportunities'));
    expect(window.location.search).toBe('');
  });
});
