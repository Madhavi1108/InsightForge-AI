"""Phase 27 (spec Phase 53, part of FR-20) - AI evidence layer.

`src/ai_evidence.py` makes **no LLM call** - it composes already-verified
output from Phases 17/20-26 into one structured package per metric.
`aggregate_confidence` is pure and gets a direct unit test.
`assemble_evidence` is tested against a stub `compare_period` plus
monkeypatched best-effort sub-lookups (the same shape as Phase 26's stubbed
`compare_period`/enrichment functions). The one direct SQL query this
module owns (`_lookup_anomaly`) gets a light `INSIGHTFORGE_PG_INTEGRATION
=1`-gated live check against a real (empty) `anomalies` table - every other
piece of real data access is already covered by the modules this one
composes, each with their own live tests.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.ai_evidence import (
    EvidencePackage,
    aggregate_confidence,
    assemble_evidence,
    evidence_package_to_dict,
)
from src.change_detection import ChangeRecord

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)


def test_phase27_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "ai_evidence.py",
        PROJECT_ROOT / "docs" / "ai-evidence-layer.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.ai_evidence"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


def test_no_llm_import():
    """Phase 27 never imports the LLM SDK - that's Phase 28."""
    import ast
    tree = ast.parse((PROJECT_ROOT / "src" / "ai_evidence.py").read_text(encoding="utf-8"))
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree) if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "google" not in imported
    assert "genai" not in imported


# --------------------------------------------------------------------------- #
# aggregate_confidence - pure
# --------------------------------------------------------------------------- #
def test_aggregate_confidence_empty_is_zero():
    assert aggregate_confidence([]) == 0.0


def test_aggregate_confidence_single_signal():
    assert aggregate_confidence([0.8]) == 0.8


def test_aggregate_confidence_mean_of_multiple():
    assert aggregate_confidence([1.0, 0.5, 0.0]) == pytest.approx(0.5)


# --------------------------------------------------------------------------- #
# assemble_evidence - stub compare_period + monkeypatched sub-lookups
# --------------------------------------------------------------------------- #
def _record(metric, direction="down", significant=True, pct_change=-20.0, magnitude=20.0,
           current=800.0, previous=1000.0):
    return ChangeRecord(
        grain="day", metric=metric, period_key="2026-09-08", previous_period_key="2026-09-07",
        current=current, previous=previous, pct_change=pct_change, direction=direction,
        magnitude=magnitude, significant=significant,
    )


def test_assemble_evidence_none_when_no_period_data(monkeypatch):
    import src.ai_evidence as ai_evidence
    monkeypatch.setattr(ai_evidence, "compare_period", lambda db, grain: [])
    assert assemble_evidence(db=object(), metric="revenue") is None


def test_assemble_evidence_none_when_metric_not_in_period_data(monkeypatch):
    import src.ai_evidence as ai_evidence
    monkeypatch.setattr(ai_evidence, "compare_period", lambda db, grain: [_record("orders")])
    assert assemble_evidence(db=object(), metric="revenue") is None


def test_assemble_evidence_happy_path_all_sources(monkeypatch):
    import src.ai_evidence as ai_evidence
    monkeypatch.setattr(ai_evidence, "compare_period", lambda db, grain: [_record("revenue")])
    monkeypatch.setattr(
        ai_evidence, "_root_cause_fields",
        lambda db, metric, key, prev: ("West", "Electronics", -5000.0, 0.9),
    )
    monkeypatch.setattr(
        ai_evidence, "_impact_fields",
        lambda db, metric, key: (1000.0, 800.0, -200.0, 200.0, 42, 30),
    )
    monkeypatch.setattr(ai_evidence, "_forecast_fields", lambda db, metric: ("down", 750.0))
    monkeypatch.setattr(
        ai_evidence, "_lookup_anomaly",
        lambda db, metric, key: {"severity": "HIGH", "confidence": 0.85},
    )
    monkeypatch.setattr(
        ai_evidence, "_matching_recommendations",
        lambda db, metric: [{"rule_id": "pricing_pressure", "title": "x",
                             "priority_score": 60.0, "priority_band": "HIGH"}],
    )

    pkg = assemble_evidence(db=object(), metric="revenue")
    assert isinstance(pkg, EvidencePackage)
    assert pkg.metric == "revenue"
    assert pkg.primary_region == "West"
    assert pkg.primary_category == "Electronics"
    assert pkg.expected == 1000.0 and pkg.actual == 800.0
    assert pkg.forecast_trend == "down"
    assert pkg.anomaly_severity == "HIGH"
    assert pkg.anomaly_confidence == 0.85
    assert len(pkg.recommendations) == 1
    assert set(pkg.sources) == {
        "change_detection", "root_cause", "impact_analysis", "forecasting",
        "anomalies", "recommendations",
    }
    assert 0.0 < pkg.confidence <= 1.0


def test_assemble_evidence_unsupported_metric_has_absent_fields(monkeypatch):
    import src.ai_evidence as ai_evidence
    # avg_shipping_days isn't in RCA_SUPPORTED_METRICS / IMPACT_METRICS / FORECAST_METRICS,
    # so _root_cause_fields/_impact_fields/_forecast_fields never touch the DB at all.
    monkeypatch.setattr(
        ai_evidence, "compare_period",
        lambda db, grain: [_record("avg_shipping_days", direction="up", pct_change=15.0, magnitude=15.0)],
    )
    monkeypatch.setattr(ai_evidence, "_lookup_anomaly", lambda db, metric, key: None)
    monkeypatch.setattr(ai_evidence, "_matching_recommendations", lambda db, metric: [])

    pkg = assemble_evidence(db=object(), metric="avg_shipping_days")
    assert pkg is not None
    assert pkg.primary_region is None and pkg.primary_category is None
    assert pkg.expected is None and pkg.actual is None
    assert pkg.forecast_trend is None
    assert pkg.anomaly_severity is None
    assert pkg.recommendations == []
    assert pkg.sources == ["change_detection"]
    assert pkg.confidence == pytest.approx(min(1.0, 15.0 / 100.0))


def test_root_cause_fields_degrades_gracefully_on_error():
    from src.ai_evidence import _root_cause_fields

    class _RaisingDB:
        def fetch_all(self, sql, params=None):
            raise RuntimeError("boom")

    assert _root_cause_fields(_RaisingDB(), "revenue", "2026-09-08", "2026-09-07") == (
        None, None, None, None,
    )


def test_impact_fields_degrades_gracefully_on_error():
    from src.ai_evidence import _impact_fields

    class _RaisingDB:
        def fetch_all(self, sql, params=None):
            raise RuntimeError("boom")

        def fetch_one(self, sql, params=None):
            raise RuntimeError("boom")

    assert _impact_fields(_RaisingDB(), "revenue", "2026-09-08") == (
        None, None, None, None, None, None,
    )


def test_forecast_fields_degrades_gracefully_on_error():
    from src.ai_evidence import _forecast_fields

    class _RaisingDB:
        def fetch_all(self, sql, params=None):
            raise RuntimeError("boom")

    assert _forecast_fields(_RaisingDB(), "revenue") == (None, None)


def test_lookup_anomaly_returns_none_on_db_error():
    from src.ai_evidence import _lookup_anomaly

    class _RaisingDB:
        def fetch_all(self, sql, params=None):
            raise RuntimeError("boom")

    assert _lookup_anomaly(_RaisingDB(), "revenue", "2026-09-08") is None


def test_matching_recommendations_returns_empty_on_error(monkeypatch):
    import src.ai_evidence as ai_evidence

    def _boom(db, grain="day"):
        raise RuntimeError("boom")

    monkeypatch.setattr(ai_evidence, "generate_recommendations", _boom)
    assert ai_evidence._matching_recommendations(object(), "revenue") == []


# --------------------------------------------------------------------------- #
# evidence_package_to_dict
# --------------------------------------------------------------------------- #
def test_evidence_package_to_dict_round_trips_every_field():
    pkg = EvidencePackage(
        metric="revenue", period_key="2026-09-08", previous_period_key="2026-09-07",
        current=800.0, previous=1000.0, pct_change=-20.0, direction="down", significant=True,
        primary_region="West", primary_category="Electronics",
        root_cause_contribution=-5000.0, root_cause_confidence=0.9,
        expected=1000.0, actual=800.0, gap=-200.0, at_risk=200.0,
        customers_affected=42, orders_affected=30,
        forecast_trend="down", forecast_next_7d_value=750.0,
        anomaly_severity="HIGH", anomaly_confidence=0.85,
        recommendations=[{"rule_id": "pricing_pressure"}], confidence=0.85,
        sources=["change_detection", "root_cause"],
    )
    d = evidence_package_to_dict(pkg)
    for f in (
        "metric", "period_key", "previous_period_key", "current", "previous", "pct_change",
        "direction", "significant", "primary_region", "primary_category",
        "root_cause_contribution", "root_cause_confidence", "expected", "actual", "gap",
        "at_risk", "customers_affected", "orders_affected", "forecast_trend",
        "forecast_next_7d_value", "anomaly_severity", "anomaly_confidence", "recommendations",
        "confidence", "sources",
    ):
        assert f in d
        assert d[f] == getattr(pkg, f)


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL, _lookup_anomaly against a real table
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


@pg_integration
def test_lookup_anomaly_runs_against_real_table():
    pytest.importorskip("psycopg2")
    from src.ai_evidence import _lookup_anomaly
    from src.database import Database

    _apply_schema()
    db = Database()
    result = _lookup_anomaly(db, "revenue", "1970-01-01")  # no data on this date - just no-error
    assert result is None
