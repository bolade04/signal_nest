import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Locate } from 'lucide-react';
import { useState } from 'react';
import * as api from '@/api/endpoints';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/api/queryKeys';
import type { GeoCoverageBase, GeoCoverageOut, LocationBase, LocationOut } from '@/api/types';
import { Field } from '@/components/common/form-field';
import { Button } from '@/components/ui/button';
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
import { Slider } from '@/components/ui/slider';
import { Switch } from '@/components/ui/switch';
import { TagInput } from '@/components/ui/tag-input';
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Textarea } from '@/components/ui/textarea';
import { useToast } from '@/components/ui/toast';
import { coverageTypeLabels } from '@/lib/labels';

interface Props {
  workspaceId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  location: LocationOut | null;
  /**
   * View-only: the same fields (including the service area, read through the
   * existing geo-coverage GET) rendered read-only, with no save and no geocode.
   * For members the API would refuse on the location write routes.
   */
  readOnly?: boolean;
}

const emptyLocation: LocationBase = {
  name: '',
  address: '',
  city: '',
  state_province: '',
  country: '',
  postal_code: '',
  timezone: '',
  currency: '',
  local_competitors: [],
  local_notes: '',
};

const emptyCoverage: GeoCoverageBase = {
  coverage_type: 'radius',
  radius_miles: 25,
  included_markets: [],
  excluded_markets: [],
  online_global: false,
};

/**
 * The business address a stored service-area rule opens with (F-1). `null` means the
 * field follows the street address: the location has no stored rule (its first save
 * sends the street address), or the stored value is the street address as last saved.
 * Otherwise it is the stored value, kept as the customer set it ('' when none is stored).
 */
function storedBusinessAddress(rule: GeoCoverageOut | undefined, savedStreet: string): string | null {
  if (!rule) return null;
  const stored = rule.business_address ?? '';
  return stored === savedStreet ? null : stored;
}

/** A stored service-area rule as the Service area tab edits it. */
function coverageFrom(rule: GeoCoverageOut): GeoCoverageBase {
  return {
    coverage_type: rule.coverage_type,
    business_address: rule.business_address,
    center_latitude: rule.center_latitude,
    center_longitude: rule.center_longitude,
    radius_miles: rule.radius_miles ?? 25,
    country: rule.country,
    state: rule.state,
    included_markets: rule.included_markets ?? [],
    excluded_markets: rule.excluded_markets ?? [],
    online_global: rule.online_global,
  };
}

export function LocationDialog({ workspaceId, open, onOpenChange, location, readOnly = false }: Props) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const isEdit = Boolean(location);
  // Example placeholders would read as data in a read-only view, so drop them.
  const hint = (text: string) => (readOnly ? '' : text);

  const [form, setForm] = useState<LocationBase>(emptyLocation);
  const [coverage, setCoverage] = useState<GeoCoverageBase>(emptyCoverage);
  // The business address the Service area tab shows and the save sends (F-1): `null`
  // while it follows the street address, otherwise the value the customer set.
  const [businessOverride, setBusinessOverride] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  // Why the last address lookup found no single place, and how to continue. Kept on
  // screen (not only a toast): the location can still be saved with its city,
  // state / province and country typed in, which is what scouting matches on.
  const [geocodeNote, setGeocodeNote] = useState<string | null>(null);

  const coverageQuery = useQuery({
    queryKey: location ? queryKeys.geoCoverage(workspaceId, location.id) : ['geo', 'none'],
    queryFn: ({ signal }) => api.getGeoCoverage(workspaceId, location!.id, signal),
    enabled: open && Boolean(location),
    retry: false,
  });

  // The fetched rule the Service area was last populated from (see below).
  const [syncedCoverage, setSyncedCoverage] = useState<typeof coverageQuery.data>(undefined);

  // Reset the form to the dialog's subject whenever it (re)opens or the edited
  // location changes. Adjusted during render (guarded by the previous open state
  // and location id) rather than in a setState effect.
  const [dialogFor, setDialogFor] = useState<{ open: boolean; id: string | null }>({
    open,
    id: location?.id ?? null,
  });
  if (dialogFor.open !== open || dialogFor.id !== (location?.id ?? null)) {
    setDialogFor({ open, id: location?.id ?? null });
    if (open) {
      setError(null);
      setGeocodeNote(null);
      if (location) {
        setForm({
          name: location.name,
          address: location.address ?? '',
          city: location.city ?? '',
          state_province: location.state_province ?? '',
          country: location.country ?? '',
          postal_code: location.postal_code ?? '',
          timezone: location.timezone ?? '',
          currency: location.currency ?? '',
          latitude: location.latitude ?? null,
          longitude: location.longitude ?? null,
          local_competitors: location.local_competitors ?? [],
          local_notes: location.local_notes ?? '',
          // Not edited here: sent back exactly as loaded, since the location PUT
          // replaces every field it is not sent with its default.
          languages: location.languages ?? [],
          local_phone: location.local_phone ?? null,
          local_website_page: location.local_website_page ?? null,
          local_social_links: location.local_social_links ?? {},
          google_business_profile: location.google_business_profile ?? null,
          overrides: location.overrides ?? {},
        });
        // Every opening starts from this location's stored rule: the cached one now, a
        // fetched one when it arrives (below), the defaults while it has none. Nothing
        // carries over from a cancelled edit or from another location.
        const cached = coverageQuery.data;
        setCoverage(cached ? coverageFrom(cached) : emptyCoverage);
        setSyncedCoverage(cached);
        setBusinessOverride(storedBusinessAddress(cached, location.address ?? ''));
      } else {
        setForm(emptyLocation);
        setCoverage(emptyCoverage);
        setBusinessOverride(null);
      }
    }
  }

  // Populate coverage from the fetched record once it arrives (guarded by the
  // previous data reference so it runs exactly once per fetch).
  if (coverageQuery.data && coverageQuery.data !== syncedCoverage) {
    setSyncedCoverage(coverageQuery.data);
    setCoverage(coverageFrom(coverageQuery.data));
    setBusinessOverride(storedBusinessAddress(coverageQuery.data, location?.address ?? ''));
  }
  const businessAddress = businessOverride ?? form.address ?? '';
  // An existing location's rule is written back only once it is known: loaded, or
  // answered 404 (none stored). Saving before that would store the defaults over it.
  const ruleMissing = coverageQuery.error instanceof ApiError && coverageQuery.error.status === 404;
  const ruleKnown = !location || coverageQuery.data !== undefined || ruleMissing;
  const ruleUnreadable = Boolean(location) && coverageQuery.isError && !ruleMissing;

  const geocodeMutation = useMutation({
    // The dialog's workspace is the active one (Locations renders it inside RequireWorkspace).
    mutationFn: () =>
      api.geocode(workspaceId, { query: [form.address, form.city, form.country].filter(Boolean).join(', ') }),
    onSuccess: (res) => {
      setGeocodeNote(null);
      setForm((f) => ({
        ...f,
        latitude: res.latitude,
        longitude: res.longitude,
        city: f.city || res.city,
        state_province: f.state_province || res.state_province,
        country: f.country || res.country,
        timezone: f.timezone || res.timezone,
      }));
      const place = res.label ?? [res.city, res.state_province, res.country].filter(Boolean).join(', ');
      toast({
        title: 'Location resolved',
        description: [place, `confidence ${Math.round(res.confidence * 100)}%`].filter(Boolean).join(' · '),
        intent: 'success',
      });
    },
    // The API's message says why and how to continue (P6-DATA-3); show it in place.
    onError: (err) =>
      setGeocodeNote(
        err instanceof ApiError && err.message
          ? err.message
          : 'Address lookup failed. You can still save this location: enter its city, state or province and country, which is what scouting uses.',
      ),
  });

  const save = useMutation({
    mutationFn: async () => {
      const saved = location
        ? await api.updateLocation(workspaceId, location.id, form)
        : await api.createLocation(workspaceId, form);
      await api.upsertGeoCoverage(workspaceId, saved.id, {
        ...coverage,
        center_latitude: coverage.center_latitude ?? form.latitude ?? null,
        center_longitude: coverage.center_longitude ?? form.longitude ?? null,
        // What the field shows; an empty field saves null (F-1).
        business_address: businessAddress.trim() ? businessAddress : null,
      });
      return saved;
    },
    onSuccess: async (saved) => {
      await queryClient.invalidateQueries({ queryKey: queryKeys.locations(workspaceId) });
      await queryClient.invalidateQueries({ queryKey: queryKeys.geoCoverage(workspaceId, saved.id) });
      toast({ title: isEdit ? 'Location updated' : 'Location added', intent: 'success' });
      onOpenChange(false);
    },
    onError: (err) => setError(err instanceof ApiError ? err.message : 'Could not save location.'),
  });

  const update = <K extends keyof LocationBase>(key: K, value: LocationBase[K]) =>
    setForm((f) => ({ ...f, [key]: value }));

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{readOnly ? 'Location details' : isEdit ? 'Edit location' : 'Add location'}</DialogTitle>
          <DialogDescription>
            Each location is scouted independently. Its market, service area and local context never
            leak into other locations.
          </DialogDescription>
        </DialogHeader>

        {error ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            {error}
          </div>
        ) : null}
        {ruleUnreadable && !readOnly ? (
          <div role="alert" className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive">
            Could not load this location&apos;s service area. Close and reopen the location to try again before saving.
          </div>
        ) : null}

        <Tabs defaultValue="details">
          <TabsList>
            <TabsTrigger value="details">Details</TabsTrigger>
            <TabsTrigger value="coverage">Service area</TabsTrigger>
          </TabsList>

          <TabsContent value="details" className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Location name" required={!readOnly} className="sm:col-span-2">
                {({ id }) => (
                  <Input id={id} value={form.name} onChange={(e) => update('name', e.target.value)} placeholder={hint('Dallas Flagship')} readOnly={readOnly} />
                )}
              </Field>
              <Field label="Street address" className="sm:col-span-2">
                {({ id }) => (
                  <div className="flex gap-2">
                    <Input id={id} value={form.address ?? ''} onChange={(e) => update('address', e.target.value)} placeholder={hint('123 Main St')} readOnly={readOnly} />
                    {readOnly ? null : (
                      <Button
                        type="button"
                        variant="outline"
                        onClick={() => geocodeMutation.mutate()}
                        disabled={geocodeMutation.isPending}
                      >
                        <Locate className="size-4" /> Geocode
                      </Button>
                    )}
                  </div>
                )}
              </Field>
              <Field label="City">
                {({ id }) => <Input id={id} value={form.city ?? ''} onChange={(e) => update('city', e.target.value)} readOnly={readOnly} />}
              </Field>
              <Field label="State / province">
                {({ id }) => <Input id={id} value={form.state_province ?? ''} onChange={(e) => update('state_province', e.target.value)} readOnly={readOnly} />}
              </Field>
              <Field label="Country">
                {({ id }) => <Input id={id} value={form.country ?? ''} onChange={(e) => update('country', e.target.value)} readOnly={readOnly} />}
              </Field>
              <Field label="Postal code">
                {({ id }) => <Input id={id} value={form.postal_code ?? ''} onChange={(e) => update('postal_code', e.target.value)} readOnly={readOnly} />}
              </Field>
              <Field label="Timezone">
                {({ id }) => <Input id={id} value={form.timezone ?? ''} onChange={(e) => update('timezone', e.target.value)} placeholder={hint('America/Chicago')} readOnly={readOnly} />}
              </Field>
              <Field label="Currency">
                {({ id }) => <Input id={id} value={form.currency ?? ''} onChange={(e) => update('currency', e.target.value)} placeholder={hint('USD')} readOnly={readOnly} />}
              </Field>
              <Field label="Coordinates" description={readOnly ? undefined : 'Auto-filled by geocoding.'} className="sm:col-span-2">
                {() => (
                  <div className="space-y-1">
                    <p className="text-sm text-muted-foreground">
                      {form.latitude != null && form.longitude != null
                        ? `${form.latitude.toFixed(4)}, ${form.longitude.toFixed(4)}`
                        : readOnly
                          ? 'Not set.'
                          : 'Not set — enter an address and geocode.'}
                    </p>
                    {geocodeNote && !readOnly ? (
                      // Foreground text on the warning tint, as the e-mail verification banner
                      // does. `warning-foreground` is for text ON a solid warning fill; on this
                      // 10% tint it measured 1.06:1 (light) and 1.18:1 (dark) (UI-AUDIT F3).
                      <p role="status" className="rounded-md border border-warning/40 bg-warning/10 px-3 py-2 text-sm text-foreground">
                        {geocodeNote}
                      </p>
                    ) : null}
                  </div>
                )}
              </Field>
              <Field label="Local competitors" className="sm:col-span-2">
                {({ id }) => (
                  <TagInput id={id} value={form.local_competitors ?? []} onChange={(v) => update('local_competitors', v)} placeholder={hint('Add a competitor')} disabled={readOnly} />
                )}
              </Field>
              <Field label="Local notes" className="sm:col-span-2">
                {({ id }) => <Textarea id={id} value={form.local_notes ?? ''} onChange={(e) => update('local_notes', e.target.value)} readOnly={readOnly} />}
              </Field>
            </div>
          </TabsContent>

          <TabsContent value="coverage" className="space-y-4">
            <Field
              label="Business address"
              description={readOnly ? undefined : 'Keep it in the same country and state or province as this location.'}
            >
              {({ id, describedBy }) =>
                readOnly ? (
                  <p className="text-sm">{businessAddress || 'Not set.'}</p>
                ) : (
                  <Input
                    id={id}
                    value={businessAddress}
                    // While it equals the street address it moves with it; any other value stays as set.
                    onChange={(e) => setBusinessOverride(e.target.value === (form.address ?? '') ? null : e.target.value)}
                    aria-describedby={describedBy}
                    maxLength={400}
                  />
                )
              }
            </Field>

            <Field label="Coverage type" description="How far this location serves customers.">
              {({ id }) => (
                <Select value={coverage.coverage_type} onValueChange={(v) => setCoverage((c) => ({ ...c, coverage_type: v }))} disabled={readOnly}>
                  <SelectTrigger id={id}>
                    <SelectValue />
                  </SelectTrigger>
                  <SelectContent>
                    {Object.entries(coverageTypeLabels).map(([value, lbl]) => (
                      <SelectItem key={value} value={value}>
                        {lbl}
                      </SelectItem>
                    ))}
                  </SelectContent>
                </Select>
              )}
            </Field>

            {coverage.coverage_type === 'radius' ? (
              <Field
                label={`Service radius — ${coverage.radius_miles ?? 0} miles`}
                description={readOnly ? undefined : 'Drag to set how far out customers come from (1–200 miles).'}
              >
                {({ id }) => (
                  <Slider
                    aria-label="Service radius in miles"
                    id={id}
                    min={1}
                    max={200}
                    step={1}
                    value={[coverage.radius_miles ?? 25]}
                    onValueChange={([v]) => setCoverage((c) => ({ ...c, radius_miles: v }))}
                    disabled={readOnly}
                  />
                )}
              </Field>
            ) : null}

            <Field label="Included markets" description="Cities or regions explicitly in scope.">
              {({ id }) => (
                <TagInput id={id} value={coverage.included_markets ?? []} onChange={(v) => setCoverage((c) => ({ ...c, included_markets: v }))} placeholder={hint('Add a market')} disabled={readOnly} />
              )}
            </Field>
            <Field label="Excluded markets" description="Places to keep out of scope.">
              {({ id }) => (
                <TagInput id={id} value={coverage.excluded_markets ?? []} onChange={(v) => setCoverage((c) => ({ ...c, excluded_markets: v }))} placeholder={hint('Add an exclusion')} disabled={readOnly} />
              )}
            </Field>
            <label className="flex items-center justify-between rounded-md border border-border px-3 py-2.5">
              <span className="text-sm">
                <span className="font-medium">Also serve online / globally</span>
                <span className="block text-xs text-muted-foreground">Include online demand beyond the physical area.</span>
              </span>
              <Switch checked={coverage.online_global} onCheckedChange={(v) => setCoverage((c) => ({ ...c, online_global: v }))} aria-label="Serve online globally" disabled={readOnly} />
            </label>
          </TabsContent>
        </Tabs>

        <DialogFooter>
          {readOnly ? (
            <Button variant="outline" onClick={() => onOpenChange(false)}>
              Close
            </Button>
          ) : (
            <>
              <Button variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
                Cancel
              </Button>
              <Button onClick={() => save.mutate()} disabled={save.isPending || !form.name.trim() || !ruleKnown}>
                {isEdit ? 'Save changes' : 'Add location'}
              </Button>
            </>
          )}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
