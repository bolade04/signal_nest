import { useQuery } from '@tanstack/react-query';
import { Search, SlidersHorizontal, Sparkles, X } from 'lucide-react';
import { useEffect, useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { OpportunityFilters } from '@/api/types';
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
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { OpportunityCardView } from './opportunities/OpportunityCardView';

const ANY = '__any__';

interface UiFilters {
  classification: string;
  decision: string;
  status: string;
  risk_level: string;
  market: string;
  min_score: number;
  search: string;
  sort: string;
  order: string;
}

const defaultFilters: UiFilters = {
  classification: ANY,
  decision: ANY,
  status: ANY,
  risk_level: ANY,
  market: '',
  min_score: 0,
  search: '',
  sort: 'opportunity_score',
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
      limit: 100,
    }),
    [locationId, filters],
  );

  const query = useQuery({
    queryKey: queryKeys.opportunities(workspaceId, apiFilters),
    queryFn: ({ signal }) => api.listOpportunities(workspaceId, apiFilters, signal),
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

  const results = query.data ?? [];

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
              const [sort, order] = v.split(':');
              setFilters((f) => ({ ...f, sort: sort!, order: order! }));
            }}>
              <SelectTrigger aria-label="Sort" className="h-9 w-[190px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                <SelectItem value="opportunity_score:desc">Highest score</SelectItem>
                <SelectItem value="opportunity_score:asc">Lowest score</SelectItem>
                <SelectItem value="relevance_score:desc">Most relevant</SelectItem>
                <SelectItem value="confidence_score:desc">Most confident</SelectItem>
                <SelectItem value="created_at:desc">Newest</SelectItem>
                <SelectItem value="created_at:asc">Oldest</SelectItem>
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
        <span>
          {query.isLoading ? 'Loading…' : `${results.length} opportunit${results.length === 1 ? 'y' : 'ies'}`}
        </span>
      </div>

      {query.isLoading ? (
        <LoadingRows rows={4} />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      ) : results.length ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {results.map((o) => (
            <OpportunityCardView key={o.id} opportunity={o} />
          ))}
        </div>
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
        <EmptyState
          icon={Sparkles}
          title="No opportunities yet"
          description="Run a scout request to generate scored, explainable opportunities for this workspace."
        />
      )}
    </div>
  );
}

export function OpportunitiesPage() {
  return <RequireWorkspace>{({ workspaceId }) => <OpportunitiesInner workspaceId={workspaceId} />}</RequireWorkspace>;
}
