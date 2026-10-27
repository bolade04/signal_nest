import { zodResolver } from '@hookform/resolvers/zod';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { Download, Trash2 } from 'lucide-react';
import { useEffect, useId, useRef, useState, type MutableRefObject } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router-dom';
import { z } from 'zod';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { MembershipImpactOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { Field } from '@/components/common/form-field';
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
import { Spinner } from '@/components/ui/spinner';
import { useToast } from '@/components/ui/toast';
import { downloadJson, exportFilename } from '@/lib/download';
import { assembleOrganizationExport } from '@/lib/organization-export';
import { canManageOrganizationData, useActorRole } from '@/lib/roles';
import { ACCOUNT_DELETED_NOTICE, calmFailureMessage } from '@/pages/auth/account-token';
import { apiErrorCode } from '@/pages/invite/invite-token';

// P6-PRIV-1 — data export and deletion from Settings.
//
// The password lives only in the open dialog's form and in the JSON body of the one
// request; it is never a query key, never stored, never in the URL. A failed request is
// reduced to a message at once; the ApiError itself is never kept in state.
//
// Account deletion: a 200 means the account no longer exists, so every token of it is
// refused — this tab clears its session LOCALLY (no /auth/logout: it would answer 401) and
// goes to sign-in with a notice. Organization deletion: the account survives; the session
// is re-read so the deleted organization leaves the membership list, then home.

const CURRENT_INCORRECT = 'The current password is incorrect.';
const UNREACHABLE =
  'The server could not be reached, so we can’t tell whether the deletion happened. Reload the page to check.';

function failureMessage(error: unknown, fallback: string): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return 'Your session has ended. Sign in again to continue.';
    if (error.status === 422 || error.status === 409) return error.message;
    if (error.status === 0) return UNREACHABLE;
  }
  return calmFailureMessage(error, fallback);
}

// The account's address is unverified, so the API leaves data found only by that address
// (invitations addressed to it that it did not accept, audit records naming it): it may be
// another person's. Invitations the account accepted are its own by id and always go.
const UNVERIFIED_EMAIL_COPY =
  'Your email address is not verified. Invitations you accepted will be deleted with your account, but other invitations addressed to it and other records that name it will not. Verify your address first to include them.';

const IMPACT_COPY: Record<MembershipImpactOut['outcome'], (name: string) => string> = {
  membership_removed: (name) => `You will leave ${name}.`,
  organization_deleted: (name) =>
    `${name} will be deleted with all of its data: you are its only member.`,
  blocked_sole_owner: (name) =>
    `You are the only owner of ${name}, which has other members. Make another member an owner, or delete the organization, first.`,
};

/**
 * "Download" for one export, with its own pending state, optional progress line and failure
 * toast. `run` receives a progress reporter and an abort signal (raised when the button
 * unmounts); a rejection shows the toast and downloads nothing.
 */
function ExportButton({
  label,
  run,
}: {
  label: string;
  run: (report: (text: string) => void, signal: AbortSignal) => Promise<void>;
}) {
  const [pending, setPending] = useState(false);
  const [progress, setProgress] = useState('');
  const { toast } = useToast();
  const controller = useRef<AbortController | null>(null);
  useEffect(() => () => controller.current?.abort(), []);
  const onClick = async () => {
    if (pending) return;
    setPending(true);
    setProgress('');
    const ctl = new AbortController();
    controller.current = ctl;
    try {
      await run(setProgress, ctl.signal);
    } catch (err) {
      if (!ctl.signal.aborted) {
        toast({
          title: 'Could not download your data',
          description: calmFailureMessage(err, 'Please try again.'),
          intent: 'error',
        });
      }
    } finally {
      setPending(false);
      setProgress('');
    }
  };
  return (
    <>
      <Button size="sm" variant="outline" onClick={() => void onClick()} disabled={pending} aria-busy={pending}>
        {pending ? <Spinner className="size-4 text-current" /> : <Download className="size-4" />}
        {label}
      </Button>
      <span role="status" aria-live="polite" className="text-sm text-muted-foreground">
        {pending ? progress : ''}
      </span>
    </>
  );
}

// --------------------------------------------------------------------------- //
// Account
// --------------------------------------------------------------------------- //

/** The Account card's export and delete controls (every signed-in account). */
export function AccountDataControls({ email }: { email: string }) {
  return (
    <div className="flex flex-wrap gap-2 pt-2">
      <ExportButton
        label="Download my data"
        run={async () => downloadJson(exportFilename('account'), await api.exportAccount())}
      />
      <DeleteAccountDialog email={email} />
    </div>
  );
}

const accountSchema = z.object({
  current_password: z.string().min(1, 'Enter your current password').max(128, 'Use at most 128 characters'),
  // An explicit boolean: an inferred type predicate would narrow the form value to 'DELETE'.
  confirmation: z.string().refine((v): boolean => v === 'DELETE', 'Type DELETE, in capitals, to confirm'),
});
type AccountValues = z.infer<typeof accountSchema>;

function DeleteAccountDialog({ email }: { email: string }) {
  const [open, setOpen] = useState(false);
  const inFlightRef = useRef(false);
  const handleOpenChange = (next: boolean) => {
    // Never close mid-request: its answer decides whether this account still exists.
    if (!next && inFlightRef.current) return;
    setOpen(next);
  };
  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline">
          <Trash2 className="size-4" /> Delete account
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Delete account</DialogTitle>
          <DialogDescription>
            This permanently deletes your account and signs you out everywhere. It cannot be undone.
          </DialogDescription>
        </DialogHeader>
        {/* Mounted only while open: closing discards everything typed. */}
        {open ? (
          <DeleteAccountForm email={email} inFlightRef={inFlightRef} onCancel={() => handleOpenChange(false)} />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function DeleteAccountForm({
  email,
  inFlightRef,
  onCancel,
}: {
  email: string;
  inFlightRef: MutableRefObject<boolean>;
  onCancel: () => void;
}) {
  const { endSessionAfterCredentialChange } = useAuth();
  const navigate = useNavigate();
  const formErrorId = useId();
  const [pending, setPending] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const preview = useQuery({
    queryKey: queryKeys.accountDeletionPreview,
    queryFn: ({ signal }) => api.getAccountDeletionPreview(signal),
    staleTime: 0,
  });
  const form = useForm<AccountValues>({
    resolver: zodResolver(accountSchema),
    defaultValues: { current_password: '', confirmation: '' },
  });
  const blocked = preview.data ? !preview.data.can_delete : true;

  const submit = async (values: AccountValues) => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setPending(true);
    setFormError(null);
    try {
      await api.deleteAccount({ current_password: values.current_password, confirmation: 'DELETE' });
    } catch (err) {
      inFlightRef.current = false;
      setPending(false);
      const code = apiErrorCode(err);
      if (code === 'current_password_incorrect') {
        form.setError('current_password', { type: 'server', message: CURRENT_INCORRECT }, { shouldFocus: true });
        return;
      }
      if (code === 'account_deletion_sole_owner') void preview.refetch();
      setFormError(failureMessage(err, 'Your account could not be deleted. Please try again.'));
      return;
    }
    // The account no longer exists: forget this tab's session and hand over to sign-in.
    endSessionAfterCredentialChange();
    navigate('/sign-in', { replace: true, state: { notice: ACCOUNT_DELETED_NOTICE } });
  };

  return (
    <form
      noValidate
      onSubmit={(event) => void form.handleSubmit(submit)(event)}
      aria-describedby={formError ? formErrorId : undefined}
      className="space-y-4"
    >
      {preview.isLoading ? (
        <p className="text-sm text-muted-foreground">Checking your organizations…</p>
      ) : preview.data ? (
        preview.data.organizations.length ? (
          <ul aria-label="What happens to your organizations" className="list-disc space-y-1 pl-5 text-sm">
            {preview.data.organizations.map((o) => (
              <li key={o.organization_id}>{IMPACT_COPY[o.outcome](o.organization_name)}</li>
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">You are not a member of any organization.</p>
        )
      ) : (
        <p role="alert" className="text-sm text-destructive">
          We couldn’t check what deleting your account would do. Close this and try again.
        </p>
      )}
      {preview.data?.email_keyed_data.status === 'excluded_unverified' ? (
        <p className="text-sm text-muted-foreground">{UNVERIFIED_EMAIL_COPY}</p>
      ) : null}

      <Field label="Email">
        {({ id }) => <Input id={id} type="email" value={email} readOnly aria-readonly="true" autoComplete="username" />}
      </Field>
      <Field label="Current password" error={form.formState.errors.current_password?.message} required>
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="password"
            autoComplete="current-password"
            aria-describedby={describedBy}
            aria-invalid={invalid}
            {...form.register('current_password')}
          />
        )}
      </Field>
      <Field label="Type DELETE to confirm" error={form.formState.errors.confirmation?.message} required>
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            autoComplete="off"
            aria-describedby={describedBy}
            aria-invalid={invalid}
            {...form.register('confirmation')}
          />
        )}
      </Field>

      {formError ? (
        <p id={formErrorId} role="alert" className="text-sm font-medium text-destructive">
          {formError}
        </p>
      ) : null}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel} disabled={pending}>
          Cancel
        </Button>
        <Button type="submit" variant="destructive" disabled={pending || blocked} aria-busy={pending}>
          {pending ? <Spinner className="size-4 text-current" /> : null}
          Delete my account
        </Button>
      </DialogFooter>
    </form>
  );
}

// --------------------------------------------------------------------------- //
// Organization (exactly OWNER)
// --------------------------------------------------------------------------- //

/** Export and delete the ACTIVE organization. Renders nothing for anyone but its owner. */
export function OrganizationDataControls({
  organizationId,
  organizationName,
}: {
  organizationId: string;
  organizationName: string;
}) {
  const { role } = useActorRole(organizationId);
  if (!canManageOrganizationData(role)) return null;
  return (
    <Card>
      <CardHeader>
        <CardTitle>Organization data</CardTitle>
        <CardDescription>
          Download everything {organizationName} holds (large organizations are fetched in pages, so it can take a moment), or delete the organization and all of its workspaces.
        </CardDescription>
      </CardHeader>
      <CardContent className="flex flex-wrap gap-2">
        <ExportButton
          label="Download organization data"
          run={async (report, signal) => {
            // Complete, by pages (D-9f): every table is read to its end, then one file is made.
            const file = await assembleOrganizationExport(
              {
                index: (s) => api.exportOrganization(organizationId, s),
                page: (table, cursor, limit, s) =>
                  api.exportOrganizationTablePage(organizationId, table, cursor, limit, s),
              },
              {
                signal,
                onProgress: (p) =>
                  report(
                    `Preparing your download: ${p.tablesDone} of ${p.tablesTotal} tables, ` +
                      `${p.rowsReceived.toLocaleString('en-US')} of ${p.rowsExpected.toLocaleString('en-US')} rows`,
                  ),
              },
            );
            downloadJson(exportFilename('organization'), file);
          }}
        />
        <DeleteOrganizationDialog organizationId={organizationId} organizationName={organizationName} />
      </CardContent>
    </Card>
  );
}

const organizationSchema = z.object({
  current_password: z.string().min(1, 'Enter your current password').max(128, 'Use at most 128 characters'),
  confirm_organization_name: z.string().min(1, 'Type the organization’s name to confirm').max(200),
});
type OrganizationValues = z.infer<typeof organizationSchema>;

function DeleteOrganizationDialog({
  organizationId,
  organizationName,
}: {
  organizationId: string;
  organizationName: string;
}) {
  const [open, setOpen] = useState(false);
  const inFlightRef = useRef(false);
  const handleOpenChange = (next: boolean) => {
    if (!next && inFlightRef.current) return;
    setOpen(next);
  };
  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline">
          <Trash2 className="size-4" /> Delete organization
        </Button>
      </DialogTrigger>
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>Delete organization</DialogTitle>
          <DialogDescription>
            This permanently deletes {organizationName}, every workspace in it and all of their data. Every member
            loses access; their accounts are kept. It cannot be undone.
          </DialogDescription>
        </DialogHeader>
        {open ? (
          <DeleteOrganizationForm
            organizationId={organizationId}
            organizationName={organizationName}
            inFlightRef={inFlightRef}
            onCancel={() => handleOpenChange(false)}
            onDeleted={() => setOpen(false)}
          />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function DeleteOrganizationForm({
  organizationId,
  organizationName,
  inFlightRef,
  onCancel,
  onDeleted,
}: {
  organizationId: string;
  organizationName: string;
  inFlightRef: MutableRefObject<boolean>;
  onCancel: () => void;
  onDeleted: () => void;
}) {
  const { refreshSession } = useAuth();
  const queryClient = useQueryClient();
  const navigate = useNavigate();
  const { toast } = useToast();
  const formErrorId = useId();
  const [pending, setPending] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const form = useForm<OrganizationValues>({
    resolver: zodResolver(organizationSchema),
    defaultValues: { current_password: '', confirm_organization_name: '' },
  });

  const submit = async (values: OrganizationValues) => {
    if (inFlightRef.current) return;
    if (values.confirm_organization_name !== organizationName) {
      form.setError('confirm_organization_name', {
        type: 'manual',
        message: 'Type the organization’s name exactly to confirm.',
      });
      return;
    }
    inFlightRef.current = true;
    setPending(true);
    setFormError(null);
    try {
      await api.deleteOrganization(organizationId, {
        current_password: values.current_password,
        confirm_organization_name: values.confirm_organization_name,
      });
    } catch (err) {
      inFlightRef.current = false;
      setPending(false);
      const code = apiErrorCode(err);
      if (code === 'current_password_incorrect') {
        form.setError('current_password', { type: 'server', message: CURRENT_INCORRECT }, { shouldFocus: true });
        return;
      }
      setFormError(failureMessage(err, 'The organization could not be deleted. Please try again.'));
      return;
    }
    inFlightRef.current = false;
    onDeleted();
    toast({ title: `${organizationName} was deleted`, intent: 'success' });
    // The organization is gone from the server: drop everything cached about it, re-read the
    // session so it leaves the membership list, then start over at home.
    queryClient.removeQueries({ queryKey: [...queryKeys.organizations, organizationId] });
    await queryClient.invalidateQueries({ queryKey: queryKeys.organizations, exact: true });
    await refreshSession().catch(() => undefined);
    navigate('/', { replace: true });
  };

  return (
    <form
      noValidate
      onSubmit={(event) => void form.handleSubmit(submit)(event)}
      aria-describedby={formError ? formErrorId : undefined}
      className="space-y-4"
    >
      <Field label="Current password" error={form.formState.errors.current_password?.message} required>
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="password"
            autoComplete="current-password"
            aria-describedby={describedBy}
            aria-invalid={invalid}
            {...form.register('current_password')}
          />
        )}
      </Field>
      <Field
        label="Organization name"
        description={`Type ${organizationName} to confirm.`}
        error={form.formState.errors.confirm_organization_name?.message}
        required
      >
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            autoComplete="off"
            aria-describedby={describedBy}
            aria-invalid={invalid}
            {...form.register('confirm_organization_name')}
          />
        )}
      </Field>

      {formError ? (
        <p id={formErrorId} role="alert" className="text-sm font-medium text-destructive">
          {formError}
        </p>
      ) : null}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel} disabled={pending}>
          Cancel
        </Button>
        <Button type="submit" variant="destructive" disabled={pending} aria-busy={pending}>
          {pending ? <Spinner className="size-4 text-current" /> : null}
          Delete organization
        </Button>
      </DialogFooter>
    </form>
  );
}
