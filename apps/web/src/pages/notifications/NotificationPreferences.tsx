import { Button } from '@/components/ui/button';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog';
import { Label } from '@/components/ui/label';
import { Skeleton } from '@/components/ui/skeleton';
import { Switch } from '@/components/ui/switch';
import { EVENT_PREFERENCE_LABEL } from './notificationVocabulary';
import {
  isDark,
  useNotificationPreferences,
  useNotificationsAnalyticsCapability,
  useUpdateNotificationPreferences,
} from './useNotifications';

/**
 * The signed-in user's notification preferences in one workspace, in a dialog (Project
 * Phase 5E, 5E-W3). The bell's "Notification settings" item opens it. Mounted only while open,
 * so nothing from one workspace or user carries over to another.
 */
export function NotificationPreferencesDialog({
  workspaceId,
  userId,
  onClose,
}: {
  workspaceId: string;
  userId: string;
  onClose: () => void;
}) {
  return (
    <Dialog open onOpenChange={(open) => (open ? undefined : onClose())}>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Notification settings</DialogTitle>
          <DialogDescription>
            Choose which in-app notifications you get in this workspace. Nothing is sent by email
            or text message.
          </DialogDescription>
        </DialogHeader>
        <NotificationPreferencesForm workspaceId={workspaceId} userId={userId} />
        <DialogFooter>
          <Button variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}

/**
 * One switch per event code. Each change is saved at once (`PUT` with that one code); the
 * answer, every code, replaces what is shown.
 *
 * Self-guarding: no preferences request is sent unless the workspace-effective reflection is
 * `enabled === true`. A 503 (the capability turned off since) shows the "not available" line.
 */
export function NotificationPreferencesForm({
  workspaceId,
  userId,
}: {
  workspaceId: string;
  userId: string;
}) {
  const capability = useNotificationsAnalyticsCapability(workspaceId);
  const preferences = useNotificationPreferences(workspaceId, userId, capability.isEnabled);
  const update = useUpdateNotificationPreferences(workspaceId, userId);

  if (capability.status === 'loading' || (capability.isEnabled && preferences.isPending)) {
    return (
      <div className="space-y-3" aria-busy="true" aria-live="polite">
        <span className="sr-only">Loading notification settings…</span>
        <Skeleton className="h-5 w-full" />
        <Skeleton className="h-5 w-full" />
        <Skeleton className="h-5 w-2/3" />
      </div>
    );
  }
  if (!capability.isEnabled || (preferences.isError && isDark(preferences.error))) {
    return (
      <p role="status" className="text-sm text-muted-foreground">
        Notifications aren&apos;t available in this workspace.
      </p>
    );
  }
  if (preferences.isError) {
    return (
      <div role="alert" className="space-y-2 text-sm">
        <p className="text-muted-foreground">Your notification settings could not be loaded.</p>
        <Button variant="outline" size="sm" onClick={() => void preferences.refetch()}>
          Try again
        </Button>
      </div>
    );
  }

  const items = preferences.data?.items ?? [];
  return (
    <div className="space-y-3">
      <ul className="space-y-3" aria-label="Notification settings">
        {items.map((item) => {
          const id = `notification-preference-${item.event_code.replace('.', '-')}`;
          return (
            <li key={item.event_code} className="flex items-center justify-between gap-4">
              <Label htmlFor={id} className="text-sm font-normal">
                {EVENT_PREFERENCE_LABEL[item.event_code]}
              </Label>
              <Switch
                id={id}
                checked={item.enabled}
                disabled={update.isPending}
                onCheckedChange={(checked) =>
                  update.mutate([{ event_code: item.event_code, enabled: checked }])
                }
              />
            </li>
          );
        })}
      </ul>
      {update.isError ? (
        <p role="alert" className="text-xs text-destructive">
          {isDark(update.error)
            ? "Notifications aren't available in this workspace."
            : 'Your choice could not be saved. Try again.'}
        </p>
      ) : null}
    </div>
  );
}
