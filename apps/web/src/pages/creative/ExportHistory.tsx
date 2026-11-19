import { useQuery } from '@tanstack/react-query';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { ExportFormat } from '@/api/types';
import { exportFormatLabels } from '@/lib/labels';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { formatRelative } from '@/lib/utils';
import { snapshotName } from '@/pages/approvals/approvalVocabulary';

/*
 * The audited manual export of an approved draft (Project Phase 5E): the export history
 * (this file) and the export dialog (`ExportDialog.tsx`), lane 5E-W3. Exactly these two files
 * are 5E-W3's in this directory (5E plan §5).
 *
 * The routes are lane 5E-W1's (interface sheet P4):
 *   POST /workspaces/{ws}/drafts/{draft}/exports   201, the export with its rendered `content`
 *   GET  /workspaces/{ws}/drafts/{draft}/exports   200, the history (items without content)
 *   GET  /workspaces/{ws}/export-capability        200, the reflection (also while dark)
 * Both export routes answer 503 while `content_export` is off for the workspace, 403 for a
 * role outside the route's set, and the uniform 404 for an unknown or foreign draft.
 *
 * The types are the generated contract's (`@/api/types`: `ExportFormat`, `DraftExport`,
 * `DraftExportSummary`, `DraftExportHistory`) and the requests are `@/api/endpoints`'s
 * wrappers, both adopted at I-5E in place of the lane's local declarations.
 *
 * Each file exports only components (the react-refresh lint rule), so the two keep their few
 * small helpers (the format labels) privately.
 */

/** The label of each export format (`lib/labels.ts`, I-5E). */
const EXPORT_FORMAT_LABEL: Readonly<Record<ExportFormat, string>> = exportFormatLabels;

/** 4xx answers and the 503 gate are deterministic: never retried. */
function deterministic(error: unknown): boolean {
  return (
    error instanceof ApiError && (error.status === 503 || (error.status >= 400 && error.status < 500))
  );
}

/** A draft's export history, keyed by workspace and draft (`queryKeys.draftExports`). */
function useDraftExports(workspaceId: string, draftId: string, enabled: boolean) {
  return useQuery({
    queryKey: queryKeys.draftExports(workspaceId, draftId),
    queryFn: ({ signal }) => api.listDraftExports(workspaceId, draftId, {}, signal),
    enabled: enabled && Boolean(workspaceId && draftId),
    retry: (failureCount, error) => !deterministic(error) && failureCount < 2,
  });
}

/** The static line for a refused history read (never the server's text). */
function exportHistoryRefusal(error: unknown): string | null {
  if (!(error instanceof ApiError)) return null;
  switch (error.status) {
    case 503:
      return "Export isn't available in this workspace.";
    case 403:
      return "Your role can't see this draft's export history.";
    case 404:
      return "This draft isn't available in this workspace.";
    default:
      return null;
  }
}

/**
 * The export history of one draft, newest first: who exported which version, in which format,
 * when, and under which jurisdiction. Readable by OWNER, ADMIN, MARKETER, REVIEWER and
 * COMPLIANCE_REVIEWER (item 32); the API decides, and a refusal is shown as a static line.
 */
export function ExportHistory({
  workspaceId,
  draftId,
  enabled = true,
}: {
  workspaceId: string;
  draftId: string;
  /** False sends no request and renders nothing (the mount's export reflection is off). */
  enabled?: boolean;
}) {
  const history = useDraftExports(workspaceId, draftId, enabled);
  if (!enabled) return null;

  if (history.isPending) {
    return (
      <div aria-busy="true" aria-live="polite">
        <span className="sr-only">Loading export history…</span>
        <Skeleton className="h-6 w-48" />
      </div>
    );
  }
  if (history.isError) {
    const refusal = exportHistoryRefusal(history.error);
    return (
      <section aria-label="Export history" className="space-y-2">
        <h4 className="text-sm font-semibold">Exports</h4>
        {refusal ? (
          <p role="status" className="text-xs text-muted-foreground">
            {refusal}
          </p>
        ) : (
          <div role="alert" className="flex flex-wrap items-center gap-2 text-xs">
            <span className="text-muted-foreground">The export history could not be loaded.</span>
            <Button variant="outline" size="sm" onClick={() => void history.refetch()}>
              Try again
            </Button>
          </div>
        )}
      </section>
    );
  }

  const items = history.data.items;
  return (
    <section aria-label="Export history" className="space-y-2">
      <h4 className="text-sm font-semibold">Exports</h4>
      {items.length === 0 ? (
        <p className="text-xs text-muted-foreground">This draft hasn&apos;t been exported yet.</p>
      ) : (
        <ol aria-label="Draft export history" className="space-y-1.5">
          {items.map((item) => (
            <li key={item.id} className="flex flex-wrap items-center gap-2 text-xs">
              <Badge intent="outline">{EXPORT_FORMAT_LABEL[item.format] ?? item.format}</Badge>
              <span>Version {item.draft_version}</span>
              <span className="text-muted-foreground">
                by {snapshotName(item.exported_by_snapshot)} · {formatRelative(item.created_at)}
              </span>
              <span className="text-muted-foreground">Jurisdiction {item.jurisdiction_code}</span>
              {item.is_simulated ? <Badge intent="muted">Simulated</Badge> : null}
            </li>
          ))}
        </ol>
      )}
    </section>
  );
}
