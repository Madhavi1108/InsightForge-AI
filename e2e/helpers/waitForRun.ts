import { expect } from '@playwright/test';
import { PipelineRun, queryLatestRunByFile } from './db';

/**
 * DB-backed ground truth for "has this file finished processing" -
 * independent of any UI state, since Streamlit reruns over a WebSocket
 * with no REST endpoint to waitForResponse() on, and a page reload during
 * a run loses whatever spinner/local state the browser had. Never
 * page.waitForTimeout(): this polls the same pipeline_runs row the
 * orchestrator itself writes.
 *
 * `sinceRunId`, when given, ignores any pre-existing row (e.g. from a
 * previous test run against the same file_name) and waits specifically
 * for a *new* row with a higher run_id to reach a terminal status.
 */
export async function waitForRunTerminal(
  fileName: string,
  opts: { timeoutMs?: number; sinceRunId?: number } = {},
): Promise<PipelineRun> {
  const timeoutMs = opts.timeoutMs ?? 180_000;

  await expect
    .poll(
      async () => {
        const run = await queryLatestRunByFile(fileName);
        if (!run) return false;
        if (opts.sinceRunId && run.run_id <= opts.sinceRunId) return false;
        return run.status !== 'RUNNING';
      },
      { timeout: timeoutMs, intervals: [1000] },
    )
    .toBe(true);

  const run = await queryLatestRunByFile(fileName);
  if (!run) throw new Error(`No pipeline_runs row found for ${fileName} after waiting`);
  return run;
}

/** The run_id of the current latest row for fileName, or 0 if none yet. */
export async function currentRunId(fileName: string): Promise<number> {
  const run = await queryLatestRunByFile(fileName);
  return run?.run_id ?? 0;
}
