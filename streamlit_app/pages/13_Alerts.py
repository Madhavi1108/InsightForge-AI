"""Alerts - severity-routed alert review (Phase 34's routing rules,
display only). This page never sends email: it calls the pure
src.alerts.build_alert() over the current recommendations, never
dispatch_alerts()/send_alert_email(), so simply viewing this page can
never resend a real alert."""
import sys
from collections import defaultdict
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database
from src.alerts import build_alert
from src.recommendations import generate_recommendations

st.set_page_config(page_title="Alerts", layout="wide")
st.title("Alerts")
st.caption("Review only - no emails are sent from this page.")

db = require_database()

try:
    recs = generate_recommendations(db)
except Exception as exc:  # noqa: BLE001 - never crash the page on a downstream failure
    st.warning(f"Could not load recommendations: {exc}")
    recs = []

if not recs:
    st.caption("No active alerts.")
    st.stop()

alerts = [build_alert(r) for r in recs]
by_route: dict[str, list] = defaultdict(list)
for alert in alerts:
    by_route[alert.route].append(alert)

route_labels = {
    "dashboard": "Dashboard (LOW)", "streamlit": "Streamlit (MEDIUM)",
    "email": "Email (HIGH)", "immediate_email": "Immediate email (CRITICAL)",
}
for route, title in route_labels.items():
    group = by_route.get(route, [])
    st.subheader(f"{title} - {len(group)}")
    if group:
        st.dataframe(
            [{"severity": a.severity, "metric": a.metric, "period_key": a.period_key,
              "issue": a.issue, "evidence": a.evidence, "impact": a.impact,
              "recommendation": a.recommendation} for a in group],
            use_container_width=True,
        )
    else:
        st.caption("None.")
