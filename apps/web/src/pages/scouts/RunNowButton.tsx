import { Radar } from 'lucide-react';
import type { ScoutRequestOut } from '@/api/types';
import { Button, type ButtonProps } from '@/components/ui/button';
import { isScoutInFlight } from '@/lib/scout-status';
import type { useScoutActions } from './useScoutActions';

/**
 * "Run now" for one scout request (UI-AUDIT F8 / P8). Disabled, and labelled with what is
 * happening, while this tab's POST is in flight ("Starting…") and while the request is
 * `queued` ("Queued…") or `running` ("Running…") — the states in which the API refuses a
 * run anyway. It is enabled again once the request settles (completed, failed, draft,
 * paused), so a failed POST or a failed run leaves it usable. What Run does on a paused or
 * draft request is unchanged (the API decides; see D-25-a/b).
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
      onClick={() => actions.startRun(request.id)}
      disabled={busy !== null}
      aria-busy={busy !== null ? true : undefined}
    >
      <Radar className="size-4" /> {busy ?? 'Run now'}
    </Button>
  );
}
