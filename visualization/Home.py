"""Landing page: what the last scrape produced and what the database holds.

Run the whole app with:

    streamlit run visualization/Home.py

`temp/jobs.csv` is rewritten whole by every board scrape, so its row count and
its mtime *are* the last run -- there is no run log to consult.
"""

import common
import streamlit as st

from db import db_connection
from job_scraper import paths

st.set_page_config(page_title="Jobbed", page_icon="::", layout="wide")

st.title("You've been Jobbed")
st.caption(f"last board scrape: {common.file_stamp(paths.JOBS_CSV)}")

jobs = common.load_csv(paths.JOBS_CSV,
                       ["company", "title", "url", "place", "via", "ats"])

if jobs.empty:
    st.warning("temp/jobs.csv is empty or missing -- run the board scrape.")

st.subheader("Last run")

columns = st.columns(4)
columns[0].metric("Postings fetched", f"{len(jobs):,}")
columns[1].metric("Companies", jobs["company"].nunique() if len(jobs) else 0)
columns[2].metric("ATS detected",
                  jobs["ats"].replace("", None).nunique() if len(jobs) else 0)
columns[3].metric("Boards with no jobs",
                  len(common.load_csv(paths.NO_JOBS_CSV,
                                      ["company", "url", "reason"])))

st.subheader("Database")

try:
    db_connection.create_tables()
    stored = len(db_connection.select_job_urls())
    analyses = db_connection.select_analyses()
    pending = db_connection.count_jobs_to_analyse()
except Exception as exc:  # noqa: BLE001 - the page must render without a DB
    st.error(f"database unavailable: {exc}")
else:
    columns = st.columns(3)
    columns[0].metric("Postings stored", f"{stored:,}")
    columns[1].metric("AI analyses", f"{len(analyses):,}")
    columns[2].metric("Awaiting analysis (24h)", f"{pending:,}")

if not jobs.empty:
    st.subheader("How the postings were read")
    st.caption("`via` is the strategy that found each posting on its board.")
    figure = common.counts_chart(jobs["via"], "postings")

    if figure is not None:
        st.pyplot(figure, use_container_width=False)

st.subheader("Pages")
st.page_link("pages/1_Boards_without_jobs.py",
             label="Boards that returned nothing", icon=":material/error:")
st.page_link("pages/2_Scrape_boards.py", label="Scrape boards",
             icon=":material/travel_explore:")
st.page_link("pages/3_Scrape_jobs.py", label="Scrape jobs and store",
             icon=":material/download:")
st.page_link("pages/4_AI_analysis.py", label="AI analysis",
             icon=":material/psychology:")
st.page_link("pages/5_CV_generation.py", label="Generate CVs",
             icon=":material/description:")
