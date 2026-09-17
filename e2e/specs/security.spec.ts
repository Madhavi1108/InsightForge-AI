import { test, expect } from '../fixtures/test-fixtures';
import { getLlmMode } from '../helpers/env';

/**
 * Real behavior check against src/nl_to_sql.py's actual `ask()` pipeline.
 *
 * Honest limitation: `generate_sql()` has "no template fallback" (its own
 * module docstring) - without a configured LLM, `ask()` short-circuits at
 * the very first stage (Question -> Intent -> SQL) and returns
 * `engine="unavailable"` for EVERY question, malicious or benign, before
 * `validate_sql()` (the actual SQL-injection defense) ever runs. This
 * environment intentionally runs with no GEMINI_API_KEY (see
 * global-setup.ts, for deterministic AI Analyst answers), so this spec
 * cannot exercise `validate_sql()`'s blocking behavior through the
 * browser - that is unit-tested directly in
 * tests/test_phase29_nl_to_sql.py, which is the authoritative coverage
 * for BANNED_KEYWORDS/TABLE_WHITELIST/single-statement enforcement. See
 * docs/CHROME_E2E_AUDIT.md for the full account of this gap.
 *
 * What this spec DOES verify for real: the app never crashes, never
 * fabricates a result, and always shows an honest "unavailable" message
 * rather than silently executing anything - across both a malicious and a
 * benign-looking question.
 */
const QUESTIONS = [
  { label: 'a DROP TABLE attempt', question: 'DROP TABLE fact_sales; SELECT 1' },
  { label: 'a benign whitelisted question', question: 'What was total revenue last month?' },
];

test.describe('NL-to-SQL - real behavior without a configured LLM', () => {
  test.beforeAll(() => {
    const llm = getLlmMode();
    test.skip(
      llm.configured,
      'LLM is configured in this run - see security-llm-configured.spec.ts intent; '
        + 'this spec specifically documents the no-LLM path.',
    );
  });

  for (const { label, question } of QUESTIONS) {
    test(`never fabricates a result for ${label}`, async ({ page }) => {
      await page.goto('/AI_Analyst');
      await page.getByRole('tab', { name: /NL-to-SQL/i }).click();
      const sqlBox = page.getByLabel('What do you want to know?');
      await sqlBox.fill(question);
      await sqlBox.press('Tab'); // commits the value to Streamlit's session state before the click
      await page.getByRole('button', { name: 'Run query', exact: true }).click();

      await expect(page.getByText('AI query generation is unavailable')).toBeVisible({
        timeout: 30_000,
      });
      // No fabricated SQL or result rows should ever appear alongside
      // the honest "unavailable" message.
      await expect(page.locator('pre, code')).toHaveCount(0);
    });
  }
});
