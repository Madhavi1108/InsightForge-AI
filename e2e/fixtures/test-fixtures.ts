import { test as base } from '@playwright/test';
import { Pool } from 'pg';
import { getPool } from '../helpers/db';
import { attachConsoleAndNetworkTracking, TrackedIssues } from '../helpers/consoleTracker';

export const test = base.extend<{ db: Pool; tracked: TrackedIssues }>({
  db: async ({}, use) => {
    await use(getPool());
  },
  tracked: async ({ page }, use) => {
    const issues = attachConsoleAndNetworkTracking(page);
    await use(issues);
  },
});

export { expect } from '@playwright/test';
