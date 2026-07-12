import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Locate } from 'lucide-react';
import { useEffect, useState } from 'react';
import * as api from '@/api/endpoints';
import { ApiError } from '@/api/client';
import { queryKeys } from '@/api/queryKeys';
import type { GeoCoverageBase, LocationBase, LocationOut } from '@/api/types';
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

export function LocationDialog({ workspaceId, open, onOpenChange, location }: Props) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const isEdit = Boolean(location);

  const [form, setForm] = useState<LocationBase>(emptyLocation);
  const [coverage, setCoverage] = useState<GeoCoverageBase>(emptyCoverage);
  const [error, setError] = useState<string | null>(null);

  const coverageQuery = useQuery({
    queryKey: location ? queryKeys.geoCoverage(workspaceId, location.id) : ['geo', 'none'],
    queryFn: ({ signal }) => api.getGeoCoverage(workspaceId, location!.id, signal),
    enabled: open && Boolean(location),
    retry: false,
  });

  useEffect(() => {
    if (!open) return;
    setError(null);
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
      });
    } else {
      setForm(emptyLocation);
      setCoverage(emptyCoverage);
    }
  }, [open, location]);

  useEffect(() => {
    if (coverageQuery.data) {
      setCoverage({
        coverage_type: coverageQuery.data.coverage_type,
        business_address: coverageQuery.data.business_address,
        center_latitude: coverageQuery.data.center_latitude,
        center_longitude: coverageQuery.data.center_longitude,
        radius_miles: coverageQuery.data.radius_miles ?? 25,
        country: coverageQuery.data.country,
        state: coverageQuery.data.state,
        included_markets: coverageQuery.data.included_markets ?? [],
        excluded_markets: coverageQuery.data.excluded_markets ?? [],
        online_global: coverageQuery.data.online_global,
      });
    }
  }, [coverageQuery.data]);

  const geocodeMutation = useMutation({
    mutationFn: () => api.geocode({ query: [form.address, form.city, form.country].filter(Boolean).join(', ') }),
    onSuccess: (res) => {
      setForm((f) => ({
        ...f,
        latitude: res.latitude,
        longitude: res.longitude,
        city: f.city || res.city,
        state_province: f.state_province || res.state_province,
        country: f.country || res.country,
        timezone: f.timezone || res.timezone,
      }));
      toast({
        title: 'Location resolved',
        description: `${res.city}, ${res.country} · confidence ${Math.round(res.confidence * 100)}%`,
        intent: 'success',
      });
    },
    onError: (err) =>
      toast({
        title: 'Could not geocode address',
        description: err instanceof Error ? err.message : undefined,
        intent: 'error',
      }),
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
        business_address: coverage.business_address ?? form.address ?? null,
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
          <DialogTitle>{isEdit ? 'Edit location' : 'Add location'}</DialogTitle>
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

        <Tabs defaultValue="details">
          <TabsList>
            <TabsTrigger value="details">Details</TabsTrigger>
            <TabsTrigger value="coverage">Service area</TabsTrigger>
          </TabsList>

          <TabsContent value="details" className="space-y-4">
            <div className="grid gap-4 sm:grid-cols-2">
              <Field label="Location name" required className="sm:col-span-2">
                {({ id }) => (
                  <Input id={id} value={form.name} onChange={(e) => update('name', e.target.value)} placeholder="Dallas Flagship" />
                )}
              </Field>
              <Field label="Street address" className="sm:col-span-2">
                {({ id }) => (
                  <div className="flex gap-2">
                    <Input id={id} value={form.address ?? ''} onChange={(e) => update('address', e.target.value)} placeholder="123 Main St" />
                    <Button
                      type="button"
                      variant="outline"
                      onClick={() => geocodeMutation.mutate()}
                      disabled={geocodeMutation.isPending}
                    >
                      <Locate className="size-4" /> Geocode
                    </Button>
                  </div>
                )}
              </Field>
              <Field label="City">
                {({ id }) => <Input id={id} value={form.city ?? ''} onChange={(e) => update('city', e.target.value)} />}
              </Field>
              <Field label="State / province">
                {({ id }) => <Input id={id} value={form.state_province ?? ''} onChange={(e) => update('state_province', e.target.value)} />}
              </Field>
              <Field label="Country">
                {({ id }) => <Input id={id} value={form.country ?? ''} onChange={(e) => update('country', e.target.value)} />}
              </Field>
              <Field label="Postal code">
                {({ id }) => <Input id={id} value={form.postal_code ?? ''} onChange={(e) => update('postal_code', e.target.value)} />}
              </Field>
              <Field label="Timezone">
                {({ id }) => <Input id={id} value={form.timezone ?? ''} onChange={(e) => update('timezone', e.target.value)} placeholder="America/Chicago" />}
              </Field>
              <Field label="Currency">
                {({ id }) => <Input id={id} value={form.currency ?? ''} onChange={(e) => update('currency', e.target.value)} placeholder="USD" />}
              </Field>
              <Field label="Coordinates" description="Auto-filled by geocoding." className="sm:col-span-2">
                {() => (
                  <p className="text-sm text-muted-foreground">
                    {form.latitude != null && form.longitude != null
                      ? `${form.latitude.toFixed(4)}, ${form.longitude.toFixed(4)}`
                      : 'Not set — enter an address and geocode.'}
                  </p>
                )}
              </Field>
              <Field label="Local competitors" className="sm:col-span-2">
                {({ id }) => (
                  <TagInput id={id} value={form.local_competitors ?? []} onChange={(v) => update('local_competitors', v)} placeholder="Add a competitor" />
                )}
              </Field>
              <Field label="Local notes" className="sm:col-span-2">
                {({ id }) => <Textarea id={id} value={form.local_notes ?? ''} onChange={(e) => update('local_notes', e.target.value)} />}
              </Field>
            </div>
          </TabsContent>

          <TabsContent value="coverage" className="space-y-4">
            <Field label="Coverage type" description="How far this location serves customers.">
              {({ id }) => (
                <Select value={coverage.coverage_type} onValueChange={(v) => setCoverage((c) => ({ ...c, coverage_type: v }))}>
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
              <Field label={`Service radius — ${coverage.radius_miles ?? 0} miles`} description="Drag to set how far out customers come from (1–200 miles).">
                {({ id }) => (
                  <Slider
                    aria-label="Service radius in miles"
                    id={id}
                    min={1}
                    max={200}
                    step={1}
                    value={[coverage.radius_miles ?? 25]}
                    onValueChange={([v]) => setCoverage((c) => ({ ...c, radius_miles: v }))}
                  />
                )}
              </Field>
            ) : null}

            <Field label="Included markets" description="Cities or regions explicitly in scope.">
              {({ id }) => (
                <TagInput id={id} value={coverage.included_markets ?? []} onChange={(v) => setCoverage((c) => ({ ...c, included_markets: v }))} placeholder="Add a market" />
              )}
            </Field>
            <Field label="Excluded markets" description="Places to keep out of scope.">
              {({ id }) => (
                <TagInput id={id} value={coverage.excluded_markets ?? []} onChange={(v) => setCoverage((c) => ({ ...c, excluded_markets: v }))} placeholder="Add an exclusion" />
              )}
            </Field>
            <label className="flex items-center justify-between rounded-md border border-border px-3 py-2.5">
              <span className="text-sm">
                <span className="font-medium">Also serve online / globally</span>
                <span className="block text-xs text-muted-foreground">Include online demand beyond the physical area.</span>
              </span>
              <Switch checked={coverage.online_global} onCheckedChange={(v) => setCoverage((c) => ({ ...c, online_global: v }))} aria-label="Serve online globally" />
            </label>
          </TabsContent>
        </Tabs>

        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={save.isPending}>
            Cancel
          </Button>
          <Button onClick={() => save.mutate()} disabled={save.isPending || !form.name.trim()}>
            {isEdit ? 'Save changes' : 'Add location'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
