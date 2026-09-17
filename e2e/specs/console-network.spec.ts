import { test, expect } from '../fixtures/test-fixtures';

/**
 * Cross-cutting console/network-error tracking across a scripted tour,
 * including the AI Analyst interactive flow (pages.spec.ts only covers a
 * static page load for each page).
 */
test('no console errors or 5xx responses across a full page tour', async ({ page, tracked }) => {
  const tour = [
    '/', '/Pipeline', '/Data_Quality', '/Anomalies', '/Root_Cause', '/Business_Impact',
    '/Forecast', '/Customers', '/Products', '/AI_Analyst', '/Logs', '/Recommendations',
    '/Reports', '/Alerts',
  ];
  for (const path of tour) {
    await page.goto(path);
    await page.waitForLoadState('domcontentloaded');
  }

  expect(tracked.consoleErrors, tracked.consoleErrors.join('\n')).toEqual([]);
  expect(tracked.failedRequests, tracked.failedRequests.join('\n')).toEqual([]);
});
