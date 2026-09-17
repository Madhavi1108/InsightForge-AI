import { spawn, execSync } from 'child_process';
import fs from 'fs';
import os from 'os';
import path from 'path';
import { PROJECT_ROOT, STREAMLIT_BASE_URL, STREAMLIT_PORT } from './env';

const PID_FILE = path.join(os.tmpdir(), '.insightforge-e2e-streamlit-pid');
const LOG_FILE = path.join(PROJECT_ROOT, 'e2e-report', 'streamlit.log');

function venvPython(): string {
  const winPy = path.join(PROJECT_ROOT, '.venv', 'Scripts', 'python.exe');
  if (fs.existsSync(winPy)) return winPy;
  const posixPy = path.join(PROJECT_ROOT, '.venv', 'bin', 'python');
  if (fs.existsSync(posixPy)) return posixPy;
  return 'python';
}

async function waitForHealth(timeoutMs: number): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  let lastErr: unknown;
  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${STREAMLIT_BASE_URL}/_stcore/health`);
      if (res.ok) return;
    } catch (err) {
      lastErr = err;
    }
    await new Promise((r) => setTimeout(r, 1000));
  }
  throw new Error(`Streamlit did not become healthy within ${timeoutMs}ms: ${lastErr}`);
}

export async function startStreamlit(): Promise<void> {
  fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });

  // Reuse an already-running instance (e.g. left over from a prior run,
  // or started manually by a developer) rather than double-launching.
  try {
    const res = await fetch(`${STREAMLIT_BASE_URL}/_stcore/health`);
    if (res.ok) return;
  } catch {
    // not running yet - fall through to spawn
  }

  const logStream = fs.createWriteStream(LOG_FILE, { flags: 'a' });
  const child = spawn(
    venvPython(),
    ['-m', 'streamlit', 'run', 'streamlit_app/Overview.py',
      `--server.port=${STREAMLIT_PORT}`, '--server.headless=true'],
    { cwd: PROJECT_ROOT, stdio: ['ignore', 'pipe', 'pipe'], detached: false },
  );
  child.stdout.pipe(logStream);
  child.stderr.pipe(logStream);
  fs.writeFileSync(PID_FILE, String(child.pid));
  child.unref();

  await waitForHealth(60_000);
}

export function stopStreamlit(): void {
  if (!fs.existsSync(PID_FILE)) return;
  const pid = fs.readFileSync(PID_FILE, 'utf-8').trim();
  fs.rmSync(PID_FILE, { force: true });
  if (!pid) return;
  try {
    if (process.platform === 'win32') {
      // Plain process.kill() doesn't reliably tear down Streamlit's
      // child Python process tree on Windows - taskkill /t kills it.
      execSync(`taskkill /pid ${pid} /t /f`, { stdio: 'ignore' });
    } else {
      process.kill(-Number(pid));
    }
  } catch {
    // already gone - fine
  }
}
