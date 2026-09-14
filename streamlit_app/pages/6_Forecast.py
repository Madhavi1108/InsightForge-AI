"""Forecast - Holt's linear trend forecast + accuracy evaluation (Phase 25)."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database
from src.forecasting import FORECAST_METRICS, forecast_metric

st.set_page_config(page_title="Forecast", layout="wide")
st.title("Forecast")

db = require_database()

metric = st.selectbox("Metric", FORECAST_METRICS)
horizon = st.radio("Horizon (days)", [7, 30], horizontal=True)

with st.spinner("Forecasting..."):
    try:
        points = forecast_metric(db, metric, horizon=horizon)
    except Exception as exc:  # noqa: BLE001
        st.warning(str(exc))
        points = []

if not points:
    st.info("Not enough history yet to forecast this metric.")
    st.stop()

st.line_chart(
    {"date": [p.forecast_date for p in points],
     "forecast": [p.forecast_value for p in points],
     "lower": [p.lower_bound for p in points],
     "upper": [p.upper_bound for p in points]},
    x="date",
)

last = points[-1]
st.caption(
    f"Model: {last.model}. "
    f"MAE={last.mae if last.mae is not None else '-'} "
    f"RMSE={last.rmse if last.rmse is not None else '-'} "
    f"MAPE={last.mape if last.mape is not None else '-'}"
)
