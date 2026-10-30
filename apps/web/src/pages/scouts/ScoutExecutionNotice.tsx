import { AlertTriangle, Info } from 'lucide-react';
import { executionNotice, type ScoutExecution } from '@/lib/scout-execution';
import { formatRelative } from '@/lib/utils';

/**
 * The Scout page's execution notice (D-25 blocked-Scout recovery, PROPOSED): what the API's
 * derived `execution` state means for this Scout, in plain words. Read-only: a customer can
 * never clear an unresolved operation from here.
 */
export function ScoutExecutionNotice({
  execution,
  status,
}: {
  execution: ScoutExecution | null | undefined;
  status: string;
}) {
  const notice = executionNotice(execution, status);
  if (!notice) return null;
  const attention = notice.tone === 'attention';
  const Icon = attention ? AlertTriangle : Info;
  const since = execution?.since ? ` (${formatRelative(execution.since)})` : '';
  return (
    <div
      role="status"
      data-execution-state={execution?.state}
      className={
        attention
          ? 'rounded-md border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm'
          : 'rounded-md border border-border bg-muted/40 px-4 py-3 text-sm'
      }
    >
      <p className={`flex items-center gap-1.5 font-medium ${attention ? 'text-destructive' : ''}`}>
        <Icon className="size-4 shrink-0" aria-hidden="true" />
        {notice.title}
        {since}
      </p>
      <p className="mt-1 text-muted-foreground">{notice.body}</p>
    </div>
  );
}
