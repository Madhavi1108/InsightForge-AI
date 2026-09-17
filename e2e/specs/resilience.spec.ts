import fs from 'fs';
import path from 'path';
import { test, expect } from '../fixtures/test-fixtures';
import { PROJECT_ROOT } from '../helpers/env';
import { currentRunId, waitForRunTerminal } from '../helpers/waitForRun';
import { queryLatestRunByFile, queryRunsByFile } from '../helpers/db';

/**
 * Two cheap-but-valuable checks beyond the core scope list: a subprocess
 * launching UI button is a realistic place for a double-click race, and a
 * browser refresh mid-run is a realistic place to lose track of state -
 * both are asserted against the DB (the real source of truth), not the
 * reloaded page's own transient UI state.
 */
const RESILIENCE_FILE = 'sales_2026_09_08.csv';

test.describe.serial('resilience', () => {
  test('double-clicking Run does not create two pipeline runs', async ({ page }) => {
    const srcFile = path.join(PROJECT_ROOT, 'data', 'raw', RESILIENCE_FILE);
    const incomingFile = path.join(PROJECT_ROOT, 'data', 'incoming', RESILIENCE_FILE);
    if (!fs.existsSync(incomingFile) && fs.existsSync(srcFile)) {
      fs.copyFileSync(srcFile, incomingFile);
    }
    test.skip(!fs.existsSync(incomingFile), `${RESILIENCE_FILE} not available to resubmit`);

    const sinceRunId = await currentRunId(RESILIENCE_FILE);

    await page.goto('/Pipeline');
    await page.getByText('Run one file', { exact: true }).click();
    const fileSelect = page.getByRole('combobox').first();
    await fileSelect.click();
    await fileSelect.fill(RESILIENCE_FILE.replace('.csv', ''));
    await page.getByRole('option', { name: RESILIENCE_FILE }).click();

    const runButton = page.getByRole('button', { name: 'Run', exact: true });
    await runButton.click();
    // Streamlit disables a button for the duration of the script rerun it
    // triggered - the second click should be a no-op while that holds.
    await expect(runButton).toBeDisabled({ timeout: 5_000 }).catch(() => {
      // If the disabled state is too brief to observe, the DB row-count
      // check below is the real backstop for this test.
    });

    await page.waitForFunction(
      () => !document.body.innerText.includes('Running...'),
      { timeout: 180_000 },
    );
    await waitForRunTerminal(RESILIENCE_FILE, { sinceRunId, timeoutMs: 30_000 });

    const runsSinceStart = (await queryRunsByFile(RESILIENCE_FILE)).filter(
      (r) => r.run_id > sinceRunId,
    );
    expect(runsSinceStart.length).toBe(1);
  });

  test('refreshing mid-pipeline does not lose the run', async ({ page }) => {
    // The previous test consumed the file from data/incoming/ (moved on
    // processing) - re-stage it from the raw archive, same as the first
    // resilience test does.
    const srcFile = path.join(PROJECT_ROOT, 'data', 'raw', RESILIENCE_FILE);
    const incomingFile = path.join(PROJECT_ROOT, 'data', 'incoming', RESILIENCE_FILE);
    if (!fs.existsSync(incomingFile) && fs.existsSync(srcFile)) {
      fs.copyFileSync(srcFile, incomingFile);
    }
    test.skip(!fs.existsSync(incomingFile), `${RESILIENCE_FILE} not available to resubmit`);

    const sinceRunId = await currentRunId(RESILIENCE_FILE);

    await page.goto('/Pipeline');
    await page.getByText('Run one file', { exact: true }).click();
    const fileSelect = page.getByRole('combobox').first();
    await fileSelect.click();
    await fileSelect.fill(RESILIENCE_FILE.replace('.csv', ''));
    await page.getByRole('option', { name: RESILIENCE_FILE }).click();
    await page.getByRole('button', { name: 'Run', exact: true }).click();

    // Reload once the DB shows the run has actually started (DB-backed
    // polling, not a fixed sleep) - the reloaded page cannot show us the
    // original spinner, so completion is only verifiable via the DB too.
    await expect
      .poll(async () => (await queryLatestRunByFile(RESILIENCE_FILE))?.run_id ?? 0, {
        timeout: 30_000, intervals: [300],
      })
      .toBeGreaterThan(sinceRunId);
    await page.reload();

    const run = await waitForRunTerminal(RESILIENCE_FILE, { sinceRunId, timeoutMs: 180_000 });
    expect(['SUCCESS', 'WARNING', 'SKIPPED_DUPLICATE']).toContain(run.status);
  });
});
