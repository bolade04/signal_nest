import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';

/**
 * Dialogs on a phone (P6-UI-012). jsdom lays nothing out, so this pins the classes that
 * leave a gutter; the rendered result at 360/390/768px was measured in a real browser for
 * this change (the UI lane's evidence set).
 */

function openDialog(className?: string) {
  render(
    <Dialog open>
      <DialogContent className={className}>
        <DialogTitle>Title</DialogTitle>
        <DialogDescription>Description</DialogDescription>
      </DialogContent>
    </Dialog>,
  );
  return screen.getByRole('dialog', { name: 'Title' });
}

describe('DialogContent on a narrow viewport (P6-UI-012)', () => {
  it('leaves a 1rem gutter on each side instead of spanning the full width', () => {
    const dialog = openDialog();
    expect(dialog).not.toHaveClass('w-full');
    expect(dialog).toHaveClass('w-[calc(100%-2rem)]');
    // Still capped on wider screens, and still scrollable when tall.
    expect(dialog).toHaveClass('max-w-lg', 'max-h-[90vh]', 'overflow-y-auto');
    // The close control sits inside the dialog, so the gutter keeps it off the screen edge.
    expect(within(dialog).getByRole('button', { name: 'Close' })).toHaveClass('absolute', 'right-4', 'top-4');
  });

  it('keeps the gutter when a caller widens the dialog', () => {
    const dialog = openDialog('max-w-2xl');
    expect(dialog).toHaveClass('w-[calc(100%-2rem)]', 'max-w-2xl');
    expect(dialog).not.toHaveClass('max-w-lg');
  });
});
