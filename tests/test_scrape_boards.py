"""Where scrape_boards gets its boards from: the DB by default, a CSV on
request, or a list handed in."""

import pytest

from db import boards as db_boards
from job_scraper import scrape_boards, paths

pytestmark = pytest.mark.anyio


@pytest.fixture
def scraped(tmp_path, monkeypatch):
    """Record which boards get scraped, without any network."""
    seen = []

    async def fake_scrape_one(entry, client, render=None):
        seen.append(entry["url"])
        board = type("B", (), {})()

        return scrape_boards.BoardResult(
            entry["company"], entry["url"], board, ats="x", jobs=[],
            error=None)

    async def fake_explain(board, exc=None):
        return "stub"

    monkeypatch.setattr(scrape_boards, "scrape_one_board", fake_scrape_one)
    monkeypatch.setattr(scrape_boards.diagnose, "explain", fake_explain)
    monkeypatch.setattr(scrape_boards, "API_SOURCES", {})
    monkeypatch.setattr(paths, "NO_JOBS_CSV", tmp_path / "no_jobs.csv")

    return seen


async def test_boards_default_to_the_active_rows_in_the_database(
        db, scraped, tmp_path):
    db_boards.upsert_boards([("a", "http://a/jobs"), ("b", "http://b/jobs")])

    stats = await scrape_boards.scrape_boards(output_file=tmp_path / "jobs.csv")

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

    await scrape_boards.scrape_boards(output_file=tmp_path / "jobs.csv")

    assert scraped == ["http://a/jobs"]


async def test_input_file_reads_a_csv_and_skips_comments(scraped, tmp_path):
    source = tmp_path / "boards.csv"
    source.write_text("company,url\nacme,http://a/jobs\n#off,http://z/jobs\n",
                      encoding="utf-8")

    await scrape_boards.scrape_boards(
        input_file=source, output_file=tmp_path / "jobs.csv")

    assert scraped == ["http://a/jobs"]


async def test_boards_argument_wins_and_limit_still_applies(
        scraped, tmp_path):
    given = [{"company": "a", "url": "http://a/jobs"},
             {"company": "b", "url": "http://b/jobs"}]

    stats = await scrape_boards.scrape_boards(
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

    monkeypatch.setattr(scrape_boards, "API_SOURCES", {"x": boom})

    with pytest.raises(RuntimeError):
        await scrape_boards.scrape_boards(
            boards=[{"company": "a", "url": "http://a/jobs"}],
            output_file=out)

    assert out.read_text(encoding="utf-8") == "old"
    assert (tmp_path / "no_jobs.csv").read_text(encoding="utf-8") == "old"


async def test_a_good_scrape_replaces_the_files_and_leaves_no_tmp(
        scraped, tmp_path):
    out = tmp_path / "jobs.csv"
    out.write_text("old", encoding="utf-8")

    await scrape_boards.scrape_boards(
        boards=[{"company": "a", "url": "http://a/jobs"}], output_file=out)

    assert out.read_text(encoding="utf-8").startswith("company,")
    assert not list(tmp_path.glob("*.tmp"))


def test_junk_links_are_recognised():
    junk = ["", "/relative/job", "http:///x"]
    fine = ["https://acme.fr/jobs/1",
            "https://www.google.com/about/careers/applications/jobs/results/1"]

    assert all(scrape_boards.is_junk(u) for u in junk)
    assert not any(scrape_boards.is_junk(u) for u in fine)


async def test_junk_rows_are_dropped_from_the_output(tmp_path, monkeypatch):
    from job_scraper.models import Job

    async def fake_scrape_one(entry, client, render=None):
        jobs = [Job("a", "ok", "https://a.fr/1", via="links"),
                Job("a", "google", "https://www.google.com/about/careers/1",
                    via="links"),
                Job("a", "nohost", "/rel", via="links")]

        return scrape_boards.BoardResult(
            entry["company"], entry["url"], object(), ats="x", jobs=jobs,
            error=None)

    monkeypatch.setattr(scrape_boards, "scrape_one_board", fake_scrape_one)
    monkeypatch.setattr(scrape_boards, "API_SOURCES", {})
    monkeypatch.setattr(paths, "NO_JOBS_CSV", tmp_path / "no_jobs.csv")

    stats = await scrape_boards.scrape_boards(
        boards=[{"company": "a", "url": "http://a/jobs"}],
        output_file=tmp_path / "jobs.csv")

    assert (stats["jobs"], stats["dropped"]) == (2, 1)


@pytest.mark.parametrize("stats,code", [
    ({"boards": 10, "jobs": 5, "failed": 0}, 0),
    ({"boards": 0, "jobs": 0, "failed": 0}, 1),
    ({"boards": 10, "jobs": 0, "failed": 0}, 1),
    ({"boards": 10, "jobs": 5, "failed": 3}, 1),
])
def test_exit_code_reflects_run_health(stats, code, monkeypatch):
    async def fake(**kwargs):
        return {**stats, "dropped": 0, "output": "o", "no_jobs": "n"}

    monkeypatch.setattr(scrape_boards, "scrape_boards", fake)
    monkeypatch.setattr("sys.argv", ["scrape_boards"])

    assert scrape_boards.main() == code


def test_render_candidates_keeps_only_js_shell_reasons(tmp_path):
    path = tmp_path / "no_jobs.csv"
    path.write_text(
        "company,url,reason\n"
        "a,http://a,likely javascript-rendered (3 anchors, 20 scripts)\n"
        "b,http://b,no links on the page (0 anchors)\n"
        "c,http://c,http 403\n"
        "d,http://d,job-shaped links present but no strategy read them\n"
        "e,http://e,no job-shaped links (40 anchors)\n", encoding="utf-8")

    assert [b["company"] for b in scrape_boards.render_candidates(path)] == [
        "a", "b"]


def test_render_candidates_without_a_file_is_empty(tmp_path):
    assert scrape_boards.render_candidates(tmp_path / "missing.csv") == []


async def test_a_separate_no_jobs_file_leaves_the_input_untouched(
        scraped, tmp_path):
    original = paths.NO_JOBS_CSV
    original.write_text("company,url,reason\nx,http://x,old\n",
                        encoding="utf-8")
    other = tmp_path / "no_jobs_rendered.csv"

    await scrape_boards.scrape_boards(
        boards=[{"company": "a", "url": "http://a/jobs"}],
        output_file=tmp_path / "jobs_rendered.csv", no_jobs_file=other)

    assert "old" in original.read_text(encoding="utf-8")
    assert other.exists()
