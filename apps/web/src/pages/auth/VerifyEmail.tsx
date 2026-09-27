import { zodResolver } from '@hookform/resolvers/zod';
import { AlertTriangle, CheckCircle2, UserX } from 'lucide-react';
import { useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link } from 'react-router-dom';
import { z } from 'zod';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { useAuth } from '@/auth/AuthContext';
import { Field } from '@/components/common/form-field';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import {
  ACCOUNT_TOKEN_ERROR_COPY,
  accountTokenErrorCode,
  calmFailureMessage,
  useFragmentToken,
} from './account-token';
import { AuthLayout } from './AuthLayout';

const signInSchema = z.object({
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
  password: z.string().min(1, 'Password is required'),
});
type SignInValues = z.infer<typeof signInSchema>;

type Stage = 'ready' | 'wrong-account' | 'refused' | 'verified';

/**
 * Public `/verify-email` route (P6-AUTH-2). The token arrives in the `#token=`
 * fragment and lives only in memory. Confirming needs the session of the token's
 * own account, so a signed-out visitor signs in right here (the token stays in
 * memory, never in a `/sign-in` URL), and the token is spent only by an explicit
 * click — never by a render or an effect.
 */
export function VerifyEmailPage() {
  const { token, generation, discard } = useFragmentToken();
  return (
    <AuthLayout title="Verify your email address" subtitle="Confirm that this email address belongs to you.">
      {/* A newly opened link starts the page over. */}
      <VerifyEmailFlow key={generation} token={token} onDone={discard} />
    </AuthLayout>
  );
}

function VerifyEmailFlow({ token, onDone }: { token: string | null; onDone: () => void }) {
  const { status, login, logout, refreshSession } = useAuth();
  const [stage, setStage] = useState<Stage>('ready');
  const [notice, setNotice] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);
  const inFlight = useRef(false);
  const signedIn = status === 'authenticated';

  const verify = async () => {
    if (!token || inFlight.current) return;
    inFlight.current = true;
    setActionError(null);
    setVerifying(true);
    try {
      await api.confirmEmailVerification({ token });
    } catch (err) {
      inFlight.current = false;
      setVerifying(false);
      const code = accountTokenErrorCode(err);
      if (code === 'email_verification_invalid') {
        // Unknown, expired, used or replaced: nothing on this page can make it work.
        onDone();
        setStage('refused');
      } else if (code === 'email_verification_wrong_account') {
        // Nothing was spent; the right account can still use the link.
        setStage('wrong-account');
      } else if (err instanceof ApiError && err.status === 401) {
        // The session ended. Sign in again right here; the token stays in memory.
        logout();
        setNotice('Your session has ended. Sign in again to verify your email address.');
      } else {
        setActionError(calmFailureMessage(err, 'Your email address could not be verified. Please try again.'));
      }
      return;
    }
    // The address is verified: re-read the session so the app knows (the banner goes).
    try {
      await refreshSession();
    } catch {
      // The verification stands either way; the next session read reports it.
    }
    inFlight.current = false;
    setVerifying(false);
    onDone();
    setStage('verified');
  };

  const submitSignIn = async (values: SignInValues) => {
    setActionError(null);
    try {
      // Success re-renders this page as the explicit verify step.
      await login(values);
      setNotice(null);
    } catch (err) {
      setActionError(
        err instanceof ApiError && err.status === 401
          ? 'Incorrect email or password.'
          : calmFailureMessage(err, 'Unable to sign in. Please try again.'),
      );
    }
  };

  const signOut = () => {
    // The app's existing sign-out. The token stays in this page's memory so the
    // account the link was sent to can sign in here next.
    logout();
    setStage('ready');
    setNotice(null);
    setActionError(null);
  };

  if (stage === 'verified') return <VerifiedPanel signedIn={signedIn} />;
  if (stage === 'refused') {
    return <LinkPanel {...ACCOUNT_TOKEN_ERROR_COPY.email_verification_invalid} signedIn={signedIn} />;
  }
  if (status === 'loading') return <Checking text="Checking your session…" />;
  if (!token) {
    return (
      <LinkPanel
        title="This verification link is unavailable or has expired"
        description={
          signedIn
            ? 'Request a new one from the banner at the top of the app.'
            : 'Sign in and request a new one from the banner.'
        }
        signedIn={signedIn}
      />
    );
  }
  if (!signedIn) return <SignInForm notice={notice} error={actionError} onSubmit={submitSignIn} />;
  if (stage === 'wrong-account') return <WrongAccountPanel onSignOut={signOut} />;
  return <ConfirmPanel busy={verifying} error={actionError} onVerify={() => void verify()} />;
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

function HomeLink({ signedIn }: { signedIn: boolean }) {
  return signedIn ? (
    <Button asChild className="w-full">
      <Link to="/">Go to SignalNest</Link>
    </Button>
  ) : (
    <Button asChild variant="outline" className="w-full">
      <Link to="/sign-in">Go to sign in</Link>
    </Button>
  );
}

function LinkPanel({ title, description, signedIn }: { title: string; description: string; signedIn: boolean }) {
  return (
    <section className="space-y-4">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-5 shrink-0 text-warning" aria-hidden />
        <div className="space-y-1">
          <h2 className="text-base font-semibold">{title}</h2>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
      </div>
      <HomeLink signedIn={signedIn} />
    </section>
  );
}

function VerifiedPanel({ signedIn }: { signedIn: boolean }) {
  return (
    <section className="space-y-4">
      <div className="flex items-start gap-3">
        <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-success" aria-hidden />
        <div className="space-y-1">
          <h2 className="text-base font-semibold">Your email address is verified.</h2>
          <p className="text-sm text-muted-foreground">There is nothing more to do.</p>
        </div>
      </div>
      <HomeLink signedIn={signedIn} />
    </section>
  );
}

function ConfirmPanel({
  busy,
  error,
  onVerify,
}: {
  busy: boolean;
  error: string | null;
  onVerify: () => void;
}) {
  return (
    <section className="space-y-4">
      <p className="text-sm text-muted-foreground">
        Confirm that the email address of the account you are signed in with belongs to you.
      </p>
      {error ? <FormAlert message={error} /> : null}
      <Button className="w-full" onClick={onVerify} disabled={busy} aria-busy={busy}>
        {busy ? <Spinner className="size-4 text-current" /> : null}
        {busy ? 'Verifying…' : 'Verify email'}
      </Button>
    </section>
  );
}

function WrongAccountPanel({ onSignOut }: { onSignOut: () => void }) {
  const copy = ACCOUNT_TOKEN_ERROR_COPY.email_verification_wrong_account;
  return (
    <section className="space-y-4">
      <div className="flex items-start gap-3">
        <UserX className="mt-0.5 size-5 shrink-0 text-warning" aria-hidden />
        <div className="space-y-1">
          <h2 className="text-base font-semibold">{copy.title}</h2>
          <p className="text-sm">{copy.description}</p>
          <p className="text-sm text-muted-foreground">
            To use it, sign out, then sign in with the account the link was sent to.
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

function SignInForm({
  notice,
  error,
  onSubmit,
}: {
  notice: string | null;
  error: string | null;
  onSubmit: (values: SignInValues) => Promise<void>;
}) {
  const form = useForm<SignInValues>({
    resolver: zodResolver(signInSchema),
    defaultValues: { email: '', password: '' },
  });

  return (
    <form onSubmit={form.handleSubmit(onSubmit)} className="space-y-4" noValidate>
      <h2 className="text-base font-semibold">Sign in to verify your email address</h2>
      {notice ? (
        <p role="status" className="rounded-md border border-border bg-secondary/50 px-3 py-2 text-sm">
          {notice}
        </p>
      ) : null}
      {error ? <FormAlert message={error} /> : null}

      <Field
        label="Email"
        description="Sign in with the account the link was sent to."
        error={form.formState.errors.email?.message}
        required
      >
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="email"
            autoComplete="email"
            aria-describedby={describedBy}
            aria-invalid={invalid}
            {...form.register('email')}
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
    </form>
  );
}
