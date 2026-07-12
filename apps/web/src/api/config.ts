// Central API configuration. In development the Vite proxy forwards /api and
// /health to the backend, so the base URL is empty (same-origin). In production
// set VITE_API_BASE_URL to the backend origin. No component should reference a
// backend URL directly — always go through the api client.

const rawBase = import.meta.env.VITE_API_BASE_URL?.trim() ?? '';

export const API_BASE_URL = rawBase.replace(/\/$/, '');
export const API_PREFIX = '/api/v1';

export function apiUrl(path: string): string {
  const suffix = path.startsWith('/') ? path : `/${path}`;
  return `${API_BASE_URL}${suffix}`;
}
