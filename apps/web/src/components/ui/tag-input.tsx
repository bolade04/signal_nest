import { X } from 'lucide-react';
import { useState } from 'react';
import { cn } from '@/lib/utils';
import { Badge } from './badge';

interface TagInputProps {
  id?: string;
  value: string[];
  onChange: (next: string[]) => void;
  placeholder?: string;
  'aria-describedby'?: string;
  'aria-invalid'?: boolean;
  disabled?: boolean;
}

// Comma/Enter-delimited free-text list editor used for keyword/topic-style fields.
export function TagInput({
  id,
  value,
  onChange,
  placeholder = 'Type and press Enter',
  disabled,
  ...aria
}: TagInputProps) {
  const [draft, setDraft] = useState('');

  const commit = (raw: string) => {
    const parts = raw
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    if (!parts.length) return;
    const next = [...value];
    for (const p of parts) if (!next.includes(p)) next.push(p);
    onChange(next);
    setDraft('');
  };

  const remove = (tag: string) => onChange(value.filter((t) => t !== tag));

  return (
    <div
      className={cn(
        'flex min-h-9 w-full flex-wrap items-center gap-1.5 rounded-md border border-input bg-background px-2 py-1.5 text-sm shadow-sm focus-within:ring-2 focus-within:ring-ring',
        disabled && 'opacity-50',
      )}
    >
      {value.map((tag) => (
        <Badge key={tag} intent="neutral" className="gap-1 py-1">
          {tag}
          {!disabled && (
            <button
              type="button"
              onClick={() => remove(tag)}
              className="rounded-full hover:text-destructive"
              aria-label={`Remove ${tag}`}
            >
              <X className="size-3" />
            </button>
          )}
        </Badge>
      ))}
      <input
        id={id}
        value={draft}
        disabled={disabled}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter' || e.key === ',') {
            e.preventDefault();
            commit(draft);
          } else if (e.key === 'Backspace' && !draft && value.length) {
            remove(value[value.length - 1]!);
          }
        }}
        onBlur={() => commit(draft)}
        placeholder={value.length ? '' : placeholder}
        className="min-w-[8ch] flex-1 bg-transparent px-1 py-0.5 outline-none placeholder:text-muted-foreground"
        {...aria}
      />
    </div>
  );
}
