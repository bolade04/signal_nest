import { ArrowRight, BarChart3, RefreshCw } from 'lucide-react';
import { Link } from 'react-router-dom';
import { LoadingRows } from '@/components/common/states';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { familySummaries, formatCount, type FamilySummary } from './analyticsFormat';
import {
  isAnalyticsDark,
  isAnalyticsNotFound,
  isAnalyticsRefused,
  useAnalyticsAccess,
  useWorkspaceAnalytics,
} from './useAnalytics';

/**
 * The Overview's compact status-analytics summary (interface sheet P11): counts by state for
 * the six families, from the analytics endpoint, for the workspace or the location selected in
 * the shell.
 *
 * Rendered only for an analytics reader (owner, admin, marketer, reviewer) while the
 * notifications-and-analytics reflection says on. For anyone else — and while either is still
 * unknown — it renders nothing and sends no analytics request; for a non-reader it does not
 * even ask the reflection. A refusal the API makes anyway (403, 503, a 404 location) also
 * renders nothing: the Overview's own content never depends on this card.
 */
export function OverviewAnalyticsSummary({
  workspaceId,
  locationId,
}: {
  workspaceId: string;
  locationId: string | null;
}) {
  const access = useAnalyticsAccess();
  if (access !== 'ready') return null;
  return <SummaryCard workspaceId={workspaceId} locationId={locationId} />;
}

function SummaryCard({ workspaceId, locationId }: { workspaceId: string; locationId: string | null }) {
  const query = useWorkspaceAnalytics(workspaceId, locationId);

  if (query.isError) {
    const error = query.error;
    if (isAnalyticsRefused(error) || isAnalyticsDark(error) || isAnalyticsNotFound(error)) {
      return null;
    }
  }

  return (
    <Card role="region" aria-labelledby="overview-status-analytics">
      <CardHeader className="flex-row items-center justify-between space-y-0">
        <CardTitle id="overview-status-analytics" className="flex items-center gap-2">
          <BarChart3 className="size-4 text-primary" aria-hidden /> Status analytics
        </CardTitle>
        <Button asChild variant="link" size="sm" className="h-auto p-0">
          <Link to="/analytics">
            View analytics <ArrowRight className="size-3.5" />
          </Link>
        </Button>
      </CardHeader>
      <CardContent>
        {query.isPending ? (
          <LoadingRows rows={1} />
        ) : query.isError ? (
          <div className="flex items-center justify-between gap-3 text-sm text-muted-foreground">
            <span>Status analytics could not be loaded.</span>
            <Button variant="outline" size="sm" onClick={() => query.refetch()}>
              <RefreshCw className="size-4" /> Try again
            </Button>
          </div>
        ) : (
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-3">
            {familySummaries(query.data).map((family) => (
              <SummaryTile key={family.key} family={family} />
            ))}
          </ul>
        )}
      </CardContent>
    </Card>
  );
}

function SummaryTile({ family }: { family: FamilySummary }) {
  const states = family.rows
    .filter((row) => row.value > 0)
    .map((row) => `${formatCount(row.value)} ${row.label.toLowerCase()}`)
    .join(' · ');
  return (
    <li className="rounded-md border border-border px-3 py-2">
      <p className="flex items-baseline justify-between gap-2">
        <span className="text-sm text-muted-foreground">{family.title}</span>
        <span className="text-lg font-semibold tabular-nums">{formatCount(family.total)}</span>
      </p>
      <p className="mt-0.5 text-xs text-muted-foreground">{states || 'None yet'}</p>
    </li>
  );
}
