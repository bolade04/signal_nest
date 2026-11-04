import { FileText, Sparkles } from 'lucide-react';
import { useState } from 'react';
import type { RecommendationBriefOut } from '@/api/types';
import { ErrorState } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { canEditWorkspace, useActorRole } from '@/lib/roles';
import { BriefDecisions } from '@/pages/approvals/BriefDecisions';
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
          </>
        ) : (
          <p className="text-sm text-muted-foreground">No brief has been generated yet.</p>
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
