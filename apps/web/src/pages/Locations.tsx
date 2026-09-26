import { useQuery } from '@tanstack/react-query';
import { Eye, MapPin, Pencil, Plus } from 'lucide-react';
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
import { canEditWorkspace, useActorRole } from '@/lib/roles';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import { LocationDialog } from './locations/LocationDialog';

function LocationsInner({ workspaceId }: { workspaceId: string }) {
  const { organizationId } = useWorkspace();
  // Adding and editing locations are editor actions (EDITORS gate). Everyone else
  // gets the same dialog read-only, so the service area stays readable. An
  // unresolved role is denied until it resolves.
  const canEdit = canEditWorkspace(useActorRole(organizationId).role);
  const [dialogOpen, setDialogOpen] = useState(false);
  const [editing, setEditing] = useState<LocationOut | null>(null);

  // Close any open dialog when the role gate closes. A create dialog has nothing
  // to show read-only, and an edit dialog may hold unsaved input that the
  // read-only view would present as saved data. Adjusted during render, like
  // SchedulePanel's capability gate, so the stale dialog is never committed.
  const [gateWasOpen, setGateWasOpen] = useState(canEdit);
  if (gateWasOpen !== canEdit) {
    setGateWasOpen(canEdit);
    if (!canEdit && dialogOpen) setDialogOpen(false);
  }

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
          canEdit ? (
            <Button onClick={openNew}>
              <Plus className="size-4" /> Add location
            </Button>
          ) : undefined
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
                  {canEdit ? (
                    <>
                      <Pencil className="size-4" /> Edit &amp; service area
                    </>
                  ) : (
                    <>
                      <Eye className="size-4" /> View details &amp; service area
                    </>
                  )}
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={MapPin}
          title="No locations yet"
          description={
            canEdit
              ? 'Add your first market to start scouting. You can add Dallas, London, Lagos, Nairobi or anywhere else — each stays fully independent.'
              : 'No locations have been added to this workspace yet.'
          }
          action={
            canEdit ? (
              <Button onClick={openNew}>
                <Plus className="size-4" /> Add location
              </Button>
            ) : undefined
          }
        />
      )}

      <LocationDialog
        workspaceId={workspaceId}
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        location={editing}
        readOnly={!canEdit}
      />
    </div>
  );
}

export function LocationsPage() {
  return <RequireWorkspace>{({ workspaceId }) => <LocationsInner workspaceId={workspaceId} />}</RequireWorkspace>;
}
