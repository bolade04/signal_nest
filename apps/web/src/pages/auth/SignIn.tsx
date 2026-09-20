import { zodResolver } from '@hookform/resolvers/zod';
import { useEffect, useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link, Navigate, useNavigate } from 'react-router-dom';
import { z } from 'zod';
import { ApiError } from '@/api/client';
import { Field } from '@/components/common/form-field';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { useAuth } from '@/auth/AuthContext';
import { AuthLayout } from './AuthLayout';
// Static import on purpose: a dynamic import would be code-split before the
// `import.meta.env.DEV` branch below is folded away, shipping the demo
// credentials in a chunk of their own. See DemoSignInShortcut.tsx.
import { DemoSignInShortcut } from './DemoSignInShortcut';

const schema = z.object({
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
  password: z.string().min(1, 'Password is required'),
});

type FormValues = z.infer<typeof schema>;

export function SignInPage() {
  const { status, login, intendedPath, setIntendedPath } = useAuth();
  const navigate = useNavigate();
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { email: '', password: '' },
  });

  useEffect(() => {
    if (status === 'authenticated') {
      const target = intendedPath ?? '/';
      setIntendedPath(null);
      navigate(target, { replace: true });
    }
  }, [status, intendedPath, setIntendedPath, navigate]);

  if (status === 'authenticated') {
    return <Navigate to={intendedPath ?? '/'} replace />;
  }

  const submit = async (values: FormValues) => {
    setFormError(null);
    try {
      await login(values);
    } catch (err) {
      setFormError(
        err instanceof ApiError && err.status === 401
          ? 'Incorrect email or password.'
          : err instanceof Error
            ? err.message
            : 'Unable to sign in. Please try again.',
      );
    }
  };

  const useDemoAccount = (email: string, password: string) => {
    form.setValue('email', email, { shouldValidate: true });
    form.setValue('password', password, { shouldValidate: true });
    void form.handleSubmit(submit)();
  };

  return (
    <AuthLayout title="Sign in" subtitle="Welcome back. Let's see what your scouts found.">
      <form onSubmit={form.handleSubmit(submit)} className="space-y-4" noValidate>
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
          No account?{' '}
          <Link to="/register" className="font-medium text-primary hover:underline">
            Create one
          </Link>
        </p>

        {/* Read at render, not module scope: a module-scope capture would be
            evaluated at import time and could not be exercised by a test. */}
        {import.meta.env.DEV ? (
          <DemoSignInShortcut onUse={useDemoAccount} disabled={form.formState.isSubmitting} />
        ) : null}
      </form>
    </AuthLayout>
  );
}
