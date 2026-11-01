import { useId } from 'react';
import type { ScoutRequestOut } from '@/api/types';
import { Badge } from '@/components/ui/badge';
import { runResultCopy, runResultIntent, runResultLabels, runResultSummaries } from '@/lib/run-outcome';
import { isEarlierRunResult, runResultLines, showsRunResult } from '@/lib/run-result';

/**
 * The Scout page's run-result panel (W21 FU-Q1-1, Q1 (a+), PROPOSED): what the last run that read
 * its sources read, what it did not read and the one next step, from the API's typed
 * `run_outcome`. A complete run says so explicitly, so a partial run never looks like a complete
 * one. Nothing is shown when the API records no outcome (no result yet, or a run recorded before
 * per-source facts existed), or when the run read no source (the zero-result panel explains it;
 * review r1 MINOR-1). When the latest run ended without a result, the panel says that it shows an
 * earlier run's (review r1 MINOR-2). Read-only: for a member who can edit the Scout (`canEdit`,
 * the page's EDITORS gate) the next step points at controls the page shows (Edit, Run now);
 * anyone else is asked to turn to someone who can.
 */
export function RunResultPanel({
  request,
  canEdit,
}: {
  request: Pick<ScoutRequestOut, 'run_outcome' | 'stats' | 'execution'>;
  canEdit: boolean;
}) {
  const headingId = useId();
  const result = request.run_outcome;
  if (!showsRunResult(result)) return null;
  const lines = runResultLines(result, request.stats, canEdit);
  const attention = result.kind !== 'complete';
  return (
    <section
      aria-labelledby={headingId}
      data-run-result={result.kind}
      className={
        attention
          ? 'rounded-md border border-warning/40 bg-warning/10 px-4 py-3 text-sm'
          : 'rounded-md border border-border bg-muted/40 px-4 py-3 text-sm'
      }
    >
      <h2 id={headingId} className="font-medium">
        {runResultCopy.heading}
      </h2>
      {isEarlierRunResult(request.execution) ? (
        <p data-earlier-run="" className="mt-1 text-muted-foreground">
          {runResultCopy.earlierRun}
        </p>
      ) : null}
      {/* Announced when a finished run changes the result while the page is open. */}
      <p role="status" className="mt-1 flex flex-wrap items-center gap-2">
        <Badge intent={runResultIntent[result.kind]}>{runResultLabels[result.kind]}</Badge>
        <span>{runResultSummaries[result.kind]}</span>
      </p>
      <dl className="mt-2 space-y-2">
        <div>
          <dt className="font-medium">{runResultCopy.whatRan}</dt>
          <dd>
            <ul className="mt-0.5 list-disc space-y-0.5 pl-5 text-muted-foreground">
              {lines.ran.map((line, i) => (
                <li key={`${i}:${line}`}>{line}</li>
              ))}
            </ul>
          </dd>
        </div>
        {lines.didNot.length ? (
          <div>
            <dt className="font-medium">{runResultCopy.whatDidNot}</dt>
            <dd>
              <ul className="mt-0.5 list-disc space-y-0.5 pl-5 text-muted-foreground">
                {lines.didNot.map((line, i) => (
                  <li key={`${i}:${line}`}>{line}</li>
                ))}
              </ul>
            </dd>
          </div>
        ) : null}
        {lines.next ? (
          <div>
            <dt className="font-medium">{runResultCopy.nextStep}</dt>
            <dd className="mt-0.5">{lines.next}</dd>
          </div>
        ) : null}
      </dl>
    </section>
  );
}
