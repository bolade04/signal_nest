import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import * as api from '@/api/endpoints';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/api/queryKeys';
import type { ScoutRequestOut } from '@/api/types';
import { Field } from '@/components/common/form-field';
import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Input } from '@/components/ui/input';
import { TagInput } from '@/components/ui/tag-input';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { sourceTypeLabels } from '@/lib/labels';
import { changedFields, formOf, type ScoutEditForm } from '@/lib/scout-edit';

/**
 * Edit an existing Scout (W16 customer repairs §B, PROPOSED). Pre-filled; only changed fields
 * are sent, so untouched settings are kept. The rules mirror the API's (a non-blank name of at
 * most 200 characters, the listed source types, cleaned keywords); the API stays the authority
 * and its refusal (for example `scout_in_progress` after a race) is shown in the dialog.
 * Location, campaign and product profile cannot be changed here.
 */
export function ScoutEditDialog({
  workspaceId,
  request,
  open,
  onOpenChange,
  locationName,
  returnFocusTo,
}: {
  workspaceId: string;
  request: ScoutRequestOut;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  locationName?: string;
  /** Where keyboard focus goes when the dialog closes (the Edit button), so it never lands on <body>. */
  returnFocusTo?: React.RefObject<HTMLElement | null>;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [form, setForm] = useState<ScoutEditForm>(() => formOf(request));
  const [error, setError] = useState<string | null>(null);
  const [prevOpen, setPrevOpen] = useState(open);
  if (open !== prevOpen) {
    setPrevOpen(open);
    if (open) {
      setError(null);
      setForm(formOf(request));
    }
  }

  const name = form.name.trim();
  const nameError = !name ? 'A name is required.' : name.length > 200 ? 'At most 200 characters.' : null;
  const changes = changedFields(request, form);
  const nothingChanged = Object.keys(changes).length === 0;

  const save = useMutation({
    mutationFn: () => api.updateScoutRequest(workspaceId, request.id, changes),
    onSuccess: async () => {
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.scoutRequests(workspaceId) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.scoutRequest(workspaceId, request.id) }),
      ]);
      toast({ title: 'Scout updated', intent: 'success' });
      onOpenChange(false);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not update the scout.'),
  });

  const toggleSource = (value: string, checked: boolean) =>
    setForm((f) => {
      const set = new Set(f.source_types);
      if (checked) set.add(value);
      else set.delete(value);
      return { ...f, source_types: Object.keys(sourceTypeLabels).filter((k) => set.has(k)) };
    });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-2xl"
        onCloseAutoFocus={(event) => {
          const target = returnFocusTo?.current;
          if (target) {
            event.preventDefault();
            target.focus();
          }
        }}
      >
        <DialogHeader>
          <DialogTitle>Edit scout</DialogTitle>
          <DialogDescription>
            Changes apply to future runs. Existing findings keep their market and content.
          </DialogDescription>
        </DialogHeader>

        {error ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" required className="sm:col-span-2" error={nameError ?? undefined}>
            {({ id, describedBy, invalid }) => (
              <Input
                id={id}
                value={form.name}
                maxLength={200}
                aria-invalid={invalid || undefined}
                aria-describedby={describedBy}
                onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              />
            )}
          </Field>

          <Field label="Resolved market" description="Optional override for the target market label. Leave empty to use the location's market.">
            {({ id, describedBy }) => (
              <Input
                id={id}
                value={form.resolved_market}
                maxLength={200}
                aria-describedby={describedBy}
                onChange={(e) => setForm((f) => ({ ...f, resolved_market: e.target.value }))}
              />
            )}
          </Field>

          <div className="space-y-1.5 text-sm">
            <p className="font-medium">Location, campaign and product</p>
            <p className="text-muted-foreground">
              {locationName ?? 'Workspace-wide'}. These are set when a scout is created and cannot be changed here; create a new scout to change them.
            </p>
          </div>

          <Field label="Keywords & topics" className="sm:col-span-2" description="What the scout should look for.">
            {({ id }) => (
              <TagInput id={id} value={form.keywords} onChange={(v) => setForm((f) => ({ ...f, keywords: v }))} placeholder="Add a keyword" />
            )}
          </Field>

          <fieldset className="sm:col-span-2">
            <legend className="mb-2 text-sm font-medium">Sources</legend>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {Object.entries(sourceTypeLabels).map(([value, lbl]) => (
                <label key={value} className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm">
                  <Checkbox
                    checked={form.source_types.includes(value)}
                    onCheckedChange={(c) => toggleSource(value, Boolean(c))}
                    aria-label={lbl}
                  />
                  {lbl}
                </label>
              ))}
            </div>
          </fieldset>

          <Field label="Notes / instructions" className="sm:col-span-2">
            {({ id }) => (
              <Textarea id={id} value={form.notes} onChange={(e) => setForm((f) => ({ ...f, notes: e.target.value }))} />
            )}
          </Field>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
            Cancel
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending || Boolean(nameError) || nothingChanged}>
            Save changes
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
