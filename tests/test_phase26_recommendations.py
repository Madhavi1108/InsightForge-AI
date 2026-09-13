"""Phase 26 (spec Phases 51-52, FR-19) - recommendation engine & priority
engine.

Rule evaluation and the priority formula are pure - no database - and get
direct unit tests. `generate_recommendations` is tested against a stub
`compare_period` plus monkeypatched best-effort lookups (the same shape as
Phase 25's stubbed `daily_metric_series`); `persist_recommendations`
against a minimal stub `Database` capturing `execute_many` calls. The real
`fact_sales` -> `recommendations` round trip needs real data, so that's
`INSIGHTFORGE_PG_INTEGRATION=1`-gated (development rule 1: never fake
functionality). Like forecasting (Phase 25) and RCA/impact/RFM (Phases
22-24), recommendations are **not** wired into `src/orchestrator.py`.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.change_detection import ChangeRecord
from src.recommendations import (
    DEFAULT_IMPACT_REFERENCE,
    Recommendation,
    compute_priority,
    default_impact_reference,
    evaluate_rules,
    generate_recommendations,
    persist_recommendations,
    severity_weight,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase26_test__"


def test_phase26_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "recommendations.py",
        PROJECT_ROOT / "docs" / "recommendations.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.recommendations"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# ChangeRecord builder
# --------------------------------------------------------------------------- #
def _record(metric, direction, significant, pct_change=20.0, magnitude=None):
    return ChangeRecord(
        grain="day", metric=metric, period_key="2026-09-08", previous_period_key="2026-09-07",
        current=100.0, previous=125.0, pct_change=pct_change, direction=direction,
        magnitude=magnitude if magnitude is not None else abs(pct_change), significant=significant,
    )


# --------------------------------------------------------------------------- #
# evaluate_rules - pure
# --------------------------------------------------------------------------- #
def test_no_significant_metrics_produces_no_matches():
    records = {"revenue": _record("revenue", "down", False)}
    assert evaluate_rules(records) == []


def test_demand_decline_rule():
    records = {
        "revenue": _record("revenue", "down", True),
        "orders": _record("orders", "down", True),
    }
    matches = evaluate_rules(records)
    assert {m.rule_id for m in matches} == {"demand_decline"}


def test_pricing_pressure_rule_when_orders_not_significantly_down():
    records = {
        "revenue": _record("revenue", "down", True),
        "orders": _record("orders", "flat", False),
    }
    matches = evaluate_rules(records)
    assert {m.rule_id for m in matches} == {"pricing_pressure"}


def test_margin_erosion_rule_without_matching_revenue_decline():
    records = {
        "profit": _record("profit", "down", True),
        "revenue": _record("revenue", "up", False),
    }
    matches = evaluate_rules(records)
    assert {m.rule_id for m in matches} == {"margin_erosion"}


def test_margin_erosion_suppressed_when_revenue_also_down():
    records = {
        "revenue": _record("revenue", "down", True),
        "orders": _record("orders", "down", True),
        "profit": _record("profit", "down", True),
    }
    matches = evaluate_rules(records)
    rule_ids = {m.rule_id for m in matches}
    assert "demand_decline" in rule_ids
    assert "margin_erosion" not in rule_ids


def test_quality_risk_rule():
    records = {"return_rate_pct": _record("return_rate_pct", "up", True)}
    matches = evaluate_rules(records)
    assert {m.rule_id for m in matches} == {"quality_risk"}


def test_logistics_delay_rule():
    records = {"avg_shipping_days": _record("avg_shipping_days", "up", True)}
    matches = evaluate_rules(records)
    assert {m.rule_id for m in matches} == {"logistics_delay"}


def test_demand_surge_rule():
    records = {
        "revenue": _record("revenue", "up", True),
        "orders": _record("orders", "up", True),
    }
    matches = evaluate_rules(records)
    assert {m.rule_id for m in matches} == {"demand_surge"}


def test_generic_watch_fallback_covers_uncovered_significant_metric():
    records = {"aov": _record("aov", "down", True)}
    matches = evaluate_rules(records)
    assert len(matches) == 1
    assert matches[0].rule_id == "generic_watch"
    assert "aov" in matches[0].title


def test_every_significant_metric_is_covered_by_a_rule_or_a_co_occurring_one():
    # orders/profit are significant but contextually explained by
    # demand_decline (they never get their own separate recommendation);
    # every other significant metric gets its own dedicated or fallback rule.
    records = {
        "revenue": _record("revenue", "down", True),
        "orders": _record("orders", "down", True),
        "profit": _record("profit", "down", True),
        "return_rate_pct": _record("return_rate_pct", "up", True),
        "avg_shipping_days": _record("avg_shipping_days", "up", True),
        "aov": _record("aov", "down", True),
    }
    matches = evaluate_rules(records)
    rule_ids = {m.rule_id for m in matches}
    assert rule_ids == {"demand_decline", "quality_risk", "logistics_delay", "generic_watch"}
    assert {m.metric for m in matches} == {"revenue", "return_rate_pct", "avg_shipping_days", "aov"}


# --------------------------------------------------------------------------- #
# severity_weight / compute_priority - pure
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("severity, expected", [
    ("LOW", 0.25), ("MEDIUM", 0.5), ("HIGH", 0.75), ("CRITICAL", 1.0),
])
def test_severity_weight(severity, expected):
    assert severity_weight(severity) == expected


def test_compute_priority_known_impact():
    score, band = compute_priority("CRITICAL", impact_value=50000.0, confidence=1.0,
                                   impact_reference=50000.0)
    assert score == pytest.approx(100.0)
    assert band == "CRITICAL"


def test_compute_priority_unknown_impact_uses_neutral_weight():
    score, _ = compute_priority("HIGH", impact_value=None, confidence=1.0, impact_reference=50000.0)
    assert score == pytest.approx(100.0 * 0.75 * 0.5 * 1.0)


def test_compute_priority_low_everything_is_low_band():
    score, band = compute_priority("LOW", impact_value=0.0, confidence=0.1, impact_reference=50000.0)
    assert band == "LOW"
    assert score < 30.0


def test_default_impact_reference(monkeypatch):
    monkeypatch.delenv("PRIORITY_IMPACT_REFERENCE", raising=False)
    assert default_impact_reference() == DEFAULT_IMPACT_REFERENCE


def test_default_impact_reference_env_override(monkeypatch):
    monkeypatch.setenv("PRIORITY_IMPACT_REFERENCE", "10000")
    assert default_impact_reference() == 10000.0


def test_default_impact_reference_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("PRIORITY_IMPACT_REFERENCE", "nope")
    assert default_impact_reference() == DEFAULT_IMPACT_REFERENCE


# --------------------------------------------------------------------------- #
# generate_recommendations - stub compare_period + monkeypatched enrichment
# --------------------------------------------------------------------------- #
def test_generate_recommendations_empty_when_no_period_data(monkeypatch):
    import src.recommendations as recommendations
    monkeypatch.setattr(recommendations, "compare_period", lambda db, grain: [])
    assert generate_recommendations(db=object()) == []


def test_generate_recommendations_happy_path(monkeypatch):
    import src.recommendations as recommendations
    records = [
        _record("revenue", "down", True, pct_change=-25.0, magnitude=25.0),
        _record("orders", "down", True, pct_change=-20.0, magnitude=20.0),
    ]
    monkeypatch.setattr(recommendations, "compare_period", lambda db, grain: records)
    monkeypatch.setattr(recommendations, "_lookup_anomaly", lambda db, metric, key: None)
    monkeypatch.setattr(recommendations, "_lookup_impact", lambda db, metric, key: 25000.0)
    monkeypatch.setattr(recommendations, "_rca_note", lambda db, metric, key, prev: None)
    monkeypatch.setattr(recommendations, "_forecast_note", lambda db, metric, direction: (None, 0.0))

    recs = generate_recommendations(db=object())
    assert len(recs) == 1
    rec = recs[0]
    assert isinstance(rec, Recommendation)
    assert rec.rule_id == "demand_decline"
    assert rec.impact_value == 25000.0
    assert rec.priority_band in ("LOW", "MEDIUM", "HIGH", "CRITICAL")
    assert 0.0 <= rec.priority_score <= 100.0


def test_generate_recommendations_uses_anomaly_upgrade_when_available(monkeypatch):
    import src.recommendations as recommendations
    records = [_record("avg_shipping_days", "up", True, pct_change=15.0, magnitude=15.0)]
    monkeypatch.setattr(recommendations, "compare_period", lambda db, grain: records)
    monkeypatch.setattr(
        recommendations, "_lookup_anomaly",
        lambda db, metric, key: {"anomaly_id": 42, "severity": "CRITICAL", "confidence": 0.95},
    )
    monkeypatch.setattr(recommendations, "_lookup_impact", lambda db, metric, key: None)
    monkeypatch.setattr(recommendations, "_rca_note", lambda db, metric, key, prev: None)
    monkeypatch.setattr(recommendations, "_forecast_note", lambda db, metric, direction: (None, 0.0))

    recs = generate_recommendations(db=object())
    assert len(recs) == 1
    assert recs[0].linked_anomaly_id == 42
    assert recs[0].severity == "CRITICAL"
    assert recs[0].confidence == 0.95


def test_generate_recommendations_forecast_note_boosts_confidence(monkeypatch):
    import src.recommendations as recommendations
    records = [_record("revenue", "down", True, pct_change=-15.0, magnitude=15.0)]
    monkeypatch.setattr(recommendations, "compare_period", lambda db, grain: records)
    monkeypatch.setattr(recommendations, "_lookup_anomaly", lambda db, metric, key: None)
    monkeypatch.setattr(recommendations, "_lookup_impact", lambda db, metric, key: None)
    monkeypatch.setattr(recommendations, "_rca_note", lambda db, metric, key, prev: None)
    monkeypatch.setattr(
        recommendations, "_forecast_note",
        lambda db, metric, direction: ("7-day forecast corroborates the down trend.", 0.05),
    )

    recs = generate_recommendations(db=object())
    assert "forecast corroborates" in recs[0].rationale
    assert recs[0].confidence == pytest.approx(min(1.0, 0.15 + 0.05), abs=1e-6)


def test_generate_recommendations_skips_enrichment_for_non_day_grain(monkeypatch):
    import src.recommendations as recommendations
    records = [_record("revenue", "down", True, pct_change=-15.0, magnitude=15.0)]
    monkeypatch.setattr(recommendations, "compare_period", lambda db, grain: records)

    def _boom(*args, **kwargs):
        raise AssertionError("enrichment must not run for a non-day grain")

    monkeypatch.setattr(recommendations, "_lookup_anomaly", _boom)
    monkeypatch.setattr(recommendations, "_lookup_impact", _boom)
    monkeypatch.setattr(recommendations, "_rca_note", _boom)
    monkeypatch.setattr(recommendations, "_forecast_note", _boom)

    recs = generate_recommendations(db=object(), grain="week")
    assert len(recs) == 1
    assert recs[0].impact_value is None
    assert recs[0].linked_anomaly_id is None


# --------------------------------------------------------------------------- #
# best-effort lookups degrade gracefully on DB errors
# --------------------------------------------------------------------------- #
class _RaisingDB:
    def fetch_all(self, sql, params=None):
        raise RuntimeError("boom")

    def fetch_one(self, sql, params=None):
        raise RuntimeError("boom")


def test_lookup_anomaly_returns_none_on_db_error():
    from src.recommendations import _lookup_anomaly
    assert _lookup_anomaly(_RaisingDB(), "revenue", "2026-09-08") is None


def test_lookup_impact_returns_none_on_db_error():
    from src.recommendations import _lookup_impact
    assert _lookup_impact(_RaisingDB(), "revenue", "2026-09-08") is None


def test_rca_note_returns_none_on_db_error():
    from src.recommendations import _rca_note
    assert _rca_note(_RaisingDB(), "revenue", "2026-09-08", "2026-09-07") is None


def test_rca_note_none_for_unsupported_metric():
    from src.recommendations import _rca_note
    assert _rca_note(_RaisingDB(), "return_rate_pct", "2026-09-08", "2026-09-07") is None


def test_forecast_note_returns_none_on_db_error():
    from src.recommendations import _forecast_note
    note, bonus = _forecast_note(_RaisingDB(), "revenue", "down")
    assert note is None and bonus == 0.0


def test_forecast_note_none_for_unsupported_metric():
    from src.recommendations import _forecast_note
    note, bonus = _forecast_note(_RaisingDB(), "return_rate_pct", "down")
    assert note is None and bonus == 0.0


# --------------------------------------------------------------------------- #
# persist_recommendations - stub Database
# --------------------------------------------------------------------------- #
class _StubDatabase:
    def __init__(self):
        self.calls = []

    def execute_many(self, sql, seq):
        seq = list(seq)
        self.calls.append((sql, seq))
        return len(seq)


def test_persist_recommendations_empty_is_noop():
    db = _StubDatabase()
    assert persist_recommendations(db, run_id=1, recs=[]) == 0
    assert db.calls == []


def test_persist_recommendations_writes_one_row_per_recommendation():
    db = _StubDatabase()
    recs = [
        Recommendation(
            rule_id="demand_decline", title="Investigate declining demand & inventory levels",
            rationale="Revenue and orders both moved down.", metric="revenue",
            period_key="2026-09-08", direction="down", severity="HIGH", confidence=0.8,
            impact_value=12000.0, priority_score=60.0, priority_band="HIGH",
            linked_anomaly_id=None, detail={"period_key": "2026-09-08"},
        ),
    ]
    written = persist_recommendations(db, run_id=9, recs=recs)
    assert written == 1
    sql, seq = db.calls[0]
    assert "INSERT INTO recommendations" in sql
    assert seq[0]["run"] == 9
    assert seq[0]["priority_band"] == "HIGH"


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL, fact_sales -> recommendations round trip
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


@pg_integration
def test_generate_and_persist_recommendations_round_trip():
    pytest.importorskip("psycopg2")
    from src.database import Database
    from src.recommendations import generate_and_persist_recommendations

    _apply_schema()
    db = Database()

    run_id = db.insert_returning(
        "INSERT INTO pipeline_runs (file_name, status, started_at) "
        "VALUES (:f, 'RUNNING', now()) RETURNING run_id",
        {"f": f"{SENTINEL}.csv"},
    )
    try:
        recs = generate_and_persist_recommendations(db, run_id)
        rows = db.fetch_all(
            "SELECT priority_band, priority_score FROM recommendations WHERE run_id = :r",
            {"r": run_id},
        )
        if recs:
            assert rows
            assert all(r["priority_band"] in ("LOW", "MEDIUM", "HIGH", "CRITICAL") for r in rows)
            assert all(0 <= float(r["priority_score"]) <= 100 for r in rows)
    finally:
        db.execute("DELETE FROM recommendations WHERE run_id = :r", {"r": run_id})
        db.execute("DELETE FROM pipeline_runs WHERE run_id = :r", {"r": run_id})
