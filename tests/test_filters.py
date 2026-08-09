"""Tests for job_scraper/filters.py filtering logic.

The `filter_files` fixture (see conftest.py) points the module's four file
constants at tmp_path and writes whichever inputs a test needs.
"""

import pandas as pd
import pytest

from job_scraper import filters
from job_scraper.filters import (
    MAX_UNJUDGEABLE_PER_COMPANY,
    MIN_KEYWORD_MATCHES,
    _build_word_pattern,
    _is_blank,
    recover_titles,
)

FIRST_COLUMNS = ["company", "title", "url", "place", "via", "ats"]
DETAIL_COLUMNS = ["company", "title", "description", "url", "place", "via",
                  "ats"]


def jobs(*rows) -> pd.DataFrame:
    """Build a scraped-stage frame from (company, title, url) triples."""
    return pd.DataFrame(
        [(company, title, url, "Paris", "sitemap", "")
         for company, title, url in rows],
        columns=FIRST_COLUMNS,
    )


def detailed(*rows) -> pd.DataFrame:
    """Build a detail-stage frame from (title, description, url) triples."""
    return pd.DataFrame(
        [("Acme", title, description, url, "Paris", "jsonld", "")
         for title, description, url in rows],
        columns=DETAIL_COLUMNS,
    )


# ======================================================================
# Pattern building
# ======================================================================


class TestBuildWordPattern:
    """Tests for regex pattern construction."""

    def test_single_word(self):
        pattern = _build_word_pattern(["python"])
        assert pd.Series(["Python Developer"]).str.contains(
            pattern, case=False, regex=True
        ).all()

    def test_multi_word_phrase(self):
        pattern = _build_word_pattern(["google cloud"])
        assert pd.Series(["Google Cloud Engineer"]).str.contains(
            pattern, case=False, regex=True
        ).all()

    def test_word_boundary_no_substring(self):
        pattern = _build_word_pattern(["go"])
        matches = pd.Series(["Go Developer", "Google"]).str.contains(
            pattern, case=False, regex=True
        )
        # "Go" matches, "Google" does not (word boundary).
        assert matches[0]
        assert not matches[1]

    def test_special_chars_escaped(self):
        """A keyword like C++ must not blow up the alternation."""
        pattern = _build_word_pattern(["c++", "rust"])
        matches = pd.Series(["Rust Developer"]).str.contains(
            pattern, case=False, regex=True
        )
        assert matches.all()


# ======================================================================
# Blankness and title recovery
# ======================================================================


class TestIsBlank:
    """A row with no title is what earns the keyword exemption, so what
    counts as 'no title' has to be exact."""

    def test_empty_and_whitespace_are_blank(self):
        found = _is_blank(pd.Series(["", "   ", "\t"]))
        assert found.all()

    def test_missing_is_blank(self):
        """pandas reads an empty CSV cell as NaN, not ''."""
        assert _is_blank(pd.Series([None, float("nan")])).all()

    def test_the_string_nan_is_blank(self):
        """astype(str) on a NaN column yields the literal 'nan'; without this
        every missing title would read as a four-letter word to match."""
        assert _is_blank(pd.Series(["nan"])).all()

    def test_real_title_is_not_blank(self):
        assert not _is_blank(pd.Series(["Senior Developer"])).any()


class TestRecoverTitles:
    """The filter judges the best title available, not whichever the scrape
    stage happened to store."""

    def test_blank_title_filled_from_url(self):
        frame = jobs(("Sander", "", "https://x.com/fr/jobs/data-engineer/769"))
        assert recover_titles(frame).iloc[0]["title"] == "Data Engineer"

    def test_real_title_is_left_alone(self):
        frame = jobs(("Acme", "Go Developer", "https://x.com/jobs/12345"))
        assert recover_titles(frame).iloc[0]["title"] == "Go Developer"

    def test_url_with_no_words_stays_blank(self):
        frame = jobs(("AXA", "", "https://x.com/jobs/843490"))
        assert recover_titles(frame).iloc[0]["title"] == ""

    def test_the_input_frame_is_not_mutated(self):
        frame = jobs(("Sander", "", "https://x.com/jobs/data-engineer/769"))
        recover_titles(frame)
        assert frame.iloc[0]["title"] == ""


# ======================================================================
# first_filter
# ======================================================================


class TestFirstFilter:
    """Runs on title and URL alone; every row it keeps costs one fetch."""

    def test_keep_job_via_keywords(self, filter_files):
        filter_files(
            jobs(("Acme", "Python Java Developer", "http://acme.com/py-job")),
            keywords=["python", "java"],
            blacklist=["senior"],
        )

        result = filters.first_filter()

        assert len(result) == 1
        assert result.iloc[0]["title"] == "Python Java Developer"

    def test_drop_job_via_blacklist(self, filter_files):
        filter_files(
            jobs(("Acme", "Senior Python Developer", "http://acme.com/py")),
            keywords=["python"],
            blacklist=["senior"],
        )

        assert len(filters.first_filter()) == 0

    def test_case_insensitive_matching(self, filter_files):
        filter_files(
            jobs(("Acme", "python java", "http://acme.com/job")),
            keywords=["PYTHON", "java"],
        )

        assert len(filters.first_filter()) == 1

    def test_one_hit_is_not_enough(self, filter_files):
        """A single keyword in the URL is 1 hit, below MIN_KEYWORD_MATCHES."""
        filter_files(
            jobs(("Acme", "Developer Job", "http://acme.com/python-role")),
            keywords=["python", "java"],
        )

        assert len(filters.first_filter()) == 0

    def test_two_keyword_hits_are_enough(self, filter_files):
        filter_files(
            jobs(("Acme", "Python Java Developer", "http://acme.com/job")),
            keywords=["python", "java", "goal"],
        )

        assert len(filters.first_filter()) == 1

    def test_url_hits_count_toward_the_threshold(self, filter_files):
        """One in the title plus one in the URL clears the bar."""
        filter_files(
            jobs(("Acme", "Python Developer", "http://acme.com/java-role")),
            keywords=["python", "java"],
        )

        assert len(filters.first_filter()) == MIN_KEYWORD_MATCHES - 1 == 1


class TestFirstFilterExemption:
    """The narrowed ID-exemption -- the whole point of the refactor.

    It used to fire on any row whose title *or URL* looked like an id. Since
    most boards file postings at /jobs/{id}, that meant 7,403 of the 8,350
    rows the filter passed had never been filtered at all.
    """

    def test_titleless_row_is_exempt(self, filter_files):
        """A row with nothing to match must not be judged on nothing."""
        filter_files(
            jobs(("AXA", "", "https://axa.com/jobs/843490")),
            keywords=["python"],
        )

        assert len(filters.first_filter()) == 1

    def test_id_like_url_alone_no_longer_exempts(self, filter_files):
        """This is the leak. The title is real and matches no keyword, so the
        row must be dropped -- the id-shaped URL is not a reason to keep it."""
        filter_files(
            jobs(("Leroy Merlin", "Junior Accountant",
                  "https://recrute.leroymerlin.fr/jobs/842306")),
            keywords=["python", "java"],
        )

        assert len(filters.first_filter()) == 0

    def test_a_title_recovered_from_the_url_is_then_judged(self, filter_files):
        """Recovery and exemption interact: once the slug yields a title, the
        row is no longer unjudgeable and has to earn its place."""
        filter_files(
            jobs(("Sander", "",
                  "https://x.com/fr/jobs/python-java-developer/76969")),
            keywords=["python", "java"],
        )

        result = filters.first_filter()

        assert len(result) == 1
        assert result.iloc[0]["title"] == "Python Java Developer"

    def test_blacklist_still_applies_to_exempt_rows(self, filter_files):
        """Exempt from the keyword requirement is not exempt from everything."""
        filter_files(
            jobs(("AXA", "", "https://axa.com/jobs/senior/843490")),
            keywords=["python"],
            blacklist=["senior"],
        )

        assert len(filters.first_filter()) == 0

    def test_exemption_is_capped_per_company(self, filter_files):
        """One board must not be able to spend the whole detail budget."""
        over = MAX_UNJUDGEABLE_PER_COMPANY + 50
        filter_files(
            jobs(*[("AXA", "", f"https://axa.com/jobs/{n}")
                   for n in range(over)]),
            keywords=["python"],
        )

        assert len(filters.first_filter()) == MAX_UNJUDGEABLE_PER_COMPANY

    def test_the_cap_is_per_company_not_global(self, filter_files):
        """A small board keeps every row even while a large one is thinned."""
        rows = [("AXA", "", f"https://axa.com/jobs/{n}")
                for n in range(MAX_UNJUDGEABLE_PER_COMPANY + 50)]
        rows += [("Leroy Merlin", "", f"https://lm.fr/jobs/{n}")
                 for n in range(10)]
        filter_files(jobs(*rows), keywords=["python"])

        result = filters.first_filter()
        counts = result["company"].value_counts()

        assert counts["AXA"] == MAX_UNJUDGEABLE_PER_COMPANY
        assert counts["Leroy Merlin"] == 10

    def test_the_capped_sample_is_reproducible(self, filter_files):
        """Seeded, so two runs over the same input agree on which rows to
        fetch -- otherwise resume and reporting both drift."""
        frame = jobs(*[("AXA", "", f"https://axa.com/jobs/{n}")
                       for n in range(MAX_UNJUDGEABLE_PER_COMPANY + 50)])
        filter_files(frame, keywords=["python"])

        first = list(filters.first_filter()["url"])
        second = list(filters.first_filter()["url"])

        assert first == second

    def test_keyword_matches_are_never_capped(self, filter_files):
        """The cap thins guesses, not evidence."""
        over = MAX_UNJUDGEABLE_PER_COMPANY + 50
        filter_files(
            jobs(*[("AXA", "Python Java Developer",
                    f"https://axa.com/jobs/{n}") for n in range(over)]),
            keywords=["python", "java"],
        )

        assert len(filters.first_filter()) == over


# ======================================================================
# second_filter
# ======================================================================


class TestSecondFilter:
    """Runs once the description is in hand, so the bar is much higher."""

    def test_keep_job_with_five_hits(self, filter_files):
        filter_files(
            detailed=detailed((
                "Python Developer",
                "Looking for a Python and Java expert. Docker and Kubernetes "
                "experience required.",
                "http://acme.com/py-job",
            )),
            keywords=["python", "java", "docker", "kubernetes"],
        )

        result = filters.second_filter()

        assert len(result) == 1
        assert result.iloc[0]["keyword_hits"] == 5

    def test_drop_job_below_five_hits(self, filter_files):
        filter_files(
            detailed=detailed(("Developer", "Looking for Java expertise.",
                               "http://acme.com/job")),
            keywords=["python", "java"],
        )

        assert len(filters.second_filter()) == 0

    def test_lower_threshold_when_title_is_missing(self, filter_files):
        """3 hits clears the bar for a row with no title to have matched on."""
        filter_files(
            detailed=detailed(("", "Python Java and Docker required.",
                               "http://acme.com/job")),
            keywords=["python", "java", "docker"],
        )

        result = filters.second_filter()

        assert len(result) == 1
        assert result.iloc[0]["keyword_hits"] == 3

    def test_drop_job_without_description(self, filter_files):
        """No description means nothing this stage exists to read."""
        filter_files(
            detailed=detailed(("Python Java Developer", "",
                               "http://acme.com/job")),
            keywords=["python", "java"],
        )

        assert len(filters.second_filter()) == 0

    def test_blacklist_spans_the_description(self, filter_files):
        filter_files(
            detailed=detailed((
                "Python Java Developer",
                "This is a senior position with internship opportunities.",
                "http://acme.com/job",
            )),
            keywords=["python", "java"],
            blacklist=["senior", "internship"],
        )

        assert len(filters.second_filter()) == 0

    def test_keyword_hits_column_annotation(self, filter_files):
        filter_files(
            detailed=detailed((
                "Python",
                "Description with Java Docker Kubernetes React and MongoDB.",
                "http://acme.com/job",
            )),
            keywords=["python", "java", "docker", "kubernetes", "react",
                      "mongodb"],
        )

        result = filters.second_filter()

        assert "keyword_hits" in result.columns
        assert result.iloc[0]["keyword_hits"] == 6

    def test_no_exemption_at_this_stage(self, filter_files):
        """A titleless row with a real description is judged on the
        description -- there is nothing blind about it any more."""
        filter_files(
            detailed=detailed(("", "We sell garden furniture.",
                               "http://axa.com/jobs/843490")),
            keywords=["python", "java"],
        )

        assert len(filters.second_filter()) == 0


# ======================================================================
# Empty inputs
# ======================================================================


@pytest.mark.parametrize("stage", ["first_filter", "second_filter"])
def test_empty_input_returns_empty_rather_than_raising(filter_files, stage):
    """An empty scrape is a bad run, not a crash halfway down the pipeline."""
    filter_files(
        jobs=pd.DataFrame(columns=FIRST_COLUMNS),
        detailed=pd.DataFrame(columns=DETAIL_COLUMNS),
        keywords=["python"],
    )

    assert len(getattr(filters, stage)()) == 0
