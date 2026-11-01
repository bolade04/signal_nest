import { Ban, FileWarning } from 'lucide-react';
import type { ContentReviewOut } from '@/api/types';
import { reviewState } from './creativeLabels';

/**
 * Why a draft version cannot be approved, when it cannot: its review is blocked (it carries
 * wording the workspace's claims library blocks, or a blocked-risk claim), or a required
 * disclaimer is missing. Nothing for a version whose review passed. The remedy offered is a
 * revised version — a stored version and its review never change.
 */
export function BlockedState({ review }: { review: ContentReviewOut | null | undefined }) {
  const state = reviewState(review);
  if (state === 'blocked') {
    const count = review?.blocked_claim_ids.length ?? 0;
    return (
      <div role="alert" className="flex items-start gap-2 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm">
        <Ban className="mt-0.5 size-4 shrink-0 text-destructive" aria-hidden />
        <div>
          <p className="font-medium">This draft is blocked and cannot be approved.</p>
          <p className="text-muted-foreground">
            {count > 0
              ? `It contains ${count === 1 ? 'a claim' : `${count} claims`} that your claims library blocks.`
              : 'It contains wording the content review blocks.'}{' '}
            Generate a revised version to continue.
          </p>
        </div>
      </div>
    );
  }
  if (state === 'missing_disclaimers') {
    return (
      <div role="alert" className="flex items-start gap-2 rounded-md border border-warning/40 bg-warning/5 p-3 text-sm">
        <FileWarning className="mt-0.5 size-4 shrink-0 text-warning" aria-hidden />
        <div>
          <p className="font-medium">A required disclaimer is missing, so this draft cannot be approved.</p>
          <p className="text-muted-foreground">Generate a revised version to continue.</p>
        </div>
      </div>
    );
  }
  return null;
}
