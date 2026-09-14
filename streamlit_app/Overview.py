"""Overview - InsightForge AI's landing page (Phase 30, page 1 of 11):
today's KPIs, the latest significant period-over-period moves, and the
top open recommendations."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import fmt_currency, fmt_pct, require_database
from src.change_detection import compare_period
from src.recommendations import generate_recommendations

st.set_page_config(page_title="InsightForge AI - Overview", layout="wide")
st.title("Overview")

db = require_database()

st.subheader("Latest daily KPIs")
kpi_row = db.fetch_one("SELECT * FROM daily_kpis ORDER BY order_date DESC LIMIT 1")
if kpi_row:
    cols = st.columns(4)
    cols[0].metric("Revenue", fmt_currency(kpi_row["revenue"]))
    cols[1].metric("Profit", fmt_currency(kpi_row["profit"]))
    cols[2].metric("Orders", kpi_row["orders"])
    cols[3].metric("Return rate", fmt_pct(kpi_row["return_rate_pct"]))
else:
    st.info("No KPI data yet - run the pipeline from the Pipeline page.")

st.subheader("Significant day-over-day moves")
records = [r for r in compare_period(db, "day") if r.significant]
if records:
    st.dataframe(
        [{"metric": r.metric, "direction": r.direction,
          "change_%": r.pct_change, "current": r.current} for r in records],
        use_container_width=True,
    )
else:
    st.caption("Nothing significant today.")

st.subheader("Top open recommendations")
try:
    recs = generate_recommendations(db)[:5]
except Exception as exc:  # noqa: BLE001 - never crash the page on a downstream failure
    st.warning(f"Could not load recommendations: {exc}")
    recs = []
if recs:
    st.dataframe(
        [{"title": r.title, "severity": r.severity,
          "priority": r.priority_score, "band": r.priority_band} for r in recs],
        use_container_width=True,
    )
else:
    st.caption("No open recommendations.")
