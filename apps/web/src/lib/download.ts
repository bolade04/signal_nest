/**
 * Hand `data` to the browser as a downloaded JSON file named `filename`.
 *
 * The object URL is revoked once the click has been dispatched, so nothing of the
 * export stays referenced by the page afterwards.
 */
export function downloadJson(filename: string, data: unknown): void {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    URL.revokeObjectURL(url);
  }
}

/** `signalnest-<kind>-export-YYYY-MM-DD.json`, dated in UTC. */
export function exportFilename(kind: 'account' | 'organization', now: Date = new Date()): string {
  return `signalnest-${kind}-export-${now.toISOString().slice(0, 10)}.json`;
}
