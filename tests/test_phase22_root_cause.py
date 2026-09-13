"""Phase 22 (spec Phases 43-45, FR-14) - root-cause engine, contribution
analysis & RCA confidence.

A pure, on-demand module (like Phase 17's change_detection.py) - no
database writes, not orchestrator-wired. The drill-down/contribution math
is pure and gets direct unit tests; the DB-querying wrapper
(`analyze_root_cause`) needs real `fact_sales` data, so it's
`INSIGHTFORGE_PG_INTEGRATION=1`-gated (development rule 1: never fake
functionality).
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from src.root_cause import (
    DEFAULT_MIN_EVIDENCE_ROWS,
    HIERARCHY,
    RCA_SUPPORTED_METRICS,
    DimensionContribution,
    RootCauseResult,
    RootCauseTier,
    contribution_by_dimension,
    drill_down,
    min_evidence_rows,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase22_test__"


def _row(region, category, sub_category, product_id, segment, revenue, profit=None, quantity=1):
    return {
        "region": region, "category": category, "sub_category": sub_category,
        "product_id": product_id, "product_name": f"{product_id} name",
        "customer_segment": segment,
        "revenue": revenue, "profit": profit if profit is not None else revenue * 0.2,
        "quantity": quantity,
    }


# Spec's own worked example: Revenue decline = 8.7L; West -5.1L, South -2.0L,
# North -1.1L, Other -0.5L (using 10.0-revenue rows scaled by count).
def _spec_example_rows():
    current = (
        [_row("West", "Electronics", "Laptop", "P1", "Consumer", 10.0)] * 10
        + [_row("South", "Electronics", "Phone", "P2", "Consumer", 10.0)] * 8
        + [_row("North", "Furniture", "Chairs", "P3", "Corporate", 10.0)] * 9
        + [_row("Other", "Furniture", "Tables", "P4", "Corporate", 10.0)] * 9
    )
    previous = (
        [_row("West", "Electronics", "Laptop", "P1", "Consumer", 10.0)] * 61
        + [_row("South", "Electronics", "Phone", "P2", "Consumer", 10.0)] * 28
        + [_row("North", "Furniture", "Chairs", "P3", "Corporate", 10.0)] * 20
        + [_row("Other", "Furniture", "Tables", "P4", "Corporate", 10.0)] * 14
    )
    return current, previous


def test_phase22_files_exist():
    for p in (
        PROJECT_ROOT / "src" / "root_cause.py",
        PROJECT_ROOT / "docs" / "root-cause-analysis.md",
    ):
        assert p.is_file(), f"missing {p}"


def test_import_has_no_side_effects():
    r = subprocess.run(
        [sys.executable, "-c", "import src.root_cause"],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


# --------------------------------------------------------------------------- #
# contribution_by_dimension
# --------------------------------------------------------------------------- #
def test_contributions_sum_to_total_change_matching_spec_example():
    current, previous = _spec_example_rows()
    contributions = contribution_by_dimension(current, previous, "region", "revenue")
    by_region = {c.value: c.contribution for c in contributions}
    assert by_region["West"] == -510.0
    assert by_region["South"] == -200.0
    assert by_region["North"] == -110.0
    assert by_region["Other"] == -50.0
    total_change = sum(r["revenue"] for r in current) - sum(r["revenue"] for r in previous)
    assert sum(c.contribution for c in contributions) == pytest.approx(total_change, abs=0.01)


def test_contributions_sorted_by_magnitude_descending():
    current, previous = _spec_example_rows()
    contributions = contribution_by_dimension(current, previous, "region", "revenue")
    magnitudes = [abs(c.contribution) for c in contributions]
    assert magnitudes == sorted(magnitudes, reverse=True)
    assert contributions[0].value == "West"


def test_contribution_result_type_and_counts():
    current, previous = _spec_example_rows()
    [west] = [c for c in contribution_by_dimension(current, previous, "region", "revenue")
             if c.value == "West"]
    assert isinstance(west, DimensionContribution)
    assert west.current_count == 10
    assert west.previous_count == 61


# --------------------------------------------------------------------------- #
# drill_down
# --------------------------------------------------------------------------- #
def test_drill_down_traces_concentrated_change_through_every_tier():
    current, previous = _spec_example_rows()
    tiers = drill_down(current, previous, "revenue")
    assert [t.dimension for t in tiers] == list(HIERARCHY)
    by_dim = {t.dimension: t for t in tiers}
    assert by_dim["region"].primary_value == "West"
    assert by_dim["category"].primary_value == "Electronics"
    assert by_dim["sub_category"].primary_value == "Laptop"
    assert by_dim["product"].primary_value == "P1"
    assert by_dim["customer_segment"].primary_value == "Consumer"
    for t in tiers:
        assert isinstance(t, RootCauseTier)
        assert 0.0 <= t.confidence <= 1.0


def test_drill_down_filters_narrow_at_each_tier():
    current, previous = _spec_example_rows()
    tiers = drill_down(current, previous, "revenue")
    by_dim = {t.dimension: t for t in tiers}
    assert by_dim["category"].filters == {"region": "West"}
    assert by_dim["sub_category"].filters == {"region": "West", "category": "Electronics"}


def test_drill_down_diffuse_change_has_low_concentration_confidence():
    # Four regions each shrink by an identical amount - no dominant driver.
    current = [_row(r, "Electronics", "Laptop", "P1", "Consumer", 10.0) for r in
               ["West", "South", "North", "Other"] for _ in range(5)]
    previous = [_row(r, "Electronics", "Laptop", "P1", "Consumer", 10.0) for r in
                ["West", "South", "North", "Other"] for _ in range(10)]
    tiers = drill_down(current, previous, "revenue")
    region_tier = tiers[0]
    # Roughly even contributions -> concentration near 0.25 (1 of 4 equal shares).
    assert region_tier.confidence < 0.6


def test_drill_down_empty_rows_returns_no_tiers():
    assert drill_down([], [], "revenue") == []


# --------------------------------------------------------------------------- #
# min_evidence_rows / env
# --------------------------------------------------------------------------- #
def test_default_min_evidence_rows(monkeypatch):
    monkeypatch.delenv("RCA_MIN_EVIDENCE_ROWS", raising=False)
    assert min_evidence_rows() == DEFAULT_MIN_EVIDENCE_ROWS


def test_min_evidence_rows_honours_env(monkeypatch):
    monkeypatch.setenv("RCA_MIN_EVIDENCE_ROWS", "5")
    assert min_evidence_rows() == 5


# --------------------------------------------------------------------------- #
# analyze_root_cause - metric scoping + assembly (stub DB)
# --------------------------------------------------------------------------- #
class _FactRowsDB:
    def __init__(self, current_rows, previous_rows, current_period, previous_period):
        self._current_rows = current_rows
        self._previous_rows = previous_rows
        self._current_period = current_period
        self._previous_period = previous_period

    def fetch_all(self, sql, params=None):
        start = params["start"]
        if start == self._current_period[0]:
            return self._current_rows
        return self._previous_rows


def test_analyze_root_cause_rejects_unsupported_metric():
    from src.root_cause import analyze_root_cause
    db = _FactRowsDB([], [], ("2026-02-01", "2026-02-07"), ("2026-01-01", "2026-01-07"))
    with pytest.raises(ValueError):
        analyze_root_cause(db, "margin_pct", "2026-02-01", "2026-02-07", "2026-01-01", "2026-01-07")


def test_analyze_root_cause_assembles_flat_and_tiered_result():
    from src.root_cause import analyze_root_cause
    current, previous = _spec_example_rows()
    db = _FactRowsDB(current, previous, ("2026-02-01", "2026-02-07"), ("2026-01-01", "2026-01-07"))
    result = analyze_root_cause(db, "revenue", "2026-02-01", "2026-02-07", "2026-01-01", "2026-01-07")
    assert isinstance(result, RootCauseResult)
    assert result.metric == "revenue"
    assert result.change == pytest.approx(-870.0, abs=0.01)
    assert result.primary_driver == "West > Electronics > Laptop > P1 > Consumer"
    assert result.contribution == pytest.approx(-510.0, abs=0.01)
    assert 0.0 <= result.confidence <= 1.0
    assert result.evidence["current_rows"] == len(current)
    assert result.evidence["previous_rows"] == len(previous)
    assert len(result.tiers) == len(HIERARCHY)


# --------------------------------------------------------------------------- #
# integration - real PostgreSQL
# --------------------------------------------------------------------------- #
def _apply_schema():
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "apply_schema.py")],
        cwd=PROJECT_ROOT, capture_output=True, text=True, check=False,
    )


def _int_paths(tmp_path):
    from src.config import PipelinePaths
    fields = ("incoming", "raw", "processed", "rejected", "archive", "logs", "reports")
    p = PipelinePaths(**{f: tmp_path / f for f in fields})
    p.ensure()
    return p


@pg_integration
def test_analyze_root_cause_traces_skewed_region(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database
    from src.root_cause import analyze_root_cause

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    df = gd.generate_dataset(rows=1500, seed=37)
    dates = sorted(df["Order_Date"].unique())
    previous_dates = dates[:7]
    current_dates = dates[7:14]

    codes = []
    for i, d in enumerate(previous_dates + current_dates):
        day_df = df[df["Order_Date"] == d].copy()
        if d in current_dates:
            # Skew this period heavily toward the West region to create a
            # concentrated, traceable driver.
            day_df.loc[day_df.sample(frac=0.6, random_state=1).index, "Region"] = "West"
        name = f"{SENTINEL}_{i}.csv"
        day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")
        codes.append(orchestrator.run_file(p.incoming / name))

    try:
        assert all(c in (0, 9) for c in codes)
        result = analyze_root_cause(
            db, "revenue",
            str(current_dates[0]), str(current_dates[-1]),
            str(previous_dates[0]), str(previous_dates[-1]),
        )
        assert result.tiers
        assert result.tiers[0].dimension == "region"
        assert "West" in result.primary_driver
    finally:
        db.execute("DELETE FROM anomalies WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM drift_results WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM fact_sales WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM pipeline_runs WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
        db.execute("DELETE FROM file_registry WHERE file_name LIKE :p", {"p": SENTINEL + "%"})
