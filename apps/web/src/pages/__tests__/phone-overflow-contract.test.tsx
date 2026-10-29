import { within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { orgModel } from '@/test/handlers';
import { renderProductionApp } from '@/test/utils';

/**
 * UI-AUDIT F4 / P3: at 390 px the Overview (482 px) and Settings (476–613 px) pages
 * scrolled sideways.
 *
 * jsdom lays nothing out, so the rendered proof (document scrollWidth == innerWidth on
 * every route at 390, 834 and 1440 px) is the layout probe in the lane's evidence set.
 * These tests pin the two layout contracts whose absence caused the overflow, read from
 * the classes as `@/test/responsive` does for display utilities.
 */

const POSITIONED = ['relative', 'absolute', 'fixed', 'sticky'];

/** The element an absolutely positioned descendant is laid out against (by class). */
function containingBlock(element: Element): Element | null {
  for (let node = element.parentElement; node; node = node.parentElement) {
    if (Array.from(node.classList).some((cls) => POSITIONED.includes(cls))) return node;
  }
  return null;
}

beforeEach(() => {
  orgModel.reset();
});

describe('phone width: no sideways page scroll (UI-AUDIT P3)', () => {
  it('Overview: the dashboard grid has an explicit shrinkable track, so the chart card cannot widen the page', async () => {
    const screen = renderProductionApp(<App />, '/', { token: 'test-token' });
    const title = await screen.findByText('Opportunities by classification');
    const grid = title.closest('.grid')!;
    // One minmax(0, 1fr) track below lg; the implicit auto track took the chart's width.
    expect(grid).toHaveClass('grid-cols-1');
    expect(grid).toHaveClass('lg:grid-cols-3');
  });

  it('Settings: each table scrolls inside its own box, and its sr-only "Actions" header stays inside that box', async () => {
    orgModel.invitationCreatedBehindTheUi('org-1', 'pending.person@example.com', 'viewer', 'user-1');
    const screen = renderProductionApp(<App />, '/settings', { token: 'test-token' });
    const members = await screen.findByRole('table', { name: /members of/i });
    const pending = await screen.findByRole('table', { name: /pending invitations/i });

    for (const [table, columns] of [
      [members, ['Name', 'Email', 'Role', 'Joined', 'Actions']],
      [pending, ['Email', 'Role', 'Created', 'Expires', 'Actions']],
    ] as const) {
      const box = table.closest('.overflow-x-auto');
      expect(box, 'horizontal scroll box').not.toBeNull();
      const actions = within(table).getByText('Actions');
      expect(actions).toHaveClass('sr-only');
      // An sr-only header is absolutely positioned. Its containing block must be the scroll
      // box (or inside it); otherwise it is placed against the page and widens it.
      const block = containingBlock(actions);
      expect(block, 'positioned ancestor of the sr-only header').not.toBeNull();
      expect(box!.contains(block!)).toBe(true);
      // Nothing is hidden: every column stays in the table, reachable by scrolling the box.
      expect(within(table).getAllByRole('columnheader').map((th) => th.textContent?.trim())).toEqual(columns);
    }
    // The row actions are still there to reach.
    expect(within(pending).getByRole('button', { name: /revoke/i })).toBeInTheDocument();
  });
});
