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

# Before anything imports db.engine: its `engine` is bound once, at import,
# and db/* modules copy that name, so rebinding it later reaches none of them.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL


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
        A function `write(jobs=..., detailed=...)` that writes whichever
        input frames a test needs. Keywords and blacklist are not files any
        more: tests pass them straight to the filter.
    """
    files = {
        "JOBS_CSV": tmp_path / "jobs.csv",
        "DETAILED_CSV": tmp_path / "detailed_jobs.csv",
    }

    for name, path in files.items():
        monkeypatch.setattr(paths, name, path)

    def write(jobs=None, detailed=None):
        if jobs is not None:
            jobs.to_csv(files["JOBS_CSV"], index=False)

        if detailed is not None:
            detailed.to_csv(files["DETAILED_CSV"], index=False)

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
    from db.tables import metadata

    # db.engine already read DATABASE_URL (set at the top of this file), so
    # every `from db.engine import engine` in db/* points at the test DB.
    test_engine = create_engine(TEST_DATABASE_URL)

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
