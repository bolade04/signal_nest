import { describe, expect, it } from 'vitest';
import { isBriefAuthor, snapshotUserId } from '@/pages/approvals/approvalVocabulary';

/**
 * Phase 5B-W4 adversarial check (W21 lane ADV5AB): the UI recognises a brief's author by the
 * exact account id, as the server does (`is_brief_author` in
 * apps/api/app/approvals/service.py compares the whole id that leads the snapshot; pinned
 * there by `TestAuthorEdges` in test_approvals_api.py). A prefix or substring match would
 * disable the decision controls for a reviewer the server lets decide (`u-1` against an
 * author `u-12`), or name the wrong person as the author.
 *
 * Kills W4B-W1 (the live link compared as a prefix), W4B-W2 (the snapshot compared as a
 * prefix) and W4B-W3 (the snapshot searched for the id anywhere). All three survive
 * approval-vocabulary.test.ts and approval-queue.test.tsx, whose ids share no prefix.
 */
const snapshot = (id: string, name = 'Ada Lovelace') => `${id} ${name} <ada@example.com>`;

describe('the brief author is matched by the exact account id', () => {
  it('a reviewer whose id is a prefix of the author id is not the author', () => {
    expect(isBriefAuthor({ created_by: 'u-12', created_by_snapshot: snapshot('u-12') }, 'u-1')).toBe(
      false,
    );
    // The author link was nulled (account deleted): the snapshot alone decides.
    expect(isBriefAuthor({ created_by: null, created_by_snapshot: snapshot('u-12') }, 'u-1')).toBe(
      false,
    );
  });

  it('an author whose id is a prefix of the reviewer id is not the reviewer', () => {
    expect(isBriefAuthor({ created_by: 'u-1', created_by_snapshot: snapshot('u-1') }, 'u-12')).toBe(
      false,
    );
    expect(isBriefAuthor({ created_by: null, created_by_snapshot: snapshot('u-1') }, 'u-12')).toBe(
      false,
    );
  });

  it('an id that appears later in the snapshot (in the name or the address) names nobody', () => {
    const named = 'u-9 u-1 Lovelace <u-1@example.com>';
    expect(snapshotUserId(named)).toBe('u-9');
    expect(isBriefAuthor({ created_by: null, created_by_snapshot: named }, 'u-1')).toBe(false);
  });

  it('positive controls: the exact id is the author, by the link or by the snapshot', () => {
    expect(isBriefAuthor({ created_by: 'u-12', created_by_snapshot: snapshot('u-12') }, 'u-12')).toBe(
      true,
    );
    expect(isBriefAuthor({ created_by: null, created_by_snapshot: snapshot('u-12') }, 'u-12')).toBe(
      true,
    );
  });
});
