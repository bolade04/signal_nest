import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { X } from 'lucide-react';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { JobOut } from '@/api/types';
import { JobStatusBadge } from '@/components/common/badges';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { useToast } from '@/components/ui/toast';
import { formatRelative } from '@/lib/utils';

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
}: {
  workspaceId: string;
  scoutRequestId: string;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const filters = { scout_request_id: scoutRequestId, limit: 10 };

  const query = useQuery({
    queryKey: queryKeys.jobs(workspaceId, filters),
    queryFn: ({ signal }) => api.listJobs(workspaceId, filters, signal),
    // Poll only while a job is still in flight; the run endpoint is async.
    refetchInterval: (q) =>
      (q.state.data?.items ?? []).some((j: JobOut) => ACTIVE.has(j.status)) ? 2000 : false,
  });

  const cancel = useMutation({
    mutationFn: (jobId: string) => api.cancelJob(workspaceId, jobId),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: ['workspaces', workspaceId, 'jobs'] });
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
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <JobStatusBadge value={job.status} />
                    {job.attempt_count > 1 ? (
                      <span className="text-xs text-muted-foreground">
                        attempt {job.attempt_count}/{job.max_attempts}
                      </span>
                    ) : null}
                  </div>
                  <p className="mt-1 truncate text-xs text-muted-foreground">
                    Updated {formatRelative(job.updated_at)}
                    {job.last_error_code ? ` · ${job.last_error_code}` : ''}
                  </p>
                </div>
                {CANCELLABLE.has(job.status) ? (
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
