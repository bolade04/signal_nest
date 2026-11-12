import type { LocationOut } from '@/api/types';
import { Field } from '@/components/common/form-field';
import { Checkbox } from '@/components/ui/checkbox';
import { Input } from '@/components/ui/input';
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from '@/components/ui/select';
import { Textarea } from '@/components/ui/textarea';
import type { CampaignFormValues } from './campaignForm';
import { locationName, offeredLocations } from './campaignForm';
import {
  CAMPAIGN_MODES,
  CAMPAIGN_STATUSES,
  isCampaignMode,
  isCampaignStatus,
  modeLabel,
  statusLabel,
} from './campaignVocabulary';

/**
 * The campaign's editable fields, for both create and edit. Mode and status are closed
 * selects over the contract's vocabularies (no free text). Locations are the workspace's
 * active locations plus any deactivated one that is already a member; the form never offers
 * a deactivated location to add, which the API would refuse.
 */
export function CampaignFields({
  values,
  onChange,
  seedLocationIds,
  locations,
  locationsError,
  idPrefix,
  creating = false,
}: {
  values: CampaignFormValues;
  onChange: (values: CampaignFormValues) => void;
  /** The membership as stored (empty for a new campaign). */
  seedLocationIds: readonly string[];
  /** The workspace's locations, or undefined while they load or when they could not load. */
  locations: readonly LocationOut[] | undefined;
  locationsError: boolean;
  idPrefix: string;
  /** The create dialog: there is no campaign yet whose locations could be "changed". */
  creating?: boolean;
}) {
  const set = <K extends keyof CampaignFormValues>(key: K, value: CampaignFormValues[K]) =>
    onChange({ ...values, [key]: value });
  const offered = locations ? offeredLocations(locations, seedLocationIds) : [];
  const toggle = (id: string, checked: boolean) =>
    set(
      'locationIds',
      checked ? [...values.locationIds, id].sort() : values.locationIds.filter((member) => member !== id),
    );

  return (
    <div className="grid gap-4 sm:grid-cols-2">
      <Field label="Name" required className="sm:col-span-2">
        {({ id }) => (
          <Input
            id={id}
            value={values.name}
            onChange={(event) => set('name', event.target.value)}
            placeholder="Spring opening"
            maxLength={200}
          />
        )}
      </Field>
      <Field label="Goal" className="sm:col-span-2">
        {({ id }) => (
          <Textarea id={id} value={values.goal} onChange={(event) => set('goal', event.target.value)} />
        )}
      </Field>
      <Field label="Location mode">
        {({ id }) => (
          <Select value={values.mode} onValueChange={(value) => isCampaignMode(value) && set('mode', value)}>
            <SelectTrigger id={id}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CAMPAIGN_MODES.map((mode) => (
                <SelectItem key={mode} value={mode}>
                  {modeLabel(mode)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </Field>
      <Field label="Status">
        {({ id }) => (
          <Select value={values.status} onValueChange={(value) => isCampaignStatus(value) && set('status', value)}>
            <SelectTrigger id={id}>
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              {CAMPAIGN_STATUSES.map((status) => (
                <SelectItem key={status} value={status}>
                  {statusLabel(status)}
                </SelectItem>
              ))}
            </SelectContent>
          </Select>
        )}
      </Field>
      <fieldset className="space-y-2 sm:col-span-2">
        <legend className="text-sm font-medium">Locations</legend>
        {locationsError ? (
          <p role="alert" className="text-sm text-destructive">
            {creating
              ? 'This workspace’s locations could not be loaded, so locations cannot be chosen now. The campaign can still be added without them.'
              : 'This workspace’s locations could not be loaded, so the campaign’s locations cannot be changed here now. Its other fields can still be saved.'}
          </p>
        ) : !locations ? (
          <p className="text-sm text-muted-foreground">Loading locations…</p>
        ) : offered.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            This workspace has no active locations. Add one on the Locations page first.
          </p>
        ) : (
          <ul className="space-y-1.5">
            {offered.map((location) => {
              const checkboxId = `${idPrefix}-location-${location.id}`;
              return (
                <li key={location.id} className="flex items-center gap-2">
                  <Checkbox
                    id={checkboxId}
                    checked={values.locationIds.includes(location.id)}
                    onCheckedChange={(checked) => toggle(location.id, checked === true)}
                  />
                  <label htmlFor={checkboxId} className="text-sm">
                    {locationName(location)}
                    {location.is_active ? null : (
                      <span className="text-muted-foreground">
                        {' '}
                        (deactivated: it can stay in this campaign, but cannot be added back once removed)
                      </span>
                    )}
                  </label>
                </li>
              );
            })}
          </ul>
        )}
      </fieldset>
    </div>
  );
}
