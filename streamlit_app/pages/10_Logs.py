"""Logs - full pipeline run history, stage metrics, and per-file
processed summaries. (Structured file-based logging is a Phase 35
concern - documented in docs/streamlit-app.md - this page's "logs" are
the pipeline_runs audit trail already persisted by every prior phase.)"""
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database
from src.config import get_paths

st.set_page_config(page_title="Logs", layout="wide")
st.title("Logs")

db = require_database()

statuses = ["All", "RUNNING", "SUCCESS", "WARNING", "PARTIAL", "FAILED", "SKIPPED_DUPLICATE"]
status = st.selectbox("Status", statuses)

if status == "All":
    runs = db.fetch_all(
        "SELECT run_id, file_name, status, started_at, finished_at, duration_s, "
        "rows_received, rows_valid, rows_rejected, dq_score, error, stage_metrics "
        "FROM pipeline_runs ORDER BY started_at DESC LIMIT 100"
    )
else:
    runs = db.fetch_all(
        "SELECT run_id, file_name, status, started_at, finished_at, duration_s, "
        "rows_received, rows_valid, rows_rejected, dq_score, error, stage_metrics "
        "FROM pipeline_runs WHERE status = :s ORDER BY started_at DESC LIMIT 100",
        {"s": status},
    )

if not runs:
    st.info("No pipeline runs match this filter.")
    st.stop()

paths = get_paths()
for run in runs:
    label = f"#{run['run_id']} - {run['file_name']} - {run['status']} ({run['started_at']})"
    with st.expander(label):
        st.write(f"Duration: {run['duration_s']}s | Rows: {run['rows_received']} received, "
                f"{run['rows_valid']} valid, {run['rows_rejected']} rejected | "
                f"DQ score: {run['dq_score']}")
        if run["error"]:
            st.error(run["error"])
        if run["stage_metrics"]:
            st.json(run["stage_metrics"])

        summary_path = paths.processed / f"{Path(run['file_name']).stem}.json"
        if summary_path.is_file():
            try:
                st.json(json.loads(summary_path.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                st.caption("Processed summary file could not be read.")
