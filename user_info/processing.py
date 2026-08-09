import re
from pathlib import Path
from typing import List

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
JOBS_FILE = ROOT / "temp" / "jobs.csv"
FILTERED_FILE = ROOT / "temp" / "filtered_file.csv"
KEYWORD_FILE = ROOT / "user_info" / "keywords.txt"
BLACKLIST_FILE = ROOT / "user_info" / "blacklist.txt"

MIN_KEYWORD_MATCHES = 2


def get_keywords() -> List[str]:
    """Load and normalize keywords from file.

    Reads keywords.txt, strips whitespace, drops empty lines, and lowercases
    all entries for case-insensitive matching.

    Returns:
        List of normalized keyword strings (lowercased, stripped).
    """
    return [
        line.strip().lower()
        for line in KEYWORD_FILE.read_text(encoding="utf-8").splitlines()
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
        for line in BLACKLIST_FILE.read_text(encoding="utf-8").splitlines()
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


def _is_id_like(title: str) -> bool:
    """Check if title is just an ID with no real words.

    Some feeds (e.g., Personio, BambooHR) return only numeric or hex IDs for
    titles. These have nothing to match keywords against and should be exempted
    from the keyword-count requirement, though they still go through blacklist
    checking.

    Args:
        title: Job title to check.

    Returns:
        True if title is purely numeric or hex digits (possibly with whitespace).
    """
    return bool(re.match(r"^\s*[0-9a-f]+\s*$", title, re.IGNORECASE))


def _url_is_id_like(url: str) -> bool:
    """Check if URL path ends with just a numeric/hex ID, not a descriptive slug.

    Some boards (e.g., Leroy Merlin, AXA) post jobs at /jobs/{id} with no
    descriptive slug, so the path segment alone has no keywords to match.
    These should be exempted from the keyword-count requirement.

    Args:
        url: Job posting URL.

    Returns:
        True if the URL path's last segment is purely numeric or hex digits.
    """
    from urllib.parse import urlparse
    path = urlparse(url).path.rstrip("/")
    if not path:
        return False
    last_segment = path.rsplit("/", 1)[-1]
    return bool(re.match(r"^[0-9a-f]+$", last_segment, re.IGNORECASE))


def first_filter() -> pd.DataFrame:
    """Filter jobs by keywords and blacklist with smart handling of ID-only titles/URLs.

    Keeps a job if:
    - NOT blacklisted, AND
    - (Has ≥2 keyword matches OR title/URL is ID-only)

    ID-only jobs (title=123456 or URL=/jobs/123456) are exempt from the keyword
    requirement since they lack descriptive words to match. Blacklist still applies
    (e.g., a job with title "senior" is dropped even if it's an ID-only URL).

    A keyword/blacklist hit can come from either the job title or URL.
    Matching is case-insensitive and anchored to word boundaries so that e.g.
    the keyword 'Go' won't match inside 'Google'.

    Returns:
        Filtered DataFrame with jobs meeting the criteria, in original order.
    """
    keywords = get_keywords()
    blacklist = get_blacklist()
    jobs = pd.read_csv(JOBS_FILE)

    if jobs.empty:
        return jobs

    # Vectorized blacklist checking: drop blacklisted rows first.
    is_blacklisted = _row_matches_any(jobs, blacklist) if blacklist else pd.Series(
        False, index=jobs.index
    )
    jobs_filtered = jobs[~is_blacklisted].copy()

    if jobs_filtered.empty or not keywords:
        return jobs_filtered.reset_index(drop=True)

    # Vectorized keyword matching: count hits per row across title and url.
    keyword_pattern = _build_word_pattern(keywords)
    title_matches = jobs_filtered["title"].astype(str).str.count(
        keyword_pattern, flags=re.IGNORECASE
    )
    url_matches = jobs_filtered["url"].astype(str).str.count(
        keyword_pattern, flags=re.IGNORECASE
    )
    kw_count = title_matches + url_matches

    # Vectorized ID-like detection for both title and URL.
    title_is_id_like = jobs_filtered["title"].astype(str).apply(_is_id_like)
    url_is_id_like = jobs_filtered["url"].astype(str).apply(_url_is_id_like)

    # Keep if: keyword hits >= MIN OR (title/URL is ID-like with no descriptive words).
    keep = (kw_count >= MIN_KEYWORD_MATCHES) | title_is_id_like | url_is_id_like

    return jobs_filtered[keep].reset_index(drop=True)


def _row_matches_any(jobs: pd.DataFrame, words: List[str]) -> pd.Series:
    """Check if each row's title or url contains any of the given words.

    Args:
        jobs: DataFrame with 'title' and 'url' columns.
        words: List of words to match.

    Returns:
        Boolean Series indicating rows with at least one match.
    """
    if not words:
        return pd.Series(False, index=jobs.index)

    pattern = _build_word_pattern(words)
    title_match = jobs["title"].astype(str).str.contains(
        pattern, case=False, regex=True, na=False
    )
    url_match = jobs["url"].astype(str).str.contains(
        pattern, case=False, regex=True, na=False
    )
    return title_match | url_match

if __name__ == '__main__':
    dataframe = first_filter()
    dataframe.to_csv(FILTERED_FILE)