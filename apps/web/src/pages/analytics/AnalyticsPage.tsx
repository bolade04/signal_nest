import { BarChart3, Lock, MapPin, PowerOff } from 'lucide-react';
import type { AnalyticsLatency, AnalyticsLocationTotals, WorkspaceAnalytics } from '@/api/types';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { PageHeader } from '@/components/layout/page-header';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import { formatDateTime } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import {
  familySummaries,
  formatCount,
  formatDuration,
  formatRate,
  NOT_ENOUGH_DATA,
  type FamilySummary,
} from './analyticsFormat';
import {
  isAnalyticsDark,
  isAnalyticsNotFound,
  isAnalyticsRefused,
  useAnalyticsAccess,
  useWorkspaceAnalytics,
} from './useAnalytics';

/**
 * Status analytics (Project Phase 5E, lane 5E-W2): counts by state for opportunities, briefs,
 * approval decisions, drafts, content reviews and exports; approval latency; generation and
 * review outcomes; and a per-location comparison — for the active workspace, or the location
 * selected in the shell.
 *
 * States, each visible:
 * - loading — the role, the capability reflection or the figures are not known yet;
 * - refused — a viewer or compliance reviewer (the API's exact-role gate answers 403); no
 *   analytics request is sent for them, and a 403 answer renders the same state;
 * - dark — the notifications-and-analytics capability (O-12(B)) is off for the workspace, by
 *   its reflection (no analytics request is sent) or by the API's 503;
 * - a location that is not the workspace's (404), and any other failure (with a retry);
 * - the figures, where a figure the API could not compute (`null`) shows a dash and
 *   "Not enough data yet", never 0.
 *
 * The integration owner mounts it (`App.tsx` route, `nav.ts` entry gated by
 * `useAnalyticsVisible`). UX only: the API re-decides role, capability and scope.
 */
export function AnalyticsPage() {
  return (
    <RequireWorkspace>{({ workspaceId }) => <AnalyticsInner workspaceId={workspaceId} />}</RequireWorkspace>
  );
}

function AnalyticsInner({ workspaceId }: { workspaceId: string }) {
  const { locationId, activeLocation } = useWorkspace();
  const access = useAnalyticsAccess();

  return (
    <div className="space-y-6">
      <PageHeader
        title="Status analytics"
        description={
          activeLocation
            ? `Where work stands for ${activeLocation.name}.`
            : 'Where work stands across all locations in this workspace.'
        }
      />
      {access === 'loading' ? (
        <LoadingRows rows={3} />
      ) : access === 'not_reader' ? (
        <AnalyticsRefused />
      ) : access === 'dark' ? (
        <AnalyticsDark />
      ) : (
        <AnalyticsDashboard workspaceId={workspaceId} locationId={locationId ?? null} />
      )}
    </div>
  );
}

/** The 403 state: the member's role may not read analytics. */
export function AnalyticsRefused() {
  return (
    <EmptyState
      icon={Lock}
      title="Status analytics is not available to your role"
      description="Owners, admins, marketers and reviewers can see status analytics. An owner or admin can change your role under Settings → Members."
    />
  );
}

/** The dark state: the capability is off for this workspace. */
export function AnalyticsDark() {
  return (
    <EmptyState
      icon={PowerOff}
      title="Status analytics is not available for this workspace yet"
      description="Notifications and status analytics are switched off here."
    />
  );
}

function AnalyticsDashboard({
  workspaceId,
  locationId,
}: {
  workspaceId: string;
  locationId: string | null;
}) {
  const query = useWorkspaceAnalytics(workspaceId, locationId);

  if (query.isPending) return <LoadingRows rows={4} />;
  if (query.isError) {
    if (isAnalyticsRefused(query.error)) return <AnalyticsRefused />;
    if (isAnalyticsDark(query.error)) return <AnalyticsDark />;
    if (isAnalyticsNotFound(query.error)) {
      return (
        <EmptyState
          icon={MapPin}
          title="This location is not part of the workspace"
          description="Choose another location, or all locations, to see status analytics."
        />
      );
    }
    return <ErrorState error={query.error} onRetry={() => query.refetch()} />;
  }

  const data = query.data;
  return (
    <div className="space-y-6">
      <section aria-labelledby="analytics-counts" className="space-y-3">
        <h2 id="analytics-counts" className="text-base font-semibold">
          Counts by state
        </h2>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-3">
          {familySummaries(data).map((family) => (
            <FamilyCard key={family.key} family={family} />
          ))}
        </div>
      </section>

      <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
        <LatencyCard briefs={data.approval_latency.briefs} drafts={data.approval_latency.drafts} />
        <OutcomesCard data={data} />
      </div>

      <LocationComparison data={data} />

      <p className="text-xs text-muted-foreground">
        Computed from this workspace&apos;s records {formatDateTime(data.generated_at)}. A dash
        (—) marks a figure there is not enough data to compute yet. Operational status only:
        advertising performance is not measured here.
      </p>
    </div>
  );
}

function FamilyCard({ family }: { family: FamilySummary }) {
  const headingId = `analytics-family-${family.key}`;
  return (
    <Card>
      <CardHeader className="pb-2">
        <CardTitle id={headingId} className="text-sm font-medium text-muted-foreground">
          {family.title}
        </CardTitle>
        <p className="text-2xl font-semibold tabular-nums">{formatCount(family.total)}</p>
      </CardHeader>
      <CardContent>
        <dl aria-labelledby={headingId} className="space-y-1 text-sm">
          {family.rows.map((row) => (
            <div key={row.key} className="flex items-center justify-between gap-3">
              <dt className="text-muted-foreground">{row.label}</dt>
              <dd className="tabular-nums">{formatCount(row.value)}</dd>
            </div>
          ))}
        </dl>
      </CardContent>
    </Card>
  );
}

/** A figure that may be `null`: the dash plus the words, never a zero. */
function NullableFigure({ value, children }: { value: number | null; children: string }) {
  return (
    <span className="flex flex-col items-end">
      <span className="tabular-nums">{children}</span>
      {value === null ? (
        <span className="text-xs text-muted-foreground">{NOT_ENOUGH_DATA}</span>
      ) : null}
    </span>
  );
}

function LatencyCard({ briefs, drafts }: { briefs: AnalyticsLatency; drafts: AnalyticsLatency }) {
  const rows: { key: string; label: string; latency: AnalyticsLatency }[] = [
    { key: 'briefs', label: 'Briefs', latency: briefs },
    { key: 'drafts', label: 'Drafts', latency: drafts },
  ];
  return (
    <Card>
      <CardHeader>
        <CardTitle>Approval latency</CardTitle>
        <CardDescription>Time from a version&apos;s creation to its first decision.</CardDescription>
      </CardHeader>
      <CardContent>
        <table className="w-full text-sm">
          <caption className="sr-only">Approval latency for briefs and drafts</caption>
          <thead>
            <tr className="text-left text-xs text-muted-foreground">
              <th scope="col" className="pb-2 font-medium">
                Subject
              </th>
              <th scope="col" className="pb-2 text-right font-medium">
                Decided
              </th>
              <th scope="col" className="pb-2 text-right font-medium">
                Median
              </th>
              <th scope="col" className="pb-2 text-right font-medium">
                Mean
              </th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ key, label, latency }) => (
              <tr key={key} className="border-t border-border align-top">
                <th scope="row" className="py-2 text-left font-medium">
                  {label}
                </th>
                <td className="py-2 text-right tabular-nums">{formatCount(latency.decided)}</td>
                <td className="py-2 text-right">
                  <NullableFigure value={latency.median_seconds}>
                    {formatDuration(latency.median_seconds)}
                  </NullableFigure>
                </td>
                <td className="py-2 text-right">
                  <NullableFigure value={latency.mean_seconds}>
                    {formatDuration(latency.mean_seconds)}
                  </NullableFigure>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </CardContent>
    </Card>
  );
}

function OutcomesCard({ data }: { data: WorkspaceAnalytics }) {
  const o = data.outcomes;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Generation and review outcomes</CardTitle>
        <CardDescription>Generated draft versions and how their content reviews ended.</CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="space-y-2 text-sm">
          <OutcomeRow label="Drafts generated" value={formatCount(o.drafts_generated)} />
          <OutcomeRow label="Revisions of an earlier version" value={formatCount(o.revisions)} />
          <OutcomeRow label="From the simulated generator" value={formatCount(o.simulated)} />
          <OutcomeRow
            label="Reviews passed"
            value={
              <NullableFigure value={o.review_pass_rate}>
                {formatRate(o.review_pass_rate)}
              </NullableFigure>
            }
          />
          <OutcomeRow
            label="Reviews blocked"
            value={
              <NullableFigure value={o.review_block_rate}>
                {formatRate(o.review_block_rate)}
              </NullableFigure>
            }
          />
        </dl>
      </CardContent>
    </Card>
  );
}

function OutcomeRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="tabular-nums">{value}</dd>
    </div>
  );
}

const COMPARED: { key: keyof AnalyticsLocationTotals; label: string }[] = [
  { key: 'opportunities', label: 'Opportunities' },
  { key: 'briefs', label: 'Briefs' },
  { key: 'drafts', label: 'Drafts' },
  { key: 'approvals', label: 'Decisions' },
  { key: 'exports', label: 'Exports' },
];

function LocationComparison({ data }: { data: WorkspaceAnalytics }) {
  const narrowed = data.location_id !== null;
  const unassigned = data.unassigned;
  return (
    <Card>
      <CardHeader>
        <CardTitle>By location</CardTitle>
        <CardDescription>
          {narrowed
            ? 'The selected location. Choose all locations to compare them.'
            : 'Every location of this workspace. Records that name no location are listed last.'}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {data.locations.length === 0 && unassigned === null ? (
          <p className="text-sm text-muted-foreground">No locations yet.</p>
        ) : (
          // The table scrolls inside its card on a narrow screen instead of widening the page.
          <div className="overflow-x-auto">
            <table className="w-full min-w-[32rem] text-sm">
              <caption className="sr-only">Totals by location</caption>
              <thead>
                <tr className="text-left text-xs text-muted-foreground">
                  <th scope="col" className="pb-2 font-medium">
                    Location
                  </th>
                  {COMPARED.map((c) => (
                    <th key={c.key} scope="col" className="pb-2 text-right font-medium">
                      {c.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {data.locations.map((row) => (
                  <tr key={row.location_id} className="border-t border-border">
                    <th scope="row" className="py-2 text-left font-medium">
                      <span className="flex items-center gap-2">
                        <BarChart3 className="size-3.5 shrink-0 text-muted-foreground" aria-hidden />
                        <span className="truncate">{row.name}</span>
                        {row.is_active ? null : <Badge intent="muted">Inactive</Badge>}
                      </span>
                    </th>
                    {COMPARED.map((c) => (
                      <td key={c.key} className="py-2 text-right tabular-nums">
                        {formatCount(row[c.key])}
                      </td>
                    ))}
                  </tr>
                ))}
                {unassigned ? (
                  <tr className="border-t border-border">
                    <th scope="row" className="py-2 text-left font-medium text-muted-foreground">
                      No location
                    </th>
                    {COMPARED.map((c) => (
                      <td key={c.key} className="py-2 text-right tabular-nums text-muted-foreground">
                        {formatCount(unassigned[c.key])}
                      </td>
                    ))}
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>
    </Card>
  );
}
