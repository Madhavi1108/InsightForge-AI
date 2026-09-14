"""Data Quality - per-dimension DQ scores and drift results for a run."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database

st.set_page_config(page_title="Data Quality", layout="wide")
st.title("Data Quality")

db = require_database()

runs = db.fetch_all(
    "SELECT run_id, file_name, started_at FROM pipeline_runs ORDER BY started_at DESC LIMIT 50"
)
if not runs:
    st.info("No pipeline runs yet - run the pipeline from the Pipeline page.")
    st.stop()

labels = [f"#{r['run_id']} - {r['file_name']}" for r in runs]
choice = st.selectbox("Run", labels)
run_id = runs[labels.index(choice)]["run_id"]

st.subheader("Data quality dimensions")
dq_rows = db.fetch_all(
    "SELECT dimension, score, passed, records_checked, records_failed "
    "FROM data_quality_results WHERE run_id = :r ORDER BY dimension", {"r": run_id},
)
st.dataframe(dq_rows, use_container_width=True) if dq_rows else st.caption("No DQ results for this run.")

st.subheader("Data drift (PSI)")
drift_rows = db.fetch_all(
    "SELECT feature, psi_score, status, baseline_count, current_count "
    "FROM drift_results WHERE run_id = :r ORDER BY psi_score DESC NULLS LAST", {"r": run_id},
)
st.dataframe(drift_rows, use_container_width=True) if drift_rows else st.caption("No drift results for this run.")
