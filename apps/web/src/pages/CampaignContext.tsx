import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Boxes, Pencil, Plus, Trash2 } from 'lucide-react';
import { useId, useMemo, useRef, useState } from 'react';
import * as api from '@/api/endpoints';
import type { ContextInput, ContextKind, ContextPatchByKind } from '@/api/endpoints';
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
import { Switch } from '@/components/ui/switch';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { TagInput } from '@/components/ui/tag-input';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { sourceTypeLabels } from '@/lib/labels';
import { canEditWorkspace, useActorRole } from '@/lib/roles';
import { titleCase } from '@/lib/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

// ---- Field + kind descriptors -------------------------------------------------

type FieldType = 'text' | 'textarea' | 'tags' | 'number' | 'boolean' | 'select';

interface FieldDefBase {
  label: string;
  type: FieldType;
  required?: boolean;
  placeholder?: string;
  description?: string;
  options?: Record<string, string>;
  defaultValue?: unknown;
  colSpan?: boolean;
}

/** A field of collection `K`: its key must be a key of that collection's PATCH schema. */
type FieldDef<K extends ContextKind> = FieldDefBase & {
  key: Extract<keyof ContextPatchByKind[K], string>;
};
type AnyFieldDef = FieldDefBase & { key: string };

interface KindConfigOf<K extends ContextKind> {
  kind: K;
  tab: string;
  singular: string;
  blurb: string;
  fields: FieldDef<K>[];
  title: (row: ContextRow) => string;
  subtitle?: (row: ContextRow) => string | null;
}
type KindConfig = Omit<KindConfigOf<ContextKind>, 'fields'> & { fields: AnyFieldDef[] };

/**
 * Checks each kind's field keys against its generated PATCH schema (which has the same
 * keys as the create schema), so a misspelt key fails to compile instead of being sent
 * and silently ignored by the API.
 */
const defineKind = <K extends ContextKind>(config: KindConfigOf<K>): KindConfig => config;

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
  defineKind({
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
  }),
  defineKind({
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
  }),
  defineKind({
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
  }),
  defineKind({
    kind: 'brand-voice',
    tab: 'Brand voice',
    singular: 'voice profile',
    blurb: 'How your brand sounds. Saved to your workspace — scouting does not use it yet.',
    title: (r) => arr(r.tone).join(', ') || 'Brand voice',
    subtitle: (r) => arr(r.personality).join(', ') || null,
    fields: [
      { key: 'tone', label: 'Tone', type: 'tags', colSpan: true, placeholder: 'e.g. warm, confident' },
      { key: 'personality', label: 'Personality', type: 'tags', colSpan: true },
      { key: 'do_use', label: 'Words to use', type: 'tags', colSpan: true },
      { key: 'avoid', label: 'Words to avoid', type: 'tags', colSpan: true },
      { key: 'example_copy', label: 'Example copy', type: 'textarea', colSpan: true },
    ],
  }),
  defineKind({
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
  }),
  defineKind({
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
  }),
  defineKind({
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
  }),
  defineKind({
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
  }),
  defineKind({
    kind: 'channel-preferences',
    tab: 'Channels',
    singular: 'channel preference',
    blurb: 'Marketing channels you use. Saved to your workspace — scouting does not use them yet.',
    title: (r) => str(r.channel) ?? 'Channel',
    subtitle: (r) => (r.enabled === false ? 'Disabled' : 'Enabled'),
    fields: [
      { key: 'channel', label: 'Channel', type: 'text', required: true, placeholder: 'Instagram' },
      { key: 'enabled', label: 'Enabled', type: 'boolean', defaultValue: true },
      { key: 'weekly_volume', label: 'Weekly volume', type: 'number' },
    ],
  }),
];

// ---- Generic create / edit dialog ---------------------------------------------

function buildDefaults(fields: AnyFieldDef[]): Record<string, unknown> {
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

/** The form as seeded from a stored row, in the same shapes the controls produce. */
function buildFromRow(fields: AnyFieldDef[], row: ContextRow): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const f of fields) {
    const v = row[f.key];
    if (f.type === 'tags') out[f.key] = arr(v);
    else if (f.type === 'boolean') out[f.key] = v !== false;
    else if (f.type === 'number') out[f.key] = typeof v === 'number' ? v : null;
    else out[f.key] = typeof v === 'string' ? v : '';
  }
  return out;
}

/** A form value as the API stores it: an empty optional text field is `null`. */
function stored(f: AnyFieldDef, v: unknown): unknown {
  return f.type !== 'tags' && typeof v === 'string' && v.trim() === '' ? null : v;
}

/**
 * The PATCH body (P6-PLAT-9): only the fields this form shows AND the user changed.
 * Anything the form does not show — an offer's eligible locations, a product's claims
 * rules — is never sent, so it is never overwritten.
 */
function changedFields(fields: AnyFieldDef[], seed: Record<string, unknown>, form: Record<string, unknown>) {
  const out: Record<string, unknown> = {};
  for (const f of fields) {
    const before = stored(f, seed[f.key]);
    const after = stored(f, form[f.key]);
    if (JSON.stringify(before) !== JSON.stringify(after)) out[f.key] = after;
  }
  return out;
}

function ContextDialog({
  workspaceId,
  config,
  open,
  onOpenChange,
  editing,
  returnFocusTo,
}: {
  workspaceId: string;
  config: KindConfig;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  /** The stored row to edit; null to add a new one. */
  editing: ContextRow | null;
  /**
   * The button that opened the dialog. Without a Radix trigger the modal would drop focus
   * on <body> when it closes; it goes back to this button while the button is still there.
   */
  returnFocusTo: React.RefObject<HTMLElement | null>;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const seedFor = () => (editing ? buildFromRow(config.fields, editing) : buildDefaults(config.fields));
  const [seed, setSeed] = useState<Record<string, unknown>>(seedFor);
  const [form, setForm] = useState<Record<string, unknown>>(seed);
  const [error, setError] = useState<string | null>(null);

  // Reset the form whenever the dialog opens for a (possibly different) kind or row.
  const dialogKey = `${config.kind}:${open}:${editing?.id ?? 'new'}`;
  const [seenKey, setSeenKey] = useState(dialogKey);
  if (dialogKey !== seenKey) {
    setSeenKey(dialogKey);
    if (open) {
      const next = seedFor();
      setSeed(next);
      setForm(next);
      setError(null);
    }
  }

  const changes = editing ? changedFields(config.fields, seed, form) : {};
  const save = useMutation({
    mutationFn: () =>
      editing
        ? api.updateContext(
            workspaceId,
            config.kind,
            editing.id,
            changes as ContextPatchByKind[typeof config.kind],
          )
        : api.createContext(workspaceId, config.kind, sanitize(form) as ContextInput),
    onSuccess: async () => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.context(workspaceId, config.kind) });
      toast({
        title: `${titleCase(config.singular)} ${editing ? 'updated' : 'added'}`,
        intent: 'success',
      });
      onOpenChange(false);
    },
    onError: async (err) => {
      setError(err instanceof ApiError ? err.message : `Could not save ${config.singular}.`);
      // The row is gone (removed meanwhile, or never in this workspace): refresh the list
      // so it stops offering a row the API no longer has. The dialog stays open with the
      // refusal; nothing else is retried.
      if (editing && err instanceof ApiError && err.status === 404) {
        await queryClient.invalidateQueries({ queryKey: queryKeys.context(workspaceId, config.kind) });
      }
    },
  });

  const missingRequired = config.fields.some((f) => f.required && isEmpty(form[f.key]));
  const unchanged = Boolean(editing) && Object.keys(changes).length === 0;
  const action = editing ? 'Save changes' : `Add ${config.singular}`;

  const set = (key: string, value: unknown) => setForm((f) => ({ ...f, [key]: value }));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent
        className="max-w-2xl"
        onCloseAutoFocus={(event) => {
          const target = returnFocusTo.current;
          if (target?.isConnected) {
            event.preventDefault();
            target.focus();
          }
        }}
      >
        <DialogHeader>
          <DialogTitle>
            {editing ? 'Edit' : 'Add'} {config.singular}
          </DialogTitle>
          {editing ? (
            <DialogDescription>
              Editing “{config.title(editing)}”. Only the fields you change are saved; everything
              else on this {config.singular} stays as it is.
            </DialogDescription>
          ) : null}
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
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
            Cancel
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending || missingRequired || unchanged}>
            {action}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

function renderControl(
  f: AnyFieldDef,
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
    case 'select': {
      // A stored row can hold a value this list does not offer (the seed's claim kind
      // "blocked", a value set through the API or onboarding). Show it as it is, so an edit
      // never displays a blank or a different value than the one stored, and keeping it
      // sends nothing.
      const options: Record<string, string> = { ...f.options };
      if (typeof value === 'string' && value && !Object.prototype.hasOwnProperty.call(options, value)) {
        options[value] = titleCase(value);
      }
      return (
        <Select value={(value as string) || ''} onValueChange={(v) => onChange(v)}>
          <SelectTrigger id={id}>
            <SelectValue placeholder="Select…" />
          </SelectTrigger>
          <SelectContent>
            {Object.entries(options).map(([v, l]) => (
              <SelectItem key={v} value={v}>
                {l}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      );
    }
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
  const { organizationId } = useWorkspace();
  // Adding, editing and removing context are editor actions (EDITORS gate); every member
  // keeps the list. An unresolved role is denied until it resolves.
  const canEdit = canEditWorkspace(useActorRole(organizationId).role);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<ContextRow | null>(null);
  const [toDelete, setToDelete] = useState<ContextRow | null>(null);
  const idPrefix = useId();
  const opener = useRef<HTMLElement | null>(null);

  // Drop latched add/edit/remove intents when the role gate closes, so no
  // dialog can spring back open by itself if the gate later reopens.
  if (!canEdit && (dialogOpen || toDelete)) {
    setDialogOpen(false);
    setEditing(null);
    setToDelete(null);
  }

  const openAdd = (event: React.MouseEvent<HTMLElement>) => {
    opener.current = event.currentTarget;
    setEditing(null);
    setDialogOpen(true);
  };
  const openEdit = (row: ContextRow, event: React.MouseEvent<HTMLElement>) => {
    opener.current = event.currentTarget;
    setEditing(row);
    setDialogOpen(true);
  };

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
        {canEdit ? (
          <Button size="sm" onClick={openAdd} className="shrink-0">
            <Plus className="size-4" /> Add {config.singular}
          </Button>
        ) : null}
      </div>

      {query.isLoading ? (
        <LoadingRows rows={3} />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => query.refetch()} />
      ) : rows.length ? (
        <div className="space-y-2">
          {rows.map((row) => {
            const subtitle = config.subtitle?.(row) ?? null;
            const titleId = `${idPrefix}-${row.id}-title`;
            return (
              <Card key={row.id}>
                <CardContent className="flex items-start justify-between gap-3 p-4">
                  <div className="min-w-0">
                    <p id={titleId} className="font-medium">
                      {config.title(row)}
                    </p>
                    {subtitle ? (
                      <p className="mt-0.5 line-clamp-2 text-sm text-muted-foreground">{subtitle}</p>
                    ) : null}
                  </div>
                  {canEdit ? (
                    <div className="flex shrink-0 gap-1">
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Edit ${config.singular}`}
                        // Every row has an "Edit <singular>" button; the row's title tells
                        // a screen-reader user which one this is.
                        aria-describedby={titleId}
                        onClick={(event) => openEdit(row, event)}
                      >
                        <Pencil className="size-4 text-muted-foreground" />
                      </Button>
                      <Button
                        variant="ghost"
                        size="icon"
                        aria-label={`Remove ${config.singular}`}
                        onClick={() => setToDelete(row)}
                        disabled={remove.isPending}
                      >
                        <Trash2 className="size-4 text-muted-foreground" />
                      </Button>
                    </div>
                  ) : null}
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
            canEdit ? (
              <Button onClick={openAdd}>
                <Plus className="size-4" /> Add {config.singular}
              </Button>
            ) : undefined
          }
        />
      )}

      {canEdit ? (
        <>
          <ContextDialog
            workspaceId={workspaceId}
            config={config}
            open={dialogOpen}
            onOpenChange={setDialogOpen}
            editing={editing}
            returnFocusTo={opener}
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
        </>
      ) : null}
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
