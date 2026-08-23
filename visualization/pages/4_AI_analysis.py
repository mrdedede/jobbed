"""Read the AI grades and launch a grading run.

Grading shells out to the `claude` CLI once per posting, serially, so a run is
minutes rather than seconds -- which is why the limit sits next to the button
and the backlog count sits above it.
"""

import common
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

from ai.main_analysis import run_analysis
from db import db_connection

#: Label -> SQLite modifier for datetime('now', ?). "All time" is an epoch,
#: not a special case: the query stays one shape.
WINDOWS = {
    "last 24 hours": "-24 hours",
    "last 7 days": "-7 days",
    "last 30 days": "-30 days",
    "all time": "-100 years",
}

COLUMNS = ["job_id", "grade", "company", "title", "place", "url", "model",
           "depth_analysis", "description", "analysed_at"]

st.set_page_config(page_title="AI analysis", layout="wide")
st.title("AI analysis")

db_connection.create_tables()
analyses = pd.DataFrame(db_connection.select_analyses(), columns=COLUMNS)

window_label = st.selectbox("Backlog window", list(WINDOWS), index=0,
                            help="How far back to look for postings the model "
                                 "has not graded yet.")
window = WINDOWS[window_label]
pending = db_connection.count_jobs_to_analyse(window)

columns = st.columns(4)
columns[0].metric("Graded postings", f"{len(analyses):,}")
columns[1].metric("Mean grade",
                  f"{analyses['grade'].mean():.0f}" if len(analyses) else "-")
columns[2].metric("Median grade",
                  f"{analyses['grade'].median():.0f}" if len(analyses) else "-")
columns[3].metric(f"Awaiting analysis ({window_label})", f"{pending:,}")

with st.form("analyse"):
    left, right = st.columns([1, 2])
    limit = left.number_input("Postings to grade (0 = the whole backlog)",
                              min_value=0, value=20, step=5)
    right.caption("One `claude` CLI call per posting, run one after another: "
                  "budget roughly a few seconds each.")
    launched = st.form_submit_button("Grade postings", type="primary",
                                     disabled=pending == 0)

if launched:
    stats = common.run_with_log(
        "grading postings",
        lambda log: run_analysis(int(limit), window, log),
    )

    if stats:
        columns = st.columns(3)
        columns[0].metric("Analysed", stats["analysed"])
        columns[1].metric("Failed", stats["failed"])
        columns[2].metric("Were due", stats["due"])
        st.button("Reload the table below")

if analyses.empty:
    st.info("No analyses stored yet.")
    st.stop()

left, right = st.columns(2)

with left:
    st.subheader("Grade distribution")
    figure, ax = plt.subplots(figsize=(7, 3.5))
    ax.hist(analyses["grade"], bins=range(0, 105, 5), color=common.BLUE)
    common.style_axes(ax, xlabel="adequation grade", ylabel="postings")
    ax.grid(axis="y", color="#eceae5", linewidth=0.8)
    figure.tight_layout()
    st.pyplot(figure, use_container_width=False)

with right:
    st.subheader("Mean grade by company")
    means = (analyses.groupby("company")["grade"].mean()
             .sort_values(ascending=False).head(15))
    st.pyplot(common.barh(means.index, means.values, xlabel="mean grade"),
              use_container_width=False)

st.subheader("Graded postings")
st.caption("Best fit first. Select a row to read the full analysis.")

table = st.dataframe(
    analyses[["grade", "company", "title", "place", "model", "analysed_at",
              "url"]],
    use_container_width=True, hide_index=True, on_select="rerun",
    selection_mode="single-row",
    column_config={"url": st.column_config.LinkColumn("url"),
                   "grade": st.column_config.ProgressColumn(
                       "grade", min_value=0, max_value=100, format="%d"),
                   "analysed_at": st.column_config.DatetimeColumn(
                       "analysed", format="DD/MM/YYYY HH:mm")},
)

selected = table.selection["rows"]

if not selected:
    st.info("Select a row above to see the analysis and the posting.")
    st.stop()

job = analyses.iloc[selected[0]]

st.divider()
st.subheader(job["title"] or "(untitled)")
st.caption(f"{job['company']} - {job['place'] or 'no place given'} - "
           f"graded {job['grade']}/100 by {job['model']} - "
           f"analysed {job['analysed_at']}")
st.link_button("Open the posting", job["url"])

st.markdown("### What the model said")
st.markdown(job["depth_analysis"])

with st.expander("Full posting description"):
    st.text(job["description"])
