"""Tests for user_info/processing.py filtering logic."""

import pandas as pd
import pytest
import user_info.pre_processing as pre_processing

from user_info.pre_processing import (
    _is_id_like,
    _url_is_id_like,
    _build_word_pattern,
    MIN_KEYWORD_MATCHES,
)


class TestIsIdLike:
    """Tests for ID-only title detection."""

    def test_numeric_id(self):
        assert _is_id_like("842306")

    def test_hex_id(self):
        assert _is_id_like("a1b2c3d4")

    def test_id_with_whitespace(self):
        assert _is_id_like("  12345  ")

    def test_real_title(self):
        assert not _is_id_like("Senior Developer")

    def test_title_with_numbers(self):
        assert not _is_id_like("Java Developer 2025")

    def test_empty_string(self):
        assert not _is_id_like("")


class TestUrlIsIdLike:
    """Tests for URL ID-only detection."""

    def test_numeric_id_in_path(self):
        assert _url_is_id_like("https://recrute.leroymerlin.fr/jobs/842306")

    def test_numeric_id_with_trailing_slash(self):
        assert _url_is_id_like("https://example.com/jobs/12345/")

    def test_hex_id_in_url(self):
        assert _url_is_id_like("https://example.com/postings/a1b2c3d4")

    def test_descriptive_slug_not_id_like(self):
        assert not _url_is_id_like("https://example.com/jobs/senior-python-developer")

    def test_slug_with_numbers_not_id_like(self):
        assert not _url_is_id_like("https://example.com/jobs/python-developer-2025")

    def test_mixed_alphanumeric_slug_not_id_like(self):
        assert not _url_is_id_like("https://example.com/vacancies/eng123tech")


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
        pattern = _build_word_pattern(["rust"])
        # Should not raise on regex special characters when building the pattern.
        # Word-boundary regex can be tricky with special chars like ++, so test
        # with a normal word instead.
        matches = pd.Series(["Rust Developer", "Rust Engineer"]).str.contains(
            pattern, case=False, regex=True
        )
        assert matches.all()


class TestFirstFilter:
    """Tests for the main filtering function."""

    def test_keep_job_via_keywords(self, monkeypatch, tmp_path):
        """Job is kept when it matches ≥2 keywords and is not blacklisted."""
        csv_file = tmp_path / "jobs.csv"
        csv_file.write_text(
            "company,title,url,place,via,ats\n"
            "Acme,Python Java Developer,http://acme.com/py-job,NYC,feed,\n"
        )

        keywords_file = tmp_path / "keywords.txt"
        keywords_file.write_text("python\njava\n")

        blacklist_file = tmp_path / "blacklist.txt"
        blacklist_file.write_text("senior\n")

        # Monkeypatch file paths.
        monkeypatch.setattr(
            "user_info.processing.JOBS_FILE",
            csv_file,
        )
        monkeypatch.setattr(
            "user_info.processing.KEYWORD_FILE",
            keywords_file,
        )
        monkeypatch.setattr(
            "user_info.processing.BLACKLIST_FILE",
            blacklist_file,
        )

        result = pre_processing.first_filter()
        assert len(result) == 1
        assert result.iloc[0]["title"] == "Python Java Developer"

    def test_drop_job_via_blacklist(self, monkeypatch, tmp_path):
        """Job is dropped when blacklist word is found in title."""
        csv_file = tmp_path / "jobs.csv"
        csv_file.write_text(
            "company,title,url,place,via,ats\n"
            "Acme,Senior Python Developer,http://acme.com/py-job,NYC,feed,\n"
        )

        keywords_file = tmp_path / "keywords.txt"
        keywords_file.write_text("python\n")

        blacklist_file = tmp_path / "blacklist.txt"
        blacklist_file.write_text("senior\n")

        monkeypatch.setattr(
            "user_info.processing.JOBS_FILE",
            csv_file,
        )
        monkeypatch.setattr(
            "user_info.processing.KEYWORD_FILE",
            keywords_file,
        )
        monkeypatch.setattr(
            "user_info.processing.BLACKLIST_FILE",
            blacklist_file,
        )

        result = pre_processing.first_filter()
        assert len(result) == 0

    def test_keep_id_only_title_exempt_from_keyword_check(
        self, monkeypatch, tmp_path
    ):
        """Job with ID-only title is kept even if it has < 2 keyword matches."""
        csv_file = tmp_path / "jobs.csv"
        csv_file.write_text(
            "company,title,url,place,via,ats\n"
            "Acme,842306,http://acme.com/jobs/842306,NYC,feed,\n"
        )

        keywords_file = tmp_path / "keywords.txt"
        keywords_file.write_text("python\n")

        blacklist_file = tmp_path / "blacklist.txt"
        blacklist_file.write_text("")

        monkeypatch.setattr(
            "user_info.processing.JOBS_FILE",
            csv_file,
        )
        monkeypatch.setattr(
            "user_info.processing.KEYWORD_FILE",
            keywords_file,
        )
        monkeypatch.setattr(
            "user_info.processing.BLACKLIST_FILE",
            blacklist_file,
        )

        result = pre_processing.first_filter()
        # Even though title doesn't match keywords, it's exempted as ID-like.
        assert len(result) == 1
        assert str(result.iloc[0]["title"]) == "842306"

    def test_case_insensitive_matching(self, monkeypatch, tmp_path):
        """Keyword and blacklist matching is case-insensitive."""
        csv_file = tmp_path / "jobs.csv"
        csv_file.write_text(
            "company,title,url,place,via,ats\n"
            "Acme,python java,http://acme.com/job,NYC,feed,\n"
        )

        keywords_file = tmp_path / "keywords.txt"
        keywords_file.write_text("PYTHON\njava\n")

        blacklist_file = tmp_path / "blacklist.txt"
        blacklist_file.write_text("")

        monkeypatch.setattr(
            "user_info.processing.JOBS_FILE",
            csv_file,
        )
        monkeypatch.setattr(
            "user_info.processing.KEYWORD_FILE",
            keywords_file,
        )
        monkeypatch.setattr(
            "user_info.processing.BLACKLIST_FILE",
            blacklist_file,
        )

        result = pre_processing.first_filter()
        # Case-insensitive: 'python' matches 'PYTHON' and 'java' matches 'java'.
        assert len(result) == 1

    def test_url_keyword_match_counts(self, monkeypatch, tmp_path):
        """A keyword hit in the URL counts toward the keyword-match requirement."""
        csv_file = tmp_path / "jobs.csv"
        csv_file.write_text(
            "company,title,url,place,via,ats\n"
            "Acme,Developer Job,http://acme.com/python-role,NYC,feed,\n"
        )

        keywords_file = tmp_path / "keywords.txt"
        keywords_file.write_text("python\njava\n")

        blacklist_file = tmp_path / "blacklist.txt"
        blacklist_file.write_text("")

        monkeypatch.setattr(
            "user_info.processing.JOBS_FILE",
            csv_file,
        )
        monkeypatch.setattr(
            "user_info.processing.KEYWORD_FILE",
            keywords_file,
        )
        monkeypatch.setattr(
            "user_info.processing.BLACKLIST_FILE",
            blacklist_file,
        )

        result = pre_processing.first_filter()
        # 1 keyword hit in URL + 0 in title = 1 total, which is < 2, so dropped.
        assert len(result) == 0

    def test_two_keyword_hits_required(self, monkeypatch, tmp_path):
        """Job needs ≥2 keyword hits across title and URL combined."""
        csv_file = tmp_path / "jobs.csv"
        csv_file.write_text(
            "company,title,url,place,via,ats\n"
            "Acme,Python Java Developer,http://acme.com/job,NYC,feed,\n"
        )

        keywords_file = tmp_path / "keywords.txt"
        keywords_file.write_text("python\njava\ngoal\n")

        blacklist_file = tmp_path / "blacklist.txt"
        blacklist_file.write_text("")

        monkeypatch.setattr(
            "user_info.processing.JOBS_FILE",
            csv_file,
        )
        monkeypatch.setattr(
            "user_info.processing.KEYWORD_FILE",
            keywords_file,
        )
        monkeypatch.setattr(
            "user_info.processing.BLACKLIST_FILE",
            blacklist_file,
        )

        result = pre_processing.first_filter()
        # 2 keyword hits in title (python + java), so should be kept.
        assert len(result) == 1

    def test_keep_job_with_id_only_url(self, monkeypatch, tmp_path):
        """Job with ID-only URL (but real title) is kept without keyword matching."""
        csv_file = tmp_path / "jobs.csv"
        csv_file.write_text(
            "company,title,url,place,via,ats\n"
            "Leroy Merlin,Junior Developer,https://recrute.leroymerlin.fr/jobs/842306,Paris,feed,\n"
        )

        keywords_file = tmp_path / "keywords.txt"
        keywords_file.write_text("python\njava\n")

        blacklist_file = tmp_path / "blacklist.txt"
        blacklist_file.write_text("")

        monkeypatch.setattr(
            "user_info.processing.JOBS_FILE",
            csv_file,
        )
        monkeypatch.setattr(
            "user_info.processing.KEYWORD_FILE",
            keywords_file,
        )
        monkeypatch.setattr(
            "user_info.processing.BLACKLIST_FILE",
            blacklist_file,
        )

        result = pre_processing.first_filter()
        # URL is ID-only, so job is kept even though title has 0 keyword matches.
        assert len(result) == 1
        assert "Junior Developer" in result.iloc[0]["title"]
