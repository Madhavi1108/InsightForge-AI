"""Pipeline - status, last run, duration, records, quality, anomalies,
failures; three controlled actions only (``docs/system-components.md``:
"controlled actions - run pipeline, inspect run, inspect errors - no
arbitrary execution")."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import incoming_files, require_database, run_pipeline_subprocess

st.set_page_config(page_title="Pipeline", layout="wide")
st.title("Pipeline")

db = require_database()

st.subheader("Run pipeline")
action = st.radio("Mode", ["Scan data/incoming/", "Run one file"], horizontal=True)
args: list[str] | None
if action == "Run one file":
    files = incoming_files()
    chosen = st.selectbox("File", files) if files else None
    if not files:
        st.caption("No files currently in data/incoming/.")
    run_clicked = st.button("Run", disabled=not chosen)
    args = ["--file", f"data/incoming/{chosen}"] if chosen else None
else:
    run_clicked = st.button("Run")
    args = ["--scan"]

if run_clicked and args:
    with st.spinner("Running..."):
        result = run_pipeline_subprocess(args)
    (st.success if result.returncode == 0 else st.error)(f"Exit code {result.returncode}")
    with st.expander("Output"):
        st.code(result.stdout + result.stderr or "(no output)")

st.subheader("Recent runs")
runs = db.fetch_all(
    "SELECT run_id, file_name, status, started_at, duration_s, rows_received, "
    "rows_valid, rows_rejected, dq_score, error FROM pipeline_runs "
    "ORDER BY started_at DESC LIMIT 20"
)
if runs:
    st.dataframe(runs, use_container_width=True)
else:
    st.caption("No pipeline runs yet.")

st.subheader("Inspect a run")
default_run_id = runs[0]["run_id"] if runs else 1
run_id = st.number_input("Run ID", min_value=1, step=1, value=default_run_id)
if st.button("Inspect run"):
    row = db.fetch_one("SELECT * FROM pipeline_runs WHERE run_id = :r", {"r": run_id})
    if row is None:
        st.warning("No such run.")
    else:
        st.json({k: str(v) for k, v in row.items()})

st.subheader("Inspect errors")
if st.button("Inspect errors"):
    rejects = db.fetch_all(
        "SELECT source_row_number, order_id, rejection_category, rejection_reason, "
        "dq_dimension FROM rejected_records WHERE run_id = :r LIMIT 200", {"r": run_id},
    )
    if rejects:
        st.dataframe(rejects, use_container_width=True)
    else:
        st.caption("No rejected rows for this run.")
