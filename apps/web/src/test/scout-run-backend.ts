import { getResponse, http, HttpResponse } from 'msw';
import { API_PREFIX } from '@/api/config';
import { handlers } from './handlers';
import { server } from './server';

/**
 * A stateful backend for one scout request's run lifecycle, for tests of Run now and of
 * status polling. It refuses a run as apps/api/app/scouting_requests/routes.py does
 * (422 "Request is already queued or running." while queued/running, 422 "Resume the
 * request before running it." while paused; messages as captured from the local API).
 * The request's status changes only through `state.phase` (the test's "worker"), and every
 * read is counted, so polling can be measured.
 */

export const RUN_REQ = 'scout-loc-dallas';
export const RUN_REQ_NAME = 'Dallas demand scan';
const P = (path: string) => `*${API_PREFIX}${path}`;

export interface RunBackendState {
  phase: string;
  posts: number;
  detailGets: number;
  listGets: number;
  jobsGets: number;
  /** Answer the next POST with this HTTP status (one shot). */
  failNextPost: number | null;
  /** While set, a POST waits for this promise before answering ("Starting…"). */
  holdPost: Promise<void> | null;
  /** Execute jobs of this request served by the jobs list, newest first. */
  jobs: Array<{ id: string; status: string }>;
}

export const runBackend: RunBackendState = {
  phase: 'completed',
  posts: 0,
  detailGets: 0,
  listGets: 0,
  jobsGets: 0,
  failNextPost: null,
  holdPost: null,
  jobs: [],
};

function refuse(message: string) {
  return HttpResponse.json({ error: { code: 'validation_error', message, request_id: 'req-run' } }, { status: 422 });
}

function jobRow(job: { id: string; status: string }) {
  return {
    id: job.id,
    job_type: 'scout_request.execute',
    status: job.status,
    attempt_count: 1,
    max_attempts: 5,
    priority: 0,
    last_error_code: null,
    result_summary: null,
    scheduled_for: null,
    scout_request_id: RUN_REQ,
    location_id: 'loc-dallas',
    started_at: null,
    completed_at: null,
    cancel_requested_at: null,
    cancelled_at: null,
    created_at: '2026-06-01T12:00:00Z',
    updated_at: '2026-06-01T12:00:00Z',
  };
}

export function resetRunBackend(phase = 'completed') {
  Object.assign(runBackend, {
    phase,
    posts: 0,
    detailGets: 0,
    listGets: 0,
    jobsGets: 0,
    failNextPost: null,
    holdPost: null,
    jobs: [],
  });
}

export function installRunBackend({ emptyOpportunities = false } = {}) {
  server.use(
    http.get(P('/workspaces/:ws/scout-requests'), async ({ request }) => {
      runBackend.listGets += 1;
      const rows = (await (await getResponse(handlers, request))!.json()) as Array<Record<string, unknown>>;
      return HttpResponse.json(rows.map((r) => (r.id === RUN_REQ ? { ...r, status: runBackend.phase } : r)));
    }),
    http.get(P('/workspaces/:ws/scout-requests/:id'), async ({ request, params }) => {
      if (params.id === RUN_REQ) runBackend.detailGets += 1;
      const row = (await (await getResponse(handlers, request))!.json()) as Record<string, unknown>;
      return HttpResponse.json(params.id === RUN_REQ ? { ...row, status: runBackend.phase } : row);
    }),
    http.post(P('/workspaces/:ws/scout-requests/:id/run'), async ({ params }) => {
      runBackend.posts += 1;
      if (runBackend.holdPost) await runBackend.holdPost;
      if (runBackend.failNextPost !== null) {
        const status = runBackend.failNextPost;
        runBackend.failNextPost = null;
        return HttpResponse.json(
          { error: { code: 'internal_error', message: 'Temporarily unavailable.', request_id: 'req-run' } },
          { status },
        );
      }
      if (runBackend.phase === 'queued' || runBackend.phase === 'running') return refuse('Request is already queued or running.');
      if (runBackend.phase === 'paused') return refuse('Resume the request before running it.');
      runBackend.phase = 'queued';
      return HttpResponse.json({
        scout_request_id: String(params.id),
        status: 'queued',
        stats: { job_id: `job-${runBackend.posts}`, job_status: 'pending' },
      });
    }),
    http.get(P('/workspaces/:ws/jobs'), () => {
      runBackend.jobsGets += 1;
      const items = runBackend.jobs.map(jobRow);
      return HttpResponse.json({ items, total: items.length, limit: 10, offset: 0 });
    }),
    http.get(P('/workspaces/:ws/scout-requests/:id/schedule'), () =>
      HttpResponse.json({ error: { code: 'not_found', message: 'no schedule' } }, { status: 404 }),
    ),
    ...(emptyOpportunities
      ? [http.get(P('/workspaces/:ws/opportunities'), () => HttpResponse.json([], { headers: { 'X-Total-Count': '0' } }))]
      : []),
  );
}
