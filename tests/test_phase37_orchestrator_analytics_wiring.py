"""Phase 37 (backend verification follow-up) - wires forecasting
(Phase 25), root-cause -> impact -> recommendations (Phase 22/23/26), and
AI evidence assembly (Phase 27) into ``src/orchestrator.py``'s per-file
pipeline.

Before this phase these five modules were pure, on-demand-only analytics
(their own module docstrings said so explicitly) - correct for RCA/impact
(no persistence table exists for them by design; see
``docs/root-cause-analysis.md`` / ``docs/business-impact-engine.md``), but
it left the master spec's connected pipeline
("DETECT -> EXPLAIN -> QUANTIFY -> PREDICT -> RECOMMEND -> COMMUNICATE")
broken after drift detection: nothing invoked forecasting or
recommendations automatically per file, so RCA and impact (which
``generate_and_persist_recommendations`` calls internally to enrich each
recommendation) never ran either.

Same pattern as Phase 20/21's orchestrator wiring: additive only
(``stage_metrics.forecast`` / ``.recommendations`` / ``.ai_evidence``),
never changes ``pipeline_runs.status``, a failure in any of them is
swallowed and logged, never fails the run.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

pg_integration = pytest.mark.skipif(
    os.environ.get("INSIGHTFORGE_PG_INTEGRATION") != "1",
    reason="set INSIGHTFORGE_PG_INTEGRATION=1 with a running PostgreSQL to enable",
)
SENTINEL = "__phase37_test__"


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
def test_orchestrator_wires_forecast_recommendations_and_evidence(tmp_path, monkeypatch):
    pytest.importorskip("psycopg2")
    _apply_schema()
    from scripts import generate_dataset as gd
    from src import orchestrator
    from src.database import Database

    p = _int_paths(tmp_path)
    monkeypatch.setattr(orchestrator, "get_paths", lambda: p)
    db = Database()

    # Enough daily history for forecasting's min_train_days() + horizon.
    df = gd.generate_dataset(rows=3000, seed=41)
    dates = sorted(df["Order_Date"].unique())[:45]
    codes = []
    names = []
    for i, d in enumerate(dates):
        day_df = df[df["Order_Date"] == d].copy()
        name = f"{SENTINEL}_{i}.csv"
        names.append(name)
        day_df.to_csv(p.incoming / name, index=False, date_format="%Y-%m-%d")
        codes.append(orchestrator.run_file(p.incoming / name))

    try:
        assert all(c in (0, 9) for c in codes)

        run_ids = [r["run_id"] for r in db.fetch_all(
            "SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p ORDER BY run_id",
            {"p": SENTINEL + "%"},
        )]
        last_run_id = run_ids[-1]

        # forecast_results: the last (latest-date) run has enough trailing
        # history to produce at least one forecast row.
        forecast_count = db.scalar(
            "SELECT count(*) FROM forecast_results WHERE run_id = :r", {"r": last_run_id},
        )
        assert forecast_count > 0

        # stage_metrics recorded the new stages for every run (success or
        # advisory failure - never silently absent).
        rows = db.fetch_all(
            "SELECT stage_metrics FROM pipeline_runs WHERE run_id = ANY(:ids)", {"ids": run_ids},
        )
        for row in rows:
            sm = row["stage_metrics"]
            assert "forecast" in sm
            assert "recommendations" in sm
            assert "ai_evidence" in sm
    finally:
        db.execute("DELETE FROM forecast_results WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
        db.execute("DELETE FROM recommendations WHERE run_id IN "
                   "(SELECT run_id FROM pipeline_runs WHERE file_name LIKE :p)",
                   {"p": SENTINEL + "%"})
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
