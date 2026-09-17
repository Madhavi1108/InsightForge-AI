import path from 'path';
import dotenv from 'dotenv';

export const PROJECT_ROOT = path.resolve(__dirname, '..', '..');

dotenv.config({ path: path.join(PROJECT_ROOT, '.env') });

/**
 * Mirrors src/config.py's PostgresSettings.url precedence: an explicit
 * DATABASE_URL wins over the discrete POSTGRES_* parts. This project's own
 * .env leaves DATABASE_URL commented out, so both languages read the same
 * discrete credentials by default.
 */
export function getPgConfig() {
  const databaseUrl = (process.env.DATABASE_URL ?? '').trim();
  if (databaseUrl) {
    // src/config.py stores the SQLAlchemy dialect form
    // (postgresql+psycopg2://...) - strip the "+psycopg2" driver suffix so
    // node-postgres accepts the same URL.
    return { connectionString: databaseUrl.replace('+psycopg2', '') };
  }
  return {
    host: process.env.POSTGRES_HOST ?? 'localhost',
    port: Number(process.env.POSTGRES_PORT ?? 5432),
    database: process.env.POSTGRES_DB ?? 'insightforge',
    user: process.env.POSTGRES_USER ?? 'insightforge',
    password: process.env.POSTGRES_PASSWORD ?? '',
  };
}

export function getLlmMode(): { provider: string; configured: boolean } {
  const provider = process.env.LLM_PROVIDER ?? 'gemini';
  const apiKey = (process.env.GEMINI_API_KEY ?? '').trim();
  return { provider, configured: provider === 'gemini' && apiKey.length > 0 };
}

export const STREAMLIT_PORT = 8501;
export const STREAMLIT_BASE_URL = `http://localhost:${STREAMLIT_PORT}`;
export const KILLER_FILE_NAME = 'sales_2026_09_09.csv';
