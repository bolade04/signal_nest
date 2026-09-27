import { zodResolver } from '@hookform/resolvers/zod';
import { AlertTriangle } from 'lucide-react';
import { useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link, useNavigate } from 'react-router-dom';
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
  PASSWORD_RESET_NOTICE,
  accountTokenErrorCode,
  calmFailureMessage,
  useFragmentToken,
} from './account-token';
import { AuthLayout } from './AuthLayout';

// Bounds mirror PasswordResetConfirmRequest, so a 422 (which echoes its input) is never provoked.
const schema = z
  .object({
    new_password: z.string().min(8, 'Use at least 8 characters').max(128, 'Use at most 128 characters'),
    confirm_password: z.string().min(1, 'Confirm your new password'),
  })
  .refine((values) => values.new_password === values.confirm_password, {
    message: 'The passwords do not match',
    path: ['confirm_password'],
  });

type FormValues = z.infer<typeof schema>;

/**
 * Public `/reset-password` route (P6-AUTH-2), usable while signed in. The token
 * arrives in the `#token=` fragment and lives only in memory; it leaves memory only
 * in the JSON body of the confirm call, which is sent only by an explicit submit.
 */
export function ResetPasswordPage() {
  const { token, generation, discard } = useFragmentToken();
  return (
    <AuthLayout title="Choose a new password" subtitle="Set the password you will sign in with from now on.">
      {/* A newly opened link starts the page over. */}
      <ResetPasswordFlow key={generation} token={token} onRefused={discard} />
    </AuthLayout>
  );
}

function ResetPasswordFlow({ token, onRefused }: { token: string | null; onRefused: () => void }) {
  const { logout } = useAuth();
  const navigate = useNavigate();
  const [refused, setRefused] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const inFlight = useRef(false);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { new_password: '', confirm_password: '' },
  });

  if (refused) return <LinkPanel {...ACCOUNT_TOKEN_ERROR_COPY.password_reset_invalid} />;
  if (!token) {
    return (
      <LinkPanel
        title="This reset link is unavailable or has expired"
        description="Open the complete link from the message you received, or request a new link."
      />
    );
  }

  // Guarded by a ref against a second submit before the disabled button renders, so
  // the form builds this handler per submit event rather than during render.
  const submit = async (values: FormValues) => {
    if (inFlight.current) return;
    inFlight.current = true;
    setFormError(null);
    try {
      await api.confirmPasswordReset({ token, new_password: values.new_password });
    } catch (err) {
      inFlight.current = false;
      if (accountTokenErrorCode(err) === 'password_reset_invalid') {
        // Unknown, expired, used or replaced: nothing on this page can make it work.
        onRefused();
        setRefused(true);
        return;
      }
      setFormError(
        err instanceof ApiError && err.status === 422
          ? err.message
          : calmFailureMessage(err, 'Your password could not be changed. Please try again.'),
      );
      return;
    }
    // The reset ended every session of the account and returned none. Sign this tab
    // out too (session token and cached data), then sign in with the new password.
    logout();
    navigate('/sign-in', { replace: true, state: { notice: PASSWORD_RESET_NOTICE } });
  };

  return (
    <form onSubmit={(event) => void form.handleSubmit(submit)(event)} className="space-y-4" noValidate>
      <p className="text-sm text-muted-foreground">
        Changing your password signs the account out everywhere, including this browser.
      </p>
      {formError ? (
        <div
          role="alert"
          className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
        >
          {formError}
        </div>
      ) : null}

      <Field
        label="New password"
        description="At least 8 characters."
        error={form.formState.errors.new_password?.message}
        required
      >
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="password"
            autoComplete="new-password"
            aria-describedby={describedBy}
            aria-invalid={invalid}
            {...form.register('new_password')}
          />
        )}
      </Field>

      <Field label="Confirm new password" error={form.formState.errors.confirm_password?.message} required>
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="password"
            autoComplete="new-password"
            aria-describedby={describedBy}
            aria-invalid={invalid}
            {...form.register('confirm_password')}
          />
        )}
      </Field>

      <Button
        type="submit"
        className="w-full"
        disabled={form.formState.isSubmitting}
        aria-busy={form.formState.isSubmitting}
      >
        {form.formState.isSubmitting ? <Spinner className="size-4 text-current" /> : null}
        Reset password
      </Button>
    </form>
  );
}

function LinkPanel({ title, description }: { title: string; description: string }) {
  return (
    <section className="space-y-4">
      <div className="flex items-start gap-3">
        <AlertTriangle className="mt-0.5 size-5 shrink-0 text-warning" aria-hidden />
        <div className="space-y-1">
          <h2 className="text-base font-semibold">{title}</h2>
          <p className="text-sm text-muted-foreground">{description}</p>
        </div>
      </div>
      <Button asChild className="w-full">
        <Link to="/forgot-password">Request a new link</Link>
      </Button>
      <Button asChild variant="outline" className="w-full">
        <Link to="/sign-in">Back to sign in</Link>
      </Button>
    </section>
  );
}
