import { useQuery, useQueryClient } from '@tanstack/react-query';
import { ArrowLeft, Pause, Play, Radar, Sparkles } from 'lucide-react';
import { useEffect, useMemo, useRef } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { OpportunityFilters } from '@/api/types';
import { ScoutStatusBadge } from '@/components/common/badges';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { sourceTypeLabels } from '@/lib/labels';
import { canEditWorkspace, useActorRole } from '@/lib/roles';
import { formatStat, statValue } from '@/lib/scout-stats';
import { formatDateTime, formatRelative, titleCase } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { OpportunityCardView } from './opportunities/OpportunityCardView';
import { JobsPanel } from './scouts/JobsPanel';
import { SchedulePanel } from './scouts/SchedulePanel';
import { useScoutActions } from './scouts/useScoutActions';

// Scout statuses where the backend is still working, so this view has to keep
// re-reading: the run endpoint returns `queued` immediately and a durable worker
// settles it later, with nothing pushing that change to the client. Mirrors
// `_IN_FLIGHT_SCOUT_STATUSES` in apps/api/app/jobs/handlers.py. Deliberately an
// explicit allowlist rather than "anything that is not completed": the status
// column is an unconstrained string, so an unrecognised value would otherwise
// poll forever. `draft` and `paused` are settled states, not in-flight ones.
const IN_FLIGHT = new Set(['queued', 'running']);

// The outcomes a run settles into. Arriving at one of these FROM an in-flight
// status is the moment this scout's opportunities have actually changed.
const TERMINAL = new Set(['completed', 'failed']);

function DetailInner({ workspaceId, requestId }: { workspaceId: string; requestId: string }) {
  const navigate = useNavigate();
  const { locations, organizationId } = useWorkspace();
  // Run / pause / resume are editor actions (EDITORS gate); the configuration,
  // schedule, jobs and opportunities below stay readable for every member.
  const canEdit = canEditWorkspace(useActorRole(organizationId).role);
  const actions = useScoutActions(workspaceId);
  const queryClient = useQueryClient();

  const query = useQuery({
    queryKey: queryKeys.scoutRequest(workspaceId, requestId),
    queryFn: ({ signal }) => api.getScoutRequest(workspaceId, requestId, signal),
    // Poll only while the run is in flight, matching the JobsPanel cadence.
    refetchInterval: (q) => (IN_FLIGHT.has(q.state.data?.status ?? '') ? 2000 : false),
  });

  const oppFilters: OpportunityFilters = useMemo(
    () => ({ scout_request_id: requestId, limit: 100 }),
    [requestId],
  );
  const oppQuery = useQuery({
    queryKey: queryKeys.opportunities(workspaceId, oppFilters),
    queryFn: ({ signal }) => api.listOpportunities(workspaceId, oppFilters, signal),
  });

  // The opportunity grid has to be refreshed on the in-flight → terminal
  // TRANSITION, not merely because the status is terminal: a request that is
  // already completed when the page opens would otherwise invalidate on every
  // render. The run mutation invalidates opportunities at QUEUE time, when the
  // worker has produced nothing yet, so without this the header would read
  // "Completed" above the pre-run grid.
  const status = query.data?.status ?? null;
  const previousStatus = useRef<string | null>(null);
  useEffect(() => {
    const previous = previousStatus.current;
    previousStatus.current = status;
    if (previous === null || status === null) return;
    if (!IN_FLIGHT.has(previous) || !TERMINAL.has(status)) return;
    // Minimum scope: this workspace and this scout request only.
    void queryClient.invalidateQueries({
      queryKey: queryKeys.opportunities(workspaceId, oppFilters),
      exact: true,
    });
  }, [status, queryClient, workspaceId, oppFilters]);

  if (query.isLoading) return <LoadingRows rows={5} />;
  if (query.isError) return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  if (!query.data) return null;

  const r = query.data;
  const locName = locations.find((l) => l.id === r.location_id)?.name;
  const canPause = ['running', 'queued', 'draft', 'completed'].includes(r.status);
  const isPaused = r.status === 'paused';
  const opportunities = oppQuery.data ?? [];

  return (
    <div className="space-y-6">
      <div>
        <Button variant="ghost" size="sm" onClick={() => navigate(-1)} className="mb-3 -ml-2">
          <ArrowLeft className="size-4" /> Back
        </Button>
        <div className="flex flex-col gap-4 lg:flex-row lg:items-start lg:justify-between">
          <div className="min-w-0">
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <ScoutStatusBadge value={r.status} />
              {r.resolved_market ? <Badge intent="outline">{r.resolved_market}</Badge> : null}
              {locName ? <Badge intent="neutral">{locName}</Badge> : null}
            </div>
            <h1 className="text-2xl font-semibold tracking-tight">{r.name}</h1>
            <p className="mt-1 text-sm text-muted-foreground">
              {formatStat(statValue(r.stats, 'opportunities'))} opportunities ·{' '}
              {formatStat(statValue(r.stats, 'signals_analyzed'))} signals ·{' '}
              {r.last_run_at ? `last run ${formatRelative(r.last_run_at)}` : 'never run'}
            </p>
          </div>
          {canEdit ? (
            <div className="flex flex-wrap gap-2">
              <Button
                onClick={() => actions.run.mutate(r.id)}
                disabled={actions.run.isPending && actions.run.variables === r.id}
              >
                <Radar className="size-4" /> Run now
              </Button>
              {isPaused ? (
                <Button variant="outline" onClick={() => actions.resume.mutate(r.id)}>
                  <Play className="size-4" /> Resume
                </Button>
              ) : (
                <Button variant="outline" onClick={() => actions.pause.mutate(r.id)} disabled={!canPause}>
                  <Pause className="size-4" /> Pause
                </Button>
              )}
            </div>
          ) : null}
        </div>
      </div>

      <div className="rounded-md border border-warning/40 bg-warning/10 px-4 py-2.5 text-sm">
        <span className="inline-flex items-center gap-1.5 font-medium text-warning">
          <Sparkles className="size-3.5" /> Simulated sources
        </span>{' '}
        <span className="text-muted-foreground">
          This build uses fixture connectors. Generated signals are clearly labeled and not from live feeds.
        </span>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <div className="space-y-6 lg:col-span-1">
        <Card>
          <CardHeader>
            <CardTitle>Configuration</CardTitle>
          </CardHeader>
          <CardContent className="space-y-4 text-sm">
            <div>
              <p className="mb-1.5 font-medium">Sources</p>
              {r.source_types.length ? (
                <div className="flex flex-wrap gap-1.5">
                  {r.source_types.map((s) => (
                    <Badge key={s} intent="outline">
                      {sourceTypeLabels[s] ?? titleCase(s)}
                    </Badge>
                  ))}
                </div>
              ) : (
                <p className="text-muted-foreground">None selected.</p>
              )}
            </div>
            <Separator />
            <div>
              <p className="mb-1.5 font-medium">Keywords</p>
              {r.keywords.length ? (
                <div className="flex flex-wrap gap-1.5">
                  {r.keywords.map((k) => (
                    <Badge key={k} intent="neutral">
                      {k}
                    </Badge>
                  ))}
                </div>
              ) : (
                <p className="text-muted-foreground">None.</p>
              )}
            </div>
            {r.notes ? (
              <>
                <Separator />
                <div>
                  <p className="mb-1.5 font-medium">Notes</p>
                  <p className="text-muted-foreground">{r.notes}</p>
                </div>
              </>
            ) : null}
            <Separator />
            <div className="space-y-1.5 text-muted-foreground">
              <div className="flex justify-between">
                <span>Created</span>
                <span className="text-foreground">{formatDateTime(r.created_at)}</span>
              </div>
              <div className="flex justify-between">
                <span>Updated</span>
                <span className="text-foreground">{formatDateTime(r.updated_at)}</span>
              </div>
            </div>
          </CardContent>
        </Card>

          <SchedulePanel workspaceId={workspaceId} requestId={requestId} />

          <JobsPanel workspaceId={workspaceId} scoutRequestId={requestId} />
        </div>

        <div className="lg:col-span-2">
          <div className="mb-3 flex items-center justify-between">
            <h2 className="text-lg font-semibold">Opportunities from this scout</h2>
            <Link to="/opportunities" className="text-sm font-medium text-primary hover:underline">
              View all
            </Link>
          </div>
          {oppQuery.isLoading ? (
            <LoadingRows rows={3} />
          ) : oppQuery.isError ? (
            <ErrorState error={oppQuery.error} onRetry={() => oppQuery.refetch()} />
          ) : opportunities.length ? (
            <div className="grid gap-4 sm:grid-cols-2">
              {opportunities.map((o) => (
                <OpportunityCardView key={o.id} opportunity={o} />
              ))}
            </div>
          ) : (
            <EmptyState
              icon={Radar}
              title="No opportunities yet"
              description={
                canEdit
                  ? 'Run this scout to process fixture signals into scored, explainable opportunities.'
                  : 'This scout has not produced any opportunities yet.'
              }
              action={
                canEdit ? (
                  <Button
                    onClick={() => actions.run.mutate(r.id)}
                    disabled={actions.run.isPending && actions.run.variables === r.id}
                  >
                    <Radar className="size-4" /> Run now
                  </Button>
                ) : undefined
              }
            />
          )}
        </div>
      </div>
    </div>
  );
}

export function ScoutRequestDetailPage() {
  const { requestId } = useParams<{ requestId: string }>();
  if (!requestId) return null;
  return (
    <RequireWorkspace>
      {({ workspaceId }) => <DetailInner workspaceId={workspaceId} requestId={requestId} />}
    </RequireWorkspace>
  );
}
