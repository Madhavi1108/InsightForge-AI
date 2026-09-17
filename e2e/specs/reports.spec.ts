import path from 'path';
import { test, expect } from '../fixtures/test-fixtures';
import { PROJECT_ROOT } from '../helpers/env';
import { assertValidPdf, assertValidXlsx } from '../helpers/reportFiles';

/**
 * Focused, standalone pass over the Reports page: every run row with a
 * working download button must produce a real, well-formed file; every
 * row without one must show the "not available" caption rather than a
 * dead button (streamlit_app/common.py::valid_report_path()'s guarantee).
 */
test('every visible report download is a real, valid file', async ({ page }) => {
  await page.goto('/Reports');
  await expect(page.getByRole('heading', { name: 'Reports', level: 1 })).toBeVisible({
    timeout: 20_000,
  });

  const expanders = page.locator('[data-testid="stExpander"]');
  const emptyState = page.getByText('No pipeline runs yet');
  // The heading renders before the pipeline_runs query resolves - wait
  // for whichever real state actually shows up, rather than assuming.
  await expect(expanders.first().or(emptyState)).toBeVisible({ timeout: 15_000 });
  const count = await expanders.count();
  test.skip(count === 0, 'no pipeline runs yet - run full-pipeline.spec.ts first');

  const maxToCheck = Math.min(count, 5);
  for (let i = 0; i < maxToCheck; i += 1) {
    const expander = expanders.nth(i);
    await expander.click();

    const excelButton = expander.getByRole('button', { name: /download excel/i });
    if (await excelButton.isVisible().catch(() => false)) {
      const downloadPromise = page.waitForEvent('download');
      await excelButton.click();
      const download = await downloadPromise;
      const savePath = path.join(PROJECT_ROOT, 'e2e-report', 'downloads', `run${i}-${await download.suggestedFilename()}`);
      await download.saveAs(savePath);
      const check = assertValidXlsx(savePath);
      expect(check.valid, `row ${i} excel missing sheets: ${check.missingSheets.join(', ')}`).toBeTruthy();
    } else {
      await expect(expander.getByText(/not available/i)).toBeVisible();
    }

    const pdfButton = expander.getByRole('button', { name: /download pdf/i });
    if (await pdfButton.isVisible().catch(() => false)) {
      const downloadPromise = page.waitForEvent('download');
      await pdfButton.click();
      const download = await downloadPromise;
      const savePath = path.join(PROJECT_ROOT, 'e2e-report', 'downloads', `run${i}-${await download.suggestedFilename()}`);
      await download.saveAs(savePath);
      const check = await assertValidPdf(savePath);
      expect(check.valid).toBeTruthy();
    }
  }
});
