import { useQuery } from '@tanstack/react-query';
import { MapPin, Pencil, Plus } from 'lucide-react';
import { useState } from 'react';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { LocationOut } from '@/api/types';
import { EmptyState, ErrorState, LoadingRows } from '@/components/common/states';
import { PageHeader } from '@/components/layout/page-header';
import { RequireWorkspace } from '@/components/layout/require-workspace';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { LocationDialog } from './locations/LocationDialog';

function LocationsInner({ workspaceId }: { workspaceId: string }) {
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<LocationOut | null>(null);

  const locationsQuery = useQuery({
    queryKey: queryKeys.locations(workspaceId),
    queryFn: ({ signal }) => api.listLocations(workspaceId, signal),
  });

  const openNew = () => {
    setEditing(null);
    setDialogOpen(true);
  };
  const openEdit = (loc: LocationOut) => {
    setEditing(loc);
    setDialogOpen(true);
  };

  return (
    <div>
      <PageHeader
        title="Locations"
        description="Manage markets and service areas. Every location is scouted and scored independently."
        actions={
          <Button onClick={openNew}>
            <Plus className="size-4" /> Add location
          </Button>
        }
      />

      {locationsQuery.isLoading ? (
        <LoadingRows rows={3} />
      ) : locationsQuery.isError ? (
        <ErrorState error={locationsQuery.error} onRetry={() => locationsQuery.refetch()} />
      ) : locationsQuery.data && locationsQuery.data.length > 0 ? (
        <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
          {locationsQuery.data.map((loc) => (
            <Card key={loc.id}>
              <CardHeader className="flex-row items-start justify-between space-y-0">
                <div className="min-w-0">
                  <CardTitle className="flex items-center gap-2">
                    <MapPin className="size-4 text-primary" /> {loc.name}
                  </CardTitle>
                  <p className="mt-1 text-sm text-muted-foreground">
                    {[loc.city, loc.state_province, loc.country].filter(Boolean).join(', ') ||
                      'No address set'}
                  </p>
                </div>
                <Badge intent={loc.is_active ? 'success' : 'muted'}>
                  {loc.is_active ? 'Active' : 'Inactive'}
                </Badge>
              </CardHeader>
              <CardContent className="space-y-3 text-sm">
                <div className="flex flex-wrap gap-1.5">
                  {loc.timezone ? <Badge intent="outline">{loc.timezone}</Badge> : null}
                  {loc.currency ? <Badge intent="outline">{loc.currency}</Badge> : null}
                  {(loc.local_competitors ?? []).length ? (
                    <Badge intent="neutral">
                      {loc.local_competitors!.length} local competitor
                      {loc.local_competitors!.length === 1 ? '' : 's'}
                    </Badge>
                  ) : null}
                </div>
                {loc.local_notes ? (
                  <p className="line-clamp-2 text-muted-foreground">{loc.local_notes}</p>
                ) : null}
                <Button variant="outline" size="sm" onClick={() => openEdit(loc)}>
                  <Pencil className="size-4" /> Edit &amp; service area
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={MapPin}
          title="No locations yet"
          description="Add your first market to start scouting. You can add Dallas, London, Lagos, Nairobi or anywhere else — each stays fully independent."
          action={
            <Button onClick={openNew}>
              <Plus className="size-4" /> Add location
            </Button>
          }
        />
      )}

      <LocationDialog
        workspaceId={workspaceId}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        location={editing}
      />
    </div>
  );
}

export function LocationsPage() {
  return <RequireWorkspace>{({ workspaceId }) => <LocationsInner workspaceId={workspaceId} />}</RequireWorkspace>;
}
