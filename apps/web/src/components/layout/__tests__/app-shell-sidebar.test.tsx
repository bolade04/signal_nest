import { waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { orgModel } from '@/test/handlers';
import { shownAt } from '@/test/responsive';
import { renderProductionApp } from '@/test/utils';

/**
 * UI-AUDIT F5 / P4: on desktop the sidebar scrolled away with long pages (nav top at
 * -682 px after a 700 px scroll) and its column ended at one viewport height, blank below.
 * html, body and #root are height:100%, and the shell carried `h-full`, so the shell — and
 * with it the sidebar column the sticky nav lives in — was fixed at one viewport.
 *
 * jsdom lays nothing out: the rendered proof (nav pinned at 16 px after scrolling, aside
 * as tall as the document, one vertical scroller) is the layout probe in the lane's
 * evidence. These tests pin the shell contract and keep the drawer and skip link working.
 */

beforeEach(() => {
  orgModel.reset();
});

async function openShell() {
  const screen = renderProductionApp(<App />, '/opportunities', { token: 'test-token' });
  await screen.findByRole('banner');
  const navs = await screen.findAllByRole('navigation', { name: 'Primary' });
  return { screen, navs };
}

describe('desktop sidebar (UI-AUDIT P4)', () => {
  it('lets the shell grow with the page, so the sidebar column spans it and the sticky nav can stay in view', async () => {
    await openShell();
    const aside = document.querySelector('aside')!;
    const shell = aside.parentElement!;
    // At least one viewport tall, never fixed at one: `h-full` resolved to the viewport here.
    expect(shell).toHaveClass('flex', 'min-h-screen');
    expect(shell).not.toHaveClass('h-full');
    expect(Array.from(shell.classList).filter((c) => /^(h|max-h)-/.test(c))).toEqual([]);
    // The column itself is not given a height (it stretches with the shell) and is not a
    // scroller of its own: no second scrollbar.
    expect(Array.from(aside.classList).filter((c) => /^(h|max-h|overflow)(-|$)/.test(c))).toEqual([]);
    // The nav keeps its sticky, viewport-tall wrapper inside the column.
    const nav = within(aside).getByRole('navigation', { name: 'Primary' });
    expect(nav.parentElement).toHaveClass('sticky', 'top-4', 'h-[calc(100vh-2rem)]');
    // Shown from 1024 px only, as before.
    expect(shownAt([aside], 1023)).toHaveLength(0);
    expect(shownAt([aside], 1024)).toHaveLength(1);
  });

  it('keeps the drawer below 1024 px: it opens from the header with the same navigation and closes on Escape', async () => {
    const { screen } = await openShell();
    const open = screen.getByRole('button', { name: 'Open navigation' });
    expect(shownAt([open], 1023)).toHaveLength(1);
    expect(shownAt([open], 1024)).toHaveLength(0);
    await screen.user.click(open);
    const drawer = await screen.findByRole('dialog', { name: 'Navigation' });
    expect(within(drawer).getByRole('navigation', { name: 'Primary' })).toBeInTheDocument();
    await screen.user.keyboard('{Escape}');
    await waitFor(() => expect(screen.queryByRole('dialog', { name: 'Navigation' })).not.toBeInTheDocument());
  });

  it('keeps the skip link first in the tab order, pointing at the main landmark', async () => {
    const { screen } = await openShell();
    await screen.user.tab();
    const skip = screen.getByRole('link', { name: 'Skip to content' });
    expect(skip).toHaveFocus();
    expect(skip).toHaveAttribute('href', '#main-content');
    expect(screen.getByRole('main')).toHaveAttribute('id', 'main-content');
  });
});
