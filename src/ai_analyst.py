"""InsightForge AI - AI Analyst & explanation engine (Phase 28 / spec
Phases 54-55, FR-20).

The spec (verbatim, ``docs/system-components.md``): answer "Why did
revenue decrease?", "Which region performed worst?", "Which products need
attention?", "What caused profit to decline?", "What are the biggest
risks?", "Summarize this month." Output is structured: Summary / Evidence /
Root Cause / Impact / Recommendation / Confidence. "Failure behaviour: no
API key or LLM error -> deterministic template explanation from the
evidence package. Never invents numbers."

**Design decision - the LLM only writes the Summary.** Root Cause / Impact
/ Recommendation are always rendered directly from typed, verified
evidence (Phase 27's ``EvidencePackage``, the ``regional_performance``
view, Phase 24's ``ProductIntelligence``, Phase 26's ``Recommendation``)
into plain sentences - **never** passed through the LLM. Only ``summary``
may be LLM-authored. This shrinks the surface a hallucination could
corrupt to one field, and keeps the grounding check below simple - only
the summary's numbers need verifying, not four separate LLM outputs.

**The grounding check is a real, automated enforcement of "never invents
numbers"**, not just a prompt instruction (``docs/architecture.md`` dev
rule 3, ``docs/security.md`` section 3): every number the LLM's summary
contains must already appear in the verified evidence bullets passed into
the prompt. A summary that introduces any other number is rejected and the
deterministic template is used instead - the same behaviour as "no API key"
or "LLM error", just one more reason for the same fallback.

**Question routing is deterministic keyword matching, never an LLM call**
- routing must be side-effect-free and testable, and an LLM has no reason
to be in the business of deciding *which* verified module to query.

**On-demand, not persisted, not wired into ``src/orchestrator.py``** -
same reasoning as Phases 22-27: this needs cross-module, on-demand
analysis (and, here, an optional external API call), not a per-file
pipeline step. No table stores an :class:`AnalystAnswer`; the caller (a
Streamlit AI Analyst page, Phase 30) consumes it directly.

**Prompt-injection protection** (``docs/security.md`` section 3): the
system prompt (:data:`_PROMPT_TEMPLATE`) is fixed, not user-editable: the
verified evidence bullets are interpolated as *data* inside a fixed
instruction, never concatenated as instructions themselves.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field

from src.ai_evidence import assemble_all_evidence, assemble_evidence
from src.config import LlmSettings, get_llm_settings
from src.database import Database
from src.product_intelligence import analyze_product_intelligence
from src.recommendations import generate_recommendations

REGION_ATTENTION_LIMIT = 5
PRODUCT_ATTENTION_LIMIT = 5
RISK_LIMIT = 5

CONFIDENCE_HIGH = 0.75
CONFIDENCE_MEDIUM = 0.5

METRIC_KEYWORDS = {
    "revenue": "revenue", "sales": "revenue",
    "profit": "profit", "profitability": "profit",
    "margin": "margin_pct",
    "orders": "orders", "order": "orders",
    "customers": "customers", "customer": "customers",
    "units": "units", "quantity": "units",
    "aov": "aov", "average order value": "aov",
    "return": "return_rate_pct", "returns": "return_rate_pct",
    "discount": "avg_discount_pct",
    "shipping": "avg_shipping_days", "delivery": "avg_shipping_days",
}

_PROMPT_TEMPLATE = (
    "You are a retail analytics assistant. Using ONLY the verified facts "
    "listed below, write one concise paragraph (2-4 sentences) summarizing "
    "them for a business audience. Do not introduce any number, date, or "
    "name that is not already present in the facts. Do not speculate "
    "beyond what the facts state.\n\nFacts:\n{facts}\n"
)

_NUMBER_RE = re.compile(r"-?\d[\d,]*\.?\d*")


# --------------------------------------------------------------------------- #
# Dataclasses
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class AnalystAnswer:
    """One structured answer to a business question - the FR-20 Summary /
    Evidence / Root Cause / Impact / Recommendation / Confidence shape."""
    question: str
    intent: str
    summary: str
    evidence: list[str]
    root_cause: str | None
    impact: str | None
    recommendation: str | None
    confidence: float
    confidence_band: str  # "HIGH" | "MEDIUM" | "LOW"
    engine: str  # "gemini" | "template"
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _AnswerMaterial:
    """Intermediate, intent-specific ingredients - not exported. Every
    intent builder produces one; :func:`_finalize_answer` then decides the
    summary engine and assembles the :class:`AnalystAnswer`."""
    evidence: list[str]
    root_cause: str | None
    impact: str | None
    recommendation: str | None
    confidence: float
    detail: dict


_UNSUPPORTED_MATERIAL = _AnswerMaterial(
    evidence=["This question is outside the standard business questions this AI Analyst "
              "supports: why a metric changed, the worst-performing region, products "
              "needing attention, the biggest risks, or a period summary."],
    root_cause=None, impact=None, recommendation=None, confidence=0.0, detail={},
)


# --------------------------------------------------------------------------- #
# Pure core - confidence banding, grounding check, question classification
# --------------------------------------------------------------------------- #
def confidence_band(confidence: float) -> str:
    """3-band HIGH/MEDIUM/LOW scale (not the 4-band severity scale reused
    elsewhere) - confidence isn't a severity."""
    if confidence >= CONFIDENCE_HIGH:
        return "HIGH"
    if confidence >= CONFIDENCE_MEDIUM:
        return "MEDIUM"
    return "LOW"


def classify_question(question: str) -> tuple[str, dict]:
    """(intent, params) via deterministic keyword matching - checked in
    this order so "region"/"product"/"risk"/"summar" never collide with a
    metric keyword (none of the ``METRIC_KEYWORDS`` values overlap those
    words). Covers all 6 spec questions; anything else -> "unsupported"."""
    q = question.lower()
    if "region" in q:
        return "worst_region", {}
    if "product" in q:
        return "products_attention", {}
    if "risk" in q or "investigate" in q:
        return "biggest_risks", {}
    if "summar" in q:
        return "summarize_period", {}
    for keyword, metric in METRIC_KEYWORDS.items():
        if keyword in q:
            return "metric_change", {"metric": metric}
    return "unsupported", {}


def _extract_numbers(text: str) -> set[float]:
    """Every number literal in text, comma-stripped, as floats."""
    out: set[float] = set()
    for m in _NUMBER_RE.findall(text):
        try:
            out.add(float(m.replace(",", "")))
        except ValueError:
            continue
    return out


def _is_grounded(candidate: str, allowed_numbers: set[float]) -> bool:
    """True only if every number in ``candidate`` matches (abs diff <
    1e-6) a number already present in the verified evidence - the
    technical enforcement of "never invents numbers", not just a prompt
    instruction. Vacuously true for a candidate with no numbers."""
    return all(
        any(abs(c - a) < 1e-6 for a in allowed_numbers)
        for c in _extract_numbers(candidate)
    )


# --------------------------------------------------------------------------- #
# Gemini call - best-effort, mirrors src.alteryx's engine-vs-fallback shape
# --------------------------------------------------------------------------- #
def _call_gemini(prompt: str, settings: LlmSettings) -> str | None:
    """Raw Gemini text, or ``None`` on any failure (missing package, API
    error, timeout) - never raises, matching every other best-effort call
    in this codebase (``src.recommendations``/``src.ai_evidence``
    ``_lookup_*``)."""
    try:
        import google.generativeai as genai
    except ImportError:
        return None
    try:
        genai.configure(api_key=settings.api_key)
        model = genai.GenerativeModel(settings.model)
        response = model.generate_content(
            prompt, request_options={"timeout": settings.timeout_seconds},
        )
        text = (response.text or "").strip()
        return text or None
    except Exception:  # noqa: BLE001 - any LLM error degrades to template
        return None


def _template_summary(evidence: list[str]) -> str:
    return " ".join(evidence) if evidence else "No evidence is available to answer this question yet."


def _build_summary(evidence: list[str], settings: LlmSettings) -> tuple[str, str]:
    """``(summary_text, engine)``. Template unless Gemini is configured
    AND returns a grounded response - any failure at any step falls back
    to the template, never raises."""
    template_summary = _template_summary(evidence)
    if not settings.is_configured() or not evidence:
        return template_summary, "template"
    fact_blob = "\n".join(evidence)
    llm_text = _call_gemini(_PROMPT_TEMPLATE.format(facts=fact_blob), settings)
    if llm_text and _is_grounded(llm_text, _extract_numbers(fact_blob)):
        return llm_text, "gemini"
    return template_summary, "template"


# --------------------------------------------------------------------------- #
# Intent material builders - each reuses an existing analytics module
# --------------------------------------------------------------------------- #
def _metric_change_material(db: Database, metric: str, grain: str = "day") -> _AnswerMaterial | None:
    """Reuses Phase 27's ``assemble_evidence`` wholesale - this is the
    intent Phase 27 was built for."""
    pkg = assemble_evidence(db, metric, grain)
    if pkg is None:
        return None
    root_cause = None
    if pkg.root_cause:
        root_cause = (f"Primary driver: {pkg.root_cause.primary_driver} "
                      f"(contribution {pkg.root_cause.contribution}, "
                      f"confidence {pkg.root_cause.confidence}).")
    impact = None
    if pkg.impact:
        impact = (f"Revenue at risk: {pkg.impact.revenue_at_risk}; "
                  f"profit at risk: {pkg.impact.profit_at_risk}; "
                  f"{pkg.impact.customers_affected} customers and "
                  f"{pkg.impact.orders_affected} orders affected on {pkg.impact.date}.")
    if pkg.recommendation:
        recommendation = f"{pkg.recommendation.title} (priority {pkg.recommendation.priority_band})."
    elif pkg.significant:
        recommendation = "No linked recommendation yet - monitor this metric closely."
    else:
        recommendation = None
    return _AnswerMaterial(
        evidence=list(pkg.notes), root_cause=root_cause, impact=impact,
        recommendation=recommendation, confidence=pkg.confidence, detail=pkg.to_dict(),
    )


def _fetch_regional_performance(db: Database) -> list[dict]:
    rows = db.fetch_all(
        "SELECT region, revenue, profit, margin_pct, return_rate_pct "
        "FROM regional_performance ORDER BY revenue ASC"
    )
    return [dict(r) for r in rows]


def _worst_region_material(db: Database) -> _AnswerMaterial | None:
    """"Worst" = lowest revenue among regions (this project's own
    documented operational reading, same situation as every prior
    spec-silent formula). Confidence = how much further behind the worst
    region is than the next-worst, normalised - a genuine gap is more
    confidently "worst" than a near-tie."""
    rows = _fetch_regional_performance(db)
    if not rows:
        return None
    worst = rows[0]
    evidence = [
        f"{r['region']}: revenue {r['revenue']}, margin {r['margin_pct']}%, "
        f"return rate {r['return_rate_pct']}%."
        for r in rows[:REGION_ATTENTION_LIMIT]
    ]
    root_cause = f"{worst['region']} has the lowest revenue ({worst['revenue']}) of all regions."
    recommendation = (f"Review {worst['region']}'s pricing, inventory, and fulfillment to close "
                      "the gap with higher-performing regions.")
    if len(rows) > 1 and rows[1]["revenue"]:
        gap = (rows[1]["revenue"] - worst["revenue"]) / rows[1]["revenue"]
        confidence = round(min(1.0, max(0.0, gap)), 4)
    else:
        confidence = 0.5
    return _AnswerMaterial(
        evidence=evidence, root_cause=root_cause, impact=None, recommendation=recommendation,
        confidence=confidence, detail={"regions": rows},
    )


def _products_attention_material(db: Database) -> _AnswerMaterial | None:
    """Reuses Phase 24's ``analyze_product_intelligence`` - "needs
    attention" = any of declining/high-return/low-margin/slow-moving,
    ranked by health score ascending (worst health first)."""
    products = analyze_product_intelligence(db)
    flagged = [
        p for p in products
        if p.is_declining or p.is_high_return or p.is_low_margin or p.is_slow_moving
    ]
    if not flagged:
        return _AnswerMaterial(
            evidence=["No products are currently flagged for attention."],
            root_cause=None, impact=None, recommendation=None, confidence=0.6, detail={},
        )
    flagged.sort(key=lambda p: p.health_score)
    top = flagged[:PRODUCT_ATTENTION_LIMIT]
    evidence = [
        f"{p.product_name} ({p.category}): health score {p.health_score}, "
        f"margin {p.margin_pct}%, return rate {p.return_rate_pct}%"
        + (f", revenue growth {p.revenue_growth_pct}%" if p.revenue_growth_pct is not None else "") + "."
        for p in top
    ]
    root_cause = f"{top[0].product_name} has the lowest health score ({top[0].health_score})."
    recommendation = f"Prioritize {top[0].product_name} for pricing, quality, or inventory review."
    confidence = round(min(1.0, len(flagged) / max(len(products), 1) + 0.3), 4)
    return _AnswerMaterial(
        evidence=evidence, root_cause=root_cause, impact=None, recommendation=recommendation,
        confidence=confidence,
        detail={"flagged_count": len(flagged), "products": [asdict(p) for p in top]},
    )


def _biggest_risks_material(db: Database) -> _AnswerMaterial | None:
    """Reuses Phase 26's ``generate_recommendations`` - already ranked by
    ``priority_score`` descending."""
    recs = generate_recommendations(db)
    if not recs:
        return _AnswerMaterial(
            evidence=["No significant risks are currently flagged."],
            root_cause=None, impact=None, recommendation=None, confidence=0.6, detail={},
        )
    top = recs[:RISK_LIMIT]
    evidence = [
        f"{r.title} (priority {r.priority_band}, score {r.priority_score}): {r.rationale}"
        for r in top
    ]
    impact_values = [r.impact_value for r in top if r.impact_value is not None]
    impact = (f"Combined at-risk value across top risks: {round(sum(impact_values), 2)}."
              if impact_values else None)
    recommendation = top[0].title
    confidence = round(sum(r.confidence for r in top) / len(top), 4)
    return _AnswerMaterial(
        evidence=evidence, root_cause=None, impact=impact, recommendation=recommendation,
        confidence=confidence, detail={"recommendations": [asdict(r) for r in top]},
    )


def _summarize_period_material(db: Database, grain: str = "month") -> _AnswerMaterial | None:
    """Reuses Phase 27's ``assemble_all_evidence`` - one ``EvidencePackage``
    per KPI at the requested grain; only significant moves are surfaced as
    bullets, so a quiet period reads as quiet."""
    packages = assemble_all_evidence(db, grain)
    if not packages:
        return None
    significant = [p for p in packages if p.significant] or packages
    evidence = [n for p in significant for n in p.notes[:1]]
    confidence = round(sum(p.confidence for p in significant) / len(significant), 4)
    recommendation = ("See biggest risks for prioritized actions."
                      if any(p.significant for p in packages) else None)
    return _AnswerMaterial(
        evidence=evidence, root_cause=None, impact=None, recommendation=recommendation,
        confidence=confidence, detail={"packages": [p.to_dict() for p in packages]},
    )


def _build_material(db: Database, intent: str, params: dict, grain: str) -> _AnswerMaterial | None:
    if intent == "metric_change":
        return _metric_change_material(db, params["metric"], grain)
    if intent == "worst_region":
        return _worst_region_material(db)
    if intent == "products_attention":
        return _products_attention_material(db)
    if intent == "biggest_risks":
        return _biggest_risks_material(db)
    if intent == "summarize_period":
        return _summarize_period_material(db, "month")
    return _UNSUPPORTED_MATERIAL


def _finalize_answer(
    question: str, intent: str, material: _AnswerMaterial | None, settings: LlmSettings,
) -> AnalystAnswer:
    if material is None:
        return AnalystAnswer(
            question=question, intent=intent,
            summary="Not enough history is available yet to answer this question.",
            evidence=[], root_cause=None, impact=None, recommendation=None,
            confidence=0.0, confidence_band="LOW", engine="template", detail={},
        )
    summary, engine = _build_summary(material.evidence, settings)
    return AnalystAnswer(
        question=question, intent=intent, summary=summary, evidence=material.evidence,
        root_cause=material.root_cause, impact=material.impact,
        recommendation=material.recommendation, confidence=material.confidence,
        confidence_band=confidence_band(material.confidence), engine=engine, detail=material.detail,
    )


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def explain_metric_change(
    db: Database, metric: str, grain: str = "day", settings: LlmSettings | None = None,
) -> AnalystAnswer:
    settings = settings or get_llm_settings()
    material = _metric_change_material(db, metric, grain)
    return _finalize_answer(f"Why did {metric} change?", "metric_change", material, settings)


def worst_performing_region(db: Database, settings: LlmSettings | None = None) -> AnalystAnswer:
    settings = settings or get_llm_settings()
    return _finalize_answer(
        "Which region performed worst?", "worst_region", _worst_region_material(db), settings,
    )


def products_needing_attention(db: Database, settings: LlmSettings | None = None) -> AnalystAnswer:
    settings = settings or get_llm_settings()
    return _finalize_answer(
        "Which products need attention?", "products_attention",
        _products_attention_material(db), settings,
    )


def biggest_risks(db: Database, settings: LlmSettings | None = None) -> AnalystAnswer:
    settings = settings or get_llm_settings()
    return _finalize_answer(
        "What are the biggest risks?", "biggest_risks", _biggest_risks_material(db), settings,
    )


def summarize_period(
    db: Database, grain: str = "month", settings: LlmSettings | None = None,
) -> AnalystAnswer:
    settings = settings or get_llm_settings()
    return _finalize_answer(
        "Summarize this period.", "summarize_period",
        _summarize_period_material(db, grain), settings,
    )


def answer_question(
    db: Database, question: str, grain: str = "day", settings: LlmSettings | None = None,
) -> AnalystAnswer:
    """The single entry point a caller (Streamlit's AI Analyst page, Phase
    30) uses: classify the question, assemble its material, decide the
    summary engine."""
    settings = settings or get_llm_settings()
    intent, params = classify_question(question)
    material = _build_material(db, intent, params, grain)
    return _finalize_answer(question, intent, material, settings)
