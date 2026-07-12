import { Bell } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';

// Placeholder surface — real-time notifications are a later-phase capability.
export function Notifications() {
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
