import { API_PREFIX, apiUrl } from './config';

export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly correlationId: string | null;

  constructor(message: string, status: number, detail: unknown, correlationId: string | null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
    this.correlationId = correlationId;
  }

  get isAuthError(): boolean {
    return this.status === 401;
  }
}

// The auth layer registers the current bearer token and a callback for expiry
// so the client stays the single place that knows about auth headers.
let authToken: string | null = null;
let onUnauthorized: (() => void) | null = null;

export function setAuthToken(token: string | null): void {
  authToken = token;
}

export function setUnauthorizedHandler(handler: (() => void) | null): void {
  onUnauthorized = handler;
}

function newCorrelationId(): string {
  if (typeof crypto !== 'undefined' && 'randomUUID' in crypto) {
    return crypto.randomUUID();
  }
  return `req-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

// --- error message derivation (P6-UI-001) ---------------------------------
//
// The API answers application errors with `{"error": {code, message, request_id,
// details?}}` (app/core/errors.py). Every FastAPI handler is overridden, so the
// `{"detail": ...}` shape this file used to look for only ever arrives from
// Starlette's own unmatched-route 404 / 405 — never from an application route.
// Reading only `detail` therefore discarded every domain message and showed
// `Request failed (422)` instead.
//
// Matching is a CLOSED ALLOWLIST of three shapes, never a search for "some string
// in the body". A server object is never stringified, spread, enumerated or
// recursed into: anything unrecognised falls through to the status fallback. The
// reason is concrete — `details[].input` echoes the submitted value back, and for
// a `missing` error Pydantic returns the WHOLE request body, password included.

/** Control, bidi and zero-width characters that can hide or reorder rendered text. */
// eslint-disable-next-line no-control-regex -- stripping control characters is the point
const UNSAFE_TEXT = /[\u0000-\u001F\u007F-\u009F\u200B-\u200F\u202A-\u202E\u2066-\u2069]/g;
/** A field label is derived only from a plain identifier; `extra="forbid"` and
 *  dict-keyed fields put CLIENT-CHOSEN text into `loc`, which must never be
 *  rendered as though the server had authored it. */
const FIELD_SEGMENT = /^[A-Za-z0-9_]{1,80}$/;
/** Request-part prefixes Pydantic puts at `loc[0]`; never a user-facing label. */
const LOC_PREFIXES = new Set(['body', 'query', 'header', 'path', 'cookie']);

const MAX_SEGMENT = 80;
const MAX_PAIR = 120;
const MAX_MESSAGE = 400;
const MAX_TEXT_BODY = 200;
const MAX_FIELD_ERRORS = 3;

function sanitize(value: string): string {
  return value.replace(UNSAFE_TEXT, '').replace(/\s+/g, ' ').trim();
}

function clamp(value: string, max: number): string {
  if (value.length <= max) return value;
  // Slicing by code unit can split a surrogate pair and leave a lone high
  // surrogate, which renders as U+FFFD; drop it rather than emit it.
  return `${value.slice(0, max - 1).replace(/[\uD800-\uDBFF]$/, '')}…`;
}

/** `business_name` -> `Business name`. Sanitize, then gate, then derive, then cap. */
function fieldLabel(loc: unknown): string | null {
  if (!Array.isArray(loc) || loc.length === 0) return null;
  const last = loc[loc.length - 1];
  // A numeric index ("body", 0, "name") carries no useful label; drop it.
  if (typeof last !== 'string') return null;
  const cleaned = sanitize(last);
  // The request-part prefix is only ever `loc[0]`, so it is a label to drop only
  // when it is the WHOLE location. Guarding on length matters: `GeocodeRequest`
  // has a body field genuinely named `query`, which would otherwise lose its label.
  if (!cleaned || (loc.length === 1 && LOC_PREFIXES.has(cleaned))) return null;
  if (!FIELD_SEGMENT.test(cleaned)) return null;
  const words = cleaned.replace(/_/g, ' ').trim();
  if (!words) return null;
  return clamp(words.charAt(0).toUpperCase() + words.slice(1), MAX_SEGMENT);
}

/**
 * Render a Pydantic error list as bounded, human text.
 *
 * Reads exactly two keys — `msg` and `loc`. `input`, `ctx`, `type`, `url` and every
 * unlisted key are dropped: `input` carries the caller's own submitted data.
 */
function humanizeFieldErrors(items: unknown): string | null {
  if (!Array.isArray(items)) return null;
  const parts: string[] = [];
  let omitted = 0;
  for (const item of items) {
    if (!item || typeof item !== 'object' || Array.isArray(item)) continue;
    const msg = (item as { msg?: unknown }).msg;
    if (typeof msg !== 'string') continue;
    const text = clamp(sanitize(msg), MAX_SEGMENT);
    if (!text) continue;
    if (parts.length >= MAX_FIELD_ERRORS) {
      omitted += 1;
      continue;
    }
    const label = fieldLabel((item as { loc?: unknown }).loc);
    parts.push(clamp(label ? `${label}: ${text}` : text, MAX_PAIR));
  }
  if (!parts.length) return null;
  const joined = parts.join('; ');
  return clamp(omitted ? `${joined}; and ${omitted} more` : joined, MAX_MESSAGE);
}

/** The `error` object of the application envelope, or null for any other shape. */
function readEnvelope(payload: unknown): Record<string, unknown> | null {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) return null;
  const error = (payload as { error?: unknown }).error;
  if (!error || typeof error !== 'object' || Array.isArray(error)) return null;
  return error as Record<string, unknown>;
}

/**
 * A short, single-line, markup-free text body — an upstream proxy's `text/plain`.
 * An HTML error page (ALB, CloudFront) is refused rather than rendered verbatim.
 */
function readTextBody(payload: unknown): string | null {
  if (typeof payload !== 'string') return null;
  if (payload.length > MAX_TEXT_BODY || payload.includes('<') || /[\r\n]/.test(payload)) return null;
  return sanitize(payload) || null;
}

/**
 * The message the user sees. First match wins; anything unrecognised falls
 * through to the stable status fallback rather than exposing the body.
 */
function errorMessage(payload: unknown, status: number): string {
  const fallback = `Request failed (${status})`;

  const envelope = readEnvelope(payload);
  if (envelope) {
    // Field errors are more specific than the generic "Request validation failed"
    // the validation handler pairs them with, so they win when present.
    if (status === 422) {
      const fields = humanizeFieldErrors(envelope.details);
      if (fields) return fields;
    }
    const message = envelope.message;
    if (typeof message === 'string') {
      const text = clamp(sanitize(message), MAX_MESSAGE);
      if (text) return text;
    }
    return fallback;
  }

  if (payload && typeof payload === 'object' && !Array.isArray(payload)) {
    const detail = (payload as { detail?: unknown }).detail;
    // Starlette's own 404 / 405 handler, still reachable for unmatched routes.
    if (typeof detail === 'string') {
      const text = clamp(sanitize(detail), MAX_MESSAGE);
      if (text) return text;
    }
    // Defensive only: no route in this API can currently produce a detail array.
    const fields = humanizeFieldErrors(detail);
    if (fields) return fields;
    return fallback;
  }

  return readTextBody(payload) ?? fallback;
}

export interface RequestOptions {
  method?: 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';
  body?: unknown;
  query?: Record<string, string | number | boolean | null | undefined>;
  signal?: AbortSignal;
  /** Prefix the path with /api/v1 (default true). */
  prefixed?: boolean;
}

export async function apiRequest<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = 'GET', body, query, signal, prefixed = true } = options;

  const basePath = prefixed ? `${API_PREFIX}${path}` : path;
  const url = new URL(apiUrl(basePath), window.location.origin);
  if (query) {
    for (const [key, value] of Object.entries(query)) {
      if (value !== undefined && value !== null && value !== '') {
        url.searchParams.set(key, String(value));
      }
    }
  }

  const correlationId = newCorrelationId();
  const headers: Record<string, string> = {
    Accept: 'application/json',
    'X-Request-ID': correlationId,
  };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (authToken) headers.Authorization = `Bearer ${authToken}`;

  let response: Response;
  try {
    response = await fetch(url.toString().replace(window.location.origin, ''), {
      method,
      headers,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal,
    });
  } catch (err) {
    if (err instanceof DOMException && err.name === 'AbortError') throw err;
    throw new ApiError('Network error — could not reach the server.', 0, null, correlationId);
  }

  const responseId = response.headers.get('x-request-id') ?? correlationId;

  if (response.status === 204) {
    return undefined as T;
  }

  const isJson = response.headers.get('content-type')?.includes('application/json');
  const payload = isJson ? await response.json().catch(() => null) : await response.text();

  if (!response.ok) {
    if (response.status === 401) {
      onUnauthorized?.();
    }
    throw new ApiError(errorMessage(payload, response.status), response.status, payload, responseId);
  }

  return payload as T;
}
