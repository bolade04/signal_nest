import { useId, useState } from 'react';
import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { useToast } from '@/components/ui/toast';
import { CampaignFields } from './CampaignFields';
import { campaignWriteError } from './campaignErrors';
import { createCampaignBody, emptyCampaignForm, hasName, type CampaignFormValues } from './campaignForm';
import { CAMPAIGN_SETTINGS_NOTE } from './campaignVocabulary';
import { useCreateCampaign, useWorkspaceLocations } from './useCampaigns';

/** Add a campaign (editors only; the caller renders this only for them). */
export function CreateCampaignDialog({
  workspaceId,
  open,
  onOpenChange,
}: {
  workspaceId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const { toast } = useToast();
  const idPrefix = useId();
  const locations = useWorkspaceLocations(workspaceId);
  const create = useCreateCampaign(workspaceId);
  const [values, setValues] = useState<CampaignFormValues>(emptyCampaignForm);
  const [error, setError] = useState<string | null>(null);

  // A fresh form each time the dialog opens.
  const [wasOpen, setWasOpen] = useState(open);
  if (open !== wasOpen) {
    setWasOpen(open);
    if (open) {
      setValues(emptyCampaignForm());
      setError(null);
    }
  }

  const submit = () => {
    setError(null);
    create.mutate(createCampaignBody(values), {
      onSuccess: () => {
        toast({ title: 'Campaign added', intent: 'success' });
        onOpenChange(false);
      },
      onError: (err) => setError(campaignWriteError('create', err)),
    });
  };

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>New campaign</DialogTitle>
          <DialogDescription>{CAMPAIGN_SETTINGS_NOTE}</DialogDescription>
        </DialogHeader>
        {error ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
          >
            {error}
          </div>
        ) : null}
        <CampaignFields
          values={values}
          onChange={setValues}
          seedLocationIds={[]}
          locations={locations.data}
          locationsError={locations.isError}
          idPrefix={idPrefix}
        />
        <DialogFooter>
          <Button variant="outline" onClick={() => onOpenChange(false)} disabled={create.isPending}>
            Cancel
          </Button>
          <Button onClick={submit} disabled={create.isPending || !hasName(values)}>
            Add campaign
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
