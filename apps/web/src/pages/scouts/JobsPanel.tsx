import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { JobListOut } from '@/api/types';
import { inFlightSignature, pollingOptions, type StatusPollSchedule } from '@/lib/status-polling';
import { jobFilters } from './jobFilters';
import { JobStatusBadge } from '@/components/common/badges';
import { Badge } from '@/components/ui/badge';
import { rawRunCodes, runOutcomeIntent, runOutcomeLabels, runOutcomeReasons } from '@/lib/run-outcome';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useToast } from '@/components/ui/toast';
import { canEditWorkspace, useActorRole } from '@/lib/roles';
import { formatRelative } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

// Statuses where the job is still in flight and the view should keep polling.
const ACTIVE = new Set([
  'pending',
  'scheduled',
  'claimed',
  'running',
  'retry_wait',
  'cancel_requested',
]);

// Statuses a customer may cancel (not yet terminal, not already cancelling).
const CANCELLABLE = new Set(['pending', 'scheduled', 'claimed', 'running', 'retry_wait']);

export function JobsPanel({
  workspaceId,
  scoutRequestId,
  schedule,
}: {
  workspaceId: string;
  scoutRequestId: string;
  /** The detail page's status-poll schedule (lib/status-polling.ts), shared with its request. */
  schedule: StatusPollSchedule;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { organizationId } = useWorkspace();
  // Cancelling is an editor action (EDITORS gate); the job list stays readable.
  const canEdit = canEditWorkspace(useActorRole(organizationId).role);
  const filters = jobFilters(scoutRequestId);

  const query = useQuery({
    queryKey: queryKeys.jobs(workspaceId, filters),
    queryFn: ({ signal }) => api.listJobs(workspaceId, filters, signal),
    // Poll only while a job is still in flight (the run endpoint is async), on the page's
    // bounded, backed-off schedule (lib/status-polling.ts, review F-02); it was a fixed 2 s.
    ...pollingOptions<JobListOut>(schedule, 'jobs', (page) =>
      inFlightSignature(page?.items, (status) => ACTIVE.has(status)),
    ),
  });

  const cancel = useMutation({
    mutationFn: (jobId: string) => api.cancelJob(workspaceId, jobId),
    onSuccess: async () => {
      // D-25 recovery (PROPOSED): a Cancel returns the Scout to a usable idle state, so the
      // Scout itself is re-read too.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: ['workspaces', workspaceId, 'jobs'] }),
        queryClient.invalidateQueries({ queryKey: queryKeys.scoutRequest(workspaceId, scoutRequestId) }),
      ]);
      toast({ title: 'Cancellation requested', intent: 'success' });
    },
    onError: (err) =>
      toast({
        title: 'Could not cancel job',
        description: err instanceof Error ? err.message : undefined,
        intent: 'error',
      }),
  });

  const jobs = query.data?.items ?? [];

  return (
    <Card>
      <CardHeader>
        <CardTitle>Background jobs</CardTitle>
      </CardHeader>
      <CardContent>
        {query.isLoading ? (
          <LoadingRows rows={2} />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => query.refetch()} />
        ) : jobs.length === 0 ? (
          <EmptyState
            title="No jobs yet"
            description="Running this scout enqueues a durable background job. Its progress shows here."
          />
        ) : (
          <ul className="space-y-2 text-sm">
            {jobs.map((job) => (
              <li
                key={job.id}
                className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2"
              >
                <div className="min-w-0" data-job-outcome={job.outcome ?? job.status}>
                  <div className="flex items-center gap-2">
                    {/* W16 §D (PROPOSED, a proposed Q1 presentation): a finished run reads its
                        outcome, never "Succeeded" for a stop or a skip; in flight, the job status. */}
                    {job.outcome ? (
                      <Badge intent={runOutcomeIntent[job.outcome]}>{runOutcomeLabels[job.outcome]}</Badge>
                    ) : (
                      <JobStatusBadge value={job.status} />
                    )}
                    {job.attempt_count > 1 ? (
                      <span className="text-xs text-muted-foreground">
                        attempt {job.attempt_count}/{job.max_attempts}
                      </span>
                    ) : null}
                  </div>
                  {job.outcome_reason ? (
                    <p className="mt-1 text-xs">{runOutcomeReasons[job.outcome_reason]}</p>
                  ) : null}
                  <p className="mt-1 truncate text-xs text-muted-foreground">
                    Updated {formatRelative(job.updated_at)}
                    {job.outcome ? ` · ${rawRunCodes(job)}` : job.last_error_code ? ` · ${job.last_error_code}` : ''}
                  </p>
                </div>
                {canEdit && CANCELLABLE.has(job.status) ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => cancel.mutate(job.id)}
                    disabled={cancel.isPending && cancel.variables === job.id}
                  >
                    <X className="size-4" /> Cancel
                  </Button>
                ) : null}
              </li>
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}
