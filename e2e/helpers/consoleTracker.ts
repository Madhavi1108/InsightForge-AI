import { Page } from '@playwright/test';

export interface TrackedIssues {
  consoleErrors: string[];
  failedRequests: string[];
}

/**
 * A small, curated allow-list of benign Streamlit-internal console
 * messages seen during development that are not application bugs - kept
 * short and specific rather than broad, so a real new error still fails
 * the suite.
 *
 * `_stcore/health` / `_stcore/host-config` 404s: confirmed via a manual
 * diagnostic (see docs/CHROME_E2E_AUDIT.md) that Streamlit's own frontend
 * issues these as *relative* fetches. Deep-linking straight to a subpage
 * (page.goto('/Data_Quality')) - rather than starting at '/' and clicking
 * the sidebar like a real user - resolves them against the current path
 * (".../Data_Quality/_stcore/health") instead of root, producing a 404
 * that is a browser-navigation artifact of this test harness, not an
 * InsightForge application defect.
 */
const ALLOWED_CONSOLE_PATTERNS = [
  /Websocket connection to .* failed: WebSocket is closed before the connection is established/i,
  /\[webpack-dev-server\]/i,
  /_stcore\/(health|host-config)/i,
  // Streamlit's own opt-out telemetry: it tries to fetch a remote metrics
  // config and degrades gracefully (logs a console error, disables
  // tracking) when that external request is slow/unreachable - a
  // Streamlit-internal network condition, not an InsightForge app defect.
  /Failed to fetch metrics config/i,
  /Undefined metrics config - deactivating metrics tracking/i,
];

export function attachConsoleAndNetworkTracking(page: Page): TrackedIssues {
  const issues: TrackedIssues = { consoleErrors: [], failedRequests: [] };

  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const text = msg.text();
    // Chrome's own "Failed to load resource: ..." console message never
    // includes the URL in msg.text() - it's only on msg.location().url.
    const url = msg.location()?.url ?? '';
    const combined = `${text} ${url}`;
    if (ALLOWED_CONSOLE_PATTERNS.some((re) => re.test(combined))) return;
    issues.consoleErrors.push(url ? `${text} (${url})` : text);
  });

  page.on('pageerror', (err) => {
    issues.consoleErrors.push(`uncaught exception: ${err.message}`);
  });

  page.on('response', (response) => {
    if (response.status() >= 500) {
      issues.failedRequests.push(`${response.status()} ${response.url()}`);
    }
  });

  page.on('requestfailed', (request) => {
    const failure = request.failure();
    // Streamlit's own WS upgrade occasionally shows as a "failed" request
    // during a normal reconnect cycle - only flag failures on ordinary
    // http(s) document/xhr/fetch requests.
    const type = request.resourceType();
    if (type === 'websocket') return;
    issues.failedRequests.push(`${type} ${request.url()} (${failure?.errorText ?? 'unknown'})`);
  });

  return issues;
}
