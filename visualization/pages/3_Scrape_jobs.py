"""Fetch each surviving posting's own page, filter again, store the keepers.

Four steps in one button, with no first_filtered_file.csv in the middle: the
first filter runs in memory, URLs already in job_data are dropped before a
single request is paid for, and only then are pages fetched. The detail CSV
does stay on disk -- the fetch resumes from it after a crash, and the second
filter reads it.
"""

import common
import streamlit as st

from db import db_connection
from job_scraper import filters, paths
from job_scraper.post_scraper import already_done, scrape_details

st.set_page_config(page_title="Scrape jobs", layout="wide")
st.title("Scrape jobs and store them")

jobs = common.load_csv(paths.JOBS_CSV,
                       ["company", "title", "url", "place", "via", "ats"])

if jobs.empty:
    st.info("temp/jobs.csv is empty -- scrape the boards first.")
    st.stop()


@st.cache_data(show_spinner="filtering...")
def pending_rows(_stamp: float, _detail_stamp: float):
    """Postings that pass the first filter and still need work.

    Args:
        _stamp: jobs.csv mtime, so a fresh scrape invalidates this.
        _detail_stamp: detailed_jobs.csv mtime, likewise for a fetch.

    Returns:
        Tuple of (rows kept by the filter, the subset not in job_data, the
        subset of *those* whose page has not been fetched either).
    """
    kept = filters.first_filter()
    todo = kept[~kept["url"].isin(db_connection.select_job_urls())]

    # A posting can be absent from job_data because it was fetched and then
    # rejected by the second filter. With resume on those cost nothing, so
    # counting them as "would fetch" overstates the run by an order of
    # magnitude -- 1,058 against the 133 pages actually left.
    fresh = todo[~todo["url"].isin(already_done(paths.DETAILED_CSV))]

    return kept, todo, fresh


db_connection.create_tables()
kept, todo, fresh = pending_rows(
    paths.JOBS_CSV.stat().st_mtime,
    paths.DETAILED_CSV.stat().st_mtime if paths.DETAILED_CSV.exists() else 0,
)

st.subheader("Before you run")

columns = st.columns(4)
columns[0].metric("Pass the first filter", f"{len(kept):,}",
                  help="Title and URL only -- keywords, blacklist, and the "
                       "capped exemption for postings with no title.")
columns[1].metric("Already stored", f"{len(kept) - len(todo):,}")
columns[2].metric("Fetched but not stored", f"{len(todo) - len(fresh):,}",
                  help="Their page was read on an earlier run and the second "
                       "filter rejected it. With resume on they cost nothing.")
columns[3].metric("Pages this run would fetch", f"{len(fresh):,}",
                  help="One HTTP request each, with resume on. Cap it below "
                       "for a trial run.")

with st.form("scrape_jobs"):
    left, middle, right = st.columns(3)
    limit = left.number_input("Postings to fetch (0 = all pending)",
                              min_value=0, value=25, step=25)
    workers = middle.number_input("Workers", min_value=1, max_value=32,
                                  value=8, step=1)
    resume = right.checkbox("Resume (skip URLs already in detailed_jobs.csv)",
                            value=True)
    launched = st.form_submit_button("Fetch, filter and store",
                                     type="primary")

if launched:
    def workflow(log):
        """Fetch the pending pages, refilter them and insert the survivors."""
        # `fresh` under resume, so a limit of 25 means 25 pages actually read
        # rather than 25 rows the fetch then skips. Without resume the detail
        # CSV is rewritten anyway, so everything pending goes back in.
        queue = fresh if resume else todo
        rows = queue.head(int(limit)) if limit else queue
        log(f"{len(rows)} postings queued")

        # fillna first: scrape_details is written against csv.DictReader,
        # which yields "" for a missing field. A DataFrame yields nan, and
        # nan is truthy -- it sails through `found.get(k) or row.get(k)` and
        # reaches html.unescape, which only handles strings.
        stats = scrape_details(rows=rows.fillna("").to_dict("records"),
                               workers=int(workers), resume=resume,
                               on_progress=log)

        # Reads temp/detailed_jobs.csv, which the fetch just appended to, so
        # it judges everything ever fetched rather than only this batch.
        # INSERT OR IGNORE makes re-offering the old rows free.
        survivors = filters.second_filter()
        log(f"{len(survivors)} rows pass the second filter")

        # NaN out, empty string in: a missing place would otherwise be stored
        # as the float nan. And an empty batch is never handed to insert_jobs,
        # which reads the filtered CSV when given no rows -- that would store
        # a stale file instead of nothing.
        columns = ["company", "title", "description", "url", "place"]
        batch = [tuple(row) for row in
                 survivors.reindex(columns=columns).fillna("").itertuples(
                     index=False)]
        inserted, skipped = (
            db_connection.insert_jobs(rows=batch) if batch else (0, 0)
        )
        log(f"{inserted} inserted, {skipped} already known")

        return {**stats, "kept": len(survivors), "inserted": inserted,
                "skipped": skipped}

    stats = common.run_with_log("fetching postings", workflow)

    if stats:
        columns = st.columns(4)
        columns[0].metric("Pages fetched", f"{stats['pending']:,}")
        columns[1].metric("Pass the second filter", f"{stats['kept']:,}")
        columns[2].metric("Inserted", f"{stats['inserted']:,}")
        columns[3].metric("Already known", f"{stats['skipped']:,}")

        if stats["counts"]:
            st.subheader("How each page was read")
            st.caption("A spike in `none` means pages that gave nothing -- "
                       "dead links, 403s or JS shells.")
            st.pyplot(
                common.barh(list(stats["counts"]), list(stats["counts"].values()),
                            xlabel="postings"),
                use_container_width=False,
            )

        st.cache_data.clear()
        st.button("Reload the views below")

st.subheader("What survives the first filter")

left, right = st.columns(2)

with left:
    st.pyplot(
        common.barh(["dropped", "kept"], [len(jobs) - len(kept), len(kept)],
                    xlabel="postings"),
        use_container_width=False,
    )

with right:
    st.caption("Companies contributing the most pages still to fetch")
    figure = common.counts_chart(fresh["company"], "postings", top=20)

    if figure is None:
        st.info("Nothing left to fetch -- every filtered posting has been "
                "read already.")
    else:
        st.pyplot(figure, use_container_width=False)

st.subheader("Queued postings")
st.dataframe(fresh[["company", "title", "place", "ats", "url"]],
             use_container_width=True, hide_index=True,
             column_config={"url": st.column_config.LinkColumn("url")})
