import { Bell } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { NotificationsMenu } from '@/pages/notifications/NotificationFeed';

/**
 * The shell's notification bell (Project Phase 5E, I-5E). It renders lane 5E-W3's feed menu
 * while the workspace-effective O-12(B) reflection says notifications are on for the active
 * workspace and the signed-in user is known. Otherwise — off, loading, failed, no workspace —
 * it renders the placeholder below and sends no feed request. UX only: every notification
 * route answers 503 while the capability is off.
 */
export function Notifications() {
  return <NotificationsMenu fallback={<NotificationsPlaceholder />} />;
}

/** The bell as it was before 5E: an empty dropdown that sends nothing. */
function NotificationsPlaceholder() {
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="ghost" size="icon" aria-label="Notifications">
          <Bell className="size-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-72">
        <DropdownMenuLabel>Notifications</DropdownMenuLabel>
        <div className="px-2 py-6 text-center text-sm text-muted-foreground">
          You&apos;re all caught up.
          <p className="mt-1 text-xs">Live alerts arrive in a later phase.</p>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
