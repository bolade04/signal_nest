import { zodResolver } from '@hookform/resolvers/zod';
import { KeyRound } from 'lucide-react';
import { useId, useRef, useState, type MutableRefObject } from 'react';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router-dom';
import { z } from 'zod';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { useAuth } from '@/auth/AuthContext';
import { Field } from '@/components/common/form-field';
import { Button } from '@/components/ui/button';
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
import { PASSWORD_CHANGED_NOTICE, calmFailureMessage } from '@/pages/auth/account-token';
import { apiErrorCode } from '@/pages/invite/invite-token';

// Self-service password change from Settings → Account (P6-UI-017, session Model A).
//
// A 204 means the server has replaced the password AND refused every token of the
// account — the one this tab holds included — and returned no session. So a success
// signs this tab out LOCALLY (no /auth/logout: it would answer 401) and hands over to
// sign-in with a notice. A wrong current password is a 422 field error, never a 401,
// so a typo keeps the session.
//
// The passwords live only in this form (react-hook-form, mounted only while the dialog
// is open) and in the JSON body of the one request: never a React Query key, query or
// mutation, never storage, the URL, the console or history. A failed request is reduced
// to a message at once; the ApiError itself — whose `detail` is the whole response
// payload — is never kept in state.

const UNCHANGED = 'Choose a password different from your current one.';
const CURRENT_INCORRECT = 'The current password is incorrect.';
const FAILED = 'Your password could not be changed. Please try again.';
// No answer arrived, so the change may have happened: say so, rather than "failed".
const UNREACHABLE =
  'The server could not be reached, so we can’t tell whether your password was changed. Try again; if you are signed out, sign in with your new password.';
// Only visible when the 401 handler had no stored session to clear (another tab had).
const SESSION_ENDED = 'Your session has ended, so your password was not changed here. Sign in again to continue.';

// Bounds mirror ChangePasswordRequest (current 1–128, new 8–128), so the form never makes
// a request the server would refuse for its shape. The confirmation is this form's own.
const schema = z
  .object({
    current_password: z
      .string()
      .min(1, 'Enter your current password')
      .max(128, 'Use at most 128 characters'),
    new_password: z.string().min(8, 'Use at least 8 characters').max(128, 'Use at most 128 characters'),
    confirm_password: z.string().min(1, 'Confirm your new password'),
  })
  .refine((values) => values.new_password === values.confirm_password, {
    message: 'The passwords do not match',
    path: ['confirm_password'],
  })
  // A convenience only: the server decides (bcrypt semantics, `password_unchanged`).
  .refine((values) => values.new_password !== values.current_password, {
    message: UNCHANGED,
    path: ['new_password'],
  });

type FormValues = z.infer<typeof schema>;

/** A failure no field owns, in this app's words; derived at once, never stored as an error. */
function failureMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 401) return SESSION_ENDED;
    // Another shape refusal: the client's own bounded rendering (field names and
    // messages only; the submitted values in the payload are never read).
    if (error.status === 422) return error.message;
    if (error.status === 0) return UNREACHABLE;
  }
  return calmFailureMessage(error, FAILED);
}

/** The Account card's "Change password" button and its dialog. */
export function ChangePasswordDialog({ email }: { email: string }) {
  const [open, setOpen] = useState(false);
  const [pending, setPending] = useState(false);
  // Set synchronously on submit, before any render, so neither a second submit nor a
  // close can slip in between the click and the disabled button.
  const inFlightRef = useRef(false);
  const currentPassword = useRef<HTMLInputElement | null>(null);

  const handleOpenChange = (next: boolean) => {
    // Never close mid-request (Esc, the close button, a click outside): its answer
    // decides whether this browser is still signed in.
    if (!next && inFlightRef.current) return;
    setOpen(next);
  };

  return (
    <Dialog open={open} onOpenChange={handleOpenChange}>
      <DialogTrigger asChild>
        <Button size="sm" variant="outline">
          <KeyRound className="size-4" /> Change password
        </Button>
      </DialogTrigger>
      <DialogContent
        className="max-w-md"
        onOpenAutoFocus={(event) => {
          // Start at the first field to fill, not the read-only address.
          event.preventDefault();
          currentPassword.current?.focus();
        }}
      >
        <DialogHeader>
          <DialogTitle>Change password</DialogTitle>
          <DialogDescription>
            Changing your password signs your account out everywhere, including this browser. You
            will then sign in with your new password.
          </DialogDescription>
        </DialogHeader>
        {/* Mounted only while the dialog is open: closing it discards everything typed. */}
        <ChangePasswordForm
          email={email}
          inFlightRef={inFlightRef}
          pending={pending}
          onPendingChange={setPending}
          currentPasswordRef={currentPassword}
          onCancel={() => handleOpenChange(false)}
        />
      </DialogContent>
    </Dialog>
  );
}

function ChangePasswordForm({
  email,
  inFlightRef,
  pending,
  onPendingChange,
  currentPasswordRef,
  onCancel,
}: {
  email: string;
  inFlightRef: MutableRefObject<boolean>;
  pending: boolean;
  onPendingChange: (pending: boolean) => void;
  currentPasswordRef: MutableRefObject<HTMLInputElement | null>;
  onCancel: () => void;
}) {
  const { endSessionAfterCredentialChange } = useAuth();
  const navigate = useNavigate();
  const formErrorId = useId();
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { current_password: '', new_password: '', confirm_password: '' },
  });
  const current = form.register('current_password');

  // Guarded by a ref against a second submit before the disabled button renders, so
  // the form builds this handler per submit event rather than during render.
  const submit = async (values: FormValues) => {
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    onPendingChange(true);
    setFormError(null);
    try {
      await api.changePassword({ current_password: values.current_password, new_password: values.new_password });
    } catch (err) {
      inFlightRef.current = false;
      onPendingChange(false);
      const code = apiErrorCode(err);
      if (code === 'current_password_incorrect') {
        form.setError('current_password', { type: 'server', message: CURRENT_INCORRECT }, { shouldFocus: true });
        return;
      }
      if (code === 'password_unchanged') {
        form.setError('new_password', { type: 'server', message: UNCHANGED }, { shouldFocus: true });
        return;
      }
      // A 401 has already been handled by the global handler (the session was over and
      // this tab is signed out); nothing changed, so no success is claimed.
      setFormError(failureMessage(err));
      return;
    }
    // 204: every token of the account is refused now, this tab's included. Clear this tab
    // locally and go to sign-in in the same tick, so the protected route cannot redirect
    // first without the notice. No sign-out request and no session re-read follow.
    endSessionAfterCredentialChange();
    navigate('/sign-in', { replace: true, state: { notice: PASSWORD_CHANGED_NOTICE } });
  };

  return (
    <form
      noValidate
      onSubmit={(event) => void form.handleSubmit(submit)(event)}
      aria-describedby={formError ? formErrorId : undefined}
      className="space-y-4"
    >
      {/* The account's own address, for password managers; not part of the request body. */}
      <Field label="Email">
        {({ id, describedBy }) => (
          <Input
            id={id}
            type="email"
            value={email}
            readOnly
            aria-readonly="true"
            autoComplete="username"
            aria-describedby={describedBy}
          />
        )}
      </Field>

      <Field label="Current password" error={form.formState.errors.current_password?.message} required>
        {({ id, describedBy, invalid }) => (
          <Input
            id={id}
            type="password"
            autoComplete="current-password"
            aria-describedby={describedBy}
            aria-invalid={invalid}
            {...current}
            ref={(element) => {
              current.ref(element);
              currentPasswordRef.current = element;
            }}
          />
        )}
      </Field>

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

      {formError ? (
        <p id={formErrorId} role="alert" className="text-sm font-medium text-destructive">
          {formError}
        </p>
      ) : null}

      <DialogFooter>
        <Button type="button" variant="outline" onClick={onCancel} disabled={pending}>
          Cancel
        </Button>
        <Button type="submit" disabled={pending} aria-busy={pending}>
          {pending ? <Spinner className="size-4 text-current" /> : null}
          Change password
        </Button>
      </DialogFooter>
    </form>
  );
}
