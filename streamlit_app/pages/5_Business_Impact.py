"""Business Impact - expected vs. actual revenue/profit gap and at-risk
figures (Phase 23)."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import fmt_currency, require_database
from src.impact_analysis import assess_business_impact_series

st.set_page_config(page_title="Business Impact", layout="wide")
st.title("Business Impact")

db = require_database()

try:
    series = assess_business_impact_series(db)
except Exception as exc:  # noqa: BLE001
    st.warning(str(exc))
    series = []

if not series:
    st.info("Not enough history yet for a business-impact assessment.")
    st.stop()

latest = series[-1]
cols = st.columns(4)
cols[0].metric("Revenue gap", fmt_currency(latest.revenue_gap))
cols[1].metric("Profit gap", fmt_currency(latest.profit_gap))
cols[2].metric("Revenue at risk", fmt_currency(latest.revenue_at_risk))
cols[3].metric("Profit at risk", fmt_currency(latest.profit_at_risk))
st.caption(f"As of {latest.date} - {latest.customers_affected} customers, "
          f"{latest.orders_affected} orders affected.")

st.subheader("Gap over time")
st.line_chart(
    {"date": [r.date for r in series],
     "revenue_gap": [r.revenue_gap for r in series],
     "profit_gap": [r.profit_gap for r in series]},
    x="date",
)
