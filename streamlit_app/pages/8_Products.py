"""Products - product intelligence flags and health score (Phase 24)."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database
from src.product_intelligence import analyze_product_intelligence

st.set_page_config(page_title="Products", layout="wide")
st.title("Products")

db = require_database()

with st.spinner("Analyzing products..."):
    try:
        products = analyze_product_intelligence(db)
    except Exception as exc:  # noqa: BLE001
        st.warning(str(exc))
        products = []

if not products:
    st.info("Not enough sales history yet for product intelligence.")
    st.stop()

FLAGS = {
    "Star": lambda p: p.is_star,
    "Declining": lambda p: p.is_declining,
    "High return": lambda p: p.is_high_return,
    "Low margin": lambda p: p.is_low_margin,
    "Slow moving": lambda p: p.is_slow_moving,
}
choice = st.selectbox("Flag", ["All"] + list(FLAGS))
filtered = products if choice == "All" else [p for p in products if FLAGS[choice](p)]
filtered = sorted(filtered, key=lambda p: p.health_score)

st.dataframe(
    [{"product": p.product_name, "category": p.category, "revenue": p.revenue,
      "margin_%": p.margin_pct, "return_rate_%": p.return_rate_pct,
      "growth_%": p.revenue_growth_pct, "health_score": p.health_score,
      "star": p.is_star, "declining": p.is_declining, "high_return": p.is_high_return,
      "low_margin": p.is_low_margin, "slow_moving": p.is_slow_moving} for p in filtered[:500]],
    use_container_width=True,
)
