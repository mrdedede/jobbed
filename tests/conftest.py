"""Shared fixtures.

The autouse one matters: `fetching.fetch` sleeps REQUEST_DELAY before every
request to stay polite to real boards, and the suite makes several thousand
fetches against in-memory doubles. Left alone it turns a 5-second run into
several minutes of sleeping at fake HTTP.
"""

import os

import pytest
from sqlalchemy import create_engine

from job_scraper import fetching, paths

TEST_DATABASE_URL = os.environ.get(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://joblister:joblister@localhost:5432/joblister_test",
)


@pytest.fixture(autouse=True)
def no_request_delay(monkeypatch):
    """Remove the politeness delay for every test.

    Nothing here talks to a real host, so there is nobody to be polite to.
    """
    monkeypatch.setattr(fetching, "REQUEST_DELAY", 0)
    monkeypatch.setattr(fetching, "RETRY_BACKOFF", 0)


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


@pytest.fixture
def anyio_backend():
    """Async tests run on asyncio only; trio is not installed or supported."""
    return "asyncio"


@pytest.fixture(scope="session")
def _db_schema():
    """Create every table once per test session, against TEST_DATABASE_URL.

    Points db.engine.engine at the test database for the rest of the run,
    then creates the schema directly from db.tables' metadata -- Alembic
    owns migrations for dev/prod, but a test run wants a clean, disposable
    schema every time rather than a migration history to maintain.
    """
    import db.engine as db_engine_module
    from db.tables import metadata

    test_engine = create_engine(TEST_DATABASE_URL)
    db_engine_module.engine = test_engine

    metadata.drop_all(test_engine)
    metadata.create_all(test_engine)

    yield test_engine

    metadata.drop_all(test_engine)
    test_engine.dispose()


@pytest.fixture
def db(_db_schema):
    """A clean set of tables for one test: every row truncated beforehand.

    Truncating (not dropping/recreating) between tests is what makes this
    fast enough to run per-test rather than per-session; CASCADE follows the
    FKs so child tables empty along with their parents regardless of order.
    """
    from db.tables import metadata

    with _db_schema.begin() as conn:
        for table in reversed(metadata.sorted_tables):
            conn.execute(table.delete())

    return _db_schema
