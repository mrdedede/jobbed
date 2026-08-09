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

# Stage outputs, in pipeline order.
JOBS_CSV = TEMP / "jobs.csv"
FIRST_FILTERED_CSV = TEMP / "first_filtered_file.csv"
DETAILED_CSV = TEMP / "detailed_jobs.csv"
FILTERED_DETAILED_CSV = TEMP / "filtered_detailed_jobs.csv"

DB_PATH = ROOT / "db" / "joblister.db"

TEMP.mkdir(exist_ok=True)
