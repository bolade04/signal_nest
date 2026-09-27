import { useState } from 'react';
import { ApiError } from '@/api/client';
import * as api from '@/api/endpoints';
import { useAuth } from '@/auth/AuthContext';
import { Button } from '@/components/ui/button';
import { apiErrorCode } from '@/pages/invite/invite-token';
import { RATE_LIMITED_MESSAGE } from '@/pages/auth/account-token';

/**
 * A soft, non-blocking reminder for a signed-in account whose email address is not
 * verified (P6-AUTH-2). Shown only when the session says `email_verified === false`;
 * nothing is persisted and there is no dismiss. The request carries no token, and a
 * 204 does not prove a message was sent (the server holds back repeats silently), so
 * the copy never claims delivery.
 */
export function EmailVerificationBanner() {
  const { user, refreshSession } = useAuth();
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  if (user?.email_verified !== false) return null;

  const request = async () => {
    setPending(true);
    setMessage(null);
    try {
      await api.requestEmailVerification();
      setMessage("Verification email requested. If it doesn't arrive, you can request another in a few minutes.");
    } catch (err) {
      if (apiErrorCode(err) === 'email_already_verified') {
        // Verified meanwhile (another tab, another device): re-read the session and
        // this banner goes away.
        try {
          await refreshSession();
        } catch {
          // Leave the banner; the next session read reports the verified address.
        }
      } else {
        setMessage(
          err instanceof ApiError && err.status === 429
            ? RATE_LIMITED_MESSAGE
            : 'The verification email could not be requested. Please try again.',
        );
      }
    } finally {
      setPending(false);
    }
  };

  return (
    <div
      role="status"
      className="mb-6 flex flex-col gap-3 rounded-md border border-warning/40 bg-warning/10 px-4 py-2.5 text-sm sm:flex-row sm:items-center sm:justify-between"
    >
      <p>
        <span className="font-medium text-warning">Your email address isn&apos;t verified yet.</span>
        {message ? <span className="text-muted-foreground"> {message}</span> : null}
      </p>
      <Button
        size="sm"
        variant="outline"
        className="shrink-0"
        onClick={() => void request()}
        disabled={pending}
        aria-busy={pending}
      >
        {pending ? 'Requesting…' : 'Send verification email'}
      </Button>
    </div>
  );
}
