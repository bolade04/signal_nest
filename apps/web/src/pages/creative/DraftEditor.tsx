import { Sparkles } from 'lucide-react';
import { useId, useState } from 'react';
import type { CreativeChannel, CreativeDraftOut } from '@/api/types';
import { Button } from '@/components/ui/button';
import { CHANNEL_LABELS, CHANNELS } from './creativeLabels';

/**
 * The draft editor: drafts are generated, never typed, so "editing" is choosing a channel and
 * either generating a new draft from the brief or a revision of the selected version (a new
 * version whose parent is that one). No free text is sent: the server builds every model
 * input from stored context. Rendered for editors only (the server's editor gate).
 */
export function DraftEditor({
  selected,
  busy,
  onGenerate,
}: {
  selected: CreativeDraftOut | null;
  busy: boolean;
  onGenerate: (channel: CreativeChannel, parentDraftId: string | null) => void;
}) {
  const selectId = useId();
  const [channel, setChannel] = useState<CreativeChannel>(selected?.channel ?? 'social_post');
  return (
    <div className="flex flex-wrap items-end gap-2" aria-label="Generate a draft" role="group">
      <div className="flex flex-col gap-1">
        <label htmlFor={selectId} className="text-xs font-medium text-muted-foreground">
          Channel
        </label>
        <select
          id={selectId}
          value={channel}
          disabled={busy}
          onChange={(event) => setChannel(event.target.value as CreativeChannel)}
          className="h-9 rounded-md border border-input bg-background px-3 text-sm"
        >
          {CHANNELS.map((value) => (
            <option key={value} value={value}>
              {CHANNEL_LABELS[value]}
            </option>
          ))}
        </select>
      </div>
      <Button type="button" size="sm" disabled={busy} onClick={() => onGenerate(channel, null)}>
        <Sparkles className="size-4" aria-hidden />
        Generate draft
      </Button>
      {selected && (
        <Button
          type="button"
          size="sm"
          variant="outline"
          disabled={busy}
          onClick={() => onGenerate(channel, selected.id)}
        >
          Revise version {selected.draft_version}
        </Button>
      )}
    </div>
  );
}
