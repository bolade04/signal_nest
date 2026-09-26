import { zodResolver } from '@hookform/resolvers/zod';
import { useQueryClient } from '@tanstack/react-query';
import { AlertTriangle, CheckCircle2, UserX } from 'lucide-react';
import { useCallback, useEffect, useLayoutEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link, useLocation, useNavigate } from 'react-router-dom';
import { z } from 'zod';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { queryKeys } from '@/api/queryKeys';
import type { InvitationPreviewOut } from '@/api/types';
import { useAuth } from '@/auth/AuthContext';
import { Field } from '@/components/common/form-field';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { useToast } from '@/components/ui/toast';
import { label, roleLabels } from '@/lib/labels';
import { formatDateTime } from '@/lib/utils';
import { AuthLayout } from '@/pages/auth/AuthLayout';
import { useWorkspace } from '@/workspace/WorkspaceContext';
import {
  INVITATION_ERROR_COPY,
  fragmentHasInviteToken,
  invitationErrorCode,
  isPlausibleInviteToken,
  parseInviteFragment,
  scrubInviteFragment,
} from './invite-token';
import type { InvitationErrorCode } from './invite-token';

/** Outcomes that end the flow: nothing on this page can make the invitation acceptable. */
const UNAVAILABLE_CODES = [
  'invitation_invalid',
  'invitation_expired',
  'invitation_revoked',
  'invitation_already_used',
  'invitation_already_member',
  'invitation_inviter_not_authorized',
] as const satisfies readonly InvitationErrorCode[];

type UnavailableCode = (typeof UNAVAILABLE_CODES)[number];

function isUnavailableCode(code: InvitationErrorCode | null): code is UnavailableCode {
  return code !== null && (UNAVAILABLE_CODES as readonly string[]).includes(code);
}

type Stage =
  | { kind: 'previewing' }
  | { kind: 'preview-failed'; message: string }
  | { kind: 'unavailable'; code: UnavailableCode }
  | { kind: 'ready'; preview: InvitationPreviewOut }
  | { kind: 'joined'; preview: InvitationPreviewOut; entering: 'pending' | 'failed' };

function initialStage(token: string | null): Stage {
  return isPlausibleInviteToken(token)
    ? { kind: 'previewing' }
    : { kind: 'unavailable', code: 'invitation_invalid' };
}

/** Only the sanitized message survives into state — never the error object or its payload. */
function failureMessage(err: unknown, fallback: string): string {
  return err instanceof ApiError ? err.message : fallback;
}

// Bounds mirror InvitationRegisterRequest, so a 422 (which echoes its input) is never provoked.
const registerSchema = z.object({
  full_name: z.string().min(1, 'Your name is required').max(200, 'Use at most 200 characters'),
  password: z.string().min(8, 'Use at least 8 characters').max(128, 'Use at most 128 characters'),
});
type RegisterValues = z.infer<typeof registerSchema>;

const signInSchema = z.object({
  password: z.string().min(1, 'Password is required'),
});
type SignInValues = z.infer<typeof signInSchema>;

/**
 * Public `/invite` route (P6-AUTH-1): preview an invitation, then create the
 * invited account or sign in, and accept. Also usable while signed in.
 *
 * The token arrives in the `#token=` fragment and lives only in this component's
 * memory. It is never rendered, stored, logged or put into a query or mutation; it
 * leaves memory only in the JSON body of the preview, register and accept calls.
 */
export function InvitePage() {
  const location = useLocation();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const { toast } = useToast();
  const { status, user, memberships, login, logout, registerWithInvitation, acceptInvitation } =
    useAuth();
  const { setOrganizationId } = useWorkspace();

  // Captured once, from the address bar (falling back to the router's copy of the
  // same URL). The initializer only reads: StrictMode renders twice before anything
  // is scrubbed, and its simulated remount keeps this state, so the token survives
  // both while the address bar is still scrubbed before the first paint.
  const [token, setToken] = useState<string | null>(
    () => parseInviteFragment(window.location.hash) ?? parseInviteFragment(location.hash),
  );
  const [stage, setStage] = useState<Stage>(() => initialStage(token));
  const [previewAttempt, setPreviewAttempt] = useState(0);
  const [mode, setMode] = useState<'register' | 'sign-in'>('register');
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pending, setPending] = useState<'registering' | 'accepting' | null>(null);
  // The router location the token was read from, or the latest one seen since.
  const [seenLocation, setSeenLocation] = useState(location);

  // Start over with the token of a newly opened link (the same link opened again
  // previews again).
  const adoptToken = useCallback((next: string | null) => {
    setToken(next);
    setStage(initialStage(next));
    setPreviewAttempt((n) => n + 1);
    setMode('register');
    setNotice(null);
    setActionError(null);
  }, []);

  // A second link opened in this tab is a fragment-only navigation: no reload and
  // no remount. Its token is adopted only from a NEW router location — never from
  // the one the current token was read from, which the router can still hold until
  // the sync below replaces it — so a scrubbed token cannot come back.
  if (location !== seenLocation) {
    setSeenLocation(location);
    const hash = fragmentHasInviteToken(window.location.hash) ? window.location.hash : location.hash;
    if (fragmentHasInviteToken(hash)) adoptToken(parseInviteFragment(hash));
  }

  // The router only follows popstate; the address bar's own hashchange is the
  // signal when nothing else re-renders this page. Under BrowserRouter the popstate
  // that precedes it has normally been handled above already, and the fragment
  // scrubbed, so this finds nothing left to adopt.
  useEffect(() => {
    const onHashChange = () => {
      if (!fragmentHasInviteToken(window.location.hash)) return;
      adoptToken(parseInviteFragment(window.location.hash));
      scrubInviteFragment();
    };
    window.addEventListener('hashchange', onHashChange);
    return () => window.removeEventListener('hashchange', onHashChange);
  }, [adoptToken]);

  // A layout effect runs before the paint and before the passive preview effect
  // below, so the token is out of the address bar before any request. It re-runs
  // after every capture opportunity; idempotent, so StrictMode's replay is harmless.
  useLayoutEffect(() => {
    scrubInviteFragment();
  }, [token, location]);

  // React Router still holds the URL it was created with; replace it too, so that
  // useLocation().hash is '' as well. A passive effect on purpose: layout effects
  // run child-first, so during this page's layout effects the Router has not yet
  // subscribed to its history, and a navigation there would move the history
  // without updating the router's state. Nothing is carried over: no hash, no state.
  useEffect(() => {
    if (!fragmentHasInviteToken(location.hash)) return;
    navigate(
      { pathname: location.pathname, search: location.search, hash: '' },
      { replace: true, state: null },
    );
  }, [location.hash, location.pathname, location.search, navigate]);

  // Preview is read-only on the server: it spends nothing, so StrictMode's
  // aborted-and-repeated first request is harmless.
  useEffect(() => {
    if (!isPlausibleInviteToken(token)) return;
    const controller = new AbortController();
    api
      .previewInvitation({ token }, controller.signal)
      .then((preview) => setStage({ kind: 'ready', preview }))
      .catch((err: unknown) => {
        if (controller.signal.aborted) return;
        const code = invitationErrorCode(err);
        setStage(
          isUnavailableCode(code)
            ? { kind: 'unavailable', code }
            : { kind: 'preview-failed', message: failureMessage(err, 'Something went wrong.') },
        );
      });
    return () => controller.abort();
  }, [token, previewAttempt]);

  const retryPreview = () => {
    setStage({ kind: 'previewing' });
    setPreviewAttempt((n) => n + 1);
  };

  const switchMode = (next: 'register' | 'sign-in') => {
    setMode(next);
    setNotice(null);
    setActionError(null);
  };

  // Make the joined organization the active one, then go into the app. The
  // workspace is then chosen by WorkspaceContext's existing rule.
  const enterJoinedOrganization = async (preview: InvitationPreviewOut) => {
    const orgId = preview.organization_id;
    setStage({ kind: 'joined', preview, entering: 'pending' });
    try {
      // The list must contain the joined organization BEFORE it is selected:
      // WorkspaceContext falls back to another organization during render when the
      // selected id is missing from the loaded list. `exact`, because the list key
      // is a prefix of every organization-scoped key. Cancelling first stops a list
      // request made under the previous session from answering for this one.
      await queryClient.cancelQueries({ queryKey: queryKeys.organizations, exact: true });
      const organizations = await queryClient.fetchQuery({
        queryKey: queryKeys.organizations,
        queryFn: ({ signal }) => api.listOrganizations(signal),
        staleTime: 0,
      });
      if (!organizations.some((o) => o.id === orgId)) {
        throw new Error('The joined organization is not in the organization list.');
      }
      // Anything this browser cached for the organization predates the membership.
      await Promise.all([
        queryClient.invalidateQueries({ queryKey: queryKeys.workspaces(orgId), exact: true }),
        queryClient.invalidateQueries({
          queryKey: queryKeys.organizationMembers(orgId),
          exact: true,
        }),
      ]);
    } catch {
      // Never leave the user in some other organization under a success message.
      setStage({ kind: 'joined', preview, entering: 'failed' });
      return;
    }
    setOrganizationId(orgId);
    toast({
      title: `Welcome to ${preview.organization_name}`,
      description: `You joined as ${label(roleLabels, preview.role)}.`,
      intent: 'success',
    });
    navigate('/', { replace: true });
  };

  // Map a failed register or accept to the page state it implies.
  const handleActionError = (err: unknown) => {
    const code = invitationErrorCode(err);
    if (isUnavailableCode(code)) {
      setStage({ kind: 'unavailable', code });
      return;
    }
    if (code === 'invitation_account_exists') {
      // Sign in right here: the token stays in memory, never in a /sign-in URL.
      setMode('sign-in');
      setNotice(INVITATION_ERROR_COPY.invitation_account_exists.description);
      return;
    }
    if (err instanceof ApiError && err.status === 401) {
      setMode('sign-in');
      setNotice('Your session has ended. Sign in again to accept the invitation.');
      return;
    }
    if (err instanceof ApiError && err.status === 403) {
      setActionError(
        'This invitation is for a different email address than the account you are signed in with.',
      );
      return;
    }
    setActionError(failureMessage(err, 'Something went wrong. Please try again.'));
  };

  const submitRegistration = async (preview: InvitationPreviewOut, values: RegisterValues) => {
    if (!isPlausibleInviteToken(token)) return;
    setActionError(null);
    setPending('registering');
    try {
      // Exactly these three fields: the email, organization and role are the
      // invitation's own, and the backend rejects anything else.
      await registerWithInvitation({
        token,
        full_name: values.full_name,
        password: values.password,
      });
    } catch (err) {
      setPending(null);
      handleActionError(err);
      return;
    }
    setPending(null);
    await enterJoinedOrganization(preview);
  };

  const submitSignIn = async (preview: InvitationPreviewOut, values: SignInValues) => {
    setActionError(null);
    try {
      // Only the invited account can accept, so that is the account to sign in as.
      // Success re-renders this page as the explicit accept step.
      await login({ email: preview.email, password: values.password });
      setNotice(null);
    } catch (err) {
      setActionError(
        err instanceof ApiError && err.status === 401
          ? 'Incorrect email or password.'
          : failureMessage(err, 'Unable to sign in. Please try again.'),
      );
    }
  };

  const accept = async (preview: InvitationPreviewOut) => {
    if (!isPlausibleInviteToken(token)) return;
    setActionError(null);
    setPending('accepting');
    try {
      await acceptInvitation({ token });
    } catch (err) {
      setPending(null);
      handleActionError(err);
      return;
    }
    setPending(null);
    await enterJoinedOrganization(preview);
  };

  const signOut = () => {
    // The app's existing sign-out. The token stays in this page's memory so the
    // invited account can sign in here next.
    logout();
    switchMode('sign-in');
  };

  let content: React.ReactNode;
  if (stage.kind === 'unavailable') {
    content = <UnavailablePanel code={stage.code} signedIn={status === 'authenticated'} />;
  } else if (stage.kind === 'preview-failed') {
    content = <PreviewFailedPanel message={stage.message} onRetry={retryPreview} />;
  } else if (stage.kind === 'previewing') {
    content = <Checking text="Checking your invitation…" />;
  } else if (stage.kind === 'joined') {
    const { preview } = stage;
    content = (
      <JoinedPanel
        preview={preview}
        failed={stage.entering === 'failed'}
        onRetry={() => void enterJoinedOrganization(preview)}
      />
    );
  } else {
    const { preview } = stage;
    // A registration in flight keeps its form: the session it creates must not
    // swap the page to the accept step before the joined organization is entered.
    if (pending !== 'registering' && status === 'loading') {
      content = <Checking text="Checking your session…" />;
    } else if (pending !== 'registering' && status === 'authenticated' && user) {
      // Exact comparison, as the backend's own check: both addresses are stored
      // through the same normalization.
      if (user.email !== preview.email) {
        content = (
          <WrongAccountPanel
            invitedEmail={preview.email}
            currentEmail={user.email}
            onSignOut={signOut}
          />
        );
      } else if (
        pending !== 'accepting' &&
        memberships.some((m) => m.organization_id === preview.organization_id)
      ) {
        content = <UnavailablePanel code="invitation_already_member" signedIn />;
      } else {
        content = (
          <AcceptPanel
            preview={preview}
            currentEmail={user.email}
            busy={pending === 'accepting'}
            error={actionError}
            onAccept={() => void accept(preview)}
          />
        );
      }
    } else if (mode === 'sign-in' && pending !== 'registering') {
      content = (
        <SignInForm
          preview={preview}
          notice={notice}
          error={actionError}
          onSubmit={(values) => submitSignIn(preview, values)}
          onCreateAccount={() => switchMode('register')}
        />
      );
    } else {
      content = (
        <RegisterForm
          preview={preview}
          error={actionError}
          onSubmit={(values) => submitRegistration(preview, values)}
          onSignIn={() => switchMode('sign-in')}
        />
      );
    }
  }

  return (
    <AuthLayout
      title="Your invitation"
      subtitle="An administrator of an organization on SignalNest shared this invitation link with you."
    >
      {content}
    </AuthLayout>
  );
}

function Checking({ text }: { text: string }) {
  return (
    <div aria-live="polite" className="flex items-center gap-3 text-sm text-muted-foreground">
      <Spinner className="size-5" />
      <span>{text}</span>
    </div>
  );
}

function FormAlert({ message }: { message: string }) {
  return (
    <div
      role="alert"
      className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
    >
      {message}
    </div>
  );
}

function InvitationSummary({ preview }: { preview: InvitationPreviewOut }) {
  return (
    <div className="space-y-2">
      <dl className="grid grid-cols-[auto,1fr] gap-x-4 gap-y-2 rounded-md border border-border bg-card/50 p-4 text-sm">
        <dt className="text-muted-foreground">Organization</dt>
        <dd className="font-medium">{preview.organization_name}</dd>
        <dt className="text-muted-foreground">Invited email</dt>
        <dd className="break-all">{preview.email}</dd>
        <dt className="text-muted-foreground">Role</dt>
        <dd>{label(roleLabels, preview.role)}</dd>
        <dt className="text-muted-foreground">Expires</dt>
        <dd>{formatDateTime(preview.expires_at)}</dd>
      </dl>
      <p className="text-xs text-muted-foreground">
        Roles apply to the whole organization and all of its workspaces.
      </p>
    </div>
  );
}

function UnavailablePanel({ code, signedIn }: { code: UnavailableCode; signedIn: boolean }) {
  const copy = INVITATION_ERROR_COPY[code];
  return (
    <section className="space-y-4">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-5 shrink-0 text-warning" aria-hidden />
        <div className="space-y-1">
          <h2 className="text-base font-semibold">{copy.title}</h2>
          <p className="text-sm text-muted-foreground">{copy.description}</p>
        </div>
      </div>
      {signedIn ? (
        <Button asChild className="w-full">
          <Link to="/">Go to SignalNest</Link>
        </Button>
      ) : (
        <Button asChild variant="outline" className="w-full">
          <Link to="/sign-in">Go to sign in</Link>
        </Button>
      )}
    </section>
  );
}

function PreviewFailedPanel({ message, onRetry }: { message: string; onRetry: () => void }) {
  return (
    <section role="alert" className="space-y-4">
      <div className="space-y-1">
        <h2 className="text-base font-semibold">We could not check this invitation</h2>
        <p className="text-sm text-muted-foreground">{message}</p>
      </div>
      <Button variant="outline" className="w-full" onClick={onRetry}>
        Try again
      </Button>
    </section>
  );
}

function AcceptPanel({
  preview,
  currentEmail,
  busy,
  error,
  onAccept,
}: {
  preview: InvitationPreviewOut;
  currentEmail: string;
  busy: boolean;
  error: string | null;
  onAccept: () => void;
}) {
  return (
    <section className="space-y-4">
      <div className="space-y-1">
        <h2 className="text-lg font-semibold">Join {preview.organization_name}</h2>
        <p className="text-sm">
          Role: <span className="font-medium">{label(roleLabels, preview.role)}</span>
        </p>
      </div>
      <InvitationSummary preview={preview} />
      <p className="text-sm text-muted-foreground">Signed in as {currentEmail}.</p>
      {error ? <FormAlert message={error} /> : null}
      <Button className="w-full" onClick={onAccept} disabled={busy} aria-busy={busy}>
        {busy ? <Spinner className="size-4 text-current" /> : null}
        {busy ? 'Accepting…' : 'Accept invitation'}
      </Button>
    </section>
  );
}

function WrongAccountPanel({
  invitedEmail,
  currentEmail,
  onSignOut,
}: {
  invitedEmail: string;
  currentEmail: string;
  onSignOut: () => void;
}) {
  return (
    <section className="space-y-4">
      <div className="flex items-start gap-3">
        <UserX className="mt-0.5 size-5 shrink-0 text-warning" aria-hidden />
        <div className="space-y-1">
          <h2 className="text-base font-semibold">This invitation is for a different account</h2>
          <p className="text-sm">
            {`This invitation is for ${invitedEmail}, but you are signed in as ${currentEmail}.`}
          </p>
          <p className="text-sm text-muted-foreground">
            {`To accept it, sign out, then sign in as ${invitedEmail} or create that account.`}
          </p>
        </div>
      </div>
      <div className="flex flex-col gap-2 sm:flex-row">
        <Button className="flex-1" onClick={onSignOut}>
          Sign out
        </Button>
        <Button asChild variant="outline" className="flex-1">
          <Link to="/">Go to SignalNest</Link>
        </Button>
      </div>
    </section>
  );
}

function JoinedPanel({
  preview,
  failed,
  onRetry,
}: {
  preview: InvitationPreviewOut;
  failed: boolean;
  onRetry: () => void;
}) {
  return (
    <section className="space-y-4">
      <div className="flex items-start gap-3">
        <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-success" aria-hidden />
        <div className="space-y-1">
          <h2 className="text-base font-semibold">Invitation accepted</h2>
          <p className="text-sm text-muted-foreground">
            {`You joined ${preview.organization_name} as ${label(roleLabels, preview.role)}.`}
          </p>
        </div>
      </div>
      {failed ? (
        <>
          <FormAlert message={`${preview.organization_name} could not be opened automatically.`} />
          <Button className="w-full" onClick={onRetry}>
            Open {preview.organization_name}
          </Button>
        </>
      ) : (
        <Checking text={`Opening ${preview.organization_name}…`} />
      )}
    </section>
  );
}

function RegisterForm({
  preview,
  error,
  onSubmit,
  onSignIn,
}: {
  preview: InvitationPreviewOut;
  error: string | null;
  onSubmit: (values: RegisterValues) => Promise<void>;
  onSignIn: () => void;
}) {
  const form = useForm<RegisterValues>({
    resolver: zodResolver(registerSchema),
    defaultValues: { full_name: '', password: '' },
  });

  return (
    <div className="space-y-6">
      <InvitationSummary preview={preview} />
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4" noValidate>
        <h2 className="text-base font-semibold">
          Create your account to join {preview.organization_name}
        </h2>
        {error ? <FormAlert message={error} /> : null}

        <Field label="Full name" error={form.formState.errors.full_name?.message} required>
          {({ id, describedBy, invalid }) => (
            <Input
              id={id}
              autoComplete="name"
              aria-describedby={describedBy}
              aria-invalid={invalid}
              {...form.register('full_name')}
            />
          )}
        </Field>

        <Field label="Email" description="The invitation is for this address, so it cannot be changed.">
          {({ id, describedBy }) => (
            <Input
              id={id}
              type="email"
              value={preview.email}
              readOnly
              aria-readonly="true"
              autoComplete="username"
              aria-describedby={describedBy}
            />
          )}
        </Field>

        <Field
          label="Password"
          description="At least 8 characters."
          error={form.formState.errors.password?.message}
          required
        >
          {({ id, describedBy, invalid }) => (
            <Input
              id={id}
              type="password"
              autoComplete="new-password"
              aria-describedby={describedBy}
              aria-invalid={invalid}
              {...form.register('password')}
            />
          )}
        </Field>

        <Button type="submit" className="w-full" disabled={form.formState.isSubmitting}>
          {form.formState.isSubmitting ? <Spinner className="size-4 text-current" /> : null}
          Create account and join
        </Button>

        <p className="text-center text-sm text-muted-foreground">
          Already have an account?{' '}
          <button
            type="button"
            className="font-medium text-primary hover:underline"
            onClick={onSignIn}
          >
            Sign in to accept
          </button>
        </p>
      </form>
    </div>
  );
}

function SignInForm({
  preview,
  notice,
  error,
  onSubmit,
  onCreateAccount,
}: {
  preview: InvitationPreviewOut;
  notice: string | null;
  error: string | null;
  onSubmit: (values: SignInValues) => Promise<void>;
  onCreateAccount: () => void;
}) {
  const form = useForm<SignInValues>({
    resolver: zodResolver(signInSchema),
    defaultValues: { password: '' },
  });

  return (
    <div className="space-y-6">
      <InvitationSummary preview={preview} />
      <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4" noValidate>
        <h2 className="text-base font-semibold">Sign in to accept the invitation</h2>
        {notice ? (
          <p role="status" className="rounded-md border border-border bg-secondary/50 px-3 py-2 text-sm">
            {notice}
          </p>
        ) : null}
        {error ? <FormAlert message={error} /> : null}

        <Field label="Email" description="Only this account can accept the invitation.">
          {({ id, describedBy }) => (
            <Input
              id={id}
              type="email"
              value={preview.email}
              readOnly
              aria-readonly="true"
              autoComplete="username"
              aria-describedby={describedBy}
            />
          )}
        </Field>

        <Field label="Password" error={form.formState.errors.password?.message} required>
          {({ id, describedBy, invalid }) => (
            <Input
              id={id}
              type="password"
              autoComplete="current-password"
              aria-describedby={describedBy}
              aria-invalid={invalid}
              {...form.register('password')}
            />
          )}
        </Field>

        <Button type="submit" className="w-full" disabled={form.formState.isSubmitting}>
          {form.formState.isSubmitting ? <Spinner className="size-4 text-current" /> : null}
          Sign in
        </Button>

        <p className="text-center text-sm text-muted-foreground">
          New to SignalNest?{' '}
          <button
            type="button"
            className="font-medium text-primary hover:underline"
            onClick={onCreateAccount}
          >
            Create your account
          </button>
        </p>
      </form>
    </div>
  );
}
