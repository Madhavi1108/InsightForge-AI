"""Recommendations - the full rule-triggered recommendation list (Overview
only shows the top 5); backed by src.recommendations (Phase 26)."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database
from src.recommendations import generate_recommendations

st.set_page_config(page_title="Recommendations", layout="wide")
st.title("Recommendations")

db = require_database()
with st.container(key="ai-page-accent"):
    st.caption("Priority-ranked, evidence-based recommendations - severity x impact x confidence.")

try:
    recs = generate_recommendations(db)
except Exception as exc:  # noqa: BLE001 - never crash the page on a downstream failure
    st.warning(f"Could not load recommendations: {exc}")
    recs = []

if not recs:
    st.info("No open recommendations.")
    st.stop()

bands = ["All", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
col1, col2 = st.columns(2)
severity = col1.selectbox("Severity", bands)
priority_band = col2.selectbox("Priority band", bands)

filtered = recs
if severity != "All":
    filtered = [r for r in filtered if r.severity == severity]
if priority_band != "All":
    filtered = [r for r in filtered if r.priority_band == priority_band]

if filtered:
    st.dataframe(
        [{"title": r.title, "severity": r.severity, "priority_score": r.priority_score,
          "priority_band": r.priority_band, "rationale": r.rationale,
          "impact_value": r.impact_value, "metric": r.metric,
          "period_key": r.period_key} for r in filtered],
        use_container_width=True,
    )
else:
    st.caption("No recommendations match this filter.")
