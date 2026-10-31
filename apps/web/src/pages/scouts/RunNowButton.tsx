import { Radar } from 'lucide-react';
import { useId } from 'react';
import type { ScoutRequestOut } from '@/api/types';
import { Button, type ButtonProps } from '@/components/ui/button';
import { runNowAvailability } from '@/lib/scout-actions';
import type { useScoutActions } from './useScoutActions';

/**
 * "Run now" for one scout request (UI-AUDIT F8 / P8; W16 customer repairs §C, PROPOSED).
 *
 * Unavailable exactly where the API refuses a run: while this tab's POST is in flight
 * ("Starting…"), while the request is `queued` ("Queued…") or `running` ("Running…"), while it is
 * paused, and while it needs attention. The last two show a visible reason. Where the API
 * accepts a run that will wait (an earlier operation still finishing) it stays available and
 * says so. The API remains the authority: a refusal after a race is shown as a toast.
 *
 * Unavailable is `aria-disabled`, not native `disabled` (review F-01): a disabled button drops
 * keyboard focus to <body>, so a keyboard user would lose their place. The button stays
 * focusable, every activation while unavailable returns before submitting, and the reason is
 * tied to it with `aria-describedby` and announced politely when it appears. Focus is never
 * moved programmatically.
 */
export function RunNowButton({
  request,
  actions,
  size,
  announce = true,
}: {
  request: Pick<ScoutRequestOut, 'id' | 'status' | 'execution'>;
  actions: ReturnType<typeof useScoutActions>;
  size?: ButtonProps['size'];
  /**
   * Whether this button's reason is a polite live region. A page that shows a second Run now for
   * the same Scout (the empty state's) passes `false` for it, so the reason is announced once; the
   * button still carries it as its accessible description, and it is not shown twice (review r1 F9).
   */
  announce?: boolean;
}) {
  const reasonId = useId();
  const state = runNowAvailability(request, actions.isRunPending(request.id));
  const unavailable = !state.available;
  const busy = state.label !== null;
  return (
    <span className="inline-flex max-w-xs flex-col items-start gap-1">
      <Button
        size={size}
        onClick={() => {
          if (unavailable) return;
          actions.startRun(request.id);
        }}
        aria-disabled={unavailable ? true : undefined}
        aria-busy={busy ? true : undefined}
        aria-describedby={state.reason ? reasonId : undefined}
        // Lets a page find this request's Run now to return focus to (see StatusRefresh).
        data-run-now=""
        // The look of the native disabled state, without its focus loss.
        className="aria-disabled:cursor-not-allowed aria-disabled:opacity-50 aria-disabled:hover:bg-primary"
      >
        <Radar className="size-4" /> {state.label ?? 'Run now'}
      </Button>
      {announce ? (
        <span id={reasonId} aria-live="polite" className="text-xs text-muted-foreground" data-run-reason="">
          {state.reason}
        </span>
      ) : (
        <span id={reasonId} className="sr-only">
          {state.reason}
        </span>
      )}
    </span>
  );
}
