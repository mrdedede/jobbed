"""Launch the board scrape and look at what it produced.

Stage one: every board in user_info/job_boards.csv is asked what postings it
lists, and temp/jobs.csv is rewritten whole -- a run with a limit therefore
*replaces* the file with just those boards' rows rather than adding to it.
"""

import common
import streamlit as st

from job_scraper import paths
from job_scraper.main_scraper import scrape_boards

st.set_page_config(page_title="Scrape boards", layout="wide")
st.title("Scrape boards")

boards = common.load_csv(paths.BOARDS_CSV, ["company", "url"])
jobs = common.load_csv(paths.JOBS_CSV,
                       ["company", "title", "url", "place", "via", "ats"])

st.caption(f"{len(boards)} boards configured - last run "
           f"{common.file_stamp(paths.JOBS_CSV)}")

with st.form("scrape_boards"):
    left, right = st.columns([1, 2])
    limit = left.number_input("Boards to scrape (0 = all)", min_value=0,
                              max_value=max(len(boards), 1), value=0, step=1)
    render = right.checkbox(
        "Render JS listings (slow; needs playwright + chromium)"
    )
    st.warning("This rewrites temp/jobs.csv from scratch.", icon=":material/"
               "warning:")
    launched = st.form_submit_button("Scrape boards", type="primary")

if launched:
    renderer = None

    if render:
        # Imported inside the branch, as main_scraper.main does: playwright is
        # an opt-in extra and this must run on a machine with no browser.
        from job_scraper.render import render as renderer

    stats = common.run_with_log(
        "scraping boards",
        lambda log: scrape_boards(limit=int(limit), render=renderer,
                                  on_board=log),
    )

    if stats:
        columns = st.columns(3)
        columns[0].metric("Boards", stats["boards"])
        columns[1].metric("Postings", f"{stats['jobs']:,}")
        columns[2].metric("Boards that failed", stats["failed"])
        # Clearing invalidates the file caches; the button just gives the user
        # a rerun so the views below redraw from the new file.
        st.cache_data.clear()
        st.button("Reload the views below")

if jobs.empty:
    st.info("temp/jobs.csv is empty -- run a scrape.")
    st.stop()

st.subheader("What the last run found")

blank_titles = int(jobs["title"].isna().sum()
                   + (jobs["title"].astype(str).str.strip() == "").sum())

columns = st.columns(4)
columns[0].metric("Postings", f"{len(jobs):,}")
columns[1].metric("Companies", jobs["company"].nunique())
columns[2].metric("Boards with no jobs",
                  len(common.load_csv(paths.NO_JOBS_CSV,
                                      ["company", "url", "reason"])))
columns[3].metric("Postings with no title", f"{blank_titles:,}",
                  help="These carry nothing for the first filter to judge, so "
                       "a capped sample of them is fetched anyway.")

left, right = st.columns(2)

with left:
    st.subheader("Postings per company")
    st.pyplot(common.counts_chart(jobs["company"], "postings", top=20),
              use_container_width=False)

with right:
    st.subheader("Postings per ATS")
    st.pyplot(common.counts_chart(jobs["ats"], "postings"),
              use_container_width=False)

    st.subheader("Postings per strategy")
    st.pyplot(common.counts_chart(jobs["via"], "postings"),
              use_container_width=False)
