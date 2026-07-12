import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Boxes, Plus, Trash2 } from 'lucide-react';
import { useMemo, useState } from 'react';
import * as api from '@/api/endpoints';
import type { ContextInput, ContextKind } from '@/api/endpoints';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/api/queryKeys';
import type { ContextRow } from '@/api/types';
import { ConfirmDialog } from '@/components/common/confirm-dialog';
import { Field } from '@/components/common/form-field';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { PageHeader } from '@/components/layout/page-header';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Button } from '@/components/ui/button';
import { Card, CardContent } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
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
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { TagInput } from '@/components/ui/tag-input';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { sourceTypeLabels } from '@/lib/labels';
import { titleCase } from '@/lib/utils';

// ---- Field + kind descriptors -------------------------------------------------

type FieldType = 'text' | 'textarea' | 'tags' | 'number' | 'boolean' | 'select';

interface FieldDef {
  key: string;
  label: string;
  type: FieldType;
  required?: boolean;
  placeholder?: string;
  description?: string;
  options?: Record<string, string>;
  defaultValue?: unknown;
  colSpan?: boolean;
}

interface KindConfig {
  kind: ContextKind;
  tab: string;
  singular: string;
  blurb: string;
  fields: FieldDef[];
  title: (row: ContextRow) => string;
  subtitle?: (row: ContextRow) => string | null;
}

const str = (v: unknown): string | null =>
  typeof v === 'string' && v.trim() ? v : null;
const arr = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);

const claimKindOptions = { approved: 'Approved', restricted: 'Restricted', prohibited: 'Prohibited' };
const riskOptions = { low: 'Low', medium: 'Medium', high: 'High' };
const campaignModeOptions = {
  same_for_all: 'Same for all locations',
  per_location: 'Independent per location',
  grouped: 'Grouped sets',
  recommend: 'Recommend',
};

const KINDS: KindConfig[] = [
  {
    kind: 'products',
    tab: 'Products & services',
    singular: 'product',
    blurb: 'What you sell. Feeds relevance scoring so opportunities match your actual offerings.',
    title: (r) => str(r.name) ?? 'Untitled product',
    subtitle: (r) => str(r.description),
    fields: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Signature service' },
      { key: 'description', label: 'Description', type: 'textarea', colSpan: true },
      { key: 'audience', label: 'Primary audience', type: 'text' },
      { key: 'keywords', label: 'Keywords', type: 'tags', colSpan: true },
      { key: 'pain_points', label: 'Pain points solved', type: 'tags', colSpan: true },
      { key: 'use_cases', label: 'Use cases', type: 'tags', colSpan: true },
    ],
  },
  {
    kind: 'audiences',
    tab: 'Audiences',
    singular: 'audience',
    blurb: 'Specific segments you serve. Used for audience-fit labels — never generic "consumers".',
    title: (r) => str(r.label) ?? 'Untitled audience',
    subtitle: (r) => str(r.description),
    fields: [
      { key: 'label', label: 'Label', type: 'text', required: true, placeholder: 'Time-poor new parents' },
      { key: 'description', label: 'Description', type: 'textarea', colSpan: true },
      { key: 'motivations', label: 'Motivations', type: 'tags', colSpan: true },
      { key: 'objections', label: 'Objections', type: 'tags', colSpan: true },
      { key: 'keywords', label: 'Keywords', type: 'tags', colSpan: true },
    ],
  },
  {
    kind: 'competitors',
    tab: 'Competitors',
    singular: 'competitor',
    blurb: 'Who you compete with. Informs competitor-dissatisfaction signals and comparison safety.',
    title: (r) => str(r.name) ?? 'Untitled competitor',
    subtitle: (r) => str(r.website),
    fields: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Rival Co.' },
      { key: 'website', label: 'Website', type: 'text' },
      { key: 'known_weaknesses', label: 'Known weaknesses', type: 'tags', colSpan: true },
      { key: 'notes', label: 'Notes', type: 'textarea', colSpan: true },
    ],
  },
  {
    kind: 'brand-voice',
    tab: 'Brand voice',
    singular: 'voice profile',
    blurb: 'How your brand sounds. Guides reasoning tone and future creative generation (Phase 3).',
    title: (r) => arr(r.tone).join(', ') || 'Brand voice',
    subtitle: (r) => arr(r.personality).join(', ') || null,
    fields: [
      { key: 'tone', label: 'Tone', type: 'tags', colSpan: true, placeholder: 'e.g. warm, confident' },
      { key: 'personality', label: 'Personality', type: 'tags', colSpan: true },
      { key: 'do_use', label: 'Words to use', type: 'tags', colSpan: true },
      { key: 'avoid', label: 'Words to avoid', type: 'tags', colSpan: true },
      { key: 'example_copy', label: 'Example copy', type: 'textarea', colSpan: true },
    ],
  },
  {
    kind: 'offers',
    tab: 'Offers',
    singular: 'offer',
    blurb: 'Promotions and deals. Referenced when an opportunity has commercial intent.',
    title: (r) => str(r.name) ?? 'Untitled offer',
    subtitle: (r) => str(r.product_service) ?? str(r.promo_code),
    fields: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Spring launch offer' },
      { key: 'product_service', label: 'Product / service', type: 'text' },
      { key: 'promo_code', label: 'Promo code', type: 'text' },
      { key: 'cta', label: 'Call to action', type: 'text' },
      { key: 'terms', label: 'Terms', type: 'textarea', colSpan: true },
      { key: 'required_disclaimer', label: 'Required disclaimer', type: 'textarea', colSpan: true },
    ],
  },
  {
    kind: 'claims',
    tab: 'Claims library',
    singular: 'claim',
    blurb: 'Approved and restricted claims. Powers claim-safety warnings on opportunity reasoning.',
    title: (r) => str(r.text) ?? 'Untitled claim',
    subtitle: (r) => {
      const kind = str(r.kind);
      const risk = str(r.risk_level);
      return [kind && titleCase(kind), risk && `${titleCase(risk)} risk`].filter(Boolean).join(' · ') || null;
    },
    fields: [
      { key: 'text', label: 'Claim text', type: 'textarea', required: true, colSpan: true },
      { key: 'kind', label: 'Kind', type: 'select', options: claimKindOptions, defaultValue: 'approved' },
      { key: 'risk_level', label: 'Risk level', type: 'select', options: riskOptions, defaultValue: 'low' },
      { key: 'category', label: 'Category', type: 'text' },
      { key: 'notes', label: 'Notes', type: 'textarea', colSpan: true },
    ],
  },
  {
    kind: 'campaigns',
    tab: 'Campaigns',
    singular: 'campaign',
    blurb: 'Campaign contexts scouts can attach to. Mode sets how locations are treated.',
    title: (r) => str(r.name) ?? 'Untitled campaign',
    subtitle: (r) => {
      const mode = str(r.mode);
      return mode ? (campaignModeOptions[mode as keyof typeof campaignModeOptions] ?? titleCase(mode)) : null;
    },
    fields: [
      { key: 'name', label: 'Name', type: 'text', required: true, placeholder: 'Q3 demand capture' },
      { key: 'goal', label: 'Goal', type: 'text' },
      { key: 'mode', label: 'Location mode', type: 'select', options: campaignModeOptions, defaultValue: 'per_location' },
    ],
  },
  {
    kind: 'source-preferences',
    tab: 'Sources',
    singular: 'source preference',
    blurb: 'Which signal sources are enabled brand-wide. Fixture connectors in this build.',
    title: (r) => {
      const s = str(r.source_type);
      return s ? (sourceTypeLabels[s] ?? titleCase(s)) : 'Source';
    },
    subtitle: (r) => (r.enabled === false ? 'Disabled' : 'Enabled'),
    fields: [
      { key: 'source_type', label: 'Source', type: 'select', required: true, options: sourceTypeLabels },
      { key: 'enabled', label: 'Enabled', type: 'boolean', defaultValue: true },
    ],
  },
  {
    kind: 'channel-preferences',
    tab: 'Channels',
    singular: 'channel preference',
    blurb: 'Marketing channels you use. Referenced by recommended actions and Phase 3 authoring.',
    title: (r) => str(r.channel) ?? 'Channel',
    subtitle: (r) => (r.enabled === false ? 'Disabled' : 'Enabled'),
    fields: [
      { key: 'channel', label: 'Channel', type: 'text', required: true, placeholder: 'Instagram' },
      { key: 'enabled', label: 'Enabled', type: 'boolean', defaultValue: true },
      { key: 'weekly_volume', label: 'Weekly volume', type: 'number' },
    ],
  },
];

// ---- Generic create dialog ----------------------------------------------------

function buildDefaults(fields: FieldDef[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of fields) {
    if (f.defaultValue !== undefined) out[f.key] = f.defaultValue;
    else if (f.type === 'tags') out[f.key] = [];
    else if (f.type === 'boolean') out[f.key] = true;
    else if (f.type === 'number') out[f.key] = null;
    else out[f.key] = '';
  }
  return out;
}

function ContextDialog({
  workspaceId,
  config,
  open,
  onOpenChange,
}: {
  workspaceId: string;
  config: KindConfig;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [form, setForm] = useState<Record<string, unknown>>(() => buildDefaults(config.fields));
  const [error, setError] = useState<string | null>(null);

  // Reset the form whenever the dialog opens for a (possibly different) kind.
  const dialogKey = `${config.kind}:${open}`;
  const [seenKey, setSeenKey] = useState(dialogKey);
  if (dialogKey !== seenKey) {
    setSeenKey(dialogKey);
    if (open) {
      setForm(buildDefaults(config.fields));
      setError(null);
    }
  }

  const create = useMutation({
    mutationFn: () => api.createContext(workspaceId, config.kind, sanitize(form) as ContextInput),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.context(workspaceId, config.kind) });
      toast({ title: `${titleCase(config.singular)} added`, intent: 'success' });
      onOpenChange(false);
    },
    onError: (err) =>
      setError(err instanceof ApiError ? err.message : `Could not save ${config.singular}.`),
  });

  const requiredField = config.fields.find((f) => f.required);
  const missingRequired = requiredField
    ? isEmpty(form[requiredField.key])
    : false;

  const set = (key: string, value: unknown) => setForm((f) => ({ ...f, [key]: value }));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Add {config.singular}</DialogTitle>
        </DialogHeader>

        {error ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        ) : null}

        <div className="grid gap-4 sm:grid-cols-2">
          {config.fields.map((f) => (
            <Field
              key={f.key}
              label={f.label}
              required={f.required}
              description={f.description}
              className={f.colSpan || f.type === 'textarea' || f.type === 'tags' ? 'sm:col-span-2' : undefined}
            >
              {({ id }) => renderControl(f, id, form[f.key], (v) => set(f.key, v))}
            </Field>
          ))}
        </div>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button onClick={() => create.mutate()} disabled={create.isPending || missingRequired}>
            Add {config.singular}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function renderControl(
  f: FieldDef,
  id: string,
  value: unknown,
  onChange: (v: unknown) => void,
): React.ReactNode {
  switch (f.type) {
    case 'textarea':
      return (
        <Textarea id={id} value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value)} placeholder={f.placeholder} />
      );
    case 'tags':
      return (
        <TagInput id={id} value={arr(value)} onChange={(v) => onChange(v)} placeholder={f.placeholder ?? 'Add an item'} />
      );
    case 'number':
      return (
        <Input
          id={id}
          type="number"
          value={value == null ? '' : String(value)}
          onChange={(e) => onChange(e.target.value === '' ? null : Number(e.target.value))}
          placeholder={f.placeholder}
        />
      );
    case 'boolean':
      return (
        <div className="pt-1">
          <Switch checked={value !== false} onCheckedChange={(v) => onChange(v)} aria-label={f.label} />
        </div>
      );
    case 'select':
      return (
        <Select value={(value as string) || ''} onValueChange={(v) => onChange(v)}>
          <SelectTrigger id={id}>
            <SelectValue placeholder="Select…" />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(f.options ?? {}).map(([v, l]) => (
              <SelectItem key={v} value={v}>
                {l}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    default:
      return (
        <Input id={id} value={(value as string) ?? ''} onChange={(e) => onChange(e.target.value)} placeholder={f.placeholder} />
      );
  }
}

function isEmpty(v: unknown): boolean {
  if (v == null) return true;
  if (typeof v === 'string') return v.trim() === '';
  if (Array.isArray(v)) return v.length === 0;
  return false;
}

// Drop empty strings so the backend applies its own defaults / nullability.
function sanitize(form: Record<string, unknown>): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const [k, v] of Object.entries(form)) {
    if (typeof v === 'string' && v.trim() === '') continue;
    out[k] = v;
  }
  return out;
}

// ---- Per-kind panel -----------------------------------------------------------

function KindPanel({ workspaceId, config }: { workspaceId: string; config: KindConfig }) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const [dialogOpen, setDialogOpen] = useState(false);
  const [toDelete, setToDelete] = useState<ContextRow | null>(null);

  const query = useQuery({
    queryKey: queryKeys.context(workspaceId, config.kind),
    queryFn: ({ signal }) => api.listContext(workspaceId, config.kind, signal),
  });

  const remove = useMutation({
    mutationFn: (id: string) => api.deleteContext(workspaceId, config.kind, id),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.context(workspaceId, config.kind) });
      toast({ title: `${titleCase(config.singular)} removed`, intent: 'success' });
    },
    onError: (err) =>
      toast({
        title: 'Could not remove',
        description: err instanceof Error ? err.message : undefined,
        intent: 'error',
      }),
  });

  const rows = query.data ?? [];

  return (
    <div>
      <div className="mb-4 flex flex-col gap-2 sm:flex-row sm:items-center sm:justify-between">
        <p className="max-w-2xl text-sm text-muted-foreground">{config.blurb}</p>
        <Button size="sm" onClick={() => setDialogOpen(true)} className="shrink-0">
          <Plus className="size-4" /> Add {config.singular}
        </Button>
      </div>

      {query.isLoading ? (
        <LoadingRows rows={3} />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      ) : rows.length ? (
        <div className="space-y-2">
          {rows.map((row) => {
            const subtitle = config.subtitle?.(row) ?? null;
            return (
              <Card key={row.id}>
                <CardContent className="flex items-start justify-between gap-3 p-4">
                  <div className="min-w-0">
                    <p className="font-medium">{config.title(row)}</p>
                    {subtitle ? (
                      <p className="mt-0.5 line-clamp-2 text-sm text-muted-foreground">{subtitle}</p>
                    ) : null}
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    aria-label={`Remove ${config.singular}`}
                    onClick={() => setToDelete(row)}
                    disabled={remove.isPending}
                  >
                    <Trash2 className="size-4 text-muted-foreground" />
                  </Button>
                </CardContent>
              </Card>
            );
          })}
        </div>
      ) : (
        <EmptyState
          icon={Boxes}
          title={`No ${config.singular}s yet`}
          description={config.blurb}
          action={
            <Button onClick={() => setDialogOpen(true)}>
              <Plus className="size-4" /> Add {config.singular}
            </Button>
          }
        />
      )}

      <ContextDialog
        workspaceId={workspaceId}
        config={config}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
      />

      <ConfirmDialog
        open={Boolean(toDelete)}
        onOpenChange={(o) => !o && setToDelete(null)}
        title={`Remove this ${config.singular}?`}
        description="This cannot be undone."
        confirmLabel="Remove"
        destructive
        onConfirm={async () => {
          if (toDelete) await remove.mutateAsync(toDelete.id);
          setToDelete(null);
        }}
      />
    </div>
  );
}

// ---- Page ---------------------------------------------------------------------

function ContextInner({ workspaceId }: { workspaceId: string }) {
  const tabs = useMemo(() => KINDS, []);
  return (
    <div>
      <PageHeader
        title="Campaign context"
        description="Shared brand-wide context that feeds scouting and scoring. This context applies to every location; per-location specifics live on each location and scout request."
      />

      <Tabs defaultValue={tabs[0]!.kind}>
        <TabsList className="mb-4 flex h-auto flex-wrap justify-start gap-1">
          {tabs.map((t) => (
            <TabsTrigger key={t.kind} value={t.kind}>
              {t.tab}
            </TabsTrigger>
          ))}
        </TabsList>
        {tabs.map((t) => (
          <TabsContent key={t.kind} value={t.kind}>
            <KindPanel workspaceId={workspaceId} config={t} />
          </TabsContent>
        ))}
      </Tabs>
    </div>
  );
}

export function CampaignContextPage() {
  return <RequireWorkspace>{({ workspaceId }) => <ContextInner workspaceId={workspaceId} />}</RequireWorkspace>;
}
