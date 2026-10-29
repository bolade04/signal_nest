import { RefreshCw } from 'lucide-react';
import { useLayoutEffect, useRef } from 'react';
import { Button } from '@/components/ui/button';
import { cn } from '@/lib/utils';

/**
 * "Refresh status" for a page whose scout status is being polled (lib/status-polling.ts,
 * review F-02 and r2 R2-01/R2-06). Rendered while anything shown is queued or running:
 * - while reads are automatic, just the button;
 * - once they have stopped, the role="status" note "Automatic status updates stopped.
 *   Refresh status to check again." beside it. The note says only that; it does not
 *   interpret the request's state or suggest pause, resume or cancel.
 * The button stays the same element in both states, so activating it while something stays
 * in flight keeps it mounted and focused. When nothing is in flight any more the page
 * removes this component; if the button had focus at that moment, `onRemovedWhileFocused`
 * tells the page, which then moves focus to a remaining control. Focus is never moved here.
 */
export function StatusRefresh({
  stopped,
  onRefresh,
  onRemovedWhileFocused,
  className,
}: {
  stopped: boolean;
  onRefresh: () => void;
  onRemovedWhileFocused?: () => void;
  className?: string;
}) {
  const buttonRef = useRef<HTMLButtonElement>(null);
  const removedWhileFocused = useRef(onRemovedWhileFocused);
  useLayoutEffect(() => {
    removedWhileFocused.current = onRemovedWhileFocused;
  });
  // Runs on removal before the button leaves the DOM, so the focus check is still true.
  useLayoutEffect(() => {
    const button = buttonRef.current;
    return () => {
      if (button && document.activeElement === button) removedWhileFocused.current?.();
    };
  }, []);
  return (
    <div
      className={cn(
        stopped
          ? 'flex flex-col gap-2 rounded-md border border-border bg-secondary/50 px-4 py-2.5 text-sm sm:flex-row sm:items-center sm:justify-between'
          : 'flex justify-end',
        className,
      )}
    >
      {/* A live region that exists before the note is written into it, so it is announced. */}
      <p role="status" className={stopped ? undefined : 'sr-only'}>
        {stopped ? 'Automatic status updates stopped. Refresh status to check again.' : ''}
      </p>
      <Button ref={buttonRef} variant="outline" size="sm" className="shrink-0" onClick={onRefresh}>
        <RefreshCw className="size-4" /> Refresh status
      </Button>
    </div>
  );
}
