"""Anomalies - the fused, 4-detector anomaly output (Phase 20)."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database

st.set_page_config(page_title="Anomalies", layout="wide")
st.title("Anomalies")

db = require_database()

severities = ["All", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
severity = st.selectbox("Severity", severities)

if severity == "All":
    rows = db.fetch_all(
        "SELECT metric, anomaly_date, grain, observed_value, expected_value, "
        "deviation_pct, direction, detector_votes, confidence, severity "
        "FROM anomalies ORDER BY anomaly_date DESC LIMIT 200"
    )
else:
    rows = db.fetch_all(
        "SELECT metric, anomaly_date, grain, observed_value, expected_value, "
        "deviation_pct, direction, detector_votes, confidence, severity "
        "FROM anomalies WHERE severity = :s ORDER BY anomaly_date DESC LIMIT 200",
        {"s": severity},
    )

if rows:
    st.dataframe(rows, use_container_width=True)
    counts = db.fetch_all(
        "SELECT severity, COUNT(*) AS n FROM anomalies GROUP BY severity ORDER BY severity"
    )
    if counts:
        st.subheader("Severity counts")
        st.bar_chart({r["severity"]: r["n"] for r in counts})
else:
    st.caption("No anomalies recorded yet.")
