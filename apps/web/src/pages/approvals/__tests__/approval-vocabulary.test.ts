import { describe, expect, it } from 'vitest';
import type { ApprovalDecision, ApprovalReason } from '@/api/types';
import { canDecideApprovals, canReadApprovalHistory } from '@/lib/roles';
import {
  isBriefAuthor,
  reasonsForDecision,
  snapshotName,
  snapshotUserId,
} from '@/pages/approvals/approvalVocabulary';

/**
 * The server's partition (apps/api/app/approvals/reasons.py), written out literally: a
 * test that asked the module under test for the expected sets could not catch it drifting.
 */
const SERVER_POLARITY: Record<ApprovalDecision, ApprovalReason[]> = {
  approved: ['evidence_sufficient', 'aligned_with_strategy', 'approved_other'],
  rejected: [
    'insufficient_evidence',
    'off_strategy',
    'brand_or_compliance_risk',
    'inaccurate',
    'rejected_other',
  ],
  revoked: ['superseded', 'circumstances_changed', 'approved_in_error', 'revoked_other'],
};

describe('approval reason polarity', () => {
  it.each(Object.entries(SERVER_POLARITY))(
    '%s offers exactly the server codes',
    (decision, codes) => {
      expect(reasonsForDecision(decision as ApprovalDecision).map((r) => r.code)).toEqual(codes);
    },
  );
});

describe('approval role predicates (backend policy as the oracle)', () => {
  const roles = [
    'owner',
    'admin',
    'marketer',
    'reviewer',
    'compliance_reviewer',
    'viewer',
  ] as const;

  it('only the reviewer decides (require_exact_roles(REVIEWER))', () => {
    expect(roles.filter(canDecideApprovals)).toEqual(['reviewer']);
  });

  it('every role but viewer reads a decision history', () => {
    expect(roles.filter(canReadApprovalHistory)).toEqual([
      'owner',
      'admin',
      'marketer',
      'reviewer',
      'compliance_reviewer',
    ]);
  });

  it('unknown or missing roles are denied', () => {
    for (const value of [null, undefined, '', 'Reviewer', 'constructor', 'superuser']) {
      expect(canDecideApprovals(value)).toBe(false);
      expect(canReadApprovalHistory(value)).toBe(false);
    }
  });
});

describe('actor snapshots', () => {
  it('shows the name only, never the id or the address', () => {
    expect(snapshotName('u-1 Ada Lovelace <ada@example.com>')).toBe('Ada Lovelace');
    expect(snapshotName('u-1 Ada <Byron> King <ada@example.com>')).toBe('Ada <Byron> King');
    expect(snapshotName('[erased]')).toBe('Erased account');
    expect(snapshotUserId('u-1 Ada Lovelace <ada@example.com>')).toBe('u-1');
    expect(snapshotUserId('[erased]')).toBeNull();
  });

  it('recognises the author by the live link or by the snapshot id', () => {
    const snap = 'u-1 Ada Lovelace <ada@example.com>';
    expect(isBriefAuthor({ created_by: 'u-1', created_by_snapshot: snap }, 'u-1')).toBe(true);
    expect(isBriefAuthor({ created_by: null, created_by_snapshot: snap }, 'u-1')).toBe(true);
    expect(isBriefAuthor({ created_by: null, created_by_snapshot: '[erased]' }, 'u-1')).toBe(false);
    expect(isBriefAuthor({ created_by: 'u-1', created_by_snapshot: snap }, 'u-2')).toBe(false);
    expect(isBriefAuthor({ created_by: 'u-1', created_by_snapshot: snap }, null)).toBe(false);
  });
});
