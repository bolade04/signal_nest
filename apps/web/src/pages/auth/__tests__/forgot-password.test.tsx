import { act, fireEvent, waitFor } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '@/App';
import { API_PREFIX } from '@/api/config';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import { renderProductionApp } from '@/test/utils';

/** `/forgot-password` (6B-4B, P6-AUTH-2) against the stateful backend model. */

const P = (path: string) => `*${API_PREFIX}${path}`;

const requestBodies = () =>
  orgModel
    .requests()
    .filter((r) => r.path.endsWith('/auth/password-reset/request'))
    .map((r) => r.body as Record<string, unknown>);

async function requestFor(email: string, session?: string) {
  const screen = renderProductionApp(<App />, '/forgot-password', { token: session });
  await screen.user.type(await screen.findByLabelText(/^email/i), email);
  await screen.user.click(screen.getByRole('button', { name: /send reset link/i }));
  return screen;
}

beforeEach(() => {
  orgModel.reset();
  orgModel.addUser({ id: 'user-rita', email: 'rita@example.com', full_name: 'Rita Reset' });
});

afterEach(() => {
  expect(orgModel.violations()).toEqual([]);
  window.history.replaceState(null, '', '/');
});

describe('asking for a password reset', () => {
  it('is linked from the sign-in page', async () => {
    const screen = renderProductionApp(<App />, '/sign-in');
    await screen.user.click(await screen.findByRole('link', { name: /forgot password\?/i }));
    expect(await screen.findByRole('heading', { name: /reset your password/i })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/forgot-password');
    expect(screen.getByLabelText(/^email/i)).toHaveValue('');
    expect(screen.getByRole('link', { name: /back to sign in/i })).toHaveAttribute('href', '/sign-in');
    const text = document.body.textContent ?? '';
    expect(text).not.toMatch(/Phase \d/);
    expect(text).not.toMatch(/creative generation/i);
    expect(text).not.toMatch(/scored opportunities/i);
  });

  it('posts exactly {email} and says the same thing for a known and an unknown address', async () => {
    const known = await requestFor('rita@example.com');
    expect(await known.findByRole('heading', { name: /check your inbox/i })).toBeInTheDocument();
    const knownText = document.body.textContent;
    // The model mailed a link to the known address…
    expect(orgModel.outbox().map((m) => [m.kind, m.to])).toEqual([['password_reset', 'rita@example.com']]);
    known.unmount();

    const unknown = await requestFor('nobody@example.com');
    expect(await unknown.findByRole('heading', { name: /check your inbox/i })).toBeInTheDocument();
    // …and nothing to the unknown one, yet the page cannot tell them apart.
    expect(orgModel.outbox()).toHaveLength(1);
    expect(document.body.textContent).toBe(knownText);
    expect(knownText).toContain('If an account can receive a reset message, instructions have been sent.');
    expect(knownText).not.toContain('rita@example.com');
    expect(knownText).not.toContain('nobody@example.com');

    expect(requestBodies()).toEqual([{ email: 'rita@example.com' }, { email: 'nobody@example.com' }]);
  });

  it('checks the address before sending anything', async () => {
    const screen = renderProductionApp(<App />, '/forgot-password');
    await screen.user.click(await screen.findByRole('button', { name: /send reset link/i }));
    expect(await screen.findByText('Email is required')).toBeInTheDocument();
    await screen.user.type(screen.getByLabelText(/^email/i), 'not-an-address');
    await screen.user.click(screen.getByRole('button', { name: /send reset link/i }));
    expect(await screen.findByText('Enter a valid email address')).toBeInTheDocument();
    expect(requestBodies()).toEqual([]);
  });

  it('sends one request for a double click while the first is pending', async () => {
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    let posts = 0;
    server.use(
      http.post(P('/auth/password-reset/request'), async () => {
        posts += 1;
        await held;
        return undefined;
      }),
    );
    const screen = renderProductionApp(<App />, '/forgot-password');
    await screen.user.type(await screen.findByLabelText(/^email/i), 'rita@example.com');
    await screen.user.dblClick(screen.getByRole('button', { name: /send reset link/i }));
    await waitFor(() => expect(posts).toBe(1));
    expect(screen.getByRole('button', { name: /send reset link/i })).toBeDisabled();
    release();
    expect(await screen.findByRole('heading', { name: /check your inbox/i })).toBeInTheDocument();
    expect(posts).toBe(1);
    expect(requestBodies()).toHaveLength(1);
  });

  it('sends one request for two submits that land before the button disables', async () => {
    const screen = renderProductionApp(<App />, '/forgot-password');
    await screen.user.type(await screen.findByLabelText(/^email/i), 'rita@example.com');
    const form = screen.getByRole('button', { name: /send reset link/i }).closest('form')!;
    // Both in one synchronous burst: no render in between can disable anything.
    act(() => {
      fireEvent.submit(form);
      fireEvent.submit(form);
    });
    expect(await screen.findByRole('heading', { name: /check your inbox/i })).toBeInTheDocument();
    await new Promise((resolve) => setTimeout(resolve, 50));
    expect(requestBodies()).toEqual([{ email: 'rita@example.com' }]);
  });

  it('is usable while signed in', async () => {
    const screen = await requestFor('rita@example.com', 'test-token');
    expect(await screen.findByRole('heading', { name: /check your inbox/i })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/forgot-password');
  });

  it('the rate limit gets calm copy that reveals nothing about the address', async () => {
    server.use(
      http.post(P('/auth/password-reset/request'), () =>
        HttpResponse.json({ error: { code: 'rate_limited', message: 'Too many requests' } }, { status: 429 }),
      ),
    );
    const screen = await requestFor('rita@example.com');
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('There have been several attempts in a short time. Wait a few minutes, then try again.');
    expect(alert).not.toHaveTextContent(/too many requests|account|rita@example\.com/i);
    // The form stays for another try.
    expect(screen.getByRole('button', { name: /send reset link/i })).toBeEnabled();
  });

  it('a field error from the server is shown as the field error, and other failures in plain words', async () => {
    let status = 422;
    server.use(
      http.post(P('/auth/password-reset/request'), () =>
        status === 422
          ? HttpResponse.json(
              {
                error: {
                  code: 'validation_error',
                  message: 'Request validation failed',
                  details: [{ loc: ['body', 'email'], msg: 'value is not a valid email address', input: 'x' }],
                },
              },
              { status: 422 },
            )
          : HttpResponse.json({ error: { code: 'internal_error', message: 'The server stumbled.' } }, { status: 500 }),
      ),
    );
    const screen = await requestFor('rita@example.com');
    expect(await screen.findByRole('alert')).toHaveTextContent('Email: value is not a valid email address');

    status = 500;
    await screen.user.click(screen.getByRole('button', { name: /send reset link/i }));
    await waitFor(() =>
      expect(screen.getByRole('alert')).toHaveTextContent('Your request could not be sent. Please try again.'),
    );
    expect(screen.queryByText(/stumbled/i)).not.toBeInTheDocument();
  });

  it('a network failure says the server could not be reached', async () => {
    server.use(http.post(P('/auth/password-reset/request'), () => HttpResponse.error()));
    const screen = await requestFor('rita@example.com');
    expect(await screen.findByRole('alert')).toHaveTextContent('Network error — could not reach the server.');
  });
});
