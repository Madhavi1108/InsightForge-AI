"""Phase 27 (spec Phase 53, FR-20) - AI evidence layer.

Pure helpers (``compute_confidence``, ``summarize_root_cause``,
``summarize_forecast``, ``build_notes``, ``EvidencePackage.to_dict``) get
direct unit tests with hand-built fixtures - no database. The best-effort
``_lookup_*`` wrappers get stub-DB tests proving they degrade to ``None`` on
any DB error rather than raising (same shape as Phase 26's
``test_lookup_anomaly_returns_none_on_db_error`` etc.).
``assemble_evidence``/``assemble_all_evidence`` are tested against a
monkeypatched ``compare_period`` plus monkeypatched lookups (the same shape
as Phase 26's ``test_generate_recommendations_happy_path``). The real
``fact_sales`` -> evidence round trip needs real data, so that's
``INSIGHTFORGE_PG_INTEGRATION=1``-gated (development rule 1: never fake
functionality). Like RCA/impact/forecasting/recommendations (Phases
22-26), the AI evidence layer is **not** wired into
``src/orchestrator.py`` and persists nothing (no ``evidence`` table).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from src.change_detection import ChangeRecord
from src.impact_analysis import BusinessImpactResult
from src.root_cause import RootCauseResult, RootCauseTier
from src.ai_evidence import (
    AnomalyLink,
    EvidencePackage,
    ForecastSummary,
    RecommendationLink,
    RootCauseSummary,
    assemble_all_evidence,
    assemble_evidence,
    build_notes,
    compute_confidence,
    summarize_forecast,
    summarize_root_cause,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)


def test_phase27_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "ai_evidence.py",
        PROJECT_ROOT / "docs" / "ai-evidence.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.ai_evidence"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# fixtures / builders
# --------------------------------------------------------------------------- #
def _record(metric="revenue", direction="down", significant=True,
            pct_change=-25.0, magnitude=25.0, grain="day"):
    return ChangeRecord(
        grain=grain, metric=metric, period_key="2026-09-08", previous_period_key="2026-09-07",
        current=100.0, previous=125.0, pct_change=pct_change, direction=direction,
        magnitude=magnitude, significant=significant,
    )


@dataclass
class _FakePoint:
    forecast_value: float
    mape: float | None = 5.0


def _rca_result(tiers):
    return RootCauseResult(
        metric="revenue", change=-25.0, primary_driver="West > Electronics",
        contribution=-25.0, confidence=0.8, evidence={}, tiers=tiers,
    )


def _tier(dimension, value):
    return RootCauseTier(
        dimension=dimension, filters={}, primary_value=value,
        contribution=-10.0, confidence=0.8, contributions=[],
    )


# --------------------------------------------------------------------------- #
# compute_confidence - pure
# --------------------------------------------------------------------------- #
def test_compute_confidence_both_signals_averages():
    assert compute_confidence(magnitude=50.0, rca_confidence=0.8, anomaly_confidence=0.6) == 0.7


def test_compute_confidence_only_rca():
    assert compute_confidence(magnitude=50.0, rca_confidence=0.9, anomaly_confidence=None) == 0.9


def test_compute_confidence_only_anomaly():
    assert compute_confidence(magnitude=50.0, rca_confidence=None, anomaly_confidence=0.4) == 0.4


def test_compute_confidence_falls_back_to_magnitude():
    assert compute_confidence(magnitude=50.0, rca_confidence=None, anomaly_confidence=None) == 0.5


def test_compute_confidence_no_signals_uses_default():
    assert compute_confidence(magnitude=None, rca_confidence=None, anomaly_confidence=None) == 0.6


# --------------------------------------------------------------------------- #
# summarize_root_cause - pure
# --------------------------------------------------------------------------- #
def test_summarize_root_cause_none_input():
    assert summarize_root_cause(None) is None


def test_summarize_root_cause_no_tiers():
    assert summarize_root_cause(_rca_result([])) is None


def test_summarize_root_cause_flattens_reached_tiers_only():
    tiers = [_tier("region", "West"), _tier("category", "Electronics"), _tier("sub_category", "Laptop")]
    summary = summarize_root_cause(_rca_result(tiers))
    assert summary.primary_region == "West"
    assert summary.primary_category == "Electronics"
    assert summary.primary_sub_category == "Laptop"
    assert summary.primary_product is None
    assert summary.primary_customer_segment is None
    assert summary.primary_driver == "West > Electronics"
    assert summary.contribution == -25.0
    assert summary.confidence == 0.8


# --------------------------------------------------------------------------- #
# summarize_forecast - pure
# --------------------------------------------------------------------------- #
def test_summarize_forecast_none_for_empty_list():
    assert summarize_forecast([], horizon_days=7) is None
    assert summarize_forecast(None, horizon_days=7) is None


def test_summarize_forecast_trend_up():
    points = [_FakePoint(100.0), _FakePoint(150.0)]
    summary = summarize_forecast(points, horizon_days=7)
    assert summary.trend == "up"
    assert summary.first_value == 100.0
    assert summary.last_value == 150.0
    assert summary.mape == 5.0


def test_summarize_forecast_trend_down():
    points = [_FakePoint(150.0), _FakePoint(100.0)]
    assert summarize_forecast(points, horizon_days=7).trend == "down"


def test_summarize_forecast_trend_flat():
    points = [_FakePoint(100.0), _FakePoint(100.0)]
    assert summarize_forecast(points, horizon_days=7).trend == "flat"


# --------------------------------------------------------------------------- #
# build_notes - pure
# --------------------------------------------------------------------------- #
def test_build_notes_change_only():
    notes = build_notes(_record(), root_cause=None, forecast=None, anomaly=None)
    assert len(notes) == 1
    assert "revenue" in notes[0]


def test_build_notes_all_components_present():
    root_cause = RootCauseSummary(
        primary_driver="West > Electronics", primary_region="West", primary_category="Electronics",
        primary_sub_category=None, primary_product=None, primary_customer_segment=None,
        contribution=-25.0, confidence=0.8,
    )
    forecast = ForecastSummary(horizon_days=7, trend="down", first_value=100.0, last_value=80.0, mape=5.0)
    anomaly = AnomalyLink(anomaly_id=1, severity="HIGH", confidence=0.9)
    notes = build_notes(_record(), root_cause, forecast, anomaly)
    assert len(notes) == 4
    assert "Primary driver" in notes[1]
    assert "forecast trend" in notes[2]
    assert "Linked anomaly" in notes[3]


# --------------------------------------------------------------------------- #
# EvidencePackage.to_dict()
# --------------------------------------------------------------------------- #
def test_evidence_package_to_dict_json_round_trips():
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
        forecast=ForecastSummary(horizon_days=7, trend="down", first_value=100.0, last_value=90.0, mape=4.0),
        anomaly=AnomalyLink(anomaly_id=1, severity="HIGH", confidence=0.9),
        recommendation=RecommendationLink(
            recommendation_id=1, title="Investigate", priority_band="HIGH", priority_score=70.0,
        ),
        confidence=0.8, notes=["revenue moved down 20.0%."],
    )
    d = pkg.to_dict()
    assert d["metric"] == "revenue"
    assert d["root_cause"]["primary_region"] == "West"
    assert d["impact"]["revenue_at_risk"] == 20.0
    assert d["anomaly"]["severity"] == "HIGH"
    assert d["recommendation"]["priority_band"] == "HIGH"
    json.dumps(d)  # never raises - plain nested dict, no custom encoder needed


# --------------------------------------------------------------------------- #
# best-effort lookups degrade gracefully on DB errors
# --------------------------------------------------------------------------- #
class _RaisingDB:
    def fetch_all(self, sql, params=None):
        raise RuntimeError("boom")

    def fetch_one(self, sql, params=None):
        raise RuntimeError("boom")


class _AssertNotCalledDB:
    def fetch_all(self, sql, params=None):
        raise AssertionError("must short-circuit before touching the DB")

    def fetch_one(self, sql, params=None):
        raise AssertionError("must short-circuit before touching the DB")


def test_lookup_anomaly_returns_none_on_db_error():
    from src.ai_evidence import _lookup_anomaly
    assert _lookup_anomaly(_RaisingDB(), "revenue", "2026-09-08") is None


def test_lookup_recommendation_returns_none_without_anomaly_id():
    from src.ai_evidence import _lookup_recommendation
    assert _lookup_recommendation(_AssertNotCalledDB(), anomaly_id=None) is None


def test_lookup_recommendation_returns_none_on_db_error():
    from src.ai_evidence import _lookup_recommendation
    assert _lookup_recommendation(_RaisingDB(), anomaly_id=1) is None


def test_lookup_root_cause_returns_none_on_db_error():
    from src.ai_evidence import _lookup_root_cause
    assert _lookup_root_cause(_RaisingDB(), "revenue", "2026-09-08", "2026-09-07") is None


def test_lookup_root_cause_short_circuits_for_unsupported_metric():
    from src.ai_evidence import _lookup_root_cause
    assert _lookup_root_cause(_AssertNotCalledDB(), "return_rate_pct", "2026-09-08", "2026-09-07") is None


def test_lookup_impact_returns_none_on_db_error():
    from src.ai_evidence import _lookup_impact
    assert _lookup_impact(_RaisingDB(), "2026-09-08") is None


def test_lookup_forecast_returns_none_on_db_error():
    from src.ai_evidence import _lookup_forecast
    assert _lookup_forecast(_RaisingDB(), "revenue") is None


def test_lookup_forecast_short_circuits_for_unsupported_metric():
    from src.ai_evidence import _lookup_forecast
    assert _lookup_forecast(_AssertNotCalledDB(), "return_rate_pct") is None


# --------------------------------------------------------------------------- #
# assemble_evidence / assemble_all_evidence - monkeypatched
# --------------------------------------------------------------------------- #
def test_assemble_evidence_none_when_metric_not_in_period_data(monkeypatch):
    import src.ai_evidence as ai_evidence
    monkeypatch.setattr(ai_evidence, "compare_period", lambda db, grain: [])
    assert assemble_evidence(db=object(), metric="revenue") is None


def test_assemble_evidence_happy_path(monkeypatch):
    import src.ai_evidence as ai_evidence
    monkeypatch.setattr(ai_evidence, "compare_period", lambda db, grain: [_record()])
    monkeypatch.setattr(ai_evidence, "_lookup_root_cause", lambda db, m, k, pk: None)
    monkeypatch.setattr(ai_evidence, "_lookup_impact", lambda db, k: None)
    monkeypatch.setattr(ai_evidence, "_lookup_forecast", lambda db, m: None)
    monkeypatch.setattr(ai_evidence, "_lookup_anomaly", lambda db, m, k: AnomalyLink(1, "HIGH", 0.9))
    monkeypatch.setattr(
        ai_evidence, "_lookup_recommendation",
        lambda db, anomaly_id: RecommendationLink(1, "Investigate", "HIGH", 70.0) if anomaly_id else None,
    )

    pkg = assemble_evidence(db=object(), metric="revenue")
    assert isinstance(pkg, EvidencePackage)
    assert pkg.metric == "revenue"
    assert pkg.anomaly.anomaly_id == 1
    assert pkg.recommendation.recommendation_id == 1
    assert pkg.confidence == 0.9  # only anomaly_confidence available
    assert len(pkg.notes) == 2  # change line + anomaly line


def test_assemble_evidence_skips_enrichment_for_non_day_grain(monkeypatch):
    import src.ai_evidence as ai_evidence
    monkeypatch.setattr(ai_evidence, "compare_period", lambda db, grain: [_record(grain="week")])

    def _boom(*args, **kwargs):
        raise AssertionError("enrichment must not run for a non-day grain")

    monkeypatch.setattr(ai_evidence, "_lookup_root_cause", _boom)
    monkeypatch.setattr(ai_evidence, "_lookup_impact", _boom)
    monkeypatch.setattr(ai_evidence, "_lookup_forecast", _boom)
    monkeypatch.setattr(ai_evidence, "_lookup_anomaly", _boom)
    monkeypatch.setattr(ai_evidence, "_lookup_recommendation", _boom)

    pkg = assemble_evidence(db=object(), metric="revenue", grain="week")
    assert pkg.anomaly is None
    assert pkg.recommendation is None
    assert pkg.root_cause is None
    assert pkg.impact is None
    assert pkg.forecast is None


def test_assemble_all_evidence_one_package_per_change_record(monkeypatch):
    import src.ai_evidence as ai_evidence
    records = [_record(metric="revenue"), _record(metric="orders", direction="down")]
    monkeypatch.setattr(ai_evidence, "compare_period", lambda db, grain: records)
    monkeypatch.setattr(ai_evidence, "_lookup_root_cause", lambda db, m, k, pk: None)
    monkeypatch.setattr(ai_evidence, "_lookup_impact", lambda db, k: None)
    monkeypatch.setattr(ai_evidence, "_lookup_forecast", lambda db, m: None)
    monkeypatch.setattr(ai_evidence, "_lookup_anomaly", lambda db, m, k: None)
    monkeypatch.setattr(ai_evidence, "_lookup_recommendation", lambda db, anomaly_id: None)

    packages = assemble_all_evidence(db=object())
    assert [p.metric for p in packages] == ["revenue", "orders"]


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL
# --------------------------------------------------------------------------- #
@pg_integration
def test_assemble_evidence_round_trip():
    pytest.importorskip("psycopg2")
    from src.database import Database

    db = Database()
    pkg = assemble_evidence(db, "revenue")
    if pkg is not None:
        assert isinstance(pkg, EvidencePackage)
        json.dumps(pkg.to_dict())
