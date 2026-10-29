import { waitFor, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { afterAll, beforeEach, describe, expect, it, vi } from 'vitest';
import { z } from 'zod';
import type * as HookformZod from '@hookform/resolvers/zod';
import type * as ReactHookForm from 'react-hook-form';
import { App } from '@/App';
import { setAuthToken } from '@/api/client';
import { API_PREFIX } from '@/api/config';
import * as api from '@/api/endpoints';
import type { AccountDeletionPreviewOut } from '@/api/types';
import { orgModel } from '@/test/handlers';
import { server } from '@/test/server';
import {
  distinctForms,
  initialisation,
  markAsserted,
  missingRequiredDefaults,
  observedForms,
  RAW_UNDEFINED_ISSUE,
  requiredInputKeys,
  runtimeAssertedSites,
  runtimeRecordedSites,
  type ObservedForm,
} from '@/test/form-defaults';
import { discoverFormSites, formatProblem } from '@/test/form-discovery';
import { renderApp, renderProductionApp } from '@/test/utils';

/**
 * FU-258-2: every web form built with react-hook-form `useForm` + `zodResolver(schema)`
 * supplies an initial default for each REQUIRED schema input key, so that after the Zod 4
 * upgrade an untouched submit shows the form's own messages and never Zod's raw
 * "Invalid input: expected string, received undefined".
 *
 * Direction: required schema input keys ⊆ defined defaults. Extra default keys are fine;
 * optional / defaulted schema keys (`isOptional()`) are not required. Forms initialised
 * later (`values:` or async `defaultValues`) are classified as deferred and must be
 * exempted explicitly here — none exist today.
 *
 * Both the zodResolver and useForm used below are the REAL implementations, wrapped only to
 * record the schema object and defaults the application itself passes (see
 * `@/test/form-defaults`). The required-key set is read from that schema at runtime.
 *
 * Coverage is per rendered scenario: a NEW useForm + zodResolver form is only checked once
 * a scenario below renders it, so add one alongside any new form.
 *
 * FU-261-1 makes that loud. After all scenarios, an `afterAll` parses every non-test source
 * under `src/` with the TypeScript compiler API (`@/test/form-discovery`) and compares the
 * discovered `file:line` sites with the sites the wrapped `useForm` RECORDED in this run and
 * whose forms passed `expectDefaultsCoverRequiredInputs`. It fails, naming each site, when
 * a discovered form was never exercised, when a recorded site is not discoverable, when an
 * asserted site was never recorded, or when the discoverer meets a shape it does not
 * understand (including a shared wrapper whose schema or defaults come from its caller, and
 * a `useForm` / `zodResolver` imported from any other module). Nothing lists the expected forms.
 * The comparison needs every scenario in this file: under `-t`, `.only` or `.skip` it fails
 * by design (never a vacuous pass), so run the whole file to see a real result.
 */

vi.mock('@hookform/resolvers/zod', async (importOriginal) => {
  const actual = await importOriginal<typeof HookformZod>();
  const { trackZodResolver } = await import('@/test/form-defaults');
  return { ...actual, zodResolver: trackZodResolver(actual.zodResolver) };
});

vi.mock('react-hook-form', async (importOriginal) => {
  const actual = await importOriginal<typeof ReactHookForm>();
  const { trackUseForm } = await import('@/test/form-defaults');
  return { ...actual, useForm: trackUseForm(actual.useForm) };
});

const { zodResolver: realZodResolver } =
  await vi.importActual<typeof HookformZod>('@hookform/resolvers/zod');

/** Measured coverage across this file, printed once at the end. */
const coverage = new Map<string, string[]>();

/**
 * The contract, for every form observed while a scenario ran: static defaults cover each
 * required input key, and the real resolver run on those untouched defaults reports no
 * raw undefined-type issue.
 */
async function expectDefaultsCoverRequiredInputs(expectedSite: RegExp) {
  const forms = distinctForms();
  expect(
    forms.some((form) => expectedSite.test(form.site)),
    `no useForm + zodResolver call observed at ${expectedSite}; observed: ${forms.map((f) => f.site).join(', ')}`,
  ).toBe(true);

  // Every recorded call, not only the first per site: defaults may differ between renders.
  for (const form of observedForms) {
    const init = initialisation(form);
    expect(init.kind, `${form.site}: deferred initialisation must be exempted deliberately`).toBe('static');
    if (init.kind !== 'static') continue;
    const missing = missingRequiredDefaults(form, init.defaults);
    expect(missing, `${form.site}: required schema input keys without a default`).toEqual([]);
  }

  for (const form of forms) {
    await expectResolverMessagesOnDefaults(form);
    coverage.set(form.site, requiredInputKeys(form.schema));
    markAsserted(form);
  }
}

async function expectResolverMessagesOnDefaults(form: ObservedForm) {
  const init = initialisation(form);
  if (init.kind !== 'static') return;
  const result = await realZodResolver(form.schema as never)(init.defaults as never, undefined, {
    fields: {},
    shouldUseNativeValidation: false,
  } as never);
  const errors = result.errors as Record<string, { message?: string; type?: string } | undefined>;
  for (const key of requiredInputKeys(form.schema)) {
    const message = errors[key]?.message;
    if (message === undefined) continue;
    expect(message, `${form.site}: "${key}" on untouched defaults`).not.toMatch(RAW_UNDEFINED_ISSUE);
    expect(errors[key]?.type, `${form.site}: "${key}" on untouched defaults`).not.toBe('invalid_type');
  }
}

/** Submit untouched and read back exactly the inline field messages the form shows. */
async function submitUntouched(screen: ReturnType<typeof renderApp>, scope: HTMLElement, button: RegExp) {
  await screen.user.click(within(scope).getByRole('button', { name: button }));
  const shown = (await within(scope).findAllByRole('alert')).map((alert) => alert.textContent?.trim() ?? '');
  for (const text of shown) expect(text).not.toMatch(RAW_UNDEFINED_ISSUE);
  return shown;
}

async function createInvitationAsOwner(email: string) {
  setAuthToken('test-token');
  try {
    return await api.createInvitation('org-1', { email, role: 'marketer' });
  } finally {
    setAuthToken(null);
  }
}

beforeEach(() => {
  orgModel.reset();
  observedForms.length = 0;
});

/** Non-test application sources, as raw text for the AST discoverer (FU-261-1). */
const appSources = import.meta.glob(
  [
    '/src/**/*.{ts,tsx,mts,cts,js,jsx,mjs,cjs}',
    '!/src/**/__tests__/**',
    '!/src/test/**',
    '!/src/**/*.test.*',
    '!/src/**/*.spec.*',
  ],
  { query: '?raw', import: 'default', eager: true },
) as Record<string, string>;

/**
 * FU-261-1: every useForm + zodResolver site found in the source AST was exercised by a
 * scenario above (recorded at runtime AND passed the defaults assertions), and every site
 * recorded at runtime is discoverable. Runs once after all scenarios in this file.
 */
afterAll(() => {
  const discovery = discoverFormSites(
    Object.fromEntries(Object.entries(appSources).map(([path, text]) => [path.replace(/^\/src\//, ''), text])),
  );
  const discovered = discovery.sites.map((entry) => entry.site);
  const discoveredSet = new Set(discovered);
  const asserted = runtimeAssertedSites();
  const recorded = runtimeRecordedSites();
  const failures: string[] = [];
  if (discovery.filesScanned === 0) failures.push('no application sources were scanned (glob is broken)');
  if (discoveredSet.size === 0) failures.push('no useForm + zodResolver site was discovered in the source');
  if (discoveredSet.size !== discovered.length) failures.push(`two discovered forms share one site label: ${discovered.join(', ')}`);
  for (const problem of discovery.problems) failures.push(`unsupported shape: ${formatProblem(problem)}`);
  for (const site of discoveredSet) {
    if (!asserted.has(site)) {
      failures.push(
        `${site}: useForm + zodResolver form in source, but no scenario in this file rendered it ` +
          `and passed expectDefaultsCoverRequiredInputs${recorded.has(site) ? ' (it was rendered, not asserted)' : ''}`,
      );
    }
  }
  for (const site of recorded) {
    if (!discoveredSet.has(site)) failures.push(`${site}: recorded at runtime but not discovered in the source`);
  }
  // Review r1 F5: coverage can only be granted for a site the wrapper itself recorded.
  for (const site of asserted) {
    if (!recorded.has(site)) failures.push(`${site}: asserted but never recorded at runtime by the useForm wrapper`);
  }
  console.info(
    `[FU-261-1] files scanned: ${discovery.filesScanned}; sites discovered: ${discoveredSet.size}; ` +
      `recorded at runtime: ${recorded.size}; asserted: ${asserted.size}; problems: ${discovery.problems.length}`,
  );
  if (failures.length > 0) {
    throw new Error(
      `[FU-261-1] form discovery and runtime coverage disagree:\n${failures.map((line) => `  - ${line}`).join('\n')}\n` +
        'This check compares across every scenario in this file; under -t, .only or .skip it fails by design.',
    );
  }
});

afterAll(() => {
  const keys = [...coverage.values()].reduce((total, list) => total + list.length, 0);
  console.info(
    `[FU-258-2] useForm + zodResolver sites covered: ${coverage.size}; required input keys asserted: ${keys}\n` +
      [...coverage.entries()].map(([site, list]) => `  ${site}: ${list.join(', ')}`).join('\n'),
  );
});

describe('required input keys are read from the schema (helper sanity)', () => {
  it('counts only keys that reject undefined; optional, defaulted and caught keys are exempt', () => {
    const schema = z.object({
      a: z.string(),
      b: z.string().optional(),
      c: z.string().default('x'),
      d: z.custom<string>((value) => typeof value === 'string'),
      e: z.string().catch(''),
    });
    expect(requiredInputKeys(schema)).toEqual(['a', 'd']);
  });
});

describe('public auth forms submit untouched with their own messages (FU-258-2)', () => {
  it('sign in', async () => {
    const screen = renderApp(<App />, { route: '/sign-in', authed: false });
    const button = await screen.findByRole('button', { name: /^sign in$/i });
    const shown = await submitUntouched(screen, button.closest('form')!, /^sign in$/i);
    expect(shown).toEqual(expect.arrayContaining(['Email is required', 'Password is required']));
    await expectDefaultsCoverRequiredInputs(/pages\/auth\/SignIn\.tsx/);
  });

  it('register', async () => {
    const screen = renderApp(<App />, { route: '/register', authed: false });
    const button = await screen.findByRole('button', { name: /^create account$/i });
    const shown = await submitUntouched(screen, button.closest('form')!, /^create account$/i);
    expect(shown).toEqual(
      expect.arrayContaining([
        'Your name is required',
        'Organization name is required',
        'Email is required',
        'Use at least 8 characters',
      ]),
    );
    await expectDefaultsCoverRequiredInputs(/pages\/auth\/Register\.tsx/);
  });

  it('forgot password', async () => {
    const screen = renderProductionApp(<App />, '/forgot-password');
    const button = await screen.findByRole('button', { name: /send reset link/i });
    const shown = await submitUntouched(screen, button.closest('form')!, /send reset link/i);
    expect(shown).toContain('Email is required');
    await expectDefaultsCoverRequiredInputs(/pages\/auth\/ForgotPassword\.tsx/);
  });

  it('reset password', async () => {
    orgModel.addUser({ id: 'user-rita', email: 'rita@example.com', full_name: 'Rita Reset' });
    const token = orgModel.issuePasswordResetToken('user-rita');
    const screen = renderProductionApp(<App />, `/reset-password#token=${encodeURIComponent(token)}`);
    const button = await screen.findByRole('button', { name: /^reset password$/i });
    const shown = await submitUntouched(screen, button.closest('form')!, /^reset password$/i);
    expect(shown).toEqual(expect.arrayContaining(['Use at least 8 characters', 'Confirm your new password']));
    await expectDefaultsCoverRequiredInputs(/pages\/auth\/ResetPassword\.tsx/);
  });

  it('verify email (signed-out inline sign-in)', async () => {
    orgModel.addUser({ id: 'user-vera', email: 'vera@example.com', full_name: 'Vera Verify', email_verified: false });
    const token = orgModel.issueEmailVerificationToken('user-vera');
    const screen = renderProductionApp(<App />, `/verify-email#token=${encodeURIComponent(token)}`);
    await screen.findByRole('heading', { name: /sign in to verify your email address/i });
    const button = screen.getByRole('button', { name: /^sign in$/i });
    const shown = await submitUntouched(screen, button.closest('form')!, /^sign in$/i);
    expect(shown).toEqual(expect.arrayContaining(['Email is required', 'Password is required']));
    await expectDefaultsCoverRequiredInputs(/pages\/auth\/VerifyEmail\.tsx/);
  });

  it('invitation: create account, then sign in to accept', async () => {
    const { token } = await createInvitationAsOwner('nia.new@example.com');
    const screen = renderProductionApp(<App />, `/invite#token=${token}`);
    const create = await screen.findByRole('button', { name: /create account and join/i });
    const registerShown = await submitUntouched(screen, create.closest('form')!, /create account and join/i);
    expect(registerShown).toEqual(expect.arrayContaining(['Your name is required', 'Use at least 8 characters']));

    await screen.user.click(screen.getByRole('button', { name: /sign in to accept/i }));
    const signIn = await screen.findByRole('button', { name: /^sign in$/i });
    const signInShown = await submitUntouched(screen, signIn.closest('form')!, /^sign in$/i);
    expect(signInShown).toContain('Password is required');

    await expectDefaultsCoverRequiredInputs(/pages\/invite\/InvitePage\.tsx/);
    const sites = distinctForms().map((form) => form.site);
    expect(sites.filter((site) => /pages\/invite\/InvitePage\.tsx/.test(site))).toHaveLength(2);
  });
});

describe('settings forms submit untouched with their own messages (FU-258-2)', () => {
  async function settingsAsOwner() {
    orgModel.setMembership('org-1', 'user-1', 'owner');
    const screen = renderApp(<App />, { route: '/settings' });
    await screen.findByRole('heading', { name: 'Account' });
    return screen;
  }

  it('create workspace', async () => {
    const screen = await settingsAsOwner();
    await screen.user.click(await screen.findByRole('button', { name: /^new$/i }));
    const dialog = await screen.findByRole('dialog', { name: /create workspace/i });
    const shown = await submitUntouched(screen, dialog, /^create$/i);
    expect(shown).toContain('Workspace name is required');
    await expectDefaultsCoverRequiredInputs(/pages\/Settings\.tsx/);
  });

  it('change password', async () => {
    const screen = await settingsAsOwner();
    await screen.user.click(await screen.findByRole('button', { name: /^change password$/i }));
    const dialog = await screen.findByRole('dialog', { name: /^change password$/i });
    const shown = await submitUntouched(screen, dialog, /^change password$/i);
    expect(shown).toEqual(
      expect.arrayContaining(['Enter your current password', 'Use at least 8 characters', 'Confirm your new password']),
    );
    await expectDefaultsCoverRequiredInputs(/pages\/settings\/ChangePasswordDialog\.tsx/);
  });

  it('invite member (role defaults to the least privileged invitable role)', async () => {
    const screen = await settingsAsOwner();
    await screen.user.click(await screen.findByRole('button', { name: /invite member/i }));
    const dialog = await screen.findByRole('dialog', { name: /invite member/i });
    const shown = await submitUntouched(screen, dialog, /create invitation/i);
    expect(shown).toContain('Enter an email address');
    // The role default is a valid choice, so no role message appears at all.
    expect(shown).not.toContain('Choose a role');
    await expectDefaultsCoverRequiredInputs(/pages\/settings\/Invitations\.tsx/);
  });

  // P6-PRIV-1: the two deletion forms in DataPrivacy.tsx. The account form's submit stays
  // disabled until the deletion preview answers, so the scenario serves one that allows it.
  const DELETABLE_PREVIEW: AccountDeletionPreviewOut = {
    can_delete: true,
    organizations: [],
    email_keyed_data: { status: 'included', note: 'Your email address is verified.' },
  };

  it('delete account', async () => {
    server.use(http.get(`*${API_PREFIX}/account/deletion-preview`, () => HttpResponse.json(DELETABLE_PREVIEW)));
    const screen = await settingsAsOwner();
    await screen.user.click(await screen.findByRole('button', { name: /delete account/i }));
    const dialog = await screen.findByRole('dialog', { name: /delete account/i });
    await waitFor(() => expect(within(dialog).getByRole('button', { name: /delete my account/i })).toBeEnabled());
    const shown = await submitUntouched(screen, dialog, /delete my account/i);
    expect(shown).toEqual(
      expect.arrayContaining(['Enter your current password', 'Type DELETE, in capitals, to confirm']),
    );
    await expectDefaultsCoverRequiredInputs(/pages\/settings\/DataPrivacy\.tsx/);
  });

  it('delete organization (owner only)', async () => {
    const screen = await settingsAsOwner();
    await screen.user.click(await screen.findByRole('button', { name: /delete organization/i }));
    const dialog = await screen.findByRole('dialog', { name: /delete organization/i });
    const shown = await submitUntouched(screen, dialog, /^delete organization$/i);
    expect(shown).toEqual(
      expect.arrayContaining(['Enter your current password', 'Type the organization’s name to confirm']),
    );
    await expectDefaultsCoverRequiredInputs(/pages\/settings\/DataPrivacy\.tsx/);
  });
});
