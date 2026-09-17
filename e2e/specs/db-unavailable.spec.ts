import { execSync } from 'child_process';
import { test, expect } from '../fixtures/test-fixtures';

/**
 * Browser-level equivalent of the existing Python AppTest DB-guard check
 * (tests/test_phase30_streamlit_app.py) - stops the real Postgres
 * container so Database.ping() genuinely returns False the way it would
 * in production, rather than faking a bad port, then confirms the app
 * shows require_database()'s exact friendly error instead of crashing or
 * blank-paging.
 */
test.describe.serial('database unavailable - graceful failure', () => {
  test('stopping Postgres shows the friendly error, not a crash', async ({ page }) => {
    execSync('docker stop insightforge-postgres', { stdio: 'ignore' });
    try {
      await page.goto('/');
      // src/database.py's ping() -> scalar() retries transient connection
      // failures up to DB_MAX_RETRIES=3 times with exponential backoff,
      // each attempt capable of taking up to DB_CONNECT_TIMEOUT=10s before
      // giving up - real, deliberate resilience behavior, not a bug. This
      // timeout must comfortably exceed that worst case (confirmed via a
      // real flaky run that a 30s timeout was too tight).
      await expect(page.getByText('The database is unavailable right now')).toBeVisible({
        timeout: 60_000,
      });
      // App shell should still render (no blank page, no raw traceback).
      await expect(page.locator('body')).not.toContainText('Traceback');
    } finally {
      execSync('docker start insightforge-postgres', { stdio: 'ignore' });
      execSync(
        'docker exec insightforge-postgres sh -c ' +
          '"until pg_isready -U insightforge -d insightforge; do sleep 1; done"',
        { stdio: 'ignore', timeout: 60_000 },
      );
    }
  });

  test('the app recovers once Postgres is back', async ({ page }) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Overview', exact: true })).toBeVisible({
      timeout: 30_000,
    });
    await expect(page.getByText('The database is unavailable right now')).not.toBeVisible();
  });
});
