"""Why boards produced nothing, from temp/no_jobs.csv.

The free-text `reason` line is split back into columns by
`common.parse_reasons`; everything here is chart and table.
"""

import common
import matplotlib.pyplot as plt
import streamlit as st

from i18n import t
from job_scraper import paths

st.set_page_config(page_title=t("boards_without_jobs.page_title"),
                   layout="wide")
common.locale_selector()
st.title(t("boards_without_jobs.title"))
st.caption(t("boards_without_jobs.caption.written",
            timestamp=common.file_stamp(paths.NO_JOBS_CSV)))

misses = common.load_csv(paths.NO_JOBS_CSV, ["company", "url", "reason"])

if misses.empty:
    st.info(t("boards_without_jobs.info.none_recorded"))
    st.stop()

misses = misses.join(common.parse_reasons(misses["reason"]))

columns = st.columns(4)
columns[0].metric(t("boards_without_jobs.metric.boards_with_no_jobs"),
                  len(misses))
columns[1].metric(t("boards_without_jobs.metric.companies_affected"),
                  misses["company"].nunique())
columns[2].metric(t("boards_without_jobs.metric.distinct_causes"),
                  misses["cause"].nunique())
columns[3].metric(t("boards_without_jobs.metric.spa_markers"),
                  int(misses["marker"].notna().sum()),
                  help=t("boards_without_jobs.help.spa_markers"))

st.caption(t("boards_without_jobs.caption.redirected",
            count=int(misses["redirect"].notna().sum())))

left, right = st.columns(2)

with left:
    st.subheader(t("boards_without_jobs.subheader.why_failed"))
    st.pyplot(common.counts_chart(misses["cause"], t("common.axis.boards")),
              use_container_width=False)

with right:
    st.subheader(t("boards_without_jobs.subheader.most_dead_boards"))
    st.pyplot(common.counts_chart(misses["company"], t("common.axis.boards"),
                                  top=15),
              use_container_width=False)

st.subheader(t("boards_without_jobs.subheader.most_anchors"))
st.caption(t("boards_without_jobs.caption.most_anchors"))

anchored = misses.dropna(subset=["anchors"]).nlargest(15, "anchors")

if anchored.empty:
    st.info(t("boards_without_jobs.info.no_anchor_count"))
else:
    st.pyplot(
        common.barh(anchored["company"] + " - " + anchored["cause"],
                    anchored["anchors"],
                    xlabel=t("boards_without_jobs.axis.anchors_on_page")),
        use_container_width=False,
    )

st.subheader(t("boards_without_jobs.subheader.anchors_vs_scripts"))
st.caption(t("boards_without_jobs.caption.anchors_vs_scripts"))

scattered = misses.dropna(subset=["anchors", "scripts"])

if scattered.empty:
    st.info(t("boards_without_jobs.info.no_anchor_script_count"))
else:
    # Three colours at most: the palette's all-pairs cap. Everything past the
    # three commonest causes is grey rather than a fourth hue.
    top_causes = list(scattered["cause"].value_counts().head(3).index)
    figure, ax = plt.subplots(figsize=(7, 4.5))

    for cause, colour in zip(top_causes, common.SERIES):
        group = scattered[scattered["cause"] == cause]
        ax.scatter(group["anchors"], group["scripts"], s=60, color=colour,
                   label=cause, edgecolor="white", linewidth=1.2, zorder=3)

    others = scattered[~scattered["cause"].isin(top_causes)]

    if not others.empty:
        ax.scatter(others["anchors"], others["scripts"], s=60,
                   color=common.GRAY,
                   label=t("boards_without_jobs.legend.other_causes"),
                   edgecolor="white", linewidth=1.2, zorder=2)

    common.style_axes(ax, xlabel=t("boards_without_jobs.axis.anchors"),
                      ylabel=t("boards_without_jobs.axis.scripts"))
    ax.grid(axis="y", color="#eceae5", linewidth=0.8)
    ax.legend(frameon=False, fontsize=8, labelcolor=common.MUTED)
    figure.tight_layout()
    st.pyplot(figure, use_container_width=False)

st.subheader(t("boards_without_jobs.subheader.every_miss"))
st.dataframe(
    misses[["company", "url", "cause", "anchors", "scripts", "marker",
            "redirect"]],
    use_container_width=True, hide_index=True,
    column_config={"url": st.column_config.LinkColumn(t("common.column.url"))},
)
