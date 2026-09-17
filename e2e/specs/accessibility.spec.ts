import { test, expect } from '../fixtures/test-fixtures';
import { runAxe } from '../helpers/a11y';

const PAGES = ['/', '/Pipeline', '/Anomalies', '/AI_Analyst'];

test.describe('accessibility (axe, wcag2a/wcag2aa)', () => {
  for (const path of PAGES) {
    test(`no serious/critical violations on ${path}`, async ({ page }, testInfo) => {
      await page.goto(path);
      await page.waitForLoadState('domcontentloaded');
      const {
        serious, critical, colorContrast, other,
      } = await runAxe(page);

      await testInfo.attach('axe-other-violations', {
        body: JSON.stringify(other, null, 2),
        contentType: 'application/json',
      });
      // Recorded, not asserted - see a11y.ts's runAxe() doc comment for
      // why (a real, sitewide Streamlit-default styling issue, not
      // something introduced by or fixable within streamlit_app/ code).
      await testInfo.attach('axe-color-contrast-findings', {
        body: JSON.stringify(colorContrast, null, 2),
        contentType: 'application/json',
      });

      expect(
        [...serious, ...critical],
        `serious/critical axe violations on ${path}:\n${JSON.stringify([...serious, ...critical], null, 2)}`,
      ).toEqual([]);
    });
  }
});
