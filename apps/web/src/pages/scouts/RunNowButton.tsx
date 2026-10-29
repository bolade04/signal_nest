import { Radar } from 'lucide-react';
import type { ScoutRequestOut } from '@/api/types';
import { Button, type ButtonProps } from '@/components/ui/button';
import { isScoutInFlight } from '@/lib/scout-status';
import type { useScoutActions } from './useScoutActions';

/**
 * "Run now" for one scout request (UI-AUDIT F8 / P8). Unavailable, and labelled with what is
 * happening, while this tab's POST is in flight ("Starting…") and while the request is
 * `queued` ("Queued…") or `running` ("Running…") — the states in which the API refuses a
 * run anyway. It is available again once the request settles (completed, failed, draft,
 * paused), so a failed POST or a failed run leaves it usable. What Run does on a paused or
 * draft request is unchanged (the API decides; see D-25-a/b).
 *
 * Busy is `aria-disabled`, not native `disabled` (review F-01): a disabled button drops
 * keyboard focus to <body>, so a keyboard user lost their place for the whole run. The
 * button stays focusable and in the tab order, its accessible name follows the label, and
 * every activation while busy (click, Enter, Space, repeats) returns before submitting;
 * `startRun` keeps its own synchronous in-flight guard as well. Focus is never moved
 * programmatically: it stays wherever the user put it.
 */
export function RunNowButton({
  request,
  actions,
  size,
}: {
  request: Pick<ScoutRequestOut, 'id' | 'status'>;
  actions: ReturnType<typeof useScoutActions>;
  size?: ButtonProps['size'];
}) {
  const busy = actions.isRunPending(request.id)
    ? 'Starting…'
    : request.status === 'running'
      ? 'Running…'
      : isScoutInFlight(request.status)
        ? 'Queued…'
        : null;
  return (
    <Button
      size={size}
      onClick={() => {
        if (busy !== null) return;
        actions.startRun(request.id);
      }}
      aria-disabled={busy !== null ? true : undefined}
      aria-busy={busy !== null ? true : undefined}
      // Lets a page find this request's Run now to return focus to (see StatusRefresh).
      data-run-now=""
      // The look of the native disabled state, without its focus loss.
      className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50 aria-disabled:hover:bg-primary"
    >
      <Radar className="size-4" /> {busy ?? 'Run now'}
    </Button>
  );
}
