"""France Travail API source tests using mocked HTTP.

FakeSession here differs from test_board.py's: it needs to see
`data=` (the token POST is form-encoded) and `params=` (the search GET's
query string), which board scraping never sends.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qsl

import httpx
import pytest

from job_scraper import paths
from job_scraper.api_sources import france_travail
from job_scraper.api_sources.france_travail import TOKEN_URL, SEARCH_URL
from job_scraper.scrape_postings import fetch_job

pytestmark = pytest.mark.anyio


class FakeSession(httpx.AsyncClient):
    """An AsyncClient answering the token POST and the search GETs.

    `requests` records ("post", url, form) and ("get", url, params, headers).
    """

    def __init__(self, token_body, search_pages):
        """search_pages: list of JSON bodies, one per expected search call."""
        self.token_body = token_body
        self.search_pages = list(search_pages)
        self.requests = []
        super().__init__(transport=httpx.MockTransport(self._answer))

    def _answer(self, request):
        if request.method == "POST":
            url = str(request.url)
            form = dict(parse_qsl(request.content.decode()))
            self.requests.append(("post", url, form))
            body = self.token_body
        else:
            url = str(request.url).split("?")[0]
            params = dict(request.url.params)
            self.requests.append(
                ("get", url, params, dict(request.headers))
            )
            body = self.search_pages.pop(0) if self.search_pages else None

        return httpx.Response(
            200 if body is not None else 404,
            content=(body or "").encode(),
            headers={"Content-Type": "application/json"},
        )


@pytest.fixture(autouse=True)
def _reset_token_cache(monkeypatch):
    france_travail._token = None
    france_travail._warned.clear()
    # Tests run against a clean environment, whatever the developer exported.
    monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_ID", raising=False)
    monkeypatch.delenv("FRANCE_TRAVAIL_CLIENT_SECRET", raising=False)
    yield
    france_travail._token = None


@pytest.fixture
def credentials_file(tmp_path, monkeypatch):
    path = tmp_path / "france_travail_credentials.json"
    path.write_text(json.dumps({
        "client_id": "id", "client_secret": "secret",
    }))
    monkeypatch.setattr(paths, "FRANCE_TRAVAIL_CREDENTIALS", path)
    # The source is off with no keywords; most tests want it on.
    monkeypatch.setattr(france_travail, "SEARCH_KEYWORDS", ["python"])
    return path


def _offer(url="https://example.fr/offre/1", title="Dev"):
    return {
        "id": "1",
        "intitule": title,
        "description": "full posting text",
        "entreprise": {"nom": "Acme"},
        "lieuTravail": {"libelle": "Paris"},
        "origineOffre": {"urlOrigine": url},
    }


async def test_no_credentials_file_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setattr(
        paths, "FRANCE_TRAVAIL_CREDENTIALS", tmp_path / "missing.json"
    )
    session = FakeSession(token_body=None, search_pages=[])

    assert await france_travail.fetch_jobs(session) == []
    assert session.requests == []


async def test_fetches_and_maps_jobs(credentials_file, monkeypatch):
    monkeypatch.setattr(france_travail, "SEARCH_KEYWORDS", ["python"])
    session = FakeSession(
        token_body=json.dumps({"access_token": "tok", "expires_in": 3600}),
        search_pages=[json.dumps({"resultats": [_offer()]})],
    )

    jobs = await france_travail.fetch_jobs(session)

    assert len(jobs) == 1
    job = jobs[0]
    assert job.company == "Acme"
    assert job.title == "Dev"
    assert job.url == "https://example.fr/offre/1"
    assert job.place == "Paris"
    assert job.via == "france_travail"
    assert job.description == "full posting text"

    token_call = next(r for r in session.requests if r[0] == "post")
    assert token_call[1] == TOKEN_URL
    assert token_call[2]["client_id"] == "id"

    search_call = next(r for r in session.requests if r[0] == "get")
    assert search_call[1] == SEARCH_URL
    assert search_call[3]["authorization"] == "Bearer tok"


async def test_paginates_until_short_page(credentials_file, monkeypatch):
    monkeypatch.setattr(france_travail, "PAGE_SIZE", 1)
    session = FakeSession(
        token_body=json.dumps({"access_token": "tok", "expires_in": 3600}),
        search_pages=[
            json.dumps({"resultats": [_offer(url="https://x/1")]}),
            json.dumps({"resultats": []}),
        ],
    )

    jobs = await france_travail.fetch_jobs(session)

    assert [j.url for j in jobs] == ["https://x/1"]
    assert sum(1 for r in session.requests if r[0] == "get") == 2


async def test_token_is_cached_across_calls(credentials_file, monkeypatch):
    session = FakeSession(
        token_body=json.dumps({"access_token": "tok", "expires_in": 3600}),
        search_pages=[
            json.dumps({"resultats": []}),
            json.dumps({"resultats": []}),
        ],
    )

    await france_travail.fetch_jobs(session)
    await france_travail.fetch_jobs(session)

    assert sum(1 for r in session.requests if r[0] == "post") == 1


async def test_bad_token_response_returns_empty(credentials_file):
    session = FakeSession(token_body="not json", search_pages=[])

    assert await france_travail.fetch_jobs(session) == []


async def test_scrape_postings_shortcircuits_api_source_rows():
    row = {
        "url": "https://example.fr/offre/1",
        "title": "Dev",
        "description": "full posting text",
        "ats": "france_travail",
    }

    job = await fetch_job(row, None)

    assert job.description == "full posting text"
    assert job.via == "france_travail"


async def test_no_credentials_logs_once(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(paths, "FRANCE_TRAVAIL_CREDENTIALS",
                        tmp_path / "missing.json")
    monkeypatch.setattr(france_travail, "SEARCH_KEYWORDS", ["python"])
    session = FakeSession(token_body=None, search_pages=[])

    with caplog.at_level("WARNING"):
        await france_travail.fetch_jobs(session)
        await france_travail.fetch_jobs(session)

    assert [r.getMessage() for r in caplog.records
            if "no credentials" in r.getMessage()].__len__() == 1
    assert session.requests == []


async def test_empty_keywords_make_no_request_and_log_once(
        credentials_file, monkeypatch, caplog):
    monkeypatch.setattr(france_travail, "SEARCH_KEYWORDS", [])
    session = FakeSession(token_body=None, search_pages=[])

    with caplog.at_level("WARNING"):
        assert await france_travail.fetch_jobs(session) == []
        assert await france_travail.fetch_jobs(session) == []

    assert session.requests == []  # not even the token call
    assert sum("SEARCH_KEYWORDS" in r.getMessage()
               for r in caplog.records) == 1


async def test_credentials_come_from_the_environment(
        tmp_path, monkeypatch):
    monkeypatch.setattr(paths, "FRANCE_TRAVAIL_CREDENTIALS",
                        tmp_path / "missing.json")
    monkeypatch.setattr(france_travail, "SEARCH_KEYWORDS", ["python"])
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_ID", "env-id")
    monkeypatch.setenv("FRANCE_TRAVAIL_CLIENT_SECRET", "env-secret")
    session = FakeSession(
        token_body=json.dumps({"access_token": "tok", "expires_in": 3600}),
        search_pages=[json.dumps({"resultats": [_offer()]})],
    )

    jobs = await france_travail.fetch_jobs(session)

    token_call = next(r for r in session.requests if r[0] == "post")
    assert token_call[2]["client_id"] == "env-id"
    assert token_call[2]["client_secret"] == "env-secret"
    assert len(jobs) == 1
