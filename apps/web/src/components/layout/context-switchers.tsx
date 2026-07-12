import { Building2, MapPin } from 'lucide-react';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useWorkspace } from '@/workspace/WorkspaceContext';

// Workspace switcher (organization + workspace). Selecting a different workspace
// re-scopes every tenant query via the workspace-keyed query keys.
export function WorkspaceSwitcher() {
  const {
    organizations,
    workspaces,
    organizationId,
    workspaceId,
    setOrganizationId,
    setWorkspaceId,
  } = useWorkspace();

  return (
    <div className="flex items-center gap-2">
      <Building2 className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      {organizations.length > 1 ? (
        <Select value={organizationId ?? undefined} onValueChange={setOrganizationId}>
          <SelectTrigger aria-label="Organization" className="h-8 w-[150px] text-xs">
            <SelectValue placeholder="Organization" />
          </SelectTrigger>
          <SelectContent>
            {organizations.map((org) => (
              <SelectItem key={org.id} value={org.id}>
                {org.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      ) : null}
      <Select value={workspaceId ?? undefined} onValueChange={setWorkspaceId}>
        <SelectTrigger aria-label="Workspace" className="h-8 w-[180px] text-xs">
          <SelectValue placeholder="Select workspace" />
        </SelectTrigger>
        <SelectContent>
          {workspaces.map((ws) => (
            <SelectItem key={ws.id} value={ws.id}>
              {ws.name}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

const ALL_LOCATIONS = '__all__';

// Location filter. "All locations" maps to a null tenant filter; a specific
// location scopes queries so markets never bleed into one another.
export function LocationSwitcher() {
  const { locations, locationId, setLocationId } = useWorkspace();

  if (!locations.length) return null;

  return (
    <div className="flex items-center gap-2">
      <MapPin className="size-4 shrink-0 text-muted-foreground" aria-hidden />
      <Select
        value={locationId ?? ALL_LOCATIONS}
        onValueChange={(v) => setLocationId(v === ALL_LOCATIONS ? null : v)}
      >
        <SelectTrigger aria-label="Location filter" className="h-8 w-[190px] text-xs">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          <SelectItem value={ALL_LOCATIONS}>All locations</SelectItem>
          {locations.map((loc) => (
            <SelectItem key={loc.id} value={loc.id}>
              {loc.name}
              {loc.city ? ` · ${loc.city}` : ''}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
