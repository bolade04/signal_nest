import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, Download } from 'lucide-react';
import { useState } from 'react';
import { ApiError, apiRequest } from '@/api/client';
import { queryKeys } from '@/api/queryKeys';
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
import { type DraftExport, type ExportFormat, ExportHistory } from './ExportHistory';

/*
 * The export dialog of one approved draft version, and the controls that open it (Project
 * Phase 5E, 5E-W3). The routes, the local types and why they are local are described at the
 * top of `ExportHistory.tsx`. Only components and types are exported (the react-refresh lint
 * rule); the helpers below are private.
 */

const draftExportsPath = (workspaceId: string, draftId: string) =>
  `/workspaces/${workspaceId}/drafts/${draftId}/exports`;

/** The label of each export format (`ExportHistory.tsx` keeps the same map). */
const EXPORT_FORMAT_LABEL: Readonly<Record<ExportFormat, string>> = {
  'text/plain': 'Plain text',
  'text/markdown': 'Markdown',
  json: 'JSON',
};

/** The export capability reflection (`GET /workspaces/{ws}/export-capability`): `{enabled}`. */
interface ExportCapabilityOut {
  enabled: boolean;
}

const getExportCapability = (workspaceId: string, signal?: AbortSignal) =>
  apiRequest<ExportCapabilityOut>(`/workspaces/${workspaceId}/export-capability`, { signal });

const createDraftExport = (workspaceId: string, draftId: string, format: ExportFormat) =>
  apiRequest<DraftExport>(draftExportsPath(workspaceId, draftId), {
    method: 'POST',
    body: { format },
  });

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
    queryFn: ({ signal }) => getExportCapability(workspaceId, signal),
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

function errorCodes(error: ApiError): string[] {
  const body = (error.detail as { error?: { code?: unknown; details?: { reason?: unknown } } } | null)
    ?.error;
  return [body?.code, body?.details?.reason].filter(
    (value): value is string => typeof value === 'string',
  );
}

/**
 * The static message for a refused export (never the server's text).
 *
 * 503: export is off for the workspace. 403: the role may not export (OWNER, ADMIN and MARKETER
 * may). 404: the draft is unknown, another workspace's, or deleted (the uniform answer). 409: a
 * precondition refused (interface sheet P5). The refusal's code and reason are matched against
 * closed families: jurisdiction mismatch (P12), jurisdiction missing or not a code (item 31),
 * the content review (P6: the draft-approval checks), a stale version, and an approval that is
 * not the controlling decision. Anything else gets the generic refusal line.
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
      const codes = errorCodes(error).join(' ');
      if (codes.includes('jurisdiction_mismatch')) {
        return "This draft's jurisdiction doesn't match its brief's, so it can't be exported.";
      }
      if (codes.includes('jurisdiction')) {
        return "This draft or its brief has no recorded jurisdiction, so it can't be exported.";
      }
      if (/review|claim|disclaimer/.test(codes)) {
        return "This draft's content review doesn't allow export, or the claims library changed since it was reviewed.";
      }
      if (codes.includes('version')) {
        return 'This version changed. Reload the drafts and export again.';
      }
      if (codes.includes('approv')) {
        return "This version isn't approved. Only a version whose current decision is an approval can be exported.";
      }
      return "This draft can't be exported in its current state.";
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
      createDraftExport(target.workspaceId, target.draftId, chosen),
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
 * routes re-decide every request and answer 503 while dark, 403 for a role outside their set.
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
  const capability = useExportCapability(workspaceId);
  const [open, setOpen] = useState(false);
  if (!capability.isEnabled) return null;
  return (
    <div className="space-y-2">
      <Button type="button" size="sm" variant="outline" onClick={() => setOpen(true)}>
        <Download className="size-4" /> Export
      </Button>
      <ExportHistory workspaceId={workspaceId} draftId={draftId} />
      {open ? (
        <ExportDialog
          key={draftId}
          target={{ workspaceId, draftId, draftVersion }}
          onClose={() => setOpen(false)}
        />
      ) : null}
    </div>
  );
}
