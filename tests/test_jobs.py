"""Tests for db/jobs.py."""

import pytest

from db import jobs
from job_scraper import paths


@pytest.fixture
def filtered_csv(tmp_path, monkeypatch):
    """Point insert_jobs at a CSV this test writes."""
    path = tmp_path / "filtered_detailed_jobs.csv"
    monkeypatch.setattr(paths, "DETAILED_CSV", path)

    def write(*urls):
        header = "company,title,description,url,place,via,ats,keyword_hits\n"
        rows = "".join(
            f"Acme,Go Developer,A description,{url},Paris,jsonld,,5\n"
            for url in urls
        )
        path.write_text(header + rows, encoding="utf-8")

        return path

    return write


def test_insert_jobs_counts_new_rows(db, filtered_csv):
    filtered_csv("http://x/1", "http://x/2")

    assert jobs.insert_jobs() == (2, 0)


def test_insert_jobs_skips_urls_already_stored(db, filtered_csv):
    """The `url` UNIQUE constraint does the deduplication, so a rerun of the
    same day's scrape is free rather than a duplicate-key error."""
    filtered_csv("http://x/1", "http://x/2")
    jobs.insert_jobs()

    filtered_csv("http://x/1", "http://x/2", "http://x/3")

    assert jobs.insert_jobs() == (1, 2)


def test_insert_jobs_deduplicates_within_one_batch(db, filtered_csv):
    """Two boards can list the same posting; ON CONFLICT DO NOTHING catches
    it mid-batch where a pre-read of existing URLs could not."""
    filtered_csv("http://x/1", "http://x/1")

    assert jobs.insert_jobs() == (1, 1)


def test_insert_jobs_on_empty_csv(db, filtered_csv):
    filtered_csv()

    assert jobs.insert_jobs() == (0, 0)


def test_select_job_urls_returns_every_stored_url(db, filtered_csv):
    filtered_csv("http://x/1", "http://x/2")
    jobs.insert_jobs()

    assert jobs.select_job_urls() == {"http://x/1", "http://x/2"}


def test_select_job_urls_empty_when_nothing_stored(db):
    assert jobs.select_job_urls() == set()


def test_insert_jobs_leaves_out_dead_pages(db):
    """A row with no description would mark its URL known forever."""
    rows = [("a", "t", "text", "http://x/1", "", "jsonld", None),
            ("a", "t", "", "http://x/2", "", "none", None)]

    assert jobs.insert_jobs(rows) == (1, 1)
    assert jobs.select_job_urls() == {"http://x/1"}


def test_insert_jobs_chunks_past_the_param_cap(db, monkeypatch):
    """Postgres rejects >65535 params per statement; big batches split."""
    monkeypatch.setattr(jobs, "_INSERT_CHUNK", 2)
    rows = [("a", "t", "text", f"http://x/{i}", "", "jsonld", None)
            for i in range(5)] + [("a", "t", "text", "http://x/0", "", "jsonld", None)]

    assert jobs.insert_jobs(rows) == (5, 1)
    assert len(jobs.select_job_urls()) == 5


def test_insert_jobs_with_an_explicit_empty_list_reads_no_file(
        tmp_path, monkeypatch):
    """[] is "nothing to store", not "go read the detail CSV"."""
    from job_scraper import paths

    monkeypatch.setattr(paths, "DETAILED_CSV", tmp_path / "missing.csv")

    assert jobs.insert_jobs([]) == (0, 0)
