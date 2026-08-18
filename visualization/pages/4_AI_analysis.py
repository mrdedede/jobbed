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
from i18n import t

#: Internal keys -> SQLite modifier for datetime('now', ?). "All time" is an
#: epoch, not a special case: the query stays one shape. The dropdown label
#: shown for each key comes from the l10n catalog, not from this dict.
WINDOWS = {
    "last_24_hours": "-24 hours",
    "last_7_days": "-7 days",
    "last_30_days": "-30 days",
    "all_time": "-100 years",
}

COLUMNS = ["job_id", "grade", "company", "title", "place", "url", "model",
           "depth_analysis", "description", "stored_at"]

st.set_page_config(page_title=t("analysis.page_title"), layout="wide")
common.locale_selector()
st.title(t("analysis.title"))

db_connection.create_tables()
analyses = pd.DataFrame(db_connection.select_analyses(), columns=COLUMNS)

window_key = st.selectbox(t("analysis.form.window"), list(WINDOWS), index=0,
                          format_func=lambda key: t(f"analysis.window.{key}"),
                          help=t("analysis.help.window"))
window_label = t(f"analysis.window.{window_key}")
window = WINDOWS[window_key]
pending = db_connection.count_jobs_to_analyse(window)

columns = st.columns(4)
columns[0].metric(t("analysis.metric.graded"), f"{len(analyses):,}")
columns[1].metric(t("analysis.metric.mean_grade"),
                  f"{analyses['grade'].mean():.0f}" if len(analyses)
                  else t("analysis.metric.no_value"))
columns[2].metric(t("analysis.metric.median_grade"),
                  f"{analyses['grade'].median():.0f}" if len(analyses)
                  else t("analysis.metric.no_value"))
columns[3].metric(t("analysis.metric.awaiting", window=window_label),
                  f"{pending:,}")

with st.form("analyse"):
    left, right = st.columns([1, 2])
    limit = left.number_input(t("analysis.form.limit"),
                              min_value=0, value=20, step=5)
    right.caption(t("analysis.caption.budget"))
    launched = st.form_submit_button(t("analysis.form.submit"),
                                     type="primary", disabled=pending == 0)

if launched:
    stats = common.run_with_log(
        t("analysis.status.grading"),
        lambda log: run_analysis(int(limit), window, log),
    )

    if stats:
        columns = st.columns(3)
        columns[0].metric(t("analysis.metric.analysed"), stats["analysed"])
        columns[1].metric(t("analysis.metric.failed"), stats["failed"])
        columns[2].metric(t("analysis.metric.were_due"), stats["due"])
        st.button(t("analysis.button.reload"))

if analyses.empty:
    st.info(t("analysis.info.none_stored"))
    st.stop()

left, right = st.columns(2)

with left:
    st.subheader(t("analysis.subheader.grade_distribution"))
    figure, ax = plt.subplots(figsize=(7, 3.5))
    ax.hist(analyses["grade"], bins=range(0, 105, 5), color=common.BLUE)
    common.style_axes(ax, xlabel=t("analysis.axis.grade"),
                      ylabel=t("common.axis.postings"))
    ax.grid(axis="y", color="#eceae5", linewidth=0.8)
    figure.tight_layout()
    st.pyplot(figure, use_container_width=False)

with right:
    st.subheader(t("analysis.subheader.mean_by_company"))
    means = (analyses.groupby("company")["grade"].mean()
             .sort_values(ascending=False).head(15))
    st.pyplot(common.barh(means.index, means.values,
                          xlabel=t("analysis.axis.mean_grade")),
              use_container_width=False)

st.subheader(t("analysis.subheader.graded_postings"))
st.caption(t("analysis.caption.select_row"))

table = st.dataframe(
    analyses[["grade", "company", "title", "place", "model", "url"]],
    use_container_width=True, hide_index=True, on_select="rerun",
    selection_mode="single-row",
    column_config={"url": st.column_config.LinkColumn(t("common.column.url")),
                   "grade": st.column_config.ProgressColumn(
                       t("common.column.grade"), min_value=0, max_value=100,
                       format="%d")},
)

selected = table.selection["rows"]

if not selected:
    st.info(t("analysis.info.select_row"))
    st.stop()

job = analyses.iloc[selected[0]]

st.divider()
st.subheader(job["title"] or t("analysis.untitled"))
st.caption(t("analysis.caption.job_summary", company=job["company"],
            place=job["place"] or t("analysis.no_place"), grade=job["grade"],
            model=job["model"], stored_at=job["stored_at"]))
st.link_button(t("analysis.button.open_posting"), job["url"])

st.markdown(f"### {t('analysis.subheader.model_said')}")
st.markdown(job["depth_analysis"])

with st.expander(t("analysis.expander.full_description")):
    st.text(job["description"])
