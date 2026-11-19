import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, Download } from 'lucide-react';
import { useState } from 'react';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { ExportFormat } from '@/api/types';
import { exportFormatLabels } from '@/lib/labels';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { canExportDrafts, canReadExportHistory, useActorRole } from '@/lib/roles';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { ExportHistory } from './ExportHistory';

/*
 * The export dialog of one approved draft version, and the controls that open it (Project
 * Phase 5E, 5E-W3). The routes, the local types and why they are local are described at the
 * top of `ExportHistory.tsx`. Only components and types are exported (the react-refresh lint
 * rule); the helpers below are private.
 */

/** The label of each export format (`lib/labels.ts`, I-5E). */
const EXPORT_FORMAT_LABEL: Readonly<Record<ExportFormat, string>> = exportFormatLabels;

type ExportCapabilityStatus = 'loading' | 'enabled' | 'disabled';

/**
 * Workspace-effective export availability (the creative-capability pattern): a tri-state,
 * `=== true` only, never retried. Advisory: both export routes re-decide and answer 503 while
 * `content_export` is off. The export UI is mounted only while this is `enabled`.
 */
function useExportCapability(
  workspaceId: string,
  { enabled = true }: { enabled?: boolean } = {},
) {
  const query = useQuery({
    queryKey: queryKeys.exportCapability(workspaceId),
    queryFn: ({ signal }) => api.getExportCapability(workspaceId, signal),
    enabled: enabled && Boolean(workspaceId),
    staleTime: 60_000,
    retry: false,
  });
  const status: ExportCapabilityStatus = query.isPending
    ? 'loading'
    : query.data?.enabled === true
      ? 'enabled'
      : 'disabled';
  return { status, isEnabled: status === 'enabled' };
}

/** The formats in the order the dialog offers them. */
const FORMATS: readonly ExportFormat[] = ['text/plain', 'text/markdown', 'json'];

const FILE_TYPE: Readonly<Record<ExportFormat, { extension: string; mime: string }>> = {
  'text/plain': { extension: 'txt', mime: 'text/plain;charset=utf-8' },
  'text/markdown': { extension: 'md', mime: 'text/markdown;charset=utf-8' },
  json: { extension: 'json', mime: 'application/json;charset=utf-8' },
};

/** The refusal code of a 409 answer (`error.code`), or null. */
function refusalCode(error: ApiError): string | null {
  const code = (error.detail as { error?: { code?: unknown } } | null)?.error?.code;
  return typeof code === 'string' ? code : null;
}

/**
 * The static line for each 409 refusal code 5E-W1's export service answers
 * (`apps/api/app/exports/service.py`; interface sheet P5, P6, P12), never the server's text.
 * The order of the checks is the service's: jurisdiction, then mismatch, then the decision,
 * then the review.
 */
const EXPORT_REFUSAL: Readonly<Record<string, string>> = {
  export_jurisdiction_missing:
    "This draft or its brief has no recorded jurisdiction, so it can't be exported.",
  jurisdiction_mismatch:
    "This draft's jurisdiction doesn't match its brief's, so it can't be exported.",
  export_not_approved:
    "This version isn't approved. Only a version whose current decision is an approval can be exported.",
  // Every draft_approval_refusal reason shares this code, D-N4 item 38's brief_not_approved
  // included (the draft-approval sentence names the brief the same way since DN4-38).
  export_review_blocked:
    "This draft can't be exported: its content review doesn't allow it, the claims library changed since it was reviewed, or its brief is no longer approved.",
};

/**
 * The static message for a refused export (never the server's text).
 *
 * 503: export is off for the workspace. 403: the role may not export (OWNER, ADMIN and MARKETER
 * may). 404: the draft is unknown, another workspace's, or deleted (the uniform answer). 409: a
 * precondition refused, by its exact code (`EXPORT_REFUSAL`); an unknown code gets the generic
 * refusal line.
 */
function exportFailureMessage(error: unknown): string {
  if (!(error instanceof ApiError)) return 'The export could not be created. Try again.';
  switch (error.status) {
    case 503:
      return "Export isn't available in this workspace.";
    case 403:
      return "Your role can't export drafts.";
    case 404:
      return "This draft isn't available in this workspace.";
    case 409: {
      const code = refusalCode(error);
      return (code !== null && Object.prototype.hasOwnProperty.call(EXPORT_REFUSAL, code)
        ? EXPORT_REFUSAL[code]
        : null) ?? "This draft can't be exported in its current state.";
    }
    default:
      return 'The export could not be created. Try again.';
  }
}

export interface ExportTarget {
  workspaceId: string;
  draftId: string;
  /** The version on screen. The server exports the draft's own version; this is for display. */
  draftVersion: number;
}

/** Hand `content` to the browser as a downloaded file. */
function saveFile(content: string, format: ExportFormat, draftVersion: number): void {
  const { extension, mime } = FILE_TYPE[format];
  const url = URL.createObjectURL(new Blob([content], { type: mime }));
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = `signalnest-draft-v${draftVersion}-export.${extension}`;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(url);
  }
}

/**
 * Export one approved draft version: choose a format, export, then copy or save the rendered
 * text. Each export is an audited record (the export history lists it). Mounted only while open
 * (keyed by the caller), so a result never carries over to another draft. Never retried: an
 * audited write must not be duplicated.
 */
export function ExportDialog({ target, onClose }: { target: ExportTarget; onClose: () => void }) {
  const queryClient = useQueryClient();
  const [format, setFormat] = useState<ExportFormat>('text/plain');
  const [copied, setCopied] = useState<'idle' | 'copied' | 'failed'>('idle');
  const create = useMutation({
    mutationFn: (chosen: ExportFormat) =>
      api.createDraftExport(target.workspaceId, target.draftId, chosen),
    retry: false,
    onSuccess: async () => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.draftExports(target.workspaceId, target.draftId),
      });
    },
  });
  const result = create.data ?? null;
  const idPrefix = `draft-export-${target.draftId}`;

  async function copy(content: string) {
    try {
      if (!navigator.clipboard) throw new Error('no clipboard');
      await navigator.clipboard.writeText(content);
      setCopied('copied');
    } catch {
      setCopied('failed');
    }
  }

  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="max-w-lg">
        <DialogHeader>
          <DialogTitle>Export draft version {target.draftVersion}</DialogTitle>
          <DialogDescription>
            Export the approved text to copy or save. Nothing is published or sent anywhere. Each
            export is recorded with your name in the draft&apos;s export history.
          </DialogDescription>
        </DialogHeader>

        {result ? (
          <div className="space-y-2">
            <Label htmlFor={`${idPrefix}-content`}>
              {EXPORT_FORMAT_LABEL[result.format] ?? result.format} export of version{' '}
              {result.draft_version}
            </Label>
            <Textarea
              id={`${idPrefix}-content`}
              value={result.content}
              readOnly
              rows={10}
              className="font-mono text-xs"
            />
            <div className="flex flex-wrap items-center gap-2">
              <Button type="button" size="sm" variant="outline" onClick={() => void copy(result.content)}>
                {copied === 'copied' ? <Check className="size-4" /> : <Copy className="size-4" />}
                {copied === 'copied' ? 'Copied' : 'Copy'}
              </Button>
              <Button
                type="button"
                size="sm"
                variant="outline"
                onClick={() => saveFile(result.content, result.format, result.draft_version)}
              >
                <Download className="size-4" /> Save as file
              </Button>
              {result.is_simulated ? (
                <span className="text-xs text-muted-foreground">
                  Generated by the simulated model.
                </span>
              ) : null}
            </div>
            {copied === 'failed' ? (
              <p role="alert" className="text-xs text-destructive">
                The text could not be copied. Select it and copy it yourself.
              </p>
            ) : null}
          </div>
        ) : (
          <div className="space-y-2">
            <p className="text-sm font-medium" id={`${idPrefix}-format`}>
              Format
            </p>
            <div
              role="group"
              aria-labelledby={`${idPrefix}-format`}
              className="flex flex-wrap gap-2"
            >
              {FORMATS.map((option) => {
                const active = format === option;
                return (
                  <Button
                    key={option}
                    type="button"
                    size="sm"
                    variant={active ? 'default' : 'outline'}
                    aria-pressed={active}
                    onClick={() => setFormat(option)}
                    disabled={create.isPending}
                  >
                    {EXPORT_FORMAT_LABEL[option]}
                  </Button>
                );
              })}
            </div>
            {create.isError ? (
              <p role="alert" className="text-sm text-destructive">
                {exportFailureMessage(create.error)}
              </p>
            ) : null}
          </div>
        )}

        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            {result ? 'Done' : 'Cancel'}
          </Button>
          {result ? null : (
            <Button onClick={() => create.mutate(format)} disabled={create.isPending}>
              {create.isPending ? 'Exporting…' : 'Export'}
            </Button>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * The export controls of one draft version: an Export button that opens the dialog, and the
 * version's export history. This is what `CreativeDraftsPanel.tsx` mounts for an approved draft
 * (interface sheet P3; the mount is 5E-W3's phase B).
 *
 * Gated by the workspace-effective export reflection: while it is not `enabled === true`
 * (off, loading, failed) this renders nothing and sends no export or history request. The
 * Export button is offered only to the exporting roles (`canExportDrafts`) and the history
 * only to its readers (`canReadExportHistory`, item 32), from the member's role in the active
 * organization; an unknown role is offered neither. UX only: the routes re-decide every
 * request and answer 503 while dark, 403 for a role outside their set.
 */
export function DraftExportControls({
  workspaceId,
  draftId,
  draftVersion,
}: {
  workspaceId: string;
  draftId: string;
  draftVersion: number;
}) {
  const { organizationId } = useWorkspace();
  const actor = useActorRole(organizationId);
  const exporter = actor.status === 'known' && canExportDrafts(actor.role);
  const historyReader = actor.status === 'known' && canReadExportHistory(actor.role);
  const capability = useExportCapability(workspaceId, { enabled: exporter || historyReader });
  const [open, setOpen] = useState(false);
  if (!capability.isEnabled || !(exporter || historyReader)) return null;
  return (
    <div className="space-y-2">
      {exporter ? (
        <Button type="button" size="sm" variant="outline" onClick={() => setOpen(true)}>
          <Download className="size-4" /> Export
        </Button>
      ) : null}
      {historyReader ? <ExportHistory workspaceId={workspaceId} draftId={draftId} /> : null}
      {open && exporter ? (
        <ExportDialog
          key={draftId}
          target={{ workspaceId, draftId, draftVersion }}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </div>
  );
}
