import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { AlertTriangle, Loader2, Radar, Search, SlidersHorizontal, Sparkles, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { Link, useSearchParams } from 'react-router-dom';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { OpportunityFilters, OpportunitySort } from '@/api/types';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { PageHeader } from '@/components/layout/page-header';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import {
  classificationLabels,
  decisionLabels,
  label,
  riskLabels,
  statusLabels,
} from '@/lib/labels';
import { formatDateTime } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { describeEmptyFeed } from './opportunities/emptyFeed';
import { OpportunityCardView } from './opportunities/OpportunityCardView';

const ANY = '__any__';
/** Rows per page of the feed; "Load more" fetches the next page by cursor (P6-UI-022). */
const PAGE_SIZE = 100;

interface UiFilters {
  classification: string;
  decision: string;
  status: string;
  risk_level: string;
  market: string;
  min_score: number;
  search: string;
  sort: OpportunitySort;
  order: string;
}

// The API's sort keys. This page used to send column names (`opportunity_score`, …),
// which the API did not recognise and silently replaced with `priority`, so every
// sort option sorted by priority. The keys are now typed from the generated contract.
const SORT_OPTIONS: { value: `${OpportunitySort}:${'asc' | 'desc'}`; label: string }[] = [
  { value: 'opportunity:desc', label: 'Highest score' },
  { value: 'opportunity:asc', label: 'Lowest score' },
  { value: 'relevance:desc', label: 'Most relevant' },
  { value: 'confidence:desc', label: 'Most confident' },
  { value: 'priority:desc', label: 'Highest priority' },
  { value: 'created:desc', label: 'Newest' },
  { value: 'created:asc', label: 'Oldest' },
];

const defaultFilters: UiFilters = {
  classification: ANY,
  decision: ANY,
  status: ANY,
  risk_level: ANY,
  market: '',
  min_score: 0,
  search: '',
  sort: 'opportunity',
  order: 'desc',
};

function EnumFilter({
  value,
  onChange,
  options,
  placeholder,
  ariaLabel,
}: {
  value: string;
  onChange: (v: string) => void;
  options: Record<string, string>;
  placeholder: string;
  ariaLabel: string;
}) {
  return (
    <Select value={value} onValueChange={onChange}>
      <SelectTrigger aria-label={ariaLabel} className="h-9 w-full sm:w-auto sm:min-w-[150px]">
        <SelectValue placeholder={placeholder} />
      </SelectTrigger>
      <SelectContent>
        <SelectItem value={ANY}>{placeholder}</SelectItem>
        {Object.entries(options).map(([v, l]) => (
          <SelectItem key={v} value={v}>
            {l}
          </SelectItem>
        ))}
      </SelectContent>
    </Select>
  );
}

function OpportunitiesInner({ workspaceId }: { workspaceId: string }) {
  const { locationId, activeLocation } = useWorkspace();
  const [searchParams, setSearchParams] = useSearchParams();
  const [filters, setFilters] = useState<UiFilters>(() => ({
    ...defaultFilters,
    search: searchParams.get('search') ?? '',
  }));
  const [searchDraft, setSearchDraft] = useState(filters.search);

  // Reset all filters when the active location changes so one market's filters
  // never carry over to another. Adjusted during render (guarded by the previous
  // location) rather than in an effect, preserving strict per-location isolation
  // without a cascading render.
  const [prevLocationId, setPrevLocationId] = useState(locationId);
  if (locationId !== prevLocationId) {
    setPrevLocationId(locationId);
    setFilters(defaultFilters);
    setSearchDraft('');
  }

  // The URL's `?search=` is the source of truth whenever it changes from outside this
  // page — the header's global search navigates here while the page is already mounted,
  // and a same-route navigation does not remount (P6-UI-021). Adjusted during render,
  // like the location reset above; the effect below writes our own changes back, which
  // this then sees as already applied.
  const urlSearch = searchParams.get('search') ?? '';
  const [prevUrlSearch, setPrevUrlSearch] = useState(urlSearch);
  if (urlSearch !== prevUrlSearch) {
    setPrevUrlSearch(urlSearch);
    if (urlSearch !== filters.search) {
      setFilters((f) => ({ ...f, search: urlSearch }));
      setSearchDraft(urlSearch);
    }
  }

  // Debounce the search box into the applied filters.
  useEffect(() => {
    const t = setTimeout(() => {
      setFilters((f) => (f.search === searchDraft ? f : { ...f, search: searchDraft }));
    }, 300);
    return () => clearTimeout(t);
  }, [searchDraft]);

  useEffect(() => {
    if (filters.search) setSearchParams({ search: filters.search }, { replace: true });
    else setSearchParams({}, { replace: true });
  }, [filters.search, setSearchParams]);

  const apiFilters: OpportunityFilters = useMemo(
    () => ({
      location_id: locationId ?? undefined,
      classification: filters.classification === ANY ? undefined : filters.classification,
      decision: filters.decision === ANY ? undefined : filters.decision,
      status: filters.status === ANY ? undefined : filters.status,
      risk_level: filters.risk_level === ANY ? undefined : filters.risk_level,
      market: filters.market || undefined,
      min_score: filters.min_score || undefined,
      search: filters.search || undefined,
      sort: filters.sort,
      order: filters.order,
      limit: PAGE_SIZE,
    }),
    [locationId, filters],
  );

  const query = useInfiniteQuery({
    queryKey: queryKeys.opportunityFeed(workspaceId, apiFilters),
    queryFn: ({ signal, pageParam }) =>
      api.listOpportunityPage(workspaceId, apiFilters, pageParam, signal),
    initialPageParam: null as string | null,
    getNextPageParam: (lastPage) => lastPage.nextCursor,
  });

  const set = <K extends keyof UiFilters>(key: K, value: UiFilters[K]) =>
    setFilters((f) => ({ ...f, [key]: value }));

  const activeChips: { key: string; label: string; clear: () => void }[] = [];
  if (filters.classification !== ANY)
    activeChips.push({
      key: 'classification',
      label: `Class: ${label(classificationLabels, filters.classification)}`,
      clear: () => set('classification', ANY),
    });
  if (filters.decision !== ANY)
    activeChips.push({ key: 'decision', label: `Decision: ${label(decisionLabels, filters.decision)}`, clear: () => set('decision', ANY) });
  if (filters.status !== ANY)
    activeChips.push({ key: 'status', label: `Status: ${label(statusLabels, filters.status)}`, clear: () => set('status', ANY) });
  if (filters.risk_level !== ANY)
    activeChips.push({ key: 'risk', label: `Risk: ${label(riskLabels, filters.risk_level)}`, clear: () => set('risk_level', ANY) });
  if (filters.market)
    activeChips.push({ key: 'market', label: `Market: ${filters.market}`, clear: () => set('market', '') });
  if (filters.min_score)
    activeChips.push({ key: 'min', label: `Min score ${filters.min_score}`, clear: () => set('min_score', 0) });
  if (filters.search)
    activeChips.push({
      key: 'search',
      label: `“${filters.search}”`,
      clear: () => {
        set('search', '');
        setSearchDraft('');
      },
    });

  const resetAll = () => {
    setFilters(defaultFilters);
    setSearchDraft('');
  };

  const pages = query.data?.pages;
  // Keyset pages do not overlap, but a row whose sort value changed between two page
  // fetches could appear twice; it is shown once.
  const results = useMemo(() => {
    const seen = new Set<string>();
    return (pages ?? []).flatMap((p) => p.items).filter((o) => !seen.has(o.id) && !!seen.add(o.id));
  }, [pages]);
  // The newest page's total: the whole filtered count, never the number loaded.
  const total = pages?.length ? pages[pages.length - 1]!.total : null;
  const noun = (n: number) => `opportunit${n === 1 ? 'y' : 'ies'}`;
  const countText =
    total === null
      ? query.hasNextPage
        ? `Showing the first ${results.length} ${noun(results.length)}`
        : `${results.length} ${noun(results.length)}`
      : results.length < total
        ? `Showing ${results.length} of ${total} ${noun(total)}`
        : `${total} ${noun(total)}`;

  const emptyUnfiltered = query.isSuccess && results.length === 0 && activeChips.length === 0;
  const scoutsQuery = useQuery({
    queryKey: queryKeys.scoutRequests(workspaceId),
    queryFn: ({ signal }) => api.listScoutRequests(workspaceId, signal),
    enabled: emptyUnfiltered,
  });

  return (
    <div>
      <PageHeader
        title="Opportunities"
        description={
          activeLocation
            ? `Explainable, scored opportunities for ${activeLocation.name}.`
            : 'Explainable, scored opportunities across all locations.'
        }
      />

      <div className="mb-4 space-y-3 rounded-lg border border-border bg-card p-4">
        <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
          <div className="relative flex-1">
            <Search className="pointer-events-none absolute left-2.5 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden />
            <Input
              type="search"
              value={searchDraft}
              onChange={(e) => setSearchDraft(e.target.value)}
              placeholder="Search opportunities…"
              aria-label="Search opportunities"
              className="pl-8"
            />
          </div>
          <div className="flex items-center gap-2">
            <SlidersHorizontal className="size-4 text-muted-foreground" aria-hidden />
            <Select value={`${filters.sort}:${filters.order}`} onValueChange={(v) => {
              const option = SORT_OPTIONS.find((o) => o.value === v);
              if (!option) return;
              const [sort, order] = option.value.split(':') as [OpportunitySort, string];
              setFilters((f) => ({ ...f, sort, order }));
            }}>
              <SelectTrigger aria-label="Sort" className="h-9 w-[190px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {SORT_OPTIONS.map((o) => (
                  <SelectItem key={o.value} value={o.value}>
                    {o.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          </div>
        </div>

        <div className="flex flex-wrap gap-2">
          <EnumFilter value={filters.classification} onChange={(v) => set('classification', v)} options={classificationLabels} placeholder="Any classification" ariaLabel="Filter by classification" />
          <EnumFilter value={filters.decision} onChange={(v) => set('decision', v)} options={decisionLabels} placeholder="Any decision" ariaLabel="Filter by decision" />
          <EnumFilter value={filters.status} onChange={(v) => set('status', v)} options={statusLabels} placeholder="Any status" ariaLabel="Filter by status" />
          <EnumFilter value={filters.risk_level} onChange={(v) => set('risk_level', v)} options={riskLabels} placeholder="Any risk" ariaLabel="Filter by risk" />
          <Input
            value={filters.market}
            onChange={(e) => set('market', e.target.value)}
            placeholder="Market"
            aria-label="Filter by market"
            className="h-9 w-full sm:w-36"
          />
          <Input
            type="number"
            min={0}
            max={100}
            value={filters.min_score || ''}
            onChange={(e) => set('min_score', Number(e.target.value) || 0)}
            placeholder="Min score"
            aria-label="Minimum opportunity score"
            className="h-9 w-full sm:w-28"
          />
        </div>

        {activeChips.length ? (
          <div className="flex flex-wrap items-center gap-2 border-t border-border pt-3">
            {activeChips.map((chip) => (
              <button
                key={chip.key}
                onClick={chip.clear}
                className="inline-flex items-center gap-1 rounded-full bg-secondary px-2.5 py-0.5 text-xs font-medium hover:bg-secondary/70"
              >
                {chip.label}
                <X className="size-3" />
              </button>
            ))}
            <Button variant="ghost" size="sm" onClick={resetAll} className="h-6 px-2 text-xs">
              Reset all
            </Button>
          </div>
        ) : null}
      </div>

      <div className="mb-3 flex items-center justify-between text-sm text-muted-foreground" aria-live="polite">
        <span>{query.isLoading ? 'Loading…' : countText}</span>
      </div>

      {query.isLoading ? (
        <LoadingRows rows={4} />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      ) : results.length ? (
        <>
          <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
            {results.map((o) => (
              <OpportunityCardView key={o.id} opportunity={o} />
            ))}
          </div>
          {query.hasNextPage || query.isFetchNextPageError ? (
            <div className="mt-6 flex flex-col items-center gap-2">
              {query.isFetchNextPageError ? (
                <p role="alert" className="text-sm text-destructive">
                  Could not load more opportunities.
                </p>
              ) : null}
              <Button
                variant="outline"
                onClick={() => query.fetchNextPage()}
                disabled={query.isFetchingNextPage}
              >
                {query.isFetchingNextPage ? <Loader2 className="size-4 animate-spin" /> : null}
                {query.isFetchNextPageError ? 'Try again' : 'Load more'}
              </Button>
            </div>
          ) : null}
        </>
      ) : activeChips.length ? (
        <EmptyState
          icon={Search}
          title="No matches"
          description="No opportunities match these filters. Try broadening or resetting them."
          action={
            <Button variant="outline" onClick={resetAll}>
              Reset filters
            </Button>
          }
        />
      ) : (
        <EmptyFeedState
          scouts={
            scoutsQuery.data
              ? scoutsQuery.data.filter((s) => !locationId || s.location_id === locationId)
              : null
          }
          locationName={activeLocation?.name ?? null}
        />
      )}
    </div>
  );
}

/**
 * The unfiltered feed is empty: say which of the cases it is (P6-UI-004), rather than
 * telling a user who just ran a scout to run one. `scouts` is null until they load.
 */
function EmptyFeedState({
  scouts,
  locationName,
}: {
  scouts: Parameters<typeof describeEmptyFeed>[0] | null;
  locationName: string | null;
}) {
  if (!scouts) {
    return <EmptyState icon={Sparkles} title="No opportunities yet" />;
  }
  const where = locationName ? ` for ${locationName}` : '';
  const state = describeEmptyFeed(scouts);
  const open = (id: string, label: string) => (
    <Button asChild variant="outline">
      <Link to={`/scout-requests/${id}`}>{label}</Link>
    </Button>
  );
  switch (state.kind) {
    case 'no-scouts':
      return (
        <EmptyState
          icon={Radar}
          title="No scouts yet"
          description={`Create a scout request${where} to generate scored, explainable opportunities.`}
          action={
            <Button asChild variant="outline">
              <Link to="/scout-requests">Go to scout requests</Link>
            </Button>
          }
        />
      );
    case 'never-ran':
      return (
        <EmptyState
          icon={Radar}
          title="Your scout has not run yet"
          description={`“${state.scout.name}” has not been run. Run it to generate opportunities${where}.`}
          action={open(state.scout.id, 'Open scout request')}
        />
      );
    case 'in-flight':
      return (
        <EmptyState
          icon={Loader2}
          title="A scout is running"
          description={`“${state.scout.name}” is ${state.scout.status}. Its opportunities appear here when it finishes.`}
          action={open(state.scout.id, 'View progress')}
        />
      );
    case 'last-run-failed':
      return (
        <EmptyState
          icon={AlertTriangle}
          title="The last scout run failed"
          description={`“${state.scout.name}” did not finish, so it produced no opportunities. Open it to see the run history.`}
          action={open(state.scout.id, 'Open scout request')}
        />
      );
    case 'ran-found-nothing': {
      const when = state.scout.last_run_at ? ` on ${formatDateTime(state.scout.last_run_at)}` : '';
      const found =
        state.reason ??
        (state.signalsAnalyzed === 0
          ? 'It found no signals to analyse.'
          : state.signalsAnalyzed !== null
            ? `It analysed ${state.signalsAnalyzed} signal${state.signalsAnalyzed === 1 ? '' : 's'}; none qualified as an opportunity.`
            : 'It produced no opportunities.');
      return (
        <EmptyState
          icon={Search}
          title="Your scout ran but found no opportunities"
          description={`“${state.scout.name}” last ran${when}. ${found}`}
          action={open(state.scout.id, 'Open scout request')}
        />
      );
    }
  }
}

export function OpportunitiesPage() {
  return <RequireWorkspace>{({ workspaceId }) => <OpportunitiesInner workspaceId={workspaceId} />}</RequireWorkspace>;
}
