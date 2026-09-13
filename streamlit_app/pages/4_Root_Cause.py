"""Root Cause - drill-down contribution analysis (Phase 22)."""
import sys
from datetime import date, timedelta
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database
from src.root_cause import RCA_SUPPORTED_METRICS, analyze_root_cause

st.set_page_config(page_title="Root Cause", layout="wide")
st.title("Root Cause")

db = require_database()

metric = st.selectbox("Metric", RCA_SUPPORTED_METRICS)
today = date.today()
col1, col2 = st.columns(2)
with col1:
    st.caption("Current period")
    current_start = st.date_input("Current start", today - timedelta(days=1), key="cs")
    current_end = st.date_input("Current end", today - timedelta(days=1), key="ce")
with col2:
    st.caption("Previous period")
    previous_start = st.date_input("Previous start", today - timedelta(days=2), key="ps")
    previous_end = st.date_input("Previous end", today - timedelta(days=2), key="pe")

if st.button("Analyze"):
    with st.spinner("Analyzing..."):
        try:
            result = analyze_root_cause(
                db, metric, str(current_start), str(current_end),
                str(previous_start), str(previous_end),
            )
        except Exception as exc:  # noqa: BLE001 - never a raw stack trace in the UI
            st.warning(str(exc))
            result = None

    if result is not None:
        cols = st.columns(3)
        cols[0].metric("Change", result.change)
        cols[1].metric("Primary driver", result.primary_driver)
        cols[2].metric("Confidence", f"{result.confidence:.2f}")

        if result.tiers:
            st.subheader("Drill-down")
            st.dataframe(
                [{"dimension": t.dimension, "primary_value": t.primary_value,
                  "contribution": t.contribution, "confidence": t.confidence} for t in result.tiers],
                use_container_width=True,
            )
        else:
            st.caption("No drill-down tiers - insufficient data for this period.")
