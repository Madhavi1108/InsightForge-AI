import crypto from 'crypto';
import fs from 'fs';
import path from 'path';
import { test, expect } from '../fixtures/test-fixtures';
import { PROJECT_ROOT, KILLER_FILE_NAME } from '../helpers/env';
import { currentRunId, waitForRunTerminal } from '../helpers/waitForRun';
import { queryFileRegistryByHash } from '../helpers/db';

/**
 * Depends on full-pipeline.spec.ts having already ingested
 * sales_2026_09_09.csv once (same `serial` Playwright project, run in file
 * order). Resubmitting the identical bytes must be recognized via
 * SHA-256, not reprocessed, and file_registry must still have exactly one
 * row for that hash.
 */
test('resubmitting the same file is detected as a duplicate, not reprocessed', async ({
  page,
}) => {
  const srcFile = path.join(PROJECT_ROOT, 'data', KILLER_FILE_NAME);
  const incomingFile = path.join(PROJECT_ROOT, 'data', 'incoming', KILLER_FILE_NAME);
  const fileHash = crypto.createHash('sha256').update(fs.readFileSync(srcFile)).digest('hex');

  const before = await queryFileRegistryByHash(fileHash);
  test.skip(before.length === 0, 'killer file was not yet ingested by full-pipeline.spec.ts');

  const sinceRunId = await currentRunId(KILLER_FILE_NAME);
  fs.copyFileSync(srcFile, incomingFile);

  await page.goto('/Pipeline');
  await page.getByText('Run one file', { exact: true }).click();
  const fileSelect = page.getByRole('combobox').first();
  await fileSelect.click();
  await fileSelect.fill(KILLER_FILE_NAME.replace('.csv', ''));
  await page.getByRole('option', { name: KILLER_FILE_NAME }).click();
  await page.getByRole('button', { name: 'Run', exact: true }).click();
  await page.waitForFunction(
    () => !document.body.innerText.includes('Running...'),
    { timeout: 60_000 },
  );

  const run = await waitForRunTerminal(KILLER_FILE_NAME, { sinceRunId, timeoutMs: 30_000 });
  expect(run.status).toBe('SKIPPED_DUPLICATE');

  const after = await queryFileRegistryByHash(fileHash);
  expect(after.length).toBe(1);
});
