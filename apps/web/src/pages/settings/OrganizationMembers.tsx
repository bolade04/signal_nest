import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useCallback, useId, useMemo, useState } from 'react';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { OrganizationMemberOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { ConfirmDialog } from '@/components/common/confirm-dialog';
import { Field } from '@/components/common/form-field';
import { ErrorState, LoadingRows } from '@/components/common/states';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card';
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
  DialogTrigger,
} from '@/components/ui/dialog';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useToast } from '@/components/ui/toast';
import { label, roleLabels } from '@/lib/labels';
import {
  canAdminOrganization,
  canGrantRole,
  canInviteMembers,
  canManageMember,
  ROLES,
  useActorRole,
  type Role,
} from '@/lib/roles';
import { InviteMemberDialog, PendingInvitations } from './Invitations';
import {
  formatDate,
  isStaleMemberError,
  removeErrorMessage,
  roleChangeErrorMessage,
  soleOwnerId,
  useReturnFocus,
} from './member-admin';

// Member management for the ACTIVE organization (P6-AUTH-1). Settings mounts this
// keyed by the organization id, so switching organization remounts it: nothing here
// (open dialogs, a one-time invitation link, pending state) survives into another
// organization, and every query key is that organization's own.
//
// Which controls appear is decided only by lib/roles.ts, from the actor's session
// role in this organization. That is UX truthfulness, not enforcement: the API
// re-decides every request, and its refusals are handled below.

export function OrganizationMembers({
  organizationId,
  organizationName,
}: {
  organizationId: string;
  organizationName: string;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { user, refreshSession } = useAuth();
  const actor = useActorRole(organizationId);
  const isAdmin = canAdminOrganization(actor.role);
  const roleOptions = useMemo(() => ROLES.filter((role) => canGrantRole(actor.role, role)), [actor.role]);

  const members = useQuery({
    queryKey: queryKeys.organizationMembers(organizationId),
    queryFn: ({ signal }) => api.listOrganizationMembers(organizationId, signal),
  });

  const invalidateMembers = useCallback(
    () =>
      queryClient.invalidateQueries({
        queryKey: queryKeys.organizationMembers(organizationId),
        exact: true,
      }),
    [queryClient, organizationId],
  );

  // A refusal that means this screen's picture is out of date: someone changed a role
  // meanwhile, possibly the actor's own. Re-read this organization's member list and
  // the session; no other organization's cache is touched.
  const onStale = useCallback(() => {
    void invalidateMembers();
    refreshSession().catch(() => {
      // The current session stays in place; a 401 has already signed the user out.
    });
  }, [invalidateMembers, refreshSession]);

  const [toRemove, setToRemove] = useState<OrganizationMemberOut | null>(null);
  const rememberOpener = useReturnFocus(toRemove !== null);
  // Drop a pending removal the moment the actor may no longer administer (a session
  // re-read demoted them), so the confirmation is never offered to that role.
  if (!isAdmin && toRemove) setToRemove(null);

  const remove = useMutation({
    mutationFn: (member: OrganizationMemberOut) => api.removeMember(organizationId, member.user_id),
    onSuccess: async (_data, member) => {
      await invalidateMembers();
      toast({
        title: 'Member removed',
        description: `${member.full_name} no longer has access to ${organizationName}.`,
        intent: 'success',
      });
    },
    onError: (err) => {
      if (isStaleMemberError(err)) onStale();
      toast({ title: 'Could not remove member', description: removeErrorMessage(err), intent: 'error' });
    },
  });

  const onlyOwner = members.data ? soleOwnerId(members.data) : null;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader className="flex-row flex-wrap items-start justify-between gap-3 space-y-0">
          <div className="space-y-1.5">
            <CardTitle>Members</CardTitle>
            <CardDescription>{organizationName}</CardDescription>
          </div>
          {canInviteMembers(actor.role) ? (
            <InviteMemberDialog organizationId={organizationId} actorRole={actor.role} onStale={onStale} />
          ) : null}
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Roles apply to the whole organization and all of its workspaces.
          </p>
          {members.isPending ? (
            <LoadingRows rows={3} />
          ) : members.isError ? (
            <ErrorState error={members.error} onRetry={() => void members.refetch()} />
          ) : members.data.length === 0 ? (
            <p className="text-sm text-muted-foreground">No members to show.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-left text-sm">
                <caption className="sr-only">{`Members of ${organizationName}`}</caption>
                <thead className="text-xs text-muted-foreground">
                  <tr className="border-b border-border">
                    <th scope="col" className="px-2 py-2 font-medium">
                      Name
                    </th>
                    <th scope="col" className="px-2 py-2 font-medium">
                      Email
                    </th>
                    <th scope="col" className="px-2 py-2 font-medium">
                      Role
                    </th>
                    <th scope="col" className="px-2 py-2 font-medium">
                      Joined
                    </th>
                    {isAdmin ? (
                      <th scope="col" className="px-2 py-2">
                        <span className="sr-only">Actions</span>
                      </th>
                    ) : null}
                  </tr>
                </thead>
                <tbody>
                  {members.data.map((member) => {
                    const isSelf = member.user_id === user?.id;
                    const isOnlyOwner = member.user_id === onlyOwner;
                    // The loaded list outranks a possibly stale session here: an
                    // organization's only owner can be neither demoted nor removed.
                    const manageable =
                      !isOnlyOwner && canManageMember(actor.role, member.role, { isSelf });
                    return (
                      <tr key={member.user_id} className="border-b border-border last:border-0">
                        <th scope="row" className="px-2 py-2 font-medium">
                          {member.full_name}
                          {isSelf ? (
                            <span className="ml-1.5 text-xs font-normal text-muted-foreground">(you)</span>
                          ) : null}
                        </th>
                        <td className="px-2 py-2">{member.email}</td>
                        <td className="px-2 py-2">
                          <Badge intent="info">{label(roleLabels, member.role)}</Badge>
                        </td>
                        <td className="whitespace-nowrap px-2 py-2 text-muted-foreground">
                          <time dateTime={member.created_at}>{formatDate(member.created_at)}</time>
                        </td>
                        {isAdmin ? (
                          <td className="px-2 py-2 text-right">
                            {manageable ? (
                              <div className="flex justify-end gap-1">
                                <ChangeRoleDialog
                                  organizationId={organizationId}
                                  member={member}
                                  options={roleOptions}
                                  onStale={onStale}
                                />
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  aria-label={`Remove ${member.full_name}`}
                                  onClick={(event) => {
                                    rememberOpener(event.currentTarget);
                                    setToRemove(member);
                                  }}
                                >
                                  Remove
                                </Button>
                              </div>
                            ) : isOnlyOwner ? (
                              <span className="text-xs text-muted-foreground">
                                Only owner. An organization must keep at least one owner.
                              </span>
                            ) : (
                              <span className="sr-only">No actions available</span>
                            )}
                          </td>
                        ) : null}
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>

      <PendingInvitations organizationId={organizationId} actorRole={actor.role} onStale={onStale} />

      <ConfirmDialog
        open={isAdmin && toRemove !== null}
        onOpenChange={(next) => {
          if (!next) setToRemove(null);
        }}
        title={toRemove ? `Remove ${toRemove.full_name}?` : 'Remove member?'}
        description={
          toRemove
            ? `${toRemove.full_name} (${toRemove.email}) will lose access to ${organizationName} and all of its workspaces. Their account isn’t deleted, and you can invite them again later.`
            : undefined
        }
        confirmLabel="Remove member"
        destructive
        onConfirm={async () => {
          if (!toRemove) return;
          try {
            await remove.mutateAsync(toRemove);
          } catch {
            // Reported by the mutation's onError; the dialog closes either way.
          }
        }}
      />
    </div>
  );
}

function ChangeRoleDialog({
  organizationId,
  member,
  options,
  onStale,
}: {
  organizationId: string;
  member: OrganizationMemberOut;
  /** Every role the actor may grant, from lib/roles.ts. Never `owner` for an admin. */
  options: readonly Role[];
  onStale: () => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const formId = useId();
  const [open, setOpen] = useState(false);
  // Null until the actor picks a role; the select shows the current one meanwhile.
  const [picked, setPicked] = useState<Role | null>(null);
  const [error, setError] = useState<string | null>(null);

  const change = useMutation({
    mutationFn: (role: Role) => api.changeMemberRole(organizationId, member.user_id, { role }),
    onSuccess: async (updated) => {
      await queryClient.invalidateQueries({
        queryKey: queryKeys.organizationMembers(organizationId),
        exact: true,
      });
      toast({
        title: 'Role updated',
        description: `${updated.full_name} is now ${label(roleLabels, updated.role)}.`,
        intent: 'success',
      });
      setOpen(false);
      setPicked(null);
      setError(null);
    },
    onError: (err) => {
      const message = roleChangeErrorMessage(err);
      setError(message);
      if (isStaleMemberError(err)) {
        // The refresh below can take this dialog away (the actor lost the right to
        // manage this member, or the member is gone), and the inline error with it.
        // The toast lives outside the dialog, so the explanation always survives.
        toast({ title: 'Could not change role', description: message, intent: 'error' });
        onStale();
      }
    },
  });

  const handleOpenChange = (next: boolean) => {
    if (!next && change.isPending) return;
    setOpen(next);
    if (!next) {
      setPicked(null);
      setError(null);
      change.reset();
    }
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button size="sm" variant="ghost" aria-label={`Change role for ${member.full_name}`}>
          Change role
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Change role</DialogTitle>
          <DialogDescription>
            {`Choose a new role for ${member.full_name} (${member.email}). The role applies across the whole organization, including all of its workspaces.`}
          </DialogDescription>
        </DialogHeader>
        <form
          id={formId}
          onSubmit={(event) => {
            event.preventDefault();
            if (picked !== null) change.mutate(picked);
          }}
        >
          <Field label="Role" error={error ?? undefined}>
            {({ id, describedBy, invalid }) => (
              <Select
                value={picked ?? member.role}
                onValueChange={(value) => {
                  setPicked(value as Role);
                  setError(null);
                }}
              >
                <SelectTrigger id={id} aria-describedby={describedBy} aria-invalid={invalid}>
                  <SelectValue />
                </SelectTrigger>
                <SelectContent>
                  {options.map((role) => (
                    <SelectItem key={role} value={role}>
                      {label(roleLabels, role)}
                    </SelectItem>
                  ))}
                </SelectContent>
              </Select>
            )}
          </Field>
        </form>
        <DialogFooter>
          <Button
            type="button"
            variant="outline"
            onClick={() => handleOpenChange(false)}
            disabled={change.isPending}
          >
            Cancel
          </Button>
          <Button type="submit" form={formId} disabled={picked === null || change.isPending}>
            {change.isPending ? 'Saving…' : 'Save role'}
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  );
}
