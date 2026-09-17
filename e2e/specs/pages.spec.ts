import { test, expect } from '../fixtures/test-fixtures';
import { queryTableRowCount } from '../helpers/db';

/**
 * All 14 Streamlit pages: loads without an unexpected console error, and
 * (where a page reads a single obviously-checkable table 1:1) a
 * real-vs-empty-state check driven by querying the DB first, so the
 * assertion isn't a guess about what state the app happens to be in.
 */
const PAGES: { path: string; title: string; table?: string; emptyText?: string }[] = [
  { path: '/', title: 'Overview' },
  { path: '/Pipeline', title: 'Pipeline', table: 'pipeline_runs', emptyText: 'No pipeline runs yet.' },
  { path: '/Data_Quality', title: 'Data Quality' },
  { path: '/Anomalies', title: 'Anomalies', table: 'anomalies', emptyText: 'No anomalies recorded yet.' },
  { path: '/Root_Cause', title: 'Root Cause' },
  { path: '/Business_Impact', title: 'Business Impact' },
  { path: '/Forecast', title: 'Forecast' },
  { path: '/Customers', title: 'Customers' },
  { path: '/Products', title: 'Products' },
  { path: '/AI_Analyst', title: 'AI Analyst' },
  { path: '/Logs', title: 'Logs' },
  { path: '/Recommendations', title: 'Recommendations' },
  { path: '/Reports', title: 'Reports', table: 'pipeline_runs', emptyText: 'No pipeline runs yet.' },
  { path: '/Alerts', title: 'Alerts' },
];

for (const p of PAGES) {
  test(`${p.title} page loads with real backend data or an honest empty state`, async ({
    page, tracked,
  }) => {
    await page.goto(p.path);
    // level: 1 - some pages (e.g. Customers) also have an h3 subheader
    // with the same text as the page's own h1 title.
    await expect(
      page.getByRole('heading', { name: p.title, exact: true, level: 1 }),
    ).toBeVisible({ timeout: 20_000 });

    if (p.table && p.emptyText) {
      const rowCount = await queryTableRowCount(p.table);
      if (rowCount === 0) {
        await expect(page.getByText(p.emptyText)).toBeVisible();
      } else {
        await expect(page.getByText(p.emptyText)).not.toBeVisible();
      }
    }

    expect(tracked.consoleErrors, tracked.consoleErrors.join('\n')).toEqual([]);
    expect(tracked.failedRequests, tracked.failedRequests.join('\n')).toEqual([]);
  });
}
