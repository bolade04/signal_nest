import type { CreativeDraftOut } from '@/api/types';
import { Badge } from '@/components/ui/badge';
import { cn } from '@/lib/utils';
import { CHANNEL_LABELS, REVIEW_STATE_LABELS, reviewState } from './creativeLabels';

/**
 * A brief's draft versions, newest first (the order the API returns). Each version is
 * immutable; selecting one shows it. A revision names the version it revised.
 */
export function VersionHistory({
  drafts,
  selectedId,
  onSelect,
}: {
  drafts: CreativeDraftOut[];
  selectedId: string | null;
  onSelect: (draftId: string) => void;
}) {
  const versionOf = new Map(drafts.map((d) => [d.id, d.draft_version]));
  return (
    <nav aria-label="Draft versions">
      <ol className="space-y-1">
        {drafts.map((draft) => {
          const state = REVIEW_STATE_LABELS[reviewState(draft.review)];
          const selected = draft.id === selectedId;
          const parentVersion = draft.parent_draft_id ? versionOf.get(draft.parent_draft_id) : undefined;
          return (
            <li key={draft.id}>
              <button
                type="button"
                aria-current={selected ? 'true' : undefined}
                onClick={() => onSelect(draft.id)}
                className={cn(
                  'flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-left text-sm',
                  selected ? 'border-primary bg-primary/5' : 'hover:bg-muted',
                )}
              >
                <span>
                  <span className="font-medium">Version {draft.draft_version}</span>{' '}
                  <span className="text-muted-foreground">
                    · {CHANNEL_LABELS[draft.channel]}
                    {parentVersion !== undefined ? ` · revises version ${parentVersion}` : ''}
                  </span>
                </span>
                <Badge intent={state.intent}>{state.label}</Badge>
              </button>
            </li>
          );
        })}
      </ol>
    </nav>
  );
}
