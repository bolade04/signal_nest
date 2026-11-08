import { useQuery } from '@tanstack/react-query';
import { FileText, Sparkles } from 'lucide-react';
import { useState } from 'react';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { RecommendationBriefOut } from '@/api/types';
import { ErrorState } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { canEditWorkspace, useActorRole } from '@/lib/roles';
import { Link } from 'react-router-dom';
import { BriefDecisions } from '@/pages/approvals/BriefDecisions';
import { useCreativeCapability } from '@/pages/creative/useCreative';
import { formatDateTime } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import {
  isBriefsDark,
  useBriefCapability,
  useCreateBrief,
  useOpportunityBriefs,
} from './useBriefs';
import { useOpportunityIntelligence } from './useOpportunityIntelligence';

/**
 * The author as captured on the brief: `created_by_snapshot` is
 * `"<user id> <full name> <<email>>"` (frozen at CF-1) or `"[erased]"`. Only the name is
 * shown; the id and address stay out of the page.
 */
// eslint-disable-next-line react-refresh/only-export-components
export function briefAuthorName(snapshot: string): string {
  if (snapshot === '[erased]') return 'a deleted account';
  const withoutId = snapshot.replace(/^\S+\s+/, '');
  const name = withoutId.replace(/\s*<[^<>]*>$/, '').trim();
  return name || 'Unknown author';
}

/**
 * Operator ruling Q-1 (2026-11-08): a draft inherits its brief's captured jurisdiction;
 * editing the location changes no existing brief, draft or review; a new brief version is
 * needed to use a changed location. Said where a version's drafts are opened, to every member
 * who sees "Open drafts"; the remedy (`NEW_VERSION_REMEDY`) only to members who can create a
 * brief version (review r3 copy C-a). No jurisdiction code is shown: briefs do not expose one.
 * For an opportunity known to have no location, `NO_LOCATION_NOTE` is said instead (Q-4).
 */
const DRAFT_JURISDICTION_NOTE =
  'Drafts from this version use the jurisdiction recorded when this version was created. ' +
  'Editing the location does not change existing briefs, drafts or reviews.';
const NEW_VERSION_REMEDY = 'To use a changed location, generate a new version first.';

/**
 * Operator ruling Q-4 (2026-11-08): an opportunity with no location gets a recommendation
 * brief with no jurisdiction, and item 31 refuses drafts from it. Said where the brief is
 * created and viewed, only when the page's opportunity is known to have no location, and only
 * while creative drafts are offered at all (as "Open drafts" is). It replaces the Q-1 note,
 * which is about a recorded jurisdiction that such a brief does not have.
 */
const NO_LOCATION_NOTE =
  'This opportunity has no location (for example, it came from a workspace-wide scout), so its ' +
  'brief gives recommendations only. Creative drafts can’t be generated from it, because a ' +
  'draft needs the jurisdiction of a location.';

/**
 * The brief capture's `jurisdiction_unresolved` refusal (5D-W3; S1-5D capture rule). Since
 * Q-4 an opportunity with no location is not refused, so the cause is always an existing
 * location: a missing or unknown country, a missing state or province where one is required
 * (the item-33 list: US, CA, AU, NG), or an address that names another place. The API sends
 * only the code, so one static wording covers them all. Never the server's text.
 */
const JURISDICTION_UNRESOLVED_MESSAGE =
  'No brief was generated because the jurisdiction of this opportunity’s location could not be ' +
  'determined. Complete or correct the location’s details so that its country, its state or ' +
  'province (required in the United States, Canada, Australia and Nigeria) and its address all ' +
  'name the same place, then generate the brief again.';

function isJurisdictionUnresolved(error: unknown): boolean {
  if (!(error instanceof ApiError)) return false;
  const detail = error.detail as { error?: { code?: string } } | null;
  return detail?.error?.code === 'jurisdiction_unresolved';
}

function ListSection({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return (
    <section className="space-y-1">
      <h4 className="text-sm font-semibold">{title}</h4>
      <ul className="list-disc space-y-1 pl-5 text-sm text-foreground">
        {items.map((item, i) => (
          <li key={`${i}-${item}`} className="break-words">
            {item}
          </li>
        ))}
      </ul>
    </section>
  );
}

function BriefView({ brief, total }: { brief: RecommendationBriefOut; total: number }) {
  return (
    <article aria-label={`Brief version ${brief.brief_version}`} className="space-y-4">
      <div className="flex flex-wrap items-center gap-1.5">
        <Badge intent="info">
          Version {brief.brief_version} of {total}
        </Badge>
        {brief.is_simulated ? <Badge intent="warning">Simulated</Badge> : null}
      </div>
      <section className="space-y-1">
        <h4 className="text-sm font-semibold">Summary</h4>
        <p className="whitespace-pre-wrap break-words text-sm text-foreground">{brief.summary}</p>
      </section>
      <section className="space-y-1">
        <h4 className="text-sm font-semibold">Reasoning</h4>
        <p className="text-xs text-muted-foreground">
          AI-generated interpretation of the evidence, not verified fact.
        </p>
        <p className="whitespace-pre-wrap break-words text-sm text-foreground">{brief.rationale}</p>
      </section>
      <ListSection title="Recommended actions" items={brief.recommended_actions ?? []} />
      <ListSection title="Suggested angles" items={brief.suggested_angles ?? []} />
      <ListSection title="Risks to check" items={brief.risk_notes ?? []} />
      <Separator />
      <dl className="grid grid-cols-1 gap-1 text-xs sm:grid-cols-2">
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">Created by</dt>
          <dd className="break-words text-foreground">{briefAuthorName(brief.created_by_snapshot)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">Created</dt>
          <dd className="text-foreground">{formatDateTime(brief.created_at)}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">Prompt</dt>
          <dd className="break-words text-foreground">
            {brief.prompt_name} {brief.prompt_version}
          </dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">Model</dt>
          <dd className="break-words text-foreground">{brief.model_identifier}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">Evidence items</dt>
          <dd className="tabular-nums text-foreground">{(brief.evidence_refs ?? []).length}</dd>
        </div>
        <div className="flex justify-between gap-2">
          <dt className="text-muted-foreground">Analysis version</dt>
          <dd className="break-words text-foreground">{brief.analysis_version}</dd>
        </div>
      </dl>
    </article>
  );
}

function BriefSkeleton() {
  return (
    <div className="space-y-3" aria-busy="true" aria-live="polite">
      <span className="sr-only">Loading briefs…</span>
      <Skeleton className="h-5 w-40" />
      <Skeleton className="h-16 w-full" />
    </div>
  );
}

function BriefPanelBody({
  workspaceId,
  opportunityId,
}: {
  workspaceId: string;
  opportunityId: string;
}) {
  const { organizationId } = useWorkspace();
  // Capture is editor-only on the server (owner / admin / marketer); everyone else reads.
  const canCreate = canEditWorkspace(useActorRole(organizationId).role);
  const briefs = useOpportunityBriefs({ workspaceId, opportunityId });
  // The same cache entry the intelligence panel reads: no extra request.
  const intelligence = useOpportunityIntelligence({ workspaceId, opportunityId });
  const create = useCreateBrief({ workspaceId, opportunityId });
  // I-5C: the shown version's drafts are one link away while creative generation is enabled.
  const creative = useCreativeCapability(workspaceId);
  // The opportunity the detail page has already loaded: its cache entry, read without a
  // request of its own. Not loaded (unknown) is not "no location": the Q-4 note stays hidden.
  const opportunity = useQuery({
    queryKey: queryKeys.opportunity(workspaceId, opportunityId),
    queryFn: ({ signal }) => api.getOpportunity(workspaceId, opportunityId, signal),
    enabled: false,
  });
  // Q-4: the record a new brief is captured from carries the opportunity's location (both are
  // the scout's), so no location here means the brief's jurisdiction is NULL.
  const noLocationNote = creative.isEnabled && opportunity.data?.location_id === null;
  const [selectedId, setSelectedId] = useState<string | null>(null);

  // Enabled globally but resolved disabled for this workspace: hide, as when dark.
  if (briefs.isError && isBriefsDark(briefs.error)) return null;

  const items = briefs.data?.items ?? [];
  const total = briefs.data?.total ?? 0;
  const shown = items.find((b) => b.id === selectedId) ?? items[0] ?? null;
  const recordId = intelligence.data?.intelligence?.intelligence_record_id ?? null;

  return (
    <Card role="region" aria-labelledby="brief-panel-heading">
      <CardHeader>
        <CardTitle id="brief-panel-heading" className="flex items-center gap-2">
          <FileText className="size-4 text-primary" /> Recommendation brief
        </CardTitle>
        <p className="text-sm text-muted-foreground">
          An AI-drafted recommendation from this opportunity&apos;s evidence. Each brief is saved
          as a new version and never changed afterwards.
        </p>
      </CardHeader>
      <CardContent className="space-y-4">
        {briefs.isLoading ? (
          <BriefSkeleton />
        ) : briefs.isError ? (
          <ErrorState error={briefs.error} onRetry={() => briefs.refetch()} />
        ) : shown ? (
          <>
            <BriefView brief={shown} total={total} />
            {/* Phase 5B (mounted at I-5B): the shown version's decisions. BriefDecisions
                renders nothing for a viewer (the history route refuses that role) and
                nothing while approvals answer dark. */}
            <BriefDecisions
              key={shown.id}
              title="Approval"
              brief={shown}
              target={{
                workspaceId,
                opportunityId,
                briefId: shown.id,
                briefVersion: shown.brief_version,
              }}
            />
            {creative.isEnabled ? (
              <div className="space-y-1.5">
                <Button size="sm" variant="outline" asChild>
                  <Link to={`/creative/${shown.id}?opportunity=${encodeURIComponent(opportunityId)}`}>
                    Open drafts
                  </Link>
                </Button>
                {noLocationNote ? (
                  <p role="note" className="text-xs text-muted-foreground">
                    {NO_LOCATION_NOTE}
                  </p>
                ) : (
                  <p className="text-xs text-muted-foreground">
                    {DRAFT_JURISDICTION_NOTE}
                    {canCreate ? ` ${NEW_VERSION_REMEDY}` : null}
                  </p>
                )}
              </div>
            ) : null}
          </>
        ) : (
          <>
            <p className="text-sm text-muted-foreground">No brief has been generated yet.</p>
            {noLocationNote ? (
              <p role="note" className="text-xs text-muted-foreground">
                {NO_LOCATION_NOTE}
              </p>
            ) : null}
          </>
        )}

        {items.length > 1 ? (
          <nav aria-label="Brief versions" className="space-y-1">
            <h4 className="text-sm font-semibold">Versions</h4>
            <ul className="flex flex-wrap gap-1.5">
              {items.map((b) => (
                <li key={b.id}>
                  <Button
                    size="sm"
                    variant={shown?.id === b.id ? 'default' : 'outline'}
                    aria-pressed={shown?.id === b.id}
                    onClick={() => setSelectedId(b.id)}
                  >
                    Version {b.brief_version}
                  </Button>
                </li>
              ))}
            </ul>
            {total > items.length ? (
              <p className="text-xs text-muted-foreground">
                Showing the {items.length} newest of {total} versions.
              </p>
            ) : null}
          </nav>
        ) : null}

        {canCreate ? (
          <div className="space-y-1">
            <Button
              onClick={() => {
                if (recordId) create.mutate({ intelligence_record_id: recordId });
              }}
              disabled={!recordId || create.isPending}
            >
              <Sparkles className="size-4" />
              {create.isPending
                ? 'Generating…'
                : items.length
                  ? 'Generate a new version'
                  : 'Generate brief'}
            </Button>
            {!recordId && !intelligence.isLoading ? (
              <p className="text-xs text-muted-foreground">
                A brief needs this opportunity&apos;s signal intelligence, which is not available
                yet.
              </p>
            ) : null}
            {create.isError && isJurisdictionUnresolved(create.error) ? (
              <p role="alert" className="text-sm text-destructive">
                {JURISDICTION_UNRESOLVED_MESSAGE}
              </p>
            ) : null}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

/**
 * Recommendation briefs for one opportunity (Project Phase 5A). Renders nothing unless the
 * workspace-effective capability reflection says enabled, and nothing when the list answers
 * the 503 gate (a decision changed since the reflection was read), so a dark workspace shows
 * no trace of the feature and sends no request to the brief endpoints.
 */
export function OpportunityBriefPanel({
  workspaceId,
  opportunityId,
}: {
  workspaceId: string;
  opportunityId: string;
}) {
  const capability = useBriefCapability(workspaceId);
  if (!capability.isEnabled) return null;
  return <BriefPanelBody workspaceId={workspaceId} opportunityId={opportunityId} />;
}
