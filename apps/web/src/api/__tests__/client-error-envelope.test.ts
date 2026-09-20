import { http, HttpResponse } from 'msw';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { ApiError, apiRequest, setAuthToken } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import { server } from '@/test/server';

// P6-UI-001. The API answers application errors with
// `{"error": {code, message, request_id, details?}}` (app/core/errors.py), but the
// client only ever recognised FastAPI's default `{"detail": ...}` shape — which no
// production route actually emits, because every handler is overridden. Every
// domain message was therefore discarded and replaced with `Request failed (422)`.
//
// These tests pin the message the user actually sees for each shape the client can
// receive, and — just as importantly — pin what must NEVER reach them: `details[].input`
// echoes the submitted value back, and for a `missing` error that is the WHOLE request
// body, password included.

const P = (path: string) => `*${API_PREFIX}${path}`;
const PATH = '/probe';

/** Reply to GET /probe once, with an arbitrary status and body. */
function replyJson(status: number, body: unknown): void {
  server.use(http.get(P(PATH), () => HttpResponse.json(body as object, { status })));
}

function replyRaw(status: number, body: string, contentType: string): void {
  server.use(
    http.get(P(PATH), () => new HttpResponse(body, { status, headers: { 'content-type': contentType } })),
  );
}

/**
 * Drive the real client and return the ApiError it rejects with.
 *
 * The `toBeInstanceOf` assertion is load-bearing, not ceremony: a malformed body
 * used to escape as a raw TypeError, which defeats every `instanceof ApiError`
 * check downstream.
 */
async function captureError(): Promise<ApiError> {
  const settled: unknown = await apiRequest(PATH).then(
    () => Symbol('resolved'),
    (err: unknown) => err,
  );
  expect(settled).toBeInstanceOf(ApiError);
  return settled as ApiError;
}

beforeEach(() => setAuthToken('test-token'));
afterEach(() => setAuthToken(null));

describe('application error envelope', () => {
  it('surfaces the server message instead of a bare status', async () => {
    // The founder-visible motivating case: ValidationDomainError from
    // scouting_requests/routes.py when Run now is pressed on an in-flight request.
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request is already queued or running.',
        request_id: 'req-1',
      },
    });
    const err = await captureError();
    expect(err.message).toBe('Request is already queued or running.');
    expect(err.message).not.toContain('Request failed');
  });

  it.each([
    [404, 'Scout request not found in this workspace.'],
    [403, "Role 'viewer' is not permitted for this action."],
    [409, 'This request already has a schedule.'],
    [503, 'Scout scheduling is not available yet.'],
    [500, 'An unexpected error occurred'],
  ])('surfaces the message for a %i envelope', async (status, message) => {
    replyJson(status, { error: { code: 'x', message, request_id: 'req-1' } });
    await expect(captureError()).resolves.toMatchObject({ message, status });
  });

  it('accepts an envelope with no request_id (the 429 rate-limit variant)', async () => {
    // core/middleware.py emits this shape without a request_id at all.
    replyJson(429, { error: { code: 'rate_limited', message: 'Too many requests' } });
    const err = await captureError();
    expect(err.message).toBe('Too many requests');
  });

  it('keeps status, detail and correlationId on the ApiError', async () => {
    const body = { error: { code: 'conflict', message: 'Nope.', request_id: 'req-9' } };
    replyJson(409, body);
    const err = await captureError();
    expect(err.name).toBe('ApiError');
    expect(err.status).toBe(409);
    expect(err.detail).toEqual(body);
    expect(err.correlationId).toBeTruthy();
  });
});

describe('422 validation details', () => {
  it('humanizes field errors instead of the generic envelope message', async () => {
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-2',
        details: [
          { type: 'missing', loc: ['body', 'business_name'], msg: 'Field required', input: {} },
        ],
      },
    });
    const err = await captureError();
    expect(err.message).toBe('Business name: Field required');
  });

  it('never leaks details[].input, ctx, url or any unknown key', async () => {
    // `input` echoes the submitted body verbatim. On a `missing` error pydantic
    // returns the ENTIRE payload — including the plaintext password.
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-3',
        details: [
          {
            type: 'string_too_short',
            loc: ['body', 'password'],
            msg: 'String should have at least 8 characters',
            input: { email: 'a@b.com', password: 'hunter2', token: 'ghp_SECRET' },
            ctx: { min_length: 8 },
            url: 'https://errors.pydantic.dev/2.13/v/string_too_short',
            debug_sql: 'SELECT * FROM users',
          },
        ],
      },
    });
    const err = await captureError();
    expect(err.message).toBe('Password: String should have at least 8 characters');
    for (const secret of ['hunter2', 'ghp_SECRET', 'a@b.com', 'min_length', 'pydantic.dev', 'SELECT']) {
      expect(err.message).not.toContain(secret);
    }
  });

  it('bounds the number of field errors shown', async () => {
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-4',
        details: Array.from({ length: 6 }, (_, i) => ({
          loc: ['body', `field_${i}`],
          msg: 'Field required',
        })),
      },
    });
    const err = await captureError();
    expect(err.message).toContain('and 3 more');
    expect(err.message.length).toBeLessThanOrEqual(400);
  });

  it('drops a label whose field name is not a plain identifier', async () => {
    // `extra="forbid"` and dict-keyed fields put CLIENT-CHOSEN text into loc, so an
    // attacker could otherwise render arbitrary text as an authoritative field label.
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-5',
        details: [{ loc: ['body', 'Contact support at evil.example'], msg: 'Extra inputs are not permitted' }],
      },
    });
    const err = await captureError();
    expect(err.message).toBe('Extra inputs are not permitted');
    expect(err.message).not.toContain('evil.example');
  });

  it('strips control and bidi characters from server text', async () => {
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-6',
        details: [{ loc: ['body', 'name'], msg: 'Bad\u202Evalue\u0000here' }],
      },
    });
    const err = await captureError();
    // eslint-disable-next-line no-control-regex -- asserting control chars are gone
    expect(err.message).not.toMatch(/[\u0000-\u001F\u202A-\u202E]/);
    expect(err.message).toContain('Name:');
  });

  it('labels a body field that shares a name with a request part', async () => {
    // GeocodeRequest has a field literally named `query` (locations/schemas.py), so
    // the request-part prefix may only be dropped when it IS the whole location.
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-8',
        details: [{ loc: ['body', 'query'], msg: 'String should have at least 2 characters' }],
      },
    });
    const err = await captureError();
    expect(err.message).toBe('Query: String should have at least 2 characters');
  });

  it('still drops a label when the location is only the request part', async () => {
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-8b',
        details: [{ loc: ['body'], msg: 'Field required' }],
      },
    });
    const err = await captureError();
    expect(err.message).toBe('Field required');
  });

  it('does not leave a lone surrogate when truncating', async () => {
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-8c',
        // 78 + 2 + 1 = 81 UTF-16 units, so the 80-unit cap cuts at index 79 —
        // exactly between the emoji's high and low surrogate.
        details: [{ loc: ['body', 'name'], msg: `${'a'.repeat(78)}😀b` }],
      },
    });
    const err = await captureError();
    // A split pair would leave an unpaired \uD800-\uDBFF, which renders as U+FFFD.
    expect(err.message).not.toMatch(/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/);
  });

  it('falls back to the envelope message when no detail is renderable', async () => {
    replyJson(422, {
      error: {
        code: 'validation_error',
        message: 'Request validation failed',
        request_id: 'req-7',
        details: [{ loc: ['body', 'x'], input: 'secret' }, null, 'nope'],
      },
    });
    const err = await captureError();
    expect(err.message).toBe('Request validation failed');
    expect(err.message).not.toContain('secret');
  });
});

describe('Starlette and non-envelope shapes', () => {
  it('still reads a plain string detail (unmatched route / 405)', async () => {
    replyJson(404, { detail: 'Not Found' });
    await expect(captureError()).resolves.toMatchObject({ message: 'Not Found' });
  });

  it('survives a malformed detail array without throwing a raw TypeError', async () => {
    // `{"detail":[null]}` used to escape as a TypeError, defeating every
    // `instanceof ApiError` check downstream.
    replyJson(422, { detail: [null] });
    const err = await captureError();
    expect(err).toBeInstanceOf(ApiError);
    expect(err.message).toBe('Request failed (422)');
  });

  it('humanizes a genuine detail array', async () => {
    replyJson(422, { detail: [{ loc: ['body', 'email'], msg: 'value is not a valid email address' }] });
    const err = await captureError();
    expect(err.message).toBe('Email: value is not a valid email address');
  });

  it('uses a short plain-text body', async () => {
    replyRaw(503, 'Service temporarily unavailable', 'text/plain');
    await expect(captureError()).resolves.toMatchObject({
      message: 'Service temporarily unavailable',
    });
  });

  it('never renders an HTML error page as the message', async () => {
    // An ALB / CloudFront 5xx page would otherwise become the toast text verbatim.
    replyRaw(502, '<html><head><title>502 Bad Gateway</title></head><body>...</body></html>', 'text/html');
    const err = await captureError();
    expect(err.message).toBe('Request failed (502)');
    expect(err.message).not.toContain('<');
  });

  it('discards an over-long plain-text body', async () => {
    replyRaw(500, 'x'.repeat(5000), 'text/plain');
    await expect(captureError()).resolves.toMatchObject({ message: 'Request failed (500)' });
  });

  it('falls back safely for an empty body', async () => {
    replyRaw(502, '', 'text/plain');
    await expect(captureError()).resolves.toMatchObject({ message: 'Request failed (502)' });
  });

  it('falls back safely for malformed JSON', async () => {
    replyRaw(500, '{"error": {"message": ', 'application/json');
    await expect(captureError()).resolves.toMatchObject({ message: 'Request failed (500)' });
  });

  it.each([
    ['an unknown object', { foo: 'bar', message: 'top-level message' }],
    ['an array', [{ msg: 'nope' }]],
    ['a bare null', null],
    ['a nested non-object error', { error: 'just a string' }],
  ])('never surfaces %s', async (_label, body) => {
    replyJson(500, body);
    const err = await captureError();
    expect(err.message).toBe('Request failed (500)');
    expect(err.message).not.toContain('[object Object]');
  });
});

describe('network and success paths are untouched', () => {
  it('keeps a network failure distinguishable from an HTTP failure', async () => {
    server.use(http.get(P(PATH), () => HttpResponse.error()));
    const err = await captureError();
    expect(err.status).toBe(0);
    expect(err.message).toBe('Network error — could not reach the server.');
  });

  it('still returns parsed JSON on success', async () => {
    server.use(http.get(P(PATH), () => HttpResponse.json({ ok: true })));
    await expect(apiRequest<{ ok: boolean }>(PATH)).resolves.toEqual({ ok: true });
  });

  it('still returns undefined for 204 without reading a body', async () => {
    server.use(http.get(P(PATH), () => new HttpResponse(null, { status: 204 })));
    await expect(apiRequest(PATH)).resolves.toBeUndefined();
  });
});
