import { useMutation } from '@tanstack/react-query';
import { act, fireEvent, waitFor, within, type RenderResult } from '@testing-library/react';
import type userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { useEffect, useState, type ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '@/App';
import { ApiError } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import { useAuth } from '@/auth/AuthContext';
import { SIGN_OUT_NOTICE_COPY } from '@/auth/sign-out';
import { orgModel, type ModelRole } from '@/test/handlers';
import { server } from '@/test/server';
import {
  expectNoApiErrorInReactState,
  expectNoSecretLeak,
  installLeakInstruments,
  leakProbes,
  probedQueryClient,
  resetLeakInstruments,
} from '@/test/token-leak';
import { renderProductionApp } from '@/test/utils';
import { useWorkspace } from '@/workspace/WorkspaceContext';

/**
 * P6-UI-017 — self-service password change from Settings → Account (session Model A).
 * Rendered in the main.tsx tree (StrictMode, BrowserRouter, the production QueryClient)
 * against the stateful backend model, as a NON-DEMO model user: the demo account's token
 * is the fixed 'test-token' and its sign-in accepts any password, so it could prove
 * nothing here. Test names carry the stable ids of the discovery TEST-MATRIX (U17-F01…F11).
 */

const P = (path: string) => `*${API_PREFIX}${path}`;
const ORIGIN = 'http://localhost:3000';
const USER = 'user-casey';
const EMAIL = 'casey@example.com';
const NAME = 'Casey Changer';
const CURRENT = 'casey current password';
const NEW = 'casey brand-new password';
const CHANGE = /\/auth\/password\/change$/;
const NOTICE = 'Your password has been changed. Sign in with your new password.';
const RESET_NOTICE = 'Your password has been reset. Sign in with your new password.';
const SIX: ModelRole[] = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];

type Screen = RenderResult & { user: ReturnType<typeof userEvent.setup> };

function AuthProbe() {
  const { status, memberships } = useAuth();
  const { organizationId } = useWorkspace();
  const role = memberships.find((m) => m.organization_id === organizationId)?.role ?? '';
  return (
    <>
      <span data-testid="auth-status">{status}</span>
      <span data-testid="auth-role">{role}</span>
    </>
  );
}

let startRefresh: (() => Promise<void>) | null = null;
/** Starts a session re-read (refreshSession) on demand, as the members area does. */
function RefreshProbe() {
  const { refreshSession } = useAuth();
  useEffect(() => {
    startRefresh = refreshSession;
  }, [refreshSession]);
  return null;
}

/** Every request as it left the page (path + bearer) and every mocked answer's status. */
let sent: { path: string; authorization: string | null }[] = [];
let answered: { path: string; status: number }[] = [];
const onStart = ({ request }: { request: Request }) => {
  sent.push({ path: new URL(request.url).pathname, authorization: request.headers.get('authorization') });
};
const onAnswer = ({ request, response }: { request: Request; response: Response }) => {
  answered.push({ path: new URL(request.url).pathname, status: response.status });
};

function addCasey(role: ModelRole = 'marketer', password = CURRENT) {
  orgModel.addUser({ id: USER, email: EMAIL, full_name: NAME, password });
  orgModel.setMembership('org-1', USER, role);
}

/** Casey signed in earlier on this browser, now on /settings with the leak instruments on. */
async function signedInAtSettings({ probe }: { probe?: ReactNode } = {}) {
  const signedInWith = orgModel.signIn(USER);
  installLeakInstruments();
  const screen: Screen = renderProductionApp(<App />, '/settings', {
    token: signedInWith,
    probe: (
      <>
        {leakProbes()}
        <AuthProbe />
        {probe}
      </>
    ),
  });
  await waitFor(() => expect(screen.getByTestId('auth-status')).toHaveTextContent(/^authenticated$/));
  await screen.findByRole('heading', { name: 'Account' });
  await waitFor(() => expect(probedQueryClient()?.getQueryData(['organizations'])).toBeTruthy());
  // The boot session read re-issued the token inside the same session: this is the one stored.
  const token = localStorage.getItem('signalnest-token')!;
  expect(token).toMatch(/^access-user-casey-/);
  expect(orgModel.accepts(token)).toBe(true);
  return { screen, token, signedInWith };
}

function accountCard(screen: Screen) {
  // CardTitle (h3) → CardHeader → Card.
  return within(screen.getByRole('heading', { name: 'Account' }).parentElement!.parentElement!);
}

async function openDialog(screen: Screen) {
  await screen.user.click(accountCard(screen).getByRole('button', { name: /^change password$/i }));
  return screen.findByRole('dialog', { name: /^change password$/i });
}

const submitButton = (dialog: HTMLElement) => within(dialog).getByRole('button', { name: /change password$/i });
const field = (dialog: HTMLElement, label: RegExp) => within(dialog).getByLabelText(label);
const CURRENT_FIELD = /^current password/i;
const NEW_FIELD = /^new password/i;
const CONFIRM_FIELD = /^confirm new password/i;

/** Replace a field's value (paste, so long values stay fast). */
async function setField(screen: Screen, input: HTMLElement, value: string) {
  await screen.user.clear(input);
  if (!value) return;
  await screen.user.click(input);
  await screen.user.paste(value);
}

async function fill(
  screen: Screen,
  dialog: HTMLElement,
  { current = CURRENT, next = NEW, confirm = next }: { current?: string; next?: string; confirm?: string } = {},
) {
  await setField(screen, field(dialog, CURRENT_FIELD), current);
  await setField(screen, field(dialog, NEW_FIELD), next);
  await setField(screen, field(dialog, CONFIRM_FIELD), confirm);
}

const changeBodies = () =>
  orgModel
    .requests()
    .filter((r) => CHANGE.test(r.path))
    .map((r) => r.body);

function cachedData(): unknown[] {
  return probedQueryClient()!
    .getQueryCache()
    .getAll()
    .filter((q) => q.state.data !== undefined)
    .map((q) => q.queryKey);
}

async function expectAccepted(token: string, accepted: boolean) {
  expect(orgModel.accepts(token)).toBe(accepted);
  const me = await fetch(`${ORIGIN}${API_PREFIX}/auth/me`, { headers: { Authorization: `Bearer ${token}` } });
  expect(me.status).toBe(accepted ? 200 : 401);
}

beforeEach(() => {
  orgModel.reset();
  resetLeakInstruments();
  startRefresh = null;
  sent = [];
  answered = [];
  server.events.on('request:start', onStart);
  server.events.on('response:mocked', onAnswer);
});

afterEach(() => {
  server.events.removeListener('request:start', onStart);
  server.events.removeListener('response:mocked', onAnswer);
  vi.restoreAllMocks();
  expect(orgModel.violations()).toEqual([]);
  window.history.replaceState(null, '', '/');
});

// ---- U17-F01 / F02: the entry point and the form ------------------------------------

describe('U17-F01 Settings → Account offers Change password to every role', () => {
  it.each(SIX)('U17-F01 %s', async (role) => {
    addCasey(role);
    const { screen } = await signedInAtSettings();
    await waitFor(() => expect(screen.getByTestId('auth-role')).toHaveTextContent(new RegExp(`^${role}$`)));
    const card = accountCard(screen);
    // Name and Email stay; the action joins them.
    expect(card.getByText(NAME)).toBeInTheDocument();
    expect(card.getByText(EMAIL)).toBeInTheDocument();
    expect(card.getByRole('button', { name: /^change password$/i })).toBeEnabled();
    expect(screen.getAllByRole('button', { name: /^change password$/i })).toHaveLength(1);
  });
});

describe('U17-F02 the dialog', () => {
  it('U17-F02 labels every field, gives password managers a read-only username, and sets autocomplete', async () => {
    addCasey();
    const { screen } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    expect(dialog).toHaveAccessibleDescription(/signs your account out everywhere, including this browser/i);

    const email = field(dialog, /^email/i);
    expect(email).toHaveValue(EMAIL);
    expect(email).toHaveAttribute('readonly');
    expect(email).toHaveAttribute('aria-readonly', 'true');
    expect(email).toHaveAttribute('autocomplete', 'username');

    const current = field(dialog, CURRENT_FIELD);
    const next = field(dialog, NEW_FIELD);
    const confirm = field(dialog, CONFIRM_FIELD);
    expect(current).toHaveAttribute('type', 'password');
    expect(current).toHaveAttribute('autocomplete', 'current-password');
    expect(next).toHaveAttribute('type', 'password');
    expect(next).toHaveAttribute('autocomplete', 'new-password');
    expect(next).toHaveAccessibleDescription(/at least 8 characters/i);
    expect(confirm).toHaveAttribute('type', 'password');
    expect(confirm).toHaveAttribute('autocomplete', 'new-password');
    expect(dialog.querySelectorAll('input[type="password"]')).toHaveLength(3);
    // One form holds the username and the passwords, so a password manager pairs them.
    const form = current.closest('form');
    expect(form).not.toBeNull();
    for (const input of [email, next, confirm]) expect(input.closest('form')).toBe(form);

    expect(within(dialog).getByRole('button', { name: /^cancel$/i })).toBeEnabled();
    expect(submitButton(dialog)).toHaveAttribute('type', 'submit');
    // Focus starts at the first field to fill, not the read-only address.
    await waitFor(() => expect(current).toHaveFocus());
    expect(changeBodies()).toEqual([]);
  });
});

// ---- U17-F03: client validation --------------------------------------------------

describe('U17-F03 client validation sends nothing', () => {
  it('U17-F03 rejects empty, too short, too long (both fields), a mismatch and new == current with zero requests', async () => {
    addCasey();
    const { screen } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    const submit = () => screen.user.click(submitButton(dialog));

    await submit();
    await waitFor(() => expect(field(dialog, CURRENT_FIELD)).toHaveAccessibleDescription('Enter your current password'));
    expect(field(dialog, NEW_FIELD)).toHaveAccessibleDescription(/use at least 8 characters/i);
    expect(field(dialog, CONFIRM_FIELD)).toHaveAccessibleDescription('Confirm your new password');

    await fill(screen, dialog, { next: 'short1' });
    await submit();
    await waitFor(() => expect(field(dialog, NEW_FIELD)).toHaveAccessibleDescription(/use at least 8 characters/i));

    await fill(screen, dialog, { next: 'p'.repeat(129) });
    await submit();
    await waitFor(() => expect(field(dialog, NEW_FIELD)).toHaveAccessibleDescription(/use at most 128 characters/i));

    await fill(screen, dialog, { current: 'c'.repeat(129) });
    await submit();
    await waitFor(() => expect(field(dialog, CURRENT_FIELD)).toHaveAccessibleDescription('Use at most 128 characters'));

    await fill(screen, dialog, { confirm: `${NEW}!` });
    await submit();
    await waitFor(() => expect(field(dialog, CONFIRM_FIELD)).toHaveAccessibleDescription('The passwords do not match'));

    await fill(screen, dialog, { next: CURRENT });
    await submit();
    await waitFor(() =>
      expect(field(dialog, NEW_FIELD)).toHaveAccessibleDescription(/choose a password different from your current one/i),
    );

    expect(changeBodies()).toEqual([]);
    expect(sent.filter((r) => CHANGE.test(r.path))).toEqual([]);
    expect(localStorage.getItem('signalnest-token')).not.toBeNull();
  });

  it.each([
    ['current 128 characters, new 8', 'c'.repeat(128), 'n'.repeat(8)],
    ['current 1 character, new 128', 'x', 'n'.repeat(128)],
  ])('U17-F03 accepts the bounds themselves (%s)', async (_case, current, next) => {
    addCasey('marketer', current);
    const { screen } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    await fill(screen, dialog, { current, next });
    await screen.user.click(submitButton(dialog));
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    // Exactly the two passwords: the confirmation is the form's own.
    expect(changeBodies()).toEqual([{ current_password: current, new_password: next }]);
  });
});

// ---- U17-F04: the server refuses the entry ---------------------------------------

describe('U17-F04 a refused entry is a field error and keeps the session', () => {
  it('U17-F04 a wrong current password: field error; token still stored and accepted; no 401, no redirect', async () => {
    addCasey();
    const { screen, token } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    await fill(screen, dialog, { current: 'not my password' });
    // Enter in the last field submits the form.
    await screen.user.type(field(dialog, CONFIRM_FIELD), '{Enter}');

    const current = field(dialog, CURRENT_FIELD);
    await waitFor(() => expect(current).toHaveAttribute('aria-invalid', 'true'));
    expect(current).toHaveAccessibleDescription('The current password is incorrect.');
    await waitFor(() => expect(current).toHaveFocus());
    expect(changeBodies()).toEqual([{ current_password: 'not my password', new_password: NEW }]);
    // The answer was a 422; no 401 reached the client, so its handler never ran.
    expect(answered.filter((a) => CHANGE.test(a.path)).map((a) => a.status)).toEqual([422]);
    expect(answered.filter((a) => a.status === 401)).toEqual([]);
    // Still signed in, still here, dialog still open.
    expect(window.location.pathname).toBe('/settings');
    expect(screen.getByRole('dialog', { name: /^change password$/i })).toBeInTheDocument();
    expect(screen.getByTestId('auth-status')).toHaveTextContent(/^authenticated$/);
    expect(localStorage.getItem('signalnest-token')).toBe(token);
    expect(orgModel.signOuts()).toEqual([]);
    expect(orgModel.user(USER)?.password).toBe(CURRENT);
    await expectAccepted(token, true);

    // Corrected, it goes through.
    await setField(screen, current, CURRENT);
    await waitFor(() => expect(current).not.toHaveAttribute('aria-invalid', 'true'));
    await screen.user.click(submitButton(dialog));
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
  });

  it('U17-F04 the server’s password_unchanged (same first 72 bytes: bcrypt) is a field error on New password', async () => {
    // Typed strings differ, so the client rule passes; bcrypt reads only 72 bytes.
    const base = 'b'.repeat(72);
    addCasey('marketer', `${base}-one`);
    const { screen, token } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    await fill(screen, dialog, { current: `${base}-one`, next: `${base}-two` });
    await screen.user.click(submitButton(dialog));

    const next = field(dialog, NEW_FIELD);
    await waitFor(() => expect(next).toHaveAttribute('aria-invalid', 'true'));
    expect(next).toHaveAccessibleDescription(/choose a password different from your current one\./i);
    expect(field(dialog, CURRENT_FIELD)).not.toHaveAttribute('aria-invalid', 'true');
    expect(changeBodies()).toHaveLength(1);
    expect(answered.filter((a) => a.status === 401)).toEqual([]);
    expect(window.location.pathname).toBe('/settings');
    expect(localStorage.getItem('signalnest-token')).toBe(token);
    expect(orgModel.user(USER)?.password).toBe(`${base}-one`);
    await expectAccepted(token, true);
  });
});

// ---- U17-F05 / F07: success -------------------------------------------------------

describe('U17-F05 a completed change', () => {
  it('U17-F05 clears this browser, lands on sign-in with its own notice, sends no sign-out and never the old token again; the new password signs in', async () => {
    addCasey();
    // Another device's session, and a reset link mailed before the change.
    const elsewhere = orgModel.signIn(USER);
    const resetLink = orgModel.issuePasswordResetToken(USER, 'reset-tok_before-change-Q');
    const { screen, token, signedInWith } = await signedInAtSettings();
    await expectAccepted(elsewhere, true);

    const dialog = await openDialog(screen);
    await fill(screen, dialog);
    await screen.user.click(submitButton(dialog));

    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    // The notice came through router state: nothing in the URL.
    expect(window.location.search).toBe('');
    expect(window.location.href).toBe(`${ORIGIN}/sign-in`);
    expect((window.history.state as { usr?: unknown } | null)?.usr).toEqual({ notice: 'password-changed' });
    // Its own notice: not the reset's, not a sign-out's.
    expect(screen.queryByText(RESET_NOTICE)).not.toBeInTheDocument();
    for (const copy of Object.values(SIGN_OUT_NOTICE_COPY)) expect(screen.queryByText(copy)).not.toBeInTheDocument();
    // This browser is cleared: stored token, auth state, cached data.
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    expect(screen.getByTestId('auth-status')).toHaveTextContent(/^unauthenticated$/);
    expect(cachedData()).toEqual([]);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();

    // One request, with exactly the two passwords, carried by the stored token.
    expect(changeBodies()).toEqual([{ current_password: CURRENT, new_password: NEW }]);
    const changeAt = sent.findIndex((r) => CHANGE.test(r.path));
    expect(sent[changeAt]?.authorization).toBe(`Bearer ${token}`);
    expect(answered.find((a) => CHANGE.test(a.path))?.status).toBe(204);
    // Nothing followed it: no sign-out (the token is already refused), no session re-read.
    expect(sent.slice(changeAt + 1).filter((r) => /\/auth\/(logout|logout-all|me)$/.test(r.path))).toEqual([]);
    expect(orgModel.signOuts()).toEqual([]);
    expect(orgModel.user(USER)?.password).toBe(NEW);

    // The new password signs in, through the page.
    await screen.user.type(screen.getByLabelText(/^email/i), EMAIL);
    await screen.user.type(screen.getByLabelText(/^password/i), NEW);
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    await waitFor(() => expect(screen.getByTestId('auth-status')).toHaveTextContent(/^authenticated$/));
    const fresh = localStorage.getItem('signalnest-token')!;
    expect(fresh).toMatch(/^access-user-casey-/);
    expect(fresh).not.toBe(token);
    // The page never sent an earlier token again, the sign-in included.
    const bearers = sent.slice(changeAt + 1).map((r) => r.authorization);
    for (const old of [token, signedInWith, elsewhere]) expect(bearers).not.toContain(`Bearer ${old}`);

    // The server refuses every earlier token — this tab's, its first one, the other device's.
    for (const old of [token, signedInWith, elsewhere]) await expectAccepted(old, false);
    await expectAccepted(fresh, true);
    // The old password no longer signs in; the earlier reset link is dead (FD-U17-3).
    const oldLogin = await fetch(`${ORIGIN}${API_PREFIX}/auth/login`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email: EMAIL, password: CURRENT }),
    });
    expect(oldLogin.status).toBe(401);
    const reset = await fetch(`${ORIGIN}${API_PREFIX}/auth/password-reset/confirm`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: resetLink, new_password: 'reset would win' }),
    });
    expect(reset.status).toBe(404);
    expect(orgModel.user(USER)?.password).toBe(NEW);
  });
});

describe('U17-F06 double submit', () => {
  it('U17-F06 two submits that land before the button disables send exactly one request', async () => {
    addCasey();
    const { screen } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    await fill(screen, dialog);
    const form = submitButton(dialog).closest('form')!;
    // Both in one synchronous burst: no render in between can disable anything.
    act(() => {
      fireEvent.submit(form);
      fireEvent.submit(form);
    });
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(changeBodies()).toEqual([{ current_password: CURRENT, new_password: NEW }]);
    expect(sent.filter((r) => CHANGE.test(r.path))).toHaveLength(1);
  });

  it('U17-F06 Enter pressed twice, then a click on the busy button, send exactly one request', async () => {
    addCasey();
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(P('/auth/password/change'), async () => {
        await held;
        return undefined;
      }),
    );
    const { screen } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    await fill(screen, dialog);
    await screen.user.type(field(dialog, CONFIRM_FIELD), '{Enter}{Enter}');
    const busy = submitButton(dialog);
    await waitFor(() => expect(busy).toBeDisabled());
    expect(busy).toHaveAttribute('aria-busy', 'true');
    await screen.user.click(busy);
    act(() => release());
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(sent.filter((r) => CHANGE.test(r.path))).toHaveLength(1);
    expect(changeBodies()).toHaveLength(1);
  });
});

describe('U17-F07 while pending, and the auth state after success', () => {
  it('U17-F07 Esc and the close button cannot close the dialog mid-request; the answer then signs this tab out', async () => {
    addCasey();
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.post(P('/auth/password/change'), async () => {
        await held;
        return undefined;
      }),
    );
    const { screen, token } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    await fill(screen, dialog);
    await screen.user.click(submitButton(dialog));

    const busy = submitButton(dialog);
    await waitFor(() => expect(busy).toBeDisabled());
    expect(busy).toHaveAttribute('aria-busy', 'true');
    expect(within(dialog).getByRole('button', { name: /^cancel$/i })).toBeDisabled();
    await screen.user.keyboard('{Escape}');
    expect(screen.getByRole('dialog', { name: /^change password$/i })).toBeInTheDocument();
    await screen.user.click(within(dialog).getByRole('button', { name: /^close$/i }));
    expect(screen.getByRole('dialog', { name: /^change password$/i })).toBeInTheDocument();
    // Nothing has ended yet.
    expect(localStorage.getItem('signalnest-token')).toBe(token);

    act(() => release());
    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    expect(screen.getByTestId('auth-status')).toHaveTextContent(/^unauthenticated$/);
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    expect(cachedData()).toEqual([]);
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    expect(orgModel.signOuts()).toEqual([]);
  });

  it('U17-F07 a session re-read in flight at success is aborted, so its late 401 cannot sign out the next sign-in', async () => {
    addCasey();
    const { screen } = await signedInAtSettings({ probe: <RefreshProbe /> });
    // Hold the NEXT /auth/me only; it is answered by the model once released.
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let holdNext = true;
    server.use(
      http.get(P('/auth/me'), async () => {
        if (!holdNext) return undefined;
        holdNext = false;
        await held;
        return undefined;
      }),
    );
    const meBefore = sent.filter((r) => r.path.endsWith('/auth/me')).length;
    let refresh: unknown = 'pending';
    startRefresh!().then(
      () => {
        refresh = 'resolved';
      },
      (err: unknown) => {
        refresh = err;
      },
    );
    await waitFor(() => expect(sent.filter((r) => r.path.endsWith('/auth/me')).length).toBe(meBefore + 1));

    const dialog = await openDialog(screen);
    await fill(screen, dialog);
    await screen.user.click(submitButton(dialog));
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    // Aborted: the re-read failed while its answer was still held, which only the abort can
    // cause. (Under jsdom the abort reason is another realm's DOMException, so the client
    // reports it as its status-0 error; the point is that it settled, not how.)
    await waitFor(() => expect(refresh).not.toBe('pending'));
    expect(refresh).not.toBe('resolved');
    expect(answered.filter((a) => a.path.endsWith('/auth/me') && a.status === 401)).toEqual([]);

    // Signed in again with the new password …
    await screen.user.type(screen.getByLabelText(/^email/i), EMAIL);
    await screen.user.type(screen.getByLabelText(/^password/i), NEW);
    await screen.user.click(screen.getByRole('button', { name: /^sign in$/i }));
    await waitFor(() => expect(screen.getByTestId('auth-status')).toHaveTextContent(/^authenticated$/));
    const fresh = localStorage.getItem('signalnest-token')!;

    // … the old re-read's answer (a 401: its token died with the change) goes nowhere.
    const meServed = orgModel.count('GET', /\/auth\/me$/);
    act(() => release());
    await waitFor(() => expect(orgModel.count('GET', /\/auth\/me$/)).toBe(meServed + 1));
    // The model served it as a dead token's request: no account behind it.
    const meRequests = orgModel.requests().filter((r) => r.path.endsWith('/auth/me'));
    expect(meRequests[meRequests.length - 1]?.userId).toBeNull();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(screen.getByTestId('auth-status')).toHaveTextContent(/^authenticated$/);
    expect(localStorage.getItem('signalnest-token')).toBe(fresh);
    expect(orgModel.accepts(fresh)).toBe(true);
  });
});

// ---- U17-F08 / F09: failures --------------------------------------------------------

describe('U17-F08 a failed request keeps the session, says so calmly, and allows a retry', () => {
  it.each([
    [
      'a 500',
      () => HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 }),
      'Your password could not be changed. Please try again.',
      /stumbled/i,
    ],
    [
      'a 429',
      () => HttpResponse.json({ error: { code: 'rate_limited', message: 'Too many requests' } }, { status: 429 }),
      'There have been several attempts in a short time. Wait a few minutes, then try again.',
      /too many requests/i,
    ],
    [
      'a network failure',
      () => HttpResponse.error(),
      'The server could not be reached, so we can’t tell whether your password was changed. Try again; if you are signed out, sign in with your new password.',
      /network error/i,
    ],
  ])('U17-F08 %s', async (_case, respond, copy, serverText) => {
    addCasey();
    let failures = 1;
    server.use(
      http.post(P('/auth/password/change'), () => {
        if (failures <= 0) return undefined;
        failures -= 1;
        return respond();
      }),
    );
    const { screen, token } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    await fill(screen, dialog);
    await screen.user.click(submitButton(dialog));

    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent(copy);
    expect(alert).not.toHaveTextContent(serverText);
    expect(window.location.pathname).toBe('/settings');
    expect(screen.getByTestId('auth-status')).toHaveTextContent(/^authenticated$/);
    expect(localStorage.getItem('signalnest-token')).toBe(token);
    expect(orgModel.accepts(token)).toBe(true);
    expect(orgModel.user(USER)?.password).toBe(CURRENT);
    expect(orgModel.signOuts()).toEqual([]);

    // Retry: the typed values are still there and the button works again.
    const button = submitButton(dialog);
    expect(button).toBeEnabled();
    expect(button).toHaveAttribute('aria-busy', 'false');
    await screen.user.click(button);
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    expect(orgModel.user(USER)?.password).toBe(NEW);
    expect(changeBodies()).toEqual([{ current_password: CURRENT, new_password: NEW }]);
  });
});

describe('U17-F09 a 401 on the change itself', () => {
  it('U17-F09 signs this tab out through the existing handler, with no success message, and changes nothing', async () => {
    addCasey();
    const { screen, token } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    await fill(screen, dialog);
    // The session ended elsewhere while the dialog was open.
    orgModel.expireSession(token);
    await screen.user.click(submitButton(dialog));

    await waitFor(() => expect(window.location.pathname).toBe('/sign-in'));
    expect(screen.getByTestId('auth-status')).toHaveTextContent(/^unauthenticated$/);
    expect(localStorage.getItem('signalnest-token')).toBeNull();
    expect(answered.filter((a) => CHANGE.test(a.path)).map((a) => a.status)).toEqual([401]);
    // No success is claimed: neither the notice nor any router state carries one.
    await screen.findByRole('button', { name: /^sign in$/i });
    expect(screen.queryByText(NOTICE)).not.toBeInTheDocument();
    expect(screen.queryByText(/password has been changed/i)).not.toBeInTheDocument();
    expect((window.history.state as { usr?: unknown } | null)?.usr ?? null).toBeNull();
    expect(orgModel.user(USER)?.password).toBe(CURRENT);
    expect(orgModel.signOuts()).toEqual([]);
  });
});

// ---- U17-F10: nothing typed survives -------------------------------------------------

describe('U17-F10 no password is kept anywhere', () => {
  it('U17-F10 an echoing 422 is reduced to a message; after close, reopen and success no password remains on any surface', async () => {
    addCasey();
    // The pre-FD-U17-5 shape: a 422 whose details echo the submitted body back.
    let echoes = 1;
    server.use(
      http.post(P('/auth/password/change'), async ({ request }) => {
        if (echoes <= 0) return undefined;
        echoes -= 1;
        const body = (await request.clone().json()) as unknown;
        return HttpResponse.json(
          {
            error: {
              code: 'validation_error',
              message: 'Request validation failed',
              request_id: 'req-echo',
              details: [{ type: 'missing', loc: ['body', 'extra_field'], msg: 'Field required', input: body }],
            },
          },
          { status: 422 },
        );
      }),
    );
    const { screen } = await signedInAtSettings();
    let dialog = await openDialog(screen);
    await fill(screen, dialog);
    await screen.user.click(submitButton(dialog));

    // The client's own bounded rendering: field name and message, never the echoed input.
    const alert = await within(dialog).findByRole('alert');
    expect(alert).toHaveTextContent('Extra field: Field required');
    expectNoApiErrorInReactState('after an echoing 422');
    for (const secret of [CURRENT, NEW]) expectNoSecretLeak(secret, 'after an echoing 422', { formOpen: true });

    await screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }));
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    for (const secret of [CURRENT, NEW]) expectNoSecretLeak(secret, 'after close');
    expectNoApiErrorInReactState('after close');

    // Reopened: empty fields and no leftover error.
    dialog = await openDialog(screen);
    for (const label of [CURRENT_FIELD, NEW_FIELD, CONFIRM_FIELD]) expect(field(dialog, label)).toHaveValue('');
    expect(within(dialog).queryByRole('alert')).not.toBeInTheDocument();
    for (const secret of [CURRENT, NEW]) expectNoSecretLeak(secret, 'after reopen');

    // And after a completed change, on the sign-in page.
    await fill(screen, dialog);
    await screen.user.click(submitButton(dialog));
    expect(await screen.findByText(NOTICE)).toBeInTheDocument();
    for (const secret of [CURRENT, NEW]) expectNoSecretLeak(secret, 'after success');
    expectNoApiErrorInReactState('after success');
  });
});

describe('U17-F10 the password scan is a measurement, not a blind spot (positive controls)', () => {
  let holdInState: ((value: unknown) => void) | null = null;
  function StateHolder() {
    const [held, setHeld] = useState<unknown>(null);
    useEffect(() => {
      holdInState = setHeld;
    }, []);
    return held ? <span data-testid="state-holder" /> : null;
  }

  let failMutation: (() => void) | null = null;
  function FailingMutation() {
    const { mutate } = useMutation({
      mutationFn: async () => {
        throw new ApiError('Request validation failed', 422, { error: { details: [{ input: { new_password: NEW } }] } }, null);
      },
    });
    useEffect(() => {
      failMutation = () => mutate();
    }, [mutate]);
    return null;
  }

  const echoed = () => new ApiError('Request validation failed', 422, { error: { details: [{ input: { new_password: NEW } }] } }, null);

  it('catches an ApiError (and the password in its payload) held in React state', async () => {
    addCasey();
    const { screen } = await signedInAtSettings({ probe: <StateHolder /> });
    expectNoApiErrorInReactState('clean baseline');
    expectNoSecretLeak(NEW, 'clean baseline');
    act(() => holdInState!(echoed()));
    await screen.findByTestId('state-holder');
    expect(() => expectNoApiErrorInReactState('planted')).toThrow(/ApiError held in React state/);
    expect(() => expectNoSecretLeak(NEW, 'planted')).toThrow(/React state/);
  });

  it('catches a password in a failed mutation’s error payload', async () => {
    addCasey();
    await signedInAtSettings({ probe: <FailingMutation /> });
    expectNoSecretLeak(NEW, 'clean baseline');
    act(() => failMutation!());
    await waitFor(() =>
      expect(probedQueryClient()!.getMutationCache().getAll().some((m) => m.state.status === 'error')).toBe(true),
    );
    expect(() => expectNoSecretLeak(NEW, 'planted')).toThrow(/React Query cache/);
  });

  it.each([
    ['a history write', () => window.history.replaceState({ planted: NEW }, '', '/settings')],
    ['localStorage', () => localStorage.setItem('planted', NEW)],
    ['the console', () => console.info('planted', NEW)],
    ['a DOM attribute', () => document.body.setAttribute('data-planted', NEW)],
  ])('catches a password planted in %s', async (_surface, plant) => {
    addCasey();
    await signedInAtSettings();
    expectNoSecretLeak(NEW, 'clean baseline');
    plant();
    try {
      expect(() => expectNoSecretLeak(NEW, 'planted')).toThrow();
    } finally {
      document.body.removeAttribute('data-planted');
    }
  });

  it('catches a password still typed into an input', async () => {
    addCasey();
    const { screen } = await signedInAtSettings();
    const dialog = await openDialog(screen);
    await setField(screen, field(dialog, NEW_FIELD), NEW);
    expect(() => expectNoSecretLeak(NEW, 'typed')).toThrow(/form values/);
  });
});

// ---- U17-F11: focus ------------------------------------------------------------------

describe('U17-F11 focus returns to the trigger after close', () => {
  it.each([
    ['Cancel', (screen: Screen, dialog: HTMLElement) => screen.user.click(within(dialog).getByRole('button', { name: /^cancel$/i }))],
    ['Escape', (screen: Screen) => screen.user.keyboard('{Escape}')],
    ['the close button', (screen: Screen, dialog: HTMLElement) => screen.user.click(within(dialog).getByRole('button', { name: /^close$/i }))],
  ])('U17-F11 after %s', async (_how, close) => {
    addCasey();
    const { screen } = await signedInAtSettings();
    const trigger = accountCard(screen).getByRole('button', { name: /^change password$/i });
    const dialog = await openDialog(screen);
    await setField(screen, field(dialog, CURRENT_FIELD), CURRENT);
    await close(screen, dialog);
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument());
    await waitFor(() => expect(trigger).toHaveFocus());
    expect(changeBodies()).toEqual([]);
  });
});
