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

function humanizeValidation(detail: unknown): string | null {
  if (
    detail &&
    typeof detail === 'object' &&
    'detail' in detail &&
    Array.isArray((detail as { detail: unknown }).detail)
  ) {
    const items = (detail as { detail: Array<{ loc?: unknown[]; msg?: string }> }).detail;
    const parts = items
      .map((item) => {
        const field = Array.isArray(item.loc) ? item.loc.slice(1).join('.') : '';
        return field ? `${field}: ${item.msg}` : item.msg;
      })
      .filter(Boolean);
    if (parts.length) return parts.join('; ');
  }
  if (detail && typeof detail === 'object' && 'detail' in detail) {
    const d = (detail as { detail: unknown }).detail;
    if (typeof d === 'string') return d;
  }
  return null;
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
    const message =
      humanizeValidation(payload) ??
      (typeof payload === 'string' && payload ? payload : `Request failed (${response.status})`);
    throw new ApiError(message, response.status, payload, responseId);
  }

  return payload as T;
}
