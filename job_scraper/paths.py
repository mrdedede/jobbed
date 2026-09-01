"""Filesystem locations for every artifact the pipeline reads or writes.

One module owns these so the stages cannot disagree about a filename. They did:
``post_scraper`` read ``temp/filtered_file.csv`` while the filter wrote
``temp/first_filtered_file.csv``, a path that never existed on disk, which
silently broke the hand-off between the two.

The ``temp`` directory is created on import, so a stage may open its output
without checking first.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

#: Scratch directory for the CSV passed between pipeline stages. These are not
#: throwaway: post_scraper resumes from its own output, and every measurement
#: in REFACTORING.md was taken from these files after the fact.
TEMP = ROOT / "temp"

USER_INFO = ROOT / "user_info"

# Inputs the user supplies (all gitignored; each has an *_example sibling).
BOARDS_CSV = USER_INFO / "job_boards.csv"
KEYWORDS_TXT = USER_INFO / "keywords.txt"
BLACKLIST_TXT = USER_INFO / "blacklist.txt"
CV_MD = USER_INFO / "my_cv.md"
CV_TEMPLATE_DOCX = USER_INFO / "CV_placeholder.docx"

# Stage outputs, in pipeline order.
JOBS_CSV = TEMP / "jobs.csv"
FIRST_FILTERED_CSV = TEMP / "first_filtered_file.csv"
DETAILED_CSV = TEMP / "detailed_jobs.csv"
FILTERED_DETAILED_CSV = TEMP / "filtered_detailed_jobs.csv"

#: Not a stage hand-off: one row per board the scrape stage got nothing from,
#: written for a human to read.
NO_JOBS_CSV = TEMP / "no_jobs.csv"

# Databases
DB = ROOT / "db"
DB_PATH = DB / "joblister.db"

# AI
AI = ROOT / "ai"
GRADE_JOB_MD = AI / "grade-job.md"
GENERATE_CV_MD = AI / "generate-cv.md"

#: Section titles and language names, one file per supported locale. The docx
#: renderer reads these; nothing else does.
L10N = ROOT / "cv_generator" / "l10n"

#: UI strings for the Streamlit dashboard, one file per supported locale.
#: Separate from L10N above: that one is CV content, this one is the app chrome.
UI_L10N = ROOT / "l10n"

TEMP.mkdir(exist_ok=True)
