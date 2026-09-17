# InsightForge AI — UI/UX Redesign

## Scope decision

A prompt requesting a full Next.js + FastAPI rewrite ("DO NOT USE STREAMLIT") was resolved with the user directly: the actual app is Streamlit, fully working and fully tested (14 pages, real Postgres data, 41/41 Playwright E2E tests, a pytest `AppTest` suite). The user confirmed the real intent was a **visual redesign within Streamlit** — dark navy/electric-blue/AI-indigo theme, keeping every real backend connection and all existing functionality intact — not a framework migration.

Scope was further narrowed to **core theme + Overview hero** (user-confirmed): a consistent design system applied across all 14 pages, plus a real-data "Insight Pulse" hero section on Overview. Explicitly out of scope this pass, and why:
- **Business Health score** — no backend calculation exists for one; the source prompt's own rule says don't fabricate a score, so it's omitted rather than invented.
- **Custom RCA-drilldown / anomaly-timeline / product-matrix visualizations** — would need new chart work per page; a larger follow-up, not a styling pass.
- **Grouped sidebar sections** (CORE/INTELLIGENCE/BUSINESS/AI/OUTPUT/SYSTEM) — Streamlit's file-based multipage nav doesn't support this without migrating to `st.navigation()`, an architecture change that risks the 41 passing E2E tests and the pytest suite's page-discovery assumptions. Not attempted.

## Old UI problems

- Default Streamlit light theme: plain white background, generic look, no visual identity distinguishing an "AI decision-intelligence platform" from a boilerplate CRUD admin panel.
- `st.caption()`'s default gray-on-white (`#83858c` on white, 3.68:1 contrast) failed WCAG AA (documented in `docs/CHROME_E2E_AUDIT.md` §6, found during the earlier E2E pass).
- KPI tiles (`st.metric`) had no card treatment — floated on the plain background with no visual hierarchy.
- No distinct visual identity for AI-related pages (AI Analyst, Recommendations) versus plain data pages.
- Overview page had no synthesized "what changed and why" summary — a user had to read three separate sections (KPIs, significant moves, recommendations) to piece together the story themselves.

## New design direction

Dark navy control-center aesthetic: deep navy background with a subtle radial blue/violet glow, electric-blue primary actions, indigo/violet accents on AI-flavored surfaces, and the existing green/red delta convention for KPI trends kept as-is (`st.metric`'s built-in delta coloring already does this correctly).

## Color system

Defined once as CSS custom properties in `streamlit_app/common.py::inject_theme_css()`, plus the base palette in `.streamlit/config.toml`'s `[theme]` section (Streamlit 1.40.1's actually-supported keys — see note below):

| Token | Value | Use |
|---|---|---|
| `--background` | `#07111F` | Page background (also `config.toml backgroundColor`) |
| `--surface` | `#111E30` | Card/metric/expander background |
| `--surface-elevated` | `#142238` | Reserved for elevated/hover states |
| `--border` | `#1B2942` | Card/table borders |
| `--primary` | `#3B82F6` | Electric blue — primary actions, active nav |
| `--secondary` | `#22D3EE` | Cyan — reserved for secondary chart series |
| `--accent-ai` | `#8B5CF6` | Indigo/violet — AI Analyst & Recommendations pages |
| `--success` / `--warning` / `--danger` | `#22C55E` / `#F59E0B` / `#EF4444` | Status colors, paired with Streamlit's built-in icons (never color-only) |
| `--text-primary` / `--text-secondary` / `--muted` | `#E7ECF5` / `#A9B4C6` / `#6B7A93` | Text hierarchy |

**Real finding**: Streamlit 1.40.1 does not support `theme.baseRadius`, `theme.borderColor`, `theme.showWidgetBorder`, or `[theme.sidebar]` — confirmed via a real startup (Streamlit logs `"... is not a valid config option"` and ignores them). Those keys were added in a later Streamlit release than what this project pins. Card radius, borders, and sidebar surface color are handled via CSS instead (`inject_theme_css()`), not `config.toml`.

## Typography

Inter (Google Fonts, `@import`'d in the injected CSS — `config.toml`'s `font` key only accepts base keywords in 1.40.1, not arbitrary custom font names). Applied via `html, body, [class*="css"] { font-family: 'Inter', sans-serif; }`.

## Component system

All in `streamlit_app/common.py::inject_theme_css()`, one `<style>` block injected inside `require_database()` — called by every one of the 14 pages as their first real line, so **zero page files needed editing** to receive the base theme:

- `[data-testid="stMetric"]` → card (surface background, border, radius, padding); value font-size reduced and wrapping enabled to fix real long-currency-value truncation found during visual QA.
- `[data-testid="stExpander"]`, `[data-testid="stDataFrame"]` → consistent card/border treatment.
- `[data-testid="stButton"] button` → radius + border; `kind="primary"` buttons get the electric-blue fill.
- `[data-testid="stAlert"]` → consistent left-border treatment; Streamlit's own icon+color pairing per alert type is preserved (never color-only).
- `[data-testid="stCaptionContainer"]` → `--text-secondary` on the dark background, fixing the previously-documented contrast failure for real (re-verified via axe in this pass — zero `color-contrast` findings now, see `docs/CHROME_UI_VALIDATION.md`).
- `[data-testid="stSidebar"]` → dark navy surface, active-page highlight refinement.
- `[data-testid="stAppViewContainer"]` → subtle layered radial-gradient background (no animation).
- `.st-key-insightforge-hero` → Overview's hero card, targeting `st.container(key="insightforge-hero", border=True)`'s stable generated class (the correct way to style a Streamlit container's contents as a unit — two separate `st.markdown("<div>")`/`</div>` calls do **not** nest subsequent widgets inside them, since each `st.*` call is an independent sibling block in Streamlit's render tree, not literal HTML the browser parses as nested; this was corrected during implementation after the first attempt rendered incorrectly).
- `.st-key-ai-page-accent` → the same `key`-based pattern, giving AI Analyst and Recommendations pages a violet left-border accent via `st.container(key="ai-page-accent")` wrapping their intro caption.

## Overview's "Insight Pulse" hero

Real data only, reusing what Overview.py already fetches — no new backend modules wired in:
- One sentence built from the most-significant `ChangeRecord` from `compare_period(db, "day")` (already computed for the "Significant day-over-day moves" table below it).
- Primary driver / business impact / confidence sourced from the top-priority `Recommendation` from `generate_recommendations(db)[:5]` (already computed for "Top open recommendations").
- A real `st.page_link` to the Root Cause page (native Streamlit cross-page navigation, not a decorative button).
- Honest empty state ("nothing significant to report") when there's no significant move and no recommendation — never a fabricated pulse.

## Real bugs found and fixed during this pass

- **`streamlit_app/pages/5_Business_Impact.py`'s date-range filter** (added in an earlier session) passed `BusinessImpactResult.date` — a `str` field, confirmed via the dataclass definition and a live query — directly into `st.date_input`'s `value=` tuple, which requires real `date` objects. This threw `AttributeError: 'str' object has no attribute 'year'` inside Streamlit's own date-input internals on every real page load. Fixed by parsing with `date.fromisoformat()` for both the widget's value and the filter comparison. Found via the pytest `AppTest` regression pass for this redesign, not previously caught because the existing E2E suite's `full-pipeline.spec.ts` doesn't visit this page during a fresh-data run scenario that exercises the filter with real multi-row data the same way pytest's live-DB tier does.

## Responsive behavior

Unchanged from the existing `responsive.spec.ts` coverage (1366×768, 1536×864, 1280×720 on Overview/Pipeline/Anomalies) — the redesign is CSS-only on top of the same DOM structure, re-verified green in this pass (see `docs/CHROME_UI_VALIDATION.md`).
