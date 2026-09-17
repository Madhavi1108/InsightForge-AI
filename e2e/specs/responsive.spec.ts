import { test, expect } from '../fixtures/test-fixtures';

/**
 * Responsive Chrome testing at the three requested resolutions. Verifies
 * no horizontal page overflow (a real usability defect on a data-dense
 * dashboard) and that the primary navigation/controls remain visible and
 * interactable - not a pixel-perfect design review.
 */
const VIEWPORTS = [
  { name: '1366x768', width: 1366, height: 768 },
  { name: '1536x864', width: 1536, height: 864 },
  { name: '1280x720', width: 1280, height: 720 },
];

const PAGES = ['/', '/Pipeline', '/Anomalies'];

for (const viewport of VIEWPORTS) {
  test.describe(`responsive @ ${viewport.name}`, () => {
    test.use({ viewport: { width: viewport.width, height: viewport.height } });

    for (const path of PAGES) {
      test(`${path} has no horizontal overflow and usable controls`, async ({ page }) => {
        await page.goto(path);
        await expect(page.locator('h1')).toBeVisible({ timeout: 20_000 });

        const overflow = await page.evaluate(
          () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
        );
        // A few px of tolerance for scrollbar/box-model rounding, not a
        // real overflow defect.
        expect(overflow, `horizontal overflow of ${overflow}px at ${viewport.name} on ${path}`)
          .toBeLessThanOrEqual(5);

        // Sidebar nav must remain reachable (Streamlit auto-collapses the
        // sidebar below a width breakpoint - either the nav links or the
        // collapsed-sidebar toggle button must be present and clickable).
        const navVisible = await page.getByRole('link', { name: 'Pipeline' }).isVisible().catch(() => false);
        const toggleVisible = await page
          .locator('[data-testid="stSidebarCollapsedControl"]')
          .isVisible()
          .catch(() => false);
        expect(navVisible || toggleVisible, 'expected sidebar nav or its collapse toggle to be reachable')
          .toBeTruthy();
      });
    }
  });
}
