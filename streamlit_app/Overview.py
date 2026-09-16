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

st.subheader("Data freshness")
try:
    latest_run = db.fetch_one(
        "SELECT run_id, file_name, started_at, finished_at, status FROM pipeline_runs "
        "ORDER BY started_at DESC LIMIT 1"
    )
    date_range = db.fetch_one("SELECT MIN(order_date) AS min_date, MAX(order_date) AS max_date "
                              "FROM daily_kpis")
except Exception as exc:  # noqa: BLE001 - never crash the page on a downstream failure
    st.warning(f"Could not load freshness info: {exc}")
    latest_run, date_range = None, None

if latest_run:
    st.caption(
        f"Last run #{latest_run['run_id']} - {latest_run['file_name']} - "
        f"{latest_run['status']} ({latest_run['started_at']} -> {latest_run['finished_at']})"
    )
else:
    st.caption("No pipeline runs yet.")
if date_range and date_range["min_date"] and date_range["max_date"]:
    st.caption(f"Data covers {date_range['min_date']} to {date_range['max_date']}.")

st.subheader("Latest daily KPIs")
records = compare_period(db, "day")
by_metric = {r.metric: r for r in records}


def _tile(col, label: str, key: str, fmt=fmt_currency) -> None:
    rec = by_metric.get(key)
    if rec is None:
        col.metric(label, "-")
        return
    delta = f"{rec.pct_change:+.2f}%" if rec.pct_change is not None else None
    col.metric(label, fmt(rec.current), delta=delta)


if records:
    row1 = st.columns(4)
    _tile(row1[0], "Revenue", "revenue")
    _tile(row1[1], "Profit", "profit")
    _tile(row1[2], "Orders", "orders", fmt=lambda v: f"{v:,.0f}")
    _tile(row1[3], "Return rate", "return_rate_pct", fmt=fmt_pct)

    row2 = st.columns(5)
    _tile(row2[0], "AOV", "aov")
    _tile(row2[1], "Margin", "margin_pct", fmt=fmt_pct)
    _tile(row2[2], "Customers", "customers", fmt=lambda v: f"{v:,.0f}")
    _tile(row2[3], "Avg discount", "avg_discount_pct", fmt=fmt_pct)
    _tile(row2[4], "Avg shipping days", "avg_shipping_days", fmt=lambda v: f"{v:,.2f}")
else:
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
significant = [r for r in records if r.significant]
if significant:
    st.dataframe(
        [{"metric": r.metric, "direction": r.direction,
          "change_%": r.pct_change, "current": r.current} for r in significant],
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
