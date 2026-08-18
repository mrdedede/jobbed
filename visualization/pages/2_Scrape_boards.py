"""Launch the board scrape and look at what it produced.

Stage one: every board in user_info/job_boards.csv is asked what postings it
lists, and temp/jobs.csv is rewritten whole -- a run with a limit therefore
*replaces* the file with just those boards' rows rather than adding to it.
"""

import common
import streamlit as st

from i18n import t
from job_scraper import paths
from job_scraper.main_scraper import scrape_boards

st.set_page_config(page_title=t("scrape_boards.page_title"), layout="wide")
common.locale_selector()
st.title(t("scrape_boards.title"))

boards = common.load_csv(paths.BOARDS_CSV, ["company", "url"])
jobs = common.load_csv(paths.JOBS_CSV,
                       ["company", "title", "url", "place", "via", "ats"])

st.caption(t("scrape_boards.caption.configured", count=len(boards),
            timestamp=common.file_stamp(paths.JOBS_CSV)))

with st.form("scrape_boards"):
    left, right = st.columns([1, 2])
    limit = left.number_input(t("scrape_boards.form.limit"), min_value=0,
                              max_value=max(len(boards), 1), value=0, step=1)
    render = right.checkbox(t("scrape_boards.form.render"))
    st.warning(t("scrape_boards.warning.rewrite"), icon=":material/"
               "warning:")
    launched = st.form_submit_button(t("scrape_boards.form.submit"),
                                     type="primary")

if launched:
    renderer = None

    if render:
        # Imported inside the branch, as main_scraper.main does: playwright is
        # an opt-in extra and this must run on a machine with no browser.
        from job_scraper.render import render as renderer

    stats = common.run_with_log(
        t("scrape_boards.status.scraping"),
        lambda log: scrape_boards(limit=int(limit), render=renderer,
                                  on_board=log),
    )

    if stats:
        columns = st.columns(3)
        columns[0].metric(t("scrape_boards.metric.boards"), stats["boards"])
        columns[1].metric(t("scrape_boards.metric.postings"),
                          f"{stats['jobs']:,}")
        columns[2].metric(t("scrape_boards.metric.boards_failed"),
                          stats["failed"])
        # Clearing invalidates the file caches; the button just gives the user
        # a rerun so the views below redraw from the new file.
        st.cache_data.clear()
        st.button(t("scrape_boards.button.reload"))

if jobs.empty:
    st.info(t("scrape_boards.info.empty"))
    st.stop()

st.subheader(t("scrape_boards.subheader.last_run_found"))

blank_titles = int(jobs["title"].isna().sum()
                   + (jobs["title"].astype(str).str.strip() == "").sum())

columns = st.columns(4)
columns[0].metric(t("scrape_boards.metric.postings"), f"{len(jobs):,}")
columns[1].metric(t("home.metric.companies"), jobs["company"].nunique())
columns[2].metric(t("home.metric.boards_with_no_jobs"),
                  len(common.load_csv(paths.NO_JOBS_CSV,
                                      ["company", "url", "reason"])))
columns[3].metric(t("scrape_boards.metric.postings_no_title"),
                  f"{blank_titles:,}",
                  help=t("scrape_boards.help.postings_no_title"))

left, right = st.columns(2)

with left:
    st.subheader(t("scrape_boards.subheader.per_company"))
    st.pyplot(common.counts_chart(jobs["company"], t("common.axis.postings"),
                                  top=20),
              use_container_width=False)

with right:
    st.subheader(t("scrape_boards.subheader.per_ats"))
    st.pyplot(common.counts_chart(jobs["ats"], t("common.axis.postings")),
              use_container_width=False)

    st.subheader(t("scrape_boards.subheader.per_strategy"))
    st.pyplot(common.counts_chart(jobs["via"], t("common.axis.postings")),
              use_container_width=False)
