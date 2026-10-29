// Scout statuses where the backend is still working on a run, so a view has to keep
// re-reading: the run endpoint returns `queued` immediately and a durable worker settles
// it later, with nothing pushing that change to the client. Mirrors
// `_IN_FLIGHT_SCOUT_STATUSES` in apps/api/app/jobs/handlers.py; the API refuses
// `POST .../run` in exactly these states (apps/api/app/scouting_requests/routes.py).
// Deliberately an explicit allowlist rather than "anything that is not completed": the
// status column is an unconstrained string, so an unrecognised value would otherwise poll
// forever. `draft` and `paused` are settled states, not in-flight ones.
export const SCOUT_IN_FLIGHT: ReadonlySet<string> = new Set(['queued', 'running']);

export function isScoutInFlight(status: string | null | undefined): boolean {
  return SCOUT_IN_FLIGHT.has(status ?? '');
}

/** How often a view re-reads a scout request while a run is in flight. */
export const SCOUT_POLL_MS = 2000;
