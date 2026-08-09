"""Keyword and blacklist filtering between the two scraping stages.

Two passes with different information available:

* `first_filter` runs on titles and URLs alone, before any posting has been
  fetched. Every row it keeps costs one HTTP request at the next stage, so
  what it drops is the single biggest lever on the run's cost.
* `second_filter` runs once each posting's own description is in hand, and can
  afford a much higher bar.

This lived in `user_info/`, the directory of gitignored user data. It is code,
and it lives with the code now; `user_info/` holds only what the user writes.
"""

import re
from typing import List

import pandas as pd

from job_scraper import paths
from job_scraper.urls import title_from_url

# Paths are read off `paths` at call time, never captured into module
# constants or default arguments. Binding them at import means a caller that
# repoints `paths` -- a test, a scheduled run against a different data
# directory -- is silently ignored, which is a bug that only shows up as
# "nothing happened".

# Thresholds, measured against temp/jobs.csv (19,793 rows) and
# temp/detailed_jobs.csv on 2026-08-09 rather than guessed.
#
# first_filter, counting keyword hits across title + URL, after
# title_from_url stopped returning "" for id-tailed paths:
#
#   MIN   kept by keywords   + unjudgeable   total
#     1              1,518           1,956   3,474
#     2              1,040           1,956   2,996      <- shipped
#     3                394           1,956   2,350
#
# 2 is the knee. At 3 the keyword arm collapses to 394 rows and the
# unjudgeable exemption would carry the filter again, which is the exact
# failure this filter just came out of.
MIN_KEYWORD_MATCHES = 2

# second_filter counts across title + URL + description, so the bar is far
# higher: a real posting body mentions its stack repeatedly, and 5 separates
# those from a page that merely lists the keyword in a nav menu.
MIN_KEYWORD_MATCHES_DETAILED = 5

# Rows that reached the detail stage with no title or no URL have less text to
# match, so they are judged on the description alone at a lower bar.
MIN_KEYWORD_MATCHES_INCOMPLETE = 3

#: Per-company ceiling on rows kept purely because nothing about them can be
#: judged (see `first_filter`). Without it one board decides the whole detail
#: budget: AXA alone files 1,891 postings at bare `/jobs/{id}` URLs, an order
#: of magnitude more than every other such board combined.
MAX_UNJUDGEABLE_PER_COMPANY = 300

#: Fixed so two runs over the same input keep the same rows. The sample is
#: random rather than the first N because sitemap order is arbitrary but
#: stable -- taking the head would mean the same postings on a large board are
#: looked at every single run, and the rest never are.
EXEMPTION_SEED = 0


def get_keywords() -> List[str]:
    """Load and normalize keywords from file.

    Reads keywords.txt, strips whitespace, drops empty lines, and lowercases
    all entries for case-insensitive matching.

    Returns:
        List of normalized keyword strings (lowercased, stripped).
    """
    return [
        line.strip().lower()
        for line in paths.KEYWORDS_TXT.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def get_blacklist() -> List[str]:
    """Load and normalize blacklist words from file.

    Reads blacklist.txt, strips whitespace, drops empty lines, and lowercases
    all entries for case-insensitive matching.

    Returns:
        List of normalized blacklist strings (lowercased, stripped).
    """
    return [
        line.strip().lower()
        for line in paths.BLACKLIST_TXT.read_text(
            encoding="utf-8"
        ).splitlines()
        if line.strip()
    ]


def _build_word_pattern(words: List[str]) -> str:
    """Build a regex pattern for whole-word/phrase matching.

    Escapes special regex characters and joins words with | operator for
    case-insensitive alternation.

    Args:
        words: List of keywords or blacklist words to match.

    Returns:
        A regex pattern matching any of the words as whole words/phrases.
    """
    escaped = [re.escape(word) for word in words]
    return r"\b(?:" + "|".join(escaped) + r")\b"


def _row_matches_any(jobs: pd.DataFrame, words: List[str],
                     columns: tuple = ("title", "url")) -> pd.Series:
    """Check if each row's specified columns contain any of the given words.

    Args:
        jobs: DataFrame with specified columns.
        words: List of words to match.
        columns: Tuple of column names to check (default: title, url).

    Returns:
        Boolean Series indicating rows with at least one match.
    """
    if not words:
        return pd.Series(False, index=jobs.index)

    pattern = _build_word_pattern(words)
    matches = pd.Series(False, index=jobs.index)

    for col in columns:
        col_match = jobs[col].astype(str).str.contains(
            pattern, case=False, regex=True, na=False
        )
        matches = matches | col_match

    return matches


def _count_keywords(jobs: pd.DataFrame, pattern: str,
                    columns: tuple) -> pd.Series:
    """Total keyword hits per row across the given columns.

    Args:
        jobs: DataFrame to count over.
        pattern: Word-boundary alternation pattern from _build_word_pattern.
        columns: Column names to count across.

    Returns:
        Integer Series of hits per row.
    """
    counts = pd.Series(0, index=jobs.index)

    for col in columns:
        counts = counts + jobs[col].astype(str).str.count(
            pattern, flags=re.IGNORECASE
        )

    return counts


def _is_blank(column: pd.Series) -> pd.Series:
    """Whether each value is missing or whitespace-only.

    Args:
        column: Any Series.

    Returns:
        Boolean Series, True where the value carries no text.
    """
    return column.isna() | column.astype(str).str.strip().isin(("", "nan"))


def recover_titles(jobs: pd.DataFrame) -> pd.DataFrame:
    """Fill blank titles from the posting URL, in place on a copy.

    The scrape stage already tries this, but rows that predate the
    `title_from_url` fix -- or that came from a board whose detail cap was hit
    -- still arrive blank. Doing it here too means the filter judges the best
    title available rather than the one that happened to be stored.

    Args:
        jobs: Scraped rows with `title` and `url` columns.

    Returns:
        A copy with blank titles replaced where the URL yields one.
    """
    recovered = jobs.copy()
    blank = _is_blank(recovered["title"])

    if not blank.any():
        return recovered

    # A column where every title is missing reads back from CSV as float64
    # (all NaN), and writing strings into that is a dtype change pandas is in
    # the process of making an error. Say "text" up front.
    recovered["title"] = recovered["title"].astype(object)
    recovered.loc[blank, "title"] = (
        recovered.loc[blank, "url"].astype(str).map(title_from_url)
    )

    return recovered


def _cap_per_company(exempt: pd.Series, companies: pd.Series) -> pd.Series:
    """Thin an exemption mask down to MAX_UNJUDGEABLE_PER_COMPANY per company.

    Args:
        exempt: Boolean mask of rows exempted from the keyword requirement.
        companies: The `company` column, aligned with `exempt`.

    Returns:
        The mask with over-quota rows switched off, chosen by a seeded random
        sample so the survivors are representative rather than whichever the
        sitemap happened to list first.
    """
    kept = pd.Series(False, index=exempt.index)
    candidates = companies[exempt]

    for _, rows in candidates.groupby(candidates):
        if len(rows) > MAX_UNJUDGEABLE_PER_COMPANY:
            rows = rows.sample(
                n=MAX_UNJUDGEABLE_PER_COMPANY, random_state=EXEMPTION_SEED
            )

        kept.loc[rows.index] = True

    return kept


def first_filter() -> pd.DataFrame:
    """Filter scraped rows on title and URL, before any posting is fetched.

    Keeps a row if it is not blacklisted, and either matches enough keywords
    or has nothing to match against at all.

    That second arm is the delicate one. It used to fire on any row whose
    title *or URL* ended in an id -- and since most boards file postings at
    `/jobs/{id}`, it fired on 7,403 of the 8,350 rows the filter passed. 89% of
    its output had never been filtered, and the next stage then fetched all of
    them. It now requires the title to be genuinely empty after
    `recover_titles`, and is capped per company on top, which is what makes it
    an exemption for a real blind spot rather than a hole.

    Blacklist still applies to exempt rows: a posting titled "senior" is
    dropped whether or not its URL is descriptive.

    A keyword hit can come from either the title or the URL. Matching is
    case-insensitive and anchored to word boundaries so that e.g. the keyword
    'Go' won't match inside 'Google'.

    Returns:
        Filtered DataFrame with jobs meeting the criteria, in original order.
    """
    keywords = get_keywords()
    blacklist = get_blacklist()
    jobs = pd.read_csv(paths.JOBS_CSV)

    if jobs.empty:
        return jobs

    jobs = recover_titles(jobs)

    # Vectorized blacklist checking: drop blacklisted rows first.
    is_blacklisted = _row_matches_any(jobs, blacklist)
    jobs_filtered = jobs[~is_blacklisted].copy()

    if jobs_filtered.empty or not keywords:
        return jobs_filtered.reset_index(drop=True)

    kw_count = _count_keywords(
        jobs_filtered, _build_word_pattern(keywords), ("title", "url")
    )

    # A row with no title after recovery has literally no words to judge --
    # AXA and Leroy Merlin publish `/jobs/843490` and nothing else anywhere in
    # the path. Fetching some of those is the only way to learn what they are.
    unjudgeable = _cap_per_company(
        _is_blank(jobs_filtered["title"]), jobs_filtered["company"]
    )

    keep = (kw_count >= MIN_KEYWORD_MATCHES) | unjudgeable

    return jobs_filtered[keep].reset_index(drop=True)


def second_filter() -> pd.DataFrame:
    """Filter detailed jobs on title, URL and description, with annotation.

    Keeps a job if it is not blacklisted in any of the three fields and its
    keyword hits clear the threshold -- MIN_KEYWORD_MATCHES_DETAILED normally,
    MIN_KEYWORD_MATCHES_INCOMPLETE when the title or URL is missing.

    There is no exemption here, and there should not be: the description is
    the real matching material, and a posting that fails to mention anything
    relevant across its entire body is a genuine miss rather than a blind spot.
    Jobs with missing or blank descriptions are dropped entirely.

    Returns:
        Filtered DataFrame with jobs meeting criteria, plus a `keyword_hits`
        column showing the total keyword match count (for inspection/tuning).
    """
    keywords = get_keywords()
    blacklist = get_blacklist()
    jobs = pd.read_csv(paths.DETAILED_CSV)

    if jobs.empty:
        return jobs

    fields = ("title", "url", "description")

    jobs_with_desc = jobs[~_is_blank(jobs["description"])].copy()

    if jobs_with_desc.empty or not keywords:
        return jobs_with_desc.reset_index(drop=True)

    is_blacklisted = _row_matches_any(jobs_with_desc, blacklist,
                                      columns=fields)
    jobs_filtered = jobs_with_desc[~is_blacklisted].copy()

    if jobs_filtered.empty:
        return jobs_filtered.reset_index(drop=True)

    kw_count = _count_keywords(
        jobs_filtered, _build_word_pattern(keywords), fields
    )

    incomplete = (_is_blank(jobs_filtered["title"])
                  | _is_blank(jobs_filtered["url"]))
    threshold = incomplete.map({
        True: MIN_KEYWORD_MATCHES_INCOMPLETE,
        False: MIN_KEYWORD_MATCHES_DETAILED,
    })

    keep = kw_count >= threshold
    result = jobs_filtered[keep].copy()
    result["keyword_hits"] = kw_count[keep]

    return result.reset_index(drop=True)
