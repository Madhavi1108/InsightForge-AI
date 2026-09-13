# InsightForge AI - AI Analyst & Explanation Engine

> Phase 28 deliverable (spec Phases 54-55, FR-20). `src/ai_analyst.py`
> answers six named business questions with a structured Summary /
> Evidence / Root Cause / Impact / Recommendation / Confidence answer,
> using Gemini when configured and a deterministic template otherwise.

## 1. What the spec says

`docs/system-components.md` (verbatim): answer "Why did revenue
decrease?", "Which region performed worst?", "Which products need
attention?", "What caused profit to decline?", "What are the biggest
risks?", "Summarize this month." Output structured: Summary / Evidence /
Root Cause / Impact / Recommendation / Confidence. "Failure behaviour: no
API key or LLM error -> deterministic template explanation from the
evidence package. Never invents numbers."

## 2. The 6 supported questions and routing

`classify_question(question)` is deterministic keyword matching - never an
LLM call, so routing stays side-effect-free and testable:

| Question (verbatim) | Matched keyword | Intent |
|---|---|---|
| Why did revenue decrease? | `revenue` | `metric_change` (`metric="revenue"`) |
| Which region performed worst? | `region` | `worst_region` |
| Which products need attention? | `product` | `products_attention` |
| What caused profit to decline? | `profit` | `metric_change` (`metric="profit"`) |
| What are the biggest risks? | `risk` | `biggest_risks` |
| Summarize this month. | `summar` | `summarize_period` |

Checked in that order (region/product/risk/summar before any metric
keyword) so they never collide - none of `METRIC_KEYWORDS`' 10 values
overlap those words. Any other question -> `"unsupported"`, answered with
a fixed, honest scope statement rather than a guess.

## 3. Design decision - the LLM only writes the Summary

Root Cause / Impact / Recommendation are always rendered directly from
typed, verified evidence (Phase 27's `EvidencePackage`, the
`regional_performance` view, Phase 24's `ProductIntelligence`, Phase 26's
`Recommendation`) into plain sentences - **never** passed through the LLM.
Only `summary` may be LLM-authored. This shrinks the surface a
hallucination could corrupt to one field, and keeps the grounding check
(section 4) simple - only the summary's numbers need verifying.

## 4. The grounding check - a real enforcement, not just a prompt instruction

```
allowed_numbers = numbers already present in the evidence bullets
candidate_numbers = numbers present in Gemini's summary text
grounded = every candidate number matches an allowed number (abs diff < 1e-6)
```

If the LLM's summary introduces any number not already asserted by a
verified module, it is rejected and the template path is used instead -
the same fallback as "no API key" or "LLM error". Worked example: evidence
`"revenue moved down 12.0% (2026-09-07 -> 2026-09-08)."` allows `{12.0,
2026, 9, 7, 8}`; a summary saying "revenue fell 12%" is grounded; a
summary saying "revenue fell 15%" is not, and falls back to the template.

## 5. Per-intent data sources

| Intent | Source | Notes |
|---|---|---|
| `metric_change` | `src.ai_evidence.assemble_evidence` (Phase 27) | evidence/root_cause/impact/recommendation all read straight off the `EvidencePackage` |
| `worst_region` | `regional_performance` view | "worst" = lowest revenue (this project's own operational definition); confidence = normalised gap to the next-worst region |
| `products_attention` | `src.product_intelligence.analyze_product_intelligence` (Phase 24) | flagged = declining / high-return / low-margin / slow-moving, ranked by ascending health score |
| `biggest_risks` | `src.recommendations.generate_recommendations` (Phase 26) | already ranked by `priority_score` descending; top 5 |
| `summarize_period` | `src.ai_evidence.assemble_all_evidence` (Phase 27), `grain="month"` | one package per KPI; only significant moves surfaced |

## 6. Confidence bands

```
HIGH   >= 0.75
MEDIUM >= 0.50
LOW    otherwise
```

A 3-band scale, distinct from the 4-band LOW/MEDIUM/HIGH/CRITICAL severity
scale reused elsewhere in this codebase (`src.anomaly_fusion`,
`src.recommendations`) - confidence isn't a severity; a 3-band scale is
the natural reading of "how sure are we", matching FR-20's own field name.

## 7. Failure behaviour

Every one of these degrades to the deterministic template, never raises,
never fails the caller: no `GEMINI_API_KEY` set; `LLM_PROVIDER` not
`gemini`; `google-generativeai` not importable; any Gemini API error or
timeout; an ungrounded response (section 4); no evidence to summarize. The
template itself is simply the evidence bullets joined into one string -
always available, since it is built from the same verified module output
the LLM path would have used.

## 8. Wiring

Not wired into `src/orchestrator.py` - same reasoning as Phases 22-27:
this needs cross-module, on-demand analysis (and, here, an optional
external API call), not a per-file pipeline step. No table stores an
`AnalystAnswer` - the caller (a Streamlit AI Analyst page, Phase 30)
consumes it directly, same no-persistence precedent as Phase 27.

## 9. API

| Symbol | Purpose |
|---|---|
| `AnalystAnswer` | the FR-20 structured answer |
| `classify_question(question)` | pure - section 2 |
| `confidence_band(confidence)` | pure - section 6 |
| `explain_metric_change(db, metric, grain="day", settings=None)` | "Why did X change?" |
| `worst_performing_region(db, settings=None)` | "Which region performed worst?" |
| `products_needing_attention(db, settings=None)` | "Which products need attention?" |
| `biggest_risks(db, settings=None)` | "What are the biggest risks?" |
| `summarize_period(db, grain="month", settings=None)` | "Summarize this month." |
| `answer_question(db, question, grain="day", settings=None)` | the single router entry point |

## 10. Configuration

No new environment variables - `LLM_PROVIDER`/`GEMINI_API_KEY`/
`GEMINI_MODEL`/`LLM_TIMEOUT_SECONDS` were already provisioned in
`.env.example` ahead of this phase (Phase 27's own note that they'd stay
unused until now). `src.config.LlmSettings`/`get_llm_settings()` (mirrors
`AlteryxSettings`, Phase 12) is the first module to actually read them.

## 11. Verify

```powershell
pytest -q tests/test_phase28_ai_analyst.py
$env:INSIGHTFORGE_PG_INTEGRATION = "1"; pytest -q tests/test_phase28_ai_analyst.py
```

## 12. Related documents

- [`ai-evidence.md`](ai-evidence.md) - the `EvidencePackage` `metric_change`/`summarize_period` reuse
- [`recommendations.md`](recommendations.md) - the `biggest_risks` input
- [`product-intelligence.md`](product-intelligence.md) - the `products_attention` input
- [`root-cause-analysis.md`](root-cause-analysis.md) - upstream of the RCA text embedded in `metric_change`
- [`security.md`](security.md) - section 3, AI/LLM safety
