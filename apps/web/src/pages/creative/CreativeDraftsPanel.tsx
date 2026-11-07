import { Loader2 } from 'lucide-react';
import { useState } from 'react';
import { ApiError } from '@/api/client';
import type { CreativeChannel, CreativeDraftOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { ErrorState } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Skeleton } from '@/components/ui/skeleton';
import { canEditWorkspace } from '@/lib/roles';
import { useStatusPolling } from '@/lib/status-polling';
import { formatRelative } from '@/lib/utils';
import { useBriefDecisions } from '@/pages/approvals/useApprovals';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { BlockedState } from './BlockedState';
import { DraftDecisions } from './DraftDecisions';
import { DraftEditor } from './DraftEditor';
import { ReviewWarningsPanel } from './ReviewWarningsPanel';
import { VersionHistory } from './VersionHistory';
import { CHANNEL_LABELS, generationFailureMessage, isGenerationInFlight } from './creativeLabels';
import { useBriefDrafts, useCreativeCapability, useGenerateDraft, useGenerationJob } from './useCreative';

/**
 * The generated drafts of one recommendation brief (Project Phase 5C, W21 lane P5C): the
 * draft editor (generate / revise, editors only), the version history, the selected
 * version's text, its content review and, when it cannot be approved, why.
 *
 * Dark until the workspace-effective `creative_generation` reflection says enabled: while
 * it is off, unknown or resolving, the panel renders nothing and issues no draft request.
 *
 * Mounted at I-5C on the `/creative/:briefId` page. Generate is offered only when the API
 * would accept it (I-5C): an editor (owner / admin / marketer), every daily ceiling usable
 * (the reflection's `generation_configured`, battery O-5) and the brief's controlling decision
 * an approval (read from the brief's decision history, which every editor may read; it needs
 * the brief's `opportunityId`). Unknown never offers it; the route's 422/503/429 answers stay
 * handled for a stale client. Each version shows its decisions (`DraftDecisions`).
 */
export function CreativeDraftsPanel({
  workspaceId,
  briefId,
  opportunityId = null,
}: {
  workspaceId: string;
  briefId: string;
  opportunityId?: string | null;
}) {
  const capability = useCreativeCapability(workspaceId);
  if (!capability.isEnabled) return null;
  return (
    <DraftsInner
      workspaceId={workspaceId}
      briefId={briefId}
      opportunityId={opportunityId}
      generationConfigured={capability.generationConfigured}
    />
  );
}

function errorCode(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  const detail = error.detail as { error?: { code?: string } } | null;
  return detail?.error?.code ?? null;
}

function requestFailureMessage(error: unknown): string {
  switch (errorCode(error)) {
    case 'brief_not_approved':
      return 'Approve this brief before generating drafts from it.';
    case 'creative_generation_unconfigured':
    case 'capability_unavailable':
      return 'Draft generation is not available right now.';
    case 'llm_quota_exceeded':
      return 'The daily limit for generated drafts has been reached. Try again tomorrow.';
    default:
      return error instanceof ApiError && error.status === 404
        ? 'That draft or brief is no longer available.'
        : 'The draft could not be requested. Try again.';
  }
}

function DraftsInner({
  workspaceId,
  briefId,
  opportunityId,
  generationConfigured,
}: {
  workspaceId: string;
  briefId: string;
  opportunityId: string | null;
  generationConfigured: boolean;
}) {
  const { memberships } = useAuth();
  const { organizationId } = useWorkspace();
  const role = memberships.find((m) => m.organization_id === organizationId)?.role;
  const isEditor = canEditWorkspace(role);
  // The brief's controlling decision (5B: the highest decision_seq). Only an editor needs it,
  // and every editor may read the history.
  const briefHistory = useBriefDecisions({
    workspaceId,
    opportunityId: opportunityId ?? '',
    briefId,
    enabled: isEditor && generationConfigured && Boolean(opportunityId),
  });
  const briefApproved = briefHistory.data?.items[0]?.decision === 'approved';
  const canGenerate = isEditor && generationConfigured && briefApproved;
  const generateUnavailable: string | null = !isEditor
    ? null
    : !generationConfigured
      ? "Generating drafts isn't available in this workspace yet."
      : !opportunityId
        ? 'Open this brief from its opportunity to generate drafts.'
        : briefHistory.isPending
          ? null
          : !briefApproved
            ? 'Approve this brief before generating drafts from it.'
            : null;

  const drafts = useBriefDrafts(workspaceId, briefId, true);
  const generate = useGenerateDraft(workspaceId, briefId);
  const { schedule, stopped } = useStatusPolling();
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useGenerationJob(workspaceId, briefId, jobId, schedule);
  const [selectedId, setSelectedId] = useState<string | null>(null);

  const items = drafts.data?.items ?? [];
  const jobStatus = job.data?.status ?? null;
  // In flight while the job is pending or running and we are still reading its status. Once
  // automatic reads stop (the shared 10-minute bound) or the job cannot be read, the editor is
  // released: the draft appears in the list if the job later succeeds.
  const jobPending =
    jobId !== null && !job.isError && !stopped && (jobStatus === null || isGenerationInFlight(jobStatus));
  const generating = generate.isPending || jobPending;

  // Until the member picks a version, the one the last generation created is shown (else the
  // newest); a new request clears the pick so its result is shown when it lands.
  const createdId =
    jobStatus === 'succeeded'
      ? ((job.data?.result_summary as { draft_id?: string } | null | undefined)?.draft_id ?? null)
      : null;
  const selected: CreativeDraftOut | null =
    items.find((d) => d.id === selectedId) ?? items.find((d) => d.id === createdId) ?? items[0] ?? null;

  function onGenerate(channel: CreativeChannel, parentDraftId: string | null) {
    setSelectedId(null);
    generate.mutate(
      { channel, ...(parentDraftId ? { parent_draft_id: parentDraftId } : {}) },
      { onSuccess: (queued) => setJobId(queued.job_id) },
    );
  }

  if (drafts.isError) {
    if (drafts.error instanceof ApiError && drafts.error.status === 503) return null;
    return (
      <section aria-labelledby="drafts-heading" className="space-y-2">
        <DraftsHeading />
        <ErrorState error={drafts.error} onRetry={() => drafts.refetch()} />
      </section>
    );
  }

  const jobFailed = jobId !== null && jobStatus !== null && !isGenerationInFlight(jobStatus) && jobStatus !== 'succeeded';

  return (
    <section aria-labelledby="drafts-heading" className="space-y-4">
      <DraftsHeading />

      {generateUnavailable && (
        <p role="note" className="text-sm text-muted-foreground">
          {generateUnavailable}
        </p>
      )}

      {canGenerate && (
        <div className="space-y-2">
          <DraftEditor selected={selected} busy={generating} onGenerate={onGenerate} />
          {generating && (
            <p role="status" className="flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" aria-hidden />
              Generating a draft…
            </p>
          )}
          {jobId !== null && !generating && (stopped || job.isError) && jobStatus !== 'succeeded' && (
            <p role="status" className="text-sm text-muted-foreground">
              Automatic status updates stopped. The draft appears below once it is ready.
            </p>
          )}
          {generate.isError && (
            <p role="alert" className="text-sm text-destructive">
              {requestFailureMessage(generate.error)}
            </p>
          )}
          {jobFailed && (
            <p role="alert" className="text-sm text-destructive">
              {generationFailureMessage(job.data?.last_error_code ?? null)}
            </p>
          )}
        </div>
      )}

      {drafts.isLoading ? (
        <div aria-busy="true">
          <span className="sr-only">Loading drafts…</span>
          <Skeleton className="h-24 w-full" />
        </div>
      ) : items.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No drafts yet.{canGenerate ? ' Generate one from this brief.' : ''}
        </p>
      ) : (
        <div className="grid gap-4 md:grid-cols-[minmax(0,14rem)_1fr]">
          <VersionHistory drafts={items} selectedId={selected?.id ?? null} onSelect={setSelectedId} />
          {selected && <DraftView workspaceId={workspaceId} draft={selected} />}
        </div>
      )}
    </section>
  );
}

function DraftsHeading() {
  return (
    <h3 id="drafts-heading" className="text-base font-semibold">
      Drafts
    </h3>
  );
}

function DraftView({ workspaceId, draft }: { workspaceId: string; draft: CreativeDraftOut }) {
  return (
    <article aria-label={`Draft version ${draft.draft_version}`} className="space-y-3">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted-foreground">
        <span>
          Version {draft.draft_version} · {CHANNEL_LABELS[draft.channel]} · generated{' '}
          {formatRelative(draft.created_at)}
        </span>
        {draft.is_simulated && <Badge intent="muted">Simulated</Badge>}
      </div>
      <BlockedState review={draft.review} />
      <div className="space-y-2 rounded-md border p-3">
        {draft.headline && <p className="font-semibold">{draft.headline}</p>}
        <p className="whitespace-pre-line text-sm">{draft.body}</p>
        {draft.call_to_action && <p className="text-sm font-medium">{draft.call_to_action}</p>}
      </div>
      {draft.review ? (
        <ReviewWarningsPanel review={draft.review} />
      ) : (
        <p className="text-sm text-muted-foreground">The content review is not available yet.</p>
      )}
      {/* I-5C: this version's decisions (every role but viewer); the reviewer's controls. */}
      <DraftDecisions key={draft.id} workspaceId={workspaceId} draft={draft} />
    </article>
  );
}
