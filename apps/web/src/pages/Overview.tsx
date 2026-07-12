import { useQuery } from '@tanstack/react-query';
import {
  Activity,
  ArrowRight,
  Gauge,
  Radar,
  ShieldAlert,
  Sparkles,
} from 'lucide-react';
import { Link } from 'react-router-dom';
import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip as RTooltip,
  XAxis,
  YAxis,
} from 'recharts';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { OpportunityCard, ScoutRequestOut } from '@/api/types';
import { ClassificationBadge, ScoutStatusBadge, SimulatedBadge } from '@/components/common/badges';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { PageHeader } from '@/components/layout/page-header';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { classificationLabels, label } from '@/lib/labels';
import { formatRelative } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

function num(stats: Record<string, unknown>, key: string): number {
  const v = stats[key];
  return typeof v === 'number' ? v : 0;
}

function StatCard({
  icon: Icon,
  label: title,
  value,
  hint,
}: {
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  value: string | number;
  hint?: string;
}) {
  return (
    <Card>
      <CardContent className="flex items-start justify-between gap-3 p-5">
        <div>
          <p className="text-sm text-muted-foreground">{title}</p>
          <p className="mt-1 text-2xl font-semibold tabular-nums">{value}</p>
          {hint ? <p className="mt-1 text-xs text-muted-foreground">{hint}</p> : null}
        </div>
        <span className="flex size-9 items-center justify-center rounded-lg bg-primary/10 text-primary">
          <Icon className="size-5" />
        </span>
      </CardContent>
    </Card>
  );
}

function OverviewInner({ workspaceId }: { workspaceId: string }) {
  const { locationId, activeLocation, locations } = useWorkspace();

  const filters = { location_id: locationId ?? undefined, limit: 200 };
  const oppsQuery = useQuery({
    queryKey: queryKeys.opportunities(workspaceId, filters),
    queryFn: ({ signal }) => api.listOpportunities(workspaceId, filters, signal),
  });
  const scoutQuery = useQuery({
    queryKey: queryKeys.scoutRequests(workspaceId),
    queryFn: ({ signal }) => api.listScoutRequests(workspaceId, signal),
  });

  if (oppsQuery.isLoading || scoutQuery.isLoading) {
    return <LoadingRows rows={5} />;
  }
  if (oppsQuery.isError) {
    return <ErrorState error={oppsQuery.error} onRetry={() => oppsQuery.refetch()} />;
  }
  if (scoutQuery.isError) {
    return <ErrorState error={scoutQuery.error} onRetry={() => scoutQuery.refetch()} />;
  }

  const opps: OpportunityCard[] = oppsQuery.data ?? [];
  const scouts: ScoutRequestOut[] = (scoutQuery.data ?? []).filter(
    (s) => !locationId || s.location_id === locationId,
  );

  const active = scouts.filter((s) => ['running', 'queued', 'paused'].includes(s.status));
  const avg = (key: 'relevance_score' | 'confidence_score') =>
    opps.length ? Math.round(opps.reduce((sum, o) => sum + o[key], 0) / opps.length) : 0;
  const riskCount = opps.filter((o) => o.risk_level === 'high' || o.risk_level === 'blocked').length;

  const signalsProcessed = scouts.reduce((sum, s) => sum + num(s.stats, 'signals_processed'), 0);
  const noiseFiltered = scouts.reduce((sum, s) => sum + num(s.stats, 'noise_filtered'), 0);

  const byClassification = Object.keys(classificationLabels)
    .map((key) => ({
      key,
      name: label(classificationLabels, key),
      count: opps.filter((o) => o.classification === key).length,
    }))
    .filter((d) => d.count > 0);

  const byMarket = Object.entries(
    opps.reduce<Record<string, number>>((acc, o) => {
      const market = o.resolved_market ?? 'Unspecified';
      acc[market] = (acc[market] ?? 0) + 1;
      return acc;
    }, {}),
  ).sort((a, b) => b[1] - a[1]);

  const recentOpps = [...opps]
    .sort((a, b) => b.created_at.localeCompare(a.created_at))
    .slice(0, 5);
  const recentScouts = [...scouts]
    .sort((a, b) => (b.last_run_at ?? '').localeCompare(a.last_run_at ?? ''))
    .slice(0, 5);

  const chartColors = ['#94a3b8', '#94a3b8', '#64748b', '#3b82f6', '#3b82f6', '#16a34a', '#16a34a', '#94a3b8'];

  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description={
          activeLocation
            ? `Scouting health for ${activeLocation.name}.`
            : 'Scouting health across all locations in this workspace.'
        }
        actions={
          <Button asChild variant="outline">
            <Link to="/scout-requests">
              <Radar className="size-4" /> Scout requests
            </Link>
          </Button>
        }
      />

      <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
        <StatCard icon={Radar} label="Active scout requests" value={active.length} hint={`${scouts.length} total`} />
        <StatCard icon={Sparkles} label="Opportunities" value={opps.length} hint={`${byMarket.length} markets`} />
        <StatCard icon={Gauge} label="Avg relevance" value={avg('relevance_score')} hint={`Avg confidence ${avg('confidence_score')}`} />
        <StatCard icon={ShieldAlert} label="High-risk / claims" value={riskCount} hint={`${noiseFiltered} noise filtered · ${signalsProcessed} signals`} />
      </div>

      {opps.length === 0 && scouts.length === 0 ? (
        <EmptyState
          icon={Radar}
          title="No scouting activity yet"
          description="Create a scout request to start collecting signals and generating explainable opportunities."
          action={
            <Button asChild>
              <Link to="/scout-requests">Create scout request</Link>
            </Button>
          }
        />
      ) : (
        <div className="grid gap-6 lg:grid-cols-3">
          <Card className="lg:col-span-2">
            <CardHeader>
              <CardTitle>Opportunities by classification</CardTitle>
            </CardHeader>
            <CardContent>
              {byClassification.length ? (
                <ResponsiveContainer width="100%" height={240}>
                  <BarChart data={byClassification} margin={{ left: -20 }}>
                    <XAxis dataKey="name" tick={{ fontSize: 11 }} interval={0} angle={-12} textAnchor="end" height={50} />
                    <YAxis allowDecimals={false} tick={{ fontSize: 11 }} />
                    <RTooltip
                      cursor={{ fill: 'hsl(var(--muted))' }}
                      contentStyle={{
                        background: 'hsl(var(--popover))',
                        border: '1px solid hsl(var(--border))',
                        borderRadius: 8,
                        fontSize: 12,
                      }}
                    />
                    <Bar dataKey="count" radius={[4, 4, 0, 0]}>
                      {byClassification.map((d, i) => (
                        <Cell key={d.key} fill={chartColors[i % chartColors.length]} />
                      ))}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              ) : (
                <p className="py-10 text-center text-sm text-muted-foreground">
                  No classified opportunities yet.
                </p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>By market</CardTitle>
            </CardHeader>
            <CardContent className="space-y-2">
              {byMarket.length ? (
                byMarket.slice(0, 8).map(([market, count]) => (
                  <div key={market} className="flex items-center justify-between text-sm">
                    <span className="truncate">{market}</span>
                    <Badge intent="neutral">{count}</Badge>
                  </div>
                ))
              ) : (
                <p className="text-sm text-muted-foreground">No markets resolved yet.</p>
              )}
            </CardContent>
          </Card>

          <Card className="lg:col-span-2">
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle>Recent opportunities</CardTitle>
              <Button asChild variant="link" size="sm" className="h-auto p-0">
                <Link to="/opportunities">
                  View all <ArrowRight className="size-3.5" />
                </Link>
              </Button>
            </CardHeader>
            <CardContent className="space-y-2">
              {recentOpps.length ? (
                recentOpps.map((o) => (
                  <Link
                    key={o.id}
                    to={`/opportunities/${o.id}`}
                    className="flex items-center justify-between gap-3 rounded-md border border-border px-3 py-2 text-sm transition-colors hover:bg-secondary"
                  >
                    <span className="min-w-0">
                      <span className="block truncate font-medium">{o.title}</span>
                      <span className="text-xs text-muted-foreground">
                        {o.resolved_market ?? 'Unspecified market'} · {formatRelative(o.created_at)}
                      </span>
                    </span>
                    <span className="flex shrink-0 items-center gap-2">
                      {o.is_simulated ? <SimulatedBadge /> : null}
                      <ClassificationBadge value={o.classification} />
                      <span className="tabular-nums font-semibold">{Math.round(o.opportunity_score)}</span>
                    </span>
                  </Link>
                ))
              ) : (
                <p className="text-sm text-muted-foreground">No opportunities yet.</p>
              )}
            </CardContent>
          </Card>

          <Card>
            <CardHeader className="flex-row items-center justify-between space-y-0">
              <CardTitle>Recent scout activity</CardTitle>
              <Activity className="size-4 text-muted-foreground" />
            </CardHeader>
            <CardContent className="space-y-2">
              {recentScouts.length ? (
                recentScouts.map((s) => (
                  <Link
                    key={s.id}
                    to={`/scout-requests/${s.id}`}
                    className="flex items-center justify-between gap-2 rounded-md border border-border px-3 py-2 text-sm transition-colors hover:bg-secondary"
                  >
                    <span className="min-w-0">
                      <span className="block truncate font-medium">{s.name}</span>
                      <span className="text-xs text-muted-foreground">
                        {s.last_run_at ? formatRelative(s.last_run_at) : 'Not run yet'}
                      </span>
                    </span>
                    <ScoutStatusBadge value={s.status} />
                  </Link>
                ))
              ) : (
                <p className="text-sm text-muted-foreground">No scout requests yet.</p>
              )}
            </CardContent>
          </Card>
        </div>
      )}

      <SetupStatus workspaceId={workspaceId} locationsCount={locations.length} opportunitiesCount={opps.length} scoutsCount={scouts.length} />
    </div>
  );
}

function SetupStatus({
  workspaceId,
  locationsCount,
  opportunitiesCount,
  scoutsCount,
}: {
  workspaceId: string;
  locationsCount: number;
  opportunitiesCount: number;
  scoutsCount: number;
}) {
  const { activeWorkspace } = useWorkspace();
  const steps = [
    { label: 'Business profile completed', done: Boolean(activeWorkspace?.onboarding_completed), to: '/onboarding' },
    { label: 'At least one location added', done: locationsCount > 0, to: '/locations' },
    { label: 'A scout request created', done: scoutsCount > 0, to: '/scout-requests' },
    { label: 'Opportunities generated', done: opportunitiesCount > 0, to: '/opportunities' },
  ];
  const done = steps.filter((s) => s.done).length;
  void workspaceId;

  if (done === steps.length) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Finish setting up ({done}/{steps.length})</CardTitle>
      </CardHeader>
      <CardContent className="grid gap-2 sm:grid-cols-2">
        {steps.map((step) => (
          <Link
            key={step.label}
            to={step.to}
            className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm hover:bg-secondary"
          >
            <span
              className={`flex size-5 items-center justify-center rounded-full text-xs ${
                step.done ? 'bg-success text-success-foreground' : 'border border-border text-muted-foreground'
              }`}
              aria-hidden
            >
              {step.done ? '✓' : ''}
            </span>
            <span className={step.done ? 'text-muted-foreground line-through' : ''}>{step.label}</span>
          </Link>
        ))}
      </CardContent>
    </Card>
  );
}

export function OverviewPage() {
  return <RequireWorkspace>{({ workspaceId }) => <OverviewInner workspaceId={workspaceId} />}</RequireWorkspace>;
}
