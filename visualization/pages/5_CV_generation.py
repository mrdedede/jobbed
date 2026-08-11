"""Write a tailored CV for one graded posting, and read the ones already written.

Generation is one Sonnet call for one posting -- slower and dearer than the
Haiku grading on page 4, which is why it is a per-row button here rather than a
batch run.
"""

import json

import common
import pandas as pd
import streamlit as st

from ai import cv_generation
from db import db_connection

ELIGIBLE_COLUMNS = ["job_id", "grade", "company", "title", "place", "url"]
CV_COLUMNS = ["cv_id", "title", "company", "grade", "locale", "cv"]

GRADE_COLUMN = st.column_config.ProgressColumn("grade", min_value=0,
                                               max_value=100, format="%d")

st.set_page_config(page_title="CV generation", layout="wide")
st.title("CV generation")

db_connection.create_tables()

min_grade = st.slider("Minimum grade", 0, 100, 70,
                      help="Postings graded below this are not worth a "
                           "tailored CV, so they stay out of the list.")

eligible = pd.DataFrame(db_connection.select_jobs_for_cv(min_grade),
                        columns=ELIGIBLE_COLUMNS)
generated = pd.DataFrame(db_connection.select_generated_cvs(),
                         columns=CV_COLUMNS)

columns = st.columns(2)
columns[0].metric(f"Eligible postings (grade >= {min_grade})",
                  f"{len(eligible):,}")
columns[1].metric("CVs generated", f"{len(generated):,}")

st.subheader("Postings without a CV")
st.caption("Best fit first. Select one, then generate.")

table = st.dataframe(
    eligible[["grade", "company", "title", "place", "url"]],
    use_container_width=True, hide_index=True, on_select="rerun",
    selection_mode="single-row",
    column_config={"url": st.column_config.LinkColumn("url"),
                   "grade": GRADE_COLUMN},
)

selected = table.selection["rows"]
job = eligible.iloc[selected[0]] if selected else None

st.caption("One Sonnet call: budget around half a minute.")
launched = st.button(
    f"Generate a CV for {job['title']}" if job is not None
    else "Generate a CV (select a posting first)",
    type="primary", disabled=job is None,
)

if launched:
    def workflow(log):
        """Generate and store the CV for the selected posting."""
        log(f"job {job['job_id']}: {job['company']} - {job['title']}")
        locale = cv_generation.generate_cv(int(job["job_id"]))
        log(f"stored, written in {locale}")

        return {"locale": locale}

    stats = common.run_with_log("generating the CV", workflow)

    if stats:
        st.success(f"CV stored, written in {stats['locale']}.")
        st.button("Reload the lists")

st.divider()
st.subheader("Generated CVs")

if generated.empty:
    st.info("No CVs generated yet.")
    st.stop()

st.caption("Newest first. Select one to read it.")

cv_table = st.dataframe(
    generated[["title", "grade"]], use_container_width=True, hide_index=True,
    on_select="rerun", selection_mode="single-row",
    column_config={"grade": GRADE_COLUMN},
)

chosen = cv_table.selection["rows"]

if not chosen:
    st.info("Select a CV above to read it.")
    st.stop()

row = generated.iloc[chosen[0]]
cv = json.loads(row["cv"])

st.divider()
st.subheader(row["title"] or "(untitled)")
st.caption(f"{row['company']} - graded {row['grade']}/100 - "
           f"written in {row['locale']}")

# Every key below is guaranteed by CV_SCHEMA in ai/cv_generation.py: the CLI
# enforces the shape, so there is nothing here to defend against.
st.markdown("### Introduction")
st.markdown(cv["cv_introduction"])

st.markdown("### Profile")
st.markdown(cv["profile_text"])

st.markdown("### Skills")

for group in cv["skills"]:
    st.markdown(f"**{group['competence']}**: {', '.join(group['skills'])}")

st.markdown("### Experience")

for experience in cv["experiences"]:
    st.markdown(f"**{experience['role']}** - {experience['company']}, "
                f"{experience['location']} "
                f"({experience['start_date']} - {experience['end_date']})")
    st.markdown("\n".join(f"- {bullet}" for bullet in experience["bullets"]))

st.markdown("### Education")

for education in cv["education"]:
    st.markdown(f"**{education['diploma']}** ({education['degree']}) - "
                f"{education['institution']}, {education['location']} "
                f"- {education['period']}")
    st.markdown(education["details"])

with st.expander("Raw JSON"):
    st.json(cv)
