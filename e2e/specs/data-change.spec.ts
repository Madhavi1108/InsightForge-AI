import { test, expect } from '../fixtures/test-fixtures';
import { queryDailyKpisForDate } from '../helpers/db';

/**
 * Anti-hardcoding proof (spec section 44's intent): if the dashboard were
 * showing frozen/hardcoded KPI numbers, every date would report the same
 * values. This verifies real day-to-day variation across two distinct,
 * already-ingested real dates, then confirms the Overview page's
 * "latest daily KPIs" actually reflects whichever date's row is most
 * recent in the database - not a static snapshot.
 *
 * Real finding that changed this test's original approach: this project
 * pins a fixed "narrative present" - config/data_contract.yaml's
 * Order_Date rule rejects any date after 2026-09-09 as out_of_range
 * (confirmed via a real run: generating and submitting a genuinely new
 * 2026-09-10 day through the real Pipeline UI produced rows_valid=0,
 * every row rejected with "Order_Date=2026-09-10 is after 2026-09-09").
 * There is therefore no way to ingest a never-before-seen *future* day
 * without violating the platform's own, correct validation - every valid
 * date through 2026-09-09 is already loaded. The comparison approach
 * below is the honest substitute: it still proves the dashboard reflects
 * real, varying backend data rather than a constant.
 */
test('daily_kpis values genuinely vary across real dates (not a static/hardcoded value)', async ({
  page, db,
}) => {
  const { rows } = await db.query<{ order_date: string; revenue: string; orders: number }>(
    "SELECT order_date::text, revenue, orders FROM daily_kpis ORDER BY order_date DESC LIMIT 5",
  );
  test.skip(rows.length < 2, 'need at least 2 days of data to compare');

  const revenues = rows.map((r) => Number(r.revenue));
  const uniqueRevenues = new Set(revenues);
  expect(
    uniqueRevenues.size,
    `expected varying revenue across dates, got identical values: ${revenues.join(', ')}`,
  ).toBeGreaterThan(1);

  // Cross-check the Overview page actually shows the most recent date's
  // real revenue value, not a cached/different number.
  const latestDate = rows[0].order_date;
  const latest = await queryDailyKpisForDate(latestDate);
  expect(latest).not.toBeNull();

  await page.goto('/');
  const expectedRevenueText = `$${Number(latest!.revenue).toLocaleString('en-US', {
    minimumFractionDigits: 2, maximumFractionDigits: 2,
  })}`;
  await expect(page.getByText(expectedRevenueText, { exact: true }).first()).toBeVisible({
    timeout: 15_000,
  });
});
