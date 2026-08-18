"""Landing page: what the last scrape produced and what the database holds.

Run the whole app with:

    streamlit run visualization/Home.py

`temp/jobs.csv` is rewritten whole by every board scrape, so its row count and
its mtime *are* the last run -- there is no run log to consult.
"""

import common
import streamlit as st

from db import db_connection
from i18n import t
from job_scraper import paths

st.set_page_config(page_title=t("home.page_title"), page_icon="::",
                   layout="wide")
common.locale_selector()

st.title(t("home.title"))
st.caption(t("home.caption.last_scrape",
            timestamp=common.file_stamp(paths.JOBS_CSV)))

jobs = common.load_csv(paths.JOBS_CSV,
                       ["company", "title", "url", "place", "via", "ats"])

if jobs.empty:
    st.warning(t("home.warning.no_jobs_csv"))

st.subheader(t("home.subheader.last_run"))

columns = st.columns(4)
columns[0].metric(t("home.metric.postings_fetched"), f"{len(jobs):,}")
columns[1].metric(t("home.metric.companies"),
                  jobs["company"].nunique() if len(jobs) else 0)
columns[2].metric(t("home.metric.ats_detected"),
                  jobs["ats"].replace("", None).nunique() if len(jobs) else 0)
columns[3].metric(t("home.metric.boards_with_no_jobs"),
                  len(common.load_csv(paths.NO_JOBS_CSV,
                                      ["company", "url", "reason"])))

st.subheader(t("home.subheader.database"))

try:
    db_connection.create_tables()
    stored = len(db_connection.select_job_urls())
    analyses = db_connection.select_analyses()
    pending = db_connection.count_jobs_to_analyse()
except Exception as exc:  # noqa: BLE001 - the page must render without a DB
    st.error(t("home.error.db_unavailable", error=exc))
else:
    columns = st.columns(3)
    columns[0].metric(t("home.metric.postings_stored"), f"{stored:,}")
    columns[1].metric(t("home.metric.ai_analyses"), f"{len(analyses):,}")
    columns[2].metric(t("home.metric.awaiting_analysis_24h"), f"{pending:,}")

if not jobs.empty:
    st.subheader(t("home.subheader.how_read"))
    st.caption(t("home.caption.via"))
    figure = common.counts_chart(jobs["via"], t("common.axis.postings"))

    if figure is not None:
        st.pyplot(figure, use_container_width=False)

st.subheader(t("home.subheader.pages"))
st.page_link("pages/1_Boards_without_jobs.py",
             label=t("home.link.boards_without_jobs"),
             icon=":material/error:")
st.page_link("pages/2_Scrape_boards.py", label=t("home.link.scrape_boards"),
             icon=":material/travel_explore:")
st.page_link("pages/3_Scrape_jobs.py", label=t("home.link.scrape_jobs"),
             icon=":material/download:")
st.page_link("pages/4_AI_analysis.py", label=t("home.link.ai_analysis"),
             icon=":material/psychology:")
st.page_link("pages/5_CV_generation.py", label=t("home.link.cv_generation"),
             icon=":material/description:")
