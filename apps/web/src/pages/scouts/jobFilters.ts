/** The jobs list the scout detail page's jobs panel reads (also re-read by "Refresh status"). */
export const jobFilters = (scoutRequestId: string) => ({ scout_request_id: scoutRequestId, limit: 10 });
