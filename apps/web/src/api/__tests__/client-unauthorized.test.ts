import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, apiRequest, setAuthToken, setUnauthorizedHandler } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import { server } from '@/test/server';

// P6-AUTH-4 follow-up (a)/(c) and the in-flight race: the 401 handler is told which
// token the REFUSED request carried — captured when it was sent, not read when the
// answer arrives — so the auth layer can tell "this tab's session ended" from "an
// answer about a session this tab no longer uses".

const P = (path: string) => `*${API_PREFIX}${path}`;

afterEach(() => {
  setUnauthorizedHandler(null);
  setAuthToken(null);
});

describe('the 401 handler receives the refused request token', () => {
  it('passes the token the request was sent with', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    setAuthToken('token-one');
    server.use(http.get(P('/probe'), () => HttpResponse.json({ error: { code: 'unauthorized', message: 'x' } }, { status: 401 })));

    await expect(apiRequest('/probe')).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
    expect(handler).toHaveBeenCalledWith('token-one');
  });

  it('passes the token sent, even when the tab has switched session before the answer', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    setAuthToken('token-one');
    let release!: () => void;
    const held = new Promise<void>((resolve) => {
      release = resolve;
    });
    server.use(
      http.get(P('/probe'), async () => {
        await held;
        return HttpResponse.json({ error: { code: 'unauthorized', message: 'x' } }, { status: 401 });
      }),
    );

    const pending = apiRequest('/probe');
    // Signed out and in again in this tab while the request was in flight.
    setAuthToken('token-two');
    release();

    await expect(pending).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledWith('token-one');
  });

  it('passes null for a request sent with no session', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    server.use(http.get(P('/probe'), () => HttpResponse.json({ error: { code: 'unauthorized', message: 'x' } }, { status: 401 })));

    await expect(apiRequest('/probe')).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledWith(null);
  });

  it('is not called for any other status', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    setAuthToken('token-one');
    server.use(http.get(P('/probe'), () => HttpResponse.json({ error: { code: 'forbidden', message: 'x' } }, { status: 403 })));

    await expect(apiRequest('/probe')).rejects.toBeInstanceOf(ApiError);
    expect(handler).not.toHaveBeenCalled();
  });
});
