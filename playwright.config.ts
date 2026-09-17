import { defineConfig, devices } from '@playwright/test';

/**
 * Streamlit reruns over an internal WebSocket, not REST - `networkidle`
 * never reliably settles against it. All synchronization in this suite
 * uses Playwright's own auto-retrying `expect()` against UI state, or
 * direct-DB polling (see e2e/helpers/waitForRun.ts) - never
 * page.waitForTimeout(). The default `expect.timeout` below is generous
 * because a Streamlit rerun is slower than a typical SPA state change;
 * the pipeline-run wait itself uses its own much longer explicit timeout.
 */
export default defineConfig({
  testDir: './e2e/specs',
  globalSetup: require.resolve('./e2e/global-setup.ts'),
  globalTeardown: require.resolve('./e2e/global-teardown.ts'),
  timeout: 240_000,
  expect: { timeout: 15_000 },
  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: 0,
  reporter: [
    ['html', { outputFolder: 'e2e-report', open: 'never' }],
    ['json', { outputFile: 'e2e-results.json' }],
    ['list'],
  ],
  use: {
    baseURL: 'http://localhost:8501',
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    // Real installed Google Chrome, not Playwright's bundled Chromium -
    // required by the brief. Confirmed present on this host at
    // C:\Program Files\Google\Chrome\Application\chrome.exe (v153.0.8010.48).
    channel: 'chrome',
    headless: false,
  },
  projects: [
    {
      name: 'serial',
      testMatch: [
        'full-pipeline.spec.ts',
        'db-unavailable.spec.ts',
        'resilience.spec.ts',
        'duplicate-resubmit.spec.ts',
      ],
      fullyParallel: false,
      workers: 1,
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'parallel',
      testMatch: [
        'pages.spec.ts',
        'security.spec.ts',
        'prompt-injection.spec.ts',
        'accessibility.spec.ts',
        'reports.spec.ts',
        'console-network.spec.ts',
        'responsive.spec.ts',
        'data-change.spec.ts',
      ],
      dependencies: ['serial'],
      // Streamlit's dev server is a single process - confirmed via real
      // runs that several simultaneous worker sessions cause it to serve
      // blank/unresponsive pages under real (not simulated) load. workers:2
      // keeps some parallelism while staying within what the dev server
      // reliably handles.
      workers: 2,
      use: { ...devices['Desktop Chrome'] },
    },
  ],
});
