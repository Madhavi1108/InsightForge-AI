"""AI Analyst - the 6 spec-named business questions (Phase 28) and
ad-hoc natural-language querying (Phase 29)."""
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import streamlit as st

from streamlit_app.common import require_database
from src.ai_analyst import AnalystAnswer, answer_question
from src.nl_to_sql import ask

st.set_page_config(page_title="AI Analyst", layout="wide")
st.title("AI Analyst")

db = require_database()
with st.container(key="ai-page-accent"):
    st.caption("Ask InsightForge about your business data - every answer is grounded in verified evidence.")

tab_analyst, tab_sql = st.tabs(["Ask the AI Analyst", "Ask an ad-hoc question (NL-to-SQL)"])


def _render_answer(answer: AnalystAnswer) -> None:
    st.caption(f"Intent: {answer.intent} | Engine: {answer.engine} | "
              f"Confidence: {answer.confidence:.2f} ({answer.confidence_band})")
    st.subheader("Summary")
    st.write(answer.summary)
    if answer.root_cause:
        st.subheader("Root Cause")
        st.write(answer.root_cause)
    if answer.impact:
        st.subheader("Impact")
        st.write(answer.impact)
    if answer.recommendation:
        st.subheader("Recommendation")
        st.write(answer.recommendation)
    if answer.evidence:
        st.subheader("Evidence")
        for note in answer.evidence:
            st.markdown(f"- {note}")


with tab_analyst:
    quick_questions = [
        "Why did revenue decrease?",
        "Which region performed worst?",
        "Which products need attention?",
        "What caused profit to decline?",
        "What are the biggest risks?",
        "Summarize this month.",
    ]
    cols = st.columns(3)
    clicked_question = None
    for i, q in enumerate(quick_questions):
        if cols[i % 3].button(q):
            clicked_question = q

    typed_question = st.text_input("Or ask your own question")
    ask_clicked = st.button("Ask", key="ask_analyst")

    question = clicked_question or (typed_question if ask_clicked else None)
    if question:
        with st.spinner("Thinking..."):
            answer = answer_question(db, question)
        _render_answer(answer)

with tab_sql:
    nl_question = st.text_input("What do you want to know?", key="nl_sql_question")
    if st.button("Run query"):
        with st.spinner("Generating and validating SQL..."):
            result = ask(db, nl_question)
        if result.sql:
            st.code(result.sql, language="sql")
        if not result.is_safe:
            st.error(result.blocked_reason or "This question could not be answered safely.")
        elif result.error:
            st.error(result.error)
        else:
            st.write(result.explanation)
            if result.rows:
                st.dataframe(result.rows, use_container_width=True)
