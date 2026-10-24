"""Filesystem locations for every artifact the pipeline reads or writes.

One module owns these so the stages cannot disagree about a filename. They did:
``scrape_postings`` read ``temp/filtered_file.csv`` while the filter wrote
``temp/first_filtered_file.csv``, a path that never existed on disk, which
silently broke the hand-off between the two.

Deployment locations come from the environment (``JOBBED_DATA_DIR`` for the
hand-off directory, ``JOBBED_BOARDS_CSV`` and
``FRANCE_TRAVAIL_CREDENTIALS_FILE`` for inputs), defaulting to the repo layout.
Nothing is created on import: the stages that write create their own parent
directory, so a missing volume mount fails loudly instead of landing on the
container's disk.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Directory for the CSV passed between pipeline stages (the PVC mount in
#: Kubernetes). These are not throwaway: scrape_postings resumes from its own
#: output, and every performance measurement was taken from these files.
TEMP = Path(os.environ.get("JOBBED_DATA_DIR", ROOT / "temp"))

USER_INFO = ROOT / "user_info"

# Inputs the user supplies (all gitignored; each has an *_example sibling).
BOARDS_CSV = Path(os.environ.get("JOBBED_BOARDS_CSV",
                                 USER_INFO / "job_boards.csv"))
CV_TEMPLATE_DOCX = USER_INFO / "CV_placeholder.docx"
FRANCE_TRAVAIL_CREDENTIALS = Path(os.environ.get(
    "FRANCE_TRAVAIL_CREDENTIALS_FILE",
    USER_INFO / "france_travail_credentials.json"))

# Stage outputs, in pipeline order.
JOBS_CSV = TEMP / "jobs.csv"
FIRST_FILTERED_CSV = TEMP / "first_filtered_file.csv"
DETAILED_CSV = TEMP / "detailed_jobs.csv"
FILTERED_DETAILED_CSV = TEMP / "filtered_detailed_jobs.csv"

#: Not a stage hand-off: one row per board the scrape stage got nothing from,
#: written for a human to read.
NO_JOBS_CSV = TEMP / "no_jobs.csv"

#: RenderBoards' two outputs: postings found by a browser (merged into the
#: next ingest) and the boards that still gave nothing. Separate from
#: NO_JOBS_CSV, which is RenderBoards' own input.
JOBS_RENDERED_CSV = TEMP / "jobs_rendered.csv"
NO_JOBS_RENDERED_CSV = TEMP / "no_jobs_rendered.csv"

#: One row per posting the ingest could not store (url, company, ats, reason).
#: RenderPostings reads the `parse-miss` ones.
FAILED_POSTINGS_CSV = TEMP / "failed_postings.csv"

# AI
AI = ROOT / "ai"
GRADE_JOB_MD = AI / "grade-job.md"
GENERATE_CV_MD = AI / "generate-cv.md"
SUGGEST_TERMS_MD = AI / "suggest-terms.md"
COVER_LETTER_MD = AI / "write-cover-letter.md"

#: Section titles and language names, one file per supported locale. The docx
#: renderer reads these; nothing else does.
L10N = ROOT / "cv_generator" / "l10n"
