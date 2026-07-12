import { zodResolver } from '@hookform/resolvers/zod';
import { useState } from 'react';
import { useForm } from 'react-hook-form';
import { Link, Navigate, useNavigate } from 'react-router-dom';
import { z } from 'zod';
import { Field } from '@/components/common/form-field';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Spinner } from '@/components/ui/spinner';
import { useAuth } from '@/auth/AuthContext';
import { AuthLayout } from './AuthLayout';

const schema = z.object({
  full_name: z.string().min(1, 'Your name is required'),
  organization_name: z.string().min(1, 'Organization name is required'),
  email: z.string().min(1, 'Email is required').email('Enter a valid email address'),
  password: z.string().min(8, 'Use at least 8 characters'),
});

type FormValues = z.infer<typeof schema>;

export function RegisterPage() {
  const { status, register: registerUser } = useAuth();
  const navigate = useNavigate();
  const [formError, setFormError] = useState<string | null>(null);

  const form = useForm<FormValues>({
    resolver: zodResolver(schema),
    defaultValues: { full_name: '', organization_name: '', email: '', password: '' },
  });

  if (status === 'authenticated') {
    return <Navigate to="/" replace />;
  }

  const submit = async (values: FormValues) => {
    setFormError(null);
    try {
      await registerUser(values);
      navigate('/onboarding', { replace: true });
    } catch (err) {
      setFormError(err instanceof Error ? err.message : 'Unable to create your account.');
    }
  };

  return (
    <AuthLayout
      title="Create your workspace"
      subtitle="Spin up an organization and start scouting in minutes."
    >
      <form onSubmit={form.handleSubmit(submit)} className="space-y-4" noValidate>
        {formError ? (
          <div
            role="alert"
            className="rounded-md border border-destructive/40 bg-destructive/5 px-3 py-2 text-sm text-destructive"
          >
            {formError}
          </div>
        ) : null}

        <Field label="Full name" error={form.formState.errors.full_name?.message} required>
          {({ id, describedBy, invalid }) => (
            <Input id={id} autoComplete="name" aria-describedby={describedBy} aria-invalid={invalid} {...form.register('full_name')} />
          )}
        </Field>

        <Field
          label="Organization name"
          error={form.formState.errors.organization_name?.message}
          required
        >
          {({ id, describedBy, invalid }) => (
            <Input id={id} aria-describedby={describedBy} aria-invalid={invalid} {...form.register('organization_name')} />
          )}
        </Field>

        <Field label="Work email" error={form.formState.errors.email?.message} required>
          {({ id, describedBy, invalid }) => (
            <Input id={id} type="email" autoComplete="email" aria-describedby={describedBy} aria-invalid={invalid} {...form.register('email')} />
          )}
        </Field>

        <Field
          label="Password"
          description="At least 8 characters."
          error={form.formState.errors.password?.message}
          required
        >
          {({ id, describedBy, invalid }) => (
            <Input id={id} type="password" autoComplete="new-password" aria-describedby={describedBy} aria-invalid={invalid} {...form.register('password')} />
          )}
        </Field>

        <Button type="submit" className="w-full" disabled={form.formState.isSubmitting}>
          {form.formState.isSubmitting ? <Spinner className="size-4 text-current" /> : null}
          Create account
        </Button>

        <p className="text-center text-sm text-muted-foreground">
          Already have an account?{' '}
          <Link to="/sign-in" className="font-medium text-primary hover:underline">
            Sign in
          </Link>
        </p>
      </form>
    </AuthLayout>
  );
}
