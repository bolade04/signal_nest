import { useQuery } from '@tanstack/react-query';
import { ListChecks, Pause, Play, Plus, Radar, Search } from 'lucide-react';
import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { ScoutRequestOut } from '@/api/types';
import { ScoutStatusBadge } from '@/components/common/badges';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
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
import { formatRelative } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { ScoutRequestDialog } from './scouts/ScoutRequestDialog';
import { useScoutActions } from './scouts/useScoutActions';

const ANY = '__any__';

function num(stats: Record<string, unknown>, key: string): number {
  const v = stats[key];
  return typeof v === 'number' ? v : 0;
}

function ScoutsInner({ workspaceId }: { workspaceId: string }) {
  const { locationId, locations } = useWorkspace();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [search, setSearch] = useState('');
  const [statusFilter, setStatusFilter] = useState(ANY);
  const actions = useScoutActions(workspaceId);

  const query = useQuery({
    queryKey: queryKeys.scoutRequests(workspaceId),
    queryFn: ({ signal }) => api.listScoutRequests(workspaceId, signal),
  });

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
    <div>
      <PageHeader
        title="Scout requests"
        description="Create, run, pause and review market scouts. Each request stays isolated to its market and campaign."
        actions={
          <Button onClick={() => setDialogOpen(true)}>
            <Plus className="size-4" /> New scout request
          </Button>
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
              <Card key={r.id}>
                <CardContent className="flex flex-col gap-3 p-4 sm:flex-row sm:items-center sm:justify-between">
                  <div className="min-w-0">
                    <div className="flex flex-wrap items-center gap-2">
                      <Link to={`/scout-requests/${r.id}`} className="font-semibold hover:text-primary hover:underline">
                        {r.name}
                      </Link>
                      <ScoutStatusBadge value={r.status} />
                      {r.resolved_market ? <Badge intent="outline">{r.resolved_market}</Badge> : null}
                      {locName(r.location_id) ? <Badge intent="neutral">{locName(r.location_id)}</Badge> : null}
                    </div>
                    <p className="mt-1 text-xs text-muted-foreground">
                      {num(r.stats, 'opportunities')} opportunities · {num(r.stats, 'signals_processed')} signals ·{' '}
                      {r.last_run_at ? `last run ${formatRelative(r.last_run_at)}` : 'never run'}
                    </p>
                  </div>
                  <div className="flex shrink-0 flex-wrap gap-2">
                    <Button
                      size="sm"
                      onClick={() => actions.run.mutate(r.id)}
                      disabled={actions.run.isPending && actions.run.variables === r.id}
                    >
                      <Radar className="size-4" /> Run now
                    </Button>
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
              : 'Create your first scout request to start collecting market signals and generating opportunities.'
          }
          action={
            <Button onClick={() => setDialogOpen(true)}>
              <Plus className="size-4" /> New scout request
            </Button>
          }
        />
      )}

      <ScoutRequestDialog workspaceId={workspaceId} open={dialogOpen} onOpenChange={setDialogOpen} />
    </div>
  );
}

export function ScoutRequestsPage() {
  return <RequireWorkspace>{({ workspaceId }) => <ScoutsInner workspaceId={workspaceId} />}</RequireWorkspace>;
}
