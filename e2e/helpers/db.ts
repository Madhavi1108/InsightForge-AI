import { Pool, types } from 'pg';
import { getPgConfig } from './env';

// node-postgres returns BIGINT/BIGSERIAL (oid 20) as strings by default,
// to avoid silent precision loss above Number.MAX_SAFE_INTEGER. Every
// run_id/anomaly_id/etc. in this schema is a small sequential ID, so
// parsing them as numbers here is safe and avoids string/number
// comparison bugs (e.g. `"273" > 191` needing explicit coercion)
// throughout the helpers and specs below.
types.setTypeParser(20, (val: string) => Number(val));

let pool: Pool | null = null;

export function getPool(): Pool {
  if (!pool) {
    pool = new Pool(getPgConfig());
    // db-unavailable.spec.ts stops the real Postgres container, which
    // kills idle pooled connections out from under us - without this
    // handler, node-postgres's default behavior is to crash the whole
    // process on that "terminating connection due to administrator
    // command" error (an unhandled 'error' event on the Pool).
    pool.on('error', () => {
      // swallow - callers that were mid-query already get a rejected
      // promise; idle-connection resets are expected during that spec.
    });
  }
  return pool;
}

export async function closePool(): Promise<void> {
  if (pool) {
    await pool.end();
    pool = null;
  }
}

export interface PipelineRun {
  run_id: number;
  file_name: string;
  file_hash: string | null;
  status: string;
  started_at: string;
  finished_at: string | null;
  duration_s: string | null;
  rows_received: number | null;
  rows_valid: number | null;
  rows_rejected: number | null;
  dq_score: string | null;
  stage_metrics: Record<string, unknown> | null;
  error: string | null;
}

/** Most recent pipeline_runs row for a given file_name, or null if none yet. */
export async function queryLatestRunByFile(
  fileName: string,
): Promise<PipelineRun | null> {
  const { rows } = await getPool().query<PipelineRun>(
    `SELECT run_id, file_name, file_hash, status, started_at, finished_at,
            duration_s, rows_received, rows_valid, rows_rejected, dq_score,
            stage_metrics, error
     FROM pipeline_runs WHERE file_name = $1
     ORDER BY started_at DESC LIMIT 1`,
    [fileName],
  );
  return rows[0] ?? null;
}

export async function queryRunById(runId: number): Promise<PipelineRun | null> {
  const { rows } = await getPool().query<PipelineRun>(
    `SELECT run_id, file_name, file_hash, status, started_at, finished_at,
            duration_s, rows_received, rows_valid, rows_rejected, dq_score,
            stage_metrics, error
     FROM pipeline_runs WHERE run_id = $1`,
    [runId],
  );
  return rows[0] ?? null;
}

export async function queryRunsByFile(fileName: string): Promise<PipelineRun[]> {
  const { rows } = await getPool().query<PipelineRun>(
    `SELECT run_id, file_name, file_hash, status, started_at, finished_at,
            duration_s, rows_received, rows_valid, rows_rejected, dq_score,
            stage_metrics, error
     FROM pipeline_runs WHERE file_name = $1 ORDER BY started_at DESC`,
    [fileName],
  );
  return rows;
}

export interface DailyKpiRow {
  revenue: string;
  profit: string;
  orders: string;
  customers: string;
  aov: string;
  margin_pct: string;
  return_rate_pct: string;
}

/** Latest daily_kpis row - the same view Overview.py reads. */
export async function queryLatestKpis(): Promise<DailyKpiRow | null> {
  const { rows } = await getPool().query<DailyKpiRow>(
    `SELECT revenue, profit, orders, customers, aov, margin_pct, return_rate_pct
     FROM daily_kpis ORDER BY order_date DESC LIMIT 1`,
  );
  return rows[0] ?? null;
}

/**
 * Direct aggregate for a region/category/sub_category slice over a date
 * range - used to cross-check the RCA page's drill-down (the planted
 * West/Electronics/Laptop anomaly has no dedicated `anomalies` table row,
 * since anomaly_fusion only runs at whole-business grain and the effect is
 * largely masked there per docs/anomaly-ground-truth.md; the real signal is
 * this segment's own revenue drop, which RCA's drill-down surfaces).
 */
export async function querySegmentRevenue(
  region: string,
  category: string,
  subCategory: string | null,
  start: string,
  end: string,
): Promise<{ orders: number; revenue: number } | null> {
  const params: unknown[] = [region, category, start, end];
  let subClause = '';
  if (subCategory) {
    params.push(subCategory);
    subClause = `AND sub_category = $${params.length}`;
  }
  const { rows } = await getPool().query<{ orders: string; revenue: string }>(
    `SELECT COUNT(DISTINCT order_id) AS orders, COALESCE(SUM(revenue), 0) AS revenue
     FROM fact_sales WHERE region = $1 AND category = $2
       AND order_date BETWEEN $3 AND $4 ${subClause}`,
    params,
  );
  const row = rows[0];
  if (!row) return null;
  return { orders: Number(row.orders), revenue: Number(row.revenue) };
}

export interface FileRegistryRow {
  file_id: number;
  file_name: string;
  file_hash: string;
  first_seen_run_id: number | null;
}

export async function queryFileRegistryByHash(
  fileHash: string,
): Promise<FileRegistryRow[]> {
  const { rows } = await getPool().query<FileRegistryRow>(
    `SELECT file_id, file_name, file_hash, first_seen_run_id
     FROM file_registry WHERE file_hash = $1`,
    [fileHash],
  );
  return rows;
}

export async function queryDqDimensionCount(runId: number): Promise<number> {
  const { rows } = await getPool().query<{ n: string }>(
    `SELECT COUNT(*) AS n FROM data_quality_results WHERE run_id = $1`,
    [runId],
  );
  return Number(rows[0]?.n ?? 0);
}

export async function queryRejectedRecordCount(runId: number): Promise<number> {
  const { rows } = await getPool().query<{ n: string }>(
    `SELECT COUNT(*) AS n FROM rejected_records WHERE run_id = $1`,
    [runId],
  );
  return Number(rows[0]?.n ?? 0);
}

export interface DailyKpiFullRow {
  order_date: string;
  revenue: string;
  profit: string;
  orders: number;
  customers: number;
}

export async function queryDailyKpisForDate(date: string): Promise<DailyKpiFullRow | null> {
  const { rows } = await getPool().query<DailyKpiFullRow>(
    `SELECT order_date, revenue, profit, orders, customers FROM daily_kpis WHERE order_date = $1`,
    [date],
  );
  return rows[0] ?? null;
}

export async function queryTableRowCount(table: string): Promise<number> {
  // `table` is always one of a small fixed set of literals passed by test
  // code (never user input) - see callers in pages.spec.ts.
  const { rows } = await getPool().query<{ n: string }>(`SELECT COUNT(*) AS n FROM ${table}`);
  return Number(rows[0]?.n ?? 0);
}
