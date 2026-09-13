"""Phase 28 (spec Phases 54-55, FR-20) - AI Analyst & explanation engine.

Pure helpers (`classify_question`, `confidence_band`, `_extract_numbers`,
`_is_grounded`) get direct unit tests. `_build_summary`/`_call_gemini` are
tested with a monkeypatched `_call_gemini`, never a real network call. Each
intent's `_..._material` builder is tested against a stub `Database`/
monkeypatched upstream module. Public entry points are tested with the
material builders and `_build_summary` monkeypatched, asserting the
returned `AnalystAnswer` assembles correctly. The real
`fact_sales` -> `AnalystAnswer` round trip needs real data, so that's
`INSIGHTFORGE_PG_INTEGRATION=1`-gated (development rule 1: never fake
functionality). Like Phases 22-27, the AI Analyst is **not** wired into
`src/orchestrator.py` and persists nothing.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.ai_analyst import (
    AnalystAnswer,
    _AnswerMaterial,
    _build_summary,
    _extract_numbers,
    _is_grounded,
    answer_question,
    biggest_risks,
    classify_question,
    confidence_band,
    explain_metric_change,
    products_needing_attention,
    summarize_period,
    worst_performing_region,
)
from src.config import LlmSettings

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)

_UNCONFIGURED = LlmSettings(provider="none", api_key=None, model="x", timeout_seconds=30)
_CONFIGURED = LlmSettings(provider="gemini", api_key="fake-key", model="gemini-1.5-pro", timeout_seconds=30)


def test_phase28_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "ai_analyst.py",
        PROJECT_ROOT / "docs" / "ai-analyst.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    for module in ("src.ai_analyst", "src.config"):
        r = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
        )
        assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# LlmSettings
# --------------------------------------------------------------------------- #
def test_llm_settings_defaults(monkeypatch):
    from src.config import LlmSettings as LS
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    settings = LS.from_env(environ={})
    assert settings.provider == "gemini"
    assert settings.api_key is None
    assert settings.model == "gemini-1.5-pro"
    assert settings.timeout_seconds == 30
    assert settings.is_configured() is False


def test_llm_settings_configured_with_api_key():
    from src.config import LlmSettings as LS
    settings = LS.from_env(environ={"GEMINI_API_KEY": "abc123"})
    assert settings.is_configured() is True


def test_llm_settings_provider_none_never_configured():
    from src.config import LlmSettings as LS
    settings = LS.from_env(environ={"LLM_PROVIDER": "none", "GEMINI_API_KEY": "abc123"})
    assert settings.is_configured() is False


def test_llm_settings_invalid_timeout_falls_back():
    from src.config import LlmSettings as LS
    settings = LS.from_env(environ={"LLM_TIMEOUT_SECONDS": "nope"})
    assert settings.timeout_seconds == 30


# --------------------------------------------------------------------------- #
# classify_question - pure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("question, expected_intent, expected_params", [
    ("Why did revenue decrease?", "metric_change", {"metric": "revenue"}),
    ("Which region performed worst?", "worst_region", {}),
    ("Which products need attention?", "products_attention", {}),
    ("What caused profit to decline?", "metric_change", {"metric": "profit"}),
    ("What are the biggest risks?", "biggest_risks", {}),
    ("Summarize this month.", "summarize_period", {}),
    ("What is the weather today?", "unsupported", {}),
])
def test_classify_question(question, expected_intent, expected_params):
    intent, params = classify_question(question)
    assert intent == expected_intent
    assert params == expected_params


# --------------------------------------------------------------------------- #
# _extract_numbers / _is_grounded - pure
# --------------------------------------------------------------------------- #
def test_extract_numbers():
    # A trailing "-DD" in a date is captured with its literal hyphen as a
    # minus sign (e.g. "-09", "-08") - a documented quirk of the regex,
    # harmless here since the same extraction is applied symmetrically to
    # both the evidence and the candidate text in _is_grounded.
    assert _extract_numbers("Revenue fell 12,000.5 on 2026-09-08.") == {12000.5, 2026.0, -9.0, -8.0}


def test_is_grounded_subset_is_true():
    assert _is_grounded("Revenue fell 12%.", {12.0, 2026.0}) is True


def test_is_grounded_new_number_is_false():
    assert _is_grounded("Revenue fell 15%.", {12.0, 2026.0}) is False


def test_is_grounded_no_numbers_is_vacuously_true():
    assert _is_grounded("Revenue fell sharply.", set()) is True


# --------------------------------------------------------------------------- #
# confidence_band - pure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("confidence, expected", [
    (0.9, "HIGH"), (0.75, "HIGH"), (0.6, "MEDIUM"), (0.5, "MEDIUM"), (0.2, "LOW"), (0.0, "LOW"),
])
def test_confidence_band(confidence, expected):
    assert confidence_band(confidence) == expected


# --------------------------------------------------------------------------- #
# _build_summary - monkeypatched _call_gemini
# --------------------------------------------------------------------------- #
def test_build_summary_unconfigured_never_calls_gemini(monkeypatch):
    import src.ai_analyst as ai_analyst

    def _boom(*args, **kwargs):
        raise AssertionError("must not call Gemini when unconfigured")

    monkeypatch.setattr(ai_analyst, "_call_gemini", _boom)
    summary, engine = _build_summary(["revenue moved down 12.0%."], _UNCONFIGURED)
    assert engine == "template"
    assert summary == "revenue moved down 12.0%."


def test_build_summary_grounded_response_uses_gemini(monkeypatch):
    import src.ai_analyst as ai_analyst
    monkeypatch.setattr(ai_analyst, "_call_gemini", lambda prompt, settings: "Revenue fell 12.0%.")
    summary, engine = _build_summary(["revenue moved down 12.0%."], _CONFIGURED)
    assert engine == "gemini"
    assert summary == "Revenue fell 12.0%."


def test_build_summary_ungrounded_response_falls_back(monkeypatch):
    import src.ai_analyst as ai_analyst
    monkeypatch.setattr(ai_analyst, "_call_gemini", lambda prompt, settings: "Revenue fell 99.0%.")
    summary, engine = _build_summary(["revenue moved down 12.0%."], _CONFIGURED)
    assert engine == "template"
    assert summary == "revenue moved down 12.0%."


def test_build_summary_gemini_failure_falls_back(monkeypatch):
    import src.ai_analyst as ai_analyst
    monkeypatch.setattr(ai_analyst, "_call_gemini", lambda prompt, settings: None)
    summary, engine = _build_summary(["revenue moved down 12.0%."], _CONFIGURED)
    assert engine == "template"


def test_build_summary_no_evidence_is_template():
    summary, engine = _build_summary([], _CONFIGURED)
    assert engine == "template"
    assert "No evidence" in summary


# --------------------------------------------------------------------------- #
# _metric_change_material
# --------------------------------------------------------------------------- #
def test_metric_change_material_none_when_no_package(monkeypatch):
    import src.ai_analyst as ai_analyst
    monkeypatch.setattr(ai_analyst, "assemble_evidence", lambda db, metric, grain: None)
    from src.ai_analyst import _metric_change_material
    assert _metric_change_material(db=object(), metric="revenue") is None


def test_metric_change_material_happy_path(monkeypatch):
    import src.ai_analyst as ai_analyst
    from src.ai_evidence import AnomalyLink, EvidencePackage, RecommendationLink, RootCauseSummary
    from src.impact_analysis import BusinessImpactResult

    pkg = EvidencePackage(
        metric="revenue", grain="day", period_key="2026-09-08", previous_period_key="2026-09-07",
        current=100.0, previous=125.0, pct_change=-20.0, direction="down", magnitude=20.0,
        significant=True,
        root_cause=RootCauseSummary(
            primary_driver="West", primary_region="West", primary_category=None,
            primary_sub_category=None, primary_product=None, primary_customer_segment=None,
            contribution=-10.0, confidence=0.7,
        ),
        impact=BusinessImpactResult(
            date="2026-09-08", expected_revenue=120.0, actual_revenue=100.0, revenue_gap=-20.0,
            expected_profit=30.0, actual_profit=25.0, profit_gap=-5.0,
            revenue_at_risk=20.0, profit_at_risk=5.0, customers_affected=10, orders_affected=12,
        ),
        forecast=None, anomaly=AnomalyLink(anomaly_id=1, severity="HIGH", confidence=0.9),
        recommendation=RecommendationLink(1, "Investigate demand", "HIGH", 70.0),
        confidence=0.8, notes=["revenue moved down 20.0%."],
    )
    monkeypatch.setattr(ai_analyst, "assemble_evidence", lambda db, metric, grain: pkg)
    from src.ai_analyst import _metric_change_material
    material = _metric_change_material(db=object(), metric="revenue")
    assert isinstance(material, _AnswerMaterial)
    assert material.evidence == ["revenue moved down 20.0%."]
    assert "West" in material.root_cause
    assert "20.0" in material.impact
    assert "Investigate demand" in material.recommendation
    assert material.confidence == 0.8


# --------------------------------------------------------------------------- #
# _worst_region_material
# --------------------------------------------------------------------------- #
class _StubDB:
    def __init__(self, rows):
        self._rows = rows

    def fetch_all(self, sql, params=None):
        return self._rows


def test_worst_region_material_none_when_no_regions():
    from src.ai_analyst import _worst_region_material
    assert _worst_region_material(_StubDB([])) is None


def test_worst_region_material_happy_path():
    from src.ai_analyst import _worst_region_material
    rows = [
        {"region": "East", "revenue": 1000.0, "profit": 100.0, "margin_pct": 10.0, "return_rate_pct": 5.0},
        {"region": "West", "revenue": 2000.0, "profit": 300.0, "margin_pct": 15.0, "return_rate_pct": 3.0},
    ]
    material = _worst_region_material(_StubDB(rows))
    assert "East" in material.root_cause
    assert material.confidence == pytest.approx((2000.0 - 1000.0) / 2000.0)


# --------------------------------------------------------------------------- #
# _products_attention_material
# --------------------------------------------------------------------------- #
def test_products_attention_material_no_flagged_products(monkeypatch):
    import src.ai_analyst as ai_analyst
    monkeypatch.setattr(ai_analyst, "analyze_product_intelligence", lambda db: [])
    from src.ai_analyst import _products_attention_material
    material = _products_attention_material(db=object())
    assert "No products" in material.evidence[0]
    assert material.confidence == 0.6


def test_products_attention_material_flags_and_ranks(monkeypatch):
    import src.ai_analyst as ai_analyst
    from src.product_intelligence import ProductIntelligence

    healthy = ProductIntelligence(
        product_id="P1", product_name="Widget", category="Gadgets", revenue=100.0, profit=20.0,
        margin_pct=20.0, units=50, orders=10, return_rate_pct=2.0, revenue_growth_pct=10.0,
        is_star=True, is_high_profit=True, is_high_revenue=True, is_fast_growing=True,
        is_declining=False, is_high_return=False, is_low_margin=False, is_slow_moving=False,
        health_score=90.0,
    )
    flagged = ProductIntelligence(
        product_id="P2", product_name="Gizmo", category="Gadgets", revenue=50.0, profit=1.0,
        margin_pct=2.0, units=5, orders=2, return_rate_pct=20.0, revenue_growth_pct=-30.0,
        is_star=False, is_high_profit=False, is_high_revenue=False, is_fast_growing=False,
        is_declining=True, is_high_return=True, is_low_margin=True, is_slow_moving=True,
        health_score=5.0,
    )
    monkeypatch.setattr(ai_analyst, "analyze_product_intelligence", lambda db: [healthy, flagged])
    from src.ai_analyst import _products_attention_material
    material = _products_attention_material(db=object())
    assert "Gizmo" in material.root_cause
    assert material.detail["flagged_count"] == 1


# --------------------------------------------------------------------------- #
# _biggest_risks_material
# --------------------------------------------------------------------------- #
def test_biggest_risks_material_none_flagged(monkeypatch):
    import src.ai_analyst as ai_analyst
    monkeypatch.setattr(ai_analyst, "generate_recommendations", lambda db: [])
    from src.ai_analyst import _biggest_risks_material
    material = _biggest_risks_material(db=object())
    assert "No significant risks" in material.evidence[0]


def test_biggest_risks_material_happy_path(monkeypatch):
    import src.ai_analyst as ai_analyst
    from src.recommendations import Recommendation

    rec = Recommendation(
        rule_id="demand_decline", title="Investigate declining demand", rationale="Revenue down.",
        metric="revenue", period_key="2026-09-08", direction="down", severity="HIGH", confidence=0.8,
        impact_value=15000.0, priority_score=60.0, priority_band="HIGH", linked_anomaly_id=1, detail={},
    )
    monkeypatch.setattr(ai_analyst, "generate_recommendations", lambda db: [rec])
    from src.ai_analyst import _biggest_risks_material
    material = _biggest_risks_material(db=object())
    assert material.recommendation == "Investigate declining demand"
    assert "15000.0" in material.impact
    assert material.confidence == 0.8


# --------------------------------------------------------------------------- #
# _summarize_period_material
# --------------------------------------------------------------------------- #
def test_summarize_period_material_none_when_no_packages(monkeypatch):
    import src.ai_analyst as ai_analyst
    monkeypatch.setattr(ai_analyst, "assemble_all_evidence", lambda db, grain: [])
    from src.ai_analyst import _summarize_period_material
    assert _summarize_period_material(db=object()) is None


def test_summarize_period_material_uses_significant_packages(monkeypatch):
    import src.ai_analyst as ai_analyst
    from src.ai_evidence import EvidencePackage

    sig = EvidencePackage(
        metric="revenue", grain="month", period_key="2026-09", previous_period_key="2026-08",
        current=1000.0, previous=1200.0, pct_change=-16.7, direction="down", magnitude=16.7,
        significant=True, root_cause=None, impact=None, forecast=None, anomaly=None,
        recommendation=None, confidence=0.5, notes=["revenue moved down 16.7%."],
    )
    quiet = EvidencePackage(
        metric="orders", grain="month", period_key="2026-09", previous_period_key="2026-08",
        current=50.0, previous=51.0, pct_change=-2.0, direction="down", magnitude=2.0,
        significant=False, root_cause=None, impact=None, forecast=None, anomaly=None,
        recommendation=None, confidence=0.6, notes=["orders moved down 2.0%."],
    )
    monkeypatch.setattr(ai_analyst, "assemble_all_evidence", lambda db, grain: [sig, quiet])
    from src.ai_analyst import _summarize_period_material
    material = _summarize_period_material(db=object())
    assert material.evidence == ["revenue moved down 16.7%."]
    assert material.recommendation is not None


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #
def test_explain_metric_change_assembles_answer(monkeypatch):
    import src.ai_analyst as ai_analyst
    material = _AnswerMaterial(
        evidence=["revenue moved down 20.0%."], root_cause="West drove it.",
        impact="20.0 at risk.", recommendation="Investigate.", confidence=0.8, detail={"x": 1},
    )
    monkeypatch.setattr(ai_analyst, "_metric_change_material", lambda db, metric, grain: material)
    monkeypatch.setattr(ai_analyst, "_build_summary", lambda evidence, settings: ("Revenue fell.", "template"))
    answer = explain_metric_change(db=object(), metric="revenue", settings=_UNCONFIGURED)
    assert isinstance(answer, AnalystAnswer)
    assert answer.intent == "metric_change"
    assert answer.summary == "Revenue fell."
    assert answer.confidence_band == "HIGH"
    assert answer.engine == "template"
    json.dumps(answer.to_dict())


def test_worst_performing_region_none_material_produces_safe_answer(monkeypatch):
    import src.ai_analyst as ai_analyst
    monkeypatch.setattr(ai_analyst, "_worst_region_material", lambda db: None)
    answer = worst_performing_region(db=object(), settings=_UNCONFIGURED)
    assert answer.confidence == 0.0
    assert answer.confidence_band == "LOW"
    assert "Not enough history" in answer.summary


def test_products_needing_attention_assembles_answer(monkeypatch):
    import src.ai_analyst as ai_analyst
    material = _AnswerMaterial(
        evidence=["Gizmo: health score 5.0."], root_cause="Gizmo has the lowest health score.",
        impact=None, recommendation="Prioritize Gizmo.", confidence=0.5, detail={},
    )
    monkeypatch.setattr(ai_analyst, "_products_attention_material", lambda db: material)
    monkeypatch.setattr(ai_analyst, "_build_summary", lambda evidence, settings: (evidence[0], "template"))
    answer = products_needing_attention(db=object(), settings=_UNCONFIGURED)
    assert answer.intent == "products_attention"
    assert answer.recommendation == "Prioritize Gizmo."


def test_biggest_risks_assembles_answer(monkeypatch):
    import src.ai_analyst as ai_analyst
    material = _AnswerMaterial(
        evidence=["Investigate demand (priority HIGH, score 60.0): Revenue down."],
        root_cause=None, impact="15000.0 at risk.", recommendation="Investigate demand",
        confidence=0.8, detail={},
    )
    monkeypatch.setattr(ai_analyst, "_biggest_risks_material", lambda db: material)
    monkeypatch.setattr(ai_analyst, "_build_summary", lambda evidence, settings: (evidence[0], "template"))
    answer = biggest_risks(db=object(), settings=_UNCONFIGURED)
    assert answer.intent == "biggest_risks"
    assert answer.confidence_band == "HIGH"


def test_summarize_period_assembles_answer(monkeypatch):
    import src.ai_analyst as ai_analyst
    material = _AnswerMaterial(
        evidence=["revenue moved down 16.7%."], root_cause=None, impact=None,
        recommendation="See biggest risks for prioritized actions.", confidence=0.5, detail={},
    )
    monkeypatch.setattr(ai_analyst, "_summarize_period_material", lambda db, grain: material)
    monkeypatch.setattr(ai_analyst, "_build_summary", lambda evidence, settings: (evidence[0], "template"))
    answer = summarize_period(db=object(), settings=_UNCONFIGURED)
    assert answer.intent == "summarize_period"


def test_answer_question_routes_all_six_spec_questions(monkeypatch):
    import src.ai_analyst as ai_analyst
    stand_in = _AnswerMaterial(evidence=["fact."], root_cause=None, impact=None,
                               recommendation=None, confidence=0.5, detail={})
    monkeypatch.setattr(ai_analyst, "_metric_change_material", lambda db, metric, grain: stand_in)
    monkeypatch.setattr(ai_analyst, "_worst_region_material", lambda db: stand_in)
    monkeypatch.setattr(ai_analyst, "_products_attention_material", lambda db: stand_in)
    monkeypatch.setattr(ai_analyst, "_biggest_risks_material", lambda db: stand_in)
    monkeypatch.setattr(ai_analyst, "_summarize_period_material", lambda db, grain: stand_in)
    monkeypatch.setattr(ai_analyst, "_build_summary", lambda evidence, settings: (evidence[0], "template"))

    cases = [
        ("Why did revenue decrease?", "metric_change"),
        ("Which region performed worst?", "worst_region"),
        ("Which products need attention?", "products_attention"),
        ("What caused profit to decline?", "metric_change"),
        ("What are the biggest risks?", "biggest_risks"),
        ("Summarize this month.", "summarize_period"),
    ]
    for question, expected_intent in cases:
        answer = answer_question(db=object(), question=question, settings=_UNCONFIGURED)
        assert answer.intent == expected_intent, question


def test_answer_question_unsupported_question_is_safe(monkeypatch):
    import src.ai_analyst as ai_analyst
    monkeypatch.setattr(ai_analyst, "_build_summary", lambda evidence, settings: (evidence[0], "template"))
    answer = answer_question(db=object(), question="What is the weather?", settings=_UNCONFIGURED)
    assert answer.intent == "unsupported"
    assert answer.confidence == 0.0


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL
# --------------------------------------------------------------------------- #
@pg_integration
def test_answer_question_round_trip():
    pytest.importorskip("psycopg2")
    from src.database import Database

    db = Database()
    answer = answer_question(db, "Why did revenue decrease?")
    assert isinstance(answer, AnalystAnswer)
    json.dumps(answer.to_dict())
