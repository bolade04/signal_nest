import { zodResolver } from '@hookform/resolvers/zod';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Check, Copy, UserPlus } from 'lucide-react';
import { useId, useMemo, useRef, useState } from 'react';
import { Controller, useForm } from 'react-hook-form';
import { z } from 'zod';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { InvitationOut } from '@/api/types';
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
import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { useToast } from '@/components/ui/toast';
import { label, roleLabels } from '@/lib/labels';
import { canInviteMembers, invitableRoles, type InvitableRole, type RoleInput } from '@/lib/roles';
import { formatDateTime } from '@/lib/utils';
import { apiErrorCode, buildInviteLink } from '@/pages/invite/invite-token';
import {
  inviteError,
  isForbidden,
  revokeErrorMessage,
  useReturnFocus,
  type InviteError,
} from './member-admin';

// Invitations for the active organization (P6-AUTH-1). There is no email transport:
// an administrator creates an invitation, copies the one-time link and shares it
// themselves. The raw token exists only in the create response, so the link is shown
// once and is unrecoverable afterwards.

interface InviteValues {
  email: string;
  role: InvitableRole;
}

interface InviteResult {
  email: string;
  expiresAt: string;
  /** The one-time share link. It embeds the raw token, so it lives only in this dialog's state. */
  link: string;
}

export function InviteMemberDialog({
  organizationId,
  actorRole,
  onStale,
}: {
  organizationId: string;
  actorRole: RoleInput;
  /** The actor's own authority may be out of date: re-read the member list and the session. */
  onStale: () => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const formId = useId();
  const formErrorId = useId();
  // Never `owner`: the policy module leaves it out of the set entirely.
  const roles = useMemo(() => invitableRoles(actorRole), [actorRole]);
  const [open, setOpen] = useState(false);
  const [serverError, setServerError] = useState<InviteError | null>(null);
  const [result, setResult] = useState<InviteResult | null>(null);

  const schema = useMemo(
    () =>
      z.object({
        email: z.string().trim().min(1, 'Enter an email address').email('Enter a valid email address'),
        role: z.custom<InvitableRole>(
          (value) => roles.includes(value as InvitableRole),
          'Choose a role',
        ),
      }),
    [roles],
  );

  const form = useForm<InviteValues>({
    resolver: zodResolver(schema),
    // Least privilege by default: the options run most privileged first.
    defaultValues: { email: '', role: roles[roles.length - 1] },
  });

  const create = useMutation({
    mutationFn: (values: InviteValues) =>
      api.createInvitation(organizationId, { email: values.email, role: values.role }),
    // This response is the only one that carries the raw token. With gcTime 0 the
    // MutationCache drops it as soon as nothing observes the mutation, and `reset()`
    // detaches it twice: the moment the link is in this dialog's own state, and
    // again when the dialog closes.
    gcTime: 0,
    onSuccess: () => {
      void queryClient.invalidateQueries({
        queryKey: queryKeys.organizationInvitations(organizationId),
        exact: true,
      });
    },
    onError: (err) => {
      const failure = inviteError(err);
      setServerError(failure);
      const code = apiErrorCode(err);
      if (code === 'invitation_pending_exists') {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.organizationInvitations(organizationId),
          exact: true,
        });
      } else if (code === 'invitation_already_member') {
        void queryClient.invalidateQueries({
          queryKey: queryKeys.organizationMembers(organizationId),
          exact: true,
        });
      }
      if (isForbidden(err)) {
        // The refresh below can revoke the actor's right to invite, which unmounts
        // this dialog and its inline error. The toast lives outside the dialog, so
        // the explanation always survives. No token exists on this path.
        toast({ title: 'Could not create invitation', description: failure.message, intent: 'error' });
        onStale();
      }
    },
  });

  const handleOpenChange = (next: boolean) => {
    // Never close mid-request: the one-time link would arrive with nowhere to show it.
    if (!next && create.isPending) return;
    setOpen(next);
    if (!next) {
      setResult(null);
      setServerError(null);
      create.reset();
      form.reset();
    }
  };

  const onSubmit = form.handleSubmit((values) => {
    setServerError(null);
    create.mutate(values, {
      onSuccess: (created) => {
        setResult({
          email: created.email,
          expiresAt: created.expires_at,
          link: buildInviteLink(window.location.origin, created.token),
        });
        create.reset();
      },
    });
  });

  const clearServerError = (field: InviteError['field']) =>
    setServerError((current) => (current?.field === field || current?.field === 'form' ? null : current));

  if (roles.length === 0) return null;

  const emailMessage =
    form.formState.errors.email?.message ??
    (serverError?.field === 'email' ? serverError.message : undefined);
  const roleMessage =
    form.formState.errors.role?.message ??
    (serverError?.field === 'role' ? serverError.message : undefined);
  const formError = serverError?.field === 'form' ? serverError.message : null;

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button size="sm">
          <UserPlus className="size-4" /> Invite member
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-md">
        {result ? (
          <InviteLinkResult result={result} onDone={() => handleOpenChange(false)} />
        ) : (
          <>
            <DialogHeader>
              <DialogTitle>Invite member</DialogTitle>
              <DialogDescription>
                SignalNest doesn’t deliver invitations by email. After you create one, you’ll get a
                link to copy and share with the person yourself.
              </DialogDescription>
            </DialogHeader>
            <form
              id={formId}
              noValidate
              onSubmit={onSubmit}
              aria-describedby={formError ? formErrorId : undefined}
              className="space-y-4"
            >
              <Field label="Email" error={emailMessage} required>
                {({ id, describedBy, invalid }) => (
                  <Input
                    id={id}
                    type="email"
                    autoComplete="off"
                    aria-describedby={describedBy}
                    aria-invalid={invalid}
                    {...form.register('email', { onChange: () => clearServerError('email') })}
                  />
                )}
              </Field>
              <Field
                label="Role"
                description="Applies to the whole organization and all of its workspaces."
                error={roleMessage}
                required
              >
                {({ id, describedBy, invalid }) => (
                  <Controller
                    control={form.control}
                    name="role"
                    render={({ field }) => (
                      <Select
                        value={field.value}
                        onValueChange={(value) => {
                          field.onChange(value);
                          clearServerError('role');
                        }}
                      >
                        <SelectTrigger
                          id={id}
                          aria-describedby={describedBy}
                          aria-invalid={invalid}
                          onBlur={field.onBlur}
                        >
                          <SelectValue placeholder="Choose a role" />
                        </SelectTrigger>
                        <SelectContent>
                          {roles.map((role) => (
                            <SelectItem key={role} value={role}>
                              {label(roleLabels, role)}
                            </SelectItem>
                          ))}
                        </SelectContent>
                      </Select>
                    )}
                  />
                )}
              </Field>
              {formError ? (
                <p id={formErrorId} role="alert" className="text-sm font-medium text-destructive">
                  {formError}
                </p>
              ) : null}
            </form>
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                onClick={() => handleOpenChange(false)}
                disabled={create.isPending}
              >
                Cancel
              </Button>
              <Button type="submit" form={formId} disabled={create.isPending}>
                {create.isPending ? 'Creating…' : 'Create invitation'}
              </Button>
            </DialogFooter>
          </>
        )}
      </DialogContent>
    </Dialog>
  );
}

function InviteLinkResult({ result, onDone }: { result: InviteResult; onDone: () => void }) {
  const [copy, setCopy] = useState<'idle' | 'copied' | 'failed'>('idle');
  const linkRef = useRef<HTMLInputElement>(null);

  const copyLink = async () => {
    try {
      // `navigator.clipboard` is missing outside a secure context; that throws here too.
      await navigator.clipboard.writeText(result.link);
      setCopy('copied');
    } catch {
      setCopy('failed');
      linkRef.current?.focus();
      linkRef.current?.select();
    }
  };

  return (
    <>
      <DialogHeader>
        <DialogTitle>Invitation created</DialogTitle>
        <DialogDescription asChild>
          <div className="space-y-1">
            <p>No email was sent.</p>
            <p>{`Copy this link and send it securely to ${result.email}.`}</p>
          </div>
        </DialogDescription>
      </DialogHeader>
      <Field label="Invitation link">
        {({ id, describedBy }) => (
          <Input
            id={id}
            ref={linkRef}
            readOnly
            value={result.link}
            aria-describedby={describedBy}
            onFocus={(event) => event.currentTarget.select()}
            className="font-mono text-xs"
          />
        )}
      </Field>
      <div className="flex flex-wrap items-center gap-3">
        {/* Focus lands here when the result replaces the form. */}
        <Button type="button" onClick={copyLink} autoFocus>
          <Copy className="size-4" /> Copy invitation link
        </Button>
        <p role="status" className="text-sm">
          {copy === 'copied' ? (
            <span className="inline-flex items-center gap-1 text-success">
              <Check className="size-4" aria-hidden /> Link copied
            </span>
          ) : copy === 'failed' ? (
            <span className="text-destructive">
              Couldn’t copy automatically. The link is selected above; copy it with your keyboard or
              the context menu.
            </span>
          ) : null}
        </p>
      </div>
      <p className="rounded-md border border-border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
        {`For security, this link can’t be shown again after you close this window. It expires ${formatDateTime(result.expiresAt)}. If it’s lost, revoke the invitation and create a new one.`}
      </p>
      <DialogFooter>
        <Button type="button" variant="outline" onClick={onDone}>
          Done
        </Button>
      </DialogFooter>
    </>
  );
}

export function PendingInvitations({
  organizationId,
  actorRole,
  onStale,
}: {
  organizationId: string;
  actorRole: RoleInput;
  /** The actor's own authority may be out of date: re-read the member list and the session. */
  onStale: () => void;
}) {
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const canList = canInviteMembers(actorRole);

  const query = useQuery({
    queryKey: queryKeys.organizationInvitations(organizationId),
    queryFn: ({ signal }) => api.listInvitations(organizationId, signal),
    // The list route admits exactly owner and admin; never request it for anyone else.
    enabled: canList,
  });

  const [toRevoke, setToRevoke] = useState<InvitationOut | null>(null);
  const rememberOpener = useReturnFocus(toRevoke !== null);

  const invalidate = () =>
    queryClient.invalidateQueries({
      queryKey: queryKeys.organizationInvitations(organizationId),
      exact: true,
    });

  const revoke = useMutation({
    mutationFn: (invitation: InvitationOut) => api.revokeInvitation(organizationId, invitation.id),
    onSuccess: async (_data, invitation) => {
      await invalidate();
      toast({
        title: 'Invitation revoked',
        description: `The invitation link for ${invitation.email} no longer works.`,
        intent: 'success',
      });
    },
    onError: (err) => {
      // Whatever the refusal (already revoked, expired, accepted, gone), the list is stale.
      void invalidate();
      if (isForbidden(err)) onStale();
      toast({
        title: 'Could not revoke invitation',
        description: revokeErrorMessage(err),
        intent: 'error',
      });
    },
  });

  // Drop a pending revocation the moment the actor may no longer manage invitations,
  // so it cannot spring back open if that permission returns.
  if (!canList && toRevoke) setToRevoke(null);
  if (!canList) return null;

  return (
    <Card>
      <CardHeader>
        <CardTitle>Pending invitations</CardTitle>
        <CardDescription>
          Invitations that haven’t been accepted yet. A link is shown only once, when its invitation
          is created.
        </CardDescription>
      </CardHeader>
      <CardContent>
        {query.isPending ? (
          <LoadingRows rows={2} />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : query.data.length === 0 ? (
          <p className="text-sm text-muted-foreground">No pending invitations.</p>
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-left text-sm">
              <caption className="sr-only">Pending invitations</caption>
              <thead className="text-xs text-muted-foreground">
                <tr className="border-b border-border">
                  <th scope="col" className="px-2 py-2 font-medium">
                    Email
                  </th>
                  <th scope="col" className="px-2 py-2 font-medium">
                    Role
                  </th>
                  <th scope="col" className="px-2 py-2 font-medium">
                    Created
                  </th>
                  <th scope="col" className="px-2 py-2 font-medium">
                    Expires
                  </th>
                  <th scope="col" className="px-2 py-2">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {query.data.map((invitation) => (
                  <tr key={invitation.id} className="border-b border-border last:border-0">
                    <td className="px-2 py-2 font-medium">{invitation.email}</td>
                    <td className="px-2 py-2">
                      <Badge intent="muted">{label(roleLabels, invitation.role)}</Badge>
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 text-muted-foreground">
                      <time dateTime={invitation.created_at}>
                        {formatDateTime(invitation.created_at)}
                      </time>
                    </td>
                    <td className="whitespace-nowrap px-2 py-2 text-muted-foreground">
                      <time dateTime={invitation.expires_at}>
                        {formatDateTime(invitation.expires_at)}
                      </time>
                    </td>
                    <td className="px-2 py-2 text-right">
                      <Button
                        size="sm"
                        variant="ghost"
                        aria-label={`Revoke invitation for ${invitation.email}`}
                        onClick={(event) => {
                          rememberOpener(event.currentTarget);
                          setToRevoke(invitation);
                        }}
                      >
                        Revoke
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardContent>

      <ConfirmDialog
        open={toRevoke !== null}
        onOpenChange={(next) => {
          if (!next) setToRevoke(null);
        }}
        title="Revoke this invitation?"
        description={
          toRevoke
            ? `The invitation link for ${toRevoke.email} will stop working. You can create a new invitation afterwards.`
            : undefined
        }
        confirmLabel="Revoke invitation"
        destructive
        onConfirm={async () => {
          if (!toRevoke) return;
          try {
            await revoke.mutateAsync(toRevoke);
          } catch {
            // Reported by the mutation's onError; the dialog closes either way.
          }
        }}
      />
    </Card>
  );
}
