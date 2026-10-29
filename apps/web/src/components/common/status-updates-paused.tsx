import { RefreshCw } from 'lucide-react';
import { Button } from '@/components/ui/button';

/**
 * Shown once automatic status reads have stopped (lib/status-polling.ts, review F-02). It
 * says only that the automatic updates are paused; it deliberately does not interpret the
 * request's state or suggest pause, resume or cancel. "Refresh status" reads at once and
 * restarts the schedule.
 */
export function StatusUpdatesPaused({ onRefresh, className }: { onRefresh: () => void; className?: string }) {
  return (
    <div
      className={
        'flex flex-col gap-2 rounded-md border border-border bg-secondary/50 px-4 py-2.5 text-sm sm:flex-row sm:items-center sm:justify-between ' +
        (className ?? '')
      }
    >
      <p role="status">Automatic status updates paused. Refresh status to check again.</p>
      <Button variant="outline" size="sm" className="shrink-0" onClick={onRefresh}>
        <RefreshCw className="size-4" /> Refresh status
      </Button>
    </div>
  );
}
