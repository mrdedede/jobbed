"""Why boards produced nothing, from temp/no_jobs.csv.

The free-text `reason` line is split back into columns by
`common.parse_reasons`; everything here is chart and table.
"""

import common
import matplotlib.pyplot as plt
import streamlit as st

from job_scraper import paths

st.set_page_config(page_title="Boards without jobs", layout="wide")
st.title("Boards that returned nothing")
st.caption(f"temp/no_jobs.csv - written {common.file_stamp(paths.NO_JOBS_CSV)}")

misses = common.load_csv(paths.NO_JOBS_CSV, ["company", "url", "reason"])

if misses.empty:
    st.info("No misses recorded. Run the board scrape first.")
    st.stop()

misses = misses.join(common.parse_reasons(misses["reason"]))

columns = st.columns(4)
columns[0].metric("Boards with no jobs", len(misses))
columns[1].metric("Companies affected", misses["company"].nunique())
columns[2].metric("Distinct causes", misses["cause"].nunique())
columns[3].metric("SPA markers found", int(misses["marker"].notna().sum()),
                  help="A React/Next/Nuxt root element: the listing exists "
                       "but arrives after the document. Try --render.")

st.caption(f"{int(misses['redirect'].notna().sum())} of these were redirected "
           "elsewhere before being read.")

left, right = st.columns(2)

with left:
    st.subheader("Why they failed")
    st.pyplot(common.counts_chart(misses["cause"], "boards"),
              use_container_width=False)

with right:
    st.subheader("Companies with the most dead boards")
    st.pyplot(common.counts_chart(misses["company"], "boards", top=15),
              use_container_width=False)

st.subheader("Most anchors, no postings")
st.caption(
    "A page full of links that yielded nothing is a URL-pattern miss, not a "
    "dead board -- these are the ones worth a new strategy."
)

anchored = misses.dropna(subset=["anchors"]).nlargest(15, "anchors")

if anchored.empty:
    st.info("No board reported an anchor count.")
else:
    st.pyplot(
        common.barh(anchored["company"] + " - " + anchored["cause"],
                    anchored["anchors"], xlabel="anchors on the page"),
        use_container_width=False,
    )

st.subheader("Anchors against scripts")
st.caption(
    "Bottom-left with many scripts is a JS-built listing (rendering would "
    "help); right-hand side is a page we could read but did not match."
)

scattered = misses.dropna(subset=["anchors", "scripts"])

if scattered.empty:
    st.info("No board reported anchor and script counts.")
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
                   color=common.GRAY, label="other causes",
                   edgecolor="white", linewidth=1.2, zorder=2)

    common.style_axes(ax, xlabel="anchors", ylabel="scripts")
    ax.grid(axis="y", color="#eceae5", linewidth=0.8)
    ax.legend(frameon=False, fontsize=8, labelcolor=common.MUTED)
    figure.tight_layout()
    st.pyplot(figure, use_container_width=False)

st.subheader("Every miss")
st.dataframe(
    misses[["company", "url", "cause", "anchors", "scripts", "marker",
            "redirect"]],
    use_container_width=True, hide_index=True,
    column_config={"url": st.column_config.LinkColumn("url")},
)
