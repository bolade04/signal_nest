import { AlertTriangle, CheckCircle2, XCircle } from 'lucide-react';
import type { ContentReviewOut } from '@/api/types';
import { Badge } from '@/components/ui/badge';
import { RISK_LABELS } from './creativeLabels';

/**
 * The output-side content review of one draft version, as stored: the claim-safety risk
 * level and findings, the review's warnings, and every required disclaimer with whether
 * the draft carries it. Read-only: a review is immutable evidence of what was checked when
 * the version was generated.
 */
export function ReviewWarningsPanel({ review }: { review: ContentReviewOut }) {
  const risk = RISK_LABELS[review.risk_level];
  const missing = new Set(review.disclaimers_resolved ? [] : review.required_disclaimers);
  return (
    <section aria-label="Content review" className="space-y-3 rounded-md border p-3">
      <div className="flex items-center justify-between gap-2">
        <h4 className="text-sm font-medium">Content review</h4>
        <Badge intent={risk.intent} data-testid="review-risk">
          {risk.label}
        </Badge>
      </div>

      {review.findings.length > 0 ? (
        <div>
          <p className="text-xs font-medium text-muted-foreground">Flagged wording</p>
          <ul className="mt-1 space-y-1 text-sm" aria-label="Flagged wording">
            {review.findings.map((finding, index) => (
              <li key={`${finding.category}-${index}`} className="flex items-start gap-2">
                <AlertTriangle className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
                <span>
                  <span className="font-medium">“{finding.excerpt}”</span>{' '}
                  <span className="text-muted-foreground">
                    {finding.category.replace(/_/g, ' ')} · {RISK_LABELS[finding.risk].label}
                  </span>
                </span>
              </li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">No risky wording was flagged.</p>
      )}

      {review.warnings.length > 0 && (
        <ul className="list-disc space-y-1 pl-5 text-sm" aria-label="Review warnings">
          {review.warnings.map((warning, index) => (
            <li key={index}>{warning}</li>
          ))}
        </ul>
      )}

      {review.required_disclaimers.length > 0 && (
        <div>
          <p className="text-xs font-medium text-muted-foreground">Required disclaimers</p>
          <ul className="mt-1 space-y-1 text-sm" aria-label="Required disclaimers">
            {review.required_disclaimers.map((text) => {
              const carried = review.disclaimers_resolved && !missing.has(text);
              return (
                <li key={text} className="flex items-start gap-2">
                  {carried ? (
                    <CheckCircle2 className="mt-0.5 size-4 shrink-0 text-success" aria-hidden />
                  ) : (
                    <XCircle className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden />
                  )}
                  <span>
                    {text}
                    <span className="sr-only">{carried ? ' (included)' : ' (not confirmed)'}</span>
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
      )}
    </section>
  );
}
