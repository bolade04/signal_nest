import { LogOut, Settings, UserRound } from 'lucide-react';
import { useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { ConfirmDialog } from '@/components/common/confirm-dialog';
import { Button } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import { useToast } from '@/components/ui/toast';
import { useAuth } from '@/auth/AuthContext';
import { type SignOutNotice, signOutEverywhereNotice, signOutNotice } from '@/auth/sign-out';
import { roleLabels } from '@/lib/labels';
import { useWorkspace } from '@/workspace/WorkspaceContext';

function initials(name: string): string {
  return name
    .split(' ')
    .map((p) => p[0])
    .filter(Boolean)
    .slice(0, 2)
    .join('')
    .toUpperCase();
}

export function UserMenu() {
  const { user, memberships, signOut, signOutEverywhere, setIntendedPath } = useAuth();
  const { organizationId } = useWorkspace();
  const navigate = useNavigate();
  const location = useLocation();
  const { toast } = useToast();
  const [confirmingEverywhere, setConfirmingEverywhere] = useState(false);

  if (!user) return null;
  const role = memberships.find((m) => m.organization_id === organizationId)?.role;

  // Once signed out, ProtectedRoute sends this page to /sign-in and remembers it. A
  // sign-out with something to say goes there itself, remembering the page the same way.
  const toSignIn = (notice: SignOutNotice | null) => {
    if (!notice) return;
    setIntendedPath(location.pathname + location.search);
    navigate('/sign-in', { replace: true, state: { notice } });
  };

  const signOutHere = async () => {
    toSignIn(signOutNotice(await signOut()));
  };

  const signOutOfEverySession = async () => {
    const outcome = await signOutEverywhere();
    if (outcome === 'failed') {
      toast({
        title: 'Could not sign out everywhere',
        description: 'Nothing was signed out. Check your connection and try again.',
        intent: 'error',
      });
      return;
    }
    toSignIn(signOutEverywhereNotice(outcome));
  };

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <Button
            variant="ghost"
            className="h-9 gap-2 px-2"
            aria-label="Account menu"
          >
            <span className="flex size-7 items-center justify-center rounded-full bg-primary/15 text-xs font-semibold text-primary">
              {initials(user.full_name)}
            </span>
            <span className="hidden text-sm font-medium sm:inline">{user.full_name}</span>
          </Button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="end" className="w-56">
          <DropdownMenuLabel className="font-normal">
            <div className="flex flex-col">
              <span className="text-sm font-semibold text-foreground">{user.full_name}</span>
              <span className="truncate text-xs text-muted-foreground">{user.email}</span>
              {role ? (
                <span className="mt-1 text-xs text-muted-foreground">
                  {roleLabels[role] ?? role}
                </span>
              ) : null}
            </div>
          </DropdownMenuLabel>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => navigate('/settings')}>
            <UserRound /> Profile &amp; account
          </DropdownMenuItem>
          <DropdownMenuItem onSelect={() => navigate('/settings')}>
            <Settings /> Workspace settings
          </DropdownMenuItem>
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => void signOutHere()} className="text-destructive focus:text-destructive">
            <LogOut /> Sign out
          </DropdownMenuItem>
          <DropdownMenuItem
            onSelect={() => setConfirmingEverywhere(true)}
            className="text-destructive focus:text-destructive"
          >
            <LogOut /> Sign out everywhere
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <ConfirmDialog
        open={confirmingEverywhere}
        onOpenChange={setConfirmingEverywhere}
        title="Sign out everywhere?"
        description="This signs your account out on every device and browser where it is signed in, including this one."
        confirmLabel="Sign out everywhere"
        destructive
        onConfirm={signOutOfEverySession}
      />
    </>
  );
}
