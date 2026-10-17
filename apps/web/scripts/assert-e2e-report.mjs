#!/usr/bin/env node
/**
 * P6-CI-2 — the browser proof must have EXECUTED, in full, once.
 *
 * Playwright exits 0 when every test that ran passed — including when a test was
 * skipped, when nothing was collected, or when a test passed only on a retry. A
 * required CI step that stopped at that exit code would report "green" for a proof
 * that never ran. This reads the JSON report the harness writes and refuses anything
 * but: the customer flow itself (the test titled as in e2e/flow.json, with every step
 * listed there executed — an emptied or substituted test is refused), executed exactly
 * once (no retry) and passed, zero skipped, zero flaky, zero unexpected, zero reporter
 * errors, and every test under the chromium project. A missing or unreadable report is
 * a failure too: "no report" means the browser test did not execute.
 *
 * Node builtins only — this must not add a dependency.
 */
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const EXPECTED_PROJECT = 'chromium';
/** The flow the report must be a report OF: one source, shared with the spec. */
const FLOW = JSON.parse(readFileSync(fileURLToPath(new URL('../e2e/flow.json', import.meta.url)), 'utf8'));

/** Every spec in the tree, with its file, flattened; nested describe suites included. */
function collectSpecs(suites, out = []) {
  for (const suite of suites ?? []) {
    for (const spec of suite.specs ?? []) out.push(spec);
    collectSpecs(suite.suites, out);
  }
  return out;
}

/** Pure: the list of problems with a parsed report (empty = acceptable). Exported for the unit test; `flow` defaults to e2e/flow.json. */
export function reportProblems(report, flow = FLOW) {
  const problems = [];
  const stats = report?.stats;
  if (!stats || typeof stats !== 'object') {
    problems.push('the report carries no stats block');
    return problems;
  }
  const specs = collectSpecs(report.suites);
  const tests = specs.flatMap((spec) =>
    (spec.tests ?? []).map((t) => ({ title: spec.title, project: t.projectName, status: t.status, results: t.results ?? [] })),
  );
  if (tests.length === 0) problems.push('no test was collected — the browser proof did not execute');
  // The report must be a report of the flow: exactly that test, with every step run.
  const flowTests = tests.filter((t) => t.title === flow.title);
  if (flowTests.length !== 1) problems.push(`the flow test ${JSON.stringify(flow.title)} appears ${flowTests.length} time(s) — the report is not a report of the customer flow`);
  for (const t of tests) {
    if (t.title !== flow.title) problems.push(`unexpected test ${JSON.stringify(t.title)} — only the customer flow is accepted`);
  }
  for (const t of flowTests) {
    const ran = (t.results[0]?.steps ?? []).map((s) => s.title);
    for (const step of flow.steps) {
      if (!ran.includes(step)) problems.push(`step ${JSON.stringify(step)} did not run (steps recorded: ${ran.length})`);
    }
  }
  for (const t of tests) {
    const label = `${t.project ?? '?'} › ${t.title}`;
    if (t.project !== EXPECTED_PROJECT) problems.push(`${label}: ran under project ${JSON.stringify(t.project)}, not ${EXPECTED_PROJECT}`);
    if (t.status !== 'expected') problems.push(`${label}: status ${JSON.stringify(t.status)} (skipped, flaky or failed tests are refused)`);
    if (t.results.length !== 1) problems.push(`${label}: ${t.results.length} result(s) recorded — exactly one attempt is required (retries would hide an unstable first run)`);
    for (const r of t.results) {
      if (r.status !== 'passed') problems.push(`${label}: attempt status ${JSON.stringify(r.status)}`);
    }
  }
  for (const [key, want] of [['unexpected', 0], ['skipped', 0], ['flaky', 0]]) {
    if (stats[key] !== want) problems.push(`stats.${key} is ${stats[key]}, expected ${want}`);
  }
  if (stats.expected !== tests.length) problems.push(`stats.expected is ${stats.expected} but ${tests.length} test(s) were collected`);
  if ((report.errors ?? []).length !== 0) problems.push(`${report.errors.length} reporter-level error(s) recorded`);
  return problems;
}

export function summarize(report) {
  const tests = collectSpecs(report.suites).flatMap((s) => s.tests ?? []);
  const projects = [...new Set(tests.map((t) => t.projectName))];
  const s = report.stats ?? {};
  const steps = tests[0]?.results?.[0]?.steps?.length ?? 0;
  return `${tests.length} test(s) collected (${steps} step(s) recorded); expected ${s.expected}, unexpected ${s.unexpected}, skipped ${s.skipped}, flaky ${s.flaky}; project(s): ${projects.join(', ') || 'none'}`;
}

function main(argv) {
  const path = argv[2];
  if (!path) {
    console.error('[e2e-report-gate] usage: assert-e2e-report.mjs <report.json>');
    return 2;
  }
  let report;
  try {
    report = JSON.parse(readFileSync(path, 'utf8'));
  } catch (err) {
    console.error(`[e2e-report-gate] no readable JSON report at ${path}: the browser test did not execute (${err.message})`);
    return 1;
  }
  const problems = reportProblems(report);
  if (problems.length > 0) {
    console.error(`[e2e-report-gate] REFUSED — ${summarize(report)}`);
    for (const p of problems) console.error(`  - ${p}`);
    return 1;
  }
  console.log(`[e2e-report-gate] OK — ${summarize(report)}`);
  return 0;
}

// Importable for the unit test; executable from the harness.
if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) {
  process.exit(main(process.argv));
}
