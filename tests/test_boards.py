"""Tests for db/boards.py."""

from db import boards


def test_upsert_boards_is_idempotent_and_refreshes_the_company(db):
    boards.upsert_boards([("acme", "http://a/jobs"), ("globex", "http://g/jobs")])
    boards.upsert_boards([("ACME Corp", "http://a/jobs")])

    assert boards.count_boards() == 2
    assert {"company": "ACME Corp", "url": "http://a/jobs"} in \
        boards.select_active_boards()


def test_upsert_boards_collapses_a_url_listed_twice(db):
    boards.upsert_boards([("a", "http://a/jobs"), ("b", "http://a/jobs")])

    assert boards.count_boards() == 1


def test_upsert_boards_empty_is_a_noop(db):
    assert boards.upsert_boards([]) == 0


def test_import_boards_csv_reads_company_and_url(db, tmp_path):
    path = tmp_path / "job_boards.csv"
    path.write_text("company,url\nacme,http://a/jobs\n,\n", encoding="utf-8")

    assert boards.import_boards_csv(path) == 1
    assert boards.import_boards_csv(path) == 1
    assert boards.count_boards() == 1
