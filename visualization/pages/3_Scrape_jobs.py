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
from i18n import t
from job_scraper import filters, paths
from job_scraper.post_scraper import already_done, scrape_details

st.set_page_config(page_title=t("scrape_jobs.page_title"), layout="wide")
common.locale_selector()
st.title(t("scrape_jobs.title"))

jobs = common.load_csv(paths.JOBS_CSV,
                       ["company", "title", "url", "place", "via", "ats"])

if jobs.empty:
    st.info(t("scrape_jobs.info.empty"))
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

st.subheader(t("scrape_jobs.subheader.before_run"))

columns = st.columns(4)
columns[0].metric(t("scrape_jobs.metric.pass_first_filter"), f"{len(kept):,}",
                  help=t("scrape_jobs.help.pass_first_filter"))
columns[1].metric(t("scrape_jobs.metric.already_stored"),
                  f"{len(kept) - len(todo):,}")
columns[2].metric(t("scrape_jobs.metric.fetched_not_stored"),
                  f"{len(todo) - len(fresh):,}",
                  help=t("scrape_jobs.help.fetched_not_stored"))
columns[3].metric(t("scrape_jobs.metric.pages_to_fetch"), f"{len(fresh):,}",
                  help=t("scrape_jobs.help.pages_to_fetch"))

with st.form("scrape_jobs"):
    left, middle, right = st.columns(3)
    limit = left.number_input(t("scrape_jobs.form.limit"),
                              min_value=0, value=25, step=25)
    workers = middle.number_input(t("scrape_jobs.form.workers"), min_value=1,
                                  max_value=32, value=8, step=1)
    resume = right.checkbox(t("scrape_jobs.form.resume"), value=True)
    launched = st.form_submit_button(t("scrape_jobs.form.submit"),
                                     type="primary")

if launched:
    def workflow(log):
        """Fetch the pending pages, refilter them and insert the survivors."""
        # `fresh` under resume, so a limit of 25 means 25 pages actually read
        # rather than 25 rows the fetch then skips. Without resume the detail
        # CSV is rewritten anyway, so everything pending goes back in.
        queue = fresh if resume else todo
        rows = queue.head(int(limit)) if limit else queue
        log(t("scrape_jobs.log.queued", count=len(rows)))

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
        log(t("scrape_jobs.log.second_filter", count=len(survivors)))

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
        log(t("scrape_jobs.log.inserted", inserted=inserted, skipped=skipped))

        return {**stats, "kept": len(survivors), "inserted": inserted,
                "skipped": skipped}

    stats = common.run_with_log(t("scrape_jobs.status.fetching"), workflow)

    if stats:
        columns = st.columns(4)
        columns[0].metric(t("scrape_jobs.metric.pages_fetched"),
                          f"{stats['pending']:,}")
        columns[1].metric(t("scrape_jobs.metric.pass_second_filter"),
                          f"{stats['kept']:,}")
        columns[2].metric(t("scrape_jobs.metric.inserted"),
                          f"{stats['inserted']:,}")
        columns[3].metric(t("scrape_jobs.metric.already_known"),
                          f"{stats['skipped']:,}")

        if stats["counts"]:
            st.subheader(t("scrape_jobs.subheader.how_read"))
            st.caption(t("scrape_jobs.caption.how_read"))
            st.pyplot(
                common.barh(list(stats["counts"]), list(stats["counts"].values()),
                            xlabel=t("common.axis.postings")),
                use_container_width=False,
            )

        st.cache_data.clear()
        st.button(t("scrape_jobs.button.reload"))

st.subheader(t("scrape_jobs.subheader.survives_first_filter"))

left, right = st.columns(2)

with left:
    st.pyplot(
        common.barh([t("scrape_jobs.bar.dropped"), t("scrape_jobs.bar.kept")],
                    [len(jobs) - len(kept), len(kept)],
                    xlabel=t("common.axis.postings")),
        use_container_width=False,
    )

with right:
    st.caption(t("scrape_jobs.caption.top_companies"))
    figure = common.counts_chart(fresh["company"], t("common.axis.postings"),
                                 top=20)

    if figure is None:
        st.info(t("scrape_jobs.info.nothing_left"))
    else:
        st.pyplot(figure, use_container_width=False)

st.subheader(t("scrape_jobs.subheader.queued"))
st.dataframe(fresh[["company", "title", "place", "ats", "url"]],
             use_container_width=True, hide_index=True,
             column_config={"url": st.column_config.LinkColumn(
                 t("common.column.url"))})
