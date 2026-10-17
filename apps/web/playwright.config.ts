import { defineConfig, devices } from '@playwright/test';

/**
 * Browser/E2E harness for the customer flow (P6-CI-2).
 *
 * What runs here is the BUILT production bundle (`dist/`, CSP meta included), served
 * by `vite preview` with the same `/api` + `/health` reverse proxy the dev server uses,
 * so the SPA talks same-origin to a real local API exactly as it does behind the edge
 * with an empty `VITE_API_BASE_URL`. Nothing is mocked in the browser: no MSW, no
 * route interception. The API, the durable worker and the disposable migrated
 * database are started by `scripts/e2e-browser.sh`, which is the only supported entry
 * point (it also gates on the JSON report written below).
 *
 * Coverage boundary: ONE browser — the Playwright-pinned Chromium build — at one
 * desktop viewport. Firefox and WebKit are not exercised.
 *
 * Fail-closed choices: `retries: 0` (an unstable first attempt is a failure, never
 * hidden by a retry), one worker (the flow is one ordered story), `forbidOnly` always
 * (a focused subset cannot pass as the whole, in CI or locally — this harness has no
 * other supported entry point), `reuseExistingServer: false` (a stale preview on the
 * port is an error, never silently reused).
 */
const webPort = Number(process.env.E2E_WEB_PORT ?? '4173');
const apiProxyTarget = process.env.VITE_API_PROXY_TARGET ?? 'http://127.0.0.1:8765';
const resultsDir = process.env.E2E_RESULTS_DIR ?? 'e2e-results';
const baseURL = `http://127.0.0.1:${webPort}`;

export default defineConfig({
  testDir: './e2e',
  outputDir: `${resultsDir}/artifacts`,
  fullyParallel: false,
  workers: 1,
  retries: 0,
  forbidOnly: true,
  // One bound for the whole story (registration through sign-out); the run itself is
  // waited for with an explicit, shorter expectation inside the spec.
  timeout: 180_000,
  expect: { timeout: 15_000 },
  reporter: [['list'], ['json', { outputFile: `${resultsDir}/report.json` }]],
  use: {
    baseURL,
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'off',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: `vite preview --host 127.0.0.1 --port ${webPort} --strictPort`,
    url: baseURL,
    reuseExistingServer: false,
    timeout: 30_000,
    env: { VITE_API_PROXY_TARGET: apiProxyTarget },
  },
});
