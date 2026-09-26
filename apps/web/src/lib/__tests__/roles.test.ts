import { afterEach, describe, expect, it } from 'vitest';
import {
  actorRoleFor,
  canAdminOrganization,
  canCreateWorkspace,
  canEditWorkspace,
  canGrantRole,
  canInviteMembers,
  canManageMember,
  canReadAuditLogs,
  invitableRoles,
  isOwnerRole,
  isRole,
  ROLE_RANK,
  ROLES,
} from '@/lib/roles';
import { orgModel } from '@/test/handlers';

// §25–§27, §36–§37. Every expectation below is written out from the BACKEND, not
// derived from lib/roles.ts, so a wrong helper cannot agree with itself:
//   _ROLE_RANK                         apps/api/app/auth/dependencies.py:23-30
//   EDITORS  = require_role(OWNER, ADMIN, MARKETER), a rank floor   dependencies.py:210-219
//   ORGANIZATION_ADMINS = exact OWNER, ADMIN                        dependencies.py:176-207
//   audit    = require_exact_roles(OWNER, ADMIN, COMPLIANCE_REVIEWER)  audit/routes.py:20-22
//   invitations: never OWNER, never above the inviter              organizations/invitations.py:380-406
//   member role change / removal rules                             organizations/members.py:205-290

// Pure functions, no requests — but like every M4 file it proves the backend model saw
// no contract violation.
afterEach(() => {
  expect(orgModel.violations()).toEqual([]);
});

const SIX = ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'] as const;
type Six = (typeof SIX)[number];

// Not a role: missing, empty, case variants, whitespace, prototype keys, invented.
const NOT_ROLES = [null, undefined, '', 'Owner', 'OWNER', ' owner', 'owner ', 'constructor', '__proto__', 'toString', 'superuser'];

describe('role rank mirrors the backend _ROLE_RANK exactly (§26)', () => {
  it('pins every rank, including the reviewer / compliance_reviewer tie', () => {
    expect({ ...ROLE_RANK }).toEqual({
      viewer: 0,
      reviewer: 1,
      compliance_reviewer: 1,
      marketer: 2,
      admin: 3,
      owner: 4,
    });
  });

  it('knows exactly the six contract roles and nothing else', () => {
    expect([...ROLES].sort()).toEqual([...SIX].sort());
    for (const r of SIX) expect(isRole(r)).toBe(true);
    for (const r of NOT_ROLES) expect(isRole(r)).toBe(false);
  });
});

describe('policy predicates × six roles (§25, §36, §37)', () => {
  const table: Record<string, [(r: string) => boolean, Six[]]> = {
    canEditWorkspace: [canEditWorkspace, ['owner', 'admin', 'marketer']],
    canAdminOrganization: [canAdminOrganization, ['owner', 'admin']],
    canCreateWorkspace: [canCreateWorkspace, ['owner', 'admin']],
    canInviteMembers: [canInviteMembers, ['owner', 'admin']],
    canReadAuditLogs: [canReadAuditLogs, ['owner', 'admin', 'compliance_reviewer']],
  };

  it.each(Object.entries(table))('%s admits exactly the backend set', (_name, [predicate, admitted]) => {
    for (const role of SIX) expect(predicate(role), role).toBe(admitted.includes(role));
  });

  it.each(Object.entries(table))('%s fails closed for anything that is not a role', (_name, [predicate]) => {
    for (const value of NOT_ROLES) expect(predicate(value as string)).toBe(false);
  });

  it('never treats reading the audit log as editing (compliance reviewer)', () => {
    expect(canReadAuditLogs('compliance_reviewer')).toBe(true);
    expect(canEditWorkspace('compliance_reviewer')).toBe(false);
    expect(canAdminOrganization('compliance_reviewer')).toBe(false);
    // A marketer edits but may not read the audit log: a duty boundary, not a rank.
    expect(canEditWorkspace('marketer')).toBe(true);
    expect(canReadAuditLogs('marketer')).toBe(false);
  });
});

describe('invitable roles (§27, §75)', () => {
  it('offers owners and admins the five non-owner roles, most privileged first', () => {
    const five = ['admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'];
    expect(invitableRoles('owner')).toEqual(five);
    expect(invitableRoles('admin')).toEqual(five);
  });

  it('never contains owner, for any input', () => {
    for (const actor of [...SIX, ...NOT_ROLES]) {
      expect(invitableRoles(actor as string)).not.toContain('owner');
    }
  });

  it('offers nothing to roles that cannot invite', () => {
    for (const actor of ['marketer', 'reviewer', 'compliance_reviewer', 'viewer', ...NOT_ROLES]) {
      expect(invitableRoles(actor as string)).toEqual([]);
    }
  });
});

describe('member management mirrors members.py (§28, §29, §52, §77, §78)', () => {
  // canGrantRole(actor, newRole): members.py:229-239.
  const GRANTABLE: Record<Six, Six[]> = {
    owner: ['owner', 'admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'],
    admin: ['admin', 'marketer', 'reviewer', 'compliance_reviewer', 'viewer'],
    marketer: [],
    reviewer: [],
    compliance_reviewer: [],
    viewer: [],
  };
  // canManageMember(actor, targetRole, { isSelf: false }): members.py:222-239, :272-289.
  const MANAGEABLE: Record<Six, Six[]> = GRANTABLE;

  it.each(SIX)('actor %s: grantable roles', (actor) => {
    for (const role of SIX) expect(canGrantRole(actor, role), `${actor} -> ${role}`).toBe(GRANTABLE[actor].includes(role));
  });

  it.each(SIX)('actor %s: manageable targets (not self)', (actor) => {
    for (const target of SIX) {
      expect(canManageMember(actor, target, { isSelf: false }), `${actor} -> ${target}`).toBe(
        MANAGEABLE[actor].includes(target),
      );
    }
  });

  it('never lets anyone manage themselves', () => {
    for (const actor of SIX) for (const target of SIX) expect(canManageMember(actor, target, { isSelf: true })).toBe(false);
  });

  it('never lets an admin touch or create an owner', () => {
    expect(canGrantRole('admin', 'owner')).toBe(false);
    expect(canManageMember('admin', 'owner', { isSelf: false })).toBe(false);
    expect(canGrantRole('owner', 'owner')).toBe(true);
  });

  it('fails closed for unknown actors and targets', () => {
    for (const value of NOT_ROLES) {
      expect(canGrantRole(value as string, 'viewer')).toBe(false);
      expect(canGrantRole('owner', value as string)).toBe(false);
      expect(canManageMember(value as string, 'viewer', { isSelf: false })).toBe(false);
      expect(canManageMember('owner', value as string, { isSelf: false })).toBe(false);
    }
  });
});

describe('owner detection for the last-owner rule (§79)', () => {
  it('isOwnerRole is true for owner alone, and fails closed', () => {
    for (const role of SIX) expect(isOwnerRole(role), role).toBe(role === 'owner');
    for (const value of NOT_ROLES) expect(isOwnerRole(value as string)).toBe(false);
  });
});

describe('actor role resolution (§16)', () => {
  const memberships = [
    { organization_id: 'org-a', role: 'owner' },
    { organization_id: 'org-b', role: 'viewer' },
  ];

  it('reads the role for the ACTIVE organization, not the first membership', () => {
    expect(actorRoleFor(memberships, 'org-b')).toEqual({ status: 'known', role: 'viewer' });
    expect(actorRoleFor(memberships, 'org-a')).toEqual({ status: 'known', role: 'owner' });
  });

  it('is unknown while anything is unresolved or unrecognised', () => {
    expect(actorRoleFor([], 'org-a').status).toBe('unknown');
    expect(actorRoleFor(memberships, null).status).toBe('unknown');
    expect(actorRoleFor(memberships, 'org-c').status).toBe('unknown');
    expect(actorRoleFor([{ organization_id: 'org-a', role: 'superuser' }], 'org-a')).toEqual({
      status: 'unknown',
      role: null,
    });
  });
});

describe('role policy lives only in lib/roles.ts (§25)', () => {
  // Every non-test source module, as text. Generated types, the policy module
  // itself and the label map (which names roles but never compares them) are the
  // only places a role literal may legitimately appear.
  const sources = import.meta.glob(['/src/**/*.{ts,tsx}', '!/src/**/__tests__/**', '!/src/test/**'], {
    query: '?raw',
    import: 'default',
    eager: true,
  }) as Record<string, string>;
  const ROLE = '(?:owner|admin|marketer|reviewer|viewer|compliance_reviewer)';
  const scattered = [
    new RegExp(`\\brole\\w*\\s*[!=]==?\\s*['"]${ROLE}['"]`), // role === 'owner'
    new RegExp(`['"]${ROLE}['"]\\s*[!=]==?\\s*\\w*\\.?role\\b`, 'i'), // 'owner' === m.role
    /\bEDITOR_ROLES\b/,
    new RegExp(`new Set(?:<[^>]*>)?\\(\\s*\\[\\s*['"]${ROLE}['"]`), // new Set(['owner', ...])
    new RegExp(`\\[\\s*['"]${ROLE}['"]\\s*,[^\\]]*\\]\\s*\\.includes\\(`), // ['owner','admin'].includes(
  ];

  it('catches each pattern it claims to (positive instances)', () => {
    const planted = [
      "const EDITOR_ROLES = new Set(['owner', 'admin', 'marketer']);", // base SchedulePanel.tsx:22
      "const canEdit = role === 'owner' || role === 'admin';",
      "if ('compliance_reviewer' === membership.role) return;",
      "const isAdmin = ['owner', 'admin'].includes(role);",
      "const staff = new Set<string>(['admin', 'owner']);",
    ];
    for (const line of planted) expect(scattered.some((re) => re.test(line)), line).toBe(true);
  });

  it('scans a real source tree', () => {
    // Liveness: the glob found the pages that used to carry local role lists.
    expect(Object.keys(sources)).toEqual(
      expect.arrayContaining(['/src/pages/scouts/SchedulePanel.tsx', '/src/pages/opportunities/FeedbackPanel.tsx', '/src/lib/roles.ts']),
    );
  });

  it('has no role-string comparison or local role list outside lib/roles.ts', () => {
    const offenders: string[] = [];
    for (const [path, text] of Object.entries(sources)) {
      if (path === '/src/lib/roles.ts' || path === '/src/api/schema.d.ts') continue;
      text.split('\n').forEach((line, i) => {
        if (scattered.some((re) => re.test(line))) offenders.push(`${path}:${i + 1}: ${line.trim()}`);
      });
    }
    expect(offenders).toEqual([]);
  });
});
