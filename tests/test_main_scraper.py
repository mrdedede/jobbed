"""Where scrape_boards gets its boards from: the DB by default, a CSV on
request, or a list handed in."""

import pytest

from db import boards as db_boards
from job_scraper import main_scraper, paths

pytestmark = pytest.mark.anyio


@pytest.fixture
def scraped(tmp_path, monkeypatch):
    """Record which boards get scraped, without any network."""
    seen = []

    async def fake_scrape_one(entry, client, render=None):
        seen.append(entry["url"])
        board = type("B", (), {})()

        return main_scraper.BoardResult(
            entry["company"], entry["url"], board, ats="x", jobs=[],
            error=None)

    async def fake_explain(board, exc=None):
        return "stub"

    monkeypatch.setattr(main_scraper, "scrape_one_board", fake_scrape_one)
    monkeypatch.setattr(main_scraper.diagnose, "explain", fake_explain)
    monkeypatch.setattr(main_scraper, "API_SOURCES", {})
    monkeypatch.setattr(paths, "NO_JOBS_CSV", tmp_path / "no_jobs.csv")

    return seen


async def test_boards_default_to_the_active_rows_in_the_database(
        db, scraped, tmp_path):
    db_boards.upsert_boards([("a", "http://a/jobs"), ("b", "http://b/jobs")])

    stats = await main_scraper.scrape_boards(output_file=tmp_path / "jobs.csv")

    assert sorted(scraped) == ["http://a/jobs", "http://b/jobs"]
    assert stats["boards"] == 2


async def test_inactive_boards_are_skipped(db, scraped, tmp_path):
    from db.engine import engine
    from db.tables import job_boards

    db_boards.upsert_boards([("a", "http://a/jobs"), ("b", "http://b/jobs")])
    with engine.begin() as conn:
        conn.execute(job_boards.update()
                     .where(job_boards.c.url == "http://b/jobs")
                     .values(active=False))

    await main_scraper.scrape_boards(output_file=tmp_path / "jobs.csv")

    assert scraped == ["http://a/jobs"]


async def test_input_file_reads_a_csv_and_skips_comments(scraped, tmp_path):
    source = tmp_path / "boards.csv"
    source.write_text("company,url\nacme,http://a/jobs\n#off,http://z/jobs\n",
                      encoding="utf-8")

    await main_scraper.scrape_boards(
        input_file=source, output_file=tmp_path / "jobs.csv")

    assert scraped == ["http://a/jobs"]


async def test_boards_argument_wins_and_limit_still_applies(
        scraped, tmp_path):
    given = [{"company": "a", "url": "http://a/jobs"},
             {"company": "b", "url": "http://b/jobs"}]

    stats = await main_scraper.scrape_boards(
        boards=given, limit=1, output_file=tmp_path / "jobs.csv")

    assert scraped == ["http://a/jobs"]
    assert stats["boards"] == 1


async def test_a_failed_scrape_leaves_the_previous_files_untouched(
        scraped, tmp_path, monkeypatch):
    out = tmp_path / "jobs.csv"
    out.write_text("old", encoding="utf-8")
    (tmp_path / "no_jobs.csv").write_text("old", encoding="utf-8")

    async def boom(client):
        raise RuntimeError("source down")

    monkeypatch.setattr(main_scraper, "API_SOURCES", {"x": boom})

    with pytest.raises(RuntimeError):
        await main_scraper.scrape_boards(
            boards=[{"company": "a", "url": "http://a/jobs"}],
            output_file=out)

    assert out.read_text(encoding="utf-8") == "old"
    assert (tmp_path / "no_jobs.csv").read_text(encoding="utf-8") == "old"


async def test_a_good_scrape_replaces_the_files_and_leaves_no_tmp(
        scraped, tmp_path):
    out = tmp_path / "jobs.csv"
    out.write_text("old", encoding="utf-8")

    await main_scraper.scrape_boards(
        boards=[{"company": "a", "url": "http://a/jobs"}], output_file=out)

    assert out.read_text(encoding="utf-8").startswith("company,")
    assert not list(tmp_path.glob("*.tmp"))
