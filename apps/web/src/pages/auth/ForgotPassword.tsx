import { zodResolver } from '@hookform/resolvers/zod';
import { MailCheck } from 'lucide-react';
import { useRef, useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link } from 'react-router-dom';
import { z } from 'zod';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { Field } from '@/components/common/form-field';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { calmFailureMessage } from './account-token';
import { AuthLayout } from './AuthLayout';

const schema = z.object({
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
});

type FormValues = z.infer<typeof schema>;

/**
 * Public `/forgot-password` route (P6-AUTH-2), usable while signed in. The server
 * answers every address alike, so this page does too: one outcome, whether or not
 * the address belongs to an account.
 */
export function ForgotPasswordPage() {
  const [requested, setRequested] = useState(false);
  const [formError, setFormError] = useState<string | null>(null);
  const inFlight = useRef(false);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: '' },
  });

  const submit = async (values: FormValues) => {
    // One request per submission, even for a second click that lands before the
    // disabled button has rendered. (A ref, so the form builds this handler per
    // submit event rather than during render.)
    if (inFlight.current) return;
    inFlight.current = true;
    setFormError(null);
    try {
      await api.requestPasswordReset({ email: values.email });
      setRequested(true);
    } catch (err) {
      // A 422 names the field that failed; nothing else from the server is shown.
      setFormError(
        err instanceof ApiError && err.status === 422
          ? err.message
          : calmFailureMessage(err, 'Your request could not be sent. Please try again.'),
      );
    } finally {
      inFlight.current = false;
    }
  };

  return (
    <AuthLayout title="Reset your password" subtitle="Enter the email address you sign in with.">
      {requested ? (
        <section className="space-y-4">
          <div className="flex items-start gap-3">
            <MailCheck className="mt-0.5 size-5 shrink-0 text-success" aria-hidden />
            <div className="space-y-1">
              <h2 className="text-base font-semibold">Check your inbox</h2>
              <p className="text-sm text-muted-foreground">
                If an account can receive a reset message, instructions have been sent. The link in
                the message works once and expires after a limited time.
              </p>
            </div>
          </div>
          <Button asChild variant="outline" className="w-full">
            <Link to="/sign-in">Back to sign in</Link>
          </Button>
        </section>
      ) : (
        <form onSubmit={(event) => void form.handleSubmit(submit)(event)} className="space-y-4" noValidate>
          {formError ? (
            <div
              role="alert"
              className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
            >
              {formError}
            </div>
          ) : null}

          <Field label="Email" error={form.formState.errors.email?.message} required>
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

          <Button
            type="submit"
            className="w-full"
            disabled={form.formState.isSubmitting}
            aria-busy={form.formState.isSubmitting}
          >
            {form.formState.isSubmitting ? <Spinner className="size-4 text-current" /> : null}
            Send reset link
          </Button>

          <p className="text-center text-sm text-muted-foreground">
            Remembered it?{' '}
            <Link to="/sign-in" className="font-medium text-primary hover:underline">
              Back to sign in
            </Link>
          </p>
        </form>
      )}
    </AuthLayout>
  );
}
