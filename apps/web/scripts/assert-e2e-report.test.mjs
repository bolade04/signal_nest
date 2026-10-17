import { spawnSync } from 'node:child_process';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { reportProblems } from './assert-e2e-report.mjs';

const SCRIPT = fileURLToPath(new URL('./assert-e2e-report.mjs', import.meta.url));

/** A Playwright JSON report with one chromium test, in the shape the reporter writes. */
function report({ status = 'expected', results = [{ status: 'passed', retry: 0 }], project = 'chromium', stats, errors = [] } = {}) {
  return {
    config: {},
    suites: [
      {
        title: 'customer-flow.spec.ts',
        file: 'customer-flow.spec.ts',
        specs: [{ title: 'the customer flow', ok: status === 'expected', tests: [{ projectName: project, status, results }] }],
        suites: [],
      },
    ],
    errors,
    stats: stats ?? { expected: status === 'expected' ? 1 : 0, unexpected: status === 'unexpected' ? 1 : 0, skipped: status === 'skipped' ? 1 : 0, flaky: status === 'flaky' ? 1 : 0 },
  };
}

function run(file) {
  const r = spawnSync(process.execPath, [SCRIPT, file], { encoding: 'utf8' });
  return { code: r.status, out: r.stdout + r.stderr };
}

describe('assert-e2e-report', () => {
  it('accepts one chromium test that passed on its only attempt', () => {
    expect(reportProblems(report())).toEqual([]);
  });

  it('refuses a skipped test', () => {
    const problems = reportProblems(report({ status: 'skipped', results: [{ status: 'skipped' }] }));
    expect(problems.join('\n')).toMatch(/status "skipped"/);
    expect(problems.join('\n')).toMatch(/stats\.skipped is 1/);
  });

  it('refuses a test that passed only on a retry', () => {
    const problems = reportProblems(report({ status: 'flaky', results: [{ status: 'failed', retry: 0 }, { status: 'passed', retry: 1 }] }));
    expect(problems.join('\n')).toMatch(/2 result\(s\) recorded/);
    expect(problems.join('\n')).toMatch(/stats\.flaky is 1/);
  });

  it('refuses a failed test', () => {
    const problems = reportProblems(report({ status: 'unexpected', results: [{ status: 'failed', retry: 0 }] }));
    expect(problems.join('\n')).toMatch(/attempt status "failed"/);
  });

  it('refuses an empty collection even when the stats are all zero', () => {
    const empty = { config: {}, suites: [], errors: [], stats: { expected: 0, unexpected: 0, skipped: 0, flaky: 0 } };
    expect(reportProblems(empty).join('\n')).toMatch(/no test was collected/);
  });

  it('refuses a test that ran under a project other than chromium', () => {
    expect(reportProblems(report({ project: 'firefox' })).join('\n')).toMatch(/not chromium/);
  });

  it('refuses stats that disagree with the collected tests, and reporter errors', () => {
    expect(reportProblems(report({ stats: { expected: 2, unexpected: 0, skipped: 0, flaky: 0 } })).join('\n')).toMatch(/stats\.expected is 2 but 1/);
    expect(reportProblems(report({ errors: [{ message: 'boom' }] })).join('\n')).toMatch(/1 reporter-level error/);
  });

  it('as a process: exits 0 on a good report, 1 on a refused one, 1 when the report is missing', () => {
    const dir = mkdtempSync(join(tmpdir(), 'e2e-report-'));
    const good = join(dir, 'good.json');
    writeFileSync(good, JSON.stringify(report()));
    const bad = join(dir, 'bad.json');
    writeFileSync(bad, JSON.stringify(report({ status: 'skipped', results: [{ status: 'skipped' }] })));
    expect(run(good).code).toBe(0);
    const refused = run(bad);
    expect(refused.code).toBe(1);
    expect(refused.out).toMatch(/REFUSED/);
    const missing = run(join(dir, 'absent.json'));
    expect(missing.code).toBe(1);
    expect(missing.out).toMatch(/did not execute/);
  });
});
