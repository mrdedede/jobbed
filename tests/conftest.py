"""Shared fixtures.

The autouse one matters: `fetching.fetch` sleeps REQUEST_DELAY before every
request to stay polite to real boards, and the suite makes several thousand
fetches against in-memory doubles. Left alone it turns a 5-second run into
several minutes of sleeping at fake HTTP.
"""

import pytest

from job_scraper import fetching, paths


@pytest.fixture(autouse=True)
def no_request_delay(monkeypatch):
    """Remove the politeness delay for every test.

    Nothing here talks to a real host, so there is nobody to be polite to.
    """
    monkeypatch.setattr(fetching, "REQUEST_DELAY", 0)


@pytest.fixture
def filter_files(tmp_path, monkeypatch):
    """Point the pipeline's data paths at writable temp files.

    Returns:
        A function `write(jobs=..., detailed=..., keywords=..., blacklist=...)`
        that writes whichever inputs a test needs and leaves the rest empty.
        Every filter test needs some subset of these four, and rebuilding the
        same four monkeypatches inline was repeated verbatim ~20 times.
    """
    files = {
        "JOBS_CSV": tmp_path / "jobs.csv",
        "DETAILED_CSV": tmp_path / "detailed_jobs.csv",
        "KEYWORDS_TXT": tmp_path / "keywords.txt",
        "BLACKLIST_TXT": tmp_path / "blacklist.txt",
    }

    for name, path in files.items():
        monkeypatch.setattr(paths, name, path)

    def write(jobs=None, detailed=None, keywords=(), blacklist=()):
        if jobs is not None:
            jobs.to_csv(files["JOBS_CSV"], index=False)

        if detailed is not None:
            detailed.to_csv(files["DETAILED_CSV"], index=False)

        files["KEYWORDS_TXT"].write_text("\n".join(keywords),
                                         encoding="utf-8")
        files["BLACKLIST_TXT"].write_text("\n".join(blacklist),
                                          encoding="utf-8")

        return files

    return write
