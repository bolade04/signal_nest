import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useState } from 'react';
import * as api from '@/api/endpoints';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/api/queryKeys';
import type { ContextRow, ScoutRequestCreate } from '@/api/types';
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
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { TagInput } from '@/components/ui/tag-input';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { sourceTypeLabels } from '@/lib/labels';
import { useWorkspace } from '@/workspace/WorkspaceContext';

const NONE = '__none__';

export function ScoutRequestDialog({
  workspaceId,
  open,
  onOpenChange,
}: {
  workspaceId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { locations, locationId } = useWorkspace();
  const queryClient = useQueryClient();
  const { toast } = useToast();

  const [form, setForm] = useState<ScoutRequestCreate>({
    name: '',
    location_id: locationId ?? null,
    campaign_id: null,
    source_types: ['manual', 'website_scan', 'rss_news'],
    keywords: [],
    product_profile_id: null,
    resolved_market: null,
    notes: null,
  });
  const [error, setError] = useState<string | null>(null);

  // Clear the error and align the form's location with the active workspace
  // location each time the dialog opens. Adjusted during render (guarded by the
  // previous open state) instead of in a setState effect.
  const [prevOpen, setPrevOpen] = useState(open);
  if (open !== prevOpen) {
    setPrevOpen(open);
    if (open) {
      setError(null);
      setForm((f) => ({ ...f, location_id: locationId ?? null }));
    }
  }

  const campaignsQuery = useQuery({
    queryKey: queryKeys.context(workspaceId, 'campaigns'),
    queryFn: ({ signal }) => api.listContext(workspaceId, 'campaigns', signal),
    enabled: open,
  });
  const productsQuery = useQuery({
    queryKey: queryKeys.context(workspaceId, 'products'),
    queryFn: ({ signal }) => api.listContext(workspaceId, 'products', signal),
    enabled: open,
  });

  const rowName = (row: ContextRow) =>
    (typeof row.name === 'string' && row.name) || (typeof row.label === 'string' && row.label) || 'Untitled';

  const create = useMutation({
    mutationFn: () => api.createScoutRequest(workspaceId, form),
    onSuccess: async (created) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.scoutRequests(workspaceId) });
      toast({ title: 'Scout request created', intent: 'success' });
      onOpenChange(false);
      setForm((f) => ({ ...f, name: '', keywords: [] }));
      void created;
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not create scout request.'),
  });

  const toggleSource = (value: string, checked: boolean) => {
    setForm((f) => {
      const set = new Set(f.source_types ?? []);
      if (checked) set.add(value);
      else set.delete(value);
      return { ...f, source_types: [...set] };
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>New scout request</DialogTitle>
          <DialogDescription>
            Scouts are isolated by workspace, brand, location and campaign. Results never mix across
            markets unless you explicitly combine them.
          </DialogDescription>
        </DialogHeader>

        {error ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2">
          <Field label="Name" required className="sm:col-span-2">
            {({ id }) => (
              <Input id={id} value={form.name} onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))} placeholder="Dallas — spring demand scan" />
            )}
          </Field>

          <Field label="Location" description="Which market this scout covers.">
            {({ id }) => (
              <Select
                value={form.location_id ?? NONE}
                onValueChange={(v) => setForm((f) => ({ ...f, location_id: v === NONE ? null : v }))}
              >
                <SelectTrigger id={id}>
                  <SelectValue placeholder="Workspace-wide" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>Workspace-wide</SelectItem>
                  {locations.map((loc) => (
                    <SelectItem key={loc.id} value={loc.id}>
                      {loc.name}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </Field>

          <Field label="Resolved market" description="Optional override for the target market label.">
            {({ id }) => (
              <Input id={id} value={form.resolved_market ?? ''} onChange={(e) => setForm((f) => ({ ...f, resolved_market: e.target.value || null }))} placeholder="Dallas, TX" />
            )}
          </Field>

          <Field label="Campaign context">
            {({ id }) => (
              <Select
                value={form.campaign_id ?? NONE}
                onValueChange={(v) => setForm((f) => ({ ...f, campaign_id: v === NONE ? null : v }))}
              >
                <SelectTrigger id={id}>
                  <SelectValue placeholder="None" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>None</SelectItem>
                  {(campaignsQuery.data ?? []).map((row) => (
                    <SelectItem key={row.id} value={row.id}>
                      {rowName(row)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </Field>

          <Field label="Product / service">
            {({ id }) => (
              <Select
                value={form.product_profile_id ?? NONE}
                onValueChange={(v) => setForm((f) => ({ ...f, product_profile_id: v === NONE ? null : v }))}
              >
                <SelectTrigger id={id}>
                  <SelectValue placeholder="None" />
                </SelectTrigger>
                <SelectContent>
                  <SelectItem value={NONE}>None</SelectItem>
                  {(productsQuery.data ?? []).map((row) => (
                    <SelectItem key={row.id} value={row.id}>
                      {rowName(row)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </Field>

          <Field label="Keywords & topics" className="sm:col-span-2" description="What the scout should look for.">
            {({ id }) => (
              <TagInput id={id} value={form.keywords ?? []} onChange={(v) => setForm((f) => ({ ...f, keywords: v }))} placeholder="Add a keyword" />
            )}
          </Field>

          <div className="sm:col-span-2">
            <p className="mb-2 text-sm font-medium">Sources</p>
            <p className="mb-2 text-xs text-muted-foreground">
              Fixture connectors are used in this build and are clearly labeled “simulated”.
            </p>
            <div className="grid grid-cols-1 gap-2 sm:grid-cols-2">
              {Object.entries(sourceTypeLabels).map(([value, lbl]) => {
                const checked = (form.source_types ?? []).includes(value);
                return (
                  <label key={value} className="flex items-center gap-2 rounded-md border border-border px-3 py-2 text-sm">
                    <Checkbox checked={checked} onCheckedChange={(c) => toggleSource(value, Boolean(c))} aria-label={lbl} />
                    {lbl}
                  </label>
                );
              })}
            </div>
          </div>

          <Field label="Notes / instructions" className="sm:col-span-2">
            {({ id }) => (
              <Textarea id={id} value={form.notes ?? ''} onChange={(e) => setForm((f) => ({ ...f, notes: e.target.value || null }))} />
            )}
          </Field>
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button onClick={() => create.mutate()} disabled={create.isPending || !form.name.trim()}>
            Create scout request
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
