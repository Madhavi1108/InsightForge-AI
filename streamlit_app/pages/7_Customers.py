"""Customers - RFM segmentation (Phase 24)."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database
from src.rfm import analyze_customer_rfm

st.set_page_config(page_title="Customers", layout="wide")
st.title("Customers")

db = require_database()

with st.spinner("Scoring customers..."):
    try:
        customers = analyze_customer_rfm(db)
    except Exception as exc:  # noqa: BLE001
        st.warning(str(exc))
        customers = []

if not customers:
    st.info("Not enough order history yet for RFM analysis.")
    st.stop()

segments = sorted({c.segment for c in customers})
choice = st.selectbox("Segment", ["All"] + segments)
filtered = customers if choice == "All" else [c for c in customers if c.segment == choice]

st.subheader("Segment counts")
counts: dict[str, int] = {}
for c in customers:
    counts[c.segment] = counts.get(c.segment, 0) + 1
st.bar_chart(counts)

st.subheader("Customers")
st.dataframe(
    [{"customer_id": c.customer_id, "name": c.customer_name, "segment": c.segment,
      "recency_days": c.recency_days, "frequency": c.frequency, "monetary": c.monetary,
      "r": c.r_score, "f": c.f_score, "m": c.m_score} for c in filtered[:500]],
    use_container_width=True,
)
