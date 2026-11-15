import { Bell, Settings2 } from 'lucide-react';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import type { NotificationOut } from '@/api/types';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { Skeleton } from '@/components/ui/skeleton';
import { cn, formatRelative } from '@/lib/utils';
import { NotificationPreferencesDialog } from './NotificationPreferences';
import { EVENT_MESSAGE, EVENT_SECTION, unreadBadge } from './notificationVocabulary';
import {
  FEED_LIMIT,
  isDark,
  useFeedScope,
  useMarkNotificationRead,
  useNotificationFeed,
  useNotificationsAnalyticsCapability,
  useNotificationsVisible,
} from './useNotifications';

/**
 * The notification bell with the signed-in user's feed (Project Phase 5E, 5E-W3).
 *
 * The app shell (`components/layout/notifications.tsx`, freeze-owned; the I-5E integration
 * owner replaces it) renders this in place of its placeholder:
 *
 *     <NotificationsMenu fallback={<the present placeholder />} />
 *
 * While the workspace-effective reflection is not `enabled === true` (off, loading, failed, no
 * workspace, no session), this renders `fallback` and sends no feed request. Only the reflection
 * itself is read (`GET /workspaces/{ws}/notifications-analytics-capability`), which answers 200
 * even while dark.
 */
export function NotificationsMenu({ fallback = null }: { fallback?: React.ReactNode }) {
  const { workspaceId, userId } = useFeedScope();
  const visible = useNotificationsVisible();
  if (!visible || !workspaceId || !userId) return <>{fallback}</>;
  // Keyed by the scope: a workspace or user switch remounts with nothing carried over.
  return (
    <NotificationsMenuInner
      key={`${workspaceId}:${userId}`}
      workspaceId={workspaceId}
      userId={userId}
    />
  );
}

function NotificationsMenuInner({ workspaceId, userId }: { workspaceId: string; userId: string }) {
  const feed = useNotificationFeed(workspaceId, userId, true);
  const [preferencesOpen, setPreferencesOpen] = useState(false);
  const unread = feed.data?.unread ?? 0;
  const badge = unreadBadge(unread);

  return (
    <>
      <DropdownMenu
        onOpenChange={(open) => {
          if (open) void feed.refetch();
        }}
      >
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            size="icon"
            className="relative"
            aria-label={badge ? `Notifications, ${unread} unread` : 'Notifications'}
          >
            <Bell className="size-4" />
            {badge ? (
              <span
                aria-hidden="true"
                className="absolute -right-0.5 -top-0.5 flex h-4 min-w-4 items-center justify-center rounded-full bg-primary px-1 text-[10px] font-semibold leading-none text-primary-foreground"
              >
                {badge}
              </span>
            ) : null}
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-80">
          <DropdownMenuLabel>Notifications</DropdownMenuLabel>
          <NotificationFeed workspaceId={workspaceId} userId={userId} />
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => setPreferencesOpen(true)}>
            <Settings2 className="size-4" aria-hidden="true" />
            Notification settings
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      {preferencesOpen ? (
        <NotificationPreferencesDialog
          workspaceId={workspaceId}
          userId={userId}
          onClose={() => setPreferencesOpen(false)}
        />
      ) : null}
    </>
  );
}

/**
 * The signed-in user's feed in one workspace, newest first, as menu items: render it inside a
 * `DropdownMenu` (the bell above). Choosing an item marks it read and, when the feed can name
 * a section for its event, opens that section.
 *
 * Self-guarding: it reads the reflection itself and sends no feed request unless it is
 * `enabled === true`. A 503 from the feed (the capability turned off since) shows the same
 * "not available" line as a reflection that is off.
 */
export function NotificationFeed({ workspaceId, userId }: { workspaceId: string; userId: string }) {
  const capability = useNotificationsAnalyticsCapability(workspaceId);
  const feed = useNotificationFeed(workspaceId, userId, capability.isEnabled);
  const markRead = useMarkNotificationRead(workspaceId, userId);
  const navigate = useNavigate();

  if (capability.status === 'loading' || (capability.isEnabled && feed.isPending)) {
    return (
      <div className="space-y-2 px-2 py-3" aria-busy="true" aria-live="polite">
        <span className="sr-only">Loading notifications…</span>
        <Skeleton className="h-4 w-full" />
        <Skeleton className="h-4 w-2/3" />
      </div>
    );
  }
  if (!capability.isEnabled || (feed.isError && isDark(feed.error))) {
    return (
      <p role="status" className="px-2 py-6 text-center text-sm text-muted-foreground">
        Notifications aren&apos;t available in this workspace.
      </p>
    );
  }
  if (feed.isError) {
    return (
      <div role="alert" className="px-2 py-4 text-center text-sm">
        <p className="text-muted-foreground">Your notifications could not be loaded.</p>
        <Button variant="outline" size="sm" className="mt-2" onClick={() => void feed.refetch()}>
          Try again
        </Button>
      </div>
    );
  }

  const page = feed.data;
  if (!page || page.items.length === 0) {
    return (
      <p role="status" className="px-2 py-6 text-center text-sm text-muted-foreground">
        You&apos;re all caught up.
      </p>
    );
  }

  function choose(item: NotificationOut) {
    if (item.read_at === null) markRead.mutate(item.id);
    const section = EVENT_SECTION[item.event_code];
    if (section) navigate(section);
  }

  return (
    <div aria-label="Your notifications" role="group">
      {page.items.map((item) => {
        const unread = item.read_at === null;
        return (
          <DropdownMenuItem
            key={item.id}
            onSelect={() => choose(item)}
            className="items-start gap-2"
          >
            <span
              aria-hidden="true"
              className={cn(
                'mt-1.5 size-2 shrink-0 rounded-full',
                unread ? 'bg-primary' : 'bg-transparent',
              )}
            />
            <span className="flex min-w-0 flex-col">
              <span className={cn('text-sm', unread && 'font-medium')}>
                {unread ? <span className="sr-only">Unread: </span> : null}
                {EVENT_MESSAGE[item.event_code]}
              </span>
              <span className="text-xs text-muted-foreground">{formatRelative(item.created_at)}</span>
            </span>
          </DropdownMenuItem>
        );
      })}
      {page.total > page.items.length ? (
        <p className="px-2 pt-1 text-xs text-muted-foreground">
          Showing the latest {Math.min(page.items.length, FEED_LIMIT)} of {page.total}.
        </p>
      ) : null}
      {markRead.isError && !isDark(markRead.error) ? (
        <p role="alert" className="px-2 pt-1 text-xs text-destructive">
          That notification could not be marked read. Try again.
        </p>
      ) : null}
    </div>
  );
}
