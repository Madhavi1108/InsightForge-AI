import { execSync } from 'child_process';
import { getLlmMode, PROJECT_ROOT } from './helpers/env';
import { getPool } from './helpers/db';
import { startStreamlit } from './helpers/streamlitProcess';

async function waitForContainerHealthy(name: string, timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const status = execSync(
        `docker inspect --format="{{.State.Health.Status}}" ${name}`,
      ).toString().trim();
      if (status === 'healthy') return;
    } catch {
      // container may not exist yet
    }
    await new Promise((r) => setTimeout(r, 2000));
  }
  throw new Error(`Container ${name} did not become healthy within ${timeoutMs}ms`);
}

export default async function globalSetup(): Promise<void> {
  // eslint-disable-next-line no-console
  console.log('[global-setup] checking Docker daemon...');
  execSync('docker info', { stdio: 'ignore' });

  const llm = getLlmMode();
  // eslint-disable-next-line no-console
  console.log(
    `[global-setup] LLM mode: provider=${llm.provider} configured=${llm.configured} ` +
      (llm.configured
        ? '(AI Analyst assertions will use groundedness checks only, not exact text)'
        : '(AI Analyst will use the deterministic template fallback - repeatable)'),
  );

  // eslint-disable-next-line no-console
  console.log('[global-setup] bringing up PostgreSQL...');
  execSync(`docker compose -f config/docker-compose.postgres.yml up -d`, {
    cwd: PROJECT_ROOT, stdio: 'inherit',
  });
  await waitForContainerHealthy('insightforge-postgres', 60_000);

  // eslint-disable-next-line no-console
  console.log('[global-setup] applying schema...');
  execSync(`"${PROJECT_ROOT}\\.venv\\Scripts\\python.exe" scripts/apply_schema.py`, {
    cwd: PROJECT_ROOT, stdio: 'inherit',
  });

  // eslint-disable-next-line no-console
  console.log('[global-setup] confirming Node -> Postgres connectivity...');
  await getPool().query('SELECT 1');

  // eslint-disable-next-line no-console
  console.log('[global-setup] starting Streamlit...');
  await startStreamlit();

  // eslint-disable-next-line no-console
  console.log('[global-setup] ready.');
}
