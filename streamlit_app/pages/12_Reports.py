"""Reports - Excel/PDF report downloads per pipeline run (Phase 33
generates them; this page only offers a download for a report that
actually exists on disk)."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database, valid_report_path

st.set_page_config(page_title="Reports", layout="wide")
st.title("Reports")

db = require_database()

runs = db.fetch_all(
    "SELECT run_id, file_name, status, started_at, stage_metrics FROM pipeline_runs "
    "ORDER BY started_at DESC LIMIT 20"
)

if not runs:
    st.info("No pipeline runs yet - run the pipeline from the Pipeline page.")
    st.stop()

for run in runs:
    label = f"#{run['run_id']} - {run['file_name']} - {run['status']} ({run['started_at']})"
    with st.expander(label):
        report = (run["stage_metrics"] or {}).get("report", {})
        excel_path = valid_report_path(report.get("excel"))
        pdf_path = valid_report_path(report.get("pdf"))

        if excel_path is None and pdf_path is None:
            st.caption("Report not available for this run.")
            continue

        col1, col2 = st.columns(2)
        if excel_path is not None:
            col1.download_button(
                "Download Excel", data=excel_path.read_bytes(), file_name=excel_path.name,
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                key=f"excel-{run['run_id']}",
            )
        else:
            col1.caption("Excel report not available.")

        if pdf_path is not None:
            col2.download_button(
                "Download PDF", data=pdf_path.read_bytes(), file_name=pdf_path.name,
                mime="application/pdf", key=f"pdf-{run['run_id']}",
            )
        else:
            col2.caption("PDF report not available.")
