import { useQuery } from '@tanstack/react-query';
import { ListChecks, Pause, Play, Plus, Search } from 'lucide-react';
import { useEffect, useMemo, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { ScoutRequestOut } from '@/api/types';
import { ScoutStatusBadge } from '@/components/common/badges';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { StatusRefresh } from '@/components/common/status-refresh';
import { PageHeader } from '@/components/layout/page-header';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { scoutStatusLabels } from '@/lib/labels';
import { canEditWorkspace, useActorRole } from '@/lib/roles';
import { formatCount, statValue } from '@/lib/scout-stats';
import { isScoutInFlight } from '@/lib/scout-status';
import { moveFocusTo } from '@/lib/focus';
import { inFlightSignature, pollingOptions, useStatusPolling } from '@/lib/status-polling';
import { formatRelative } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { RunNowButton } from './scouts/RunNowButton';
import { ScoutRequestDialog } from './scouts/ScoutRequestDialog';
import { useScoutActions } from './scouts/useScoutActions';

const ANY = '__any__';

function ScoutsInner({ workspaceId }: { workspaceId: string }) {
  const { locationId, locations, organizationId } = useWorkspace();
  // Create / run / pause / resume are editor actions (EDITORS gate). Everyone
  // else keeps the list; an unresolved role is denied until it resolves.
  const canEdit = canEditWorkspace(useActorRole(organizationId).role);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState(ANY);
  const actions = useScoutActions(workspaceId);

  // Drop a latched open intent when the role gate closes, so the create dialog
  // cannot spring back open by itself if the gate later reopens.
  if (!canEdit && dialogOpen) setDialogOpen(false);

  const { schedule, active: statusInFlight, stopped: statusUpdatesStopped } = useStatusPolling();
  const query = useQuery({
    queryKey: queryKeys.scoutRequests(workspaceId),
    queryFn: ({ signal }) => api.listScoutRequests(workspaceId, signal),
    // Re-read while any run is queued or running, so each row's status (and its Run now
    // button) follows the worker without a reload (UI-AUDIT F8 / P8), on the bounded,
    // backed-off schedule of lib/status-polling.ts (review F-02).
    // W16 §C (PROPOSED): also while a row's derived execution is `in_progress` under a settled
    // status, so its badge and Run now follow the worker as on the detail page.
    ...pollingOptions<ScoutRequestOut[]>(schedule, 'requests', (rows) =>
      inFlightSignature(
        rows?.map((r) => ({
          ...r,
          status: r.execution?.state === 'in_progress' && !isScoutInFlight(r.status) ? 'running' : r.status,
        })),
        isScoutInFlight,
      ),
    ),
  });
  const refreshStatus = () => {
    schedule.restart(Date.now());
    void query.refetch();
  };

  // R2-01: if "Refresh status" is removed (nothing in flight any more) while it has keyboard
  // focus, return focus to the Run now of the row that was in flight, else to the page
  // heading. Never otherwise.
  const pageRef = useRef<HTMLDivElement>(null);
  const lastInFlight = useRef<string[]>([]);
  const refocusAfterSettle = useRef(false);
  const inFlightIds = (query.data ?? []).filter((r) => isScoutInFlight(r.status)).map((r) => r.id).join(',');
  useEffect(() => {
    if (inFlightIds) lastInFlight.current = inFlightIds.split(',');
  }, [inFlightIds]);
  useEffect(() => {
    if (statusInFlight || !refocusAfterSettle.current) return;
    refocusAfterSettle.current = false;
    const root = pageRef.current;
    moveFocusTo(
      ...lastInFlight.current.map((id) => root?.querySelector<HTMLElement>(`[data-scout-row="${id}"] [data-run-now]`)),
      root?.querySelector<HTMLElement>('h1'),
    );
  }, [statusInFlight]);

  const locName = (id: string | null) => locations.find((l) => l.id === id)?.name;

  const filtered = useMemo(() => {
    let rows: ScoutRequestOut[] = query.data ?? [];
    if (locationId) rows = rows.filter((r) => r.location_id === locationId);
    if (statusFilter !== ANY) rows = rows.filter((r) => r.status === statusFilter);
    if (search.trim()) {
      const q = search.toLowerCase();
      rows = rows.filter(
        (r) =>
          r.name.toLowerCase().includes(q) ||
          (r.resolved_market ?? '').toLowerCase().includes(q) ||
          r.keywords.some((k) => k.toLowerCase().includes(q)),
      );
    }
    return rows;
  }, [query.data, locationId, statusFilter, search]);

  return (
    <div ref={pageRef}>
      <PageHeader
        title="Scout requests"
        description="Create, run, pause and review market scouts. Each request stays isolated to its market and campaign."
        actions={
          canEdit ? (
            <Button onClick={() => setDialogOpen(true)}>
              <Plus className="size-4" /> New scout request
            </Button>
          ) : undefined
        }
      />

      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center">
        <div className="relative flex-1">
          <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search scout requests…" aria-label="Search scout requests" className="pl-8" />
        </div>
        <Select value={statusFilter} onValueChange={setStatusFilter}>
          <SelectTrigger aria-label="Filter by status" className="w-full sm:w-44">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value={ANY}>Any status</SelectItem>
            {Object.entries(scoutStatusLabels).map(([v, l]) => (
              <SelectItem key={v} value={v}>
                {l}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>

      {statusInFlight ? (
        <StatusRefresh
          stopped={statusUpdatesStopped}
          onRefresh={refreshStatus}
          onRemovedWhileFocused={() => {
            refocusAfterSettle.current = true;
          }}
          className="mb-4"
        />
      ) : null}

      {query.isLoading ? (
        <LoadingRows rows={3} />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      ) : filtered.length ? (
        <div className="space-y-3">
          {filtered.map((r) => {
            const canPause = ['running', 'queued', 'draft', 'completed'].includes(r.status);
            const isPaused = r.status === 'paused';
            return (
              <Card key={r.id} data-scout-row={r.id}>
                <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link to={`/scout-requests/${r.id}`} className="font-semibold hover:text-primary hover:underline">
                        {r.name}
                      </Link>
                      <ScoutStatusBadge value={r.status} />
                      {r.execution?.state === 'needs_attention' ? (
                        <Badge intent="danger" data-needs-attention="">
                          Needs attention
                        </Badge>
                      ) : null}
                      {r.resolved_market ? <Badge intent="outline">{r.resolved_market}</Badge> : null}
                      {locName(r.location_id) ? <Badge intent="neutral">{locName(r.location_id)}</Badge> : null}
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {formatCount(statValue(r.stats, 'opportunities'), 'opportunity', 'opportunities')} ·{' '}
                      {formatCount(statValue(r.stats, 'signals_analyzed'), 'signal', 'signals')} ·{' '}
                      {r.last_run_at ? `last run ${formatRelative(r.last_run_at)}` : 'never run'}
                    </p>
                  </div>
                  {canEdit ? (
                    <div className="flex shrink-0 flex-wrap gap-2">
                      <RunNowButton request={r} actions={actions} size="sm" />
                      {isPaused ? (
                        <Button size="sm" variant="outline" onClick={() => actions.resume.mutate(r.id)}>
                          <Play className="size-4" /> Resume
                        </Button>
                      ) : (
                        <Button size="sm" variant="outline" onClick={() => actions.pause.mutate(r.id)} disabled={!canPause}>
                          <Pause className="size-4" /> Pause
                        </Button>
                      )}
                    </div>
                  ) : null}
                </CardContent>
              </Card>
            );
          })}
        </div>
      ) : (
        <EmptyState
          icon={ListChecks}
          title={query.data?.length ? 'No matching scout requests' : 'No scout requests yet'}
          description={
            query.data?.length
              ? 'Adjust your search or status filter.'
              : canEdit
                ? 'Create your first scout request to start collecting market signals and generating opportunities.'
                : 'No scout requests have been created in this workspace yet.'
          }
          action={
            canEdit ? (
              <Button onClick={() => setDialogOpen(true)}>
                <Plus className="size-4" /> New scout request
              </Button>
            ) : undefined
          }
        />
      )}

      {canEdit ? (
        <ScoutRequestDialog workspaceId={workspaceId} open={dialogOpen} onOpenChange={setDialogOpen} />
      ) : null}
    </div>
  );
}

export function ScoutRequestsPage() {
  return <RequireWorkspace>{({ workspaceId }) => <ScoutsInner workspaceId={workspaceId} />}</RequireWorkspace>;
}
